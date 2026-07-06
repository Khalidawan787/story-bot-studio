from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from .config import settings
from .models import Lesson, Scene
from .visuals import escape_filter_path, ffmpeg_bin, ffprobe_bin, font_path, resolve_scene_image, scene_color


def _audio_duration(audio_path: Path) -> float:
    result = subprocess.run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def validate_rendered_video(video_path: Path) -> None:
    if not video_path.exists() or video_path.stat().st_size == 0:
        raise RuntimeError(f"Rendered video missing or empty: {video_path}")
    result = subprocess.run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if "video" not in streams:
        raise RuntimeError(f"Rendered video has no video stream: {video_path}")
    if "audio" not in streams:
        raise RuntimeError(f"Rendered video has no audio stream: {video_path}")


def _drawtext_filter(title_file: Path, line_file: Path, duration: float) -> str:
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    title_path = escape_filter_path(str(title_file))
    line_path = escape_filter_path(str(line_file))
    fade_alpha = f"if(lt(t,0.25),t/0.25,if(gt(t,{max(0.3, duration - 0.25):.2f}),({duration:.2f}-t)/0.25,1))"
    # Clean, smaller, non-wobbling text: a small title chip at top and a
    # wrapped caption band at the bottom.
    return ",".join(
        [
            f"drawbox=x=0:y=96:w={settings.video_width}:h=132:color=black@0.30:t=fill",
            f"drawtext=textfile='{title_path}'{font_arg}:fontsize=58:fontcolor=0xffd43b:borderw=4:bordercolor=black:x=(w-text_w)/2:y=126:alpha='{fade_alpha}'",
            f"drawbox=x=40:y={settings.video_height - 360}:w={settings.video_width - 80}:h=230:color=black@0.48:t=fill",
            f"drawtext=textfile='{line_path}'{font_arg}:fontsize=42:fontcolor=white:borderw=3:bordercolor=black:line_spacing=10:x=(w-text_w)/2:y=h-330:alpha='{fade_alpha}'",
        ]
    )


def _motion_filter(duration: float, scene: Scene | None = None) -> str:
    """Dynamic, per-scene camera motion (free): each scene gets a different
    move — zoom in / zoom out / pan right / pan left / pan up — for variety."""
    W, H = settings.video_width, settings.video_height
    frames = max(1, int(duration * settings.video_fps))

    if not settings.enable_motion:
        base = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
        filters = [base, "eq=saturation=1.12:contrast=1.05"]
    else:
        cover = (
            f"scale={W + 160}:{H + 280}:force_original_aspect_ratio=increase,"
            f"crop={W + 160}:{H + 280}"
        )
        presets = ["zoom_in", "zoom_out", "pan_right", "pan_left", "pan_up"]
        key = sum(ord(c) for c in (scene.label if scene else "scene"))
        motion = presets[key % len(presets)]
        p = f"(on/{frames})"
        cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        if motion == "zoom_in":
            z, x, y = f"1+0.16*{p}", cx, cy
        elif motion == "zoom_out":
            z, x, y = f"1.16-0.16*{p}", cx, cy
        elif motion == "pan_right":
            z, x, y = "1.14", f"(iw-iw/zoom)*{p}", cy
        elif motion == "pan_left":
            z, x, y = "1.14", f"(iw-iw/zoom)*(1-{p})", cy
        else:  # pan_up
            z, x, y = "1.14", cx, f"(ih-ih/zoom)*(1-{p})"
        zoompan = (
            f"zoompan=z='{z}':x='{x}':y='{y}':"
            f"d={frames}:s={W}x{H}:fps={settings.video_fps}"
        )
        filters = [cover, zoompan, "eq=saturation=1.14:contrast=1.06"]

    if settings.enable_fades and duration > 1.0:
        filters.append("fade=t=in:st=0:d=0.3")
        filters.append(f"fade=t=out:st={max(0.0, duration - 0.3):.2f}:d=0.3")
    return ",".join(filters)


def _render_scene(scene: Scene, duration: float, output_path: Path, run_dir: Path, audio_path: Path | None = None, channel=None) -> Path:
    image_path = resolve_scene_image(scene, run_dir, channel)
    if not image_path or not image_path.exists():
        raise RuntimeError(f"Scene image missing for {scene.label}")
    title_file = run_dir / f"{output_path.stem}_title.txt"
    line_file = run_dir / f"{output_path.stem}_line.txt"
    title_file.write_text(scene.label, encoding="utf-8")
    line_text = scene.line.split(". ", 1)[1] if ". " in scene.line else scene.line
    line_text = "\n".join(textwrap.wrap(line_text, width=32)) or line_text
    line_file.write_text(line_text, encoding="utf-8")
    if image_path:
        input_args = ["-loop", "1", "-i", str(image_path)]
        base_filter = _motion_filter(duration, scene)
    else:
        input_args = [
            "-f",
            "lavfi",
            "-i",
            f"color=c={scene_color(scene)}:s={settings.video_width}x{settings.video_height}:d={duration}",
        ]
        base_filter = "format=yuv420p"

    vf = f"{base_filter},{_drawtext_filter(title_file, line_file, duration)}"
    command = [
        ffmpeg_bin(),
        "-y",
        *input_args,
    ]
    if audio_path:
        command.extend(["-i", str(audio_path)])
    command.extend([
        "-t",
        f"{duration:.3f}",
        "-vf",
        vf,
        "-r",
        str(settings.video_fps),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
    ])
    if audio_path:
        command.extend(["-c:a", "aac", "-shortest"])
    else:
        command.append("-an")
    command.append(str(output_path))
    subprocess.run(command, check=True)
    return output_path


def render_video(lesson: Lesson, audio_path: Path, output_path: Path, run_dir: Path, scene_audio_paths: list[Path] | None = None, channel=None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene_count = max(1, len(lesson.scenes))

    scene_files = []
    for index, scene in enumerate(lesson.scenes, start=1):
        scene_file = run_dir / f"scene_{index:02}.mp4"
        scene_audio = scene_audio_paths[index - 1] if scene_audio_paths else None
        if scene_audio:
            scene_duration = max(2.5, _audio_duration(scene_audio) + 0.35)
        else:
            total_duration = max(2.5 * scene_count, _audio_duration(audio_path))
            scene_duration = total_duration / scene_count
        scene_files.append(_render_scene(scene, scene_duration, scene_file, run_dir, scene_audio, channel))

    concat_file = run_dir / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{path.as_posix()}'" for path in scene_files),
        encoding="utf-8",
    )
    silent_video = run_dir / "joined_video.mp4"
    subprocess.run(
        [
            ffmpeg_bin(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(silent_video),
        ],
        check=True,
    )
    if scene_audio_paths:
        silent_video.replace(output_path)
        return output_path

    subprocess.run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(silent_video),
            "-i",
            str(audio_path),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(output_path),
        ],
        check=True,
    )
    return output_path
