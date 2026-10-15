"""Free "2.5D" animation for cartoon scenes: no GPU, no paid API.

A still picture is cut into two layers — the character in front and the scenery
behind it — and the layers are moved separately. The background drifts like a
camera move, the character breathes, bobs and sways on top of it (landing
with a cartoon squash), and a few hearts, stars, snowflakes, petals,
raindrops, bubbles or sparkles - whichever suit the scene - float across. It is not real character animation (nothing
walks or talks), but the picture stops looking like a slideshow.

The cut-out uses a small open model (IS-Net, Apache-2.0) run on the CPU through
OpenCV, so it works on a plain GitHub runner. Anything that fails here raises,
and the renderer answers by drawing that one scene the old way.
"""
from __future__ import annotations

import math
import re
import urllib.request
from pathlib import Path

from .config import settings
from .models import Scene

_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx"
_MODEL_PATH = settings.root / "data" / "models" / "isnet-general-use.onnx"
_MODEL_MIN_BYTES = 150_000_000
_NET = None

# The character layer is drawn slightly larger than the frame, so it can move
# without its own edge (or the patched hole behind it) ever coming into view.
_FRONT_SCALE = 1.07


def animation_enabled(channel=None) -> bool:
    """True when this channel's scenes should be animated in layers."""
    channel_id = str(getattr(channel, "id", "") or "kids").lower()
    return channel_id in settings.animated_channels or getattr(channel, "visuals", "") == "cartoon"


def _net():
    global _NET
    if _NET is None:
        import cv2

        if not _MODEL_PATH.is_file() or _MODEL_PATH.stat().st_size < _MODEL_MIN_BYTES:
            _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            partial = _MODEL_PATH.with_suffix(".part")
            request = urllib.request.Request(_MODEL_URL, headers={"User-Agent": "StoryBotStudio/1.0"})
            with urllib.request.urlopen(request, timeout=300) as response, partial.open("wb") as handle:
                while chunk := response.read(1 << 20):
                    handle.write(chunk)
            if partial.stat().st_size < _MODEL_MIN_BYTES:
                partial.unlink(missing_ok=True)
                raise RuntimeError("cut-out model download was incomplete")
            partial.replace(_MODEL_PATH)
        _NET = cv2.dnn.readNetFromONNX(str(_MODEL_PATH))
    return _NET


def prepare_layers(image_path: Path, run_dir: Path, stem: str, width: int, height: int) -> tuple[Path, Path]:
    """Split a scene image into (background, character) files at frame size.

    Raises when the picture has no clear subject — a plain landscape, or one
    object filling the whole frame — because moving a bad cut-out looks worse
    than a still.
    """
    import cv2
    import numpy as np

    image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"cannot read {image_path.name}")
    # Same "cover" fit the normal renderer uses, so framing does not change.
    scale = max(width / image.shape[1], height / image.shape[0])
    resized = cv2.resize(
        image, (max(width, round(image.shape[1] * scale)), max(height, round(image.shape[0] * scale))),
        interpolation=cv2.INTER_LANCZOS4,
    )
    top, left = (resized.shape[0] - height) // 2, (resized.shape[1] - width) // 2
    frame = resized[top:top + height, left:left + width]

    blob = cv2.resize(frame, (1024, 1024), interpolation=cv2.INTER_AREA)
    blob = cv2.cvtColor(blob, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0 - 0.5
    net = _net()
    net.setInput(blob.transpose(2, 0, 1)[None])
    mask = net.forward()[0, 0]
    low, high = float(mask.min()), float(mask.max())
    if high - low < 1e-6:
        raise RuntimeError("no subject found")
    mask = cv2.resize((mask - low) / (high - low), (width, height), interpolation=cv2.INTER_LINEAR)

    solid = (mask > 0.5).astype(np.uint8)
    coverage = float(solid.mean())
    if not (0.04 <= coverage <= 0.80):
        raise RuntimeError(f"subject covers {coverage:.0%} of the picture")

    # Background: paint over where the character stood. Done small and blurred,
    # since the character layer hides nearly all of it and only a sliver ever
    # peeks out while the layers move apart.
    small_w, small_h = width // 4, height // 4
    hole = cv2.dilate(solid, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41)))
    patched = cv2.inpaint(
        cv2.resize(frame, (small_w, small_h), interpolation=cv2.INTER_AREA),
        cv2.resize(hole, (small_w, small_h), interpolation=cv2.INTER_NEAREST),
        5, cv2.INPAINT_TELEA,
    )
    patched = cv2.GaussianBlur(cv2.resize(patched, (width, height), interpolation=cv2.INTER_CUBIC), (0, 0), 6)
    blend = cv2.GaussianBlur(hole.astype(np.float32), (0, 0), 8)[..., None]
    background = (frame * (1 - blend) + patched * blend).astype(np.uint8)

    # Character: soft edge, pulled in a touch so no background fringe travels with it.
    alpha = np.clip((mask - 0.35) / 0.4, 0, 1)
    alpha = cv2.GaussianBlur(cv2.erode(alpha, np.ones((3, 3), np.uint8)), (0, 0), 1.2)
    front = np.dstack([frame, (alpha * 255).astype(np.uint8)])

    out_dir = run_dir / "layers"
    out_dir.mkdir(parents=True, exist_ok=True)
    back_path, front_path = out_dir / f"{stem}_back.jpg", out_dir / f"{stem}_front.png"
    cv2.imwrite(str(back_path), background, [cv2.IMWRITE_JPEG_QUALITY, 95])
    cv2.imwrite(str(front_path), front)
    return back_path, front_path


# Floating extras chosen from what the scene is about. Words are English or
# Roman Urdu; a scene matching none of them gets bubbles or sparkles as before.
_THEMES = (
    ("star", {"night", "star", "moon", "space", "planet", "rocket", "sleep", "bedtime", "dream",
              "raat", "chand", "sitara", "sitare", "taare", "tara"}),
    ("snow", {"snow", "winter", "cold", "ice", "snowman", "frozen", "barf", "sardi", "thand"}),
    ("drop", {"rain", "raining", "rainy", "cloud", "storm", "umbrella", "puddle", "baarish", "barish", "badal"}),
    ("petal", {"flower", "garden", "tree", "leaf", "leaves", "spring", "butterfly", "bee", "park", "plant",
               "phool", "bagh", "baagh", "patta", "pattay", "darakht", "titli"}),
    ("heart", {"love", "friend", "friendship", "hug", "kind", "kindness", "share", "sharing",
               "mother", "mom", "mommy", "family", "care", "dost", "dosti", "pyar", "ammi", "maa", "abbu"}),
    ("bubble", {"fish", "sea", "ocean", "water", "swim", "bath", "river", "pani", "samandar", "machli", "nadi"}),
)
# kind -> (direction, how many, speed range as a share of frame height per
# second, sideways wobble as a share of frame width)
_MOTION = {
    "bubble": ("up", 7, (0.035, 0.095), 0.018),
    "heart": ("up", 6, (0.04, 0.09), 0.022),
    "sparkle": ("down", 7, (0.035, 0.095), 0.018),
    "star": ("down", 7, (0.025, 0.06), 0.010),
    "snow": ("down", 9, (0.04, 0.08), 0.025),
    "petal": ("down", 7, (0.05, 0.09), 0.035),
    "drop": ("down", 9, (0.35, 0.50), 0.002),
}


def scene_theme(scene: Scene, key: int) -> str:
    """Which floating extra suits this scene."""
    words = set(re.findall(r"[a-z]+", f"{scene.label} {scene.line} {scene.image_prompt}".lower()))
    words |= {word[:-1] for word in words if word.endswith("s")}
    for kind, vocabulary in _THEMES:
        if words & vocabulary:
            return kind
    return "bubble" if (key // 3) % 2 == 0 else "sparkle"


def _shape(kind: str, size: int, variant: int):
    """(alpha 0..1, BGR colour) for one sprite of the given kind."""
    import cv2
    import numpy as np

    c = size // 2
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    dx, dy = (xx - c) / c, (yy - c) / c
    dist = np.sqrt(dx * dx + dy * dy)
    if kind == "bubble":
        # A thin bright rim, a faint body and one highlight.
        rim = np.exp(-((dist - 0.86) / 0.07) ** 2)
        body = 0.16 * (dist < 0.86)
        shine = np.exp(-(((dx + 0.35) / 0.2) ** 2 + ((dy + 0.4) / 0.2) ** 2))
        return np.clip(0.75 * rim + body + 0.8 * shine, 0, 1), (255, 245, 225)
    if kind == "sparkle":
        # Four-point sparkle: two thin crossed rays and a glowing core.
        rays = np.exp(-(np.abs(dx) / 0.05)) * np.exp(-(np.abs(dy) / 0.45)) \
            + np.exp(-(np.abs(dy) / 0.05)) * np.exp(-(np.abs(dx) / 0.45))
        core = np.exp(-(dist / 0.22) ** 2)
        return np.clip(rays + core, 0, 1) * (dist < 1.0), (170, 235, 255)
    if kind == "heart":
        x, y = dx * 1.25, -dy * 1.25 + 0.2
        inside = ((x * x + y * y - 1) ** 3 - x * x * y ** 3 <= 0).astype(np.float32)
        alpha = cv2.GaussianBlur(inside, (0, 0), max(0.8, size / 60))
        shine = 0.35 * np.exp(-(((dx + 0.38) / 0.16) ** 2 + ((dy + 0.25) / 0.16) ** 2))
        colour = ((150, 90, 255), (190, 140, 255), (120, 80, 240))[variant % 3]
        return np.clip(alpha + shine * alpha, 0, 1), colour

    # The rest are drawn on a mask; LINE_AA keeps their edges soft.
    mask = np.zeros((size, size), np.uint8)
    r = c * 0.92
    if kind == "star":
        points = []
        for i in range(10):
            radius = r if i % 2 == 0 else r * 0.45
            angle = -math.pi / 2 + i * math.pi / 5
            points.append((c + radius * math.cos(angle), c + radius * math.sin(angle)))
        cv2.fillPoly(mask, [np.round(np.array(points) * 16).astype(np.int32)], 255, cv2.LINE_AA, shift=4)
        colour = ((60, 215, 255), (130, 235, 255), (90, 200, 255))[variant % 3]
    elif kind == "snow":
        thick = max(1, size // 14)
        for i in range(3):
            angle = i * math.pi / 3
            ox, oy = r * math.cos(angle), r * math.sin(angle)
            cv2.line(mask, (round(c - ox), round(c - oy)), (round(c + ox), round(c + oy)), 255, thick, cv2.LINE_AA)
        cv2.circle(mask, (c, c), max(1, size // 9), 255, -1, cv2.LINE_AA)
        colour = (255, 250, 242)
    elif kind == "petal":
        angle = (35, 110, 160)[variant % 3]
        cv2.ellipse(mask, (c, c), (round(r), round(r * 0.5)), angle, 0, 360, 255, -1, cv2.LINE_AA)
        colour = ((200, 180, 255), (225, 205, 255), (170, 150, 250))[variant % 3]
    else:  # drop: a round bottom with a point on top, pale so it shows on blue sky
        cv2.circle(mask, (c, round(c + r * 0.35)), round(r * 0.55), 255, -1, cv2.LINE_AA)
        tip = np.array([[c, round(c - r)], [round(c - r * 0.52), round(c + r * 0.25)],
                        [round(c + r * 0.52), round(c + r * 0.25)]], np.int32)
        cv2.fillPoly(mask, [tip], 255, cv2.LINE_AA)
        return mask.astype(np.float32) / 255 * 0.9, (255, 240, 215)
    return mask.astype(np.float32) / 255, colour


def _sprites(run_dir: Path, kind: str, unit: int) -> list[Path]:
    """Small see-through pictures of one kind in three sizes."""
    import cv2
    import numpy as np

    out_dir = run_dir / "layers"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, factor in enumerate((0.6, 0.85, 1.15)):
        path = out_dir / f"{kind}_{unit}_{index}.png"
        if not path.exists():
            size = max(16, int(unit * factor)) | 1
            alpha, colour = _shape(kind, size, index)
            rgba = np.dstack([
                np.broadcast_to(np.array(colour, np.float32), (size, size, 3)),
                alpha * 255 * 0.85,
            ]).astype(np.uint8)
            cv2.imwrite(str(path), rgba)
        paths.append(path)
    return paths


def build_animation(
    scene: Scene, duration: float, back: Path, front: Path, run_dir: Path,
    width: int, height: int, fps: int, supersample: int,
) -> tuple[list[str], str]:
    """ffmpeg inputs and a filter graph ending in [anim] for one animated scene."""
    key = sum(ord(c) for c in (scene.label or "scene"))
    frames = max(1, int(duration * fps))
    # Eased progress 0 -> 1 over the scene: once per output frame for the
    # background, and by clock time for the layers drawn over it.
    p_frame = f"(1-cos(PI*min(1,on/{frames})))/2"
    p_time = f"(1-cos(PI*min(1,t/{duration:.3f})))/2"

    # --- Background: a slow camera move. The character then moves further in
    # the same screen direction, which is what reads as depth.
    motion = ("zoom_in", "pan_right", "zoom_out", "pan_left", "pan_up")[key % 5]
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    drift_x = drift_y = grow = "0"
    if motion == "zoom_in":
        z, x, y = f"1+0.10*{p_frame}", cx, cy
        grow = f"0.035*{p_time}"
    elif motion == "zoom_out":
        z, x, y = f"1.10-0.10*{p_frame}", cx, cy
        grow = f"0.035*(1-{p_time})"
    elif motion == "pan_right":
        z, x, y = "1.10", f"(iw-iw/zoom)*{p_frame}", cy
        drift_x = f"-{0.014 * width:.1f}*(2*{p_time}-1)"
    elif motion == "pan_left":
        z, x, y = "1.10", f"(iw-iw/zoom)*(1-{p_frame})", cy
        drift_x = f"{0.014 * width:.1f}*(2*{p_time}-1)"
    else:  # pan_up
        z, x, y = "1.10", cx, f"(ih-ih/zoom)*(1-{p_frame})"
        drift_y = f"{0.010 * height:.1f}*(2*{p_time}-1)"
    work_w, work_h = (width + 160) * supersample, (height + 280) * supersample
    back_chain = (
        f"[0:v]scale={work_w}:{work_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={work_w}:{work_h},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps}[back]"
    )

    # --- Character: breathing, a soft bob and a sway, each scene favouring one
    # so twenty scenes do not all move the same way. The bouncy style lands
    # with a cartoon squash, and every scene opens with a small "boing".
    style = (key // 5) % 3
    breathe, bob, sway, squash = (
        (0.012, 0.006, 0.006, 0.008), (0.006, 0.017, 0.005, 0.028), (0.006, 0.005, 0.014, 0.008),
    )[style]
    phase = (key % 7) * 0.9
    pop = "0.05*exp(-5*t)*cos(14*t)"
    size = f"({_FRONT_SCALE}+{breathe}*sin(2*PI*t/2.8+{phase:.2f})+{grow}+{pop})"
    # 1 at the moment the bob touches down, falling to 0 quickly in the air.
    landing = f"pow(1-abs(sin(PI*t/1.7+{phase:.2f})),3)"
    front_chain = (
        f"[1:v]format=rgba,fps={fps},"
        f"rotate=a='{sway}*sin(2*PI*t/3.6+{phase:.2f})':c=none,"
        f"scale=w='trunc({width}*{size}*(1+{squash}*{landing})/2)*2':"
        f"h='trunc({height}*{size}*(1-{squash}*{landing})/2)*2':eval=frame:flags=bilinear[front]"
    )
    bob_px = bob * height
    # Squashing shrinks the layer about its middle; push it down by half of
    # that so the feet stay on the ground instead of lifting.
    settle_px = height * _FRONT_SCALE * squash / 2
    place = (
        f"[back][front]overlay=x='(W-w)/2+{0.005 * width:.1f}*sin(2*PI*t/4.4+{phase:.2f})+{drift_x}':"
        f"y='(H-h)/2-{bob_px:.1f}*abs(sin(PI*t/1.7+{phase:.2f}))+{bob_px / 2:.1f}"
        f"+{settle_px:.1f}*{landing}+{drift_y}':"
        f"eval=frame:format=auto[v0]"
    )

    # --- Floating extras that suit the scene: hearts and bubbles rise; stars,
    # snow, petals, rain and sparkles come down.
    kind = scene_theme(scene, key)
    direction, count, (slow, fast), wobble_share = _MOTION[kind]
    unit = int(min(width, height) * (0.075 if kind in ("bubble", "heart", "star") else 0.06))
    sprites = _sprites(run_dir, kind, unit)
    input_args = ["-loop", "1", "-i", str(back), "-loop", "1", "-i", str(front)]
    parts = [back_chain, front_chain, place]
    for index in range(count):
        input_args.extend(["-loop", "1", "-i", str(sprites[index % len(sprites)])])
        # Golden-ratio spacing spreads them evenly without a random generator,
        # so the same scene always renders the same way.
        column = (0.07 + 0.86 * math.modf((index + 1) * 0.618 + key * 0.13)[0]) * width
        speed = (slow + (fast - slow) * ((index * 3) % 4) / 3) * height
        offset = math.modf(index * 0.37 + key * 0.11)[0] * height
        wobble = f"{wobble_share * width:.1f}*sin(t*{0.6 + 0.15 * (index % 3):.2f}+{index * 1.7:.2f})"
        travel = f"mod(t*{speed:.1f}+{offset:.1f},H+h)"
        y = f"H-{travel}" if direction == "up" else f"{travel}-h"
        parts.append(
            f"[v{index}][{index + 2}:v]overlay=x='{column:.1f}+{wobble}':y='{y}':"
            f"eval=frame:format=auto[{'anim' if index == count - 1 else f'v{index + 1}'}]"
        )
    return input_args, ";".join(parts)
