"""One picture post a day on a channel's Facebook Page, besides its videos.

Switched on per channel ("Also post one picture a day" in the Facebook
settings). The post is a picture with a headline burned in and a short
blog-style text under it (a few paragraphs, ending with a question for the
comments), in the language chosen for that Page. It is scheduled on Facebook
for the hour the Page's readers are most active (BEST_POST_TIME), whatever
hour the run happens.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .channels import Channel, load_channels
from .config import settings
from .db import connect
from .models import Lesson, Scene
from .social_accounts import account

# Licences that allow reposting with a credit line.
_FREE_LICENCE = re.compile(r"^(public domain|pd\b|cc0|cc[ -]by)", re.I)


def daily_post_on(channel_id: str) -> bool:
    from .social_accounts import facebook_posts_here

    cfg = account(channel_id, "facebook")
    return bool(cfg.get("daily_post") and cfg.get("page_id") and cfg.get("page_access_token")
                and facebook_posts_here())


def _table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS page_posts (
               id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, day TEXT NOT NULL,
               subject TEXT, post_id TEXT, post_url TEXT, created_at TEXT NOT NULL)"""
    )


# When each kind of Page's readers are most active, US Eastern time (the
# channels' audience): news in the morning, history over lunch, bedtime stories
# before the children sleep, true crime in the evening. A Page's own
# "post_time" setting ("HH:MM") overrides this.
POST_TZ = ZoneInfo("America/New_York")
BEST_POST_TIME = {
    "trending": "08:30",
    "heroic history": "12:30",
    "history": "12:30",
    "bedtime stories": "19:30",
    "crime": "20:00",
}
_DEFAULT_POST_TIME = "13:00"


def next_post_time(channel: Channel, now: datetime | None = None) -> datetime:
    """The next best posting moment for this Page, at least 20 minutes ahead
    (Facebook schedules no sooner than 10 minutes)."""
    now = now or datetime.now(timezone.utc)
    wanted = str(account(channel.id, "facebook").get("post_time")
                 or BEST_POST_TIME.get(str(channel.genre).lower(), _DEFAULT_POST_TIME))
    hour, minute = (int(part) for part in wanted.split(":", 1))
    local = now.astimezone(POST_TZ)
    when = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when < local + timedelta(minutes=20):
        when += timedelta(days=1)
    return when.astimezone(timezone.utc)


def _post_day(when: datetime) -> str:
    return when.astimezone(POST_TZ).date().isoformat()


def posted_on(channel_id: str, day: str) -> bool:
    """A post is already made (or scheduled) for this Page on this day."""
    conn = connect()
    try:
        _table(conn)
        return conn.execute(
            "SELECT 1 FROM page_posts WHERE channel = ? AND day = ?", (channel_id, day),
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


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _page_messages(channel_id: str, limit: int = 60) -> list[str]:
    """The text of the Page's recent and scheduled posts, read from Facebook.

    The local record only knows posts made on this machine; the PC and the
    GitHub run each posted Harriet Tubman once on the same Page. Facebook
    itself knows every post, whoever made it.
    """
    from urllib.parse import urlencode

    from .social_publish import FB_API_VERSION, _request_json

    cfg = account(channel_id, "facebook")
    page_id, token = str(cfg.get("page_id") or ""), str(cfg.get("page_access_token") or "")
    if not page_id or not token:
        return []
    messages: list[str] = []
    for edge in ("posts", "scheduled_posts"):
        query = urlencode({"fields": "message", "limit": limit, "access_token": token})
        try:
            data = _request_json(f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/{edge}?{query}")
        except Exception as exc:
            print(f"[page_posts] {channel_id}: could not read the Page's {edge} ({exc})")
            continue
        messages += [str(row.get("message") or "") for row in data.get("data") or []]
    return messages


def _on_page(subject: str, page_text: str) -> bool:
    """True when the Page already has a post about `subject`. Matched on the
    squashed name, so an Urdu post still matches through its #Name tag and its
    Wikipedia link (wiki/Harriet_Tubman)."""
    key = _squash(subject)
    return len(key) >= 5 and key in page_text


def _record(channel_id: str, day: str, subject: str, post_id: str, post_url: str) -> None:
    conn = connect()
    try:
        _table(conn)
        conn.execute(
            "INSERT INTO page_posts (channel, day, subject, post_id, post_url, created_at) VALUES (?,?,?,?,?,?)",
            (channel_id, day, subject, post_id, post_url, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _hashtags(channel: Channel) -> str:
    from .seo import _GENRE_KEYWORDS

    keywords = list(getattr(channel, "keywords", ()) or ()) or _GENRE_KEYWORDS.get(channel.genre, [])
    words = [re.sub(r"[^A-Za-z0-9]", "", str(k).title()) for k in keywords]
    return " ".join(f"#{word}" for word in words[:3] if word)


COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
# A file with one of these in its name shows something named after the person
# or made by them, not the person: "Ruth-Pfau-Schule.jpg" is a school.
_NOT_THE_PERSON = (
    "signature", "autograph", "grave", "tomb", "mausoleum", "memorial", "monument", "statue", "bust",
    "plaque", "stamp", "coin", "medal", "map", "logo", "flag", "coat of arms", "school", "schule",
    "college", "university", "hospital", "airport", "road", "street", "station", "stadium", "park",
    "building", "house", "museum", "book", "cover", "poster", "letter", "document", "painting of",
    "family tree", "diagram", "chart",
)
_PHOTO_INFO = {"prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": "2000"}


def _usable(page: dict, own_repository: bool = True) -> tuple[str, str, int] | None:
    """(download url, credit line, shorter side in px) of a file that may be reused, else None."""
    image = (page.get("imageinfo") or [{}])[0]
    meta = image.get("extmetadata") or {}
    licence = str((meta.get("LicenseShortName") or {}).get("value") or "")
    # A file kept on Wikipedia itself (not Commons) is usually a non-free
    # picture used under "fair use", which does not cover a Page post.
    if not own_repository or (meta.get("NonFree") or {}).get("value") or not _FREE_LICENCE.match(licence):
        return None
    url = str(image.get("thumburl") or image.get("url") or "")
    if not url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png")):
        return None
    artist = re.sub(r"<[^>]+>", "", html.unescape(str((meta.get("Artist") or {}).get("value") or "")))
    artist = " ".join(artist.split())
    # Commons repeats the name in a hidden copy ("Unknown authorUnknown author").
    artist = re.sub(r"^(.+?)\1$", r"\1", artist)
    credit = f"Photo: {artist[:80] or 'Wikimedia Commons'} ({licence}), via Wikimedia Commons"
    return url, credit, min(int(image.get("width") or 0), int(image.get("height") or 0))


def _commons_photo(title: str) -> tuple[str, str] | None:
    """A free photo of the person from the whole of Wikimedia Commons: (url, credit).

    Many articles lead with a picture Wikipedia only holds under "fair use",
    while Commons has free photos of the same person. They are found through
    the person's Wikidata entry, not by searching for the name, so a namesake
    cannot slip in: first the entry's own portrait (P18), then the files in
    the person's Commons category (P373).
    """
    from .verified_facts import _api

    pages = _api({"action": "query", "prop": "pageprops", "ppprop": "wikibase_item",
                  "redirects": "1", "titles": title}).get("query", {}).get("pages") or []
    entity = ((pages[0] if pages else {}).get("pageprops") or {}).get("wikibase_item")
    if not entity:
        return None
    claims = _api({"action": "wbgetclaims", "entity": entity}, base=WIKIDATA_API).get("claims") or {}

    def value(prop: str) -> str:
        try:
            return str(claims[prop][0]["mainsnak"]["datavalue"]["value"])
        except (KeyError, IndexError, TypeError):
            return ""

    portrait, category = value("P18"), value("P373")
    if portrait:
        found = _api({"action": "query", "titles": f"File:{portrait}", **_PHOTO_INFO},
                     base=COMMONS_API).get("query", {}).get("pages") or []
        usable = _usable(found[0]) if found else None
        if usable:
            return usable[0], usable[1]
    if not category:
        return None
    files = _api({"action": "query", "generator": "categorymembers", "gcmtitle": f"Category:{category}",
                  "gcmtype": "file", "gcmlimit": "60", **_PHOTO_INFO},
                 base=COMMONS_API).get("query", {}).get("pages") or []
    best: tuple[float, str, str] | None = None
    for page in files:
        name = str(page.get("title") or "").lower()
        if any(word in name for word in _NOT_THE_PERSON):
            continue
        usable = _usable(page)
        if not usable or usable[2] < 500:
            continue
        image = page["imageinfo"][0]
        # Sharper is better; an upright picture is more likely a portrait.
        score = min(usable[2], 2000) * (1.25 if image.get("height", 0) >= image.get("width", 0) else 1.0)
        if best is None or score > best[0]:
            best = (score, usable[0], usable[1])
    return (best[1], best[2]) if best else None


def _free_article_photo(title: str, out: Path) -> tuple[Path, str] | None:
    """A photo of the subject that may be reused, and its credit line.

    The article's own lead picture when that is free; otherwise a free photo
    of the same person from Wikimedia Commons. None when there is neither.
    """
    from .verified_facts import _api, fetch

    pages = _api({"action": "query", "prop": "pageimages", "piprop": "name", "redirects": "1",
                  "titles": title}).get("query", {}).get("pages") or []
    name = (pages[0] if pages else {}).get("pageimage")
    chosen: tuple[str, str] | None = None
    if name:
        info = _api({"action": "query", "titles": f"File:{name}", **_PHOTO_INFO}).get("query", {}).get("pages") or []
        page = info[0] if info else {}
        usable = _usable(page, own_repository=page.get("imagerepository") == "shared")
        if usable:
            chosen = usable[0], usable[1]
    if chosen is None:
        chosen = _commons_photo(title)
    if chosen is None:
        return None
    out.write_bytes(fetch(chosen[0]))
    return out, chosen[1]


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
    page_text = _squash(" ".join(_page_messages(channel.id)))
    names = [name for name in vf._listed_subjects(channel)
             if name.lower() not in used and not _on_page(name, page_text)]
    article = photo = None
    credit = ""
    for name in names[:12]:
        try:
            # The long article text: a hero's post tells the whole life, not a few lines.
            candidate = vf.fetch_article(name, long_form=True)
            if candidate.title.lower() in used or _on_page(candidate.title, page_text):
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
    # Long form: about 22 single facts, each checked, joined into 6 paragraphs.
    # When too few of them stand in the article, 10 checked facts instead.
    try:
        key, raw = vf.write_lesson(channel, article, scenes=6, long_form=True, min_words=280)
    except RuntimeError as exc:
        print(f"[page_posts] {channel.id}: long post failed its check ({exc}); writing 10 facts")
        key, raw = vf.write_lesson(channel, article, scenes=10, long_form=False)
    lesson = Lesson(
        topic_key=key, title=article.title, category=raw["category"], intro="", outro="",
        scenes=[Scene(**scene) for scene in raw["scenes"]],
    )
    title, lines = _in_page_language(channel, lesson, strict=True)
    picture = post_card(photo, article.title, subject_blurb(article.title), channel, work / "page_post.jpg")
    caption = "\n\n".join(filter(None, [
        title, _paragraphs(lines, per=1), _question(channel, article.title),
        f"Source: Wikipedia — {article.url}", credit,
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
    page_text = _squash(" ".join(_page_messages(channel.id)))
    stories = [
        (key, raw) for key, raw in load_genre_lessons(channel).items()
        if key.startswith("trend_") and str(raw.get("title") or "").lower() not in posted
        and not _on_page(str(raw.get("title") or ""), page_text)
    ]
    for key, raw in reversed(stories[-6:]):          # newest first
        lesson = Lesson(
            topic_key=key, title=str(raw["title"]), category="Trending", intro="", outro="",
            scenes=[Scene(**scene) for scene in raw["scenes"]],
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
                title, _paragraphs(lines), _question(channel, subject), f"Pictured: {subject}. {credit}",
                _hashtags(channel) or "#News",
            ]))
            return caption, picture, str(raw["title"])
    raise RuntimeError("no recent story has a real, free-to-use picture of its subject; nothing posted")


def _written_post(channel: Channel, work: Path) -> tuple[str, Path, str]:
    """(caption, picture, subject) for an ordinary channel, written in its own tone."""
    from .genre_topics import GENRE_TONE, _script_json
    from .thumbnails import create_thumbnail

    tone = getattr(channel, "tone", "") or GENRE_TONE.get(channel.genre, "engaging")
    # Headlines from this machine's record and the first line of each recent
    # post on the Page itself, wherever it was made.
    on_page = [message.split("\n", 1)[0].strip() for message in _page_messages(channel.id)]
    avoid = "; ".join([*sorted(_posted_subjects(channel.id))[-30:], *filter(None, on_page[:30])]) or "none"
    raw = _script_json(
        f"Write one short Facebook Page post for the {channel.genre} page '{channel.name}'. Tone: {tone}.\n"
        "A short blog post: a headline of at most 8 words and 8 to 10 sentences (about 180-230 words) "
        "that are useful or interesting on their own, telling one small story or idea from start to end. "
        "The last sentence asks the readers a simple question to answer in the comments. "
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
        scenes=[Scene(label=title[:30], line=line, image="", image_prompt=prompt) for line in lines[:10]],
    )
    shown_title, shown_lines = _in_page_language(channel, lesson, strict=False)
    picture = create_thumbnail(lesson, None, work / "page_post.jpg", channel, "long")
    caption = "\n\n".join(filter(None, [shown_title, _paragraphs(shown_lines), _hashtags(channel)]))
    return caption, picture, title


def _paragraphs(lines: list[str], per: int = 2) -> str:
    """Sentences grouped into short paragraphs, the way a blog post reads."""
    return "\n\n".join(" ".join(lines[i:i + per]) for i in range(0, len(lines), per))


def _question(channel: Channel, subject: str) -> str:
    """A closing question for the comments. It states no fact, so it needs no check."""
    from .language import is_urdu

    if is_urdu(channel) or str(account(channel.id, "facebook").get("language") or "") == "ur":
        return f"آپ {subject} کی کہانی کے بارے میں کیا سوچتے ہیں؟ کمنٹ میں ضرور بتائیں۔"
    return f"What do you think about {subject}? Tell us in the comments."


def publish_daily_post(channel: Channel, dry_run: bool = False, when: datetime | None = None,
                       right_now: bool = False) -> str:
    """Make the day's picture post for one channel and schedule it on its Page.

    It goes live at `when` (default: next_post_time), or at once with
    `right_now`. `dry_run` builds the picture and text and shows them without
    posting.
    """
    when = datetime.now(timezone.utc) if right_now else (when or next_post_time(channel))
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
    from .social_accounts import facebook_elsewhere_reason, facebook_posts_here

    if not dry_run and not facebook_posts_here():
        raise RuntimeError(facebook_elsewhere_reason())
    local = when.astimezone(POST_TZ).strftime("%d %b %I:%M %p ET")
    if dry_run:
        return f"NOT posted (dry run; would go live {local}). Picture: {picture}\n{caption}"
    fields = {"caption": caption, "access_token": token}
    if not right_now:
        fields.update({"published": "false", "scheduled_publish_time": str(int(when.timestamp()))})
    body, content_type = _multipart(fields, "source", picture)
    result = _send_binary(
        f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/photos", body,
        {"Content-Type": content_type}, "POST",
    )
    post_id = str(result.get("post_id") or result.get("id") or "")
    if not post_id:
        raise RuntimeError(f"Facebook did not accept the picture post: {result}")
    url = f"https://www.facebook.com/{post_id}"
    _record(channel.id, _post_day(when), subject, post_id, url)
    return f"{'posted' if right_now else 'scheduled'} '{subject}' for {local} -> {url}"


def run_daily_posts(slot: set[int] | None = None, dry_run: bool = False) -> list[tuple[str, str]]:
    """Today's picture post for every channel that has it switched on."""
    from .slots import channel_slot

    results: list[tuple[str, str]] = []
    for channel in load_channels():
        if slot is not None and channel_slot(channel) not in slot:
            continue
        if not daily_post_on(channel.id):
            continue
        when = next_post_time(channel)
        if posted_on(channel.id, _post_day(when)) and not dry_run:
            results.append((channel.id, f"SKIPPED the post for {_post_day(when)} is already scheduled"))
            continue
        try:
            results.append((channel.id, "OK " + publish_daily_post(channel, dry_run=dry_run, when=when)))
        except Exception as exc:
            results.append((channel.id, f"FAILED {exc}"))
    return results
