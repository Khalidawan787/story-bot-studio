"""Click-worthy YouTube thumbnails.

The old thumbnail was the first scene frame (vertical, 9:16) cropped down to
16:9 with a caption stamped on it — the subject was usually cut off and the
title ran past the right edge. This module instead asks an image model for a
purpose-built 16:9 thumbnail of the video's own topic, then composes the title
on top with wrapped, auto-sized text.

Sources, tried in order (see _thumbnail_provider_order): free Pexels stock
photos -> OpenAI (paid) -> free Pollinations -> the scene image that was already
rendered -> a generated backdrop. Compositing is done with ffmpeg, which is
already bundled, so no extra image library is needed. Every step is non-fatal:
a thumbnail problem must never stop a finished video from uploading.
"""

from __future__ import annotations

import base64
import json
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import settings
from .db import api_usage_today, record_api_usage
from .models import Lesson


THUMB_WIDTH, THUMB_HEIGHT = 1280, 720
# Text may not run edge to edge or YouTube's duration badge / progress bar
# covers it. This is the usable width for the title.
SAFE_TEXT_WIDTH = 1130
# A flat cartoon 1280x720 can be genuinely small, so accept it but keep looking
# for a richer one within the attempt budget.
MIN_USABLE_THUMBNAIL_BYTES = 18_000
GOOD_THUMBNAIL_BYTES = 60_000

# Arial Bold advance widths (em/1000). Guessing a single average ratio here
# overflows the frame on wide letters (M, W, O), so measure per character.
_ARIAL_BOLD_WIDTHS = {
    "A": 722, "B": 722, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 556, "K": 722, "L": 611, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "0": 556, "1": 556, "2": 556, "3": 556, "4": 556, "5": 556, "6": 556,
    "7": 556, "8": 556, "9": 556,
    " ": 278, ",": 278, ".": 278, "!": 333, "?": 611, "'": 238, '"': 474,
    "-": 333, ":": 333, ";": 333, "(": 333, ")": 333, "&": 722, "#": 556,
    "…": 1000,
}
_DEFAULT_CHAR_WIDTH = 722
# DejaVu Sans Bold (the Linux fallback font) runs a little wider than Arial.
_WIDTH_SAFETY = 1.06


def text_width_px(text: str, font_size: int) -> float:
    total = sum(_ARIAL_BOLD_WIDTHS.get(char, _DEFAULT_CHAR_WIDTH) for char in text.upper())
    return total / 1000.0 * font_size * _WIDTH_SAFETY


def _ffmpeg() -> str:
    return settings.ffmpeg_bin


def _font_path() -> str:
    from .visuals import font_path

    return font_path()


def _escape(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:")


def _write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return _escape(str(path))


# ---------------------------------------------------------------- title text

_TITLE_NOISE = re.compile(
    r"\s*(?:\||-)\s*(?:fun learning shorts|kids learning|shorts?|storytime|"
    r"story|full video|episode)\s*$",
    re.I,
)


def thumbnail_headline(lesson: Lesson, channel=None, content_type: str = "short",
                       max_words: int = 7) -> str:
    """Short, punchy text for the thumbnail — not the full SEO title.

    The published title is the better hook ("Can You Guess Cow, Dog, Cat? | Animal
    Sounds 1") than the raw lesson title ("Animal Sounds 1"), but at 1280x720 the
    whole thing is unreadable — so this keeps only its first segment.
    """
    try:
        from .seo import build_metadata

        source = str(build_metadata(lesson, channel, content_type=content_type)["title"])
    except Exception:
        source = str(lesson.title or "")

    text = " ".join(source.split())
    # Keep the first segment of a "A | B | C" title — that is the hook.
    text = text.split("|")[0].strip(" .:-")
    text = _TITLE_NOISE.sub("", text).strip(" .:-")
    # "Animal Sounds 1" -> "Animal Sounds": the batch number means nothing here.
    text = re.sub(r"\s+\d{1,3}$", "", text)
    if not text:
        labels = [scene.label for scene in lesson.scenes[:3]]
        text = ", ".join(labels) or str(lesson.category or "Learning")
    words = text.split()
    if len(words) > max_words:
        ends_as_question = text.endswith("?")
        text = " ".join(words[:max_words]).rstrip(",;:- ")
        if ends_as_question and not text.endswith("?"):
            text += "?"
    return text.upper()


def _wrap(text: str, font_size: int, max_lines: int) -> list[str] | None:
    """Greedy word wrap; None when the text cannot fit at this size."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        if text_width_px(word, font_size) > SAFE_TEXT_WIDTH:
            return None
        candidate = f"{current} {word}".strip()
        if text_width_px(candidate, font_size) <= SAFE_TEXT_WIDTH:
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                return None
    if current:
        lines.append(current)
    return lines if lines and len(lines) <= max_lines else None


def fit_headline(text: str, max_lines: int = 2) -> tuple[list[str], int]:
    """Largest font size at which the headline fits inside the safe area."""
    for font_size in range(104, 39, -4):
        lines = _wrap(text, font_size, max_lines)
        if lines:
            return lines, font_size
    # Longer than any two lines can hold: allow a third line, then truncate.
    for font_size in range(72, 39, -4):
        lines = _wrap(text, font_size, max_lines + 1)
        if lines:
            return lines, font_size
    clipped = text
    while clipped and text_width_px(clipped + "…", 44) > SAFE_TEXT_WIDTH:
        clipped = clipped[:-1]
    return [clipped + "…"], 44


# ------------------------------------------------------------------- artwork

_GENRE_ART_STYLE = {
    "kids": (
        "bright 3D cartoon YouTube thumbnail for preschool kids, one huge "
        "cute smiling subject filling the frame, bold saturated primary "
        "colors, soft studio lighting, simple uncluttered background, "
        "friendly and joyful"
    ),
    "crime": (
        "dark cinematic true-crime YouTube thumbnail, moody teal and amber "
        "lighting, dramatic shadows, one clear focal subject, film-grain "
        "realism, suspenseful mood, no gore"
    ),
    "horror": (
        "eerie cinematic horror YouTube thumbnail, cold blue moonlight and "
        "deep shadows, fog, one unsettling focal subject, tense atmosphere, "
        "suggestive not graphic, no gore and no blood"
    ),
    "love": (
        "warm romantic cinematic YouTube thumbnail, golden-hour light, soft "
        "bokeh, tender emotional focal subject, rich warm colors"
    ),
    "motivation": (
        "epic motivational YouTube thumbnail, dramatic sunrise rim light, "
        "one powerful determined focal subject, cinematic wide vista, "
        "high contrast and inspiring"
    ),
    "trending": (
        "clean modern news-explainer YouTube thumbnail, one clear real-world "
        "focal subject, bold documentary photography look, crisp daylight, "
        "serious factual tone, no logos and no flags of political parties"
    ),
}


def build_thumbnail_prompt(lesson: Lesson, channel=None, content_type: str = "short") -> str:
    """A 16:9 prompt about THIS video's topic, with room left for the title."""
    genre = str(getattr(channel, "genre", "kids") or "kids").lower()
    style = _GENRE_ART_STYLE.get(genre, _GENRE_ART_STYLE["kids"])
    subject = thumbnail_headline(lesson, channel, content_type, max_words=10).title()
    highlights = ", ".join(scene.label for scene in lesson.scenes[:4])
    detail = f" Key elements to show: {highlights}." if highlights else ""
    return (
        f"YouTube thumbnail artwork, 16:9 landscape, about: {subject}.{detail} "
        f"Style: {style}. Composition: main subject on the RIGHT side, the LEFT "
        "third kept simple and uncluttered so a title can be placed there. "
        "Extremely high contrast so it stands out as a small phone thumbnail. "
        "No text, no letters, no words, no captions, no watermark, no logo, "
        "no borders, no collage, no split panels."
    )


def _openai_thumbnail_ready() -> bool:
    import importlib.util

    return (
        bool(settings.openai_api_key)
        and importlib.util.find_spec("openai") is not None
        and api_usage_today("openai", "thumbnail") < settings.thumbnail_daily_limit
    )


def generate_openai_thumbnail(lesson: Lesson, output_path: Path, channel=None,
                              content_type: str = "short") -> Path:
    from openai import OpenAI

    output_path.parent.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=settings.openai_api_key, timeout=90.0, max_retries=0)
    result = client.images.generate(
        model=settings.openai_thumbnail_model,
        prompt=build_thumbnail_prompt(lesson, channel, content_type),
        size="1536x1024",
        quality=settings.openai_thumbnail_quality,
    )
    encoded = result.data[0].b64_json
    if not encoded:
        raise RuntimeError("OpenAI thumbnail response did not include image data.")
    output_path.write_bytes(base64.b64decode(encoded))
    record_api_usage("openai", "thumbnail")
    return output_path


def generate_pollinations_thumbnail(lesson: Lesson, output_path: Path, channel=None,
                                    content_type: str = "short", attempts: int = 3) -> Path:
    from .image_assets import pollinations_api_key

    from .image_assets import _POLLINATIONS_SEMAPHORE

    prompt = urllib.parse.quote(build_thumbnail_prompt(lesson, channel, content_type))
    key = pollinations_api_key()
    base_url = "https://gen.pollinations.ai/image" if key else "https://image.pollinations.ai/prompt"
    headers = {"User-Agent": "KidsLearningBot/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # Keep the largest (most detailed) result across attempts rather than
    # throwing away a thin-but-usable image and ending with nothing.
    best: bytes | None = None
    last_error = "no attempts"
    for attempt in range(max(1, attempts)):
        if attempt:
            # The free endpoint rate-limits bursts; a short wait usually clears it.
            time.sleep(6 * attempt)
        params = urllib.parse.urlencode({
            "width": "1280",
            "height": "720",
            "seed": str(random.randint(0, 1_000_000)),
            "model": "zimage" if key else "flux",
            "enhance": "true",
            "nologo": "true",
            "safe": "true",
        })
        request = urllib.request.Request(f"{base_url}/{prompt}?{params}", headers=headers)
        try:
            # Serialize with the scene-image generator so the two do not
            # rate-limit each other on the shared free endpoint.
            with _POLLINATIONS_SEMAPHORE:
                with urllib.request.urlopen(request, timeout=90) as response:
                    data = response.read()
                    content_type = response.headers.get("Content-Type", "")
        except (urllib.error.URLError, OSError) as exc:
            last_error = str(exc)
            continue
        if "image" not in content_type or len(data) < MIN_USABLE_THUMBNAIL_BYTES:
            last_error = f"unusable response ({content_type}, {len(data)} bytes)"
            continue
        if best is None or len(data) > len(best):
            best = data
        if len(best) >= GOOD_THUMBNAIL_BYTES:
            break
    if best is None:
        raise RuntimeError(f"Pollinations thumbnail failed after {attempts} attempts: {last_error}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(best)
    return output_path


# ------------------------------------------------- your own picture, dropped in

MANUAL_THUMBNAIL_DIR = "data/thumbnails"


def manual_thumbnail_for(lesson: Lesson) -> tuple[Path, bool] | None:
    """A picture you made yourself (Bing Image Creator, Canva, Photopea, ...).

    Drop it in data/thumbnails/ named after the topic key:
      <topic_key>.jpg        -> used as the background, the title is added on top
      <topic_key>.final.jpg  -> used exactly as-is, nothing is drawn on it

    This is how a hand-made thumbnail joins the automatic pipeline: there is no
    public API for Bing Image Creator, and driving its web page with a browser
    breaks its terms of use, so the file is the supported route.
    """
    folder = settings.root / MANUAL_THUMBNAIL_DIR
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        finished = folder / f"{lesson.topic_key}.final{suffix}"
        if finished.exists():
            return finished, True
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        backdrop = folder / f"{lesson.topic_key}{suffix}"
        if backdrop.exists():
            return backdrop, False
    return None


# ------------------------------------------------------------------ ComfyUI

def _comfyui_graph(prompt: str, seed: int) -> dict:
    """FLUX.1-schnell workflow in ComfyUI's API format.

    Drop your own exported API-format workflow at data/comfyui_workflow.json to
    replace this; {prompt}, {seed}, {width} and {height} are substituted in it.
    """
    override = settings.root / "data" / "comfyui_workflow.json"
    if override.exists():
        raw = override.read_text(encoding="utf-8")
        raw = (raw.replace("{prompt}", json.dumps(prompt)[1:-1])
                  .replace("{seed}", str(seed))
                  .replace("{width}", str(THUMB_WIDTH))
                  .replace("{height}", str(THUMB_HEIGHT)))
        return json.loads(raw)
    return {
        "1": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": settings.comfyui_checkpoint}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "text, letters, words, watermark, logo, border, collage",
                         "clip": ["1", 1]}},
        "4": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": THUMB_WIDTH, "height": THUMB_HEIGHT, "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": max(1, settings.comfyui_steps),
                         "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0, "model": ["1", 0], "positive": ["2", 0],
                         "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "thumbnail", "images": ["6", 0]}},
    }


def comfyui_ready() -> bool:
    """True when a local ComfyUI server answers, so we never block on a dead port."""
    if not settings.enable_comfyui_thumbnails:
        return False
    try:
        request = urllib.request.Request(f"{settings.comfyui_url}/system_stats")
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status == 200
    except Exception:
        return False


def generate_comfyui_thumbnail(lesson: Lesson, output_path: Path, channel=None,
                               content_type: str = "short") -> Path:
    """Render the thumbnail on your own PC through a local ComfyUI server."""
    prompt_text = build_thumbnail_prompt(lesson, channel, content_type)
    seed = (sum(ord(char) for char in lesson.topic_key) * 7919 + random.randint(0, 9999)) % 2**31
    payload = json.dumps({"prompt": _comfyui_graph(prompt_text, seed)}).encode("utf-8")
    request = urllib.request.Request(
        f"{settings.comfyui_url}/prompt", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        prompt_id = json.loads(response.read().decode("utf-8")).get("prompt_id")
    if not prompt_id:
        raise RuntimeError("ComfyUI did not accept the workflow (no prompt_id returned).")

    # Local generation is slow, so poll rather than hold one long connection.
    deadline = time.monotonic() + max(30, settings.comfyui_timeout)
    images: list[dict] = []
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            with urllib.request.urlopen(
                f"{settings.comfyui_url}/history/{prompt_id}", timeout=15,
            ) as response:
                history = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue
        entry = history.get(str(prompt_id)) or {}
        status = (entry.get("status") or {}).get("status_str", "")
        if status == "error":
            raise RuntimeError(f"ComfyUI reported an error running the workflow: {entry.get('status')}")
        for node_output in (entry.get("outputs") or {}).values():
            images.extend(node_output.get("images") or [])
        if images:
            break
    if not images:
        raise RuntimeError(
            f"ComfyUI produced no image within {settings.comfyui_timeout}s "
            "(a CPU-only machine is usually far slower than this)."
        )

    image = images[0]
    params = urllib.parse.urlencode({
        "filename": image.get("filename", ""),
        "subfolder": image.get("subfolder", ""),
        "type": image.get("type", "output"),
    })
    with urllib.request.urlopen(f"{settings.comfyui_url}/view?{params}", timeout=60) as response:
        data = response.read()
    if len(data) < MIN_USABLE_THUMBNAIL_BYTES:
        raise RuntimeError(f"ComfyUI returned an unusably small image ({len(data)} bytes).")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    return output_path


# ------------------------------------------------------------------- Pexels

_PEXELS_KEY_FILE = "data/pexels_api_key.txt"
# Mood words that steer the free stock search towards the channel's look.
_GENRE_STOCK_HINT = {
    "crime": "dark moody night investigation",
    "horror": "dark fog eerie night",
    "love": "warm romantic golden hour",
    "motivation": "sunrise determined success",
    "trending": "news documentary photography",
    "kids": "colorful cartoon illustration children",
}
_STOCK_STOP_WORDS = {
    "can", "you", "the", "and", "for", "with", "your", "this", "that", "what",
    "why", "how", "part", "guess", "find", "name", "spot", "say", "count",
}


def pexels_api_key() -> str:
    """Read the free Pexels key from .env or the file the dashboard writes."""
    env_key = str(getattr(settings, "pexels_api_key", "") or "").strip()
    if env_key:
        return env_key
    try:
        return (settings.root / _PEXELS_KEY_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pexels_key_configured() -> bool:
    return bool(pexels_api_key())


def save_pexels_api_key(value: str) -> None:
    key = value.strip()
    path = settings.root / _PEXELS_KEY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if key:
        path.write_text(key, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def _pexels_query(lesson: Lesson, channel=None, content_type: str = "short") -> str:
    genre = str(getattr(channel, "genre", "kids") or "kids").lower()
    words = [
        word for word in re.findall(
            r"[A-Za-z]{3,}", thumbnail_headline(lesson, channel, content_type, max_words=10),
        )
        if word.lower() not in _STOCK_STOP_WORDS
    ][:4]
    if not words:
        words = [scene.label for scene in lesson.scenes[:2]]
    hint = _GENRE_STOCK_HINT.get(genre, "")
    return " ".join([*[word.lower() for word in words], hint]).strip()[:120]


def generate_pexels_thumbnail(lesson: Lesson, output_path: Path, channel=None,
                              content_type: str = "short") -> Path:
    """A free, license-clear landscape photo from Pexels for the thumbnail."""
    key = pexels_api_key()
    if not key:
        raise RuntimeError("No Pexels API key. Get a free one at pexels.com/api.")
    query = _pexels_query(lesson, channel, content_type)
    params = urllib.parse.urlencode({
        "query": query,
        "orientation": "landscape",
        "size": "large",
        "per_page": "15",
    })
    request = urllib.request.Request(
        f"https://api.pexels.com/v1/search?{params}",
        headers={"Authorization": key, "User-Agent": "KidsLearningBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    photos = [
        photo for photo in (payload.get("photos") or [])
        if int(photo.get("width") or 0) >= THUMB_WIDTH
    ]
    if not photos:
        raise RuntimeError(f"Pexels returned no usable landscape photo for: {query}")

    # Vary the pick per topic so two videos never land on the same stock photo.
    offset = sum(ord(char) for char in lesson.topic_key) % len(photos)
    for index in range(len(photos)):
        photo = photos[(offset + index) % len(photos)]
        source = photo.get("src") or {}
        url = source.get("large2x") or source.get("original") or source.get("landscape")
        if not url:
            continue
        try:
            image_request = urllib.request.Request(url, headers={"User-Agent": "KidsLearningBot/1.0"})
            with urllib.request.urlopen(image_request, timeout=45) as response:
                data = response.read()
        except (urllib.error.URLError, OSError):
            continue
        if len(data) < MIN_USABLE_THUMBNAIL_BYTES:
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        # Pexels does not require attribution, but crediting the photographer is
        # good practice and costs one line in the description.
        output_path.with_suffix(output_path.suffix + ".pexels.json").write_text(
            json.dumps({
                "provider": "pexels",
                "creator": photo.get("photographer"),
                "source_page": photo.get("url"),
                "license": "Pexels License (free to use)",
            }, indent=2),
            encoding="utf-8",
        )
        return output_path
    raise RuntimeError(f"Every Pexels result failed to download for: {query}")


def _thumbnail_provider_order(channel=None) -> list[str]:
    """Which sources to try, in order.

    'auto' is genre-aware: real-world channels lead with free Pexels stock,
    while the cartoon kids channel leads with generated art because stock photos
    do not match its look.
    """
    known = ("comfyui", "pexels", "openai", "pollinations")
    configured = str(settings.thumbnail_provider or "auto").lower()
    if configured in known:
        return [configured, *[name for name in known if name != configured]]
    genre = str(getattr(channel, "genre", "kids") or "kids").lower()
    # ComfyUI leads when it is switched on: it is free, unlimited and local.
    lead = ["comfyui"] if settings.enable_comfyui_thumbnails else []
    if genre == "kids":
        return [*lead, "openai", "pollinations", "pexels"]
    return [*lead, "pexels", "openai", "pollinations"]


def generate_thumbnail_art(lesson: Lesson, output_path: Path, channel=None,
                           content_type: str = "short") -> Path | None:
    """Purpose-built 16:9 artwork for this topic, or None if every provider failed."""
    if not settings.enable_ai_thumbnails or settings.thumbnail_provider == "scene":
        return None
    errors: list[str] = []

    for provider in _thumbnail_provider_order(channel):
        try:
            if provider == "comfyui":
                if not comfyui_ready():
                    if settings.enable_comfyui_thumbnails:
                        errors.append(f"ComfyUI: no server answering at {settings.comfyui_url}")
                    continue
                return generate_comfyui_thumbnail(lesson, output_path, channel, content_type)
            if provider == "pexels":
                if not pexels_key_configured():
                    errors.append("Pexels: no free API key saved")
                    continue
                return generate_pexels_thumbnail(lesson, output_path, channel, content_type)
            if provider == "openai":
                if not _openai_thumbnail_ready():
                    errors.append("OpenAI: not configured or daily thumbnail limit reached")
                    continue
                return generate_openai_thumbnail(lesson, output_path, channel, content_type)
            if provider == "pollinations":
                if not settings.enable_pollinations_images:
                    continue
                return generate_pollinations_thumbnail(lesson, output_path, channel, content_type)
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
    if errors:
        print(f"[thumbnail] AI artwork unavailable, using the scene image instead ({' | '.join(errors)})")
    return None


# --------------------------------------------------------------- composition

_BADGE_COLORS = {
    "kids": ("0xffd43b", "0x1a1a1a"),
    "crime": ("0xe03131", "0xffffff"),
    "horror": ("0xc92a2a", "0xffffff"),
    "love": ("0xf06595", "0xffffff"),
    "motivation": ("0xf59f00", "0x1a1a1a"),
    "trending": ("0x1971c2", "0xffffff"),
}


def compose_thumbnail(lesson: Lesson, art_path: Path | None, output_path: Path,
                      channel=None, content_type: str = "short") -> Path:
    """Burn the headline + channel badge onto the artwork at 1280x720."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = _font_path()
    font_arg = f":fontfile='{_escape(font)}'" if font else ""
    genre = str(getattr(channel, "genre", "kids") or "kids").lower()
    badge_bg, badge_fg = _BADGE_COLORS.get(genre, _BADGE_COLORS["kids"])
    is_genre = channel is not None and not getattr(channel, "builtin", False)
    badge_text = (channel.name.upper() if is_genre else "KIDS LEARNING")[:22]

    headline = thumbnail_headline(lesson, channel, content_type)
    lines, font_size = fit_headline(headline)
    # Arial's drawn line box is taller than the nominal font size; ffmpeg's own
    # multi-line spacing is unpredictable, so each line gets its own drawtext at
    # a position we control.
    line_height = int(font_size * 1.16)
    block_height = len(lines) * line_height
    # Sit the title block in the lower third, clear of YouTube's duration badge.
    block_top = max(120, THUMB_HEIGHT - 74 - block_height)
    panel_top = max(0, block_top - 24)
    panel_height = min(THUMB_HEIGHT - panel_top, block_height + 52)

    badge_width = int(48 + text_width_px(badge_text, 34) + 48)
    badge_file = _write_text(output_path.with_suffix(".channel.txt"), badge_text)
    line_layers = []
    for index, line in enumerate(lines):
        line_file = _write_text(output_path.with_suffix(f".line{index}.txt"), line)
        line_layers.append(
            f"drawtext=textfile='{line_file}'{font_arg}:fontsize={font_size}:fontcolor=white:"
            f"borderw={max(4, font_size // 15)}:bordercolor=black:"
            f"x=(w-text_w)/2:y={block_top + index * line_height}"
        )

    if art_path and Path(art_path).exists():
        input_args = ["-loop", "1", "-i", str(art_path)]
        base = (
            f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={THUMB_WIDTH}:{THUMB_HEIGHT},"
            "eq=saturation=1.14:contrast=1.08:brightness=0.02,unsharp=5:5:0.8,"
        )
    else:
        input_args = ["-f", "lavfi", "-i", f"color=c=0x1864ab:s={THUMB_WIDTH}x{THUMB_HEIGHT}:d=1"]
        base = ""

    vf = ",".join([
        *([base.rstrip(",")] if base else []),
        # Darkened panel behind the title so white text always reads.
        f"drawbox=x=0:y={panel_top}:w={THUMB_WIDTH}:h={panel_height}:color=black@0.48:t=fill",
        f"drawbox=x=0:y={panel_top}:w={THUMB_WIDTH}:h=6:color={badge_bg}@0.95:t=fill",
        f"drawbox=x=44:y=38:w={badge_width}:h=62:color={badge_bg}@0.95:t=fill",
        f"drawtext=textfile='{badge_file}'{font_arg}:fontsize=34:fontcolor={badge_fg}:x=72:y=52",
        *line_layers,
    ])

    command = [
        _ffmpeg(), "-y", *input_args,
        "-frames:v", "1", "-update", "1",
        "-vf", vf, "-q:v", "2", str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output_path


def create_thumbnail(lesson: Lesson, image_path: Path | None, output_path: Path,
                     channel=None, content_type: str = "short") -> Path:
    """Public entry point used by the pipeline.

    Tries topic-specific AI artwork first and falls back to the rendered scene
    image, then to a plain card. Never raises for an artwork problem.
    """
    # A picture you dropped in yourself always wins over any generator.
    manual = manual_thumbnail_for(lesson)
    if manual is not None:
        manual_path, is_finished = manual
        if is_finished:
            print(f"[thumbnail] using your finished thumbnail: {manual_path.name}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [_ffmpeg(), "-y", "-i", str(manual_path), "-frames:v", "1", "-update", "1",
                 "-vf", f"scale={THUMB_WIDTH}:{THUMB_HEIGHT}:force_original_aspect_ratio=increase,"
                        f"crop={THUMB_WIDTH}:{THUMB_HEIGHT}",
                 "-q:v", "2", str(output_path)],
                check=True, capture_output=True, text=True,
            )
            return output_path
        print(f"[thumbnail] using your background image: {manual_path.name}")
        return compose_thumbnail(lesson, manual_path, output_path, channel, content_type)

    art_path: Path | None = None
    try:
        art_path = generate_thumbnail_art(
            lesson, output_path.with_name("thumbnail_art.jpg"), channel, content_type,
        )
    except Exception as exc:  # provider bug must not kill a finished video
        print(f"[thumbnail] artwork step skipped: {exc}")

    offline_path: Path | None = None
    if art_path is None and not (image_path and Path(image_path).exists()):
        # Both providers down and no scene image: a generated gradient backdrop
        # still reads far better than a flat color card.
        try:
            from .visuals import create_emergency_scene_image

            offline_path = create_emergency_scene_image(
                lesson.scenes[0], output_path.with_name("thumbnail_backdrop.jpg"),
                channel, landscape=True,
            )
        except Exception as exc:
            print(f"[thumbnail] offline backdrop skipped: {exc}")

    # None is the last resort (a plain color card) — never the first thing tried.
    sources: list[Path | None] = [
        path for path in (art_path, image_path, offline_path)
        if path and Path(path).exists()
    ]
    sources.append(None)
    for source in sources:
        try:
            return compose_thumbnail(lesson, source, output_path, channel, content_type)
        except subprocess.CalledProcessError as exc:
            print(f"[thumbnail] compose failed for {source}: {exc.stderr or exc}")
        except Exception as exc:
            print(f"[thumbnail] compose failed for {source}: {exc}")
    raise RuntimeError("Thumbnail composition failed for every source.")
