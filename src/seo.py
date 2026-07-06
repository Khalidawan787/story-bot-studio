from __future__ import annotations

from .models import Lesson


def build_metadata(lesson: Lesson, channel=None) -> dict[str, object]:
    # Non-kids genre channels get their own SEO; kids keeps the original output.
    if channel is not None and not getattr(channel, "builtin", False):
        return _genre_metadata(lesson, channel)

    labels = [scene.label for scene in lesson.scenes]
    title = f"{lesson.title} for Kids | Fun Learning Shorts"
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
            f"Learn {lesson.category.lower()} with this colorful educational Short for kids.",
            "Simple words, bright pictures, clear voice, and gentle music for preschool learning.",
            "",
            "In this lesson:",
            *[f"- {label}" for label in labels],
            "",
            "Great for toddlers, preschool, kindergarten, and early learners.",
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
