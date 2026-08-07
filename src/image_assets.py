from __future__ import annotations

import base64
import html
import importlib.util
import json
import math
import random
import re
import subprocess
import time
import threading
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .db import api_usage_today, record_api_usage
from .lessons import load_topics
from .models import Scene


# A real 1080x1920 illustration is ~300KB-2MB. Anything much smaller is a
# low-detail / near-broken result we should retry rather than keep.
MIN_GOOD_IMAGE_BYTES = 130_000
_PROVIDER_PAUSED_UNTIL: dict[str, float] = {}
_POLLINATIONS_SEMAPHORE = threading.Semaphore(1)
_POLLINATIONS_MIN_BYTES = 30_000


def _provider_available(name: str) -> bool:
    return time.time() >= _PROVIDER_PAUSED_UNTIL.get(name, 0)


def _pause_provider(name: str, seconds: int = 900) -> None:
    """Avoid repeating the same unavailable provider for every scene in a video."""
    _PROVIDER_PAUSED_UNTIL[name] = time.time() + seconds


@dataclass(frozen=True)
class AssetStats:
    total: int
    existing: int
    missing: int


def image_api_ready() -> bool:
    return (
        settings.image_provider == "openai"
        and bool(settings.openai_api_key)
        and importlib.util.find_spec("openai") is not None
        and api_usage_today("openai", "image") < settings.openai_image_daily_limit
    )


def google_image_ready() -> bool:
    return bool(settings.google_image_api_key and settings.google_image_cx)


def pollinations_image_ready() -> bool:
    return settings.enable_pollinations_images


def pollinations_api_key() -> str:
    """Read an optional free Pollinations key without requiring an app restart."""
    env_key = getattr(settings, "pollinations_api_key", "")
    if env_key:
        return env_key.strip()
    key_file = settings.root / "data" / "pollinations_api_key.txt"
    try:
        return key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def pollinations_key_configured() -> bool:
    return bool(pollinations_api_key())


def save_pollinations_api_key(value: str) -> None:
    key = value.strip()
    if key and not key.startswith(("pk_", "sk_")):
        raise ValueError("Pollinations key must start with pk_ or sk_.")
    path = settings.root / "data" / "pollinations_api_key.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    if key:
        path.write_text(key, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


# --------------------------------------------------- Cloudflare Workers AI
#
# Free AI image generation with a generous daily allowance and no credit card.
# Unlike Pexels this produces *generated art*, so it is the free provider that
# can serve the cartoon kids channel, where stock photography is not allowed.

_CLOUDFLARE_ACCOUNT_FILE = "data/cloudflare_account_id.txt"
_CLOUDFLARE_TOKEN_FILE = "data/cloudflare_api_token.txt"


def _read_key_file(relative_path: str) -> str:
    try:
        return (settings.root / relative_path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def cloudflare_account_id() -> str:
    return str(getattr(settings, "cloudflare_account_id", "") or "").strip() or _read_key_file(
        _CLOUDFLARE_ACCOUNT_FILE
    )


def cloudflare_api_token() -> str:
    return str(getattr(settings, "cloudflare_api_token", "") or "").strip() or _read_key_file(
        _CLOUDFLARE_TOKEN_FILE
    )


def cloudflare_image_ready() -> bool:
    return bool(cloudflare_account_id() and cloudflare_api_token())


def save_cloudflare_credentials(account_id: str, api_token: str) -> None:
    """Store the free Workers AI credentials without needing an app restart."""
    for relative_path, value in (
        (_CLOUDFLARE_ACCOUNT_FILE, account_id.strip()),
        (_CLOUDFLARE_TOKEN_FILE, api_token.strip()),
    ):
        path = settings.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if value:
            path.write_text(value, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)


# Tried in order. Models differ in which parameters they accept, so each entry
# carries its own body builder and the first one that returns a usable image
# wins. flux-1-schnell leads on quality; the SDXL models honour width/height,
# which frames a 9:16 scene properly instead of cropping a square.
_CLOUDFLARE_MODELS = (
    "@cf/black-forest-labs/flux-1-schnell",
    "@cf/bytedance/stable-diffusion-xl-lightning",
    "@cf/stabilityai/stable-diffusion-xl-base-1.0",
)


def _cloudflare_body(model: str, prompt: str, seed: int, landscape: bool) -> dict:
    """Both families honour width/height; they differ on the step parameter."""
    width, height = (1344, 768) if landscape else (768, 1344)
    body = {"prompt": prompt, "width": width, "height": height, "seed": seed}
    # flux-1-schnell caps steps at 8 and names the field "steps"; the SDXL
    # models use "num_steps".
    body["steps" if "flux" in model else "num_steps"] = 8
    return body


def _cloudflare_image_bytes(payload: bytes, content_type: str) -> bytes:
    """Workers AI answers either with raw image bytes or base64 inside JSON."""
    if "image" in content_type.lower() and not payload.lstrip().startswith(b"{"):
        return payload
    body = json.loads(payload.decode("utf-8"))
    if not body.get("success", True):
        errors = body.get("errors") or body.get("messages") or []
        raise RuntimeError(str(errors)[:200] or "Cloudflare rejected the request")
    result = body.get("result") or {}
    encoded = result.get("image") if isinstance(result, dict) else None
    if not encoded:
        raise RuntimeError("Cloudflare returned no image data")
    return base64.b64decode(encoded)


def generate_cloudflare_scene_image(
    scene: Scene,
    output_path: Path,
    channel=None,
    landscape: bool = False,
) -> Path:
    """Generate one scene image with Cloudflare Workers AI (free tier)."""
    account = cloudflare_account_id()
    token = cloudflare_api_token()
    if not (account and token):
        raise RuntimeError(
            "Cloudflare Workers AI is not configured. Add CLOUDFLARE_ACCOUNT_ID "
            "and CLOUDFLARE_API_TOKEN (both free at dash.cloudflare.com)."
        )
    orientation = "landscape 16:9" if landscape else "vertical 9:16"
    if _is_genre(channel):
        prompt = _prompt_for_scene(scene, channel) + f" High quality, cinematic, {orientation}."
    else:
        prompt = (
            _prompt_for_scene(scene, channel)
            + f" High quality, kid friendly, colorful, professional YouTube illustration, {orientation}."
        )
    # A random seed keeps every render unique, so the duplicate-image guard in
    # resolve_scene_image never rejects a repeat of the same picture.
    seed = random.randint(0, 2_000_000_000)

    errors: list[str] = []
    for model in _CLOUDFLARE_MODELS:
        request = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}",
            data=json.dumps(_cloudflare_body(model, prompt, seed, landscape)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "StoryBotStudio/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
            data = _cloudflare_image_bytes(payload, content_type)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200] if exc.fp else ""
            errors.append(f"{model.split('/')[-1]}: HTTP {exc.code} {detail}")
            continue
        except Exception as exc:
            errors.append(f"{model.split('/')[-1]}: {exc}")
            continue
        if len(data) < 20_000:
            errors.append(f"{model.split('/')[-1]}: image too small ({len(data)} bytes)")
            continue
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(data)
        _write_provider_marker(output_path, "cloudflare")
        return output_path
    raise RuntimeError(f"Cloudflare Workers AI failed. {' | '.join(errors)}")


def scene_image_path(scene: Scene) -> Path:
    return settings.root / scene.image


def _is_generated_asset(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    return "/assets/generated/" in normalized


def _is_replaceable_asset(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    replaceable_folders = [
        "/assets/colors/",
        "/assets/numbers/",
        "/assets/generated/",
    ]
    return any(folder in normalized for folder in replaceable_folders)


def _looks_low_quality(path: Path) -> bool:
    return path.exists() and path.stat().st_size < settings.low_quality_image_bytes


def _ai_marker_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".ai")


def _write_ai_marker(path: Path) -> None:
    _ai_marker_path(path).write_text("openai", encoding="utf-8")


def _provider_marker_path(path: Path, provider: str) -> Path:
    return path.with_suffix(path.suffix + f".{provider}")


def _write_provider_marker(path: Path, provider: str) -> None:
    _provider_marker_path(path, provider).write_text(provider, encoding="utf-8")


def _category_from_scene(scene: Scene) -> str:
    image = scene.image.replace("\\", "/").lower()
    parts = image.split("/")
    if "generated" in parts:
        index = parts.index("generated")
        if len(parts) > index + 1:
            return parts[index + 1].replace("_", " ")
    if "assets" in parts:
        index = parts.index("assets")
        if len(parts) > index + 1:
            return parts[index + 1].replace("_", " ")
    return "kids learning"


def _is_genre(channel) -> bool:
    return channel is not None and not getattr(channel, "builtin", False)


_STOCK_SAFE_KIDS_CATEGORIES = {
    "animals", "fruits", "vegetables", "vehicles", "nature", "science",
}


def _stock_fallback_allowed(scene: Scene, channel=None) -> bool:
    """Genre channels may use relevant stock; Kids must remain cartoon-only."""
    return channel is not None and not getattr(channel, "builtin", False)

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}

# Rotated per number so ten counting scenes are not ten pictures of apples.
_COUNT_SUBJECTS = (
    "red apples", "yellow rubber ducks", "blue balloons", "green frogs",
    "orange pumpkins", "purple butterflies", "brown teddy bears",
    "pink flowers", "white bunnies", "colourful building blocks",
)


def counting_scene_count(scene: Scene, channel=None) -> int | None:
    """How many objects this scene should show, or None if it is not counting."""
    if _is_genre(channel) or _category_from_scene(scene) != "numbers":
        return None
    count = _NUMBER_WORDS.get(str(scene.label).strip().lower())
    return count if count and 1 <= count <= 20 else None


def compose_counting_image(single: Path, count: int, output_path: Path,
                           landscape: bool = False) -> Path:
    """Tile ONE generated object exactly `count` times.

    Diffusion models cannot count. Asked for three teddy bears they return five,
    and they add stray digits however firmly the prompt forbids text — which is
    fatal on a channel whose whole point is the number. So the model draws a
    single object and this lays out exactly the right number of copies, which is
    arithmetic rather than luck.
    """
    width, height = (1920, 1080) if landscape else (1080, 1920)
    columns = min(5, max(1, math.ceil(math.sqrt(count))))
    rows = math.ceil(count / columns)

    margin = 0.06
    cell_w = int(width * (1 - 2 * margin) / columns)
    cell_h = int(height * (1 - 2 * margin) / rows)
    size = max(48, int(min(cell_w, cell_h) * 0.86))
    # Rows are as tall as the frame allows, which on a 9:16 canvas leaves the
    # objects marooned in white. Pack them at their own height and centre the
    # whole block instead.
    cell_h = int(size * 1.12)
    top = max(int(height * margin), (height - cell_h * rows) // 2)

    # Fit the whole object inside its slot rather than cropping to a square:
    # cropping cut the feet off every teddy bear. The padding is white and so is
    # the canvas, so the tiles leave no visible seam.
    # format/setsar normalise the tile so every overlay sees identical frame
    # properties; without that ffmpeg aborts with "Error reinitializing filters"
    # as soon as more than a few copies are stacked.
    filters = [f"[1:v]scale={size}:{size}:force_original_aspect_ratio=decrease,"
               f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=white,"
               f"format=rgb24,setsar=1[obj]"]
    # Every copy is the same picture, so it is split into as many streams as
    # there are slots and each one is dropped at its own coordinates.
    filters.append("[obj]split=%d%s" % (count, "".join(f"[o{i}]" for i in range(count))))
    current = "[0:v]"
    for index in range(count):
        row, column = divmod(index, columns)
        in_row = min(columns, count - row * columns)
        span_w = width * (1 - 2 * margin) / in_row
        x = int(width * margin + span_w * (column % in_row) + (span_w - size) / 2)
        y = int(top + cell_h * row + (cell_h - size) / 2)
        label = f"[s{index}]" if index < count - 1 else "[out]"
        filters.append(f"{current}[o{index}]overlay={x}:{y}{label}")
        current = f"[s{index}]"

    from .visuals import ffmpeg_bin

    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_bin(), "-y",
            "-f", "lavfi", "-i", f"color=c=white:s={width}x{height}:d=1,format=rgb24",
            "-i", str(single),
            "-filter_complex", ";".join(filters),
            "-map", "[out]", "-frames:v", "1", str(output_path),
        ],
        check=True, capture_output=True, text=True,
    )
    return output_path


def _singular(plural: str) -> str:
    words = plural.split()
    last = words[-1]
    if last.endswith("ies"):
        last = last[:-3] + "y"
    elif last.endswith("es") and last[:-2].endswith(("ch", "sh", "s", "x")):
        last = last[:-2]
    elif last.endswith("s"):
        last = last[:-1]
    return " ".join(words[:-1] + [last])


def _count_subject(label: str) -> str:
    index = sum(ord(ch) for ch in str(label).lower()) % len(_COUNT_SUBJECTS)
    return _COUNT_SUBJECTS[index]


def _prompt_for_scene(scene: Scene, channel=None) -> str:
    # Genre channels (crime/love/horror/...) carry their own styled prompt.
    if _is_genre(channel):
        return scene.image_prompt or f"{scene.label}, {channel.image_style}"
    category = _category_from_scene(scene)
    if category == "colors":
        return (
            "Professional 3D cartoon preschool learning image. "
            f"Theme: the color {scene.label}. Show cheerful objects, paint splashes, balloons, "
            f"and a clean kids classroom scene where the main color is clearly {scene.label}. "
            "Vertical 9:16, large centered subject, bright lighting, no readable text, no watermark."
        )
    if category == "opposites":
        return (
            "Professional 3D cartoon preschool learning image about opposites. "
            f"Main concept: {scene.label}. Show a clear visual comparison that children can understand, "
            "cute classroom/playground style, vertical 9:16, large centered subject, no readable text, no watermark."
        )
    if category == "numbers":
        # Never ask an image model to draw the numeral. It cannot render glyphs
        # reliably, and the old prompt asked for "Main subject: Nineteen" while
        # also saying "no readable text" — so it produced blobs that looked like
        # broken digits and did not match the narration at all. Counting is
        # taught by the QUANTITY of objects, which a model draws well.
        count = _NUMBER_WORDS.get(str(scene.label).strip().lower())
        subject = _count_subject(scene.label)
        if count is not None and 1 <= count <= 20:
            # Only ONE object is generated; compose_counting_image lays out the
            # right number of copies afterwards.
            return (
                "Professional 3D cartoon preschool illustration of exactly ONE "
                f"{_singular(subject)}, centred, complete, nothing cut off, "
                "plain flat white background, no shadow behind it, "
                "no numbers, no letters, no text, no watermark."
            )
        if count is not None and count > 0:
            # One apple is not "1 identical apples", and a dozen objects only
            # stay countable when they are laid out in rows.
            if count == 1:
                what = f"exactly ONE {_singular(subject)}, alone in the picture"
            elif count <= 10:
                what = (f"EXACTLY {count} identical {subject}, clearly separated "
                        "and easy to count, in a simple row or group")
            else:
                what = (f"EXACTLY {count} identical {subject}, laid out in neat "
                        "rows of five so they can be counted at a glance")
            return (
                "Professional 3D cartoon preschool learning illustration. "
                f"Show {what}. "
                "Nothing else in the picture that could be miscounted. "
                "Cute kids educational style, vertical 9:16, bright clean background, "
                "no numbers, no letters, no text, no watermark."
            )
    if category == "alphabet":
        # Same problem with letters: the letter itself is burned on by the
        # renderer, so the picture only has to carry objects that start with it.
        return (
            "Professional 3D cartoon preschool learning illustration. "
            f"Show two or three cheerful objects whose names start with the letter "
            f"{str(scene.label).strip()[:1].upper()}, arranged clearly side by side. "
            "Cute kids educational style, vertical 9:16, bright clean background, "
            "no numbers, no letters, no text, no watermark."
        )
    if category in {"shapes", "fruits", "vegetables", "vehicles", "science"}:
        return (
            "Professional 3D cartoon preschool learning illustration. "
            f"Category: {category}. Main subject: {scene.label}. "
            "Cute high-quality kids educational style, vertical 9:16, large centered subject, "
            "bright clean background, no readable text, no watermark."
        )
    if scene.image_prompt:
        return scene.image_prompt
    return (
        "Bright professional 3D cartoon kids learning illustration. "
        f"Main subject: {scene.label}. Vertical 9:16, large centered subject, "
        "clean cheerful preschool background, no readable text, no watermark."
    )


def generate_openai_scene_image(scene: Scene, output_path: Path, channel=None, landscape: bool = False) -> Path:
    from openai import OpenAI

    output_path.parent.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=settings.openai_api_key, timeout=75.0, max_retries=0)
    result = client.images.generate(
        model=settings.openai_image_model,
        prompt=_prompt_for_scene(scene, channel) + (" Landscape 16:9 composition." if landscape else " Vertical 9:16 composition."),
        size="1536x1024" if landscape else settings.openai_image_size,
        quality=settings.openai_image_quality,
    )
    image_base64 = result.data[0].b64_json
    if not image_base64:
        raise RuntimeError("OpenAI image response did not include image data.")
    output_path.write_bytes(base64.b64decode(image_base64))
    record_api_usage("openai", "image")
    _write_ai_marker(output_path)
    return output_path


def _search_query_for_scene(scene: Scene) -> str:
    category = _category_from_scene(scene)
    if category == "colors":
        return f"{scene.label} color kids learning cartoon objects"
    if category == "opposites":
        return f"{scene.label} opposite kids learning cartoon illustration"
    return f"{scene.label} {category} kids learning cartoon illustration"


def generate_google_scene_image(scene: Scene, output_path: Path) -> Path:
    query = _search_query_for_scene(scene)
    params = urllib.parse.urlencode(
        {
            "key": settings.google_image_api_key,
            "cx": settings.google_image_cx,
            "q": query,
            "searchType": "image",
            "num": "1",
            "safe": "active",
            "imgSize": "large",
            "rights": "cc_publicdomain,cc_attribute,cc_sharealike",
        }
    )
    request = urllib.request.Request(
        f"https://www.googleapis.com/customsearch/v1?{params}",
        headers={"User-Agent": "KidsLearningBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    items = payload.get("items") or []
    if not items:
        raise RuntimeError(f"Google image search returned no images for: {query}")

    image_url = items[0]["link"]
    image_request = urllib.request.Request(image_url, headers={"User-Agent": "KidsLearningBot/1.0"})
    with urllib.request.urlopen(image_request, timeout=45) as response:
        data = response.read()
    if len(data) < 10_000:
        raise RuntimeError(f"Downloaded image too small from Google result: {image_url}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    _write_provider_marker(output_path, "google")
    return output_path



def openverse_image_ready() -> bool:
    """Openverse needs no API key; availability is checked per request."""
    return True


def _openverse_query(scene: Scene, channel=None) -> str:
    """Build a short concrete stock-image query from both label and visual prompt."""
    label = scene.label.strip()
    prompt_words = [
        word.strip(".,:;!?()[]").lower()
        for word in (scene.image_prompt or "").split()
    ]
    ignored = {
        "high", "quality", "detailed", "cinematic", "illustration", "image",
        "vertical", "landscape", "composition", "lighting", "style", "scene",
        "colorful", "bright", "friendly", "dramatic", "beautiful",
    }
    concrete = [
        word for word in prompt_words
        if len(word) > 2 and word not in ignored and word.isascii()
    ][:6]
    genre = getattr(channel, "genre", "") if channel is not None else ""
    suffix = "children education" if genre == "kids" else genre
    return " ".join(part for part in [label, *concrete, suffix] if part)[:120]

_STOCK_STOP_WORDS = {
    "about", "against", "around", "clear", "close", "detailed", "dramatic",
    "evening", "high", "image", "illustration", "lighting", "moody", "part",
    "scene", "showing", "style", "true", "video", "visual", "with", "without",
    "dark", "cinematic", "composition", "quality", "original", "complete",
    "create", "inspired", "date", "seed", "july", "story", "noir", "gore",
}
_STOCK_BANNED_WORDS = {
    "advertisement", "banner", "book cover", "brochure", "campaign", "communism",
    "election", "infographic", "magazine", "manifesto", "new world order",
    "newspaper", "political", "politician", "politics", "poster", "propaganda",
    "soviet", "typography", "666",
}
_STOCK_ALIASES = {
    "investigator": "detective", "investigation": "detective", "police": "detective",
    "mansion": "manor", "estate": "manor", "house": "manor",
    "footprint": "footprints", "shoeprint": "footprints", "prints": "footprints",
    "aroma": "scent", "perfume": "scent", "odor": "scent", "smell": "scent",
    "document": "letter", "documents": "letter", "records": "letter",
    "interrogation": "suspect", "criminal": "suspect",
}


def _stock_tokens(value: object) -> set[str]:
    text = html.unescape(str(value or "")).lower()
    return {
        _STOCK_ALIASES.get(word, word)
        for word in re.findall(r"[a-z]{3,}", text)
        if word not in _STOCK_STOP_WORDS
    }


def _stock_relevance_score(scene: Scene, candidate_text: object, channel=None) -> int:
    """Reject stock results whose metadata does not match the narrated scene."""
    if not _is_genre(channel):
        return 1
    lowered = html.unescape(str(candidate_text or "")).lower()
    if any(term in lowered for term in _STOCK_BANNED_WORDS):
        return -100
    anchors = _stock_tokens(f"{scene.label} {scene.image_prompt or ''}")
    candidate = _stock_tokens(lowered)
    overlap = anchors & candidate
    genre = str(getattr(channel, "genre", "") or "").lower()
    return len(overlap) * 3 + (1 if genre and genre in candidate else 0)


def _openverse_candidate_text(item: dict) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "tags", "category", "source"))

def _write_openverse_metadata(output_path: Path, item: dict) -> None:
    metadata = {
        "provider": "openverse",
        "title": item.get("title"),
        "creator": item.get("creator"),
        "creator_url": item.get("creator_url"),
        "license": item.get("license"),
        "license_url": item.get("license_url"),
        "source": item.get("source"),
        "source_page": item.get("foreign_landing_url"),
        "image_url": item.get("url"),
        "width": item.get("width"),
        "height": item.get("height"),
        "attribution": item.get("attribution"),
    }
    output_path.with_suffix(output_path.suffix + ".openverse.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def pexels_image_ready() -> bool:
    """Pexels needs a free key; the dashboard stores it in data/pexels_api_key.txt."""
    from .thumbnails import pexels_api_key

    return bool(pexels_api_key())


def _pexels_scene_query(scene: Scene, channel=None) -> str:
    """Search words taken from the scene itself, plus the channel's mood hint."""
    from .thumbnails import _GENRE_STOCK_HINT

    words = [
        word.lower() for word in re.findall(r"[A-Za-z]{3,}", f"{scene.label} {scene.image_prompt or ''}")
        if word.lower() not in _STOCK_STOP_WORDS
    ][:5]
    genre = str(getattr(channel, "genre", "") or "").lower()
    return " ".join([*words, _GENRE_STOCK_HINT.get(genre, "")]).strip()[:120]


def generate_pexels_scene_image(
    scene: Scene,
    output_path: Path,
    channel=None,
    landscape: bool = False,
    attempts: int = 6,
) -> Path:
    """A free, license-clear Pexels photo for one scene.

    This is the dependable stock fallback for real-world channels when the AI
    providers are down (OpenAI billing limit, Pollinations HTTP 429). Photos are
    picked from a random offset so repeat renders of the same scene get
    different pictures instead of tripping the duplicate-image guard.
    """
    from .thumbnails import pexels_api_key

    key = pexels_api_key()
    if not key:
        raise RuntimeError("No Pexels API key. Get a free one at pexels.com/api.")
    query = _pexels_scene_query(scene, channel)
    params = urllib.parse.urlencode({
        "query": query,
        "orientation": "landscape" if landscape else "portrait",
        "size": "large",
        "per_page": "40",
    })
    request = urllib.request.Request(
        f"https://api.pexels.com/v1/search?{params}",
        headers={"Authorization": key, "User-Agent": "StoryBotStudio/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    photos = list(payload.get("photos") or [])
    if not photos:
        raise RuntimeError(f"Pexels returned no photo for: {query}")

    offset = random.randrange(len(photos))
    errors: list[str] = []
    best: tuple[bytes, dict] | None = None
    for index in range(min(len(photos), max(1, attempts))):
        photo = photos[(offset + index) % len(photos)]
        source = photo.get("src") or {}
        url = source.get("large2x") or source.get("original") or source.get("large")
        if not url:
            continue
        try:
            image_request = urllib.request.Request(
                str(url), headers={"User-Agent": "StoryBotStudio/1.0"},
            )
            with urllib.request.urlopen(image_request, timeout=60) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "")
        except Exception as exc:
            errors.append(str(exc))
            continue
        if "image" not in content_type.lower() or len(data) < 50_000:
            continue
        if best is None or len(data) > len(best[0]):
            best = (data, photo)
        if len(data) >= MIN_GOOD_IMAGE_BYTES:
            break
    if best is None:
        detail = errors[-1] if errors else "downloaded files were too small"
        raise RuntimeError(f"Pexels image download failed: {detail}")

    data, photo = best
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    _write_provider_marker(output_path, "pexels")
    output_path.with_suffix(output_path.suffix + ".pexels.json").write_text(
        json.dumps({
            "provider": "pexels",
            "creator": photo.get("photographer"),
            "creator_url": photo.get("photographer_url"),
            "source_page": photo.get("url"),
            "width": photo.get("width"),
            "height": photo.get("height"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


def generate_openverse_scene_image(
    scene: Scene,
    output_path: Path,
    channel=None,
    landscape: bool = False,
    attempts: int = 6,
) -> Path:
    """Download a commercially reusable, openly licensed image without an API key."""
    detailed = _openverse_query(scene, channel)
    prompt_words = [word.strip(".,:;!?()[]") for word in (scene.image_prompt or "").split() if len(word) > 3]
    queries = [detailed, " ".join(prompt_words[:5])]
    payload = {"results": []}
    search_errors: list[str] = []
    for query in dict.fromkeys(q for q in queries if q):
        params = urllib.parse.urlencode({
            "q": query,
            "license_type": "commercial",
            "mature": "false",
            "filter_dead": "true",
            "page_size": "20",
        })
        request = urllib.request.Request(
            f"https://api.openverse.org/v1/images/?{params}",
            headers={"User-Agent": "StoryBotStudio/1.0 (Openverse image fallback)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                candidate_payload = json.loads(response.read().decode("utf-8"))
            if candidate_payload.get("results"):
                payload = candidate_payload
                break
        except Exception as exc:
            search_errors.append(str(exc))
    if not payload.get("results") and search_errors:
        raise RuntimeError(f"Openverse search failed: {search_errors[-1]}")

    candidates = []
    for item in payload.get("results", []):
        if str(item.get("license", "")).lower() not in {"cc0", "pdm", "by", "by-sa"}:
            continue
        # The renderer safely center-crops any orientation to 16:9 or 9:16.
        # Do not discard a relevant reusable image only because its source is
        # portrait or square.
        relevance = _stock_relevance_score(scene, _openverse_candidate_text(item), channel)
        if relevance < 1:
            continue
        item["_relevance_score"] = relevance
        candidates.append(item)
    if not candidates:
        raise RuntimeError("Openverse returned no matching reusable images.")
    candidates.sort(key=lambda item: int(item.get("_relevance_score", 0)), reverse=True)

    best: tuple[bytes, dict] | None = None
    errors: list[str] = []
    for item in candidates[:max(1, attempts)]:
        urls = [item.get("url"), item.get("thumbnail")]
        for image_url in [url for url in urls if url]:
            try:
                image_request = urllib.request.Request(
                    str(image_url), headers={"User-Agent": "StoryBotStudio/1.0"},
                )
                with urllib.request.urlopen(image_request, timeout=60) as response:
                    data = response.read()
                    content_type = response.headers.get("Content-Type", "")
                if "image" not in content_type.lower() or len(data) < 50_000:
                    continue
                if best is None or len(data) > len(best[0]):
                    best = (data, item)
                if len(data) >= MIN_GOOD_IMAGE_BYTES:
                    break
            except Exception as exc:
                errors.append(str(exc))
        if best is not None and len(best[0]) >= MIN_GOOD_IMAGE_BYTES:
            break
    if best is None:
        detail = errors[-1] if errors else "downloaded files were too small"
        raise RuntimeError(f"Openverse image download failed: {detail}")

    data, item = best
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    _write_provider_marker(output_path, "openverse")
    _write_openverse_metadata(output_path, item)
    return output_path


def wikimedia_image_ready() -> bool:
    """Wikimedia Commons API is free and needs no key."""
    return True


def _plain_metadata(value) -> str:
    raw = value.get("value", "") if isinstance(value, dict) else str(value or "")
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def generate_wikimedia_scene_image(
    scene: Scene,
    output_path: Path,
    channel=None,
    landscape: bool = False,
    attempts: int = 8,
) -> Path:
    """Download an openly licensed image from Wikimedia Commons without a key."""
    params = urllib.parse.urlencode({
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": _openverse_query(scene, channel),
        "gsrnamespace": "6",
        "gsrlimit": "20",
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": "1600",
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Credit|ImageDescription|UsageTerms",
    })
    request = urllib.request.Request(
        f"https://commons.wikimedia.org/w/api.php?{params}",
        headers={"User-Agent": "StoryBotStudio/1.0 (free image fallback)"},
    )
    payload = None
    last_search_error = None
    for retry in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except Exception as exc:
            last_search_error = exc
            if retry < 2:
                time.sleep(2 * (retry + 1))
    if payload is None:
        raise RuntimeError(f"Wikimedia search failed after 3 attempts: {last_search_error}")

    candidates: list[tuple[dict, dict]] = []
    for page in payload.get("query", {}).get("pages", []):
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        metadata = info.get("extmetadata") or {}
        license_name = _plain_metadata(metadata.get("LicenseShortName")).lower()
        if not any(token in license_name for token in ("public domain", "cc0", "cc zero", "cc by", "attribution")):
            continue
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        if width and height:
            if landscape and width <= height:
                continue
            if not landscape and height <= width:
                continue
        if not str(info.get("mime", "")).startswith("image/"):
            continue
        candidate_text = " ".join([
            str(page.get("title") or ""),
            _plain_metadata(metadata.get("ImageDescription")),
            _plain_metadata(metadata.get("Credit")),
        ])
        relevance = _stock_relevance_score(scene, candidate_text, channel)
        if relevance < 1:
            continue
        page["_relevance_score"] = relevance
        candidates.append((page, info))
    if not candidates:
        raise RuntimeError("Wikimedia returned no matching Public Domain/CC0 images.")
    candidates.sort(key=lambda pair: int(pair[0].get("_relevance_score", 0)), reverse=True)

    best: tuple[bytes, dict, dict] | None = None
    errors: list[str] = []
    for page, info in candidates[:max(1, attempts)]:
        for image_url in (info.get("thumburl"), info.get("url")):
            if not image_url:
                continue
            try:
                image_request = urllib.request.Request(
                    str(image_url), headers={"User-Agent": "StoryBotStudio/1.0"},
                )
                with urllib.request.urlopen(image_request, timeout=60) as response:
                    data = response.read()
                    content_type = response.headers.get("Content-Type", "")
                if "image" not in content_type.lower() or len(data) < 50_000:
                    continue
                if best is None or len(data) > len(best[0]):
                    best = (data, page, info)
                if len(data) >= MIN_GOOD_IMAGE_BYTES:
                    break
            except Exception as exc:
                errors.append(str(exc))
        if best is not None and len(best[0]) >= MIN_GOOD_IMAGE_BYTES:
            break
    if best is None:
        detail = errors[-1] if errors else "downloaded files were too small"
        raise RuntimeError(f"Wikimedia image download failed: {detail}")

    data, page, info = best
    metadata = info.get("extmetadata") or {}
    record = {
        "provider": "wikimedia",
        "title": page.get("title"),
        "creator": _plain_metadata(metadata.get("Artist")),
        "credit": _plain_metadata(metadata.get("Credit")),
        "license": _plain_metadata(metadata.get("LicenseShortName")),
        "license_url": _plain_metadata(metadata.get("LicenseUrl")),
        "source_page": info.get("descriptionurl"),
        "image_url": info.get("url"),
        "width": info.get("width"),
        "height": info.get("height"),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    _write_provider_marker(output_path, "wikimedia")
    output_path.with_suffix(output_path.suffix + ".wikimedia.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return output_path

def _encoded_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read PNG/JPEG dimensions without adding a Pillow dependency."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                height = int.from_bytes(data[index + 3:index + 5], "big")
                width = int.from_bytes(data[index + 5:index + 7], "big")
                return width, height
            index += length
    return None


def _generated_image_has_full_frame(data: bytes) -> bool:
    """Reject provider JPEGs whose lower/upper half decodes as a flat gray block."""
    try:
        result = subprocess.run(
            [settings.ffmpeg_bin, "-v", "error", "-i", "pipe:0", "-vf", "scale=16:24",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
            input=data, capture_output=True, timeout=20,
        )
    except Exception:
        return False
    raw = result.stdout
    if result.returncode != 0 or len(raw) != 16 * 24 * 3:
        return False
    rows = []
    for y in range(24):
        luminance = []
        for x in range(16):
            offset = (y * 16 + x) * 3
            r, g, b = raw[offset:offset + 3]
            luminance.append((r * 299 + g * 587 + b * 114) / 1000.0)
        rows.append(luminance)

    def variation(block: list[list[float]]) -> tuple[float, float]:
        values = [value for row in block for value in row]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        return mean, variance ** 0.5

    top_mean, top_sd = variation(rows[:12])
    bottom_mean, bottom_sd = variation(rows[12:])
    # Broken Pollinations responses commonly contain a perfectly flat neutral
    # gray half while the other half contains a normal image.
    if bottom_sd < 2.0 and top_sd > 8.0 and 85 <= bottom_mean <= 190:
        return False
    if top_sd < 2.0 and bottom_sd > 8.0 and 85 <= top_mean <= 190:
        return False
    return True

def _usable_pollinations_image(data: bytes, landscape: bool) -> bool:
    if len(data) < _POLLINATIONS_MIN_BYTES:
        return False
    dimensions = _encoded_image_dimensions(data)
    if dimensions is None:
        return len(data) >= MIN_GOOD_IMAGE_BYTES
    width, height = dimensions
    if min(width, height) < 512 or max(width, height) < 960:
        return False
    orientation_ok = width > height if landscape else height > width
    return orientation_ok and _generated_image_has_full_frame(data)


def generate_pollinations_scene_image(scene: Scene, output_path: Path, channel=None,
                                      fresh: bool = False, attempts: int = 5,
                                      landscape: bool = False) -> Path:
    # The free endpoint rate-limits parallel calls. Serialize only this provider;
    # video jobs continue independently and wait their turn for a fresh image.
    with _POLLINATIONS_SEMAPHORE:
        return _generate_pollinations_scene_image(
            scene, output_path, channel, fresh=fresh,
            attempts=attempts, landscape=landscape,
        )


def _generate_pollinations_scene_image(scene: Scene, output_path: Path, channel=None,
                                      fresh: bool = False, attempts: int = 5,
                                      landscape: bool = False) -> Path:
    if _is_genre(channel):
        # Genre videos must NOT get kid-friendly styling.
        orientation = "landscape 16:9" if landscape else "vertical 9:16"
        prompt = _prompt_for_scene(scene, channel) + f" High quality, cinematic, {orientation}."
    else:
        orientation = "landscape 16:9" if landscape else "vertical 9:16"
        prompt = (
            _prompt_for_scene(scene, channel)
            + f" High quality, kid friendly, colorful, professional YouTube illustration, {orientation}."
        )
    encoded_prompt = urllib.parse.quote(prompt)
    # fresh=True picks a random seed so each render gets a NEW image.
    base_seed = random.randint(0, 1_000_000) if fresh else (abs(hash(scene.image)) % 1_000_000)

    # Try a few seeds and KEEP THE BEST (largest = most detailed). This is what
    # stops the occasional tiny/near-broken result from being saved and then
    # stuck forever behind its provider marker.
    best_data: bytes | None = None
    last_error = "no attempts"
    key = pollinations_api_key()
    for i in range(max(1, attempts)):
        seed = (base_seed + i * 97) % 1_000_000
        params = urllib.parse.urlencode(
            {
                "width": "1920" if landscape else "1080",
                "height": "1080" if landscape else "1920",
                "seed": str(seed),
                "model": "zimage" if key else "flux",
                "enhance": "true",
                "nologo": "true",
                "safe": "true",
            }
        )
        base_url = "https://gen.pollinations.ai/image" if key else "https://image.pollinations.ai/prompt"
        url = f"{base_url}/{encoded_prompt}?{params}"
        headers = {"User-Agent": "KidsLearningBot/1.0"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code in (429, 503) and i + 1 < max(1, attempts):
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    wait_seconds = float(retry_after)
                except (TypeError, ValueError):
                    wait_seconds = 3 * (2 ** i)
                time.sleep(max(1, min(wait_seconds, 20)))
            continue
        except Exception as exc:  # network hiccup - try the next seed
            last_error = str(exc)
            if i + 1 < max(1, attempts):
                time.sleep(min(2 ** i, 8))
            continue
        if "image" not in content_type.lower() or len(data) < 20_000:
            last_error = f"invalid image (type={content_type}, {len(data)}B)"
            continue
        if not _usable_pollinations_image(data, landscape):
            dimensions = _encoded_image_dimensions(data)
            last_error = f"image quality too low ({len(data)}B, dimensions={dimensions})"
            continue
        if best_data is None or len(data) > len(best_data):
            best_data = data
        break

    if best_data is None:
        raise RuntimeError(f"Pollinations did not return a valid image. {last_error}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(best_data)
    _write_provider_marker(output_path, "pollinations")
    return output_path


def _generate_best_available_scene_image(scene: Scene, output_path: Path, channel=None) -> Path:
    """Prefer free generation, then paid OpenAI, then free stock-photo sources."""
    errors: list[str] = []
    stock_allowed = _stock_fallback_allowed(scene, channel)
    if image_api_ready() and _provider_available("openai"):
        try:
            return generate_openai_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"OpenAI: {exc}")
            _pause_provider("openai")
    if cloudflare_image_ready() and _provider_available("cloudflare"):
        try:
            return generate_cloudflare_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Cloudflare: {exc}")
            _pause_provider("cloudflare", seconds=120)
    if pollinations_image_ready() and _provider_available("pollinations"):
        try:
            return generate_pollinations_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Pollinations: {exc}")
            _pause_provider("pollinations", seconds=90)
    if stock_allowed and pexels_image_ready():
        try:
            return generate_pexels_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Pexels: {exc}")
    if stock_allowed and openverse_image_ready():
        try:
            return generate_openverse_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Openverse: {exc}")
    if stock_allowed and wikimedia_image_ready():
        try:
            return generate_wikimedia_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Wikimedia: {exc}")
    if google_image_ready() and _is_genre(channel):
        try:
            return generate_google_scene_image(scene, output_path)
        except Exception as exc:
            errors.append(f"Google: {exc}")
    if errors:
        raise RuntimeError(" | ".join(errors))
    raise RuntimeError("No real-image provider is available.")

def ensure_scene_asset(scene: Scene, channel=None) -> Path | None:
    output_path = scene_image_path(scene)

    # Genre channels: use the styled prompt, generate a free image when missing.
    if _is_genre(channel):
        if output_path.exists():
            return output_path
        try:
            return _generate_best_available_scene_image(scene, output_path, channel)
        except Exception:
            return None

    if output_path.exists():
        if (
            (
                (_is_generated_asset(output_path) and settings.auto_replace_generated_with_ai)
                or (
                    settings.auto_replace_low_quality_images
                    and _is_replaceable_asset(output_path)
                    and _looks_low_quality(output_path)
                )
            )
            and settings.auto_generate_missing_images
            and (image_api_ready() or pollinations_image_ready() or google_image_ready())
            and not _ai_marker_path(output_path).exists()
            and not _provider_marker_path(output_path, "pollinations").exists()
            and not _provider_marker_path(output_path, "google").exists()
            and not _provider_marker_path(output_path, "openverse").exists()
            and not _provider_marker_path(output_path, "wikimedia").exists()
        ):
            try:
                return _generate_best_available_scene_image(scene, output_path)
            except Exception:
                return output_path
        return output_path
    if settings.auto_generate_missing_images and image_api_ready():
        try:
            return _generate_best_available_scene_image(scene, output_path)
        except Exception:
            pass
    if settings.auto_generate_missing_images and pollinations_image_ready():
        try:
            return generate_pollinations_scene_image(scene, output_path)
        except Exception:
            pass
    return None


def generate_fresh_image(
    scene: Scene,
    output_path: Path,
    channel=None,
    landscape: bool = False,
) -> Path:
    """Generate a unique real image; paid OpenAI is the first fallback after free AI."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    stock_allowed = _stock_fallback_allowed(scene, channel)
    if image_api_ready() and _provider_available("openai"):
        try:
            return generate_openai_scene_image(scene, output_path, channel, landscape=landscape)
        except Exception as exc:
            errors.append(f"OpenAI: {exc}")
            _pause_provider("openai")
    # Free generated art. Ahead of Pollinations because the free Pollinations
    # endpoint now rate-limits anonymous callers, and ahead of every stock
    # source because only generated art suits the cartoon kids channel.
    if cloudflare_image_ready() and _provider_available("cloudflare"):
        try:
            return generate_cloudflare_scene_image(scene, output_path, channel, landscape=landscape)
        except Exception as exc:
            errors.append(f"Cloudflare: {exc}")
            _pause_provider("cloudflare", seconds=120)
    if pollinations_image_ready() and _provider_available("pollinations"):
        try:
            return generate_pollinations_scene_image(
                scene, output_path, channel, fresh=True, attempts=5,
                landscape=landscape,
            )
        except Exception as exc:
            errors.append(f"Pollinations: {exc}")
            _pause_provider("pollinations", seconds=90)
    # Pexels is the reliable stock source when both AI providers are down.
    # Openverse/Wikimedia are searched afterwards because they return far fewer
    # usable, on-topic photos.
    if stock_allowed and pexels_image_ready():
        try:
            return generate_pexels_scene_image(
                scene, output_path, channel, landscape=landscape, attempts=6,
            )
        except Exception as exc:
            errors.append(f"Pexels: {exc}")
    if stock_allowed and openverse_image_ready():
        try:
            return generate_openverse_scene_image(
                scene, output_path, channel, landscape=landscape, attempts=6,
            )
        except Exception as exc:
            errors.append(f"Openverse: {exc}")
    if stock_allowed and wikimedia_image_ready():
        try:
            return generate_wikimedia_scene_image(
                scene, output_path, channel, landscape=landscape, attempts=8,
            )
        except Exception as exc:
            errors.append(f"Wikimedia: {exc}")
    if google_image_ready() and _is_genre(channel):
        try:
            return generate_google_scene_image(scene, output_path)
        except Exception as exc:
            errors.append(f"Google: {exc}")
    detail = " | ".join(errors) if errors else "No real-image provider is available."
    raise RuntimeError(f"Real image generation failed for '{scene.label}'. {detail}")

def unique_missing_scenes(limit: int | None = None) -> list[Scene]:
    seen_paths: set[Path] = set()
    missing: list[Scene] = []
    for raw_lesson in load_topics().values():
        for raw_scene in raw_lesson["scenes"]:
            scene = Scene(**raw_scene)
            path = scene_image_path(scene)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            if not path.exists():
                missing.append(scene)
                if limit and len(missing) >= limit:
                    return missing
    return missing


def asset_stats() -> AssetStats:
    total = 0
    existing = 0
    seen_paths: set[Path] = set()
    for raw_lesson in load_topics().values():
        for raw_scene in raw_lesson["scenes"]:
            scene = Scene(**raw_scene)
            path = scene_image_path(scene)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            total += 1
            if path.exists():
                existing += 1
    return AssetStats(total=total, existing=existing, missing=total - existing)


def generate_missing_assets(limit: int = 10) -> tuple[int, list[str]]:
    if not image_api_ready() and not pollinations_image_ready() and not google_image_ready():
        return 0, [
            "Image API is not ready. Enable Pollinations, set OpenAI billing/key, or add GOOGLE_IMAGE_API_KEY + GOOGLE_IMAGE_CX."
        ]

    created = 0
    messages: list[str] = []
    for scene in unique_missing_scenes(limit):
        try:
            path = _generate_best_available_scene_image(scene, scene_image_path(scene))
            created += 1
            messages.append(f"Created {path}")
        except Exception as exc:
            messages.append(f"Failed {scene.label}: {exc}")
            break
    return created, messages


def low_quality_scenes(limit: int | None = None) -> list[Scene]:
    """Existing assets that are too small/low-detail and worth regenerating."""
    seen_paths: set[Path] = set()
    found: list[Scene] = []
    for raw_lesson in load_topics().values():
        for raw_scene in raw_lesson["scenes"]:
            scene = Scene(**raw_scene)
            path = scene_image_path(scene)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            if path.exists() and path.stat().st_size < MIN_GOOD_IMAGE_BYTES:
                found.append(scene)
                if limit and len(found) >= limit:
                    return found
    return found


def upgrade_low_quality_assets(limit: int = 30) -> tuple[int, list[str]]:
    """Regenerate the existing tiny/low-quality images with a fresh, higher-
    quality result. Clears the old file + provider markers first so the marker
    check no longer blocks a better image. Good images are left untouched."""
    if not image_api_ready() and not pollinations_image_ready() and not google_image_ready():
        return 0, ["No image provider is ready (enable Pollinations or set an API key)."]

    scenes = low_quality_scenes(limit)
    if not scenes:
        return 0, ["All images already look good — nothing to upgrade."]

    upgraded = 0
    messages: list[str] = []
    for scene in scenes:
        path = scene_image_path(scene)
        old_size = path.stat().st_size if path.exists() else 0
        # Drop the old file and its markers so regeneration is not blocked.
        for marker in (
            _ai_marker_path(path),
            _provider_marker_path(path, "pollinations"),
            _provider_marker_path(path, "google"),
            _provider_marker_path(path, "openverse"),
            _provider_marker_path(path, "wikimedia"),
        ):
            marker.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        try:
            new_path = _generate_best_available_scene_image(scene, path)
            new_size = new_path.stat().st_size
            if new_size > old_size:
                upgraded += 1
                messages.append(f"Upgraded {scene.label}: {old_size // 1024}KB -> {new_size // 1024}KB")
            else:
                messages.append(f"Kept {scene.label}: {new_size // 1024}KB")
        except Exception as exc:
            messages.append(f"Failed {scene.label}: {exc}")
    return upgraded, messages
