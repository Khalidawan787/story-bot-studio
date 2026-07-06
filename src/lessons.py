from __future__ import annotations

from .models import Lesson, Scene
from .topic_bank import load_all_lessons


def load_lesson(topic_key: str) -> Lesson:
    lessons = load_all_lessons()

    if topic_key not in lessons:
        available = ", ".join(sorted(lessons))
        raise ValueError(f"Unknown topic '{topic_key}'. Available topics: {available}")

    raw = lessons[topic_key]
    return Lesson(
        topic_key=topic_key,
        title=raw["title"],
        category=raw.get("category", topic_key.title()),
        intro=raw.get("intro", ""),
        outro=raw.get("outro", ""),
        scenes=[Scene(**scene) for scene in raw["scenes"]],
    )


def load_topics() -> dict:
    return load_all_lessons()


def _lesson_from_raw(topic_key: str, raw: dict) -> Lesson:
    return Lesson(
        topic_key=topic_key,
        title=raw["title"],
        category=raw.get("category", topic_key.title()),
        intro=raw.get("intro", ""),
        outro=raw.get("outro", ""),
        scenes=[Scene(**scene) for scene in raw["scenes"]],
    )


def load_topics_for(channel) -> dict:
    """Topic pool for a channel: built-in bank (kids) or genre file, PLUS any
    custom-prompt topics the user wrote."""
    from .genre_topics import load_genre_lessons, load_custom_lessons
    base = dict(load_all_lessons() if channel.builtin else load_genre_lessons(channel))
    base.update(load_custom_lessons(channel))
    return base


def load_lesson_for(channel, topic_key: str) -> Lesson:
    """Load one lesson from the correct source for a channel (custom first)."""
    from .genre_topics import load_custom_lessons
    custom = load_custom_lessons(channel)
    if topic_key in custom:
        return _lesson_from_raw(topic_key, custom[topic_key])
    if channel.builtin:
        return load_lesson(topic_key)
    from .genre_topics import load_genre_lessons
    lessons = load_genre_lessons(channel)
    if topic_key not in lessons:
        raise ValueError(f"Unknown {channel.genre} topic '{topic_key}'.")
    return _lesson_from_raw(topic_key, lessons[topic_key])
