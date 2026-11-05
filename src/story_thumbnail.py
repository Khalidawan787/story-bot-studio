"""A biography channel's thumbnail: the hero's photo, a short name and one fact.

The owner's examples for World Valor (2026-11-05, data/brand/<id>/examples:
"Franklin D. Roosevelt, A Life of Service" and "The Untold Life of Napoleon"):

    the photo on the left, fading into a dark navy sky with grain and a warm glow;
    top left the channel's mark, top right a slanted crimson TRUE STORY badge;
    on the right, centred:   FRANKLIN D.            (ivory, Cinzel Bold)
                             ROOSEVELT              (antique gold, Cinzel Black,
                                                     dark edge and shadow)
                             [ 32ND US PRESIDENT ]  (white on a crimson ribbon)
                             NEW DEAL - WAR - ...   (gold, in a gold-edged pill)

The colours come from the channel's style notes (brand.palette: navy, steel
blue, gold, crimson, ivory), so changing them there changes the card. Switched
on per channel with "story_thumbnail": true in channels.json. The photo is the
subject's own free Wikipedia photo (article_photo.jpg); without one the art the
thumbnail was given is used. The words come from the script only.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import settings
from .models import Lesson
from .presenter import _escape, _text_width
from .thumbnails import SAFE_MARGIN, THUMB_HEIGHT, THUMB_WIDTH

FONTS = settings.root / "fonts"
TITLE = FONTS / "Cinzel-Black.ttf"
HEADING = FONTS / "Cinzel-Bold.ttf"
LABEL = FONTS / "Montserrat-ExtraBold.ttf"
# The owner's palette (style.md); brand.palette overrides these per channel.
DEFAULTS = {"bg": "0x0b1729", "bg2": "0x172c40", "title": "0xd9aa49", "accent": "0xa52125",
            "text": "0xf2e7cb"}
_PHOTO_W = 640
_CX = 958          # centre of the text column
_TEXT_LIMIT = 560
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


def colours(channel) -> dict:
    try:
        from .brand import palette

        return palette(channel.id, DEFAULTS) if channel is not None else dict(DEFAULTS)
    except Exception:
        return dict(DEFAULTS)


def themes(lesson: Lesson, run_dir: Path) -> list[str]:
    """Up to 3 short themes of the life ('NEW DEAL', 'WORLD WAR II'), each in the script."""
    cache = run_dir / "thumbnail_themes.txt"
    if cache.exists():
        return [line for line in cache.read_text(encoding="utf-8").splitlines() if line.strip()]
    script = " ".join(scene.line for scene in lesson.scenes)
    found: list[str] = []
    try:
        from .genre_topics import _script_json

        raw = _script_json(
            "For a history thumbnail, give 3 short themes of this person's story, 1-3 words "
            "each, the way a documentary poster lists them (e.g. 'New Deal', 'World War II', "
            "'Social Security' or 'Rise', 'Power', 'Downfall'). Every word must appear in the "
            "script. No new names, numbers or claims.\n"
            f"Title: {lesson.title}\nScript: {script[:3000]}\n"
            'Return ONLY JSON: {"themes": [str, str, str]}'
        )
        words_in_script = set(re.findall(r"[a-z0-9]+", script.lower()))
        for item in raw.get("themes") or []:
            item = " ".join(str(item).split()).strip(" .")
            words = re.findall(r"[a-z0-9]+", item.lower())
            if item and len(words) <= 3 and all(w in words_in_script for w in words):
                found.append(item.upper())
    except Exception as exc:
        print(f"[thumbnail] story themes failed ({exc})")
    # A theme that repeats the ribbon's fact adds nothing.
    _name, fact = name_and_fact(lesson, run_dir)
    found = [x for x in found if x.lower() not in fact.lower()][:3]
    if sum(len(x) for x in found) > 34:
        found = found[:2]
    cache.write_text("\n".join(found), encoding="utf-8")
    return found


def poster_words(lesson: Lesson, run_dir: Path) -> tuple[str, str, str, list[str]]:
    """(lead line, big name, ribbon fact, themes): 'FRANKLIN D.', 'ROOSEVELT', ..."""
    name, fact = name_and_fact(lesson, run_dir)
    subject = re.sub(r"\(.*?\)", "", _subject(lesson, run_dir)).strip()
    words = name.upper().split()
    big = words[-1] if words else "HERO"
    # The rest of the full name goes above ("FRANKLIN D."), else a story line.
    full = subject.upper().replace(",", " ").split()
    lead = ""
    if big in full:
        lead = " ".join(full[:full.index(big)])
    if not lead or len(lead) > 22:
        rest = " ".join(words[:-1])
        lead = rest if rest and len(rest) <= 22 else ""
    return lead or "THE LIFE OF", big, fact.upper(), themes(lesson, run_dir)


def _fit(text: str, file: Path, font: Path, limit: int, largest: int, smallest: int) -> int:
    file.write_text(text, encoding="utf-8")
    return max(smallest, min(largest, 100 * limit // max(1, _text_width(file, str(font), 100))))


def compose_story_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                            channel=None, content_type: str = "short") -> Path:
    run_dir = output_path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    c = colours(channel)
    lead, big, fact, theme_list = poster_words(lesson, run_dir)
    photo = run_dir / "article_photo.jpg"
    art = Path(art_path) if art_path and Path(art_path).exists() else None
    # Only one picture: the person's own photo, else the art. The sky behind
    # is drawn (a second picture showed a ghost copy of the face, or a stranger).
    picture = photo if photo.is_file() else art

    def tmp(name: str, text: str) -> Path:
        file = output_path.with_suffix(f".{name}.txt")
        file.write_text(text, encoding="utf-8")
        return file

    lead_file = tmp("lead", lead)
    lead_size = _fit(lead, lead_file, HEADING, _TEXT_LIMIT, 66, 36)
    big_file = tmp("big", big)
    big_size = _fit(big, big_file, TITLE, _TEXT_LIMIT, 150, 64)
    fact_file = tmp("fact", fact) if fact else None
    fact_size = _fit(fact, fact_file, LABEL, _TEXT_LIMIT - 70, 36, 22) if fact else 0
    theme_text = "  •  ".join(theme_list)
    theme_file = tmp("themes", theme_text) if theme_text else None
    theme_size = _fit(theme_text, theme_file, HEADING, _TEXT_LIMIT - 80, 28, 18) if theme_text else 0

    ribbon_h = fact_size + 34 if fact else 0
    pill_h = theme_size + 30 if theme_text else 0
    block = (int(lead_size * 1.15) + int(big_size * 1.02) + (22 + ribbon_h if fact else 0)
             + (26 + pill_h if theme_text else 0))
    # Centred between the badge row and the bottom margin.
    area_top, area_bottom = SAFE_MARGIN + 70, THUMB_HEIGHT - SAFE_MARGIN
    top = max(area_top, area_top + (area_bottom - area_top - block) // 2)
    y_lead = top
    y_big = y_lead + int(lead_size * 1.15)
    y_ribbon = y_big + int(big_size * 1.02) + 22
    y_pill = (y_ribbon + ribbon_h if fact else y_big + int(big_size * 1.02)) + 26

    def text(file: Path, font: Path, size: int, y: int, colour: str, extra: str = "") -> str:
        return (f"drawtext=fontfile='{_escape(font)}':textfile='{_escape(file)}':fontsize={size}:"
                f"x={_CX}-text_w/2:y={y}:fontcolor={colour}{extra}")

    def rgb(value: str) -> tuple[int, int, int]:
        value = value.replace("0x", "")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    (r1, g1, b1), (r2, g2, b2) = rgb(c["bg"]), rgb(c["bg2"])
    inputs = ["-f", "lavfi", "-i", f"color=c=black:s={THUMB_WIDTH}x{THUMB_HEIGHT}"]
    # Navy sky: steel blue high on the right, a warm gold glow low on the right.
    glow = "exp(-((X-1150)*(X-1150)+(Y-640)*(Y-640))/90000)"
    lift = "exp(-((X-980)*(X-980)+(Y-160)*(Y-160))/260000)"
    parts = [
        f"[0]format=rgb24,geq="
        f"r='{r1}+({r2}-{r1})*{lift}+120*{glow}':"
        f"g='{g1}+({g2}-{g1})*{lift}+70*{glow}':"
        f"b='{b1}+({b2}-{b1})*{lift}+10*{glow}'[sky0]",
    ]
    current = "sky0"
    if picture is not None:
        inputs += ["-i", str(picture)]
        parts += [
            # Keep the top of a portrait, where the face is; fade into the sky.
            f"[1]scale={_PHOTO_W}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={_PHOTO_W}:{THUMB_HEIGHT}:(iw-{_PHOTO_W})/2:(ih-{THUMB_HEIGHT})*0.12,"
            f"eq=contrast=1.12:saturation=0.92,format=rgba,"
            f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='255*min(1,max(0,({_PHOTO_W}-X)/230))'[ph]",
            f"[{current}][ph]overlay=0:0[withphoto]",
        ]
        current = "withphoto"
    # Grain and a soft vignette, like the examples.
    parts.append(f"[{current}]noise=alls=7:allf=t,vignette=PI/5[base]")
    current = "base"

    draw = []
    if fact:
        ribbon_w = _text_width(fact_file, str(LABEL), fact_size) + 70
        draw += [
            f"drawbox=x={_CX - ribbon_w // 2 + 5}:y={y_ribbon + 5}:w={ribbon_w}:h={ribbon_h}:color=black@0.45:t=fill",
            f"drawbox=x={_CX - ribbon_w // 2}:y={y_ribbon}:w={ribbon_w}:h={ribbon_h}:color={c['accent']}:t=fill",
            text(fact_file, LABEL, fact_size, y_ribbon + (ribbon_h - fact_size) // 2 - 2, "white"),
        ]
    if theme_text:
        pill_w = _text_width(theme_file, str(HEADING), theme_size) + 64
        x0 = _CX - pill_w // 2
        draw += [
            f"drawbox=x={x0}:y={y_pill}:w={pill_w}:h={pill_h}:color={c['bg']}@0.85:t=fill",
            f"drawbox=x={x0}:y={y_pill}:w={pill_w}:h={pill_h}:color={c['title']}:t=3",
            text(theme_file, HEADING, theme_size, y_pill + (pill_h - theme_size) // 2 - 1, c["title"]),
        ]
    draw += [
        text(lead_file, HEADING, lead_size, y_lead, c["text"],
             ":borderw=2:bordercolor=black@0.7:shadowx=2:shadowy=3:shadowcolor=black@0.7"),
        text(big_file, TITLE, big_size, y_big, c["title"],
             ":borderw=4:bordercolor=0x1a0f05:shadowx=5:shadowy=6:shadowcolor=black@0.75"),
    ]
    # Top left: the channel's mark on a dark plate, with a gold rule.
    mark = str(getattr(channel, "name", "") or "").upper()[:28]
    if mark:
        mark_file = tmp("mark", mark)
        mark_w = _text_width(mark_file, str(LABEL), 20)
        draw += [
            f"drawbox=x=30:y={SAFE_MARGIN}:w={mark_w + 36}:h=46:color=black@0.5:t=fill",
            f"drawbox=x=30:y={SAFE_MARGIN + 42}:w={mark_w + 36}:h=4:color={c['title']}:t=fill",
            f"drawtext=fontfile='{_escape(LABEL)}':textfile='{_escape(mark_file)}':fontsize=20:"
            f"x=48:y={SAFE_MARGIN + 11}:fontcolor={c['text']}",
        ]
    parts.append(f"[{current}]{','.join(draw)}[texted]")
    badge_file = tmp("badge", "TRUE STORY")
    badge_w = _text_width(badge_file, str(LABEL), 26) + 44
    parts.append(
        f"color=c={c['accent']}:s={badge_w}x52,"
        f"drawtext=fontfile='{_escape(LABEL)}':textfile='{_escape(badge_file)}':fontsize=26:fontcolor=white:"
        f"x=22:y=(52-text_h)/2,format=rgba,pad=w=iw+24:h=ih:x=12:y=0:color=black@0,"
        f"shear=shx=-0.22:fillcolor=black@0,format=rgba[badge]"
    )
    parts.append(f"[texted][badge]overlay={THUMB_WIDTH - badge_w - 70}:{SAFE_MARGIN}")
    subprocess.run(
        [settings.ffmpeg_bin, "-y", *inputs, "-filter_complex", ";".join(parts),
         "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path
