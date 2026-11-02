"""SnuggleMoon Stories thumbnail: a cosy painted scene with a glowing storybook title.

The artwork fills the frame, a little darker at the top. Centred at the top,
the story's title in cream serif capitals with a soft gold glow, and under it
a smaller line ("A Calming Bedtime Story", or the title's own subtitle).
Like "The Honey House: A Gentle Victorian Sleep Story".

Switched on per channel with "sleep_thumbnail": true in channels.json; such a
channel's thumbnail art is drawn (Cloudflare FLUX first), not a stock photo.
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
_CREAM = "0xf8ebc8"
_GOLD = "0xffc35a"
_DEFAULT_SUBTITLE = "A Calming Bedtime Story"


def title_parts(lesson: Lesson) -> tuple[str, str]:
    """("THE HONEY HOUSE", "A Gentle Victorian Sleep Story") from the video title."""
    title = re.split(r"\s*\|\s*", str(lesson.title or ""))[0].strip()
    main, _, sub = title.partition(":")
    sub = sub.strip()
    if not sub or len(sub) > 46:
        sub = _DEFAULT_SUBTITLE
    return main.strip().upper(), sub


def art_prompt(lesson: Lesson) -> str:
    """A cosy painted scene for the story, with no figures or lettering."""
    main, _sub = title_parts(lesson)
    return (
        f"A cosy, peaceful storybook painting inspired by the bedtime story \"{main.title()}\": "
        "a quiet place at dusk or night, warm lantern and window light, soft moonlight, gentle "
        "stars, rich detail, painterly oil-on-canvas look, calm and sleepy mood, wide 16:9 "
        "landscape with an open sky at the top. No people, no faces, no characters, no animals "
        "in front, no text, no letters, no signs, no watermark."
    )


def _fit(text: str, file: Path, limit: int, largest: int, smallest: int) -> int:
    file.write_text(text, encoding="utf-8")
    return max(smallest, min(largest, 100 * limit // _text_width(file, str(SERIF), 100)))


def compose_sleep_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                            channel=None, content_type: str = "short") -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    main, sub = title_parts(lesson)
    words = main.split()
    lines = [main]
    title_file = output_path.with_suffix(".sleep0.txt")
    size = _fit(main, title_file, 1060, 104, 50)
    if size < 72 and len(words) > 2:
        half = (len(words) + 1) // 2
        lines = [" ".join(words[:half]), " ".join(words[half:])]
        size = min(_fit(line, output_path.with_suffix(f".sleep{i}.txt"), 1060, 96, 50)
                   for i, line in enumerate(lines))
    files = [output_path.with_suffix(f".sleep{i}.txt") for i in range(len(lines))]
    for file, line in zip(files, lines):
        file.write_text(line, encoding="utf-8")
    sub_file = output_path.with_suffix(".sleepsub.txt")
    sub_size = _fit(sub, sub_file, 760, 46, 28)
    gap = int(size * 1.08)
    block_h = gap * len(lines) + sub_size + 30

    def title_text(colour: str, extra: str = "") -> str:
        drawn = [
            f"drawtext=fontfile='{_escape(SERIF)}':textfile='{_escape(file)}':fontsize={size}:"
            f"fontcolor={colour}:x=(w-text_w)/2:y={10 + i * gap}{extra}"
            for i, file in enumerate(files)
        ]
        drawn.append(
            f"drawtext=fontfile='{_escape(SERIF)}':textfile='{_escape(sub_file)}':fontsize={sub_size}:"
            f"fontcolor={colour}:x=(w-text_w)/2:y={16 + gap * len(lines)}{extra}"
        )
        return ",".join(drawn)

    if art_path and Path(art_path).exists():
        inputs = ["-i", str(art_path)]
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0x1c2350:s={THUMB_WIDTH}x{THUMB_HEIGHT}"]
    layer = f"color=c=black@0:s={THUMB_WIDTH}x{block_h + 30},format=rgba"
    graph = ";".join([
        f"[0]scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT},eq=saturation=1.08[pic]",
        # Darker at the top for the title, and a soft vignette.
        f"color=c=black:s={THUMB_WIDTH}x{THUMB_HEIGHT},format=rgba,"
        f"geq=r=0:g=0:b=0:a='170*max(0\\,1-Y/330)+60*pow(hypot(X-W/2\\,Y-H/2)/hypot(W/2\\,H/2)\\,2)'[shade]",
        "[pic][shade]overlay=0:0[base]",
        f"{layer},{title_text(_GOLD)},gblur=sigma=10[glow]",
        f"{layer},{title_text(_CREAM, ':shadowx=2:shadowy=3:shadowcolor=black@0.7')}[crisp]",
        "[glow][crisp]overlay=0:0[title]",
        "[base][title]overlay=0:22",
    ])
    subprocess.run(
        [settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", graph,
         "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path
