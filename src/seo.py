from __future__ import annotations

import re

from .models import Lesson


# YouTube's hard limits. Tags are budgeted by TOTAL characters, not by count,
# so a few long junk tags would silently crowd out the strong ones.
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000
TAGS_TOTAL_LIMIT = 450
MAX_TAG_LENGTH = 45
MIN_STRONG_TAGS = 12

_KEYWORD_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "how", "in", "into", "is", "it", "its", "of", "on", "or",
    "our", "over", "part", "that", "the", "their", "then", "there", "these",
    "this", "to", "up", "was", "were", "what", "when", "which", "who", "why",
    "will", "with", "you", "your", "we", "us", "they", "them", "his", "her",
    "one", "two", "video", "story", "episode", "full", "complete", "watch",
}

# Search terms people actually type for each genre. These lead the tag list so
# every upload carries strong, human-searched keywords, not only auto-generated
# scene labels.
_GENRE_KEYWORDS = {
    "kids": [
        "kids learning", "learning videos for kids", "preschool learning",
        "toddler learning", "kindergarten learning", "educational videos for kids",
        "learn for kids", "nursery rhymes learning", "fun learning for kids",
        "early learning", "kids education", "learning for toddlers",
    ],
    "crime": [
        "true crime", "true crime story", "true crime stories", "crime documentary",
        "unsolved mystery", "detective story", "murder mystery", "crime explained",
        "mystery story", "cold case", "crime investigation", "true crime podcast",
    ],
    "horror": [
        "horror story", "scary story", "scary stories", "creepypasta",
        "horror stories in english", "true horror story", "ghost story",
        "paranormal story", "scary story time", "horror narration",
        "haunted story", "creepy story",
    ],
    "love": [
        "love story", "romantic story", "sad love story", "true love story",
        "emotional story", "heart touching story", "romance story",
        "relationship story", "love story in english", "sad story",
        "breakup story", "romantic short story",
    ],
    "motivation": [
        "motivational video", "motivational speech", "motivation", "self improvement",
        "inspirational video", "success mindset", "discipline motivation",
        "life changing motivation", "best motivational speech", "mindset motivation",
        "never give up", "morning motivation",
    ],
    "trending": [
        "trending news", "world news", "breaking news", "news today",
        "news explained", "current affairs", "global news", "top news",
        "latest news", "news update", "world news today", "explained news",
    ],
}


def _genre_of(channel) -> str:
    return str(getattr(channel, "genre", "kids") or "kids").lower()


def _keyword_phrases(lesson: Lesson, limit: int = 12) -> list[str]:
    """Searchable phrases taken from the video's own title and scene labels."""
    phrases: list[str] = []
    for label in [scene.label for scene in lesson.scenes]:
        cleaned = " ".join(
            word for word in re.findall(r"[A-Za-z0-9']+", label)
            if word.lower() not in _KEYWORD_STOP_WORDS
        ).strip().lower()
        if len(cleaned) > 2 and cleaned not in phrases:
            phrases.append(cleaned)
    title_words = [
        word.lower() for word in re.findall(r"[A-Za-z0-9']+", _clean_publish_title(lesson.title))
        if word.lower() not in _KEYWORD_STOP_WORDS and len(word) > 2
    ]
    # Two-word phrases from the title search far better than single words.
    for first, second in zip(title_words, title_words[1:]):
        phrase = f"{first} {second}"
        if phrase not in phrases:
            phrases.append(phrase)
    phrases.extend(word for word in title_words if word not in phrases)
    return phrases[:limit]


def _pack_tags(candidates: list[str]) -> list[str]:
    """Dedupe and fill YouTube's 500-character tag budget, strongest first.

    YouTube rejects the whole upload with `invalidTags` when the budget is
    exceeded, and it wraps any tag that contains a space in quotes before
    counting, so a multi-word tag really costs len + 2. Counting only len + 1
    overshot the real limit and made every upload fail with HTTP 400.
    """
    tags: list[str] = []
    seen: set[str] = set()
    used = 0
    for raw in candidates:
        # `<` and `>` are rejected outright; quotes break YouTube's own quoting.
        cleaned = "".join(ch for ch in str(raw or "") if ch not in '<>"')
        tag = " ".join(cleaned.split()).strip(" ,.-").lower()
        if len(tag) < 3 or len(tag) > MAX_TAG_LENGTH or tag in seen:
            continue
        # Separator, plus the two quote characters YouTube adds around any
        # tag containing a space.
        cost = len(tag) + 1 + (2 if " " in tag else 0)
        if used + cost > TAGS_TOTAL_LIMIT:
            continue
        seen.add(tag)
        tags.append(tag)
        used += cost
    return tags


def _story_body(lesson: Lesson, skip_intro: bool = False) -> str:
    """The full narration, so the description carries the whole story text.

    skip_intro avoids repeating the intro when the caller already used it as the
    opening hook paragraph.
    """
    parts = [] if skip_intro else [" ".join(str(lesson.intro or "").split())]
    for scene in lesson.scenes:
        line = " ".join(str(scene.line or "").split())
        if line:
            parts.append(line)
    outro = " ".join(str(lesson.outro or "").split())
    if outro:
        parts.append(outro)
    return "\n\n".join(part for part in parts if part)


def _keyword_line(keywords: list[str]) -> str:
    picked = [keyword for keyword in keywords if keyword][:10]
    return f"Topics covered: {', '.join(picked)}." if picked else ""


def _hashtags(genre: str, keywords: list[str], content_type: str) -> str:
    tags = [f"#{genre}"]
    for keyword in keywords[:6]:
        squashed = re.sub(r"[^a-z0-9]", "", keyword.lower())
        if 3 <= len(squashed) <= 24 and f"#{squashed}" not in tags:
            tags.append(f"#{squashed}")
    if content_type != "long":
        tags.append("#shorts")
    return " ".join(tags)


STORY_MARKER = "<<story>>"


def _assemble_description(blocks: list[str]) -> str:
    """Join the description sections and keep it inside YouTube's limit.

    A block passed as (STORY_MARKER, text) is the flexible one: the story text
    shrinks to whatever room is left, so chapters, keywords and hashtags at the
    end are never the parts that get cut.
    """
    fixed = [
        block for block in blocks
        if isinstance(block, str) and block and block.strip()
    ]
    story = next(
        (str(block[1]) for block in blocks
         if isinstance(block, tuple) and block[0] == STORY_MARKER),
        "",
    )
    reserved = sum(len(block.strip()) + 2 for block in fixed)
    room = DESCRIPTION_LIMIT - reserved - 2
    if story.strip() and room > 300:
        story = _trim_paragraphs(story.strip(), room)
    else:
        story = ""

    ordered: list[str] = []
    for block in blocks:
        if isinstance(block, tuple) and block[0] == STORY_MARKER:
            if story:
                ordered.append(story)
        elif isinstance(block, str) and block.strip():
            ordered.append(block.strip())
    return "\n\n".join(ordered)[:DESCRIPTION_LIMIT]


def _trim_paragraphs(text: str, budget: int) -> str:
    """Keep whole paragraphs up to the budget rather than cutting mid-sentence."""
    if len(text) <= budget:
        return text
    kept: list[str] = []
    used = 0
    for paragraph in text.split("\n\n"):
        cost = len(paragraph) + 2
        if used + cost > budget:
            break
        kept.append(paragraph)
        used += cost
    return "\n\n".join(kept)


def _clean_publish_title(value: str) -> str:
    text = " ".join(str(value or "").split()).strip()
    match = re.search(r"inspired\s+by\s*:\s*(.+?)(?:\.\s*Date seed\s*:|\.\s*Creator guideline\s*:|$)", text, re.I)
    if match:
        text = match.group(1)
    text = re.split(r"\b(?:Date seed|Creator guideline|intended for)\s*:", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"^(?:create|write|make)\s+.*?(?:video|story|episode)\s*(?:about|on)?\s*:?\s*", "", text, flags=re.I)
    return text.strip(" .:-") or "Untitled Story"


def _kids_short_title(lesson: Lesson, labels: list[str]) -> str:
    category = lesson.category.strip().lower()
    names = ", ".join(labels[:3]) or lesson.title
    if "animal" in category:
        challenge = f"Can You Guess {names}?"
    elif "color" in category or "colour" in category:
        challenge = f"Can You Find {names}?"
    elif "number" in category or "count" in category:
        challenge = f"Can You Count {names}?"
    elif "shape" in category:
        challenge = f"Can You Spot {names}?"
    elif "alphabet" in category or "letter" in category:
        challenge = f"Can You Say {names}?"
    else:
        challenge = f"Can You Name {names}?"
    return f"{challenge} | {lesson.title}"[:100]


def build_metadata(lesson: Lesson, channel=None, content_type: str = "short") -> dict[str, object]:
    if content_type == "long":
        return _long_metadata(lesson, channel)
    # Non-kids genre channels get their own SEO; kids keeps the original output.
    if channel is not None and not getattr(channel, "builtin", False):
        return _genre_metadata(lesson, channel)

    labels = [scene.label for scene in lesson.scenes]
    title = _kids_short_title(lesson, labels)
    category = lesson.category.lower()
    keywords = _keyword_phrases(lesson)
    core = _GENRE_KEYWORDS["kids"]
    hashtags = [
        "#KidsLearning", "#LearnWithFun", "#PreschoolLearning",
        "#EducationalVideos", "#YouTubeShorts", "#Shorts",
    ]
    label_tags = [f"#{label.replace(' ', '')}" for label in labels[:6]]
    description = _assemble_description([
        f"Play a quick {category} guessing game with your child. "
        "Pause, guess, say the answer aloud, and learn with fresh pictures and a clear voice.",
        (STORY_MARKER, _story_body(lesson)),
        "In this lesson:\n" + "\n".join(f"- {label}" for label in labels),
        _keyword_line([f"{category} for kids", *core[:4], *keywords[:5]]),
        "Great for toddlers, preschool, kindergarten, and early learners. "
        "Can you get every answer? Subscribe for a new learning video every day.",
        " ".join([*hashtags, *label_tags]),
    ])
    tags = _pack_tags([
        *core,
        f"{category} for kids",
        f"learn {category}",
        f"{category} for toddlers",
        *[label.lower() for label in labels],
        "educational shorts", "fun learning shorts", "youtube shorts kids",
        "learning game for kids", "kids guessing game",
        *keywords,
    ])
    return {"title": title[:TITLE_LIMIT], "description": description, "tags": tags}


def _long_metadata(lesson: Lesson, channel=None) -> dict[str, object]:
    genre = getattr(channel, "genre", "kids")
    if genre == "trending":
        return _news_metadata(lesson, channel, "long")
    suffix = getattr(channel, "seo_suffix", "")
    suffix = suffix.replace("Shorts", "").replace("Short", "").strip(" |")
    clean_title = _clean_publish_title(lesson.title)
    labels = [scene.label for scene in lesson.scenes]
    keywords = _keyword_phrases(lesson)
    core = _GENRE_KEYWORDS.get(genre, [genre])
    title = _seo_title(clean_title, f"| {suffix or genre.title() + ' Video'}", core[:2])

    chapter_step = max(10, int(300 / max(1, len(labels))))
    chapters = [
        f"{(index * chapter_step) // 60}:{(index * chapter_step) % 60:02d} {label}"
        for index, label in enumerate(labels)
    ]
    description = _assemble_description([
        lesson.intro or f"Enjoy this complete {genre} video: {clean_title}.",
        (STORY_MARKER, _story_body(lesson, skip_intro=True)),
        "Chapters:\n" + "\n".join(chapters),
        "In this video:\n" + "\n".join(f"- {label}" for label in labels[:20]),
        _keyword_line(core[:5] + keywords[:5]),
        f"Subscribe for a full-length {genre} video every week.",
        _hashtags(genre, core[:3] + keywords[:3], "long"),
    ])
    tags = _pack_tags([
        *core,
        clean_title.lower(),
        *keywords,
        f"{genre} video", f"full {genre} video", "storytime", "long video",
        *[f"{keyword} explained" for keyword in keywords[:3]],
    ])
    return {"title": title, "description": description, "tags": tags}

def _news_metadata(lesson: Lesson, channel, content_type: str) -> dict[str, object]:
    """News SEO: no 'story/storytime' wording, and a factual-reporting notice."""
    clean_title = _clean_publish_title(lesson.title)
    labels = [scene.label for scene in lesson.scenes]
    keywords = _keyword_phrases(lesson)
    core = _GENRE_KEYWORDS["trending"]
    subject = clean_title.lower()
    title = _seo_title(clean_title, channel.seo_suffix, core[:2])

    description = _assemble_description([
        lesson.intro or f"What is happening with {subject}, and why it matters.",
        (STORY_MARKER, _story_body(lesson, skip_intro=True)),
        "In this video:\n" + "\n".join(f"- {label}" for label in labels[:20]),
        _keyword_line(core[:4] + keywords[:6]),
        "This is an explainer based on publicly reported headlines. Details can "
        "change as a story develops — always check current sources before "
        "drawing conclusions.",
        _hashtags("trending", ["worldnews", "breakingnews", "explained", *keywords[:3]], content_type),
    ])
    tags = _pack_tags([
        *core,
        subject,
        *keywords,
        "what happened", "news analysis", "news story",
        *[f"{keyword} news" for keyword in keywords[:3]],
        *(["news shorts", "shorts"] if content_type != "long" else ["full news explainer"]),
    ])
    return {"title": title, "description": description, "tags": tags}


def _seo_title(hook: str, suffix: str, keywords: list[str]) -> str:
    """Hook first, then the genre suffix, then one more searched keyword if it fits."""
    title = f"{hook} {suffix}".strip() if suffix else hook
    if len(title) > TITLE_LIMIT:
        return title[:TITLE_LIMIT].rstrip(" |-,")
    existing = set(re.findall(r"[a-z]{4,}", title.lower()))
    for keyword in keywords:
        extra = f" | {keyword.title()}"
        words = set(re.findall(r"[a-z]{4,}", keyword.lower()))
        # Skip keywords that only echo words the title already ranks for.
        if len(title) + len(extra) <= TITLE_LIMIT and words and not (words & existing):
            return title + extra
    return title


def _genre_metadata(lesson: Lesson, channel) -> dict[str, object]:
    genre = channel.genre
    if genre == "trending":
        return _news_metadata(lesson, channel, "short")
    labels = [scene.label for scene in lesson.scenes]
    keywords = _keyword_phrases(lesson)
    core = _GENRE_KEYWORDS.get(genre, [genre])
    hook = _clean_publish_title(lesson.title)
    title = _seo_title(hook, channel.seo_suffix, core[:2])

    description = _assemble_description([
        lesson.intro or f"A {genre} story: {hook}.",
        # The full story text: this is what YouTube search actually reads.
        (STORY_MARKER, _story_body(lesson, skip_intro=True)),
        "In this video:\n" + "\n".join(f"- {label}" for label in labels[:15]),
        _keyword_line(core[:5] + keywords[:5]),
        "Subscribe for a new " + genre + " story every day, and tell us in the "
        "comments which part hit hardest.",
        _hashtags(genre, core[:3] + keywords[:3], "short"),
    ])
    tags = _pack_tags([
        *core,
        hook.lower(),
        *keywords,
        f"{genre} shorts", f"{genre} story shorts", "shorts", "short story",
        *[f"{keyword} story" for keyword in keywords[:4]],
    ])
    return {"title": title, "description": description, "tags": tags}
