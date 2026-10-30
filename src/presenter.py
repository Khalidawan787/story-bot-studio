"""A fixed AI news presenter: on the thumbnail, and in a corner of the video.

She is one fictional woman, generated once with Cloudflare FLUX on a green
screen and keyed out with ffmpeg (data/presenter/presenter_source.jpg ->
presenter.png, chromakey=0x0bb14a:0.12:0.05 then despill). Using the same picture everywhere is what gives a channel the
"same face on every thumbnail" look of the big commentary channels, without a
real person's likeness and without any paid avatar service.

Switched on per channel with "presenter": true in channels.json. Every step is
non-fatal: a presenter problem falls back to the ordinary thumbnail, or leaves
the video without her, and never costs a finished upload.
"""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from .config import settings
from .models import Lesson

# Under data/, not assets/: assets/ is git-ignored and GitHub Actions needs her.
PRESENTER_PNG = settings.root / "data" / "presenter" / "presenter.png"
HEADLINE_FONT = settings.root / "fonts" / "Anton-Regular.ttf"
# presenter.png is 768x1344; this crop is head, hand and shoulders, which is
# what stays readable when she is small in a corner of the video.
_BUST_CROP = (768, 820)
_GLOW = "0x2ad4ff"
_TEXT_RED = "0xff1f1f"
THUMB_WIDTH, THUMB_HEIGHT = 1280, 720
# The headline may use the full width minus its black outline on both sides.
_HEADLINE_WIDTH = 1190
# Seconds of the end card (video.add_end_card) she stays off.
_END_CARD_SECONDS = 4.0


def presenter_enabled(channel) -> bool:
    return bool(getattr(channel, "presenter", False)) and PRESENTER_PNG.is_file()


def _ffmpeg() -> str:
    return settings.ffmpeg_bin


def _escape(path: Path | str) -> str:
    return str(path).replace("\\", "/").replace(":", "\\:")


def _font() -> str:
    if HEADLINE_FONT.is_file():
        return str(HEADLINE_FONT)
    from .visuals import font_path

    return font_path()


# ------------------------------------------------------------------ headline

def presenter_headline(lesson: Lesson, channel=None, content_type: str = "short") -> str:
    """Two to four words and a question mark: "EMERALD HEIRESS ?".

    "The Curious Case of the Clockwork Killer" -> "CLOCKWORK KILLER ?": the
    words after the last "of" are the subject, the words before it are filler.
    """
    from .thumbnails import thumbnail_headline

    text = thumbnail_headline(lesson, channel, content_type, max_words=12)
    text = re.split(r"[:—]", text)[0].strip(" ?!.")
    tail = re.split(r"\bOF(?: THE| A| AN)?\b", text)[-1].strip()
    if tail != text and 1 <= len(tail.split()) <= 3:
        text = tail
    text = re.sub(r"^(THE|A|AN)\s+", "", text)
    text = " ".join(text.split()[:4])
    return f"{text} ?" if text else "?"


def _text_width(text_file: Path, font: str, size: int) -> int:
    """Rendered width in pixels, measured by ffmpeg itself (bbox filter).

    Anton is a condensed face, so the Arial width table in thumbnails.py would
    leave the line far too small.
    """
    result = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-f", "lavfi", "-i", "color=c=black:s=4000x400",
         "-vf", f"drawtext=fontfile='{_escape(font)}':textfile='{_escape(text_file)}':"
                f"fontsize={size}:fontcolor=white:x=20:y=20,bbox",
         "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    spans = [int(b) - int(a) for a, b in re.findall(r"x1:(\d+) x2:(\d+)", result.stderr)]
    if not spans:
        raise RuntimeError("could not measure the headline")
    return max(spans) + 1


# ------------------------------------------------------------------ artwork

_SCENE_MOOD = {
    "crime": "flashing red and blue police lights, crime-scene tape, rain-soaked street",
    "motivation": "a lone figure on a mountain ridge against a blazing sunrise, lightning in the distance",
}


def presenter_art_prompt(lesson: Lesson, channel=None, content_type: str = "short") -> str:
    from .thumbnails import thumbnail_headline

    genre = str(getattr(channel, "genre", "") or "").lower()
    subject = thumbnail_headline(lesson, channel, content_type, max_words=10).title()
    highlights = ", ".join(scene.label for scene in lesson.scenes[:3])
    mood = _SCENE_MOOD.get(genre, "dramatic storm clouds and glowing embers")
    # "Poster" or a quoted title makes FLUX letter the title onto the picture,
    # misspelled, so the topic is given as a theme and text is ruled out twice.
    return (
        f"Dramatic cinematic film still, wide shot. Theme: {subject.lower()}; {highlights.lower()}. "
        f"{mood}, fire glow, smoke and embers, dark storm clouds, intense orange and teal "
        "color grading, hyper detailed, high contrast. Main subject in the centre and "
        "right. No close-up faces, no bodies, no victims, no gore. Absolutely no text, "
        "no lettering, no title, no signs, no logo, no watermark."
    )


def generate_presenter_art(lesson: Lesson, output_path: Path, channel=None,
                           content_type: str = "short") -> Path | None:
    """Free Cloudflare FLUX artwork, or None so the normal providers are tried."""
    from .image_assets import cloudflare_image_ready, generate_cloudflare_image

    if not cloudflare_image_ready():
        return None
    try:
        return generate_cloudflare_image(
            presenter_art_prompt(lesson, channel, content_type), output_path, landscape=True,
        )
    except Exception as exc:
        print(f"[presenter] Cloudflare artwork failed: {exc}")
        return None


# ----------------------------------------------------------------- thumbnail

def compose_presenter_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                                channel=None, content_type: str = "short") -> Path:
    """Artwork, the presenter on the left with a cyan glow, and a red headline."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = _font()
    text_file = output_path.with_suffix(".headline.txt")
    text_file.write_text(presenter_headline(lesson, channel, content_type), encoding="utf-8")
    size = max(80, min(170, int(170 * _HEADLINE_WIDTH / _text_width(text_file, font, 170))))
    text = (
        f"drawtext=fontfile='{_escape(font)}':textfile='{_escape(text_file)}':fontsize={size}:"
        f"x=(w-text_w)/2:y=h-text_h-40"
    )

    if art_path and Path(art_path).exists():
        background = ["-i", str(art_path)]
    else:
        background = ["-f", "lavfi", "-i", f"color=c=0x3a0a0a:s={THUMB_WIDTH}x{THUMB_HEIGHT}"]
    # She fills the frame height, cropped at the waist, slightly off the left edge.
    width = 536
    graph = ";".join([
        f"[0]scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT},eq=saturation=1.25:contrast=1.12,vignette=PI/5[bg]",
        f"[1]scale={width}:938,crop={width}:{THUMB_HEIGHT}:0:20,split[p][pa]",
        "[pa]alphaextract,boxblur=14:2,lutyuv=y='min(val*2.2\\,255)'[ga]",
        f"color=c={_GLOW}:s={width}x{THUMB_HEIGHT}[gc]",
        "[gc][ga]alphamerge[glow]",
        "[bg][glow]overlay=x=-6:y=0[b1]",
        "[b1][p]overlay=x=-6:y=0[b2]",
        # Darken towards the bottom so the headline reads on any artwork.
        f"[b2]drawbox=x=0:y={THUMB_HEIGHT - 230}:w={THUMB_WIDTH}:h=230:color=black@0.18:t=fill,"
        f"drawbox=x=0:y={THUMB_HEIGHT - 150}:w={THUMB_WIDTH}:h=150:color=black@0.22:t=fill,"
        # A fat black outline first, then the red fill with a white edge.
        f"{text}:fontcolor=black:borderw=22:bordercolor=black:shadowx=6:shadowy=8:shadowcolor=black@0.6,"
        f"{text}:fontcolor={_TEXT_RED}:borderw=9:bordercolor=white",
    ])
    subprocess.run(
        [_ffmpeg(), "-y", *background, "-i", str(PRESENTER_PNG),
         "-filter_complex", graph, "-frames:v", "1", "-update", "1", "-q:v", "2", str(output_path)],
        check=True, capture_output=True, text=True,
    )
    return output_path


# --------------------------------------------------------------------- video

def add_presenter(video_path: Path, run_dir: Path, channel=None, content_type: str = "short") -> Path:
    """Overlay her in a corner, gently bobbing, for the whole video but the end card.

    Long videos: bottom right, standing on the frame edge like a news inset.
    Shorts: top left, because the bottom and the right are covered by YouTube's
    own title and buttons, and the captions sit in the lower middle.
    """
    from .video import _audio_duration, _dimensions, _video_encode_args

    width, height = _dimensions(content_type)
    duration = _audio_duration(video_path)
    if content_type == "long":
        ph = 400
        pw = int(round(_BUST_CROP[0] * ph / _BUST_CROP[1] / 2) * 2)
        x, y = width - pw - 40, height - ph
    else:
        ph = 520
        pw = int(round(_BUST_CROP[0] * ph / _BUST_CROP[1] / 2) * 2)
        x, y = 24, 210
    visible_until = max(1.0, duration - _END_CARD_SECONDS)
    fade_out = max(0.6, visible_until - 0.5)
    # A slow 6 px rise and fall: enough to read as alive, not as shaking.
    bob = f"{y}+6*sin(2*{math.pi:.5f}*t/3.4)"
    fades = f"fade=t=in:st=0.4:d=0.6:alpha=1,fade=t=out:st={fade_out:.2f}:d=0.5:alpha=1"
    graph = ";".join([
        f"[1]crop={_BUST_CROP[0]}:{_BUST_CROP[1]}:0:0,scale={pw}:{ph},format=rgba,{fades},split[p][pa]",
        "[pa]alphaextract,boxblur=8:2,lutyuv=y='min(val*1.8\\,255)'[ga]",
        f"color=c={_GLOW}:s={pw}x{ph}:r={settings.video_fps}[gc]",
        "[gc][ga]alphamerge[glow]",
        f"[0:v][glow]overlay=x={x}:y='{bob}':shortest=1[v1]",
        f"[v1][p]overlay=x={x}:y='{bob}':shortest=1,format=yuv420p[v]",
    ])
    output = run_dir / "video_presenter.mp4"
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(video_path),
         "-loop", "1", "-framerate", str(settings.video_fps), "-t", f"{duration:.3f}",
         "-i", str(PRESENTER_PNG),
         "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
         *_video_encode_args(), "-c:a", "copy", str(output)],
        check=True,
    )
    output.replace(video_path)
    return video_path
