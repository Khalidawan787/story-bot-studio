"""Upright (1080x1920) thumbnails for Shorts, in each channel's own style.

YouTube shows a Short's thumbnail as a tall 9:16 card, so a 16:9 thumbnail lost
both sides: on 3 Nov 2026 "MICHAEL JORDAN" showed as "EL N" and its text looked
huge. A Short now gets an upright card with the channel's badge at the top, its
picture filling the card, and the headline in the channel's colours and fonts,
small enough to fit whole (the owner's request). The words are the ones the
16:9 layout uses (story_thumbnail.name_and_fact, crime_thumbnail.split_headline,
motivation_thumbnail.slogan, ain_thumbnail.headline_parts, ...), so the facts
rules stay the same. The same empty space is kept at the top and the bottom.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import settings
from .models import Lesson
from .presenter import _escape, _text_width

WIDTH, HEIGHT = 1080, 1920
# The channel's Shorts tab shows the card cropped to 2:3: 150 px off the top
# and the bottom of the 1920. On 3 Nov the CRIME FILES badge and the yellow
# name were cut there, so everything sits inside that band, 80 px in.
MARGIN = 150 + 80    # top badge and last line
TEXT_LIMIT = 900     # widest line, leaving room on both sides
FONTS = settings.root / "fonts"
SERIF = FONTS / "DMSerifDisplay-Regular.ttf"
ANTON = FONTS / "Anton-Regular.ttf"
BLACK = FONTS / "ArchivoBlack-Regular.ttf"


def style_of(channel) -> str | None:
    for flag, name in (("story_thumbnail", "story"), ("crime_thumbnail", "crime"),
                       ("motivation_thumbnail", "motivation"), ("trend_thumbnail", "trend"),
                       ("news_thumbnail", "ain"), ("sleep_thumbnail", "sleep")):
        if getattr(channel, flag, False):
            return name
    return None


def _wrap(text: str, font: Path, size: int, limit: int) -> list[str]:
    """Words into lines no wider than `limit` at `size`."""
    probe = Path(settings.runs_dir) / "_short_thumb_probe.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        probe.write_text(trial, encoding="utf-8")
        if current and _text_width(probe, str(font), size) > limit:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _row(text: str, font: Path, size: int, colour: str, *, bar: str = "", ink: str = "",
         max_lines: int = 3, edge: str = "", gap: int = 18, outline: str = "",
         centred: bool = False) -> dict:
    """One headline part: wrapped and shrunk until it fits in `max_lines`."""
    text = " ".join(str(text or "").split())
    while True:
        lines = _wrap(text, font, size, TEXT_LIMIT - (60 if bar else 0))
        if len(lines) <= max_lines or size <= 40:
            break
        size -= 6
    return {"lines": lines, "font": font, "size": size, "colour": ink or colour, "bar": bar,
            "edge": edge, "gap": gap, "outline": outline, "centred": centred}


def _rows_for(style: str, lesson: Lesson, run_dir: Path, channel=None) -> tuple[list[dict], dict]:
    """The headline rows and the card's look (badge, frame, where the text sits)."""
    if style == "story":
        # The poster of the wide card, upright: same words, fonts and colours.
        from .story_thumbnail import HEADING, LABEL, TITLE, colours, poster_words

        c = colours(channel)
        lead, big, fact, theme_list = poster_words(lesson, run_dir)
        rows = [_row(lead, HEADING, 76, c["text"], gap=6),
                _row(big, TITLE, 170, c["title"], max_lines=1, edge="0x1a0f05", gap=26)]
        if fact:
            rows.append(_row(fact, LABEL, 50, "", bar=c["accent"], ink="white", max_lines=2, gap=26,
                             centred=True))
        # The themes on one line (never a theme cut in two), in the wide card's
        # gold-edged navy box.
        while len(theme_list) > 1 and len(_wrap("  \u2022  ".join(theme_list), HEADING, 40, TEXT_LIMIT - 60)) > 1:
            theme_list = theme_list[:-1]
        if theme_list:
            rows.append(_row("  \u2022  ".join(theme_list), HEADING, 40, "", bar=c["bg"], ink=c["title"],
                             max_lines=1, outline=c["title"], centred=True))
        return rows, {"badge": ("TRUE STORY", c["accent"], "white", LABEL), "shade": c["bg"]}
    if style == "crime":
        from .crime_thumbnail import _subject, split_headline

        white, yellow = split_headline(lesson, _subject(run_dir))
        rows = [_row(" ".join(white), ANTON, 104, "white", max_lines=2, gap=12)] if white else []
        rows.append(_row(yellow, ANTON, 120, "", bar="0xffd21a", ink="black", max_lines=3))
        return rows, {"badge": ("CRIME FILES", "0xe0141e", "white", ANTON),
                      "mark": "CRIME CHRONICLES", "shade": "0x2a0606"}
    if style == "motivation":
        from .motivation_thumbnail import slogan

        return ([_row(slogan(lesson, run_dir), BLACK, 118, "white", max_lines=4, edge="0xff8a00")],
                {"badge": ("DAILY MOTIVATION", "0xf4b329", "0x111111", BLACK), "frame": "0xf4b329",
                 "shade": "0x100a04"})
    if style == "trend":
        from .trend_thumbnail import _headline

        return ([_row(_headline(lesson), BLACK, 78, "white", max_lines=4)],
                {"badge": ("WORLD TRENDING", "0x1f6fe5", "white", BLACK), "frame": "0x4aa3ff",
                 "band": "0x07122b", "shade": "0x07122b"})
    if style == "ain":
        from .ain_thumbnail import headline_parts
        from .stock_video import crime_text

        top, main, sub = headline_parts(lesson, bool(crime_text(str(lesson.title or ""))))
        rows = [_row(top, BLACK, 64, "", bar="0xd9141f", ink="white", max_lines=2)] if top else []
        rows.append(_row(main, BLACK, 104, "", bar="white", ink="0x0b2d6e", max_lines=2))
        if sub:
            rows.append(_row(sub, BLACK, 50, "", bar="0xffd21a", ink="black", max_lines=2))
        return rows, {"badge": ("WORLD NEWS", "0xd9141f", "white", BLACK),
                      "mark": "Ain The Explainer", "shade": "0x0b2d6e"}
    from .sleep_thumbnail import title_parts

    main, sub = title_parts(lesson)
    return ([_row(main, SERIF, 104, "0xf8ebc8", max_lines=4, edge="0x7a5310", gap=8),
             _row(sub, SERIF, 56, "0xffc35a", max_lines=2)],
            {"top": True, "shade": "0x10142e"})


def compose_short_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                            channel=None) -> Path:
    style = style_of(channel)
    if not style:
        raise RuntimeError("this channel has no thumbnail style")
    run_dir = output_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    rows, look = _rows_for(style, lesson, run_dir, channel)
    rows = [row for row in rows if row["lines"]]

    # Heights, then the block sits on the bottom margin (or under the top one).
    blocks = []
    for row in rows:
        line_h = int(row["size"] * 1.18)
        pad = 22 if row["bar"] else 0
        blocks.append((row, line_h, len(row["lines"]) * line_h + 2 * pad, pad))
    total = sum(height for _r, _l, height, _p in blocks) + sum(r["gap"] for r, *_ in blocks[:-1])
    if look.get("top"):
        y = MARGIN
    else:
        y = HEIGHT - MARGIN - total

    photo = run_dir / "article_photo.jpg"
    picture = photo if photo.is_file() else (Path(art_path) if art_path and Path(art_path).exists() else None)
    if picture is None and style == "crime":
        from .crime_thumbnail import backdrop

        picture = backdrop(run_dir.name)
    inputs = ["-i", str(picture)] if picture else ["-f", "lavfi", "-i", f"color=c={look['shade']}:s=64x64"]
    shade = look["shade"].replace("0x", "")
    r, g, b = (int(shade[i:i + 2], 16) for i in (0, 2, 4))
    # Darken towards the text so it reads on any picture.
    fade = (f"200*min(1\\,max(0\\,(Y-{y - 260})/420))" if not look.get("top")
            else f"190*min(1\\,max(0\\,({y + total + 220}-Y)/420))")
    parts = [
        f"[0]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT}:(iw-{WIDTH})/2:(ih-{HEIGHT})*0.2,setsar=1[pic]",
        f"color=c=black:s={WIDTH}x{HEIGHT},format=rgba,geq=r={r}:g={g}:b={b}:a='{fade}'[shade]",
        "[pic][shade]overlay=0:0",
    ]
    draw: list[str] = []
    if look.get("band"):
        draw.append(f"drawbox=x=0:y={y - 50}:w={WIDTH}:h={HEIGHT - y + 50}:color={look['band']}@0.92:t=fill")
        draw.append(f"drawbox=x=0:y={y - 50}:w={WIDTH}:h=8:color={look.get('frame', 'white')}:t=fill")
    files = []
    for index, (row, line_h, height, pad) in enumerate(blocks):
        if row["bar"]:
            widest = max(_text_width(_write(output_path, f"w{index}_{i}", line), str(row["font"]), row["size"])
                         for i, line in enumerate(row["lines"]))
            bar_w = widest + 60
            draw.append(f"drawbox=x={(WIDTH - bar_w) // 2}:y={y}:w={bar_w}:h={height}:color={row['bar']}:t=fill")
            if row.get("outline"):
                draw.append(f"drawbox=x={(WIDTH - bar_w) // 2}:y={y}:w={bar_w}:h={height}:color={row['outline']}:t=5")
        for i, line in enumerate(row["lines"]):
            file = _write(output_path, f"r{index}_{i}", line)
            files.append(file)
            edge = f":borderw=6:bordercolor={row['edge']}" if row["edge"] else ""
            shadow = "" if row["bar"] else ":shadowx=3:shadowy=4:shadowcolor=black@0.8"
            # Centred on the letters' own height: the same space above and below.
            top_y = (f"{y + pad + i * line_h}+({line_h}-text_h)/2" if row.get("centred")
                     else y + pad + i * line_h + int(row['size'] * 0.06))
            draw.append(
                f"drawtext=fontfile='{_escape(row['font'])}':textfile='{_escape(file)}':"
                f"fontsize={row['size']}:fontcolor={row['colour']}{edge}{shadow}:"
                f"x=(w-text_w)/2:y={top_y}"
            )
        if look.get("rule") and index == 0:
            draw.append(f"drawbox=x={(WIDTH - 140) // 2}:y={y + height + 2}:w=140:h=7:color={look['rule']}:t=fill")
        y += height + row["gap"]
    if look.get("badge"):
        text, colour, ink, font = look["badge"]
        file = _write(output_path, "badge", text)
        draw.append(
            f"drawtext=fontfile='{_escape(font)}':textfile='{_escape(file)}':fontsize=46:fontcolor={ink}:"
            f"box=1:boxcolor={colour}:boxborderw=22:x=(w-text_w)/2:y={MARGIN + 22}"
        )
    if look.get("mark"):
        file = _write(output_path, "mark", look["mark"])
        draw.append(
            f"drawtext=fontfile='{_escape(BLACK)}':textfile='{_escape(file)}':fontsize=34:fontcolor=white:"
            f"shadowx=2:shadowy=2:shadowcolor=black@0.8:x=(w-text_w)/2:y={MARGIN + 140}"
        )
    if look.get("frame"):
        draw.append(f"drawbox=x=18:y=18:w={WIDTH - 36}:h={HEIGHT - 36}:color={look['frame']}:t=6")
    parts[-1] += "," + ",".join(draw) if draw else ""
    subprocess.run(
        [settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", ";".join(parts),
         "-frames:v", "1", "-update", "1", "-q:v", "3", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path


def _write(output_path: Path, name: str, text: str) -> Path:
    file = output_path.with_suffix(f".short_{name}.txt")
    file.write_text(re.sub(r"\s+", " ", text).strip(), encoding="utf-8")
    return file
