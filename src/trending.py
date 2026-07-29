"""World trending topics -> daily video scripts (no API key needed).

The other genre channels invent their own topics. This one reads what the
world is actually searching for and talking about RIGHT NOW (Google Trends,
Google News world headlines, Reddit r/worldnews), ranks it, drops anything
already covered, and turns the winners into scripts in the SAME lesson shape
the render pipeline already understands.

Because the trend list changes every day, the channel automatically publishes
a different subject every day without anyone typing a topic.

All sources are free and keyless; every one of them is optional — if a source
is down the rest still produce a topic, and if ALL of them fail the caller
falls back to the normal genre generator.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .channels import Channel
from .config import settings


HISTORY_FILE = settings.root / "data" / "trending_history.json"
HISTORY_DAYS = 45
USER_AGENT = "StoryBotStudio/1.0 (+trending)"

# Trend text that we never build a video around: adult content, explicit gore,
# and pure product/piracy spam. War and politics ARE allowed (that is the point
# of the channel) — the script prompt keeps them factual and neutral instead.
BLOCKED_PATTERNS = (
    "porn", "nude", "nsfw", "onlyfans", "sex tape", "xxx",
    "beheading", "execution video", "gore", "suicide method",
    "torrent", "free download", "crack version", "betting tips",
)

# Small words that make a headline unique-looking but mean nothing when we are
# checking "did we already cover this story?".
_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "for", "to", "and", "or", "is", "are",
    "was", "were", "be", "as", "at", "by", "with", "from", "after", "over",
    "new", "latest", "breaking", "news", "update", "updates", "today", "live",
    "says", "said", "vs", "his", "her", "their", "its", "this", "that",
}


@dataclass
class Trend:
    """One ranked world topic, with the evidence that made it rank."""

    title: str
    score: float = 0.0
    sources: set[str] = field(default_factory=set)
    traffic: int = 0
    headlines: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return _topic_signature(self.title)

    def context(self, limit: int = 6) -> str:
        rows = [h for h in dict.fromkeys(self.headlines) if h][:limit]
        return "\n".join(f"- {row}" for row in rows)


# ---------------------------------------------------------------- utilities

def _get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return " ".join(text.split()).strip()


def _tag(element: ET.Element) -> str:
    """Local tag name without the XML namespace ({...}title -> title)."""
    return element.tag.rsplit("}", 1)[-1].lower()


def _topic_signature(title: str) -> str:
    """Normalized fingerprint used to detect 'same story, different headline'."""
    words = re.findall(r"[a-z0-9]+", str(title or "").lower())
    keep = [w for w in words if w not in _STOPWORDS and len(w) > 2]
    return " ".join(sorted(set(keep))[:6])


def _is_blocked(title: str) -> bool:
    lowered = str(title or "").lower()
    return any(pattern in lowered for pattern in BLOCKED_PATTERNS)


def _overlap(left: str, right: str) -> float:
    a, b = set(left.split()), set(right.split())
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# ------------------------------------------------------------------ sources

def _google_trends(geo: str) -> list[Trend]:
    """Real-time search trends for one country (keyless RSS feed)."""
    url = f"https://trends.google.com/trending/rss?geo={urllib.parse.quote(geo)}"
    root = ET.fromstring(_get(url))
    trends: list[Trend] = []
    for position, item in enumerate(root.iter()):
        if _tag(item) != "item":
            continue
        title = ""
        traffic = 0
        headlines: list[str] = []
        for child in item.iter():
            name = _tag(child)
            value = _clean(child.text or "")
            if name == "title" and not title:
                title = value
            elif name == "approx_traffic":
                traffic = int(re.sub(r"[^0-9]", "", value) or 0)
            elif name in {"news_item_title", "news_item_snippet"} and value:
                headlines.append(value)
        if not title or _is_blocked(title):
            continue
        trends.append(Trend(
            title=title,
            # Search volume is the strongest "the world cares right now" signal.
            score=6.0 + min(6.0, traffic / 50_000.0) + max(0.0, 3.0 - position * 0.15),
            sources={f"google-trends:{geo}"},
            traffic=traffic,
            headlines=headlines,
        ))
    return trends


def _rss_headlines(url: str, source: str, weight: float) -> list[Trend]:
    root = ET.fromstring(_get(url))
    trends: list[Trend] = []
    position = 0
    for item in root.iter():
        if _tag(item) != "item":
            continue
        title = ""
        for child in item:
            if _tag(child) == "title":
                title = _clean(child.text or "")
                break
        # Google News appends " - Publisher" to every headline.
        title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()
        if not title or _is_blocked(title):
            continue
        position += 1
        trends.append(Trend(
            title=title,
            score=weight + max(0.0, 2.5 - position * 0.1),
            sources={source},
            headlines=[title],
        ))
    return trends


def _reddit_worldnews(subreddit: str = "worldnews", limit: int = 25) -> list[Trend]:
    url = f"https://www.reddit.com/r/{subreddit}/top.json?t=day&limit={limit}"
    payload = json.loads(_get(url))
    trends: list[Trend] = []
    for position, child in enumerate(payload.get("data", {}).get("children", [])):
        data = child.get("data", {})
        title = _clean(data.get("title", ""))
        if not title or _is_blocked(title):
            continue
        upvotes = int(data.get("score", 0) or 0)
        trends.append(Trend(
            title=title,
            score=3.0 + min(4.0, upvotes / 6_000.0) + max(0.0, 2.0 - position * 0.08),
            sources={f"reddit:{subreddit}"},
            headlines=[title],
        ))
    return trends


def _source_plan() -> list[tuple[str, callable]]:
    geos = [g.strip().upper() for g in settings.trending_geos.split(",") if g.strip()]
    plan: list[tuple[str, callable]] = []
    for geo in geos:
        plan.append((f"google-trends:{geo}", lambda g=geo: _google_trends(g)))
    plan.append((
        "google-news:world",
        lambda: _rss_headlines(
            "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en",
            "google-news:world", weight=5.0,
        ),
    ))
    plan.append((
        "google-news:top",
        lambda: _rss_headlines(
            "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
            "google-news:top", weight=4.0,
        ),
    ))
    plan.append((
        "bbc:world",
        lambda: _rss_headlines(
            "https://feeds.bbci.co.uk/news/world/rss.xml", "bbc:world", weight=4.5,
        ),
    ))
    plan.append((
        "aljazeera:all",
        lambda: _rss_headlines(
            "https://www.aljazeera.com/xml/rss/all.xml", "aljazeera:all", weight=4.0,
        ),
    ))
    # Reddit blocks many datacenter/VPN IPs with a 403; it is a bonus signal, not
    # a requirement, so it goes last and its failure is ignored like any other.
    plan.append(("reddit:worldnews", lambda: _reddit_worldnews("worldnews")))
    return plan


# ------------------------------------------------------------------ ranking

def fetch_world_trends(limit: int = 25) -> list[Trend]:
    """Collect and rank what the world is searching for / reading right now.

    A topic that shows up in several independent sources is genuinely trending,
    so cross-source agreement is the biggest part of the score.
    """
    merged: dict[str, Trend] = {}
    failures: list[str] = []

    for name, fetch in _source_plan():
        try:
            found = fetch()
        except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError,
                json.JSONDecodeError, TimeoutError, OSError, ValueError) as exc:
            failures.append(f"{name}: {exc}")
            continue
        for trend in found:
            signature = trend.key
            if not signature:
                continue
            existing = merged.get(signature)
            if existing is None:
                # Also merge near-identical wordings of the same story.
                for other_key, other in merged.items():
                    if _overlap(signature, other_key) >= 0.7:
                        existing = other
                        break
            if existing is None:
                merged[signature] = trend
                continue
            existing.score += trend.score
            existing.sources |= trend.sources
            existing.traffic = max(existing.traffic, trend.traffic)
            existing.headlines.extend(trend.headlines)
            if trend.traffic > 0 and len(trend.title) < len(existing.title):
                existing.title = trend.title

    if failures:
        print(f"[trending] {len(failures)} source(s) unavailable: {failures[0]}")

    ranked = sorted(merged.values(), key=lambda t: (-(t.score + 4.0 * (len(t.sources) - 1)), t.title))
    return ranked[:limit]


# ------------------------------------------------------------------ history

def _load_history() -> dict[str, str]:
    if not HISTORY_FILE.exists():
        return {}
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = (datetime.now() - timedelta(days=HISTORY_DAYS)).date()
    fresh: dict[str, str] = {}
    for signature, stamp in data.items():
        try:
            if datetime.fromisoformat(stamp).date() >= cutoff:
                fresh[signature] = stamp
        except (TypeError, ValueError):
            continue
    return fresh


def _remember(signatures: list[str]) -> None:
    history = _load_history()
    now = datetime.now().isoformat(timespec="seconds")
    for signature in signatures:
        history[signature] = now
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def pick_fresh_trends(channel: Channel, count: int) -> list[Trend]:
    """Top trends that this channel has not already made a video about."""
    from .lessons import load_topics_for

    history = _load_history()
    covered = list(history)
    for raw in load_topics_for(channel).values():
        signature = _topic_signature(raw.get("title", ""))
        if signature:
            covered.append(signature)

    picked: list[Trend] = []
    for trend in fetch_world_trends(limit=40):
        signature = trend.key
        if not trend.headlines:
            # A bare search term with no reporting behind it gives the script
            # writer nothing factual to stand on, so it would invent the story.
            continue
        if any(_overlap(signature, seen) >= 0.6 for seen in covered):
            continue
        picked.append(trend)
        covered.append(signature)
        if len(picked) >= count:
            break
    return picked


# --------------------------------------------------------------- generation

def _news_prompt(channel: Channel, trend: Trend, scenes: int, long_form: bool) -> str:
    """Grounded prompt: the model explains the REAL headlines, not invented ones."""
    context = trend.context() or f"- {trend.title}"
    today = datetime.now().strftime("%B %d, %Y")
    if long_form:
        shape = (
            f"Write a YouTube long-form explainer targeting about 5 minutes of narration. "
            f"Use exactly {scenes} scenes, each scene line 38-43 words "
            f"(about 760-860 narrated words total)."
        )
    else:
        shape = (
            f"Write a fast, punchy YouTube Shorts news explainer. Use exactly {scenes} "
            "scenes. Each scene line is one narrated sentence. Open with the single "
            "most surprising fact so nobody scrolls away."
        )
    return (
        f"You are the writer for a world-news explainer channel. Today is {today}.\n"
        f"This subject is trending worldwide right now:\n\"{trend.title}\"\n\n"
        f"Real headlines about it:\n{context}\n\n"
        f"{shape}\n"
        "RULES — follow all of them:\n"
        "1. Stay strictly factual and neutral. Explain WHAT is happening, the "
        "BACKGROUND that led here, and WHY it matters to ordinary people.\n"
        "2. Use ONLY what the headlines above support. Never invent quotes, "
        "casualty numbers, statistics, dates, or named accusations.\n"
        "3. Where facts are still unconfirmed, say so plainly "
        "(\"reports suggest\", \"officials have not confirmed\").\n"
        "4. Take no political side, endorse no group, and do not describe "
        "graphic violence. Keep it advertiser-friendly.\n"
        "5. The title must be a clear, non-clickbait description of the story.\n"
        "Return ONLY JSON: {\"title\": str, \"intro\": str, \"outro\": str, "
        "\"scenes\": [{\"label\": str, \"line\": str, \"image_prompt\": str}]}.\n"
        "'label' = a 2-4 word on-screen caption. 'image_prompt' = a neutral, "
        "photo-real news visual for that scene (setting and objects only — no "
        "real people's faces, no text, no logos, no flags being burned)."
    )


def generate_trending_topics(channel: Channel, count: int = 2, scenes: int = 8) -> list[str]:
    """Turn today's top world trends into scripts. Returns the new topic keys."""
    from .genre_topics import _build_lesson, _script_json, load_genre_lessons

    trends = pick_fresh_trends(channel, count)
    if not trends:
        print("[trending] no fresh world trends available right now")
        return []

    lessons = load_genre_lessons(channel)
    added: list[str] = []
    used_signatures: list[str] = []

    for trend in trends:
        try:
            raw = _script_json(_news_prompt(channel, trend, scenes, long_form=False))
        except Exception as exc:
            print(f"[trending] {channel.id}: script failed for '{trend.title[:60]}' ({exc})")
            continue
        key, lesson = _build_lesson(channel, raw, fallback_title=trend.title)
        lesson["category"] = "Trending"
        key = f"trend_{key}"
        if key in lessons:
            key = f"{key}_{len(lessons) + 1}"
        lessons[key] = lesson
        added.append(key)
        used_signatures.append(trend.key)
        sources = ", ".join(sorted(trend.sources))
        print(f"[trending] picked '{trend.title[:70]}' (sources: {sources})")

    if added:
        path = channel.lessons_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(lessons, indent=2, ensure_ascii=False), encoding="utf-8")
        _remember(used_signatures)
    return added


def trending_long_idea(channel: Channel) -> str | None:
    """Today's top uncovered world trend, phrased as a long-form video brief."""
    trends = pick_fresh_trends(channel, 1)
    if not trends:
        return None
    trend = trends[0]
    _remember([trend.key])
    context = trend.context(limit=4)
    return (
        f"Create a factual, neutral world-news explainer about this trending "
        f"subject: {trend.title}. Real headlines:\n{context}\n"
        "Cover what happened, the background behind it, and why it matters. "
        "Do not invent quotes, numbers, or accusations, and take no political side."
    )


def is_trending_channel(channel: Channel) -> bool:
    return str(getattr(channel, "genre", "")).lower() == "trending"
