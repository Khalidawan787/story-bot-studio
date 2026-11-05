"""Crime Chronicles thumbnail: the case's own photo, a red-black backdrop, a yellow name.

The subject's own free Wikipedia photo fills the left and fades into a dark red
backdrop. Top left a "CRIME CHRONICLES" mark in red corner brackets, top right a
slanted red "CRIME FILES" badge. On the right the headline in white condensed
capitals, with the person's name (or the last words) in black on a tilted
yellow brush stripe.

Only the subject's own photo is used (pipeline.verified_thumbnail saves it as
article_photo.jpg): never a stranger next to a criminal's name. Without one,
the given art (stock with no people for crime lines) is the backdrop.
Switched on per channel with "crime_thumbnail": true in channels.json.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import settings
from .models import Lesson
from .presenter import _escape, _text_width
from .thumbnails import SAFE_MARGIN, THUMB_HEIGHT, THUMB_WIDTH

FONT = settings.root / "fonts" / "Anton-Regular.ttf"
_RED = "0xe0141e"
_YELLOW = "0xffd21a"
_TEXT_X = 610
_TEXT_LIMIT = 620
_PHOTO_W = 700
# No-people crime pictures (police tape, prison bars, evidence, police lights;
# made once with Cloudflare FLUX) for a case with no photo and no art.
BACKDROPS = settings.root / "data" / "thumbnail_backdrops" / "crime"


def backdrop(seed: str) -> Path | None:
    import zlib

    pictures = sorted(BACKDROPS.glob("*.jpg"))
    return pictures[zlib.crc32(seed.encode("utf-8")) % len(pictures)] if pictures else None


def _subject(run_dir: Path) -> str:
    from .stock_video import SUBJECT_FILE

    try:
        return (run_dir / SUBJECT_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def split_headline(lesson: Lesson, subject: str) -> tuple[list[str], str]:
    """(white lines, yellow part): "THE PONZI SCHEME OF" + "BERNIE MADOFF"."""
    title = re.split(r"\s*\|\s*", str(lesson.title or ""))[0].strip().upper()
    title = re.sub(r"\s+", " ", title)
    if len(title) > 60:
        # Cut at a word, never inside one ("A CENTENNIAL MYSTER").
        title = title[:61].rsplit(" ", 1)[0].rstrip(" :,-")
    name = re.sub(r"\(.*?\)", "", subject).strip().upper()
    words = title.split()
    highlight = ""
    # The person's name as the title writes it (full, or the surname alone).
    for candidate in (name, " ".join(name.split()[-2:]), name.split()[-1] if name else ""):
        if candidate and len(candidate) > 2 and candidate in title:
            highlight = candidate
            break
    if highlight:
        rest = " ".join(title.replace(highlight, " ").split()).strip(" :-")
    else:
        # No name in it: the last word, or two when the last is short.
        cut = len(words) - (2 if len(words) > 2 and len(words[-1]) <= 4 else 1)
        rest, highlight = " ".join(words[:max(cut, 0)]), " ".join(words[max(cut, 0):])
    if rest in {"THE", "A", "AN"}:
        # "THE" alone on a white line looks lost; it joins the yellow part.
        rest, highlight = "", f"{rest} {highlight}"
    rest_words = rest.split()
    if len(rest) > 16 and len(rest_words) > 1:
        half = (len(rest_words) + 1) // 2
        white = [" ".join(rest_words[:half]), " ".join(rest_words[half:])]
    else:
        white = [rest] if rest else []
    return white, highlight


def _fit(text: str, file: Path, limit: int, largest: int, smallest: int) -> int:
    file.write_text(text, encoding="utf-8")
    return max(smallest, min(largest, 100 * limit // _text_width(file, str(FONT), 100)))


def compose_crime_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                            channel=None, content_type: str = "short") -> Path:
    run_dir = output_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    font = _escape(FONT)
    white, yellow = split_headline(lesson, _subject(run_dir))
    yellow_lines = yellow.split() if len(yellow) > 11 and len(yellow.split()) > 1 else [yellow]
    if len(yellow_lines) > 2:
        half = (len(yellow_lines) + 1) // 2
        yellow_lines = [" ".join(yellow_lines[:half]), " ".join(yellow_lines[half:])]
    w_files = [output_path.with_suffix(f".cw{i}.txt") for i in range(len(white))]
    y_files = [output_path.with_suffix(f".cy{i}.txt") for i in range(len(yellow_lines))]
    w_size = min([_fit(t, f, _TEXT_LIMIT, 100, 52) for t, f in zip(white, w_files)] or [100])
    y_size = min(_fit(t, f, _TEXT_LIMIT - 60, 118, 60) for t, f in zip(yellow_lines, y_files))
    import math

    stripe_h = len(yellow_lines) * int(y_size * 1.08) + 26
    stripe_w = max(_text_width(f, str(FONT), y_size) for f in y_files) + 70
    tilt = math.radians(2.2)
    stripe_drawn_h = int(stripe_h * math.cos(tilt) + stripe_w * math.sin(tilt)) + 2
    # The block (white lines, then the tilted stripe) ends at the bottom
    # margin, as far from the bottom as the logo and badge are from the top.
    block = len(white) * int(w_size * 1.05) + stripe_drawn_h
    top = max(SAFE_MARGIN + 100 + 14, THUMB_HEIGHT - SAFE_MARGIN - block)

    photo = run_dir / "article_photo.jpg"
    has_photo = photo.is_file()
    if not has_photo and not (art_path and Path(art_path).exists()):
        art_path = backdrop(run_dir.name)
    inputs = ["-f", "lavfi", "-i", f"color=c=0x0b0707:s={THUMB_WIDTH}x{THUMB_HEIGHT}"]
    parts = [
        # Dark backdrop with a red glow on the right.
        f"[0]format=rgb24,geq=r='18+150*exp(-((X-1000)*(X-1000)+(Y-420)*(Y-420))/160000)':"
        f"g='8+6*exp(-((X-1000)*(X-1000))/200000)':b='9+8*exp(-((X-1000)*(X-1000))/200000)'[bg]",
    ]
    current = "bg"
    if has_photo:
        inputs += ["-i", str(photo)]
        parts += [
            f"[1]scale={_PHOTO_W}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={_PHOTO_W}:{THUMB_HEIGHT}:(iw-{_PHOTO_W})/2:(ih-{THUMB_HEIGHT})*0.12,"
            f"eq=contrast=1.08:saturation=0.9,format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*min(1,max(0,({_PHOTO_W}-X)/220))'[ph]",
            f"[{current}][ph]overlay=0:0[withphoto]",
        ]
        current = "withphoto"
    elif art_path and Path(art_path).exists():
        inputs += ["-i", str(art_path)]
        parts += [
            f"[1]scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={THUMB_WIDTH}:{THUMB_HEIGHT},eq=brightness=-0.18:saturation=0.8,format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*min(1,max(0.25,(900-X)/500))'[art]",
            f"[{current}][art]overlay=0:0[withphoto]",
        ]
        current = "withphoto"

    # The yellow brush stripe behind the name, tilted a little.
    y_top = top + len(white) * int(w_size * 1.05) + 14
    parts.append(
        f"color=c={_YELLOW}:s={stripe_w}x{stripe_h},format=rgba,"
        f"geq=r=255:g=210:b=26:a='255*(1-0.35*lt(mod(X*7+Y*3\\,53)\\,4)*lt(Y\\,10))*"
        f"(1-0.6*lt(mod(X*5\\,41)\\,6)*gt(Y\\,H-9))',"
        f"rotate=-2.2*PI/180:c=none:ow=rotw(-2.2*PI/180):oh=roth(-2.2*PI/180)[stripe]"
    )
    parts.append(f"[{current}][stripe]overlay={_TEXT_X - 30}:{y_top - 14}[striped]")
    current = "striped"

    def text(file: Path, size: int, x: int, y: int, colour: str, shadow: str) -> str:
        return (f"drawtext=fontfile='{font}':textfile='{_escape(file)}':fontsize={size}:x={x}:y={y}:"
                f"fontcolor={colour}:shadowx=4:shadowy=4:shadowcolor={shadow}")

    mark = [output_path.with_suffix(".cm0.txt"), output_path.with_suffix(".cm1.txt"),
            output_path.with_suffix(".cbadge.txt")]
    mark[0].write_text("CRIME", encoding="utf-8")
    mark[1].write_text("CHRONICLES", encoding="utf-8")
    mark[2].write_text("CRIME FILES", encoding="utf-8")
    right = 48 + _text_width(mark[1], str(FONT), 32) + 14
    m = SAFE_MARGIN + 8
    drawn = [
        # A dark plate keeps the mark readable on a light photo.
        f"drawbox=x=14:y={SAFE_MARGIN}:w={right}:h=100:color=black@0.55:t=fill",
        # Red corner brackets round the mark, like a case-file stamp.
        *(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={_RED}:t=fill" for x, y, w, h in (
            (24, m, 34, 5), (24, m, 5, 34), (24, m + 79, 34, 5), (24, m + 50, 5, 34),
            (right - 34, m, 34, 5), (right - 5, m, 5, 34), (right - 34, m + 79, 34, 5),
            (right - 5, m + 50, 5, 34))),
        text(mark[0], 40, 48, m + 4, "white", "black@0.6"),
        text(mark[1], 32, 48, m + 44, _RED, "black@0.6"),
        *(text(f, w_size, _TEXT_X, top + i * int(w_size * 1.05), "white", "black@0.85")
          for i, f in enumerate(w_files)),
        *(text(f, y_size, _TEXT_X + 6, y_top + i * int(y_size * 1.08), "black", "black@0")
          for i, f in enumerate(y_files)),
    ]
    parts.append(f"[{current}]{','.join(drawn)}[texted]")
    badge_w = _text_width(mark[2], str(FONT), 46) + 50
    parts.append(
        f"color=c={_RED}:s={badge_w}x66,"
        f"drawtext=fontfile='{font}':textfile='{_escape(mark[2])}':fontsize=46:fontcolor=white:"
        f"x=25:y=(66-text_h)/2,format=rgba,pad=w=iw+30:h=ih:x=15:y=0:color=black@0,"
        f"shear=shx=0.2:fillcolor=black@0,format=rgba[badge]"
    )
    parts.append(f"[texted][badge]overlay={THUMB_WIDTH - badge_w - 60}:{SAFE_MARGIN}")
    subprocess.run(
        [settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", ";".join(parts),
         "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path
