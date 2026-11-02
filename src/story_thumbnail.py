"""A biography channel's thumbnail: the hero's photo, a short name and one fact.

Dark left side, the person's own photo filling the right, and in a serif face:

    The story of ────
    ROOSEVELT
    The 32nd US President

Switched on per channel with "story_thumbnail": true in channels.json (World
Valor Chronicles). The photo is the subject's own free Wikipedia photo, which
pipeline.verified_thumbnail saves as article_photo.jpg; without one the art the
thumbnail was given is used.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import settings
from .models import Lesson
from .presenter import _escape, _text_width
from .thumbnails import THUMB_HEIGHT, THUMB_WIDTH

SERIF = settings.root / "fonts" / "DMSerifDisplay-Regular.ttf"
_BG = "0x0d0e12"
_GOLD = "0xd4ad55"
_CREAM = "0xf3ead2"
_PHOTO_X = 440
_TEXT_X = 70
_TEXT_LIMIT = 600
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def _subject(lesson: Lesson, run_dir: Path) -> str:
    from .stock_video import SUBJECT_FILE

    try:
        subject = (run_dir / SUBJECT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        subject = ""
    return subject or re.split(r"[:|]", str(lesson.title or ""))[0].strip()


def _rule_name(subject: str) -> str:
    """'Franklin D. Roosevelt' -> 'Franklin Roosevelt'; 'Helen Adams Keller' -> 'Helen Keller'."""
    words = [w for w in re.sub(r"\(.*?\)", "", subject).replace(",", " ").split()
             if not re.fullmatch(r"[A-Z]\.?", w) and w.lower() not in _SUFFIXES]
    return " ".join(words if len(words) <= 2 else [words[0], words[-1]])


def name_and_fact(lesson: Lesson, run_dir: Path) -> tuple[str, str]:
    """The short name people know and a 2-5 word fact, from this script only."""
    cache = run_dir / "thumbnail_story.txt"
    if cache.exists():
        name, _, fact = cache.read_text(encoding="utf-8").partition("\n")
        return name.strip(), fact.strip()
    subject = _subject(lesson, run_dir)
    script = " ".join(scene.line for scene in lesson.scenes)[:3000]
    name, fact = _rule_name(subject), ""
    try:
        from .genre_topics import _script_json

        raw = _script_json(
            "For a YouTube thumbnail about a real person, return the short name people "
            "know them by (1-2 words, e.g. 'Napoleon', 'Roosevelt', 'Abdus Salam', "
            "'Helen Keller') and, in 2-5 words, the ONE thing from the script they are most "
            "famous for: a title, a prize or a record, not a minor detail "
            "(e.g. '35th US President', 'Nobel Prize in Physics', '1971 World Series MVP'). "
            "Use only what the script says: no new names, numbers or claims.\n"
            f"Person: {subject}\nTitle: {lesson.title}\nScript: {script}\n"
            'Return ONLY JSON: {"name": str, "fact": str}'
        )
        got_name = " ".join(str(raw.get("name") or "").split())
        got_fact = " ".join(str(raw.get("fact") or "").split()).strip(" .")
        # The name must be words of the person's own name.
        if got_name and all(w.lower() in subject.lower() for w in got_name.split()) \
                and len(got_name.split()) <= 3:
            name = got_name
        numbers = set(re.findall(r"\d[\d,.]*\d|\d", got_fact))
        if got_fact and len(got_fact.split()) <= 6 and not numbers - set(re.findall(r"\d[\d,.]*\d|\d", script)):
            fact = got_fact
    except Exception as exc:
        print(f"[thumbnail] story name/fact failed ({exc})")
    if not fact:
        tail = str(lesson.title or "").split("|")[0].partition(":")[2].strip()
        fact = tail if 0 < len(tail.split()) <= 6 else ""
    cache.write_text(f"{name}\n{fact}", encoding="utf-8")
    return name, fact


def _fit(text: str, file: Path, limit: int, largest: int, smallest: int) -> int:
    file.write_text(text, encoding="utf-8")
    return max(smallest, min(largest, 100 * limit // _text_width(file, str(SERIF), 100)))


def compose_story_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                            channel=None, content_type: str = "short") -> Path:
    run_dir = output_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    name, fact = name_and_fact(lesson, run_dir)
    photo = run_dir / "article_photo.jpg"
    picture = photo if photo.is_file() else (Path(art_path) if art_path and Path(art_path).exists() else None)

    intro_file = output_path.with_suffix(".intro.txt")
    intro_file.write_text("The story of", encoding="utf-8")
    intro_size = 46
    intro_w = _text_width(intro_file, str(SERIF), intro_size)
    # The name: one line as big as fits, or two lines when one gets too small.
    words = name.upper().split()
    name_lines = [" ".join(words)]
    size = _fit(name_lines[0], output_path.with_suffix(".name0.txt"), _TEXT_LIMIT, 150, 60)
    if size < 92 and len(words) >= 2:
        name_lines = [" ".join(words[:len(words) // 2]), " ".join(words[len(words) // 2:])]
        size = min(_fit(line, output_path.with_suffix(f".name{i}.txt"), _TEXT_LIMIT, 130, 60)
                   for i, line in enumerate(name_lines))
    name_files = [output_path.with_suffix(f".name{i}.txt") for i in range(len(name_lines))]
    for file, line in zip(name_files, name_lines):
        file.write_text(line, encoding="utf-8")
    fact_file = output_path.with_suffix(".fact.txt")
    fact_size = _fit(fact, fact_file, _TEXT_LIMIT, 56, 34) if fact else 0

    line_gap = int(size * 0.98)
    block = 64 + line_gap * len(name_lines) + (fact_size + 16 if fact else 0)
    top = max(60, (THUMB_HEIGHT - block) // 2)
    name_top = top + 64

    def draw(file: Path, size_: int, y: int, colour: str) -> str:
        return (f"drawtext=fontfile='{_escape(SERIF)}':textfile='{_escape(file)}':fontsize={size_}:"
                f"x={_TEXT_X}:y={y}:fontcolor={colour}:shadowx=2:shadowy=3:shadowcolor=black@0.8")

    text = [
        f"drawbox=x=28:y=56:w=4:h={THUMB_HEIGHT - 112}:color={_GOLD}:t=fill",
        draw(intro_file, intro_size, top, _CREAM),
        f"drawbox=x={_TEXT_X + intro_w + 22}:y={top + intro_size // 2}:w=130:h=9:color={_GOLD}:t=fill",
        *(draw(file, size, name_top + i * line_gap, _CREAM) for i, file in enumerate(name_files)),
    ]
    if fact:
        text.append(draw(fact_file, fact_size, name_top + line_gap * len(name_lines) + 12, _GOLD))
    width = THUMB_WIDTH - _PHOTO_X
    inputs = ["-f", "lavfi", "-i", f"color=c={_BG}:s={THUMB_WIDTH}x{THUMB_HEIGHT}"]
    if picture:
        inputs += ["-i", str(picture)]
        graph = (
            # Keep the top of a portrait, where the face is.
            f"[1]scale={width}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={width}:{THUMB_HEIGHT}:(iw-{width})/2:(ih-{THUMB_HEIGHT})*0.15,format=rgba[p];"
            f"color=c=black:s={width}x{THUMB_HEIGHT},format=rgba,"
            f"geq=r=13:g=14:b=18:a='255*max(0\\,1-X/380)'[fade];"
            f"[p][fade]overlay=0:0[pf];"
            f"[0][pf]overlay={_PHOTO_X}:0,{','.join(text)}"
        )
    else:
        graph = f"[0]{','.join(text)}"
    subprocess.run(
        [settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", graph,
         "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path
