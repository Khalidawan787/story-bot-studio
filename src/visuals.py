from __future__ import annotations

import math
import random
import subprocess
import time
from pathlib import Path

from .config import settings
from .db import claim_unique_image
from .image_assets import generate_fresh_image
from .models import Lesson, Scene


PALETTE = ["0xff6060", "0x45aaf2", "0x26de81", "0xfed330", "0xa55eea", "0xfa8231"]
SOFT_PALETTE = ["0xff6b6b", "0x4dabf7", "0x51cf66", "0xffd43b", "0xcc5de8", "0xff922b"]


def ffmpeg_bin() -> str:
    return settings.ffmpeg_bin


def ffprobe_bin() -> str:
    return settings.ffprobe_bin


def font_path() -> str:
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return ""


def escape_filter_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:")


def _drawtext_file(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return escape_filter_path(str(path))


def scene_color(scene: Scene) -> str:
    return PALETTE[sum(ord(char) for char in scene.label) % len(PALETTE)]


def accent_color(scene: Scene) -> str:
    return SOFT_PALETTE[(sum(ord(char) for char in scene.label) + 3) % len(SOFT_PALETTE)]


def _scene_theme(scene: Scene) -> str:
    image = scene.image.replace("\\", "/").lower()
    if "/opposites/" in image:
        return "opposites"
    if "/shapes/" in image:
        return "shapes"
    if "/science/" in image:
        return "science"
    if "/vehicles/" in image:
        return "vehicles"
    if "/fruits/" in image:
        return "fruits"
    if "/vegetables/" in image:
        return "vegetables"
    if "/alphabet/" in image:
        return "alphabet"
    if "/colors/" in image:
        return "colors"
    if "/numbers/" in image:
        return "numbers"
    return "general"


def _illustration_filter(scene: Scene) -> str:
    label = scene.label.lower()
    color = scene_color(scene)
    accent = accent_color(scene)
    theme = _scene_theme(scene)

    sky = (
        "drawbox=x=0:y=0:w=1080:h=1920:color=0x9be7ff:t=fill,"
        "drawbox=x=0:y=1350:w=1080:h=570:color=0x8ce99a:t=fill,"
        "drawbox=x=70:y=110:w=170:h=170:color=0xfff3bf:t=fill,"
        "drawbox=x=80:y=120:w=150:h=150:color=0xffd43b:t=fill"
    )

    if theme == "opposites":
        if label in {"short", "small", "low", "down"}:
            return (
                f"{sky},"
                "drawbox=x=210:y=760:w=180:h=520:color=0x4dabf7:t=fill,"
                "drawbox=x=690:y=1040:w=180:h=240:color=0xff8787:t=fill,"
                "drawbox=x=185:y=690:w=230:h=90:color=0x228be6:t=fill,"
                "drawbox=x=665:y=970:w=230:h=90:color=0xfa5252:t=fill,"
                "drawbox=x=250:y=1280:w=100:h=150:color=0x343a40:t=fill,"
                "drawbox=x=730:y=1280:w=100:h=150:color=0x343a40:t=fill,"
                f"drawbox=x=620:y=930:w=330:h=560:color={accent}@0.28:t=fill"
            )
        if label in {"tall", "big", "high", "up"}:
            return (
                f"{sky},"
                "drawbox=x=210:y=1040:w=180:h=240:color=0x4dabf7:t=fill,"
                "drawbox=x=690:y=700:w=180:h=580:color=0xff8787:t=fill,"
                "drawbox=x=185:y=970:w=230:h=90:color=0x228be6:t=fill,"
                "drawbox=x=665:y=630:w=230:h=90:color=0xfa5252:t=fill,"
                "drawbox=x=250:y=1280:w=100:h=150:color=0x343a40:t=fill,"
                "drawbox=x=730:y=1280:w=100:h=150:color=0x343a40:t=fill,"
                f"drawbox=x=620:y=590:w=330:h=900:color={accent}@0.25:t=fill"
            )
        if label in {"hot", "day"}:
            return f"{sky},drawbox=x=360:y=560:w=360:h=360:color=0xffd43b:t=fill,drawbox=x=390:y=590:w=300:h=300:color=0xff922b:t=fill"
        if label in {"cold", "wet"}:
            return f"{sky},drawbox=x=130:y=420:w=820:h=850:color=0xd0ebff@0.65:t=fill,drawbox=x=260:y=650:w=150:h=150:color=white:t=fill,drawbox=x=470:y=760:w=150:h=150:color=white:t=fill,drawbox=x=650:y=620:w=150:h=150:color=white:t=fill"
        if label in {"happy", "open", "full"}:
            return f"{sky},drawbox=x=250:y=540:w=580:h=580:color=0xffd43b:t=fill,drawbox=x=370:y=720:w=80:h=80:color=0x202020:t=fill,drawbox=x=630:y=720:w=80:h=80:color=0x202020:t=fill,drawbox=x=390:y=920:w=300:h=80:color=0xff6b6b:t=fill"
        return f"{sky},drawbox=x=230:y=620:w=620:h=560:color={accent}:t=fill,drawbox=x=310:y=700:w=460:h=400:color=white@0.55:t=fill"

    if theme == "shapes":
        if label == "circle" or label == "oval":
            return f"{sky},drawbox=x=255:y=540:w=570:h=570:color={color}:t=fill,drawbox=x=300:y=585:w=480:h=480:color={accent}:t=fill"
        if label == "triangle":
            return f"{sky},drawbox=x=240:y=990:w=600:h=120:color={color}:t=fill,drawbox=x=330:y=820:w=420:h=170:color={color}:t=fill,drawbox=x=420:y=650:w=240:h=170:color={color}:t=fill"
        if label == "star":
            return f"{sky},drawbox=x=450:y=480:w=180:h=680:color={color}:t=fill,drawbox=x=240:y=720:w=600:h=180:color={accent}:t=fill,drawbox=x=330:y=580:w=420:h=460:color={color}@0.72:t=fill"
        if label == "heart":
            return f"{sky},drawbox=x=290:y=600:w=220:h=220:color=0xff6b6b:t=fill,drawbox=x=570:y=600:w=220:h=220:color=0xff6b6b:t=fill,drawbox=x=360:y=760:w=360:h=360:color=0xff6b6b:t=fill"
        return f"{sky},drawbox=x=250:y=560:w=580:h=580:color={color}:t=fill,drawbox=x=320:y=630:w=440:h=440:color=white@0.25:t=fill"

    if theme == "vehicles":
        return (
            f"{sky},drawbox=x=155:y=920:w=770:h=260:color={color}:t=fill,"
            "drawbox=x=280:y=760:w=430:h=180:color=0x74c0fc:t=fill,"
            "drawbox=x=230:y=1130:w=160:h=160:color=0x212529:t=fill,"
            "drawbox=x=690:y=1130:w=160:h=160:color=0x212529:t=fill,"
            "drawbox=x=270:y=1170:w=80:h=80:color=0xf8f9fa:t=fill,"
            "drawbox=x=730:y=1170:w=80:h=80:color=0xf8f9fa:t=fill"
        )

    if theme in {"fruits", "vegetables"}:
        return (
            f"{sky},drawbox=x=270:y=590:w=540:h=540:color={color}:t=fill,"
            "drawbox=x=470:y=470:w=140:h=180:color=0x2f9e44:t=fill,"
            "drawbox=x=430:y=500:w=260:h=100:color=0x69db7c:t=fill,"
            "drawbox=x=350:y=700:w=130:h=130:color=white@0.22:t=fill"
        )

    if theme == "science":
        if label in {"sun", "stars"}:
            return f"drawbox=x=0:y=0:w=1080:h=1920:color=0x1c7ed6:t=fill,drawbox=x=310:y=520:w=460:h=460:color=0xffd43b:t=fill,drawbox=x=360:y=570:w=360:h=360:color=0xff922b:t=fill"
        if label in {"moon", "earth", "solar system"}:
            return f"drawbox=x=0:y=0:w=1080:h=1920:color=0x16213e:t=fill,drawbox=x=270:y=560:w=540:h=540:color=0x74c0fc:t=fill,drawbox=x=360:y=690:w=180:h=160:color=0x51cf66:t=fill,drawbox=x=560:y=820:w=210:h=140:color=0x51cf66:t=fill"
        return f"{sky},drawbox=x=320:y=620:w=440:h=620:color={accent}:t=fill,drawbox=x=410:y=730:w=260:h=360:color=white@0.45:t=fill"

    if theme == "alphabet":
        return f"{sky},drawbox=x=260:y=560:w=560:h=560:color=0xffd43b:t=fill,drawbox=x=320:y=620:w=440:h=440:color=white@0.65:t=fill"

    if theme == "numbers":
        return f"{sky},drawbox=x=230:y=610:w=160:h=160:color={color}:t=fill,drawbox=x=460:y=610:w=160:h=160:color={accent}:t=fill,drawbox=x=690:y=610:w=160:h=160:color={color}:t=fill,drawbox=x=345:y=860:w=160:h=160:color={accent}:t=fill,drawbox=x=575:y=860:w=160:h=160:color={color}:t=fill"

    if theme == "colors":
        return f"{sky},drawbox=x=230:y=560:w=620:h=620:color={color}:t=fill,drawbox=x=310:y=640:w=460:h=460:color=white@0.25:t=fill"

    return f"{sky},drawbox=x=250:y=560:w=580:h=580:color={color}:t=fill,drawbox=x=330:y=640:w=420:h=420:color={accent}:t=fill"


# Clean, friendly (background, card, accent) palettes for the fallback card
# used ONLY when no real/AI image is available. Chosen so the card looks
# intentional — never like a broken placeholder.
def create_emergency_scene_image(
    scene: Scene,
    output_path: Path,
    channel=None,
    landscape: bool = False,
    variant: int = 1,
) -> Path:
    """Create a unique text-free scenic illustration when every online source fails."""
    width, height = (1920, 1080) if landscape else (1080, 1920)
    genre = getattr(channel, "genre", "kids") if channel is not None else "kids"
    palettes = {
        "kids": ((91, 192, 235), (244, 222, 126), (70, 166, 88), (255, 196, 61)),
        "crime": ((30, 48, 75), (105, 82, 83), (20, 27, 39), (234, 179, 86)),
        "horror": ((35, 37, 63), (92, 70, 91), (20, 25, 34), (196, 203, 222)),
        "love": ((111, 76, 145), (245, 154, 154), (96, 57, 91), (255, 215, 153)),
        "motivation": ((48, 128, 178), (244, 185, 96), (43, 94, 83), (255, 225, 128)),
        "trending": ((25, 42, 66), (58, 96, 140), (16, 24, 38), (228, 106, 78)),
    }
    top, bottom, foreground, accent = palettes.get(genre, palettes["kids"])
    seed = sum(ord(char) for char in (scene.label + scene.image_prompt)) + time.time_ns() + variant * 7919
    rng = random.Random(seed)
    pixels = bytearray(width * height * 3)

    def hline(y: int, x1: int, x2: int, color: tuple[int, int, int]) -> None:
        if y < 0 or y >= height:
            return
        left, right = max(0, min(x1, x2)), min(width, max(x1, x2))
        if right <= left:
            return
        start = (y * width + left) * 3
        pixels[start:start + (right - left) * 3] = bytes(color) * (right - left)

    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        hline(y, 0, width, color)

    def ellipse(cx: int, cy: int, rx: int, ry: int, color: tuple[int, int, int]) -> None:
        for yy in range(max(0, cy - ry), min(height, cy + ry + 1)):
            dy = (yy - cy) / max(1, ry)
            dx = int(rx * math.sqrt(max(0.0, 1.0 - dy * dy)))
            hline(yy, cx - dx, cx + dx + 1, color)

    def triangle(cx: int, apex: int, half_base: int, base: int, color: tuple[int, int, int]) -> None:
        span = max(1, base - apex)
        for yy in range(max(0, apex), min(height, base)):
            half = int(half_base * ((yy - apex) / span))
            hline(yy, cx - half, cx + half + 1, color)

    def rect(x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int]) -> None:
        for yy in range(max(0, y1), min(height, y2)):
            hline(yy, x1, x2, color)

    celestial_y = height // 5
    ellipse(width * 4 // 5, celestial_y, width // 12, width // 12, accent)
    for _ in range(28 if genre in {"crime", "horror"} else 10):
        x, y = rng.randrange(width), rng.randrange(max(1, height // 2))
        radius = rng.randrange(2, max(3, width // 180))
        ellipse(x, y, radius, radius, (235, 235, 220))

    horizon = height * 3 // 5
    if genre in {"crime", "horror"}:
        for x in range(-width // 10, width, width // 8):
            building_h = rng.randrange(height // 8, height // 3)
            rect(x, horizon - building_h, x + width // 10, horizon, foreground)
            for wy in range(horizon - building_h + 30, horizon - 20, max(35, height // 20)):
                for wx in range(x + 18, x + width // 10 - 12, max(28, width // 35)):
                    if rng.random() > 0.45:
                        rect(wx, wy, wx + max(5, width // 120), wy + max(8, height // 90), accent)
        triangle(width // 2, horizon, width // 5, height, (52, 55, 65))
        for _ in range(7):
            tx = rng.randrange(width)
            triangle(tx, horizon - rng.randrange(height // 10, height // 4), width // 18, horizon, (15, 35, 32))
    else:
        ellipse(width // 4, horizon + height // 5, width * 3 // 5, height // 3, foreground)
        ellipse(width * 4 // 5, horizon + height // 4, width * 3 // 5, height // 3, tuple(min(255, c + 24) for c in foreground))
        for x in (width // 5, width // 2, width * 4 // 5):
            triangle(x, horizon - rng.randrange(height // 7, height // 4), width // 5, horizon + height // 12, (76, 112, 126))
        for _ in range(4):
            cx, cy = rng.randrange(width // 8, width * 7 // 8), rng.randrange(height // 9, height // 3)
            ellipse(cx, cy, width // 16, height // 45, (245, 246, 240))
            ellipse(cx + width // 18, cy, width // 18, height // 40, (245, 246, 240))
        if genre == "kids":
            for _ in range(5):
                bx, by = rng.randrange(width // 8, width * 7 // 8), rng.randrange(height // 3, horizon)
                ellipse(bx, by, width // 35, height // 35, rng.choice([(255, 105, 120), (255, 205, 70), (80, 170, 245)]))
                for yy in range(by + height // 35, min(height, by + height // 10)):
                    hline(yy, bx, bx + 2, (80, 80, 80))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    ppm_path = output_path.with_suffix(".ppm")
    ppm_path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + pixels)
    try:
        subprocess.run(
            [ffmpeg_bin(), "-y", "-i", str(ppm_path), "-frames:v", "1", str(output_path)],
            check=True, capture_output=True, text=True,
        )
    finally:
        ppm_path.unlink(missing_ok=True)
    return output_path

def resolve_scene_image(
    scene: Scene,
    run_dir: Path,
    channel=None,
    require_real_image: bool = True,
    landscape: bool = False,
) -> Path:
    """Generate and atomically claim image bytes that no earlier video used."""
    channel_id = getattr(channel, "id", "kids") if channel is not None else "kids"
    errors: list[str] = []
    base_name = Path(scene.image).name or "scene.jpg"
    stem = Path(base_name).stem
    suffix = Path(base_name).suffix or ".jpg"

    for attempt in range(1, 5):
        fresh = run_dir / "gen" / f"{stem}_fresh_{attempt}{suffix}"
        try:
            generated = generate_fresh_image(
                scene, fresh, channel, landscape=landscape,
            )
            if claim_unique_image(generated, channel_id, scene.label):
                return generated
            errors.append(f"attempt {attempt}: duplicate image rejected")
            for extra in (
                generated,
                generated.with_suffix(generated.suffix + ".ai"),
                generated.with_suffix(generated.suffix + ".pollinations"),
                generated.with_suffix(generated.suffix + ".google"),
                generated.with_suffix(generated.suffix + ".openverse"),
                generated.with_suffix(generated.suffix + ".openverse.json"),
            ):
                extra.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"online providers: {exc}")
            break  # every configured real-image provider has already been tried

    # Never substitute cards, geometric art, or placeholders for a real image.
    # Keeping the failed render out of the upload queue is safer than publishing
    # a visibly broken video.
    detail = " | ".join(errors) if errors else "No usable image was found."
    raise RuntimeError(
        f"Real image unavailable for scene '{scene.label}'. Video stopped to prevent "
        f"placeholder artwork from being published. {detail}"
    )


def create_thumbnail(lesson: Lesson, image_path: Path | None, output_path: Path,
                     channel=None, content_type: str = "short") -> Path:
    """Kept for callers that already import it; the real work lives in thumbnails.py."""
    from .thumbnails import create_thumbnail as build_thumbnail

    return build_thumbnail(lesson, image_path, output_path, channel, content_type)
