"""Follow competitor channels: their topics that are working right now become
our next video ideas, made as our OWN original videos.

What is taken from a competitor is only the SUBJECT and the fact that viewers
want it now. Nothing of theirs is reused: not their script (it is never even
read), footage, thumbnail, voice or title. A subject or an idea is not
protected by copyright; their words and pictures are. The script writer gets
the subject plus strict rules (own angle, own words, a clearly different
title, never name their channel), and a title that still repeats theirs is
written again.

How "working" is measured: each competitor video's views per day, divided by
that channel's usual (median) views per day over the last 30 days. A video at
3x is a topic their audience is hungry for; ours goes after the same hunger.

    python -m src.cli competitors --channel chronovanta          # refresh + show
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from .config import settings

REFRESH_EVERY = timedelta(hours=12)
LOOKBACK_DAYS = 30
MIN_AGE = timedelta(hours=12)      # too young to judge
MIN_RATIO = 1.0                    # at least as good as their usual video
_STOP = {"the", "a", "an", "of", "in", "on", "to", "and", "or", "is", "are", "was", "be", "for",
         "with", "how", "what", "why", "this", "that", "it", "you", "your", "we", "our", "by",
         "at", "from", "will", "can", "could", "would", "if", "do", "does", "did", "they", "their"}


# ------------------------------------------------------------------ storage

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS competitor_channels (
               channel TEXT NOT NULL, url TEXT NOT NULL, competitor_id TEXT, name TEXT,
               uploads TEXT, error TEXT, fetched_at TEXT,
               PRIMARY KEY (channel, url))"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS competitor_videos (
               channel TEXT NOT NULL, video_id TEXT NOT NULL, competitor_id TEXT, competitor_name TEXT,
               title TEXT, views INTEGER, published_at TEXT, short INTEGER, ratio REAL,
               fetched_at TEXT, used_at TEXT, our_title TEXT,
               PRIMARY KEY (channel, video_id))"""
    )
    have = {row[1] for row in conn.execute("PRAGMA table_info(competitor_channels)")}
    for column in ("avatar TEXT", "subs INTEGER", "handle TEXT"):
        if column.split()[0] not in have:
            conn.execute(f"ALTER TABLE competitor_channels ADD COLUMN {column}")
    return conn


def set_competitors(channel_id: str, urls: list[str]) -> list[str]:
    """Save the competitor list on the channel (channels.json)."""
    import json

    from .channel_factory import CONFIG_FILE

    clean: list[str] = []
    for url in urls:
        url = url.strip()
        if url and url not in clean:
            clean.append(url)
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for row in config.get("channels", []):
        if row.get("id") == channel_id:
            row["competitors"] = clean[:10]
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    conn = _conn()
    try:
        # Forget channels that were removed from the list.
        marks = ",".join("?" for _ in clean) or "''"
        conn.execute(f"DELETE FROM competitor_channels WHERE channel = ? AND url NOT IN ({marks})",
                     (channel_id, *clean))
        conn.commit()
    finally:
        conn.close()
    return clean[:10]


# ------------------------------------------------------------------ YouTube

def _service(channel):
    from .youtube_accounts import channel_for_account, connected_accounts
    from .youtube_upload import _youtube

    accounts = connected_accounts(channel.id)
    if not accounts:
        raise RuntimeError("connect this channel's YouTube first; its login is used to read public data")
    own = channel_for_account(channel.id, accounts[0].id)
    return _youtube(own.client_secret_path, own.token_path)


def _resolve(youtube, url: str) -> dict:
    """The YouTube channel item (snippet, statistics, contentDetails) for a URL or @handle."""
    text = url.strip()
    part = "snippet,statistics,contentDetails"
    found = None
    if m := re.search(r"/channel/(UC[\w-]{20,})", text):
        found = youtube.channels().list(part=part, id=m.group(1)).execute()
    elif m := re.search(r"(?:^|/)(@[\w.\-]+)", text):
        found = youtube.channels().list(part=part, forHandle=m.group(1)).execute()
    elif m := re.search(r"/(?:user|c)/([\w.\-]+)", text):
        found = youtube.channels().list(part=part, forUsername=m.group(1)).execute()
        if not found.get("items"):
            hit = youtube.search().list(part="snippet", q=m.group(1), type="channel", maxResults=1).execute()
            ids = [i["snippet"]["channelId"] for i in hit.get("items", [])]
            found = youtube.channels().list(part=part, id=ids[0]).execute() if ids else {}
    if not found or not found.get("items"):
        raise ValueError("not a YouTube channel link (use the channel page, e.g. youtube.com/@name)")
    return found["items"][0]


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return 0
    d, h, m, s = (int(x or 0) for x in match.groups())
    return ((d * 24 + h) * 60 + m) * 60 + s


def refresh(channel, force: bool = False) -> list[str]:
    """Read each competitor's last 30 days of videos and score them."""
    urls = list(getattr(channel, "competitors", ()) or ())
    if not urls:
        return []
    now = datetime.now(timezone.utc)
    conn = _conn()
    try:
        if not force:
            last = conn.execute("SELECT MAX(fetched_at) FROM competitor_channels WHERE channel = ?",
                                (channel.id,)).fetchone()[0]
            if last and now - datetime.fromisoformat(last) < REFRESH_EVERY:
                return [f"[{channel.id}] competitors read {last[:16]}; next refresh after 12 h"]
        youtube = _service(channel)
        notes = []
        for url in urls:
            cached = conn.execute("SELECT competitor_id FROM competitor_channels "
                                  "WHERE channel = ? AND url = ?", (channel.id, url)).fetchone()
            try:
                if cached and cached["competitor_id"]:
                    # 1 unit: also refreshes the picture and subscriber count.
                    found = youtube.channels().list(part="snippet,statistics,contentDetails",
                                                    id=cached["competitor_id"]).execute().get("items") or []
                    item = found[0] if found else _resolve(youtube, url)
                else:
                    item = _resolve(youtube, url)
                cid, name = item["id"], item["snippet"]["title"]
                uploads = item["contentDetails"]["relatedPlaylists"]["uploads"]
                thumbs = item["snippet"].get("thumbnails") or {}
                avatar = (thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
                subs = int(item.get("statistics", {}).get("subscriberCount", 0) or 0)
                handle = item["snippet"].get("customUrl", "")
                listed = youtube.playlistItems().list(part="contentDetails", playlistId=uploads,
                                                      maxResults=50).execute()
                ids = [i["contentDetails"]["videoId"] for i in listed.get("items", [])]
                videos = youtube.videos().list(part="snippet,statistics,contentDetails",
                                               id=",".join(ids)).execute().get("items", []) if ids else []
                rows = []
                for video in videos:
                    published = datetime.fromisoformat(video["snippet"]["publishedAt"].replace("Z", "+00:00"))
                    age = now - published
                    if age > timedelta(days=LOOKBACK_DAYS):
                        continue
                    views = int(video.get("statistics", {}).get("viewCount", 0) or 0)
                    per_day = views / max(age.total_seconds() / 86400, 0.5)
                    rows.append((video, published, views, per_day, age))
                usual = statistics.median([r[3] for r in rows]) if rows else 0
                for video, published, views, per_day, age in rows:
                    ratio = per_day / usual if usual else 0.0
                    conn.execute(
                        """INSERT INTO competitor_videos (channel, video_id, competitor_id, competitor_name, title,
                               views, published_at, short, ratio, fetched_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(channel, video_id) DO UPDATE SET views=excluded.views,
                               ratio=excluded.ratio, fetched_at=excluded.fetched_at, title=excluded.title""",
                        (channel.id, video["id"], cid, name, video["snippet"]["title"], views,
                         published.isoformat(),
                         1 if _duration_seconds(video["contentDetails"].get("duration", "")) <= 180 else 0,
                         round(ratio, 2) if age >= MIN_AGE else 0.0, now.isoformat()),
                    )
                conn.execute(
                    """INSERT INTO competitor_channels (channel, url, competitor_id, name, uploads, error, fetched_at,
                           avatar, subs, handle)
                       VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                       ON CONFLICT(channel, url) DO UPDATE SET competitor_id=excluded.competitor_id,
                           name=excluded.name, uploads=excluded.uploads, error=NULL, fetched_at=excluded.fetched_at,
                           avatar=excluded.avatar, subs=excluded.subs, handle=excluded.handle""",
                    (channel.id, url, cid, name, uploads, now.isoformat(), avatar, subs, handle),
                )
                notes.append(f"[{channel.id}] {name}: {len(rows)} video(s) in the last {LOOKBACK_DAYS} days")
            except Exception as exc:
                conn.execute(
                    """INSERT INTO competitor_channels (channel, url, error, fetched_at) VALUES (?, ?, ?, ?)
                       ON CONFLICT(channel, url) DO UPDATE SET error=excluded.error, fetched_at=excluded.fetched_at""",
                    (channel.id, url, str(exc)[:300], now.isoformat()),
                )
                notes.append(f"[{channel.id}] {url}: could not read ({str(exc)[:120]})")
        conn.commit()
        return notes
    finally:
        conn.close()


# ------------------------------------------------------------------ ideas

def _words(title: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", str(title).lower()) if w not in _STOP]


def too_similar(ours: str, theirs: str) -> bool:
    """True when our title repeats theirs: most of its words, or 3 of them in a row."""
    a, b = _words(ours), _words(theirs)
    if not a or not b:
        return False
    if len(set(a) & set(b)) / min(len(set(a)), len(set(b))) >= 0.6:
        return True
    grams = {tuple(b[i:i + 3]) for i in range(len(b) - 2)}
    return any(tuple(a[i:i + 3]) in grams for i in range(len(a) - 2))


def _our_titles(channel) -> list[str]:
    titles = []
    conn = sqlite3.connect(settings.db_path)
    try:
        titles += [r[0] for r in conn.execute(
            "SELECT title FROM videos WHERE channel = ? ORDER BY id DESC LIMIT 300", (channel.id,))]
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
    return titles


def next_idea(channel, kind: str = "short") -> dict | None:
    """The best unused competitor topic for our next video, reserved for it; None if there is none."""
    if not getattr(channel, "competitors", ()):
        return None
    try:
        refresh(channel)
    except Exception as exc:
        print(f"[competitors] {channel.id}: refresh failed ({exc}); using what was read before")
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT * FROM competitor_videos
               WHERE channel = ? AND used_at IS NULL AND ratio >= ? AND published_at >= ?
               ORDER BY (short = ?) DESC, ratio DESC LIMIT 40""",
            (channel.id, MIN_RATIO, since, 1 if kind == "short" else 0),
        ).fetchall()
        ours = _our_titles(channel)
        for row in rows:
            if any(too_similar(title, row["title"]) for title in ours):
                # We already have a video on this; do not offer it again.
                conn.execute("UPDATE competitor_videos SET used_at = ?, our_title = ? WHERE channel = ? AND video_id = ?",
                             (datetime.now(timezone.utc).isoformat(), "(already covered)", channel.id, row["video_id"]))
                continue
            conn.execute("UPDATE competitor_videos SET used_at = ? WHERE channel = ? AND video_id = ?",
                         (datetime.now(timezone.utc).isoformat(), channel.id, row["video_id"]))
            conn.commit()
            return dict(row)
        conn.commit()
        return None
    finally:
        conn.close()


def mark_written(channel_id: str, video_id: str, our_title: str) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE competitor_videos SET our_title = ? WHERE channel = ? AND video_id = ?",
                     (our_title, channel_id, video_id))
        conn.commit()
    finally:
        conn.close()


def inspiration_brief(idea: dict) -> str:
    """The instruction added to the script prompt. Their title is the only thing passed on."""
    return (
        "\nTOPIC INSPIRATION (subject only, never copy): another channel in this niche has a video "
        f"titled \"{idea['title']}\" that is getting {idea['ratio']:.1f}x its usual views, so viewers "
        "want this subject right now. Make an ORIGINAL video about the same general subject:\n"
        "- pick your own angle and your own question; do not retell or summarise their video;\n"
        "- every line is yours, from well-known facts; never quote or paraphrase another video;\n"
        "- your title must be clearly different: not their wording, never three of their words in a row;\n"
        "- never mention that channel or any other YouTube channel;\n"
        "- all the channel rules above still apply.\n"
    )


def _short_number(n: int | None) -> str:
    n = int(n or 0)
    for size, mark in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + mark
    return str(n)


def _age(published: str | None) -> str:
    if not published:
        return ""
    hours = (datetime.now(timezone.utc) - datetime.fromisoformat(published)).total_seconds() / 3600
    if hours < 24:
        return f"{max(1, int(hours))}h ago"
    days = int(hours // 24)
    return f"{days}d ago" if days < 30 else f"{days // 30}mo ago"


def overview(channel_id: str, limit: int = 30) -> dict:
    """Competitor channels (with picture, subscribers, best topic) and their top recent videos."""
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    conn = _conn()
    try:
        channels = [dict(r) for r in conn.execute(
            """SELECT url, competitor_id, name, error, fetched_at, avatar, subs, handle
               FROM competitor_channels WHERE channel = ? ORDER BY subs DESC""", (channel_id,))]
        stats = {r["competitor_id"]: dict(r) for r in conn.execute(
            """SELECT competitor_id, COUNT(*) AS n, SUM(short) AS shorts, MAX(ratio) AS best
               FROM competitor_videos WHERE channel = ? AND published_at >= ? GROUP BY competitor_id""",
            (channel_id, since))}
        videos = [dict(r) for r in conn.execute(
            """SELECT video_id, competitor_id, competitor_name, title, views, published_at, short, ratio,
                      used_at, our_title
               FROM competitor_videos WHERE channel = ? AND published_at >= ?
               ORDER BY ratio DESC LIMIT ?""", (channel_id, since, limit))]
    finally:
        conn.close()
    avatars = {c["competitor_id"]: c.get("avatar") or "" for c in channels}
    for c in channels:
        s = stats.get(c["competitor_id"]) or {}
        c.update(n=s.get("n") or 0, shorts=s.get("shorts") or 0, best=s.get("best") or 0,
                 subs_text=_short_number(c.get("subs")),
                 link=f"https://www.youtube.com/{c['handle']}" if c.get("handle") else c["url"])
    for v in videos:
        v.update(views_text=_short_number(v["views"]), age=_age(v["published_at"]),
                 thumb=f"https://i.ytimg.com/vi/{v['video_id']}/mqdefault.jpg",
                 avatar=avatars.get(v["competitor_id"], ""),
                 state=("made" if v["our_title"] else "making" if v["used_at"]
                        else "next" if (v["ratio"] or 0) >= MIN_RATIO else "skip"))
    return {"channels": channels, "videos": videos}
