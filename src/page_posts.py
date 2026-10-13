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
    from .seo import _GENRE_KEYWORDS

    keywords = list(getattr(channel, "keywords", ()) or ()) or _GENRE_KEYWORDS.get(channel.genre, [])
    words = [re.sub(r"[^A-Za-z0-9]", "", str(k).title()) for k in keywords]
    return " ".join(f"#{word}" for word in words[:3] if word)


def _free_article_photo(title: str, out: Path) -> tuple[Path, str] | None:
    """The article's lead photo and its credit line, only if it may be reused."""
    from .verified_facts import _api, fetch

    pages = _api({"action": "query", "prop": "pageimages", "piprop": "name", "redirects": "1",
                  "titles": title}).get("query", {}).get("pages") or []
    name = (pages[0] if pages else {}).get("pageimage")
    if not name:
        return None
    info = _api({"action": "query", "titles": f"File:{name}", "prop": "imageinfo",
                 "iiprop": "url|extmetadata", "iiurlwidth": "2000"}).get("query", {}).get("pages") or []
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
    out.write_bytes(fetch(url))
    artist = re.sub(r"<[^>]+>", "", html.unescape(str((meta.get("Artist") or {}).get("value") or "")))
    artist = " ".join(artist.split())
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


CARD_W, CARD_H = 1080, 1350          # 4:5, the tallest picture Facebook shows whole in the feed
_CARD_MARGIN = 72


def _card_text_width(text: str, size: int) -> float:
    """Drawn width of mixed-case text. thumbnails.text_width_px measures capitals,
    which made ordinary words look a fifth wider than they are and cut them short."""
    from .thumbnails import text_width_px

    return text_width_px(text, size) * 0.86


def _wrap_card_text(text: str, max_lines: int) -> tuple[list[str], int]:
    """The headline as lines, in the largest size that fits the card whole."""
    words = " ".join(str(text).split()).split(" ")
    width = CARD_W - 2 * _CARD_MARGIN
    lines: list[str] = []
    for size in (96, 84, 74, 64, 56, 48, 42):
        lines, current = [], ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and _card_text_width(trial, size) > width:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
        if len(lines) <= max_lines and all(_card_text_width(line, size) <= width for line in lines):
            return lines, size
    # Still too long at the smallest size: keep what fits and say it goes on.
    return [*lines[:max_lines - 1], lines[max_lines - 1].rstrip(" ,;:") + "…"], 42


def subject_blurb(title: str) -> str:
    """Wikipedia's own one-line description of a subject ("Polish humanitarian (1910–2008)")."""
    try:
        from .verified_facts import _api

        pages = _api({"action": "query", "prop": "description", "redirects": "1", "titles": title})
        text = str((pages.get("query", {}).get("pages") or [{}])[0].get("description") or "")
        return text[:1].upper() + text[1:]
    except Exception:
        return ""


def post_card(photo: Path, headline: str, subtitle: str, channel: Channel, out: Path,
              max_lines: int = 3) -> Path:
    """The picture for a Page post: the photo edge to edge, the words on a soft dark fade.

    The first version squeezed a tall portrait into a 1280x720 frame between
    blurred side bars, which threw away most of the photo's detail and looked
    cheap. This keeps the photo at full width in a tall 4:5 card. A photo too
    small to fill the card is shown framed at its own size instead of being
    blown up into a blur.
    """
    import subprocess

    import cv2

    from .thumbnails import _BADGE_COLORS, _escape, _ffmpeg, _font_path, _write_text

    image = cv2.imread(str(photo))
    if image is None:
        raise RuntimeError("the photo could not be read")
    height, width = image.shape[:2]
    accent, accent_text = _BADGE_COLORS.get(str(channel.genre).lower(), ("0xf2b441", "0x14110a"))
    font = _font_path()
    font_arg = f":fontfile='{_escape(font)}'" if font else ""

    if min(width / CARD_W, height / CARD_H) >= 0.6:
        # Fill the card; a portrait keeps its upper part, where the face is.
        picture = (
            f"[0:v]scale={CARD_W}:{CARD_H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={CARD_W}:{CARD_H}:(iw-{CARD_W})/2:(ih-{CARD_H})*0.18[pic]"
        )
    else:
        scale = min(1.5, (CARD_W - 160) / width, 760 / height)
        w, h = int(width * scale) // 2 * 2, int(height * scale) // 2 * 2
        picture = (
            f"[0:v]split[a][b];"
            f"[a]scale={CARD_W}:{CARD_H}:force_original_aspect_ratio=increase,crop={CARD_W}:{CARD_H},"
            f"boxblur=34:6,eq=brightness=-0.22:saturation=0.8[back];"
            f"[b]scale={w}:{h}:flags=lanczos,pad={w + 12}:{h + 12}:6:6:color=white[front];"
            f"[back][front]overlay=(W-w)/2:120[pic]"
        )

    lines, size = _wrap_card_text(headline, max_lines)
    line_height = int(size * 1.14)
    subtitle = " ".join(str(subtitle or "").split())
    while subtitle and _card_text_width(subtitle, 34) > CARD_W - 2 * _CARD_MARGIN:
        subtitle = subtitle.rsplit(" ", 1)[0].rstrip(" ,;:(") + "…" if " " in subtitle else ""
    bottom = CARD_H - 84 - (54 if subtitle else 0)
    top = bottom - len(lines) * line_height
    # Dark towards the bottom for the words, a touch at the top for the badge.
    fade = (
        f"color=c=black:s={CARD_W}x{CARD_H}:d=1,format=rgba,"
        f"geq=r=0:g=0:b=0:a='255*max(0.93*pow(clip((Y-{top - 330})/380\\,0\\,1)\\,1.2)\\,"
        f"0.5*clip((170-Y)/170\\,0\\,1))'[fade]"
    )
    layers = [
        f"drawtext=textfile='{_write_text(out.with_suffix('.badge.txt'), channel.name.upper())}'{font_arg}:"
        f"fontsize=26:fontcolor={accent_text}:x={_CARD_MARGIN}:y=64:box=1:boxcolor={accent}@0.95:boxborderw=14",
        f"drawbox=x={_CARD_MARGIN}:y={top - 40}:w=110:h=8:color={accent}@1:t=fill",
    ]
    for index, line in enumerate(lines):
        layers.append(
            f"drawtext=textfile='{_write_text(out.with_suffix(f'.line{index}.txt'), line)}'{font_arg}:"
            f"fontsize={size}:fontcolor=white:x={_CARD_MARGIN}:y={top + index * line_height}:"
            "shadowcolor=black@0.55:shadowx=0:shadowy=4"
        )
    if subtitle:
        layers.append(
            f"drawtext=textfile='{_write_text(out.with_suffix('.sub.txt'), subtitle)}'{font_arg}:"
            f"fontsize=34:fontcolor=0xe3e9ff@0.95:x={_CARD_MARGIN}:y={bottom + 16}"
        )
    graph = f"{picture};{fade};[pic][fade]overlay=0:0,{','.join(layers)}"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_ffmpeg(), "-y", "-i", str(photo), "-filter_complex", graph,
         "-frames:v", "1", "-update", "1", "-q:v", "1", str(out)],
        check=True, capture_output=True, text=True,
    )
    return out


def real_photo(title: str, work: Path) -> tuple[Path, str] | None:
    """A Wikipedia subject's own free photo, fitted whole to the card, and its credit line."""
    found = _free_article_photo(title, work / "article_photo.jpg")
    if not found:
        return None
    photo, credit = found
    return _whole_photo(photo, work / "article_photo_fitted.jpg"), credit


def _verified_post(channel: Channel, work: Path) -> tuple[str, Path, str]:
    """(caption, picture, subject) about a real person, from their Wikipedia article.

    The picture is always that person's own photo. Someone whose article has
    no photo free to reuse is passed over for the next person: a post about a
    real person never goes out with a stand-in picture.
    """
    from . import verified_facts as vf
    used = _posted_subjects(channel.id) | vf._used_subjects(channel, False)
    names = [name for name in vf._listed_subjects() if name.lower() not in used]
    article = photo = None
    credit = ""
    for name in names[:12]:
        try:
            candidate = vf.fetch_article(name)
            if candidate.title.lower() in used:
                continue
            found = _free_article_photo(candidate.title, work / "article_photo.jpg")
        except Exception as exc:
            print(f"[page_posts] {channel.id}: skipping {name} ({exc})")
            continue
        if not found:
            print(f"[page_posts] {channel.id}: skipping {name} (no photo of them that is free to reuse)")
            continue
        article, (photo, credit) = candidate, found
        break
    if article is None:
        raise RuntimeError("no unused real subject with a real photo was found for a picture post")
    key, raw = vf.write_lesson(channel, article, scenes=5, long_form=False)
    lesson = Lesson(
        topic_key=key, title=article.title, category=raw["category"], intro="", outro="",
        scenes=[Scene(**scene) for scene in raw["scenes"][:5]],
    )
    title, lines = _in_page_language(channel, lesson, strict=True)
    picture = post_card(photo, article.title, subject_blurb(article.title), channel, work / "page_post.jpg")
    caption = "\n\n".join(filter(None, [
        title, "\n".join(lines), f"Source: Wikipedia — {article.url}", credit,
        # The person's own name as the tag: a channel keyword such as
        # "#AncientCivilizations" would be a false label on most of them.
        "#" + re.sub(r"[^A-Za-z0-9]", "", article.title),
    ]))
    return caption, picture, article.title


def _news_subjects(lesson: Lesson) -> list[str]:
    """Wikipedia titles of who or what a news script is about, most central first.

    The model only proposes names. A name is kept when its own words stand in
    the script, so it cannot hand back someone the story never mentions.
    """
    from .genre_topics import _script_json

    text = " ".join([lesson.title, *[scene.line for scene in lesson.scenes]])
    raw = _script_json(
        "Below is a short news script. Name the real person, organisation, place or event it is "
        "mainly about, as English Wikipedia article titles: the most central first, at most 4. "
        "Only subjects the script itself names.\n"
        'Return ONLY JSON: {"subjects": [str]}\n\n' + text
    )
    lowered = text.lower()
    kept = []
    for name in raw.get("subjects") or []:
        words = [word for word in re.findall(r"[a-z]+", str(name).lower()) if len(word) > 2]
        if words and all(word in lowered for word in words[-2:]):
            kept.append(str(name).strip())
    return kept


def _trending_post(channel: Channel, work: Path) -> tuple[str, Path, str]:
    """(caption, picture, subject) for a news channel: its newest story with a real picture.

    The text is the channel's own newest news script, not a new piece of
    writing. The picture is the free Wikipedia photo of the person, place or
    organisation the story is about. With no such photo there is no post.
    """
    from .genre_topics import load_genre_lessons

    posted = _posted_subjects(channel.id)
    stories = [
        (key, raw) for key, raw in load_genre_lessons(channel).items()
        if key.startswith("trend_") and str(raw.get("title") or "").lower() not in posted
    ]
    for key, raw in reversed(stories[-6:]):          # newest first
        lesson = Lesson(
            topic_key=key, title=str(raw["title"]), category="Trending", intro="", outro="",
            scenes=[Scene(**scene) for scene in raw["scenes"][:4]],
        )
        for subject in _news_subjects(lesson):
            try:
                found = _free_article_photo(subject, work / "article_photo.jpg")
            except Exception as exc:
                print(f"[page_posts] {channel.id}: no photo for {subject} ({exc})")
                continue
            if not found:
                continue
            photo, credit = found
            title, lines = _in_page_language(channel, lesson, strict=False)
            picture = post_card(
                photo, str(raw["title"]), f"Pictured: {subject}", channel, work / "page_post.jpg", max_lines=4,
            )
            caption = "\n\n".join(filter(None, [
                title, "\n".join(lines), f"Pictured: {subject}. {credit}", _hashtags(channel) or "#News",
            ]))
            return caption, picture, str(raw["title"])
    raise RuntimeError("no recent story has a real, free-to-use picture of its subject; nothing posted")


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
    from .trending import is_trending_channel

    build = (_verified_post if is_verified_channel(channel)
             else _trending_post if is_trending_channel(channel) else _written_post)
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
