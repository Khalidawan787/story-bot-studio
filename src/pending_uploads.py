from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import settings
from .channels import default_channel, get_channel
from .db import mark_scheduled, mark_thumbnail_pending, mark_upload_failed, mark_uploaded
from .lessons import load_lesson_for
from .schedule import next_publish_at
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


def pending_rows(limit: int = 20) -> list[sqlite3.Row]:
    if not settings.db_path.exists():
        return []
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT id, topic, video_path, thumbnail_path, video_url, status, error, upload_date, channel
            FROM videos
            WHERE status IN ('rendered', 'upload_failed', 'thumbnail_pending')
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def _thumbnail_rate_limited_today(row: sqlite3.Row) -> bool:
    error = (row["error"] or "").lower()
    if "uploadratelimitexceeded" not in error and "too many thumbnails" not in error:
        return False
    upload_date = row["upload_date"]
    if not upload_date:
        return False
    try:
        return datetime.fromisoformat(upload_date).date() == datetime.now(timezone.utc).date()
    except Exception:
        return False


def _process_row(row: sqlite3.Row) -> str:
    video_id = int(row["id"])
    video_path = Path(row["video_path"])
    thumbnail_path = Path(row["thumbnail_path"])
    channel = _channel_for(row["channel"])

    # SAFETY: never auto-open OAuth for a genre channel that has no token yet.
    # (This is what caused crime/love to land on the kids channel.) A genre
    # channel must be deliberately authorized once, to its OWN YouTube channel,
    # via `python -m src.cli authorize --channel <id>` before it can upload.
    if not channel.builtin and not channel.token_path.exists():
        return (f"{video_id}: skipped — '{channel.id}' not authorized. Create its own "
                f"YouTube channel, then run: authorize --channel {channel.id}")

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

        metadata = build_metadata(load_lesson_for(channel, row["topic"]), channel)
        publish_at = next_publish_at(channel.id)
        video_url = upload_video(video_path, thumbnail_path, metadata, channel=channel,
                                 publish_at=publish_at)
        if publish_at is not None:
            mark_scheduled(video_id, video_url, publish_at.isoformat())
            return f"{video_id}: scheduled for {publish_at.isoformat()} {video_url}"
        mark_uploaded(video_id, video_url)
        return f"{video_id}: uploaded {video_url}"
    except ThumbnailUploadError as exc:
        mark_thumbnail_pending(video_id, exc.video_url, str(exc))
        return f"{video_id}: video uploaded but thumbnail failed: {exc.video_url}"
    except Exception as exc:
        mark_upload_failed(video_id, str(exc))
        return f"{video_id}: failed {exc}"


def retry_pending_uploads(limit: int = 20) -> list[str]:
    return [_process_row(row) for row in pending_rows(limit)]


def _row_by_id(video_id: int) -> sqlite3.Row | None:
    if not settings.db_path.exists():
        return None
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT id, topic, video_path, thumbnail_path, video_url, status, error, upload_date, channel
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
