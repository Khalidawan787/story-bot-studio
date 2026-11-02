"""Daily Motivation (Risevion) thumbnail: a gold frame and a short slogan that glows.

The picture fills a thin gold frame, darker on the left. Top left a slanted
gold "DAILY MOTIVATION" badge; under it a 2-4 word slogan ("PAIN CREATES
POWER") in heavy white slanted capitals with an orange edge and glow, one or
two words a line.

Switched on per channel with "motivation_thumbnail": true in channels.json.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import settings
from .models import Lesson
from .presenter import _escape, _text_width
from .thumbnails import THUMB_HEIGHT, THUMB_WIDTH

FONT = settings.root / "fonts" / "ArchivoBlack-Regular.ttf"
BADGE_TEXT = "DAILY MOTIVATION"
_GOLD = "0xf4b329"
_ORANGE = "0xff8a00"
_TEXT_X = 48
_TEXT_LIMIT = 600
_SLANT = 0.16


def slogan(lesson: Lesson, run_dir: Path) -> str:
    """2-4 punchy words for the picture, not the whole title."""
    cache = run_dir / "thumbnail_slogan.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8").strip()
    title = re.split(r"\s*\|\s*", str(lesson.title or ""))[0].strip()
    text = ""
    try:
        from .genre_topics import _script_json

        raw = _script_json(
            "Write a 2-4 word slogan for a motivational YouTube thumbnail, in the spirit of "
            "'No Excuses. Just Action.', 'Become Unstoppable', 'Pain Creates Power', "
            "'Discipline Builds Destiny'. It must fit this video and not copy its title.\n"
            f"Title: {title}\n"
            'Return ONLY JSON: {"slogan": str}'
        )
        text = " ".join(str(raw.get("slogan") or "").split())
    except Exception as exc:
        print(f"[thumbnail] slogan failed ({exc})")
    if not text or len(text.split()) > 5 or len(text) > 34:
        words = [w for w in re.sub(r"[^\w' ]", " ", title).split()
                 if w.lower() not in {"the", "a", "an", "of", "your", "to", "and"}]
        text = " ".join(words[:3]) or title
    text = text.upper()
    cache.write_text(text, encoding="utf-8")
    return text


def _lines(text: str) -> list[str]:
    words = text.split()
    # One word a line, as big as fits; longer slogans pair short words up.
    if len(words) <= 3:
        return words
    lines, current = [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > 11:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    lines.append(current)
    return lines[:3]


def compose_motivation_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                                 channel=None, content_type: str = "short") -> Path:
    run_dir = output_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    font = _escape(FONT)
    lines = _lines(slogan(lesson, run_dir))
    files = [output_path.with_suffix(f".mot{i}.txt") for i in range(len(lines))]
    for file, line in zip(files, lines):
        file.write_text(line, encoding="utf-8")
    size = max(70, min(150, min(100 * _TEXT_LIMIT // _text_width(f, str(FONT), 100) for f in files)))
    gap = int(size * 1.05)
    block_h = gap * len(lines) + 40
    top = max(110, 110 + (THUMB_HEIGHT - 130 - block_h) // 2)

    badge_file = output_path.with_suffix(".motbadge.txt")
    badge_file.write_text(BADGE_TEXT, encoding="utf-8")
    badge_w = _text_width(badge_file, str(FONT), 38) + 52

    def words(colour: str, border: int, border_colour: str) -> str:
        return ",".join(
            f"drawtext=fontfile='{font}':textfile='{_escape(file)}':fontsize={size}:fontcolor={colour}:"
            f"borderw={border}:bordercolor={border_colour}:x=40:y={20 + i * gap}"
            for i, file in enumerate(files)
        )

    if art_path and Path(art_path).exists():
        inputs = ["-i", str(art_path)]
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0x15100a:s={THUMB_WIDTH}x{THUMB_HEIGHT}"]
    layer = f"color=c=black@0:s={_TEXT_LIMIT + 160}x{block_h},format=rgba"
    graph = ";".join([
        f"[0]scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT},eq=contrast=1.05:saturation=1.1[pic]",
        # Darker on the left, where the words go.
        f"color=c=black:s={THUMB_WIDTH}x{THUMB_HEIGHT},format=rgba,"
        f"geq=r=0:g=0:b=0:a='200*max(0\\,1-X/820)'[shade]",
        "[pic][shade]overlay=0:0[base]",
        # An orange glow under white words with an orange edge, then slanted.
        f"{layer},{words(_ORANGE, 14, _ORANGE)},gblur=sigma=14[glow]",
        f"{layer},{words('white', 5, _ORANGE)}[crisp]",
        f"[glow][crisp]overlay=0:0,shear=shx={_SLANT}:fillcolor=black@0,format=rgba[slogan]",
        f"[base][slogan]overlay={_TEXT_X - 40}:{top}[withtext]",
        f"color=c={_GOLD}:s={badge_w}x64,"
        f"drawtext=fontfile='{font}':textfile='{_escape(badge_file)}':fontsize=38:fontcolor=0x111111:"
        f"x=26:y=(64-text_h)/2,format=rgba,pad=w=iw+30:h=ih:x=15:y=0:color=black@0,"
        f"shear=shx=0.2:fillcolor=black@0,format=rgba[badge]",
        f"[withtext][badge]overlay=36:40,"
        f"drawbox=x=10:y=10:w={THUMB_WIDTH - 20}:h={THUMB_HEIGHT - 20}:color={_GOLD}:t=4",
    ])
    subprocess.run(
        [settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", graph,
         "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path
