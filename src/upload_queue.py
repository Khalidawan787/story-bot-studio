from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from .channels import get_channel
from .config import settings
from .db import active_upload_backoff, auto_upload_queue_enabled
from .pending_uploads import upload_one

_WORKER_LOCK = threading.Lock()


def _interval() -> timedelta:
    hours = max(2.0, min(3.0, float(settings.youtube_schedule_interval_hours)))
    return timedelta(hours=hours)


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
    last = last_global_upload()
    now = datetime.now(timezone.utc)
    return max(now, last + _interval()) if last else now


def queue_snapshot(channel_id: str) -> dict[str, object]:
    conn = sqlite3.connect(settings.db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE channel = ? AND status = 'queued_for_upload'",
            (channel_id,),
        ).fetchone()[0]
        unuploaded = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE channel = ? AND video_url IS NULL AND hidden_at IS NULL",
            (channel_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "enabled": auto_upload_queue_enabled(channel_id),
        "count": int(total),
        "unuploaded_count": int(unuploaded),
        "next_at": next_allowed_upload().isoformat(),
        "gap_hours": _interval().total_seconds() / 3600,
    }


def due_video_id() -> int | None:
    if datetime.now(timezone.utc) < next_allowed_upload():
        return None
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, channel FROM videos
               WHERE status = 'queued_for_upload' AND hidden_at IS NULL
               ORDER BY created_at ASC, id ASC"""
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        channel_id = str(row["channel"] or "kids")
        if not auto_upload_queue_enabled(channel_id):
            continue
        if active_upload_backoff(channel_id):
            continue
        try:
            channel = get_channel(channel_id)
        except Exception:
            continue
        if not channel.token_path.exists():
            continue
        return int(row["id"])
    return None


def process_next_upload() -> str:
    if not _WORKER_LOCK.acquire(blocking=False):
        return "Auto-upload worker is already running"
    try:
        video_id = due_video_id()
        if video_id is None:
            return "No queued video is due yet"
        return upload_one(video_id)
    finally:
        _WORKER_LOCK.release()