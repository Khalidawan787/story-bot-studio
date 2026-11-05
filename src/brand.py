"""Each channel's thumbnail style and brand kit, shown on the dashboard.

Adapted from the Chronovanta bot (2026-11-05). In data/brand/<channel id>/:

    style.md    the owner's notes on the channel's look (shown and edited on the dashboard)
    examples/   reference thumbnails the owner made or chose (the look to aim for)
    samples/    the bot's latest long + Short card, drawn in the current style

The style itself is one of the layouts in src/*_thumbnail.py, switched on per
channel by its flag in channels.json (only one at a time).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import settings

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
# channels.json flag -> what the dashboard calls it ("" = the plain card).
STYLE_FLAGS = {
    "": "Standard (picture + title bar)",
    "crime_thumbnail": "Crime case file (photo, yellow name)",
    "story_thumbnail": "History poster (hero photo, gold name, red ribbon)",
    "trend_thumbnail": "World Trending news frame",
    "news_thumbnail": "Ain The Explainer (anchor + 3 bars)",
    "motivation_thumbnail": "Daily Motivation gold glow",
    "sleep_thumbnail": "Painted storybook (sleep stories)",
}


def brand_dir(channel_id: str) -> Path:
    path = settings.root / "data" / "brand" / str(channel_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_style(channel) -> str:
    return next((flag for flag in STYLE_FLAGS if flag and getattr(channel, flag, False)), "")


def set_style(channel_id: str, flag: str) -> None:
    """Switch the channel to one layout: its flag on, the other style flags off."""
    from .channel_factory import CONFIG_FILE

    if flag not in STYLE_FLAGS:
        raise ValueError(f"unknown style {flag!r}")
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for row in config.get("channels", []):
        if row.get("id") == channel_id:
            for other in STYLE_FLAGS:
                if other:
                    row.pop(other, None)
            if flag:
                row[flag] = True
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def style_text(channel_id: str) -> str:
    path = brand_dir(channel_id) / "style.md"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def save_style(channel_id: str, text: str) -> None:
    (brand_dir(channel_id) / "style.md").write_text(text.strip() + "\n", encoding="utf-8")


def _images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if p.suffix.lower() in IMAGE_TYPES), key=lambda p: p.name)


def examples(channel_id: str) -> list[Path]:
    return _images(brand_dir(channel_id) / "examples")


def samples(channel_id: str) -> list[Path]:
    return _images(brand_dir(channel_id) / "samples")


def save_example(channel_id: str, storage) -> Path:
    name = Path(str(storage.filename or "")).name
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_TYPES:
        raise ValueError("Upload a JPG, PNG or WEBP picture.")
    folder = brand_dir(channel_id) / "examples"
    folder.mkdir(exist_ok=True)
    stem = re.sub(r"[^a-z0-9_-]+", "_", Path(name).stem.lower()).strip("_")[:40] or "example"
    target = folder / f"{stem}{suffix}"
    storage.save(str(target))
    return target


def delete_example(channel_id: str, name: str) -> None:
    path = brand_dir(channel_id) / "examples" / Path(name).name
    if path.is_file():
        path.unlink()


def _latest_topic(channel_id: str, kind: str) -> tuple[str, str] | None:
    """(topic, title) of the channel's latest uploaded video of this kind, else of any kind."""
    import sqlite3

    if not settings.db_path.exists():
        return None
    conn = sqlite3.connect(settings.db_path)
    try:
        row = None
        for wanted in (kind, None):
            row = conn.execute(
                "SELECT topic, title FROM videos WHERE channel = ? AND video_url IS NOT NULL "
                + ("AND COALESCE(content_type, 'short') = ? " if wanted else "")
                + "ORDER BY id DESC LIMIT 1", (channel_id, wanted) if wanted else (channel_id,),
            ).fetchone()
            if row:
                break
    finally:
        conn.close()
    return (row[0], row[1] or row[0]) if row else None


def make_samples(channel) -> list[str]:
    """A long and a Short card in today's style, from the channel's latest real videos."""
    from .lessons import load_lesson_for
    from .models import Lesson, Scene
    from .pipeline import _real_subject_thumbnail
    from .thumbnail_check import check_thumbnail
    from .thumbnails import create_thumbnail

    folder = brand_dir(channel.id) / "samples"
    folder.mkdir(exist_ok=True)
    notes = []
    for kind in ("long", "short"):
        lesson = None
        latest = _latest_topic(channel.id, kind)
        if latest:
            try:
                lesson = load_lesson_for(channel, latest[0])
            except Exception:
                lesson = None
        if lesson is None:
            # The script lives on GitHub only: the real title is enough for a card
            # (and, on a true-story channel, finds the person's own photo).
            topic, title = latest or (f"sample_{kind}", f"A New {str(channel.genre).title()} Story")
            lesson = Lesson(topic_key=topic, title=title, category=str(channel.genre).title(),
                            intro="", outro="", scenes=[Scene(label=title, line=title, image="")])
        work = folder / f"_{kind}"
        work.mkdir(exist_ok=True)
        made = (_real_subject_thumbnail(lesson, channel, work, kind)
                or create_thumbnail(lesson, None, work / "thumbnail.jpg", channel, kind))
        from .pipeline import style_matched
        from .style_match import save_check

        made = style_matched(made, lambda name: create_thumbnail(lesson, None, work / name, channel, kind),
                             work, channel)
        target = folder / f"{kind}.jpg"
        target.write_bytes(Path(made).read_bytes())
        from .style_match import compare

        save_check(target, compare(target, channel.id))
        notes.append(f"{kind}: {lesson.title[:60]} -> {check_thumbnail(target).summary}")
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    return notes


# Words in the style notes -> the part of the card that colour paints.
_ROLES = (
    ("bg", ("navy", "midnight")),
    ("bg2", ("steel", "teal", "slate blue")),
    ("title", ("gold",)),
    ("accent", ("crimson", "red")),
    ("text", ("ivory", "cream")),
)


def palette(channel_id: str, defaults: dict | None = None) -> dict:
    """The colours named in the channel's style notes, e.g. '#0B1729 Navy' -> bg.

    The first colour whose name has a role word wins (navy/midnight = background,
    steel/teal = second background, gold = title, crimson/red = ribbon,
    ivory/cream = lettering). Missing roles keep the defaults. Values are
    ffmpeg colours ("0x0b1729").
    """
    found = dict(defaults or {})
    taken = set()
    for hex_code, name in re.findall(r"#([0-9A-Fa-f]{6})\s*[-:]?\s*([A-Za-z][A-Za-z ]{1,30})", style_text(channel_id)):
        name = name.strip().lower()
        for role, words in _ROLES:
            if role not in taken and any(word in name for word in words):
                found[role] = "0x" + hex_code.lower()
                taken.add(role)
                break
    return found
