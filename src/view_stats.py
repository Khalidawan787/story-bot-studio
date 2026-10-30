"""Real view counts for every uploaded video.

The `views` column of the videos table was never filled in: all 450 uploaded
videos read 0, so no decision about topics, titles or posting times could use
real numbers. This reads the counts from the YouTube Data API (videos.list,
part=statistics: one quota unit per 50 videos, nothing against the upload quota)
with the same login that uploaded each video, so a private or scheduled video of
that channel is counted too.

Run it with `python -m src.cli refresh-views`.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .db import connect

_ID = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")
_BATCH = 50


def video_id(url: str | None) -> str:
    match = _ID.search(str(url or ""))
    return match.group(1) if match else ""


def refresh_views(channel_id: str = "") -> list[str]:
    """Update `views` for every uploaded video; one summary line per login."""
    from .channels import load_channels
    from .youtube_accounts import channel_for_account
    from .youtube_upload import _youtube

    known = {channel.id for channel in load_channels()}
    conn = connect()
    try:
        # hidden_at only hides a row on the dashboard; the video is still on
        # YouTube and still has views, so hidden rows are counted too (444 of the
        # 450 uploaded videos were hidden, and the first version skipped them).
        rows = conn.execute(
            "SELECT id, channel, COALESCE(account, 'main'), video_url FROM videos "
            "WHERE video_url IS NOT NULL AND video_url != ''"
        ).fetchall()
    finally:
        conn.close()

    groups: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for row_id, channel, account, url in rows:
        if channel_id and channel != channel_id:
            continue
        vid = video_id(url)
        if vid and channel in known:
            groups[(channel, account)][vid] = row_id

    messages: list[str] = []
    for (channel, account), ids in sorted(groups.items()):
        label = channel if account == "main" else f"{channel}/{account}"
        try:
            target = channel_for_account(channel, account)
            if not target.token_path.is_file():
                messages.append(f"[{label}] skipped: no YouTube login on this machine")
                continue
            service = _youtube(target.client_secret_path, target.token_path)
            counts: dict[str, int] = {}
            wanted = list(ids)
            for start in range(0, len(wanted), _BATCH):
                batch = wanted[start:start + _BATCH]
                response = service.videos().list(part="statistics", id=",".join(batch)).execute()
                for item in response.get("items", []):
                    counts[item["id"]] = int((item.get("statistics") or {}).get("viewCount") or 0)
        except Exception as exc:  # a login problem on one channel must not stop the others
            messages.append(f"[{label}] failed: {str(exc)[:160]}")
            continue
        conn = connect()
        try:
            for vid, views in counts.items():
                conn.execute("UPDATE videos SET views = ? WHERE id = ?", (views, ids[vid]))
            conn.commit()
        finally:
            conn.close()
        missing = len(ids) - len(counts)
        messages.append(
            f"[{label}] {len(counts)} updated, {missing} not found on YouTube, "
            f"{sum(counts.values())} views in total"
        )
    if not messages:
        messages.append("no uploaded videos to refresh")
    return messages


def top_videos(limit: int = 10) -> list[str]:
    """The most watched videos, one readable line each."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT channel, COALESCE(content_type, 'short'), COALESCE(views, 0), title FROM videos "
            "WHERE video_url IS NOT NULL AND video_url != '' ORDER BY COALESCE(views, 0) DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    return [f"{views:>6}  {channel}/{kind}  {str(title)[:70]}" for channel, kind, views, title in rows]
