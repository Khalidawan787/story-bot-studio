"""The weekly scorecard: each channel's Shorts against its own recent past
(the owner's choice, 2026-11-17, from the growth plan V2).

Per video (table video_metrics, filled by the daily run):
    views at 24 h and 72 h after it went public (read from the view counts the
    run already refreshes, at the first run after each mark)
    average % viewed, shares and subscribers gained (YouTube Analytics, for the
    channels whose Analytics login is connected on the dashboard)

Per channel: the week of Shorts that went public 3-10 days ago (all past their
72-hour mark; a younger one is "pending", growth plan V3) against the 28 days
before that, as MEDIANS (one lucky video does not move a median), plus shares
and subscribers per 1,000 views, and the hook test (hook_tests.py): the same
numbers for each opening style.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from .db import connect


def _table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS video_metrics (
        video_id INTEGER PRIMARY KEY, channel TEXT, youtube_id TEXT,
        views_24h INTEGER, views_72h INTEGER, avg_pct REAL, avg_seconds REAL,
        shares INTEGER, subs INTEGER, analytics_views INTEGER, updated_at TEXT)""")


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _public_at(row) -> datetime | None:
    """When the video went (or goes) public: its booked time, else its upload time."""
    return _when(row["publish_at"]) or _when(row["upload_date"])


def _youtube_id(url: str | None) -> str:
    match = re.search(r"(?:v=|shorts/|youtu\.be/)([\w-]{11})", str(url or ""))
    return match.group(1) if match else ""


def mark_views() -> int:
    """After refresh-views: store each video's views at its 24 h and 72 h marks. Returns how many."""
    now = datetime.now(timezone.utc)
    conn = connect()
    conn.row_factory = sqlite3.Row
    marked = 0
    try:
        _table(conn)
        rows = conn.execute(
            """SELECT v.id, v.channel, v.video_url, v.views, v.publish_at, v.upload_date,
                      m.views_24h, m.views_72h
               FROM videos v LEFT JOIN video_metrics m ON m.video_id = v.id
               WHERE v.video_url IS NOT NULL AND v.video_url != '' AND v.views IS NOT NULL
                 AND (m.views_72h IS NULL)""").fetchall()
        for row in rows:
            public = _public_at(row)
            if public is None:
                continue
            age = now - public
            updates = {}
            if age >= timedelta(hours=24) and row["views_24h"] is None and age < timedelta(hours=48):
                updates["views_24h"] = row["views"]
            if age >= timedelta(hours=72) and age < timedelta(days=6):
                updates["views_72h"] = row["views"]
            if not updates:
                continue
            conn.execute(
                "INSERT INTO video_metrics (video_id, channel, youtube_id, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(video_id) DO NOTHING",
                (row["id"], row["channel"], _youtube_id(row["video_url"]), now.isoformat()))
            for column, value in updates.items():
                conn.execute(f"UPDATE video_metrics SET {column} = ?, updated_at = ? WHERE video_id = ?",
                             (value, now.isoformat(), row["id"]))
            marked += 1
        conn.commit()
    finally:
        conn.close()
    return marked


def fetch_analytics(days: int = 35) -> list[str]:
    """% viewed, shares and subscribers per video, for channels with an Analytics login."""
    from googleapiclient.discovery import build

    from .channels import load_channels
    from .youtube_analytics import AnalyticsPermissionRequired, _credentials, is_connected

    messages = []
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).isoformat()
    for channel in load_channels():
        if not channel.token_path.is_file():
            continue
        if not is_connected(channel):
            messages.append(f"[{channel.id}] no Analytics login: connect it on the dashboard (Analytics & Queue)")
            continue
        conn = connect()
        try:
            _table(conn)
            rows = conn.execute(
                "SELECT id, video_url FROM videos WHERE channel = ? AND video_url IS NOT NULL "
                "AND COALESCE(publish_at, upload_date) >= ?", (channel.id, since)).fetchall()
        finally:
            conn.close()
        ids = {_youtube_id(url): vid for vid, url in rows if _youtube_id(url)}
        if not ids:
            continue
        try:
            service = build("youtubeAnalytics", "v2", credentials=_credentials(channel), cache_discovery=False)
            got = 0
            wanted = list(ids)
            for start in range(0, len(wanted), 150):
                batch = wanted[start:start + 150]
                response = service.reports().query(
                    ids="channel==MINE", startDate=(now - timedelta(days=days + 2)).date().isoformat(),
                    endDate=now.date().isoformat(),
                    metrics="views,averageViewPercentage,averageViewDuration,shares,subscribersGained",
                    dimensions="video", filters="video==" + ",".join(batch), maxResults=200,
                ).execute()
                headers = [h["name"] for h in response.get("columnHeaders", [])]
                conn = connect()
                try:
                    for values in response.get("rows") or []:
                        data = dict(zip(headers, values))
                        vid = ids.get(str(data.get("video")))
                        if vid is None:
                            continue
                        conn.execute(
                            "INSERT INTO video_metrics (video_id, channel, youtube_id, updated_at) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(video_id) DO NOTHING", (vid, channel.id, data["video"], now.isoformat()))
                        conn.execute(
                            "UPDATE video_metrics SET avg_pct = ?, avg_seconds = ?, shares = ?, subs = ?, "
                            "analytics_views = ?, updated_at = ? WHERE video_id = ?",
                            (data.get("averageViewPercentage"), data.get("averageViewDuration"),
                             int(data.get("shares") or 0), int(data.get("subscribersGained") or 0),
                             int(data.get("views") or 0), now.isoformat(), vid))
                        got += 1
                    conn.commit()
                finally:
                    conn.close()
            messages.append(f"[{channel.id}] analytics for {got} of {len(ids)} videos")
        except AnalyticsPermissionRequired as exc:
            messages.append(f"[{channel.id}] {exc}")
        except Exception as exc:
            messages.append(f"[{channel.id}] analytics failed: {str(exc)[:160]}")
    return messages


def _median(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _summary(rows: list[dict]) -> dict:
    views = sum(r["views"] or 0 for r in rows)
    a_views = sum(r["analytics_views"] or 0 for r in rows)
    shares = sum(r["shares"] or 0 for r in rows)
    subs = sum(r["subs"] or 0 for r in rows)
    return {
        "videos": len(rows),
        "median_views": _median([r["views"] for r in rows]),
        "median_72h": _median([r["views_72h"] for r in rows]),
        "median_pct": _median([r["avg_pct"] for r in rows]),
        "shares_per_1k": round(1000 * shares / a_views, 1) if a_views else None,
        "subs_per_1k": round(1000 * subs / a_views, 1) if a_views else None,
        "total_views": views,
    }


def build(now: datetime | None = None) -> list[dict]:
    """One entry per channel with Shorts: this week, the 28 days before, and the hook test."""
    from .channels import load_channels
    from .hook_tests import style_of

    now = now or datetime.now(timezone.utc)
    mature = now - timedelta(hours=72)          # younger Shorts are pending
    week_start, base_start = mature - timedelta(days=7), mature - timedelta(days=35)
    names = {c.id: c.name for c in load_channels()}
    conn = connect()
    conn.row_factory = sqlite3.Row
    try:
        _table(conn)
        rows = [dict(r) for r in conn.execute(
            """SELECT v.id, v.channel, v.topic, v.title, v.views, v.publish_at, v.upload_date,
                      m.views_24h, m.views_72h, m.avg_pct, m.shares, m.subs, m.analytics_views
               FROM videos v LEFT JOIN video_metrics m ON m.video_id = v.id
               WHERE v.video_url IS NOT NULL AND v.video_url != ''
                 AND COALESCE(v.content_type, 'short') = 'short'""")]
        for row in rows:
            row["hook"] = style_of(conn, row["channel"], str(row["topic"] or ""))
    finally:
        conn.close()
    cards = []
    for channel_id in sorted({r["channel"] for r in rows}):
        mine = [r for r in rows if r["channel"] == channel_id and _public_at(r)]
        week = [r for r in mine if week_start <= _public_at(r) <= mature]
        pending = sum(1 for r in mine if mature < _public_at(r) <= now)
        base = [r for r in mine if base_start <= _public_at(r) < week_start]
        if not week and not base:
            continue
        tested = [r for r in mine if r["hook"] and base_start <= _public_at(r) <= mature]
        hooks = {style: _summary([r for r in tested if r["hook"] == style])
                 for style in sorted({r["hook"] for r in tested})}
        cards.append({"channel": channel_id, "name": names.get(channel_id, channel_id),
                      "week": _summary(week), "baseline": _summary(base), "hooks": hooks,
                      "pending": pending})
    return cards
