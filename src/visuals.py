from __future__ import annotations

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
    """Guaranteed unique, text-free local illustration when all online sources fail."""
    width, height = (1920, 1080) if landscape else (1080, 1920)
    genre = getattr(channel, "genre", "kids") if channel is not None else "kids"
    palettes = {
        "kids": [("0x51cf66", "0x74c0fc", "0xffd43b"), ("0xff8787", "0x4dabf7", "0xffd43b")],
        "crime": [("0x101828", "0x334155", "0xb45309"), ("0x111827", "0x374151", "0x991b1b")],
        "horror": [("0x09090b", "0x27272a", "0x7f1d1d"), ("0x18181b", "0x3f3f46", "0x581c87")],
        "love": [("0x4c1d95", "0xbe185d", "0xfda4af"), ("0x831843", "0xdb2777", "0xfbcfe8")],
        "motivation": [("0x1e3a8a", "0x0369a1", "0xf59e0b"), ("0x064e3b", "0x0f766e", "0xfbbf24")],
    }
    options = palettes.get(genre, palettes["kids"])
    seed = sum(ord(char) for char in scene.label) + time.time_ns() + variant * 7919
    bg, mid, accent = options[seed % len(options)]
    shift = int(seed % max(60, width // 5))
    vf = ",".join([
        f"drawbox=x=0:y=0:w={width}:h={height}:color={bg}:t=fill",
        f"drawbox=x={width//10 + shift}:y={height//8}:w={width*7//10}:h={height*3//4}:color={mid}@0.72:t=fill",
        f"drawbox=x={width//5}:y={height//4 + shift//3}:w={width*3//5}:h={height//3}:color={accent}@0.48:t=fill",
        f"drawbox=x={width//3}:y={height//6}:w={width//3}:h={height*2//3}:color=white@0.08:t=fill",
        "noise=alls=7:allf=t+u",
        "vignette=0.45",
    ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_bin(), "-y", "-f", "lavfi", "-i",
        f"color=c=black:s={width}x{height}:d=1",
        "-frames:v", "1", "-update", "1", "-vf", vf, str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
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
            break  # each provider already retried; continue immediately with local art

    for variant in range(1, 5):
        emergency = run_dir / "emergency" / f"{stem}_{time.time_ns()}_{variant}.jpg"
        try:
            create_emergency_scene_image(
                scene, emergency, channel, landscape=landscape, variant=variant,
            )
            if claim_unique_image(emergency, channel_id, scene.label):
                return emergency
            emergency.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"emergency {variant}: {exc}")

    detail = " | ".join(errors) if errors else "No usable image was found."
    raise RuntimeError(
        f"Unable to create any unique image for scene '{scene.label}'. {detail}"
    )


def create_thumbnail(lesson: Lesson, image_path: Path | None, output_path: Path, channel=None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    is_genre = channel is not None and not getattr(channel, "builtin", False)
    top_text = channel.name.upper() if is_genre else "KIDS LEARNING"
    title_file = _drawtext_file(output_path.with_suffix(".title.txt"), lesson.title.upper())
    top_file = _drawtext_file(output_path.with_suffix(".channel.txt"), top_text)
    subtitle_file = _drawtext_file(output_path.with_suffix(".subtitle.txt"), "Fun learning for kids")
    labels_file = _drawtext_file(
        output_path.with_suffix(".labels.txt"),
        "  -  ".join(scene.label for scene in lesson.scenes[:4]),
    )

    if image_path and image_path.exists():
        input_args = ["-loop", "1", "-i", str(image_path)]
        vf = (
            "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
            "eq=saturation=1.12:contrast=1.05,"
            "drawbox=x=0:y=0:w=1280:h=720:color=black@0.18:t=fill,"
            "drawbox=x=46:y=44:w=520:h=62:color=0xffd43b@0.95:t=fill,"
            f"drawtext=textfile='{top_file}'{font_arg}:fontsize=34:fontcolor=0x202020:x=74:y=58,"
            "drawbox=x=42:y=438:w=790:h=210:color=black@0.48:t=fill,"
            f"drawtext=textfile='{title_file}'{font_arg}:fontsize=74:fontcolor=white:borderw=4:bordercolor=black:x=70:y=468,"
            f"drawtext=textfile='{labels_file}'{font_arg}:fontsize=44:fontcolor=0xffd43b:borderw=3:bordercolor=black:x=76:y=572"
        )
    else:
        input_args = ["-f", "lavfi", "-i", "color=c=0x45aaf2:s=1280x720:d=1"]
        vf = (
            "drawbox=x=42:y=438:w=790:h=210:color=black@0.38:t=fill,"
            f"drawtext=textfile='{title_file}'{font_arg}:fontsize=80:fontcolor=white:x=(w-text_w)/2:y=270,"
            f"drawtext=textfile='{subtitle_file}'{font_arg}:fontsize=42:fontcolor=white:x=(w-text_w)/2:y=390"
        )

    command = [
        ffmpeg_bin(),
        "-y",
        *input_args,
        "-frames:v",
        "1",
        "-update",
        "1",
        "-vf",
        vf,
        str(output_path),
    ]
    subprocess.run(command, check=True)
    return output_path
