from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import settings
from .channels import default_channel, get_channel
from .db import (
    active_upload_backoff, apply_upload_error_backoff, mark_scheduled,
    mark_thumbnail_pending, mark_upload_failed, mark_uploaded,
)
from .lessons import load_lesson_for
from .schedule import cancel_publish_at, next_publish_at
from .seo import build_metadata
from .youtube_upload import ThumbnailUploadError, set_thumbnail, upload_video


def _channel_for(channel_id):
    try:
        return get_channel(channel_id or "kids")
    except Exception:
        return default_channel()


def _video_id_from_url(video_url: str) -> str:
    parsed = urlparse(video_url)
    if parsed.hostname and "youtu.be" in parsed.hostname:
        return parsed.path.strip("/")
    values = parse_qs(parsed.query).get("v", [])
    if values:
        return values[0]
    raise ValueError(f"Could not read video id from URL: {video_url}")


def pending_rows(
    limit: int = 20, channel_id: str | None = None,
    content_type: str | None = None,
) -> list[sqlite3.Row]:
    if not settings.db_path.exists():
        return []
    if content_type not in {None, "short", "long"}:
        raise ValueError("content_type must be short, long, or None")
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        where = ["status IN ('rendered', 'upload_failed', 'thumbnail_pending')"]
        params: list[object] = []
        if channel_id:
            where.append("channel = ?")
            params.append(channel_id)
        if content_type:
            where.append("COALESCE(content_type, 'short') = ?")
            params.append(content_type)
        params.append(limit)
        return conn.execute(
            f"""
            SELECT id, topic, video_path, thumbnail_path, video_url, status, error,
                   upload_date, channel, content_type
            FROM videos
            WHERE {' AND '.join(where)}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
    finally:
        conn.close()


def _thumbnail_rate_limited_today(row: sqlite3.Row) -> bool:
    error = (row["error"] or "").lower()
    limited = (
        "uploadratelimitexceeded" in error
        or "too many thumbnails" in error
        or "quotaexceeded" in error
        or ("exceeded your" in error and "quota" in error)
    )
    if not limited:
        return False
    upload_date = row["upload_date"]
    if not upload_date:
        return False
    try:
        return datetime.fromisoformat(upload_date).date() == datetime.now(timezone.utc).date()
    except Exception:
        return False


def _quota_day_start() -> datetime:
    """Start of the current YouTube-quota day. Quota resets at midnight Pacific;
    08:00 UTC (PST) is a safe, dependency-free approximation for a soft cap."""
    now = datetime.now(timezone.utc)
    boundary = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now < boundary:
        boundary -= timedelta(days=1)
    return boundary


def uploads_today_count() -> int:
    """How many videos have actually reached YouTube since the last quota reset."""
    if not settings.db_path.exists():
        return 0
    conn = sqlite3.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM videos WHERE video_url IS NOT NULL AND upload_date >= ?",
            (_quota_day_start().isoformat(),),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def daily_upload_cap_reached() -> bool:
    limit = settings.youtube_upload_daily_limit
    return limit > 0 and uploads_today_count() >= limit


def _process_row(row: sqlite3.Row) -> str:
    video_id = int(row["id"])
    video_path = Path(row["video_path"])
    thumbnail_path = Path(row["thumbnail_path"])
    channel = _channel_for(row["channel"])
    reservation_key = f"video:{video_id}"

    # SAFETY: never auto-open OAuth for a genre channel that has no token yet.
    # (This is what caused crime/love to land on the kids channel.) A genre
    # channel must be deliberately authorized once, to its OWN YouTube channel,
    # via `python -m src.cli authorize --channel <id>` before it can upload.
    if not channel.builtin and not channel.token_path.exists():
        return (f"{video_id}: skipped — '{channel.id}' not authorized. Create its own "
                f"YouTube channel, then run: authorize --channel {channel.id}")

    backoff = active_upload_backoff(channel.id)
    if backoff:
        reason, retry_after = backoff
        return f"{video_id}: upload paused until {retry_after}: {reason}"

    try:
        if row["status"] == "thumbnail_pending" and _thumbnail_rate_limited_today(row):
            return f"{video_id}: thumbnail rate-limited today, will retry later"
        if not thumbnail_path.exists():
            raise FileNotFoundError(f"Thumbnail missing: {thumbnail_path}")

        if row["video_url"]:
            youtube_id = _video_id_from_url(row["video_url"])
            set_thumbnail(youtube_id, thumbnail_path,
                          client_secret_file=channel.client_secret_path,
                          token_file=channel.token_path)
            mark_uploaded(video_id, row["video_url"])
            return f"{video_id}: thumbnail fixed, uploaded status restored"

        if not video_path.exists():
            raise FileNotFoundError(f"Video missing: {video_path}")

        # Safety cap: stop the unattended queue/retry once today's upload budget
        # is spent, so it never exhausts YouTube's daily quota. The video stays
        # pending and uploads automatically after the quota resets.
        if daily_upload_cap_reached():
            return (f"{video_id}: daily upload cap reached "
                    f"({settings.youtube_upload_daily_limit}/day) — resumes after the quota resets")

        metadata = build_metadata(
            load_lesson_for(channel, row["topic"]), channel,
            content_type=row["content_type"] or "short",
        )
        publish_at = next_publish_at(channel.id, reservation_key=reservation_key)
        video_url = upload_video(video_path, thumbnail_path, metadata, channel=channel,
                                 publish_at=publish_at)
        if publish_at is not None:
            mark_scheduled(video_id, video_url, publish_at.isoformat())
            return f"{video_id}: scheduled for {publish_at.isoformat()} {video_url}"
        mark_uploaded(video_id, video_url)
        return f"{video_id}: uploaded {video_url}"
    except ThumbnailUploadError as exc:
        apply_upload_error_backoff(channel.id, str(exc))
        mark_thumbnail_pending(video_id, exc.video_url, str(exc))
        return f"{video_id}: video uploaded but thumbnail failed: {exc.video_url}"
    except Exception as exc:
        cancel_publish_at(reservation_key)
        apply_upload_error_backoff(channel.id, str(exc))
        mark_upload_failed(video_id, str(exc))
        return f"{video_id}: failed {exc}"


def retry_pending_uploads(
    limit: int = 20, channel_id: str | None = None, content_type: str | None = None,
) -> list[str]:
    return [
        _process_row(row)
        for row in pending_rows(limit, channel_id=channel_id, content_type=content_type)
    ]


def _row_by_id(video_id: int) -> sqlite3.Row | None:
    if not settings.db_path.exists():
        return None
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT id, topic, video_path, thumbnail_path, video_url, status, error, upload_date, channel, content_type
            FROM videos WHERE id = ?
            """,
            (video_id,),
        ).fetchone()
    finally:
        conn.close()


def upload_one(video_id: int) -> str:
    """Upload / fix a single video by its DB id (used by the dashboard)."""
    row = _row_by_id(video_id)
    if row is None:
        return f"{video_id}: not found"
    return _process_row(row)
