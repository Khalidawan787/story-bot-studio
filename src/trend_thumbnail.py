"""World Trending (TrendNerve Global) thumbnail: a framed news picture with a slanted badge.

The picture fills a frame with a thin blue edge. Top left is a slanted blue
"WORLD TRENDING" badge with a red slash. At the bottom a navy band with a blue
top line carries the headline in white, bold, slanted capitals, over a faint
dotted map texture, with a red line under it.

Switched on per channel with "trend_thumbnail": true in channels.json.
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
BADGE_TEXT = "WORLD TRENDING"
_NAVY = "0x07122b"
_BLUE = "0x1f6fe5"
_EDGE = "0x4aa3ff"
_RED = "0xe3192b"
_MARGIN = 16
_BAND_TOP = 470
_TEXT_X = 54
_TEXT_LIMIT = 1120
_SLANT = 0.18


def _headline(lesson: Lesson) -> str:
    title = re.split(r"\s*\|\s*", str(lesson.title or ""))[0].strip().upper()
    return title if len(title) <= 70 else title[:69].rsplit(" ", 1)[0]


def _lines(headline: str) -> list[str]:
    from .news_thumbnail import _two_lines

    return _two_lines(headline) if len(headline) > 26 else [headline]


def _fit(line: str, file: Path, limit: int, largest: int, smallest: int) -> int:
    file.write_text(line, encoding="utf-8")
    return max(smallest, min(largest, 100 * limit // _text_width(file, str(FONT), 100)))


def compose_trend_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                            channel=None, content_type: str = "short") -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = _escape(FONT)
    lines = _lines(_headline(lesson))
    files = [output_path.with_suffix(f".trend{i}.txt") for i in range(len(lines))]
    size = min(_fit(line, file, _TEXT_LIMIT, 86, 44) for line, file in zip(lines, files))
    badge_file = output_path.with_suffix(".trendbadge.txt")
    badge_file.write_text(BADGE_TEXT, encoding="utf-8")
    badge_size = 40
    badge_w = _text_width(badge_file, str(FONT), badge_size) + 56

    inner_w, inner_h = THUMB_WIDTH - 2 * _MARGIN, THUMB_HEIGHT - 2 * _MARGIN
    band_h = THUMB_HEIGHT - _MARGIN - _BAND_TOP
    gap = int(size * 1.12)
    text_top = _BAND_TOP + (band_h - 14 - gap * len(lines)) // 2 + 6

    # Headline and badge are drawn on their own transparent layers, then slanted.
    # Shear slants around the layer's middle, so each layer is cut to its own
    # content and has room on both sides for the slant.
    block_h = gap * len(lines) + 16
    text_layer = ",".join(
        f"drawtext=fontfile='{font}':textfile='{_escape(file)}':fontsize={size}:fontcolor=white:"
        f"x=40:y={4 + i * gap}:shadowx=3:shadowy=3:shadowcolor=black@0.8"
        for i, file in enumerate(files)
    )
    # drawbox leaves alpha alone, so the badge is drawn opaque and padded after.
    badge_layer = (
        f"drawbox=x={badge_w}:y=0:w=14:h=70:color={_RED}:t=fill,"
        f"drawtext=fontfile='{font}':textfile='{_escape(badge_file)}':fontsize={badge_size}:"
        f"fontcolor=white:x=28:y=(70-text_h)/2:shadowx=2:shadowy=2:shadowcolor=black@0.5,"
        f"format=rgba,pad=w=iw+40:h=ih:x=20:y=0:color=black@0"
    )
    if art_path and Path(art_path).exists():
        inputs = ["-i", str(art_path)]
        picture = (f"[1]scale={inner_w}:{inner_h}:force_original_aspect_ratio=increase,"
                   f"crop={inner_w}:{inner_h}[pic]")
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c=0x15305e:s={inner_w}x{inner_h}"]
        picture = "[1]null[pic]"
    graph = ";".join([
        picture,
        f"color=c={_NAVY}:s={THUMB_WIDTH}x{THUMB_HEIGHT}[base]",
        f"[base][pic]overlay={_MARGIN}:{_MARGIN},"
        f"drawbox=x={_MARGIN}:y={_MARGIN}:w={inner_w}:h={inner_h}:color={_EDGE}@0.85:t=3,"
        f"drawbox=x={_MARGIN}:y={_BAND_TOP}:w={inner_w}:h={band_h}:color={_NAVY}@0.9:t=fill,"
        f"drawbox=x={_MARGIN}:y={_BAND_TOP}:w={inner_w}:h=5:color={_EDGE}:t=fill[framed]",
        # A faint dotted "world map" texture on the right of the band.
        f"color=c=black@0:s=520x{band_h - 10},format=rgba,"
        f"geq=r=110:g=170:b=255:a='if(lt(mod(X\\,9)\\,3)*lt(mod(Y\\,9)\\,3)\\,70*X/W*(1-abs(Y/H-0.5)*1.4)\\,0)'[dots]",
        f"[framed][dots]overlay={THUMB_WIDTH - _MARGIN - 520}:{_BAND_TOP + 6}[withdots]",
        # The red line under the headline, fading out to the right.
        f"color=c={_RED}:s=620x5,format=rgba,geq=r=227:g=25:b=43:a='255*(1-X/W)'[red]",
        f"[withdots][red]overlay={_TEXT_X}:{THUMB_HEIGHT - _MARGIN - 18}[lined]",
        f"color=c=black@0:s={_TEXT_LIMIT + 80}x{block_h},format=rgba,{text_layer},"
        f"shear=shx={_SLANT}:fillcolor=black@0,format=rgba[text]",
        f"color=c={_BLUE}:s={badge_w + 14}x70,{badge_layer},"
        f"shear=shx={_SLANT}:fillcolor=black@0,format=rgba[badge]",
        f"[lined][text]overlay={_TEXT_X - 40}:{text_top}[t1]",
        f"[t1][badge]overlay=24:{_MARGIN + 22}",
    ])
    subprocess.run(
        [settings.ffmpeg_bin, "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16", *inputs,
         "-filter_complex", graph, "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path
