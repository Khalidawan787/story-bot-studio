from __future__ import annotations

from .models import Lesson


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
    hashtags = [
        "#KidsLearning",
        "#LearnWithFun",
        "#PreschoolLearning",
        "#EducationalVideos",
        "#YouTubeShorts",
        "#Shorts",
    ]
    label_tags = [f"#{label.replace(' ', '')}" for label in labels[:6]]
    description = "\n".join(
        [
            f"Play a quick {lesson.category.lower()} guessing game with your child.",
            "Pause, guess, say the answer aloud, and learn with fresh pictures and a clear voice.",
            "",
            "In this lesson:",
            *[f"- {label}" for label in labels],
            "",
            "Great for toddlers, preschool, kindergarten, and early learners. Can you get every answer?",
            "",
            " ".join([*hashtags, *label_tags]),
        ]
    )
    tags = [
        "kids learning",
        "learn for kids",
        lesson.category.lower(),
        "educational shorts",
        "preschool learning",
        "kindergarten learning",
        "toddler learning",
        "nursery learning",
        "learning videos for kids",
        "fun learning shorts",
        "kids education",
        "youtube shorts kids",
        *[label.lower() for label in labels],
    ]
    return {"title": title[:100], "description": description, "tags": tags}


def _long_metadata(lesson: Lesson, channel=None) -> dict[str, object]:
    genre = getattr(channel, "genre", "kids")
    suffix = getattr(channel, "seo_suffix", "")
    suffix = suffix.replace("Shorts", "").replace("Short", "").strip(" |")
    title = f"{lesson.title} | {suffix or genre.title() + ' Video'}"
    labels = [scene.label for scene in lesson.scenes]
    chapter_step = max(10, int(300 / max(1, len(labels))))
    chapters = []
    for index, label in enumerate(labels):
        seconds = index * chapter_step
        chapters.append(f"{seconds // 60}:{seconds % 60:02d} {label}")
    description = "\n".join([
        lesson.intro or f"Enjoy this complete {genre} video: {lesson.title}.",
        "", "Chapters:", *chapters,
        "", "In this video:",
        *[f"- {label}" for label in labels[:12]],
        "", f"#{genre} #{genre}video #story",
    ])
    tags = [
        genre, f"{genre} video", f"{genre} story", "long video",
        "storytime", lesson.title.lower(), *[label.lower() for label in labels[:10]],
    ]
    return {"title": title[:100], "description": description, "tags": tags}

def _genre_metadata(lesson: Lesson, channel) -> dict[str, object]:
    genre = channel.genre
    title = f"{lesson.title} {channel.seo_suffix}".strip()
    description = "\n".join([
        lesson.intro or f"A {genre} story: {lesson.title}.",
        "",
        "Watch till the end!",
        "",
        f"#{genre} #{genre}story #story #shorts",
    ])
    tags = [
        genre, f"{genre} story", f"{genre} stories", "story", "storytime",
        "shorts", f"{genre} shorts", lesson.title.lower(),
    ]
    return {"title": title[:100], "description": description, "tags": tags}
