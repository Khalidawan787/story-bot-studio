"""One picture post a day on a channel's Facebook Page, besides its videos.

Switched on per channel ("Also post one picture a day" in the Facebook
settings). The post is a picture with a headline burned in and a few sentences
of text under it, in the language chosen for that Page.

A fact-only channel (verified_facts.py) posts about a real person: the text is
written from the Wikipedia article and checked against it, the article is
linked, and the picture is the article's own photo when that photo is free to
reuse. If it is not, the card carries the headline on a plain backdrop: a
stock or AI picture next to a real person's name would pass a stranger off as
that person.
"""
from __future__ import annotations

import html
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .channels import Channel, load_channels
from .config import settings
from .db import connect
from .models import Lesson, Scene
from .social_accounts import account

# Licences that allow reposting with a credit line.
_FREE_LICENCE = re.compile(r"^(public domain|pd\b|cc0|cc[ -]by)", re.I)


def daily_post_on(channel_id: str) -> bool:
    cfg = account(channel_id, "facebook")
    return bool(cfg.get("daily_post") and cfg.get("page_id") and cfg.get("page_access_token"))


def _table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS page_posts (
               id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, day TEXT NOT NULL,
               subject TEXT, post_id TEXT, post_url TEXT, created_at TEXT NOT NULL)"""
    )


def _today() -> str:
    return datetime.now().date().isoformat()


def posted_today(channel_id: str) -> bool:
    conn = connect()
    try:
        _table(conn)
        return conn.execute(
            "SELECT 1 FROM page_posts WHERE channel = ? AND day = ?", (channel_id, _today()),
        ).fetchone() is not None
    finally:
        conn.close()


def _posted_subjects(channel_id: str) -> set[str]:
    conn = connect()
    try:
        _table(conn)
        rows = conn.execute("SELECT subject FROM page_posts WHERE channel = ?", (channel_id,)).fetchall()
        return {str(subject).lower() for (subject,) in rows if subject}
    finally:
        conn.close()


def _record(channel_id: str, subject: str, post_id: str, post_url: str) -> None:
    conn = connect()
    try:
        _table(conn)
        conn.execute(
            "INSERT INTO page_posts (channel, day, subject, post_id, post_url, created_at) VALUES (?,?,?,?,?,?)",
            (channel_id, _today(), subject, post_id, post_url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _hashtags(channel: Channel) -> str:
    words = [re.sub(r"[^A-Za-z0-9]", "", str(k).title()) for k in (getattr(channel, "keywords", ()) or ())]
    return " ".join(f"#{word}" for word in words[:3] if word)


def _free_article_photo(title: str, out: Path) -> tuple[Path, str] | None:
    """The article's lead photo and its credit line, only if it may be reused."""
    from .verified_facts import USER_AGENT, _api

    pages = _api({"action": "query", "prop": "pageimages", "piprop": "name", "redirects": "1",
                  "titles": title}).get("query", {}).get("pages") or []
    name = (pages[0] if pages else {}).get("pageimage")
    if not name:
        return None
    info = _api({"action": "query", "titles": f"File:{name}", "prop": "imageinfo",
                 "iiprop": "url|extmetadata", "iiurlwidth": "1600"}).get("query", {}).get("pages") or []
    page = info[0] if info else {}
    image = (page.get("imageinfo") or [{}])[0]
    meta = image.get("extmetadata") or {}
    licence = str((meta.get("LicenseShortName") or {}).get("value") or "")
    # A file kept on Wikipedia itself (not Commons) is usually a non-free
    # picture used under "fair use", which does not cover a Page post.
    if page.get("imagerepository") != "shared" or (meta.get("NonFree") or {}).get("value") \
            or not _FREE_LICENCE.match(licence):
        return None
    url = str(image.get("thumburl") or image.get("url") or "")
    if not url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png")):
        return None
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        out.write_bytes(response.read())
    artist = re.sub(r"<[^>]+>", "", html.unescape(str((meta.get("Artist") or {}).get("value") or ""))).strip()
    # Commons repeats the name in a hidden copy ("Unknown authorUnknown author").
    artist = re.sub(r"^(.+?)\1$", r"\1", artist)
    credit = f"Photo: {artist[:80] or 'Wikimedia Commons'} ({licence}), via Wikimedia Commons"
    return out, credit


def _whole_photo(photo: Path, out: Path) -> Path:
    """The photo fitted whole into the 16:9 card, over a blurred copy of itself.

    Cropping a portrait to 16:9 keeps the middle strip, which on a head shot is
    the chin and beard with the eyes cut off.
    """
    import subprocess

    from .thumbnails import THUMB_HEIGHT, THUMB_WIDTH, _ffmpeg

    size = f"{THUMB_WIDTH}:{THUMB_HEIGHT}"
    subprocess.run(
        [_ffmpeg(), "-y", "-i", str(photo), "-frames:v", "1", "-update", "1", "-filter_complex",
         f"[0:v]scale={size}:force_original_aspect_ratio=increase,crop={size},boxblur=24:4[bg];"
         f"[0:v]scale={size}:force_original_aspect_ratio=decrease[fg];"
         "[bg][fg]overlay=(W-w)/2:(H-h)/2", "-q:v", "2", str(out)],
        check=True, capture_output=True, text=True,
    )
    return out


def _in_page_language(channel: Channel, lesson: Lesson, strict: bool) -> tuple[str, list[str]]:
    """Title and sentences in the language chosen for this Page (Urdu script for Urdu)."""
    from .dual_language import translate_lesson
    from .language import LANGUAGES, channel_language

    title, lines = lesson.title, [scene.line for scene in lesson.scenes]
    try:
        target = str(account(channel.id, "facebook").get("language") or "same").lower()
        if target in LANGUAGES and target != channel_language(channel):
            translated = translate_lesson(lesson, target, strict=strict)
            title = translated.title_speech or translated.title
            lines = [scene.speech or scene.line for scene in translated.scenes]
    except Exception as exc:
        print(f"[page_posts] {channel.id}: translation skipped ({exc})")
    return title, lines


def _verified_post(channel: Channel, work: Path) -> tuple[str, Path, str]:
    """(caption, picture, subject) about a real person, from their Wikipedia article."""
    from . import verified_facts as vf
    from .thumbnails import compose_thumbnail

    used = _posted_subjects(channel.id) | vf._used_subjects(channel, False)
    names = [name for name in vf._listed_subjects() if name.lower() not in used]
    article = None
    for name in names[:10]:
        try:
            candidate = vf.fetch_article(name)
        except Exception:
            continue
        if candidate.title.lower() not in used:
            article = candidate
            break
    if article is None:
        raise RuntimeError("no unused real subject left for a picture post")
    key, raw = vf.write_lesson(channel, article, scenes=5, long_form=False)
    lesson = Lesson(
        topic_key=key, title=article.title, category=raw["category"], intro="", outro="",
        scenes=[Scene(**scene) for scene in raw["scenes"][:5]],
    )
    title, lines = _in_page_language(channel, lesson, strict=True)
    credit = ""
    photo = None
    try:
        found = _free_article_photo(article.title, work / "article_photo.jpg")
        if found:
            photo, credit = found
            photo = _whole_photo(photo, work / "article_photo_fitted.jpg")
    except Exception as exc:
        print(f"[page_posts] {channel.id}: no article photo ({exc})")
    picture = compose_thumbnail(lesson, photo, work / "page_post.jpg", channel, "long")
    caption = "\n\n".join(filter(None, [
        title, "\n".join(lines), f"Source: Wikipedia — {article.url}", credit,
        # The person's own name as the tag: a channel keyword such as
        # "#AncientCivilizations" would be a false label on most of them.
        "#" + re.sub(r"[^A-Za-z0-9]", "", article.title),
    ]))
    return caption, picture, article.title


def _written_post(channel: Channel, work: Path) -> tuple[str, Path, str]:
    """(caption, picture, subject) for an ordinary channel, written in its own tone."""
    from .genre_topics import GENRE_TONE, _script_json
    from .thumbnails import create_thumbnail

    tone = getattr(channel, "tone", "") or GENRE_TONE.get(channel.genre, "engaging")
    avoid = "; ".join(sorted(_posted_subjects(channel.id))[-30:]) or "none"
    raw = _script_json(
        f"Write one short Facebook Page post for the {channel.genre} page '{channel.name}'. Tone: {tone}.\n"
        "A headline of at most 8 words and 3 or 4 sentences that are useful or interesting on their own. "
        "No hashtags, no emojis, no invented numbers, quotes or names of real people.\n"
        f"Do not repeat these headlines: {avoid}.\n"
        'Return ONLY JSON: {"title": str, "lines": [str], "image_prompt": str}'
    )
    title = " ".join(str(raw.get("title") or "").split())
    lines = [" ".join(str(line).split()) for line in raw.get("lines") or [] if str(line).strip()]
    if not title or not lines:
        raise RuntimeError("the post writer returned nothing usable")
    prompt = f"{raw.get('image_prompt') or title}, {channel.image_style}"
    lesson = Lesson(
        topic_key=re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:60] or "page_post",
        title=title, category=channel.genre.title(), intro="", outro="",
        scenes=[Scene(label=title[:30], line=line, image="", image_prompt=prompt) for line in lines[:4]],
    )
    shown_title, shown_lines = _in_page_language(channel, lesson, strict=False)
    picture = create_thumbnail(lesson, None, work / "page_post.jpg", channel, "long")
    caption = "\n\n".join(filter(None, [shown_title, "\n".join(shown_lines), _hashtags(channel)]))
    return caption, picture, title


def publish_daily_post(channel: Channel, dry_run: bool = False) -> str:
    """Make today's picture post for one channel and put it on its Page.

    `dry_run` builds the picture and text and shows them without posting.
    """
    from .social_publish import FB_API_VERSION, _multipart, _send_binary
    from .verified_facts import is_verified_channel

    cfg = account(channel.id, "facebook")
    page_id, token = str(cfg.get("page_id") or ""), str(cfg.get("page_access_token") or "")
    if not page_id or not token:
        raise RuntimeError("Facebook is not configured for this channel.")
    work = settings.root / "runs" / f"page_post_{channel.id}_{datetime.now():%Y%m%d_%H%M%S}"
    work.mkdir(parents=True, exist_ok=True)
    build = _verified_post if is_verified_channel(channel) else _written_post
    caption, picture, subject = build(channel, work)
    if dry_run:
        return f"NOT posted (dry run). Picture: {picture}\n{caption}"
    body, content_type = _multipart({"caption": caption, "access_token": token}, "source", picture)
    result = _send_binary(
        f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/photos", body,
        {"Content-Type": content_type}, "POST",
    )
    post_id = str(result.get("post_id") or result.get("id") or "")
    if not post_id:
        raise RuntimeError(f"Facebook did not accept the picture post: {result}")
    url = f"https://www.facebook.com/{post_id}"
    _record(channel.id, subject, post_id, url)
    return f"posted '{subject}' -> {url}"


def run_daily_posts(slot: set[int] | None = None, dry_run: bool = False) -> list[tuple[str, str]]:
    """Today's picture post for every channel that has it switched on."""
    from .slots import channel_slot

    results: list[tuple[str, str]] = []
    for channel in load_channels():
        if slot is not None and channel_slot(channel) not in slot:
            continue
        if not daily_post_on(channel.id):
            continue
        if posted_today(channel.id) and not dry_run:
            results.append((channel.id, "SKIPPED today's picture post already exists"))
            continue
        try:
            results.append((channel.id, "OK " + publish_daily_post(channel, dry_run=dry_run)))
        except Exception as exc:
            results.append((channel.id, f"FAILED {exc}"))
    return results
