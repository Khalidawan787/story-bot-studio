"""What YouTube's own data says works, for titles, tags and the next topics.

Two free signals, the owner's request (2026-11-03), in place of a paid tool
such as vidIQ:

* Search suggestions: the phrases YouTube's search box offers as people type
  (the same public endpoint the box uses). They become tags, and story
  channels see them when the title is written.
* Views: `view_stats.refresh_views` fills the `views` column before each
  daily run, and the channel's most-watched titles guide the next story
  topics. Watch time would need the yt-analytics scope, so every channel would
  have to log in again; views need nothing new.

Every call is best-effort: no network, no data, or YOUTUBE_INSIGHTS=off gives
empty results and the video is made exactly as before.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import settings

_SUGGEST_URL = "https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q="
_CACHE: dict[str, list[str]] = {}
# A word that ties a suggestion to the kind of video, so "jesse james west"
# (a different man) is not mistaken for the outlaw's story.
_CONTEXT = {
    "crime": ("true story", "documentary", "story"),
    "heroic history": ("biography", "story", "documentary"),
    "trending": ("news", "explained"),
    "motivation": ("motivation", "motivational"),
    "bedtime stories": ("bedtime story", "sleep story"),
    "kids": ("for kids", "song"),
}


_FILLER = frozenset({
    "the", "and", "for", "with", "from", "life", "legend", "story", "true", "tale", "how", "why",
    "what", "who", "his", "her", "their", "this", "that", "into", "over", "about", "calming",
    "bedtime", "stories", "video", "explained", "update", "full", "rise", "fall", "chronicles",
    "echoes", "greatness", "journey", "service", "valor",
})


_OFF_TOPIC = frozenset({
    "movie", "movies", "film", "trailer", "song", "songs", "soundtrack", "lyrics", "cast", "actor",
    "game", "reaction", "hindi", "urdu", "tamil", "telugu", "motorcycle", "netflix", "episode",
})


def _off() -> bool:
    return os.getenv("YOUTUBE_INSIGHTS", "on").strip().lower() in {"off", "0", "false"}


def suggestions(query: str) -> list[str]:
    """What YouTube's search box offers for `query`; [] on any problem."""
    query = " ".join(str(query or "").lower().split())
    if not query or _off():
        return []
    if query in _CACHE:
        return _CACHE[query]
    try:
        request = urllib.request.Request(_SUGGEST_URL + urllib.parse.quote(query),
                                         headers={"User-Agent": "Mozilla/5.0 StoryBotStudio"})
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        found = [str(item).strip() for item in (data[1] if len(data) > 1 else []) if str(item).strip()]
    except Exception:
        found = []
    _CACHE[query] = found
    return found


def _genre(channel) -> str:
    return "kids" if getattr(channel, "builtin", False) else str(getattr(channel, "genre", "") or "").lower()


def search_tags(channel, subject: str, limit: int = 5) -> list[str]:
    """Real search phrases about this video's subject, for its tags."""
    subject = re.sub(r"[^\w' ]", " ", re.split(r"\s*[|:]\s*", str(subject or ""))[0]).strip().lower()
    words = [w for w in subject.split() if len(w) > 2 and w not in _FILLER]
    found: list[str] = []
    # The whole subject first, then its last two words ("jesse james" out of
    # "life legend jesse james"), which are usually the name.
    for core_words in (words[:4], words[-2:]):
        if not core_words:
            continue
        core = " ".join(core_words)
        for hint in _CONTEXT.get(_genre(channel), ("story",)):
            for phrase in suggestions(f"{core} {hint}"):
                text = phrase.lower()
                # Only phrases that keep the whole subject and the kind of video,
                # and are not about a film, song or other thing of the same name.
                if all(w in text for w in core_words) and phrase not in found                         and not set(text.split()) & _OFF_TOPIC:
                    found.append(phrase)
        if found:
            break
    return found[:limit]


def title_phrases(channel, limit: int = 8) -> list[str]:
    """What people search for in this channel's field, to word a story title."""
    if getattr(channel, "builtin", False):
        return []  # the kids channel's titles follow their own fixed pattern
    seeds = list(getattr(channel, "keywords", ()) or ())[:2]
    seeds += [hint for hint in _CONTEXT.get(_genre(channel), ())[:1]]
    found: list[str] = []
    for seed in seeds:
        for phrase in suggestions(seed):
            if phrase not in found:
                found.append(phrase)
    return found[:limit]


def best_titles(channel, limit: int = 5, days: int = 90) -> list[str]:
    """This channel's most-viewed titles of the last `days` days (views > 0)."""
    if _off() or not settings.db_path.exists():
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT title, views FROM videos WHERE channel = ? AND COALESCE(views, 0) > 0 "
            "AND created_at >= ? ORDER BY views DESC LIMIT ?",
            (channel.id, since, limit * 3),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    titles: list[str] = []
    for title, _views in rows:
        head = re.split(r"\s*\|\s*", str(title or ""))[0].strip()
        if head and head not in titles:
            titles.append(head)
    return titles[:limit]


def prompt_note(channel) -> str:
    """Extra lines for a free-written story's prompt; "" when there is nothing to add."""
    lines = []
    best = best_titles(channel)
    if best:
        lines.append("These videos got the most views on this channel; write a NEW story with the "
                     "same kind of appeal, never the same story: " + "; ".join(best) + ".")
    phrases = title_phrases(channel)
    if phrases:
        lines.append("People search YouTube for: " + "; ".join(phrases) + ". If one fits this story "
                     "naturally, use its words in the title; never force it.")
    return ("\n" + "\n".join(lines)) if lines else ""
