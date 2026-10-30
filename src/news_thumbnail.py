"""The news channel's thumbnail: a coloured frame, a two-line headline, a badge.

Red for breaking news, purple for an explainer. A long video also shows the
anchor (fictional, data/presenter/anchor.jpg) in a box on the right; Shorts have
no anchor, so theirs keep the whole picture. Switched on per channel with
"news_thumbnail": true in channels.json.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import settings
from .models import Lesson
from .presenter import _escape, _text_width
from .stock_video import crime_text
from .thumbnails import _DANGLING_WORDS, THUMB_HEIGHT, THUMB_WIDTH, thumbnail_headline

ANCHOR_IMAGE = settings.root / "data" / "presenter" / "anchor.jpg"
HEADLINE_FONT = settings.root / "fonts" / "ArchivoBlack-Regular.ttf"
_RED = "0xe0101a"
_PURPLE = "0x5b1fa8"
_FRAME = 22
_BADGE_LEFT, _BADGE_BOTTOM = 1078, 134
_TEXT_LEFT = 64
_MAX_FONT = 84
_MIN_FONT = 40
# The anchor's box, against the right edge of the frame and under the badge.
_BOX_X, _BOX_Y = 975, 217
_BOX_W, _BOX_H = THUMB_WIDTH - _FRAME - _BOX_X, THUMB_HEIGHT - _FRAME - _BOX_Y
# Head and shoulders out of the 1344x768 picture, in the box's own shape.
_ANCHOR_CROP_H = 530
_ANCHOR_CROP_W = _ANCHOR_CROP_H * _BOX_W // _BOX_H
_ANCHOR_CROP_X, _ANCHOR_CROP_Y = 680 - _ANCHOR_CROP_W // 2, 25
_EXPLAINER = re.compile(r"^(WHY|HOW|WHAT|WHO)\b|\bEXPLAINED\b")
_FULL_NAME = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")


def _without_names(headline: str, lesson: Lesson) -> str:
    """Take the people the script names out of a headline.

    The anchor must never stand next to a real criminal's or suspect's name, or
    she reads as that person.
    """
    script = " ".join(str(scene.line or "") for scene in lesson.scenes)
    for name in set(_FULL_NAME.findall(script)):
        for part in (name, name.split()[1]):
            headline = re.sub(rf"\b{re.escape(part)}\b", " ", headline, flags=re.I)
    words = headline.split()
    while words and words[-1].lower().strip(",;:") in _DANGLING_WORDS:
        words.pop()
    return " ".join(words)


def _two_lines(headline: str) -> list[str]:
    """Split evenly by length, but never leave "OF" or "IN" ending the first line."""
    words = headline.split()
    if len(words) < 2:
        return [headline]

    def cost(cut: int) -> int:
        first, second = " ".join(words[:cut]), " ".join(words[cut:])
        return abs(len(first) - len(second)) + (20 if words[cut - 1].lower() in _DANGLING_WORDS else 0)

    cut = min(range(1, len(words)), key=cost)
    return [" ".join(words[:cut]), " ".join(words[cut:])]


def _fit(line: str, text_file: Path, limit: int, font: str) -> int:
    text_file.write_text(line, encoding="utf-8")
    return max(_MIN_FONT, min(_MAX_FONT, 100 * limit // _text_width(text_file, font, 100)))


def compose_news_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                           channel=None, content_type: str = "short") -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = str(HEADLINE_FONT)
    headline = thumbnail_headline(lesson, channel, content_type)
    show_anchor = content_type == "long" and ANCHOR_IMAGE.is_file()
    if show_anchor and crime_text(f"{lesson.title} {headline}"):
        cleaned = _without_names(headline, lesson)
        if len(cleaned.split()) >= 2:
            headline = cleaned
        else:
            show_anchor = False

    colour = _PURPLE if _EXPLAINER.search(headline) else _RED
    limit = (_BOX_X - 20 if show_anchor else THUMB_WIDTH - _FRAME - 20) - _TEXT_LEFT
    lines = _two_lines(headline)
    files = [output_path.with_suffix(f".line{index}.txt") for index in range(len(lines))]
    sizes = [_fit(line, path, limit, font) for line, path in zip(lines, files)]
    if len(sizes) == 2:
        sizes[1] = min(sizes)
    bottom = THUMB_HEIGHT - _FRAME - 56
    tops = [bottom - sizes[-1]]
    if len(sizes) == 2:
        tops.insert(0, tops[0] - sizes[0] - 22)
    band_top = tops[0] - 34

    def text(file: Path, size: int, y: int, x: int = _TEXT_LEFT) -> str:
        return (f"drawtext=fontfile='{_escape(font)}':textfile='{_escape(file)}':fontsize={size}:"
                f"x={x}:y={y}:fontcolor=white:shadowx=3:shadowy=3:shadowcolor=black@0.7")

    badge_files = [output_path.with_suffix(f".badge{index}.txt") for index in range(2)]
    badge_files[0].write_text("AIN", encoding="utf-8")
    badge_files[1].write_text("NEWS", encoding="utf-8")
    picture = ",".join([
        f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}",
        f"drawbox=x=0:y={band_top}:w={THUMB_WIDTH}:h={THUMB_HEIGHT - band_top}:color=black@0.6:t=fill",
    ])
    finish = ",".join([
        f"drawbox=x=0:y=0:w={THUMB_WIDTH}:h={THUMB_HEIGHT}:color={colour}:t={_FRAME}",
        f"drawbox=x={_BADGE_LEFT}:y=0:w={THUMB_WIDTH - _BADGE_LEFT}:h={_BADGE_BOTTOM}:color={colour}:t=fill",
        text(badge_files[0], 40, 30, _BADGE_LEFT + 20),
        text(badge_files[1], 40, 76, _BADGE_LEFT + 20),
        *(text(path, size, top) for path, size, top in zip(files, sizes, tops)),
    ])
    if art_path and Path(art_path).exists():
        inputs = ["-i", str(art_path)]
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0x10203a:s={THUMB_WIDTH}x{THUMB_HEIGHT}"]
    if show_anchor:
        graph = (
            f"[0]{picture}[bg];"
            f"[1]crop={_ANCHOR_CROP_W}:{_ANCHOR_CROP_H}:{_ANCHOR_CROP_X}:{_ANCHOR_CROP_Y},"
            f"scale={_BOX_W}:{_BOX_H},drawbox=x=0:y=0:w={_BOX_W}:h={_BOX_H}:color={colour}:t=6[anchor];"
            f"[bg][anchor]overlay=x={_BOX_X}:y={_BOX_Y},{finish}"
        )
        inputs += ["-i", str(ANCHOR_IMAGE)]
    else:
        graph = f"[0]{picture},{finish}"
    subprocess.run(
        [settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", graph,
         "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path
