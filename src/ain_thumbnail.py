"""Ain The Explainer thumbnail: the anchor on navy, the news picture, three headline bars.

Long videos: the anchor (fictional, data/presenter/anchor.jpg) on the left over
a navy panel with a dotted map, a slanted red-and-white edge, and the news
picture on the right. Shorts never show her: the picture fills the frame.
Top: an "Ain The Explainer" mark and a slanted red "WORLD NEWS" badge. The
headline is split over three slanted bars: white on red ("U.S. GOVERNMENT"),
big navy on white ("SHUTDOWN") and black on yellow ("WHAT IT MEANS FOR
AMERICANS").

The headline words come from the video's title only (news stays factual), and
a crime headline drops the people the script names: the anchor must never
stand next to a suspect's name (news_thumbnail._without_names). Used for
channels with "news_thumbnail": true; the older framed layout is the fallback.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import settings
from .models import Lesson
from .presenter import _escape, _text_width
from .thumbnails import SAFE_MARGIN, THUMB_HEIGHT, THUMB_WIDTH

FONT = settings.root / "fonts" / "ArchivoBlack-Regular.ttf"
ANCHOR_IMAGE = settings.root / "data" / "presenter" / "anchor.jpg"
_NAVY = "0x0b2d6e"
_RED = "0xd9141f"
_YELLOW = "0xffd21a"
_SLANT = 0.16
_EDGE_X = 470
# Head and shoulders out of the 1344x768 desk picture.
_ANCHOR_CROP = (500, 640, 430, 18)
_FRAMING = {"what", "it", "means", "for", "why", "how", "explained", "matters", "this", "is",
            "next", "comes", "what's", "now", "the", "a", "to", "of", "and", "in"}


def headline_parts(lesson: Lesson, crime: bool) -> tuple[str, str, str]:
    """(top, main, sub) for the three bars, words from the title only."""
    from .news_thumbnail import _without_names

    title = re.split(r"\s*\|\s*", str(lesson.title or ""))[0].strip()
    title_words = {w.lower().strip(".,:;!?'\"") for w in title.split()}
    top = main = sub = ""
    try:
        from .genre_topics import _script_json

        raw = _script_json(
            "Split this news headline for a 3-bar YouTube thumbnail: TOP (2-3 words of "
            "context), MAIN (1-2 words, the key word, shown biggest) and SUB (2-6 words; "
            "a framing like 'What It Means' or the rest of the headline, or empty). Use only "
            "words from the headline plus framing words; add no names, numbers or claims.\n"
            "Example: 'U.S. Government Shutdown: What It Means for Americans' -> "
            '{"top": "U.S. Government", "main": "Shutdown", "sub": "What It Means for Americans"}\n'
            f"Headline: {title}\n"
            'Return ONLY JSON: {"top": str, "main": str, "sub": str}'
        )
        got = [" ".join(str(raw.get(key) or "").split()) for key in ("top", "main", "sub")]

        def from_title(text: str, allowed: set[str]) -> bool:
            return all(w.lower().strip(".,:;!?'\"") in allowed for w in text.split())

        def in_order(text: str) -> bool:
            """The words read in the headline's own order, so no meaning changes."""
            remaining = iter(w.lower().strip(".,:;!?'\"") for w in title.split())
            return all(any(w == r for r in remaining)
                       for w in (x.lower().strip(".,:;!?'\"") for x in text.split()))

        sub_framing = bool(got[2]) and not from_title(got[2], title_words)
        ordered = f"{got[0]} {got[1]}" if sub_framing else " ".join(got)
        if got[1] and from_title(got[0], title_words) and from_title(got[1], title_words) \
                and from_title(got[2], title_words | _FRAMING) and len(got[1].split()) <= 2 \
                and in_order(ordered):
            top, main, sub = got
    except Exception as exc:
        print(f"[thumbnail] Ain headline split failed ({exc})")
    if not main:
        words = re.sub(r"[:\-–]", " ", title).split()
        cut = max(1, min(3, len(words) - 1))
        top, main = " ".join(words[:cut]), " ".join(words[cut:cut + 2])
        sub = " ".join(words[cut + 2:])
    if crime:
        top, main, sub = (_without_names(part, lesson) for part in (top, main, sub))
        if not main:
            main, top = top, ""
    return top.upper(), main.upper(), sub.upper()


def _bar(file: Path, text: str, size: int, colour: str, ink: str, pad: int) -> tuple[str, int, int]:
    """A slanted coloured bar with its text, as a filter chain ending in a label."""
    file.write_text(text, encoding="utf-8")
    width = _text_width(file, str(FONT), size) + 2 * pad
    height = int(size * 1.35)
    chain = (
        f"color=c={colour}:s={width}x{height},"
        f"drawtext=fontfile='{_escape(FONT)}':textfile='{_escape(file)}':fontsize={size}:"
        f"fontcolor={ink}:x={pad}:y=(h-text_h)/2,format=rgba,"
        f"pad=w=iw+{height}:h=ih:x={height // 2}:y=0:color=black@0,"
        f"shear=shx={_SLANT}:fillcolor=black@0,format=rgba"
    )
    return chain, width + height, height


def _fit(text: str, file: Path, limit: int, largest: int, smallest: int, pad: int) -> int:
    """The font size whose whole bar (text, padding, slant room) fits in `limit`."""
    file.write_text(text or " ", encoding="utf-8")
    per_point = _text_width(file, str(FONT), 100) / 100
    return max(smallest, min(largest, int((limit - 2 * pad) / (per_point + 1.35))))


def compose_ain_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                          channel=None, content_type: str = "short") -> Path:
    from .stock_video import crime_text

    output_path.parent.mkdir(parents=True, exist_ok=True)
    crime = bool(crime_text(str(lesson.title or "")))
    top, main, sub = headline_parts(lesson, crime)
    show_anchor = content_type == "long" and ANCHOR_IMAGE.is_file()
    text_x = 600 if show_anchor else 70
    limit = THUMB_WIDTH - text_x - 30
    stem = output_path.with_suffix("")
    bars = []
    if top:
        bars.append(_bar(Path(f"{stem}.ainTop.txt"), top,
                         _fit(top, Path(f"{stem}.ainTop.txt"), limit, 70, 34, 22), _RED, "white", 22))
    bars.append(_bar(Path(f"{stem}.ainMain.txt"), main,
                     _fit(main, Path(f"{stem}.ainMain.txt"), limit, 124, 48, 26), "white", _NAVY, 26))
    if sub:
        bars.append(_bar(Path(f"{stem}.ainSub.txt"), sub,
                         _fit(sub, Path(f"{stem}.ainSub.txt"), limit, 48, 26, 20), _YELLOW, "black", 20))
    total = sum(height for _chain, _w, height in bars) + 10 * (len(bars) - 1)
    y = THUMB_HEIGHT - SAFE_MARGIN - total

    inputs = ["-f", "lavfi", "-i", f"color=c={_NAVY}:s={THUMB_WIDTH}x{THUMB_HEIGHT}"]
    picture_index = 1
    if art_path and Path(art_path).exists():
        inputs += ["-i", str(art_path)]
    else:
        inputs += ["-f", "lavfi", "-i", "color=c=0x1d3f8a:s=64x36"]
    parts = [
        # Navy with a faint dotted world-map texture on the left.
        f"[0]format=rgba,geq=r=11:g=45:b=110:a=255[navy]",
        f"color=c=black@0:s=600x{THUMB_HEIGHT},format=rgba,"
        f"geq=r=90:g=150:b=255:a='if(lt(mod(X\\,10)\\,3)*lt(mod(Y\\,10)\\,3)\\,60*(1-X/W)\\,0)'[dots]",
        "[navy][dots]overlay=0:0[bg]",
    ]
    current = "bg"
    if show_anchor:
        w, h, x, ay = _ANCHOR_CROP
        inputs += ["-i", str(ANCHOR_IMAGE)]
        parts += [
            f"[2]crop={w}:{h}:{x}:{ay},scale=-2:{THUMB_HEIGHT}[anchor]",
            f"[{current}][anchor]overlay=-20:0[withanchor]",
        ]
        current = "withanchor"
        pic_w = THUMB_WIDTH - _EDGE_X
        # The picture's left edge leans, with a red and a white stripe along it.
        slope = 0.22
        parts += [
            f"[{picture_index}]scale={pic_w}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={pic_w}:{THUMB_HEIGHT},format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='if(gt(X\\,{slope}*(H-Y)+18)\\,255\\,0)'[pic]",
            f"color=c=black@0:s={pic_w}x{THUMB_HEIGHT},format=rgba,"
            f"geq=r='if(lt(X\\,{slope}*(H-Y)+10)\\,217\\,255)':g='if(lt(X\\,{slope}*(H-Y)+10)\\,20\\,255)':"
            f"b='if(lt(X\\,{slope}*(H-Y)+10)\\,31\\,255)':"
            f"a='if(between(X\\,{slope}*(H-Y)-6\\,{slope}*(H-Y)+18)\\,255\\,0)'[edge]",
            f"[{current}][edge]overlay={_EDGE_X}:0[edged]",
            f"[edged][pic]overlay={_EDGE_X}:0[withpic]",
        ]
    else:
        parts += [
            f"[{picture_index}]scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}[pic]",
            f"[{current}][pic]overlay=0:0[withpic]",
        ]
    current = "withpic"
    # Darken behind the bars so they read on a bright picture.
    parts.append(f"[{current}]drawbox=x={text_x - 40}:y={y - 30}:w={THUMB_WIDTH}:h={THUMB_HEIGHT}:"
                 f"color=black@0.25:t=fill[shaded]")
    current = "shaded"
    for index, (chain, width, height) in enumerate(bars):
        parts.append(f"{chain}[bar{index}]")
        parts.append(f"[{current}][bar{index}]overlay={text_x}:{y}[b{index}]")
        current = f"b{index}"
        y += height + 10

    # The mark ("Ain" over "The" and "Explainer") and the WORLD NEWS badge.
    # Right of the slanted edge (which leans 0.22 px per row: ~160 px at the top).
    mark_x = (_EDGE_X + 175) if show_anchor else 40
    for name, text in (("ainMark", "Ain"), ("ainThe", "The"), ("ainExp", "Explainer"), ("ainBadge", "WORLD NEWS")):
        Path(f"{stem}.{name}.txt").write_text(text, encoding="utf-8")
    ain_chain = (f"color=c=black@0:s=200x80,format=rgba,"
                 f"drawtext=fontfile='{_escape(FONT)}':textfile='{_escape(Path(f'{stem}.ainMark.txt'))}':"
                 f"fontsize=64:fontcolor=white:x=10:y=0:shadowx=3:shadowy=3:shadowcolor=black@0.6,"
                 f"shear=shx={_SLANT}:fillcolor=black@0,format=rgba")
    the_chain, the_w, the_h = _bar(Path(f"{stem}.ainThe.txt"), "The", 26, "white", _NAVY, 10)
    exp_chain, _exp_w, _exp_h = _bar(Path(f"{stem}.ainExp.txt"), "Explainer", 26, _RED, "white", 10)
    badge_chain, badge_w, _badge_h = _bar(Path(f"{stem}.ainBadge.txt"), "WORLD NEWS", 38, _RED, "white", 20)
    parts += [
        f"{ain_chain}[ain]", f"{the_chain}[the]", f"{exp_chain}[exp]", f"{badge_chain}[badge]",
        f"[{current}][ain]overlay={mark_x}:{SAFE_MARGIN - 6}[m1]",
        f"[m1][the]overlay={mark_x}:{SAFE_MARGIN + 68}[m2]",
        f"[m2][exp]overlay={mark_x + the_w - the_h // 2 - 6}:{SAFE_MARGIN + 68}[m3]",
        f"[m3][badge]overlay={THUMB_WIDTH - badge_w - 30}:{SAFE_MARGIN}",
    ]
    subprocess.run(
        [settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", ";".join(parts),
         "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path
