"""Scripts for fact-only channels: every sentence comes from a Wikipedia article.

A channel with "verified_facts": true in channels.json tells true stories about
real people. The free-writing script prompt cannot be trusted for that: asked
for "a heroic history story" it happily invents heroes, numbers and outcomes.
So for these channels the subject is a real person with a Wikipedia article,
the script writer is given that article and nothing else to draw on, and the
draft is checked before it is saved:

  1. every number in a line must stand in the article;
  2. every name (capitalised word) in a line must stand in the article;
  3. a second model reads the article and marks any line it does not support.

A line that fails is dropped. A script left too short is thrown away rather
than padded, so a bad day makes no video instead of an invented one. The
article link is kept with the script and added to the video description.
"""
from __future__ import annotations

import json
import random
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .channels import Channel
from .config import settings
from .language import is_urdu, script_rules

WIKI_API = "https://en.wikipedia.org/w/api.php"
SUBJECTS_FILE = settings.root / "data" / "verified_subjects.json"
# Wikimedia asks for a client that says who it is, and throttles ones that do not.
USER_AGENT = "StoryBotStudio/1.0 (https://github.com/Khalidawan787/story-bot-studio; fact-checked scripts)"

# Sections that are lists of links or trivia, not the person's story.
_CUT_SECTIONS = (
    "see also", "references", "external links", "notes", "further reading",
    "bibliography", "sources", "citations", "footnotes", "in popular culture",
    "filmography", "works", "publications", "gallery",
)
_SHORT_SOURCE_CHARS = (1500, 9000)    # (least needed, most sent to the writer)
_LONG_SOURCE_CHARS = (14000, 20000)
_LONG_MIN_WORDS = 720                 # about 4.5 minutes of narration
_SHORT_MIN_SCENES = 5


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    text: str


def is_verified_channel(channel) -> bool:
    return bool(getattr(channel, "verified_facts", False))


def _api(params: dict, base: str = WIKI_API) -> dict:
    """One call to a MediaWiki API: Wikipedia by default, or Commons / Wikidata via `base`."""
    query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
    return json.loads(fetch(f"{base}?{query}").decode("utf-8"))


def fetch(url: str, timeout: int = 60) -> bytes:
    """GET from Wikipedia or Wikimedia, waiting and trying again when told to slow down."""
    import time
    import urllib.error

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for wait in (4, 12, 30, None):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if wait is None or exc.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(wait)
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            # A dropped connection; Commons does this now and then.
            if wait is None:
                raise
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _story_text(extract: str, limit: int) -> str:
    """The article without its link lists, cut at a paragraph end."""
    kept: list[str] = []
    skipping = False
    for block in extract.split("\n"):
        heading = re.fullmatch(r"\s*(=+)\s*(.+?)\s*=+\s*", block)
        if heading:
            # A top-level section decides; its sub-sections follow it.
            if len(heading.group(1)) == 2:
                skipping = heading.group(2).strip().lower() in _CUT_SECTIONS
            continue
        if not skipping and block.strip():
            kept.append(block.strip())
    text = ""
    for paragraph in kept:
        if text and len(text) + len(paragraph) > limit:
            break
        text += paragraph + "\n"
    return text.strip()[: limit + 2000]


def fetch_article(title: str, long_form: bool = False) -> Article:
    """The Wikipedia article called `title`, or an error if it cannot carry a script."""
    least, most = _LONG_SOURCE_CHARS if long_form else _SHORT_SOURCE_CHARS
    data = _api({
        "action": "query", "prop": "extracts|pageprops|info", "explaintext": "1",
        "redirects": "1", "inprop": "url", "titles": title,
    })
    pages = data.get("query", {}).get("pages") or []
    page = pages[0] if pages else {}
    if not page or page.get("missing") or not page.get("extract"):
        raise RuntimeError(f"No Wikipedia article for '{title}'")
    if "disambiguation" in (page.get("pageprops") or {}):
        raise RuntimeError(f"'{title}' is a list of several articles, not one subject")
    text = _story_text(str(page["extract"]), most)
    if len(text) < least:
        raise RuntimeError(f"The article on '{page.get('title', title)}' is too short for this video")
    url = str(page.get("fullurl") or
              "https://en.wikipedia.org/wiki/" + urllib.parse.quote(str(page["title"]).replace(" ", "_")))
    return Article(title=str(page["title"]), url=url, text=text)


def search_article(query: str, long_form: bool = False) -> Article:
    """The best-matching article for words a person typed on the dashboard."""
    hits = _api({"action": "query", "list": "search", "srsearch": query, "srlimit": "3"})
    for hit in hits.get("query", {}).get("search") or []:
        try:
            return fetch_article(str(hit["title"]), long_form)
        except Exception:
            continue
    raise RuntimeError(
        f"Nothing on Wikipedia matches '{query[:80]}'. This channel only tells stories "
        "that can be checked: type the real name of a person or an event."
    )


# ---------------------------------------------------------------- subjects --

def _bank_rows(channel: Channel) -> dict:
    from .genre_topics import load_custom_lessons, load_genre_lessons

    rows = dict(load_genre_lessons(channel))
    rows.update(load_custom_lessons(channel))
    return rows


# Subjects whose script could not be written in this run; not offered again.
_FAILED: dict[tuple[str, bool], set[str]] = {}


def _used_subjects(channel: Channel, long_form: bool) -> set[str]:
    used = {
        str(raw.get("source_title") or "").lower()
        for key, raw in _bank_rows(channel).items()
        if raw.get("source_title") and key.startswith("long_") == long_form
    }
    return used | _FAILED.get((channel.id, long_form), set())


def _listed_subjects(channel: Channel | None = None) -> list[str]:
    """The channel's genre list when there is one (crime cases), else the people list."""
    try:
        data = json.loads(SUBJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    country = str(getattr(channel, "focus_country", "") or "").strip()
    if country:
        # A country run (focus_country) takes only that country's people; once
        # they are used up, _suggested_subjects asks for more from it.
        return [str(name) for name in (data.get("subjects_by_country") or {}).get(country, [])]
    genre = str(getattr(channel, "genre", "") or "").lower()
    names = (data.get("subjects_by_genre") or {}).get(genre)
    if names is None:
        # The people list is for the history/heroes channels only. A crime Page
        # once got Rosa Parks and Abdul Sattar Edhi because its own list was
        # missing (an old copy of this file came back from the bot-state
        # branch); with no list of its own a channel now asks for subjects of
        # its own genre instead (_suggested_subjects).
        names = data.get("subjects") or [] if genre in ("", "heroic history", "history") else []
    return [str(name) for name in names]


# What to ask for when a genre's list runs out. The model only proposes names;
# each must still be a Wikipedia article, and the script comes from that article.
_SUGGEST_BRIEF = {
    "crime": (
        "Name 15 real, famous, well-documented criminal cases or criminals from history whose "
        "story fits a true-crime video channel: heists, robberies, frauds and swindles, outlaws, "
        "prison escapes, notable investigations. Only subjects with their own English Wikipedia "
        "article, settled long ago (before 2015). No cases whose victims were children, no "
        "serial or mass killings, nobody who was never charged, no living politicians."
    ),
}


def _suggested_subjects(channel: Channel, used: set[str]) -> list[str]:
    """More real people, asked from the script model once the list is used up.

    The model only proposes names. Each still has to be a Wikipedia article,
    and the script is written from that article, so a made-up name ends here.
    """
    from .genre_topics import _script_json

    brief = _SUGGEST_BRIEF.get(str(channel.genre).lower()) or (
        f"Name 15 real, well-documented people whose life story fits a "
        f"'{channel.genre}' video channel: heroes of courage, sacrifice or service, and also "
        "famous lives - cricketers, other sports stars, scientists and inventors, past national "
        "leaders, Hollywood, Bollywood and Pakistani film and music stars. Mix the kinds. "
        "Only people with their own English Wikipedia article. No fictional or legendary "
        "figures, no religious prophets, no living politicians."
    )
    country = str(getattr(channel, "focus_country", "") or "").strip()
    if country:
        brief += f" All 15 must be from {country} (born there or best known as its citizens)."
    raw = _script_json(
        brief + "\n"
        f"Do not repeat any of these: {'; '.join(sorted(used))[:3000] or 'none'}.\n"
        'Return ONLY JSON: {"names": [str]}'
    )
    return [str(name).strip() for name in raw.get("names") or [] if str(name).strip()]


def next_article(channel: Channel, long_form: bool = False) -> Article:
    """A real person this channel has not covered yet in this format."""
    used = _used_subjects(channel, long_form)
    names = [name for name in _listed_subjects(channel) if name.lower() not in used]
    # Each channel walks the list in its own order, and its long videos in
    # another, so a channel's Short and long video are not the same person.
    random.Random(f"{channel.id}:{long_form}").shuffle(names)
    for pool in (names, None):
        if pool is None:
            try:
                pool = [name for name in _suggested_subjects(channel, used) if name.lower() not in used]
            except Exception as exc:
                raise RuntimeError(f"No unused real subject left and none could be suggested ({exc})") from exc
        for name in pool[:12]:
            try:
                article = fetch_article(name, long_form)
            except Exception as exc:
                print(f"[verified_facts] {channel.id}: skipping {name} ({exc})")
                continue
            if article.title.lower() not in used:
                return article
    raise RuntimeError("No real subject with a usable Wikipedia article was found")


# ------------------------------------------------------------------ checks --

_EASTERN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
# Capitalised mid-sentence but not names of anything.
_PLAIN_CAPITALS = {"i", "god", "he", "she", "his", "her", "they", "the", "a", "an", "in", "on", "at", "it"}


def numbers_in(text: str) -> set[str]:
    """Every number in the text, as plain digits ("1,800" and "۱۸۰۰" -> "1800")."""
    text = str(text).translate(_EASTERN_DIGITS)
    return {token.replace(",", "").lstrip("0") or "0" for token in re.findall(r"\d[\d,]*", text)}


def _names_in(line: str) -> set[str]:
    """Capitalised words that do not merely open a sentence."""
    names: set[str] = set()
    for sentence in re.split(r"(?<=[.!?:;])\s+", line):
        for word in sentence.split()[1:]:
            word = re.sub(r"['’]s$", "", word.strip("\"'“”‘’()[],.;:!?—–-"))
            if len(word) > 2 and word[0].isupper() and word.lower() not in _PLAIN_CAPITALS:
                names.add(word)
    return names


def unsupported_reason(line: str, article: Article, check_names: bool = True) -> str | None:
    """Why this line cannot be shown to stand in the article, or None."""
    source_numbers = numbers_in(article.text)
    missing = numbers_in(line) - source_numbers
    if missing:
        return f"number(s) not in the article: {', '.join(sorted(missing))}"
    if check_names:
        source = (article.text + " " + article.title).lower()
        unknown = sorted(name for name in _names_in(line) if name.lower() not in source)
        if unknown:
            return f"name(s) not in the article: {', '.join(unknown)}"
    return None


def _audit(lines: list[str], article: Article) -> dict[int, str]:
    """Lines a second read of the article does not support: {index from 0: missing claim}."""
    from .genre_topics import _script_json

    numbered = "\n".join(f"{index}. {line}" for index, line in enumerate(lines, start=1))
    raw = _script_json(
        "You are a strict fact checker. Below is a SOURCE and numbered LINES of a video script.\n"
        "Mark a line as unsupported when it states a fact that the SOURCE does not state: a name, "
        "date, number, place, event, cause, outcome, quote, feeling or motive. Rewording, shortening "
        "or joining facts that the SOURCE does state is fine, and praise is fine when the SOURCE says "
        "the same thing. Mark a line only when you can quote the claim the SOURCE is missing.\n"
        'Return O\nY JSON: {"unsupported": [{"line": number, "claim": "the words not in the SOURCE"}]}\n\n'
        f"SOURCE:\n{article.text}\n\nLINES:\n{numbered}"
    )
    marked: dict[int, str] = {}
    for item in raw.get("unsupported") or []:
        number = item.get("line") if isinstance(item, dict) else item
        claim = str(item.get("claim") or "") if isinstance(item, dict) else ""
        try:
            marked[int(number) - 1] = claim or "not stated in the article"
        except (TypeError, ValueError):
            continue
    return marked


# ----------------------------------------------------------------- writing --

def _prompt(channel: Channel, article: Article, scenes: int, long_form: bool, feedback: str) -> str:
    tone = getattr(channel, "tone", "") or "clear, respectful and engaging"
    if long_form:
        # A long script is asked for as single facts, one sentence each, and
        # joined into scenes afterwards. Asked for 40-word scenes the writer
        # pads every one with commentary ("a turning point in her journey"),
        # and one padded clause then costs the whole scene.
        shape = (f"Write a long-form script of about 5 minutes as {scenes * 2 + 10} entries in 'scenes'. "
                 "Each 'line' is ONE plain sentence of 16-24 words reporting one thing the SOURCE says "
                 "happened. No commentary on what it meant, proved or showed, and nothing about feelings "
                 "or character unless the SOURCE states it. Follow the story in order and do "
                 "not repeat a fact.")
    else:
        # Viewers left at 0:02 when scene 1 was "X was born in rural Tennessee".
        shape = (f"Write a short video script: exactly {scenes} scenes, each 'line' one sentence of 14-26 words. "
                 "Scene 1 is the HOOK, heard in the first two seconds: the single most astonishing "
                 "fact in the SOURCE, under 16 words, stated plainly (the deed, the odds, the number). "
                 "Then tell the story in order from scene 2.")
    speech = '"speech": str, ' if is_urdu(channel) else ""
    return (
        f"You write true stories for the video channel '{channel.name}'. Tone: {tone}.\n"
        f"{shape}\n"
        "THE ONE RULE: use ONLY facts written in the SOURCE below. Do not add any name, date, "
        "number, place, event, quote, thought, feeling or outcome that the SOURCE does not state. "
        "Do not invent scenes or dialogue. If the SOURCE does not say it, leave it out.\n"
        "- Write every number as digits exactly as in the SOURCE (1957, 20,000).\n"
        "- Say the subject's full name (the person, or the case) in the first or second scene.\n"
        "- 'title' must contain that name and no claim that is not in the SOURCE.\n"
        "- No graphic violence or gore; describe crimes plainly, as a documentary would.\n"
        "- 'label' = a 2-4 word caption without numbers.\n"
        "- 'image_prompt' = a plain real-world scene that fits the line (a place, an object, people "
        "at work). Never the person's face, never text. No art-style words.\n"
        f'Return ONLY JSON: {{"title": str, "scenes": [{{"label": str, "line": str, {speech}"image_prompt": str}}]}}'
        + script_rules(channel)
        + (f"\nA previous draft was rejected: {feedback}\n" if feedback else "")
        + f"\n\nSOURCE (Wikipedia, '{article.title}'):\n{article.text}"
    )


def _checked_rows(channel: Channel, article: Article, raw: dict) -> tuple[list[dict], list[str]]:
    """The draft's scenes that pass every check, and why the others were dropped."""
    rows = [dict(row) for row in raw.get("scenes") or [] if str(row.get("line") or "").strip()]
    check_names = not is_urdu(channel)  # Roman Urdu capitalises freely
    kept: list[dict] = []
    dropped: list[str] = []
    for row in rows:
        row["line"] = " ".join(str(row["line"]).split())
        reason = unsupported_reason(row["line"], article, check_names)
        # The Urdu voice reads its own text, so that must hold the same numbers.
        if not reason and row.get("speech") and numbers_in(row["speech"]) - numbers_in(article.text):
            reason = "the Urdu voice text has a number that is not in the article"
        if reason:
            dropped.append(f"\"{row['line'][:90]}\" — {reason}")
        else:
            kept.append(row)
    if kept:
        marked = _audit([row["line"] for row in kept], article)
        # The checker sometimes quotes words that do stand in the article.
        source = _plain(article.text)
        marked = {index: claim for index, claim in marked.items()
                  if not (len(claim.split()) >= 3 and _plain(claim) in source)}
        dropped.extend(f"\"{row['line'][:90]}\" — not in the article: {marked[index][:120]}"
                       for index, row in enumerate(kept) if index in marked)
        kept = [row for index, row in enumerate(kept) if index not in marked]
    return kept, dropped


def _plain(text: str) -> str:
    """Lower-case words only, for comparing a quote with the article."""
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower()))


def _joined(rows: list[dict], scenes: int) -> list[dict]:
    """Checked single sentences, joined in order into about `scenes` scenes."""
    size = max(1, round(len(rows) / max(1, scenes)))
    joined: list[dict] = []
    for start in range(0, len(rows), size):
        group = rows[start:start + size]
        row = dict(group[0])
        row["line"] = " ".join(item["line"] for item in group)
        if any(item.get("speech") for item in group):
            row["speech"] = " ".join(str(item.get("speech") or item["line"]) for item in group)
        joined.append(row)
    return joined


def _enough(rows: list[dict], long_form: bool) -> bool:
    if long_form:
        return sum(len(row["line"].split()) for row in rows) >= _LONG_MIN_WORDS
    return len(rows) >= _SHORT_MIN_SCENES


def write_lesson(channel: Channel, article: Article, scenes: int = 8, long_form: bool = False) -> tuple[str, dict]:
    """A checked script about `article`'s subject, as (key, lesson) for the topic bank."""
    from .genre_topics import _script_json, _slug, _speech_of

    feedback = ""
    for _attempt in range(2):
        raw = _script_json(_prompt(channel, article, scenes, long_form, feedback))
        rows, dropped = _checked_rows(channel, article, raw)
        for note in dropped:
            print(f"[verified_facts] {channel.id}: dropped {note}")
        if _enough(rows, long_form):
            break
        feedback = (
            f"only {len(rows)} of its lines were supported by the SOURCE. Unsupported: "
            + " | ".join(dropped[:8]) + ". Stay closer to the SOURCE and keep the full length."
        )
    else:
        raise RuntimeError(
            f"Could not write a script about {article.title} using only facts from its article; "
            "no video is better than an invented one."
        )
    if long_form:
        rows = _joined(rows, scenes)

    surname = article.title.split("(")[0].split()[-1].lower()
    title = " ".join(str(raw.get("title") or "").split())
    if surname not in title.lower() or unsupported_reason(title, article, check_names=False):
        title = f"{article.title}: The True Story"
    key = _slug(title)
    lesson_scenes = []
    for index, row in enumerate(rows, start=1):
        label = " ".join(str(row.get("label") or "").split())
        if not label or numbers_in(label) - numbers_in(article.text):
            label = f"Part {index}"
        lesson_scenes.append({
            "label": label,
            "line": row["line"],
            "speech": _speech_of(channel, row),
            "image": f"assets/generated/{key}_{index:02}.jpg",
            "image_prompt": f"{row.get('image_prompt') or article.title}, {channel.image_style}",
        })
    return key, {
        "title": title,
        "category": channel.genre.title(),
        # Left empty on purpose: an opening or closing line would be one more
        # sentence nobody checked.
        "intro": "",
        "outro": "",
        "scenes": lesson_scenes,
        "verified": True,
        "source_title": article.title,
        "source_url": article.url,
    }


def article_for_prompt(channel: Channel, user_prompt: str, long_form: bool) -> Article:
    """The subject for a custom-prompt video.

    The daily run's own instruction ("Create a complete, original ... video
    about ...") names no real subject, so it gets the next unused person. Words
    a person typed are looked up on Wikipedia.
    """
    from .genre_topics import _looks_like_instruction_title

    text = " ".join(str(user_prompt or "").split())
    if not text or _looks_like_instruction_title(text):
        return next_article(channel, long_form)
    return search_article(text, long_form)


def write_next_lesson(channel: Channel, scenes: int, long_form: bool,
                      user_prompt: str = "") -> tuple[str, dict]:
    """Pick a subject and write its script; try another person if one fails."""
    from .genre_topics import _looks_like_instruction_title

    text = " ".join(str(user_prompt or "").split())
    # Words a person typed name the subject they want; that one is not swapped.
    asked_for = bool(text) and not _looks_like_instruction_title(text)
    error = "No verified script could be written"
    for _attempt in range(1 if asked_for else 3):
        article = article_for_prompt(channel, text, long_form)
        try:
            return write_lesson(channel, article, scenes, long_form)
        except Exception as exc:
            error = str(exc)
            print(f"[verified_facts] {channel.id}: {exc}")
            _FAILED.setdefault((channel.id, long_form), set()).add(article.title.lower())
    raise RuntimeError(error)


def source_url(channel, topic_key: str) -> str:
    """The article a saved script was written from ("" for ordinary scripts)."""
    try:
        return str(_bank_rows(channel).get(topic_key, {}).get("source_url") or "")
    except Exception:
        return ""


def source_url_for(channel_id: str, topic_key: str) -> str:
    """`source_url` by channel id, for callers that hold only a database row."""
    try:
        from .channels import get_channel

        return source_url(get_channel(channel_id), topic_key)
    except Exception:
        return ""


def only_verified(channel, lessons: dict) -> dict:
    """On a fact-only channel, hide scripts that were not written from a source."""
    if not is_verified_channel(channel):
        return lessons
    return {key: raw for key, raw in lessons.items() if raw.get("verified")}
