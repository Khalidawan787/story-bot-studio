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


def _title_drawtext(title_file: Path, duration: float) -> str:
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    title_path = escape_filter_path(str(title_file))
    fade_alpha = f"if(lt(t,0.25),t/0.25,if(gt(t,{max(0.3, duration - 0.25):.2f}),({duration:.2f}-t)/0.25,1))"
    return (
        f"drawtext=textfile='{title_path}'{font_arg}:fontsize=62:fontcolor=0xffe066:"
        f"borderw=3:bordercolor=black@0.85:box=1:boxcolor=0x14142b@0.55:boxborderw=28:"
        f"x=(w-text_w)/2:y=150:alpha='{fade_alpha}'"
    )


def _line_drawtext(line_file: Path, duration: float) -> str:
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    line_path = escape_filter_path(str(line_file))
    fade_alpha = f"if(lt(t,0.25),t/0.25,if(gt(t,{max(0.3, duration - 0.25):.2f}),({duration:.2f}-t)/0.25,1))"
    caption_y = settings.video_height - 560
    return (
        f"drawtext=textfile='{line_path}'{font_arg}:fontsize=48:fontcolor=white:"
        f"borderw=2:bordercolor=black@0.85:box=1:boxcolor=0x14142b@0.60:boxborderw=32:"
        f"line_spacing=14:x=(w-text_w)/2:y={caption_y}:alpha='{fade_alpha}'"
    )


def _drawtext_filter(title_file: Path, line_file: Path, duration: float) -> str:
    # Clean captions using drawtext's OWN padded background box (auto-sized and
    # centred) instead of muddy full-width grey bars. The caption sits in the
    # lower-middle, clear of YouTube Shorts' bottom-left title and right-side
    # action buttons.
    return ",".join(
        [
            _title_drawtext(title_file, duration),
            _line_drawtext(line_file, duration),
        ]
    )


def _ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centis = int(round(seconds * 100))
    hours = centis // 360000
    minutes = (centis % 360000) // 6000
    secs = (centis % 6000) // 100
    cs = centis % 100
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"


def _write_karaoke_ass(marks: list[tuple[str, float, float]], duration: float, out_path: Path) -> Path | None:
    """Build an ASS subtitle where each word lights up (white -> yellow) as it is
    spoken. Returns the file path, or None if there is nothing usable to show."""
    words = [(w.strip(), s, d) for (w, s, d) in marks if w and w.strip()]
    if not words:
        return None

    W, H = settings.video_width, settings.video_height
    # Karaoke \k timings are in centiseconds; each word holds its highlight until
    # the next word begins (last word holds to the end of the clip).
    parts: list[str] = []
    lead = int(round(words[0][1] * 100))
    if lead > 0:
        parts.append(f"{{\\k{lead}}}")
    for i, (word, start, dur) in enumerate(words):
        next_start = words[i + 1][1] if i + 1 < len(words) else min(duration, start + dur)
        hold = max(1, int(round((next_start - start) * 100)))
        safe = word.replace("{", "(").replace("}", ")").replace("\\", "")
        parts.append(f"{{\\k{hold}}}{safe} ")
    text = "".join(parts).rstrip()

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # PrimaryColour = sung (yellow), SecondaryColour = not yet sung (white).
        "Style: Kids,Arial,58,&H0000E5FF,&H00FFFFFF,&H00202020,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,4,2,2,110,110,470,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dialogue = f"Dialogue: 0,{_ass_timestamp(0)},{_ass_timestamp(duration)},Kids,,0,0,0,,{text}\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + dialogue, encoding="utf-8")
    return out_path


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


def _render_scene(scene: Scene, duration: float, output_path: Path, run_dir: Path, audio_path: Path | None = None, channel=None, marks: list | None = None) -> Path:
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

    # Word-by-word karaoke caption when timings are available; otherwise the
    # original static caption. The title (top) is always drawn with drawtext.
    caption_filter = None
    if settings.enable_karaoke_captions and marks:
        ass_file = run_dir / f"{output_path.stem}_caption.ass"
        if _write_karaoke_ass(marks, duration, ass_file):
            ass_path = escape_filter_path(str(ass_file))
            fonts_dir = escape_filter_path("C:/Windows/Fonts")
            caption_filter = f"{_title_drawtext(title_file, duration)},ass=filename='{ass_path}':fontsdir='{fonts_dir}'"
    if caption_filter is None:
        caption_filter = _drawtext_filter(title_file, line_file, duration)

    vf = f"{base_filter},{caption_filter}"
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


def render_video(lesson: Lesson, audio_path: Path, output_path: Path, run_dir: Path, scene_audio_paths: list[Path] | None = None, channel=None, scene_marks: list | None = None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene_count = max(1, len(lesson.scenes))

    scene_files = []
    for index, scene in enumerate(lesson.scenes, start=1):
        scene_file = run_dir / f"scene_{index:02}.mp4"
        scene_audio = scene_audio_paths[index - 1] if scene_audio_paths else None
        marks = scene_marks[index - 1] if scene_marks else None
        if scene_audio:
            scene_duration = max(2.5, _audio_duration(scene_audio) + 0.35)
        else:
            total_duration = max(2.5 * scene_count, _audio_duration(audio_path))
            scene_duration = total_duration / scene_count
        scene_files.append(_render_scene(scene, scene_duration, scene_file, run_dir, scene_audio, channel, marks))

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
