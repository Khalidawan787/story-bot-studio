from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings


def connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            video_path TEXT NOT NULL,
            thumbnail_path TEXT NOT NULL,
            upload_date TEXT,
            video_url TEXT,
            views INTEGER DEFAULT 0,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration: add per-channel column so genres are tracked separately.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()]
    if "channel" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN channel TEXT DEFAULT 'kids'")
    if "account" not in cols:
        # Which connected YouTube account of that channel the video belongs to.
        # Everything created before multi-account support belongs to "main".
        conn.execute("ALTER TABLE videos ADD COLUMN account TEXT NOT NULL DEFAULT 'main'")
    if "drive_url" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN drive_url TEXT")
    if "publish_at" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN publish_at TEXT")
    if "content_type" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN content_type TEXT DEFAULT 'short'")
    if "hidden_at" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN hidden_at TEXT")
    if "quality_report" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN quality_report TEXT")
    if "upload_progress" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN upload_progress INTEGER DEFAULT 0")
    if "upload_started_at" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN upload_started_at TEXT")
    if "is_daily" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN is_daily INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "UPDATE videos SET content_type = 'long' "
        "WHERE topic LIKE 'long_%' AND COALESCE(content_type, 'short') != 'long'"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS publish_slots (
            reservation_key TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            publish_at TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS used_images (
            image_hash TEXT PRIMARY KEY,
            channel TEXT NOT NULL,
            scene_label TEXT NOT NULL,
            image_path TEXT NOT NULL,
            used_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS upload_backoff (
            scope TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            retry_after TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS generation_locks (
            lock_name TEXT PRIMARY KEY,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analytics_cache (
            channel TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_preferences (
            channel TEXT PRIMARY KEY,
            approval_mode INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    pref_cols = [row[1] for row in conn.execute("PRAGMA table_info(channel_preferences)").fetchall()]
    if "auto_upload_queue" not in pref_cols:
        conn.execute("ALTER TABLE channel_preferences ADD COLUMN auto_upload_queue INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            used_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS social_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            status TEXT NOT NULL,
            remote_id TEXT,
            post_url TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(video_id, platform)
        )
        """
    )
    conn.commit()
    return conn


def save_video(
    job_id: str,
    topic: str,
    title: str,
    video_path: Path,
    thumbnail_path: Path,
    status: str,
    video_url: str | None = None,
    error: str | None = None,
    channel: str = "kids",
    publish_at: str | None = None,
    content_type: str = "short",
    quality_report: str | None = None,
    is_daily: bool = False,
    account: str = "main",
) -> int:
    conn = connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO videos (
                job_id, topic, title, video_path, thumbnail_path, upload_date,
                video_url, status, error, created_at, channel, publish_at, content_type, quality_report, is_daily,
                account
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                topic,
                title,
                str(video_path),
                str(thumbnail_path),
                datetime.now(timezone.utc).isoformat() if video_url else None,
                video_url,
                status,
                error,
                datetime.now(timezone.utc).isoformat(),
                channel,
                publish_at,
                content_type,
                quality_report,
                            1 if is_daily else 0,
                account or "main",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()




def record_api_usage(provider: str, operation: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO api_usage (provider, operation, used_at) VALUES (?, ?, ?)",
            (provider, operation, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def api_usage_today(provider: str, operation: str | None = None) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = connect()
    try:
        if operation:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_usage WHERE provider = ? AND operation = ? AND used_at >= ?",
                (provider, operation, start),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM api_usage WHERE provider = ? AND used_at >= ?",
                (provider, start),
            ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def approval_mode_enabled(channel: str) -> bool:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT approval_mode FROM channel_preferences WHERE channel = ?", (channel,)
        ).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def set_approval_mode(channel: str, enabled: bool) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO channel_preferences (channel, approval_mode) VALUES (?, ?)",
            (channel, 1 if enabled else 0),
        )
        conn.commit()
    finally:
        conn.close()


def auto_upload_queue_enabled(channel: str) -> bool:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT auto_upload_queue FROM channel_preferences WHERE channel = ?", (channel,)
        ).fetchone()
        return True if row is None else bool(row[0])
    finally:
        conn.close()


def set_auto_upload_queue(channel: str, enabled: bool) -> None:
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO channel_preferences (channel, approval_mode, auto_upload_queue)
               VALUES (?, 0, ?)
               ON CONFLICT(channel) DO UPDATE SET auto_upload_queue=excluded.auto_upload_queue""",
            (channel, 1 if enabled else 0),
        )
        conn.commit()
    finally:
        conn.close()


def clear_generation_lock(lock_name: str) -> None:
    release_generation_lock(lock_name)

def set_upload_backoff(scope: str, reason: str, hours: float = 24.0) -> str:
    retry_after = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO upload_backoff (scope, reason, retry_after) VALUES (?, ?, ?)",
            (scope, reason[:1000], retry_after),
        )
        conn.commit()
        return retry_after
    finally:
        conn.close()


def set_upload_backoff_until(scope: str, reason: str, retry_at: datetime) -> str:
    retry_after = retry_at.astimezone(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO upload_backoff (scope, reason, retry_after) VALUES (?, ?, ?)",
            (scope, reason[:1000], retry_after),
        )
        conn.commit()
        return retry_after
    finally:
        conn.close()


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7 + (occurrence - 1) * 7
    return first + timedelta(days=offset)


def _pacific_midnight_utc(day: date) -> datetime:
    # US Pacific DST begins at 02:00 on March's second Sunday and ends at
    # 02:00 on November's first Sunday. At midnight on those transition days,
    # the old offset still applies.
    dst_start = _nth_weekday(day.year, 3, 6, 2)
    dst_end = _nth_weekday(day.year, 11, 6, 1)
    offset_hours = -7 if dst_start < day <= dst_end else -8
    local_midnight = datetime(day.year, day.month, day.day, tzinfo=timezone(timedelta(hours=offset_hours)))
    return local_midnight.astimezone(timezone.utc)


def youtube_quota_day_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates = [
        _pacific_midnight_utc(now.date() + timedelta(days=delta))
        for delta in (-2, -1, 0, 1)
    ]
    return max(item for item in candidates if item <= now)


def next_youtube_quota_reset() -> datetime:
    now = datetime.now(timezone.utc)
    candidates = [
        _pacific_midnight_utc(now.date() + timedelta(days=delta))
        for delta in (-1, 0, 1, 2)
    ]
    return min(item for item in candidates if item > now)


def youtube_project_scope(channel: str) -> str:
    """Group quota backoff by OAuth project, not by the whole application."""
    try:
        from .channels import get_channel
        secret_path = get_channel(channel).client_secret_path
        digest = hashlib.sha256(secret_path.read_bytes()).hexdigest()[:24]
        return f"__youtube_project__:{digest}"
    except Exception:
        return channel


def active_upload_backoff(channel: str) -> tuple[str, str] | None:
    now = datetime.now(timezone.utc)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT scope, reason, retry_after FROM upload_backoff WHERE scope IN (?, ?, '__global__')",
            (channel, youtube_project_scope(channel)),
        ).fetchall()
        active: list[tuple[str, str]] = []
        for scope, reason, retry_after in rows:
            try:
                retry_time = datetime.fromisoformat(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
            except ValueError:
                retry_time = now
            if retry_time > now:
                active.append((reason, retry_after))
            else:
                conn.execute("DELETE FROM upload_backoff WHERE scope = ?", (scope,))
        conn.commit()
        return max(active, key=lambda item: item[1]) if active else None
    finally:
        conn.close()


def apply_upload_error_backoff(channel: str, error: str) -> str | None:
    lowered = error.lower()
    if "uploadlimitexceeded" in lowered or "daily upload limit" in lowered:
        return set_upload_backoff_until(channel, error, next_youtube_quota_reset())
    if "quotaexceeded" in lowered or "exceeded your quota" in lowered:
        return set_upload_backoff_until(youtube_project_scope(channel), error, next_youtube_quota_reset())
    return None


def acquire_generation_lock(lock_name: str, hours: float = 12.0) -> bool:
    """Compatibility hook: generation is never blocked; upload timing is handled separately."""
    return True

def release_generation_lock(lock_name: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM generation_locks WHERE lock_name = ?", (lock_name,))
        conn.commit()
    finally:
        conn.close()

def claim_unique_image(image_path: Path, channel: str, scene_label: str) -> bool:
    """Atomically claim image bytes; False means the same image was used before."""
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    conn = connect()
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO used_images (
                image_hash, channel, scene_label, image_path, used_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                digest, channel, scene_label, str(image_path),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()

def reserve_publish_slot(
    reservation_key: str,
    channel: str,
    interval_hours: float = 3.0,
    first_delay_hours: float = 1.0,
) -> str:
    """Atomically reserve this channel's next spaced publish time.

    The spacing is per channel, not global. A single global chain meant six
    channels shared one 3-hour slot line, so publish dates ran days into the
    future and videos uploaded today only went public much later. Each channel
    now keeps its own line, and with a small daily upload budget its releases
    stay inside the same day.
    """
    gap = max(2.0, min(3.0, float(interval_hours)))
    now = datetime.now(timezone.utc)
    base = now + timedelta(hours=max(0.0, float(first_delay_hours)))
    conn = connect()
    try:
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT publish_at FROM publish_slots WHERE reservation_key = ?",
            (reservation_key,),
        ).fetchone()
        if existing:
            conn.commit()
            return str(existing[0])
        row = conn.execute(
            """
            SELECT MAX(publish_at) FROM (
                SELECT publish_at FROM videos
                 WHERE publish_at IS NOT NULL AND channel = ?
                UNION ALL
                SELECT publish_at FROM publish_slots WHERE channel = ?
            )
            """,
            (channel, channel),
        ).fetchone()
        candidate = base
        if row and row[0]:
            last = datetime.fromisoformat(str(row[0]))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            candidate = max(base, last + timedelta(hours=gap))
        publish_at = candidate.isoformat()
        conn.execute(
            "INSERT INTO publish_slots (reservation_key, channel, publish_at, created_at) VALUES (?, ?, ?, ?)",
            (reservation_key, channel, publish_at, now.isoformat()),
        )
        conn.commit()
        return publish_at
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_publish_slot(reservation_key: str) -> None:
    """Release a slot when the video never reached YouTube."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM publish_slots WHERE reservation_key = ?",
            (reservation_key,),
        )


def latest_scheduled_publish_at(channel: str = "kids") -> str | None:
    """Most-future publishAt already reserved for this channel, so the next
    video can be queued after it (keeps a steady drip instead of a dump)."""
    if not settings.db_path.exists():
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT MAX(publish_at) FROM videos WHERE channel = ? AND publish_at IS NOT NULL",
            (channel,),
        ).fetchone()
    return row[0] if row else None


def mark_scheduled(video_id: int, video_url: str, publish_at: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE videos
            SET status = ?, video_url = ?, upload_date = ?, publish_at = ?, error = NULL, upload_progress = 100
            WHERE id = ?
            """,
            ("scheduled", video_url, datetime.now(timezone.utc).isoformat(), publish_at, video_id),
        )


def set_drive_url(video_id: int, drive_url: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE videos SET drive_url = ? WHERE id = ?", (drive_url, video_id))


def delete_video(video_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT video_path, thumbnail_path FROM videos WHERE id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))

    for raw_path in row:
        if raw_path:
            path = Path(raw_path)
            path.unlink(missing_ok=True)
    return True


def cleanup_uploaded_videos(days: int = 7) -> int:
    """Remove old dashboard records/local files; never touches YouTube."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, video_path, thumbnail_path
            FROM videos
            WHERE status IN ('uploaded', 'scheduled')
              AND upload_date IS NOT NULL
              AND upload_date <= ?
              AND hidden_at IS NULL
            """,
            (cutoff,),
        ).fetchall()
        if rows:
            hidden_at = datetime.now(timezone.utc).isoformat()
            conn.executemany(
                "UPDATE videos SET hidden_at = ? WHERE id = ?",
                [(hidden_at, row[0]) for row in rows],
            )
        conn.commit()
    finally:
        conn.close()

    for _video_id, video_raw, thumbnail_raw in rows:
        paths = [Path(video_raw), Path(thumbnail_raw)]
        if video_raw:
            paths.append(Path(video_raw).with_name("subtitles.srt"))
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            run_dir = Path(video_raw).parent.resolve()
            runs_root = settings.runs_dir.resolve()
            if runs_root in run_dir.parents and not any(run_dir.iterdir()):
                run_dir.rmdir()
        except OSError:
            pass
    return len(rows)


def mark_queued_for_upload(video_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE videos SET status = 'queued_for_upload', error = NULL, upload_progress = 0, upload_started_at = NULL WHERE id = ? AND video_url IS NULL",
            (video_id,),
        )
        conn.commit()
    finally:
        conn.close()

def mark_daily_upload_pending(video_id: int, error: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE videos
               SET status = 'daily_upload_pending', is_daily = 1, error = ?,
                   upload_progress = 0, upload_started_at = NULL
               WHERE id = ? AND video_url IS NULL""",
            (error, video_id),
        )


def set_upload_progress(video_id: int, progress: int) -> None:
    value = max(0, min(100, int(progress)))
    with connect() as conn:
        conn.execute(
            """UPDATE videos
               SET status = 'uploading', upload_progress = ?,
                   upload_started_at = COALESCE(upload_started_at, ?), error = NULL
               WHERE id = ? AND video_url IS NULL""",
            (value, datetime.now(timezone.utc).isoformat(), video_id),
        )


def mark_uploaded(video_id: int, video_url: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE videos
            SET status = ?, video_url = ?, upload_date = ?, error = NULL, upload_progress = 100
            WHERE id = ?
            """,
            ("uploaded", video_url, datetime.now(timezone.utc).isoformat(), video_id),
        )


def mark_upload_failed(video_id: int, error: str, video_url: str | None = None) -> None:
    with connect() as conn:
        if video_url:
            conn.execute(
                "UPDATE videos SET status = ?, error = ?, video_url = ?, upload_progress = 0 WHERE id = ?",
                ("upload_failed", error, video_url, video_id),
            )
        else:
            conn.execute(
                "UPDATE videos SET status = ?, error = ?, upload_progress = 0 WHERE id = ?",
                ("upload_failed", error, video_id),
            )


def mark_thumbnail_pending(video_id: int, video_url: str, error: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE videos
            SET status = ?, video_url = ?, upload_date = ?, error = ?
            WHERE id = ?
            """,
            ("thumbnail_pending", video_url, datetime.now(timezone.utc).isoformat(), error, video_id),
        )


def set_social_upload(
    video_id: int, platform: str, status: str,
    remote_id: str | None = None, post_url: str | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO social_uploads
                (video_id, platform, status, remote_id, post_url, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id, platform) DO UPDATE SET
                status=excluded.status, remote_id=excluded.remote_id,
                post_url=excluded.post_url, error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (video_id, platform, status, remote_id, post_url, error, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def social_uploads_for_channel(channel: str) -> dict[int, dict[str, dict[str, object]]]:
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT su.* FROM social_uploads su
               JOIN videos v ON v.id = su.video_id
               WHERE v.channel = ? ORDER BY su.updated_at DESC""",
            (channel,),
        ).fetchall()
    finally:
        conn.close()
    result: dict[int, dict[str, dict[str, object]]] = {}
    for row in rows:
        item = dict(row)
        result.setdefault(int(row["video_id"]), {})[str(row["platform"])] = item
    return result


def video_for_social_upload(video_id: int, channel: str | None = None) -> sqlite3.Row | None:
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        if channel:
            return conn.execute(
                """SELECT id, title, video_path, thumbnail_path, channel,
                          COALESCE(content_type,'short') AS content_type
                   FROM videos WHERE id = ? AND channel = ?""",
                (video_id, channel),
            ).fetchone()
        return conn.execute(
            """SELECT id, title, video_path, thumbnail_path, channel,
                      COALESCE(content_type,'short') AS content_type
               FROM videos WHERE id = ?""",
            (video_id,),
        ).fetchone()
    finally:
        conn.close()