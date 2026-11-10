"""Daily check: does every new video on every connected YouTube channel show a
custom thumbnail? If not, make one and set it.

How a missing thumbnail is spotted, without spending API quota: YouTube keeps
four public pictures per video, the shown thumbnail (hqdefault) and three
frames it picked itself (hq1, hq2, hq3). Without a custom thumbnail the shown
one IS one of those frames (difference ~0); a custom thumbnail differs from
all three (difference 28+ on a sample of real videos). A private or scheduled
video has no public pictures yet; it is checked once it is public.

Every video of the last few days is looked at, Shorts and long videos, also
ones uploaded by hand. The thumbnail used for a fix is, in order: the bot's own
card for that video, a fresh one from its script, or a new one from its title.

    python -m src.cli audit-thumbnails --days 7
"""
from __future__ import annotations

import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import settings

AUTO_FRAME_LIMIT = 8.0       # mean grey difference below this = YouTube's own frame
REFIX_AFTER = timedelta(hours=12)   # YouTube's picture cache can lag behind a fresh set
MAX_TRIES = 5
# YouTube allows only a few thumbnail changes a day per channel; on 4 Nov 2026 a
# bulk run used them up and new videos went out without one. So few fixes, and
# only once the channel's own videos of the day are out (see _channel_ready).
MAX_FIXES_PER_CHANNEL = 3


# ------------------------------------------------------------------ detection

def _picture(video_id: str, name: str):
    import cv2
    import numpy as np

    request = urllib.request.Request(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg",
                                     headers={"User-Agent": "Mozilla/5.0 story-bot-studio"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
    except urllib.error.HTTPError:
        return None
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    return None if image is None else cv2.resize(image, (48, 36), interpolation=cv2.INTER_AREA).astype(float)


def _exists(video_id: str, name: str) -> bool:
    request = urllib.request.Request(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg", method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0 story-bot-studio"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except Exception:
        return False


def shorts_tab_state(video_id: str) -> str:
    """For a public Short: 'custom' when the Shorts tab shows our card ("sardefault"),
    'frame' when it shows a frame YouTube picked ("oar2" only), else 'unknown'.

    On 9-10 Nov new Shorts had their card as the video thumbnail, yet the Shorts
    tab showed a frame: the thumbnail was set before YouTube finished processing.
    """
    if _exists(video_id, "sardefault"):
        return "custom"
    return "frame" if _exists(video_id, "oar2") else "unknown"


def thumbnail_state(video_id: str) -> str:
    """'custom', 'missing' (YouTube shows one of its own frames) or 'unknown' (not public yet)."""
    shown = _picture(video_id, "hqdefault")
    if shown is None:
        return "unknown"
    differences = []
    for name in ("hq1", "hq2", "hq3"):
        frame = _picture(video_id, name)
        if frame is not None:
            differences.append(float(abs(shown - frame).mean()))
    if not differences:
        return "unknown"
    return "missing" if min(differences) < AUTO_FRAME_LIMIT else "custom"


# ------------------------------------------------------------------ records

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS thumbnail_audit (
               video_id TEXT PRIMARY KEY, channel TEXT NOT NULL, title TEXT,
               state TEXT NOT NULL, checked_at TEXT NOT NULL,
               fixed_at TEXT, tries INTEGER NOT NULL DEFAULT 0, note TEXT)"""
    )
    return conn


def _record(video_id: str, channel: str, title: str, state: str, note: str = "",
            fixed: bool = False) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    try:
        conn.execute(
            """INSERT INTO thumbnail_audit (video_id, channel, title, state, checked_at, fixed_at, tries, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                   title=excluded.title, state=excluded.state, checked_at=excluded.checked_at,
                   note=excluded.note,
                   fixed_at=COALESCE(excluded.fixed_at, thumbnail_audit.fixed_at),
                   tries=thumbnail_audit.tries + excluded.tries""",
            (video_id, channel, title, state, now, now if fixed else None, 1 if fixed else 0, note),
        )
        conn.commit()
    finally:
        conn.close()


def _previous(video_id: str) -> tuple[str | None, int]:
    conn = _conn()
    try:
        row = conn.execute("SELECT fixed_at, tries FROM thumbnail_audit WHERE video_id = ?",
                           (video_id,)).fetchone()
    finally:
        conn.close()
    return (row[0], int(row[1])) if row else (None, 0)


def summary(channel_id: str, days: int = 7) -> dict:
    """Counts for the dashboard: how the last `days` of videos stand."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT state, COUNT(*), MAX(checked_at) FROM thumbnail_audit
               WHERE channel = ? AND checked_at >= ? GROUP BY state""",
            (channel_id, since),
        ).fetchall()
        problems = conn.execute(
            """SELECT video_id, title, state, note FROM thumbnail_audit
               WHERE channel = ? AND checked_at >= ? AND state NOT IN ('custom', 'unknown')
               ORDER BY checked_at DESC LIMIT 10""",
            (channel_id, since),
        ).fetchall()
    finally:
        conn.close()
    counts = {state: n for state, n, _ in rows}
    last = max((at for _, _, at in rows), default=None)
    return {"counts": counts, "last": last,
            "problems": [{"video_id": v, "title": t, "state": s, "note": n} for v, t, s, n in problems]}


# ------------------------------------------------------------------ YouTube

def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return 0
    d, h, m, s = (int(x or 0) for x in match.groups())
    return ((d * 24 + h) * 60 + m) * 60 + s


def recent_uploads(channel, days: int) -> list[dict]:
    """This account's videos uploaded in the last `days` (YouTube's own list)."""
    from .youtube_upload import _youtube

    youtube = _youtube(channel.client_secret_path, channel.token_path)
    mine = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = mine.get("items") or []
    if not items:
        return []
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    since = datetime.now(timezone.utc) - timedelta(days=days)
    ids: list[str] = []
    listed = youtube.playlistItems().list(part="contentDetails,snippet", playlistId=uploads,
                                          maxResults=50).execute()
    for item in listed.get("items", []):
        added = datetime.fromisoformat(item["snippet"]["publishedAt"].replace("Z", "+00:00"))
        if added >= since:
            ids.append(item["contentDetails"]["videoId"])
    if not ids:
        return []
    details = youtube.videos().list(part="snippet,status,contentDetails", id=",".join(ids)).execute()
    return [{
        "id": video["id"],
        "title": video["snippet"].get("title", ""),
        "privacy": video["status"].get("privacyStatus", ""),
        "publish_at": video["status"].get("publishAt"),
        "short": _duration_seconds(video["contentDetails"].get("duration", "")) <= 180,
    } for video in details.get("items", [])]


# ------------------------------------------------------------------ fixing

def _bot_row(video_id: str):
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """SELECT id, topic, thumbnail_path, video_path, status, publish_at,
                      COALESCE(content_type, 'short') AS content_type
               FROM videos WHERE video_url LIKE ? ORDER BY id DESC LIMIT 1""",
            (f"%{video_id}%",),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _title_lesson(title: str, channel):
    from .models import Lesson, Scene

    key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:60] or "video"
    return Lesson(topic_key=key, title=title, category=str(channel.genre).title(), intro="", outro="",
                  scenes=[Scene(label=title[:30], line=title, image="", image_prompt=title)])


def thumbnail_for(channel, video: dict) -> Path:
    """A checked 1280x720 thumbnail for this video, made if necessary."""
    from .lessons import load_lesson_for
    from .thumbnail_check import ThumbnailNotReady, check_thumbnail
    from .thumbnails import create_thumbnail

    content_type = "short" if video.get("short") else "long"
    row = _bot_row(video["id"])
    if row is not None and str(row["thumbnail_path"] or "") and Path(str(row["thumbnail_path"])).is_file():
        own_card = Path(str(row["thumbnail_path"]))
        # A Short's old 16:9 card is cropped on the Shorts shelf: remake it upright.
        if content_type == "short" and check_thumbnail(own_card).ok and check_thumbnail(own_card).height < check_thumbnail(own_card).width:
            row = dict(row)
            row["thumbnail_path"] = ""
    if row is not None:
        own = Path(str(row["thumbnail_path"] or ""))
        if not own.is_file():
            try:
                from . import github_artifacts

                github_artifacts.restore_run_files(own.parent)
            except Exception:
                pass
        if own.is_file() and check_thumbnail(own).ok:
            return own
        content_type = row["content_type"] or content_type
    out = settings.root / "runs" / "thumbnail_audit" / f"{video['id']}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    lesson = None
    if row is not None:
        try:
            lesson = load_lesson_for(channel, row["topic"])
        except Exception:
            lesson = None
    lesson = lesson or _title_lesson(video["title"], channel)
    made = None
    if row is not None:
        # A true story about a real person: that person's own photo (pipeline).
        from .pipeline import _real_subject_thumbnail

        found = _real_subject_thumbnail(lesson, channel, out.parent / video["id"], content_type)
        made = Path(found) if found else None
    made = made or Path(create_thumbnail(lesson, None, out, channel, content_type))
    check = check_thumbnail(made)
    if not check.ok:
        raise ThumbnailNotReady(check.summary)
    return made


def _channel_ready(channel) -> tuple[bool, str]:
    """Fixes wait until the channel's own new videos have their thumbnails."""
    from .pending_uploads import thumbnail_wait_reason
    from .slots import SLOTS_UTC, channel_slot

    hour, minute = SLOTS_UTC[channel_slot(channel)]
    now = datetime.now(timezone.utc)
    if now.hour * 60 + now.minute < hour * 60 + minute + 180:
        return False, "the channel's own videos of today come first"
    waiting = thumbnail_wait_reason(channel.id)
    if waiting:
        return False, waiting
    return True, ""


def audit(days: int = 7, fix: bool = True, channel_id: str | None = None) -> list[str]:
    """Check every connected channel's recent videos; set a thumbnail where one is missing."""
    from .channels import load_channels
    from .pending_uploads import (
        disable_thumbnail_upload, enable_thumbnail_upload, thumbnail_permission_denied,
        thumbnail_upload_allowed,
    )
    from .youtube_accounts import channel_for_account, connected_accounts
    from .youtube_upload import set_thumbnail

    messages: list[str] = []
    now = datetime.now(timezone.utc)
    for base in load_channels():
        if channel_id and base.id != channel_id:
            continue
        for account in connected_accounts(base.id):
            channel = channel_for_account(base.id, account.id)
            tag = f"[{base.id}/{account.id}]"
            try:
                videos = recent_uploads(channel, days)
            except Exception as exc:
                messages.append(f"{tag} could not list the channel's videos: {exc}")
                continue
            fixes, counts = 0, {"custom": 0, "missing": 0, "unknown": 0, "fixed": 0}
            for video in videos:
                state = thumbnail_state(video["id"])
                if state == "custom" and video["short"] and shorts_tab_state(video["id"]) == "frame":
                    # The card is the video's thumbnail but the Shorts tab shows a
                    # frame: setting it again after processing fixes the tab.
                    state = "missing"
                kind = "Short" if video["short"] else "long"
                label = f"{tag} {video['id']} ({kind}) '{video['title'][:50]}'"
                if state != "missing":
                    counts[state] += 1
                    _record(video["id"], base.id, video["title"], state,
                            "not public yet" if state == "unknown" else "")
                    continue
                counts["missing"] += 1
                fixed_at, tries = _previous(video["id"])
                if fixed_at and now - datetime.fromisoformat(fixed_at) < REFIX_AFTER:
                    _record(video["id"], base.id, video["title"], "fix_waiting",
                            "set recently; waiting for YouTube to show it")
                    messages.append(f"{label}: thumbnail set recently, waiting for YouTube to show it")
                    continue
                if not fix or tries >= MAX_TRIES or fixes >= MAX_FIXES_PER_CHANNEL:
                    why = ("check only" if not fix else
                           f"gave up after {tries} tries" if tries >= MAX_TRIES else "today's fix limit reached")
                    _record(video["id"], base.id, video["title"], "missing", why)
                    messages.append(f"{label}: NO custom thumbnail ({why})")
                    continue
                ready, why_not = _channel_ready(base)
                if not ready:
                    _record(video["id"], base.id, video["title"], "missing", why_not)
                    messages.append(f"{label}: NO custom thumbnail yet ({why_not})")
                    continue
                if not thumbnail_upload_allowed(base.id):
                    _record(video["id"], base.id, video["title"], "missing",
                            "channel not verified for custom thumbnails (youtube.com/verify)")
                    messages.append(f"{label}: NO custom thumbnail; verify the channel at youtube.com/verify")
                    continue
                try:
                    path = thumbnail_for(channel, video)
                    set_thumbnail(video["id"], path, attempts=2, client_secret_file=channel.client_secret_path,
                                  token_file=channel.token_path)
                except Exception as exc:
                    error = str(exc)
                    _record(video["id"], base.id, video["title"], "fix_failed", error[:300])
                    messages.append(f"{label}: fix FAILED: {error[:160]}")
                    if thumbnail_permission_denied(error):
                        disable_thumbnail_upload(base.id)
                        break
                    if "uploadratelimitexceeded" in error.lower() or "too many thumbnails" in error.lower():
                        break
                    continue
                fixes += 1
                counts["fixed"] += 1
                _record(video["id"], base.id, video["title"], "fixed", f"set from {path.name}", fixed=True)
                messages.append(f"{label}: thumbnail was missing, set it now")
                row = _bot_row(video["id"])
                if row is not None and row["status"] == "thumbnail_pending":
                    from .db import mark_scheduled, mark_uploaded

                    url = f"https://www.youtube.com/watch?v={video['id']}"
                    if row["publish_at"]:
                        mark_scheduled(int(row["id"]), url, str(row["publish_at"]))
                    else:
                        mark_uploaded(int(row["id"]), url)
                if not thumbnail_upload_allowed(base.id):
                    enable_thumbnail_upload(base.id)
            messages.append(
                f"{tag} checked {len(videos)} video(s) from the last {days} days: "
                f"{counts['custom']} with thumbnail, {counts['fixed']} fixed now, "
                f"{counts['missing'] - counts['fixed']} still missing, {counts['unknown']} not public yet"
            )
    return messages
