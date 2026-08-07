"""Immediate YouTube uploader.

The old design held finished videos in a timed queue and released at most one
video every 2-3 hours globally, across every channel. With six channels that
meant videos piled up faster than the queue drained and nothing reached
YouTube for days.

There is no waiting queue any more: as soon as a video is rendered it uploads,
and the only thing that holds a video back is that channel's own daily upload
limit (`YOUTUBE_UPLOAD_DAILY_LIMIT_<CHANNEL>`). Each channel gets its own
budget every quota day, so every channel publishes daily.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .channels import get_channel
from .config import settings
from .db import active_upload_backoff
from .pending_uploads import (
    daily_upload_cap_reached, upload_limit_for_channel, upload_one,
    uploads_today_count,
)

_WORKER_LOCK = threading.Lock()

# Statuses that mean "finished rendering, still not on YouTube".
PENDING_STATUSES = (
    "rendered", "upload_failed", "daily_upload_pending",
    "thumbnail_pending", "queued_for_upload",
)


def _interval() -> timedelta:
    """No global spacing any more — kept so old callers keep working."""
    return timedelta(0)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError:
        return None


def last_global_upload() -> datetime | None:
    conn = sqlite3.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT MAX(upload_date) FROM videos WHERE video_url IS NOT NULL AND upload_date IS NOT NULL"
        ).fetchone()
        return _parse(row[0] if row else None)
    finally:
        conn.close()


def next_allowed_upload() -> datetime:
    """Uploads are never time-gated now, so the next slot is always 'now'."""
    return datetime.now(timezone.utc)


def _pending_rows() -> list[sqlite3.Row]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        return conn.execute(
            f"""SELECT id, channel, COALESCE(account, 'main') AS account FROM videos
                WHERE status IN ({placeholders})
                  AND video_url IS NULL AND hidden_at IS NULL
                ORDER BY created_at ASC, id ASC""",
            PENDING_STATUSES,
        ).fetchall()
    finally:
        conn.close()


def queue_snapshot(channel_id: str) -> dict[str, object]:
    conn = sqlite3.connect(settings.db_path)
    try:
        placeholders = ",".join("?" for _ in PENDING_STATUSES)
        total = conn.execute(
            f"""SELECT COUNT(*) FROM videos
                WHERE channel = ? AND status IN ({placeholders})
                  AND video_url IS NULL AND hidden_at IS NULL""",
            (channel_id, *PENDING_STATUSES),
        ).fetchone()[0]
        unuploaded = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE channel = ? AND video_url IS NULL AND hidden_at IS NULL",
            (channel_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    limit = upload_limit_for_channel(channel_id)
    used = uploads_today_count(channel_id)
    return {
        "enabled": True,
        "count": int(total),
        "unuploaded_count": int(unuploaded),
        "next_at": next_allowed_upload().isoformat(),
        "gap_hours": 0.0,
        "daily_limit": limit,
        "used_today": used,
        "remaining_today": max(0, limit - used) if limit else 0,
    }


def expected_upload_times() -> dict[int, str]:
    """Everything pending uploads on the next worker pass, budget permitting."""
    now = next_allowed_upload().isoformat()
    return {int(row["id"]): now for row in _pending_rows()}


def _uploadable(channel_id: str, account_id: str = "main") -> bool:
    """True when this YouTube account can still take another upload today."""
    if daily_upload_cap_reached(channel_id, account_id):
        return False
    if active_upload_backoff(channel_id):
        return False
    try:
        from .youtube_accounts import get_account

        return get_account(channel_id, account_id).connected
    except Exception:
        pass
    try:
        return get_channel(channel_id).token_path.exists()
    except Exception:
        return False


def due_video_id() -> int | None:
    for row in _pending_rows():
        if _uploadable(str(row["channel"] or "kids"), str(row["account"] or "main")):
            return int(row["id"])
    return None


def process_next_upload() -> str:
    """Upload every pending video whose account still has budget left today."""
    if not _WORKER_LOCK.acquire(blocking=False):
        return "Auto-upload worker is already running"
    try:
        messages: list[str] = []
        for row in _pending_rows():
            if not _uploadable(str(row["channel"] or "kids"), str(row["account"] or "main")):
                continue
            messages.append(upload_one(int(row["id"])))
        return "; ".join(messages) if messages else "Nothing pending to upload"
    finally:
        _WORKER_LOCK.release()
