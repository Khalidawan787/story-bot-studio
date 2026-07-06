from __future__ import annotations

import subprocess
from pathlib import Path

from .config import settings
from .image_assets import ensure_scene_asset, generate_fresh_image
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


def escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace("\n", " ")
    )


def scene_color(scene: Scene) -> str:
    return PALETTE[sum(ord(char) for char in scene.label) % len(PALETTE)]


def accent_color(scene: Scene) -> str:
    return SOFT_PALETTE[(sum(ord(char) for char in scene.label) + 3) % len(SOFT_PALETTE)]


def label_symbol(scene: Scene) -> str:
    symbols = {
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
        "ten": "10",
        "red": "RED",
        "blue": "BLUE",
        "yellow": "YELLOW",
        "green": "GREEN",
        "circle": "O",
        "square": "[]",
        "triangle": "TRI",
        "rectangle": "RECT",
        "star": "*",
        "heart": "<3",
        "apple": "APPLE",
        "banana": "BANANA",
        "carrot": "CARROT",
        "car": "CAR",
        "bus": "BUS",
        "sun": "SUN",
        "moon": "MOON",
    }
    return symbols.get(scene.label.lower(), scene.label)


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


def create_auto_scene_image(scene: Scene, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    label = escape_drawtext(scene.label)
    symbol = escape_drawtext(label_symbol(scene))
    color = scene_color(scene)
    accent = accent_color(scene)
    illustration = _illustration_filter(scene)
    vf = (
        f"{illustration},"
        "drawbox=x=70:y=68:w=940:h=138:color=black@0.20:t=fill,"
        f"drawtext=text='{label}'{font_arg}:fontsize=92:fontcolor=white:borderw=7:bordercolor=black:x=(w-text_w)/2:y=92,"
        "drawbox=x=130:y=1450:w=820:h=128:color=0xffd43b@0.96:t=fill,"
        f"drawtext=text='Kids Learning'{font_arg}:fontsize=58:fontcolor=0x202020:x=(w-text_w)/2:y=1486"
    )
    command = [
        ffmpeg_bin(),
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=1080x1920:d=1",
        "-frames:v",
        "1",
        "-update",
        "1",
        "-vf",
        vf,
        str(output_path),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    return output_path


def resolve_scene_image(scene: Scene, run_dir: Path, channel=None) -> Path | None:
    genre = channel is not None and not getattr(channel, "builtin", False)

    # Genre channels: generate a BRAND-NEW image every render (no reuse).
    if settings.enable_fresh_images and genre:
        try:
            fresh = run_dir / "gen" / Path(scene.image).name
            return generate_fresh_image(scene, fresh, channel)
        except Exception:
            pass

    source = ensure_scene_asset(scene, channel) or (settings.root / scene.image)
    if source.exists():
        return source
    return create_auto_scene_image(scene, run_dir / "auto_assets" / f"{scene.label.lower()}.jpg")


def create_thumbnail(lesson: Lesson, image_path: Path | None, output_path: Path, channel=None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    title = escape_drawtext(lesson.title)
    is_genre = channel is not None and not getattr(channel, "builtin", False)
    top_label = escape_drawtext(channel.name.upper()) if is_genre else "KIDS LEARNING"
    subtitle = escape_drawtext("Fun learning for kids")
    labels = escape_drawtext("  -  ".join(scene.label for scene in lesson.scenes[:4]))

    if image_path and image_path.exists():
        input_args = ["-loop", "1", "-i", str(image_path)]
        vf = (
            "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,"
            "eq=saturation=1.12:contrast=1.05,"
            "drawbox=x=0:y=0:w=1280:h=720:color=black@0.18:t=fill,"
            "drawbox=x=46:y=44:w=520:h=62:color=0xffd43b@0.95:t=fill,"
            f"drawtext=text='{top_label}'{font_arg}:fontsize=34:fontcolor=0x202020:x=74:y=58,"
            "drawbox=x=42:y=438:w=790:h=210:color=black@0.48:t=fill,"
            f"drawtext=text='{title.upper()}'{font_arg}:fontsize=74:fontcolor=white:borderw=4:bordercolor=black:x=70:y=468,"
            f"drawtext=text='{labels}'{font_arg}:fontsize=44:fontcolor=0xffd43b:borderw=3:bordercolor=black:x=76:y=572"
        )
    else:
        input_args = ["-f", "lavfi", "-i", "color=c=0x45aaf2:s=1280x720:d=1"]
        vf = (
            "drawbox=x=42:y=438:w=790:h=210:color=black@0.38:t=fill,"
            f"drawtext=text='{title.upper()}'{font_arg}:fontsize=80:fontcolor=white:x=(w-text_w)/2:y=270,"
            f"drawtext=text='{subtitle}'{font_arg}:fontsize=42:fontcolor=white:x=(w-text_w)/2:y=390"
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
