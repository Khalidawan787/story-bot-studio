from __future__ import annotations

import base64
import importlib.util
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .lessons import load_topics
from .models import Scene


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


def generate_openai_scene_image(scene: Scene, output_path: Path, channel=None) -> Path:
    from openai import OpenAI

    output_path.parent.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=settings.openai_api_key)
    result = client.images.generate(
        model=settings.openai_image_model,
        prompt=_prompt_for_scene(scene, channel),
        size=settings.openai_image_size,
        quality=settings.openai_image_quality,
    )
    image_base64 = result.data[0].b64_json
    if not image_base64:
        raise RuntimeError("OpenAI image response did not include image data.")
    output_path.write_bytes(base64.b64decode(image_base64))
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


def generate_pollinations_scene_image(scene: Scene, output_path: Path, channel=None) -> Path:
    if _is_genre(channel):
        # Genre videos must NOT get kid-friendly styling.
        prompt = _prompt_for_scene(scene, channel) + " High quality, cinematic, vertical 9:16."
    else:
        prompt = (
            _prompt_for_scene(scene, channel)
            + " High quality, kid friendly, colorful, professional YouTube Shorts illustration."
        )
    encoded_prompt = urllib.parse.quote(prompt)
    params = urllib.parse.urlencode(
        {
            "width": "1080",
            "height": "1920",
            "seed": str(abs(hash(scene.image)) % 1_000_000),
            "model": "flux",
            "enhance": "true",
            "nologo": "true",
            "safe": "true",
        }
    )
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "KidsLearningBot/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read()
        content_type = response.headers.get("Content-Type", "")
    if "image" not in content_type.lower() or len(data) < 20_000:
        raise RuntimeError(f"Pollinations did not return a valid image. Content-Type={content_type}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
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


def generate_free_asset_pack(limit: int = 50) -> tuple[int, list[str]]:
    from .visuals import create_auto_scene_image

    created = 0
    messages: list[str] = []
    for scene in unique_missing_scenes(limit):
        path = scene_image_path(scene)
        try:
            create_auto_scene_image(scene, path)
            created += 1
            messages.append(f"Created free asset {path}")
        except Exception as exc:
            messages.append(f"Failed {scene.label}: {exc}")
            break
    return created, messages
