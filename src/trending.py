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
    # The best single source's score. `score` adds up every source that carried
    # the topic, so a sports fixture listed by three countries' search lists
    # out-scored a story two newsrooms ran. Ranking uses `peak` plus bonuses.
    peak: float = 0.0

    def __post_init__(self) -> None:
        if not self.peak:
            self.peak = self.score

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


# A channel with "news_regions" in channels.json reads these sources instead of
# the world mix: an American audience wants American news first, then Europe.
_REGION_FEEDS = {
    "US": {
        "geos": ("US",),
        "feeds": (
            ("google-news:us-top", "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en", 5.5),
            ("google-news:us-nation",
             "https://news.google.com/rss/headlines/section/topic/NATION?hl=en-US&gl=US&ceid=US:en", 5.0),
            ("npr:news", "https://feeds.npr.org/1001/rss.xml", 4.5),
            ("bbc:us-canada", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml", 4.0),
        ),
        "reddit": ("news",),
    },
    "EU": {
        # Britain only: three European search lists together outranked the
        # American news with tennis players and holiday spots.
        "geos": ("GB",),
        "feeds": (
            ("bbc:europe", "https://feeds.bbci.co.uk/news/world/europe/rss.xml", 4.0),
            ("google-news:uk", "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en", 3.5),
        ),
        "reddit": ("europe",),
    },
}


def _region_plan(regions: list[str]) -> list[tuple[str, callable]]:
    plan: list[tuple[str, callable]] = []
    for region in regions:
        spec = _REGION_FEEDS.get(region.upper())
        if not spec:
            continue
        for geo in spec["geos"]:
            plan.append((f"google-trends:{geo}", lambda g=geo: _google_trends(g)))
        for name, url, weight in spec["feeds"]:
            plan.append((name, lambda n=name, u=url, w=weight: _rss_headlines(u, n, weight=w)))
    # Reddit last, as in the world mix: often blocked from datacenter addresses.
    for region in regions:
        for subreddit in (_REGION_FEEDS.get(region.upper()) or {}).get("reddit", ()):
            plan.append((f"reddit:{subreddit}", lambda s=subreddit: _reddit_worldnews(s)))
    return plan


def _source_plan(channel: Channel | None = None) -> list[tuple[str, callable]]:
    regions = [str(r) for r in (getattr(channel, "news_regions", ()) or ())]
    if regions:
        return _region_plan(regions)
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

# Sports ARE covered by this channel (the owner wants them), so they are not in
# the not-news list below. They are recognised only so that each day's picks can
# hold a sports story (see SPORTS_SLOTS).
_SPORTS = re.compile(
    r"\b(football|soccer|cricket|tennis|rugby|basketball|baseball|nfl|nba|mlb|nhl|ipl|psl|"
    r"premier league|la liga|serie a|bundesliga|ligue 1|champions league|europa league|"
    r"world cup|golf|formula 1|formula one|grand prix|wimbledon|atp|wta|ufc|wwe|boxing|"
    r"live score|live stream(?:ing)?|fixtures?|line-?ups?|playoffs?|transfer window|"
    r"group stage|innings|wickets?|touchdown|jockey|horse racing|hong kong sixes|"
    r"super league|isl|olympics?|medal|marathon|cycling|athletics)\b", re.I)
# Tournaments and fixtures the whole world follows, tried first for the sports place.
_MAJOR_EVENT = re.compile(
    r"\b(world cup|olympics?|champions league|europa league|grand prix|formula 1|wimbledon|"
    r"us open|french open|australian open|super bowl|nba finals|test match|the ashes|"
    r"asia cup|t20|ipl|psl|premier league|la liga|serie a|bundesliga|final|semi-?final)\b", re.I)
# Of a day's picks, this many are sports stories when any is available and the
# day makes two or more videos. Set it to 0 to rank sports like any other topic.
SPORTS_SLOTS = 1

# Search lists are also full of game titles, film promotion and price tickers;
# none of that is news. A topic whose words say it is one of these is dropped
# from the picks and sunk in the ranking.
_NOT_NEWS = {
    "entertainment": re.compile(
        r"\b(box office|trailer|teaser|season finale|release date|netflix|album|grammys?|oscars?|"
        r"emmys?|bafta|celebrity|red carpet|reality tv|bigg boss|love island|call of duty|"
        r"fortnite|gta|playstation|xbox|nintendo|video game|k-pop|taylor swift|voice actor)\b", re.I),
    "markets": re.compile(
        r"\b(price today|rate today|gold rate|gold price|silver price|share price|stock price|"
        r"etf|etfs|crypto price|bitcoin price|forex|nifty|sensex|horoscope|lottery|powerball|"
        r"mega millions)\b", re.I),
}
# "a vs b" as a bare search term is a match fixture.
_FIXTURE = re.compile(r"^[^:]{2,60}\s+vs\.?\s+[^:]{2,60}$", re.I)
# Searches below this are too small to show the world cares; with no news outlet
# behind them they are rated down (real world-news searches are far above it).
_BIG_SEARCH = 50_000


def _mostly(pattern: re.Pattern, trend: Trend) -> bool:
    """The pattern matches at least half of the topic's title and headlines."""
    snippets = [trend.title, *list(dict.fromkeys(trend.headlines))[:4]]
    hits = sum(1 for text in snippets if pattern.search(text))
    return bool(hits) and hits * 2 >= len(snippets)


def is_sports(trend: Trend) -> bool:
    """True when the topic is a match, a tournament, a transfer or other sport."""
    feeds = [s for s in trend.sources if not s.startswith("google-trends")]
    if _mostly(_SPORTS, trend):
        return True
    return not feeds and bool(_FIXTURE.match(trend.title))


def trend_kind(trend: Trend) -> str | None:
    """'entertainment' or 'markets' when the topic is not news, else None.

    Sports is not in this list: the channel covers it.
    """
    for kind, pattern in _NOT_NEWS.items():
        if _mostly(pattern, trend):
            return kind
    return None


def _story_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(title or "").lower())
            if w not in _STOPWORDS and len(w) > 2}


def _same_story(left: str, right: str) -> bool:
    """Two headlines about one story: at least three telling words in common and
    half of the shorter headline's words. The old fingerprint kept only the first
    six words in alphabetical order, so two reports of one crash rarely matched."""
    a, b = _story_words(left), _story_words(right)
    shared = len(a & b)
    return shared >= 3 and shared / min(len(a), len(b)) >= 0.5


def rank_value(trend: Trend) -> float:
    """How strongly the world is covering this topic, for a news channel."""
    feeds = {s for s in trend.sources if not s.startswith("google-trends")}
    geos = {s for s in trend.sources if s.startswith("google-trends")}
    value = trend.peak
    value += 4.0 * max(0, len(feeds) - 1)      # several newsrooms ran it
    if feeds and geos:
        value += 4.0                            # and people are searching for it too
    value += 1.0 * max(0, len(geos) - 1)       # several countries: little weight, leagues do it too
    if not feeds and trend.traffic < _BIG_SEARCH and not is_sports(trend):
        value -= 8.0                            # a small search term with no reporting behind it
    if trend_kind(trend):
        value -= 30.0
    return value


def fetch_world_trends(limit: int = 25, channel: Channel | None = None) -> list[Trend]:
    """Collect and rank what the world is searching for / reading right now.

    A topic that shows up in several independent sources is genuinely trending,
    so cross-source agreement is the biggest part of the score.
    """
    merged: dict[str, Trend] = {}
    failures: list[str] = []

    for name, fetch in _source_plan(channel):
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
                    if _overlap(signature, other_key) >= 0.7 or _same_story(trend.title, other.title):
                        existing = other
                        break
            if existing is None:
                merged[signature] = trend
                continue
            existing.score += trend.score
            existing.peak = max(existing.peak, trend.peak)
            existing.sources |= trend.sources
            existing.traffic = max(existing.traffic, trend.traffic)
            existing.headlines.extend(trend.headlines)
            if trend.traffic > 0 and len(trend.title) < len(existing.title):
                existing.title = trend.title

    if failures:
        print(f"[trending] {len(failures)} source(s) unavailable: {failures[0]}")

    ranked = sorted(merged.values(), key=lambda t: (-rank_value(t), t.title))
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


def _already_covered(signature: str, seen: str) -> bool:
    """True when `signature` is the same story as one already made.

    A signature of one or two words ("israel", "stock") is a bare search term.
    Measured as a share of itself it matches every story that mentions the word,
    so one search term used to block a whole country or market for the 45 days
    the history is kept. A signature that short only counts as covered when it
    is the very same one.
    """
    if signature == seen:
        return True
    if min(len(signature.split()), len(seen.split())) < 3:
        return False
    return _overlap(signature, seen) >= 0.6


def pick_fresh_trends(channel: Channel, count: int) -> list[Trend]:
    """Top trends that this channel has not already made a video about."""
    from .lessons import load_topics_for

    history = _load_history()
    covered = list(history)
    for raw in load_topics_for(channel).values():
        signature = _topic_signature(raw.get("title", ""))
        if signature:
            covered.append(signature)

    eligible: list[Trend] = []
    for trend in fetch_world_trends(limit=60, channel=channel):
        if len(re.findall(r"[^\W\d_]", trend.title)) < 5 or not re.search(r"[^\W\d_]{3,}", trend.title):
            # A stub search term such as "vz s": nothing a viewer would recognise.
            continue
        if not trend.headlines:
            # A bare search term with no reporting behind it gives the script
            # writer nothing factual to stand on, so it would invent the story.
            continue
        if trend_kind(trend):
            # Game titles, film promotion, price tickers: not news.
            continue
        if any(_already_covered(trend.key, seen) for seen in covered):
            continue
        eligible.append(trend)

    picked: list[Trend] = []
    for trend in eligible:
        if any(_already_covered(trend.key, other.key) for other in picked):
            continue
        picked.append(trend)
        if len(picked) >= count:
            break

    # Sport is part of this channel, but a match scores below a story that
    # several newsrooms ran. Without a reserved place a day of two videos would
    # almost never hold one, so the lowest pick makes way for the best sports story.
    wanted = SPORTS_SLOTS if count >= 2 else 0
    have = sum(1 for trend in picked if is_sports(trend))
    # Among sports stories a big tournament comes first, then the most searched,
    # then the one with most reporting. The news score does not rank sport well:
    # it put a 100-search league match above a World Cup game.
    sports_first = sorted(
        (trend for trend in eligible if is_sports(trend)),
        key=lambda trend: (
            not _MAJOR_EVENT.search(" ".join([trend.title, *trend.headlines[:3]])),
            -trend.traffic, -len(trend.headlines),
        ),
    )
    for trend in sports_first:
        if have >= wanted or len(picked) < count:
            break
        if trend in picked:
            continue
        if any(_already_covered(trend.key, other.key) for other in picked[:-1]):
            continue
        picked[-1] = trend
        have += 1
    return picked


# --------------------------------------------------------------- generation

# A news channel for an American audience (news_regions) wants titles that
# state the story: "Spain Reels From Rail Tragedy Amidst Political Division"
# padded it with a headline verb, and "Reels" reads like Facebook Reels.
PLAIN_TITLE_RULE = (
    "Title: state the news plainly in its key words, like 'Spain Rail Tragedy Sparks Political "
    "Divide'. No filler verbs (reels, grapples, braces, amidst) and never the words Reels, "
    "Shorts or Video."
)
# Only "Reels"/"Reeling" is cut afterwards: dropping "Braces For" from "Market
# Braces For Rate Cut" would lose the meaning, so the prompt handles the rest.
_FILLER_TITLE = re.compile(r"\b(?:reels|reeling)\s+(?:from|over)\s+", re.I)


def plain_title(channel: Channel, title: str) -> str:
    """Drop headline filler from a news_regions channel's title; others unchanged."""
    if not getattr(channel, "news_regions", ()):
        return title
    title = _FILLER_TITLE.sub("", title)
    title = re.sub(r"\bamidst\b", "Amid", title, flags=re.I)
    title = re.sub(r"\b(?:reels?|shorts?)\b", "", title, flags=re.I)
    return " ".join(title.split()).strip(" -:|")


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
            "scenes. Each scene line is one narrated sentence of 12-20 words, so the Short "
            "runs 60 to 85 seconds. Open with the single most surprising fact so nobody scrolls away."
        )
    sport_rule = (
        "   This is a SPORT story. Report only the results, scores, injuries, "
        "transfers and records the headlines state. Do not predict the outcome, "
        "give betting odds or tips, or invent player statistics.\n"
        if is_sports(trend) else ""
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
        f"{sport_rule}"
        "5. The title must be a clear, non-clickbait description of the story.\n"
        + (f"   {PLAIN_TITLE_RULE}\n" if getattr(channel, "news_regions", ()) else "")
        + "Return ONLY JSON: {\"title\": str, \"intro\": str, \"outro\": str, "
        "\"scenes\": [{\"label\": str, \"line\": str, \"image_prompt\": str}]}.\n"
        "'label' = a 2-4 word on-screen caption. 'image_prompt' = a neutral, "
        "photo-real news visual for that scene (setting and objects only — no "
        "real people's faces, no text, no logos, no flags being burned)."
    )


def generate_trending_topics(channel: Channel, count: int = 2, scenes: int = 12) -> list[str]:
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
        lesson["title"] = plain_title(channel, str(lesson.get("title") or ""))
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
        + (f" {PLAIN_TITLE_RULE}" if getattr(channel, "news_regions", ()) else "")
    )


def is_trending_channel(channel: Channel) -> bool:
    return str(getattr(channel, "genre", "")).lower() == "trending"
