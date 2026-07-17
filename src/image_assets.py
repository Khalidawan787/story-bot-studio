from __future__ import annotations

import base64
import html
import importlib.util
import json
import random
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .db import api_usage_today, record_api_usage
from .lessons import load_topics
from .models import Scene


# A real 1080x1920 illustration is ~300KB-2MB. Anything much smaller is a
# low-detail / near-broken result we should retry rather than keep.
MIN_GOOD_IMAGE_BYTES = 130_000


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
    if category in {"shapes", "numbers", "alphabet", "fruits", "vegetables", "vehicles", "science"}:
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
    client = OpenAI(api_key=settings.openai_api_key)
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
    label = scene.label.strip()
    generic = label.lower().startswith(("part ", "scene ", "chapter ", "step "))
    if generic and scene.image_prompt:
        words = [word.strip(".,:;!?()[]") for word in scene.image_prompt.split()]
        return " ".join(word for word in words if len(word) > 2)[:80]
    return label[:120]


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


def generate_openverse_scene_image(
    scene: Scene,
    output_path: Path,
    channel=None,
    landscape: bool = False,
    attempts: int = 6,
) -> Path:
    """Download a CC0/Public Domain image from Openverse without an API key."""
    params = urllib.parse.urlencode({
        "q": _openverse_query(scene, channel),
        "license": "cc0,pdm",
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
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Openverse search failed: {exc}") from exc

    candidates = []
    for item in payload.get("results", []):
        if str(item.get("license", "")).lower() not in {"cc0", "pdm"}:
            continue
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        if width and height:
            if landscape and width <= height:
                continue
            if not landscape and height <= width:
                continue
        candidates.append(item)
    if not candidates:
        raise RuntimeError("Openverse returned no matching CC0/Public Domain images.")
    random.shuffle(candidates)

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
    """Download a Public Domain/CC0 image from Wikimedia Commons without a key."""
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
        if not any(token in license_name for token in ("public domain", "cc0", "cc zero")):
            continue
        width, height = int(info.get("width") or 0), int(info.get("height") or 0)
        if width and height:
            if landscape and width <= height:
                continue
            if not landscape and height <= width:
                continue
        if not str(info.get("mime", "")).startswith("image/"):
            continue
        candidates.append((page, info))
    if not candidates:
        raise RuntimeError("Wikimedia returned no matching Public Domain/CC0 images.")
    random.shuffle(candidates)

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

def generate_pollinations_scene_image(scene: Scene, output_path: Path, channel=None,
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
    for i in range(max(1, attempts)):
        seed = (base_seed + i * 97) % 1_000_000
        params = urllib.parse.urlencode(
            {
                "width": "1920" if landscape else "1080",
                "height": "1080" if landscape else "1920",
                "seed": str(seed),
                "model": "flux",
                "enhance": "true",
                "nologo": "true",
                "safe": "true",
            }
        )
        url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?{params}"
        request = urllib.request.Request(url, headers={"User-Agent": "KidsLearningBot/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read()
                content_type = response.headers.get("Content-Type", "")
        except Exception as exc:  # network hiccup — try the next seed
            last_error = str(exc)
            continue
        if "image" not in content_type.lower() or len(data) < 20_000:
            last_error = f"invalid image (type={content_type}, {len(data)}B)"
            continue
        if best_data is None or len(data) > len(best_data):
            best_data = data
        if len(data) >= MIN_GOOD_IMAGE_BYTES:
            break  # good enough — stop early

    if best_data is None:
        raise RuntimeError(f"Pollinations did not return a valid image. {last_error}")
    if len(best_data) < MIN_GOOD_IMAGE_BYTES:
        raise RuntimeError(
            f"Pollinations images were below the quality threshold "
            f"({len(best_data)}B < {MIN_GOOD_IMAGE_BYTES}B)."
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(best_data)
    _write_provider_marker(output_path, "pollinations")
    return output_path


def _generate_best_available_scene_image(scene: Scene, output_path: Path, channel=None) -> Path:
    errors: list[str] = []
    genre = _is_genre(channel)
    # Genre channels use free Pollinations first (no OpenAI cost, correct style).
    if genre and pollinations_image_ready():
        try:
            return generate_pollinations_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Pollinations: {exc}")
    if openverse_image_ready():
        try:
            return generate_openverse_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Openverse: {exc}")
    if wikimedia_image_ready():
        try:
            return generate_wikimedia_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Wikimedia: {exc}")
    if image_api_ready():
        try:
            return generate_openai_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"OpenAI: {exc}")
    if pollinations_image_ready() and not genre:
        try:
            return generate_pollinations_scene_image(scene, output_path, channel)
        except Exception as exc:
            errors.append(f"Pollinations: {exc}")
    if google_image_ready():
        try:
            return generate_google_scene_image(scene, output_path)
        except Exception as exc:
            errors.append(f"Google: {exc}")
    if errors:
        raise RuntimeError(" | ".join(errors))
    raise RuntimeError("No AI/Google image provider configured.")


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
    if settings.auto_generate_missing_images and google_image_ready():
        try:
            return generate_google_scene_image(scene, output_path)
        except Exception:
            pass
    return None


def generate_fresh_image(
    scene: Scene,
    output_path: Path,
    channel=None,
    landscape: bool = False,
) -> Path:
    """Generate a real image with provider fallbacks; never make a text card."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    if pollinations_image_ready():
        try:
            return generate_pollinations_scene_image(
                scene, output_path, channel, fresh=True, attempts=5,
                landscape=landscape,
            )
        except Exception as exc:
            errors.append(f"Pollinations: {exc}")
    if openverse_image_ready():
        try:
            return generate_openverse_scene_image(
                scene, output_path, channel, landscape=landscape, attempts=6,
            )
        except Exception as exc:
            errors.append(f"Openverse: {exc}")
    if wikimedia_image_ready():
        try:
            return generate_wikimedia_scene_image(
                scene, output_path, channel, landscape=landscape, attempts=8,
            )
        except Exception as exc:
            errors.append(f"Wikimedia: {exc}")
    if image_api_ready():
        try:
            return generate_openai_scene_image(scene, output_path, channel, landscape=landscape)
        except Exception as exc:
            errors.append(f"OpenAI: {exc}")
    if google_image_ready():
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
