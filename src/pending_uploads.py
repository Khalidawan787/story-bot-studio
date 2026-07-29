from __future__ import annotations

import os
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from .config import settings
from .channels import default_channel, get_channel
from .db import (
    active_upload_backoff, apply_upload_error_backoff, mark_scheduled,
    mark_thumbnail_pending, mark_upload_failed, mark_uploaded, set_upload_progress,
    mark_daily_upload_pending, youtube_quota_day_start,
)
from .lessons import load_lesson_for
from .schedule import cancel_publish_at, next_publish_at
from .seo import build_metadata
from .youtube_upload import ThumbnailUploadError, set_thumbnail, upload_video


# Serialize cap checks and upload starts across dashboard background jobs.
_UPLOAD_GATE = threading.Lock()


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
        where = ["status IN ('rendered', 'upload_failed', 'thumbnail_pending', 'daily_upload_pending')"]
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
                   upload_date, publish_at, channel, content_type, COALESCE(is_daily, 0) AS is_daily
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
    """Start of the current YouTube quota day (midnight Pacific)."""
    return youtube_quota_day_start()
def upload_limit_for_channel(channel_id: str) -> int:
    """Return this channel's unattended daily upload cap."""
    key = f"YOUTUBE_UPLOAD_DAILY_LIMIT_{channel_id.upper().replace('-', '_')}"
    raw = os.getenv(key, "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return max(0, int(settings.youtube_upload_daily_limit))


def uploads_today_count(channel_id: str | None = None) -> int:
    """How many videos reached YouTube for one channel since quota reset."""
    if not settings.db_path.exists():
        return 0
    conn = sqlite3.connect(settings.db_path)
    try:
        sql = "SELECT COUNT(*) FROM videos WHERE video_url IS NOT NULL AND upload_date >= ?"
        params: list[object] = [_quota_day_start().isoformat()]
        if channel_id:
            sql += " AND channel = ?"
            params.append(channel_id)
        row = conn.execute(sql, params).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def daily_upload_cap_reached(channel_id: str) -> bool:
    limit = upload_limit_for_channel(channel_id)
    return limit > 0 and uploads_today_count(channel_id) >= limit


def _file_sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _topic_scene_signature(channel_id: str, topic: str) -> set[str]:
    try:
        lesson = load_lesson_for(_channel_for(channel_id), topic)
    except Exception:
        return set()
    result: set[str] = set()
    for scene in lesson.scenes:
        value = str(getattr(scene, "label", "") or getattr(scene, "line", "")).lower()
        normalized = " ".join(
            "".join(ch if ch.isalnum() else " " for ch in value).split()
        )
        if normalized:
            result.add(normalized)
    return result


def _near_duplicate_signature(left: set[str], right: set[str]) -> bool:
    return bool(left and right) and len(left & right) / min(len(left), len(right)) >= 0.66

def _youtube_url_available(video_url: str, status: str, publish_at: str | None) -> bool | None:
    """Verify a public YouTube URL without consuming Data API quota."""
    if str(status) == "scheduled" and publish_at:
        return True
    from urllib.error import HTTPError, URLError
    from urllib.parse import quote
    from urllib.request import Request, urlopen

    endpoint = "https://www.youtube.com/oembed?format=json&url=" + quote(str(video_url), safe="")
    try:
        request = Request(endpoint, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=10) as response:
            return int(getattr(response, "status", 200)) == 200
    except HTTPError as exc:
        if int(exc.code) in {401, 403, 404}:
            return False
        return None
    except (URLError, TimeoutError, OSError):
        return None


def _existing_copy_is_valid(existing: sqlite3.Row) -> bool:
    available = _youtube_url_available(
        str(existing["video_url"]), str(existing["status"] or ""),
        existing["publish_at"],
    )
    return available is not False

def find_uploaded_duplicate(
    channel_id: str, topic: str, content_type: str, video_path: Path,
    exclude_video_id: int | None = None,
) -> tuple[int, str, str] | None:
    """Return an existing YouTube copy of this topic or exact MP4."""
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, topic, video_path, video_url
               FROM videos
               WHERE channel = ? AND COALESCE(content_type, 'short') = ?
                 AND video_url IS NOT NULL AND status != 'remote_missing'
                 AND id != COALESCE(?, -1)
               ORDER BY id DESC""",
            (channel_id, content_type or "short", exclude_video_id),
        ).fetchall()
    finally:
        conn.close()
    for existing in rows:
        if str(existing["topic"]) == str(topic):
            return int(existing["id"]), str(existing["video_url"]), "same topic"
    current_signature = _topic_scene_signature(channel_id, topic)
    for existing in rows:
        old_signature = _topic_scene_signature(channel_id, str(existing["topic"]))
        if _near_duplicate_signature(current_signature, old_signature) and _existing_copy_is_valid(existing):
            return (
                int(existing["id"]), str(existing["video_url"]),
                "near-duplicate scenes",
            )
    if not video_path.is_file():
        return None
    current_size = video_path.stat().st_size
    current_hash = None
    for existing in rows:
        old_path = Path(existing["video_path"] or "")
        if not old_path.is_file() or old_path.stat().st_size != current_size:
            continue
        if current_hash is None:
            current_hash = _file_sha256(video_path)
        if _file_sha256(old_path) == current_hash and _existing_copy_is_valid(existing):
            return int(existing["id"]), str(existing["video_url"]), "identical MP4"
    return None


def mark_duplicate_blocked(video_id: int, duplicate: tuple[int, str, str]) -> None:
    old_id, old_url, reason = duplicate
    message = f"Duplicate upload blocked ({reason}); existing video #{old_id}: {old_url}"
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            """UPDATE videos SET status = 'duplicate_blocked', error = ?,
                      upload_progress = 0, upload_started_at = NULL
               WHERE id = ? AND video_url IS NULL""",
            (message, video_id),
        )
        conn.commit()
    finally:
        conn.close()

def thumbnail_permission_denied(error: str | None) -> bool:
    text = str(error or "").lower()
    return "doesn't have permissions to upload and set custom video thumbnails" in text


_THUMBNAIL_DISABLED_PATH = settings.root / "data" / "thumbnail_disabled_channels.json"


def _thumbnail_disabled_channels() -> set[str]:
    import json
    try:
        raw = json.loads(_THUMBNAIL_DISABLED_PATH.read_text(encoding="utf-8"))
        return {str(item) for item in raw if str(item)}
    except Exception:
        return set()


def disable_thumbnail_upload(channel_id: str) -> None:
    import json
    with _UPLOAD_GATE:
        disabled = _thumbnail_disabled_channels()
        disabled.add(str(channel_id))
        _THUMBNAIL_DISABLED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _THUMBNAIL_DISABLED_PATH.write_text(
            json.dumps(sorted(disabled), indent=2), encoding="utf-8",
        )

_THUMBNAIL_REENABLED_PATH = settings.root / "data" / "thumbnail_reenabled_channels.json"


def _thumbnail_reenabled_at(channel_id: str) -> str | None:
    """When the channel was re-verified, so older refusals stop counting."""
    import json
    try:
        raw = json.loads(_THUMBNAIL_REENABLED_PATH.read_text(encoding="utf-8"))
        value = raw.get(str(channel_id))
        return str(value) if value else None
    except Exception:
        return None


def enable_thumbnail_upload(channel_id: str) -> None:
    """Turn custom thumbnails back on after verifying the channel on YouTube.

    YouTube only allows custom thumbnails on phone-verified channels
    (youtube.com/verify). Once that is done this clears both the disabled flag
    and the historical "no permission" errors that would otherwise keep
    thumbnails off forever.
    """
    import json
    from datetime import datetime, timezone

    with _UPLOAD_GATE:
        disabled = _thumbnail_disabled_channels()
        disabled.discard(str(channel_id))
        _THUMBNAIL_DISABLED_PATH.parent.mkdir(parents=True, exist_ok=True)
        _THUMBNAIL_DISABLED_PATH.write_text(
            json.dumps(sorted(disabled), indent=2), encoding="utf-8",
        )
        try:
            raw = json.loads(_THUMBNAIL_REENABLED_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raw = {}
        except Exception:
            raw = {}
        raw[str(channel_id)] = datetime.now(timezone.utc).isoformat()
        _THUMBNAIL_REENABLED_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def thumbnail_upload_allowed(channel_id: str) -> bool:
    """False after YouTube has confirmed that this channel cannot set thumbnails."""
    if str(channel_id) in _thumbnail_disabled_channels():
        return False
    since = _thumbnail_reenabled_at(channel_id)
    conn = sqlite3.connect(settings.db_path)
    try:
        needle = "%doesn't have permissions to upload and set custom video thumbnails%"
        if since:
            row = conn.execute(
                """SELECT 1 FROM videos
                   WHERE channel = ? AND lower(COALESCE(error, '')) LIKE ?
                     AND COALESCE(upload_date, '') > ?
                   LIMIT 1""",
                (channel_id, needle, since),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT 1 FROM videos
                   WHERE channel = ? AND lower(COALESCE(error, '')) LIKE ?
                   LIMIT 1""",
                (channel_id, needle),
            ).fetchone()
        return row is None
    finally:
        conn.close()

def _process_row(row: sqlite3.Row, *, bypass_safety_cap: bool = False) -> str:
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

    if row["video_url"] and thumbnail_permission_denied(row["error"]):
        disable_thumbnail_upload(channel.id)
        if row["publish_at"]:
            mark_scheduled(video_id, row["video_url"], row["publish_at"])
        else:
            mark_uploaded(video_id, row["video_url"])
        return f"{video_id}: custom thumbnail skipped; video is already on YouTube"
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

        duplicate = find_uploaded_duplicate(
            channel.id, str(row["topic"]), str(row["content_type"] or "short"),
            video_path, exclude_video_id=video_id,
        )
        if duplicate:
            mark_duplicate_blocked(video_id, duplicate)
            return f"{video_id}: duplicate upload blocked; existing video: {duplicate[1]}"
        # Safety cap: stop the unattended queue/retry once today's upload budget
        # is spent, so it never exhausts YouTube's daily quota. The video stays
        # pending and uploads automatically after the quota resets.
        with _UPLOAD_GATE:
            if not bypass_safety_cap and daily_upload_cap_reached(channel.id):
                return (f"{video_id}: {channel.name} app upload cap reached "
                        f"({upload_limit_for_channel(channel.id)}/day for this channel) "
                        "— resumes after the YouTube quota day resets")

            metadata = build_metadata(
                load_lesson_for(channel, row["topic"]), channel,
                content_type=row["content_type"] or "short",
            )
            publish_at = next_publish_at(channel.id, reservation_key=reservation_key)
            set_upload_progress(video_id, 0)
            video_url = upload_video(
                video_path, thumbnail_path, metadata, channel=channel,
                publish_at=publish_at,
                progress_callback=lambda value: set_upload_progress(video_id, value),
                upload_thumbnail=thumbnail_upload_allowed(channel.id),
            )
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
        if bool(row["is_daily"]):
            mark_daily_upload_pending(video_id, str(exc))
        else:
            mark_upload_failed(video_id, str(exc))
        return f"{video_id}: failed {exc}"


def retry_pending_uploads(
    limit: int = 20, channel_id: str | None = None, content_type: str | None = None,
) -> list[str]:
    return [
        _process_row(row)
        for row in pending_rows(limit, channel_id=channel_id, content_type=content_type)
    ]


def daily_pending_rows(limit: int = 20) -> list[sqlite3.Row]:
    if not settings.db_path.exists():
        return []
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """SELECT id, topic, video_path, thumbnail_path, video_url, status, error,
                      upload_date, publish_at, channel, content_type, COALESCE(is_daily, 0) AS is_daily
               FROM videos
               WHERE COALESCE(is_daily, 0) = 1
                 AND status IN ('daily_upload_pending', 'thumbnail_pending')
                 AND hidden_at IS NULL
               ORDER BY created_at ASC, id ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def has_daily_pending() -> bool:
    return bool(daily_pending_rows(limit=1))


def retry_daily_uploads(limit: int = 20) -> list[str]:
    return [_process_row(row) for row in daily_pending_rows(limit=limit)]


def _row_by_id(video_id: int) -> sqlite3.Row | None:
    if not settings.db_path.exists():
        return None
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT id, topic, video_path, thumbnail_path, video_url, status, error, upload_date, publish_at, channel, content_type, COALESCE(is_daily, 0) AS is_daily
            FROM videos WHERE id = ?
            """,
            (video_id,),
        ).fetchone()
    finally:
        conn.close()


def _published_rows(limit: int, channel_id: str | None = None) -> list[sqlite3.Row]:
    if not settings.db_path.exists():
        return []
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        where = ["video_url IS NOT NULL", "video_url <> ''", "hidden_at IS NULL"]
        params: list[object] = []
        if channel_id:
            where.append("channel = ?")
            params.append(channel_id)
        params.append(limit)
        return conn.execute(
            f"""SELECT id, topic, title, video_path, thumbnail_path, video_url, status,
                       error, upload_date, publish_at, channel, content_type,
                       COALESCE(is_daily, 0) AS is_daily
                FROM videos WHERE {' AND '.join(where)}
                ORDER BY id DESC LIMIT ?""",
            params,
        ).fetchall()
    finally:
        conn.close()


def refresh_thumbnails(limit: int = 20, channel_id: str | None = None,
                       regenerate: bool = True) -> list[str]:
    """Build a fresh AI thumbnail for videos that are already on YouTube and set it.

    This is the catch-up path for videos published before thumbnails worked:
    nothing is re-uploaded, only the thumbnail image is replaced.
    """
    from .thumbnails import create_thumbnail

    messages: list[str] = []
    for row in _published_rows(limit, channel_id=channel_id):
        video_id = int(row["id"])
        channel = _channel_for(row["channel"])
        if not thumbnail_upload_allowed(channel.id):
            messages.append(
                f"{video_id}: skipped — custom thumbnails are off for '{channel.id}'. "
                f"Verify that channel at youtube.com/verify, then run: "
                f"python -m src.cli enable-thumbnails --channel {channel.id}"
            )
            continue
        if not channel.builtin and not channel.token_path.exists():
            messages.append(f"{video_id}: skipped — '{channel.id}' is not authorized yet")
            continue
        thumbnail_path = Path(row["thumbnail_path"])
        try:
            if regenerate or not thumbnail_path.exists():
                thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
                lesson = load_lesson_for(channel, row["topic"])
                create_thumbnail(
                    lesson, None, thumbnail_path, channel,
                    str(row["content_type"] or "short"),
                )
            set_thumbnail(
                _video_id_from_url(row["video_url"]), thumbnail_path,
                client_secret_file=channel.client_secret_path,
                token_file=channel.token_path,
            )
            messages.append(f"{video_id}: thumbnail updated — {row['video_url']}")
        except Exception as exc:
            if thumbnail_permission_denied(str(exc)):
                disable_thumbnail_upload(channel.id)
                messages.append(
                    f"{video_id}: '{channel.id}' cannot set custom thumbnails yet. "
                    "Verify the channel at youtube.com/verify and try again."
                )
                continue
            messages.append(f"{video_id}: failed — {exc}")
    return messages


def upload_one(video_id: int, *, bypass_safety_cap: bool = False) -> str:
    """Upload / fix a single video by its DB id (used by the dashboard)."""
    row = _row_by_id(video_id)
    if row is None:
        return f"{video_id}: not found"
    return _process_row(row, bypass_safety_cap=bypass_safety_cap)
