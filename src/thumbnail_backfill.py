"""Put the channel's current thumbnail style on older videos, a few a day.

Older videos have no custom thumbnail (World Valor and SnuggleMoon were
blocked from 15 Oct to 2 Nov 2026), an older design, or a 16:9 card on a
Short, which the Shorts shelf crops (owner's request, 2026-11-04). YouTube
allows each channel only a few thumbnail changes a day, and on 4 Nov 2026 a bulk
run used that up so three new Ain News videos went out without one. So this
does at most PER_DAY old videos per channel a day, never on a channel whose
thumbnail limit is hit (pending_uploads.thumbnail_wait_reason) or that still
has a new video waiting for its thumbnail, and stops a channel at its first
refusal. Newest videos first; each is done once (table thumbnail_backfill).

Run with `python -m src.cli thumbnail-backfill` (the daily workflow does).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .db import connect

PER_DAY = 2
# Videos made from here on got the current styles at upload.
STYLE_START = "2026-11-03T16:00:00+00:00"
# Newer videos whose thumbnail must be drawn again, newest first, REDO_PER_DAY
# a day: channel -> (from, to, why). On 5 Nov 2026 TrendNerve Global's logo
# was uploaded to World Valor's brand card by mistake and drawn on every World
# Valor thumbnail until 16 Nov (the owner asked for them to be redone, 2-3 a
# day). Only once the channel has no logo on the runner, so the wrong one is
# never drawn again.
REDO = {
    "world_valor_chronicles": ("2026-11-05T07:00:00+00:00", "2026-11-16T23:59:59+00:00", "wrong logo"),
}
REDO_PER_DAY = 3


def _table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS thumbnail_backfill (
        video_id INTEGER PRIMARY KEY, channel TEXT NOT NULL, done_at TEXT NOT NULL, result TEXT)""")


def run(per_day: int = PER_DAY) -> list[str]:
    from .channels import load_channels
    from .lessons import load_lesson_for
    from .pending_uploads import thumbnail_wait_reason
    from .pipeline import _real_subject_thumbnail
    from .slots import SLOTS_UTC, channel_slot
    from .thumbnails import create_thumbnail
    from .view_stats import video_id as youtube_id
    from .youtube_accounts import channel_for_account
    from .youtube_upload import set_thumbnail

    today = datetime.now(timezone.utc).date().isoformat()
    messages: list[str] = []
    conn = connect()
    try:
        _table(conn)
        conn.commit()
        channels = {channel.id: channel for channel in load_channels()}
        for channel_id, channel in channels.items():
            if not channel.token_path.is_file():
                continue
            done_today = conn.execute(
                "SELECT COUNT(*) FROM thumbnail_backfill WHERE channel = ? AND done_at >= ? AND result = 'set'",
                (channel_id, today),
            ).fetchone()[0]
            redo = REDO.get(channel_id)
            if redo:
                from .brand import logo_path

                if logo_path(channel_id):
                    messages.append(f"[{channel_id}] redo ({redo[2]}) waits: the channel still has a logo "
                                    "(press Send to GitHub after removing it)")
                    redo = None
            room = (max(per_day, REDO_PER_DAY) if redo else per_day) - int(done_today)
            if room <= 0:
                continue
            # Only once today's own videos are made: their build slot began at
            # least 3 hours ago, so the old ones never use their thumbnail room.
            hour, minute = SLOTS_UTC[channel_slot(channel)]
            now = datetime.now(timezone.utc)
            if now.hour * 60 + now.minute < hour * 60 + minute + 180:
                continue
            todays_pending = conn.execute(
                "SELECT 1 FROM videos WHERE channel = ? AND created_at >= ? "
                "AND status IN ('daily_upload_pending', 'pending', 'rendered') LIMIT 1",
                (channel_id, today),
            ).fetchone()
            if todays_pending:
                continue
            waiting = thumbnail_wait_reason(channel_id)
            new_waiting = conn.execute(
                "SELECT 1 FROM videos WHERE channel = ? AND status = 'thumbnail_pending' "
                "AND created_at >= ? LIMIT 1", (channel_id, STYLE_START),
            ).fetchone()
            if waiting or new_waiting:
                messages.append(f"[{channel_id}] skipped: new videos come first ({waiting or 'one waits for its thumbnail'})")
                continue
            # Old-style videos, plus (first, being newer) any in the channel's redo window.
            start, end = (redo[0], redo[1]) if redo else ("", "")
            rows = conn.execute(
                """SELECT id, topic, COALESCE(content_type, 'short'), video_url, COALESCE(account, 'main')
                   FROM videos WHERE channel = ? AND video_url IS NOT NULL AND video_url != ''
                     AND (created_at < ? OR (created_at >= ? AND created_at <= ?))
                     AND status IN ('scheduled', 'uploaded', 'thumbnail_pending')
                     AND id NOT IN (SELECT video_id FROM thumbnail_backfill)
                   ORDER BY id DESC LIMIT ?""",
                (channel_id, STYLE_START, start or "9999", end or "0000", room),
            ).fetchall()
            for video_id, topic, content_type, url, account in rows:
                result = "set"
                try:
                    lesson = load_lesson_for(channel, topic)
                    run_dir = Path(settings.runs_dir) / f"backfill_{video_id}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    thumb = (_real_subject_thumbnail(lesson, channel, run_dir, content_type)
                             or create_thumbnail(lesson, None, run_dir / "thumbnail.jpg", channel, content_type))
                    target = channel_for_account(channel_id, account)
                    set_thumbnail(youtube_id(url), Path(thumb), client_secret_file=target.client_secret_path,
                                  token_file=target.token_path, channel_id=channel_id)
                    messages.append(f"[{channel_id}] {video_id}: thumbnail set")
                except Exception as exc:
                    text = str(exc).lower()
                    if "httperror 429" in text or "too many thumbnails" in text or "uploadratelimit" in text:
                        # Not counted as done; tried again on a later day.
                        messages.append(f"[{channel_id}] {video_id}: YouTube limit reached; stops for today")
                        break
                    result = f"failed: {str(exc)[:200]}"
                    messages.append(f"[{channel_id}] {video_id}: {result}")
                conn.execute(
                    "INSERT OR REPLACE INTO thumbnail_backfill (video_id, channel, done_at, result) VALUES (?, ?, ?, ?)",
                    (video_id, channel_id, datetime.now(timezone.utc).isoformat(), result),
                )
                conn.commit()
    finally:
        conn.close()
    return messages or ["nothing to backfill"]
