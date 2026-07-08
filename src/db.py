from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
    if "drive_url" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN drive_url TEXT")
    if "publish_at" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN publish_at TEXT")
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
) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO videos (
                job_id, topic, title, video_path, thumbnail_path, upload_date,
                video_url, status, error, created_at, channel, publish_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        return int(cur.lastrowid)


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
            SET status = ?, video_url = ?, upload_date = ?, publish_at = ?, error = NULL
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


def mark_uploaded(video_id: int, video_url: str) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE videos
            SET status = ?, video_url = ?, upload_date = ?, error = NULL
            WHERE id = ?
            """,
            ("uploaded", video_url, datetime.now(timezone.utc).isoformat(), video_id),
        )


def mark_upload_failed(video_id: int, error: str, video_url: str | None = None) -> None:
    with connect() as conn:
        if video_url:
            conn.execute(
                "UPDATE videos SET status = ?, error = ?, video_url = ? WHERE id = ?",
                ("upload_failed", error, video_url, video_id),
            )
        else:
            conn.execute(
                "UPDATE videos SET status = ?, error = ? WHERE id = ?",
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
