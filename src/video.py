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


def validate_rendered_video(video_path: Path, content_type: str = "short") -> None:
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



def _dimensions(content_type: str) -> tuple[int, int]:
    """Shorts stay vertical; long-form videos use standard YouTube 16:9."""
    if content_type == "long":
        return 1920, 1080
    return settings.video_width, settings.video_height


# Render quality tiers. `supersample` is the multiplier applied to the working
# canvas before the camera move: zoompan can only step whole pixels, so a bigger
# canvas is what turns visible stair-stepping into smooth motion, and the final
# downscale sharpens the (half-resolution) free-provider images on the way out.
_QUALITY_PRESETS = {
    "fast": {"crf": "22", "preset": "veryfast", "supersample": 1, "polish": False},
    "balanced": {"crf": "19", "preset": "medium", "supersample": 2, "polish": True},
    "best": {"crf": "17", "preset": "slow", "supersample": 2, "polish": True},
}


def _quality() -> dict:
    return _QUALITY_PRESETS.get(settings.video_quality, _QUALITY_PRESETS["balanced"])


def _video_encode_args() -> list[str]:
    quality = _quality()
    return [
        "-c:v", "libx264",
        "-crf", quality["crf"],
        "-preset", quality["preset"],
        "-profile:v", "high",
        "-level", "4.2",
        # Keyframe every second: YouTube's transcoder keeps more detail and
        # seeking/looping in the Shorts player stays sharp.
        "-g", str(settings.video_fps * 2),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]


def _audio_encode_args() -> list[str]:
    return ["-c:a", "aac", "-b:a", settings.audio_bitrate, "-ar", "48000"]


def _polish_filters() -> list[str]:
    """Sharpen and grade the frame after the camera move."""
    if not (settings.enable_image_polish and _quality()["polish"]):
        return ["eq=saturation=1.12:contrast=1.05"]
    return [
        # Recover perceived detail lost when a ~576px-wide source is scaled to 1080p.
        "unsharp=5:5:0.9:5:5:0.3",
        "eq=saturation=1.16:contrast=1.08:gamma=1.02",
        # A soft vignette pulls the eye to the middle and hides upscale mush at
        # the edges — the single cheapest "this looks produced" touch.
        "vignette=angle=PI/6",
    ]

def _title_drawtext(title_file: Path, duration: float, content_type: str = "short") -> str:
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    title_path = escape_filter_path(str(title_file))
    fade_alpha = f"if(lt(t,0.25),t/0.25,if(gt(t,{max(0.3, duration - 0.25):.2f}),({duration:.2f}-t)/0.25,1))"
    font_size = 52 if content_type == "long" else 62
    title_y = 55 if content_type == "long" else 150
    return (
        f"drawtext=textfile='{title_path}'{font_arg}:fontsize={font_size}:fontcolor=0xffe066:"
        f"borderw=3:bordercolor=black@0.85:box=1:boxcolor=0x14142b@0.55:boxborderw=28:"
        f"x=(w-text_w)/2:y={title_y}:alpha='{fade_alpha}'"
    )


def _line_drawtext(line_file: Path, duration: float, content_type: str = "short") -> str:
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    line_path = escape_filter_path(str(line_file))
    fade_alpha = f"if(lt(t,0.25),t/0.25,if(gt(t,{max(0.3, duration - 0.25):.2f}),({duration:.2f}-t)/0.25,1))"
    _width, height = _dimensions(content_type)
    caption_y = height - (220 if content_type == "long" else 560)
    font_size = 36 if content_type == "long" else 48
    return (
        f"drawtext=textfile='{line_path}'{font_arg}:fontsize={font_size}:fontcolor=white:"
        f"borderw=2:bordercolor=black@0.85:box=1:boxcolor=0x14142b@0.60:boxborderw=32:"
        f"line_spacing=14:x=(w-text_w)/2:y={caption_y}:alpha='{fade_alpha}'"
    )


def _drawtext_filter(line_file: Path, duration: float, content_type: str = "short") -> str:
    """Draw narration captions only; scene-title boards are intentionally disabled."""
    return _line_drawtext(line_file, duration, content_type)

def _ass_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centis = int(round(seconds * 100))
    hours = centis // 360000
    minutes = (centis % 360000) // 6000
    secs = (centis % 6000) // 100
    cs = centis % 100
    return f"{hours}:{minutes:02}:{secs:02}.{cs:02}"


def _write_karaoke_ass(marks: list[tuple[str, float, float]], duration: float, out_path: Path, content_type: str = "short") -> Path | None:
    """Build an ASS subtitle where each word lights up (white -> yellow) as it is
    spoken. Returns the file path, or None if there is nothing usable to show."""
    words = [(w.strip(), s, d) for (w, s, d) in marks if w and w.strip()]
    if not words:
        return None

    W, H = _dimensions(content_type)
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
        # \kf sweeps the highlight across the word instead of snapping it on,
        # which reads as smooth motion rather than a blinking word.
        parts.append(f"{{\\kf{hold}}}{safe} ")
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
        # Heavier face, thicker outline and a real drop shadow: captions have to
        # survive being watched on a phone over a busy photo.
        f"Style: Kids,Arial Black,{48 if content_type == 'long' else 64},&H0000E5FF,&H00FFFFFF,&H00101010,&H96000000,"
        f"-1,0,0,0,100,100,1,0,1,5,3,2,{160 if content_type == 'long' else 110},{160 if content_type == 'long' else 110},{100 if content_type == 'long' else 470},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dialogue = f"Dialogue: 0,{_ass_timestamp(0)},{_ass_timestamp(duration)},Kids,,0,0,0,,{text}\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + dialogue, encoding="utf-8")
    return out_path


def _motion_filter(
    duration: float,
    scene: Scene | None = None,
    content_type: str = "short",
    edge_fades: bool = True,
) -> str:
    """Dynamic, per-scene camera motion (free): each scene gets a different
    move — zoom in / zoom out / pan right / pan left / pan up — for variety.

    `edge_fades` is turned off when the scenes are joined with crossfades, so a
    clip does not dip to black right where the next one is already fading in.
    """
    W, H = _dimensions(content_type)
    frames = max(1, int(duration * settings.video_fps))
    scale = _quality()["supersample"]

    if not settings.enable_motion:
        base = (
            f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={W}:{H}"
        )
        filters = [base, *_polish_filters()]
    else:
        # Supersampled working canvas -> smooth (sub-pixel) camera motion.
        work_w, work_h = (W + 160) * scale, (H + 280) * scale
        cover = (
            f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={work_w}:{work_h}"
        )
        presets = ["zoom_in", "zoom_out", "pan_right", "pan_left", "pan_up"]
        key = sum(ord(c) for c in (scene.label if scene else "scene"))
        motion = presets[key % len(presets)]
        # Ease in/out instead of a constant slide: a linear move reads as a
        # slideshow, an eased one reads as a camera.
        p = f"(1-cos(PI*min(1,on/{frames})))/2"
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
        filters = [cover, zoompan, *_polish_filters()]

    if edge_fades and settings.enable_fades and duration > 1.0:
        filters.append("fade=t=in:st=0:d=0.3")
        filters.append(f"fade=t=out:st={max(0.0, duration - 0.3):.2f}:d=0.3")
    return ",".join(filters)


def _render_scene(scene: Scene, duration: float, output_path: Path, run_dir: Path, audio_path: Path | None = None, channel=None, marks: list | None = None, content_type: str = "short", edge_fades: bool = True) -> Path:
    image_path = resolve_scene_image(
        scene, run_dir, channel, require_real_image=True,
        landscape=content_type == "long",
    )
    if not image_path or not image_path.exists():
        raise RuntimeError(f"Scene image missing for {scene.label}")
    line_file = run_dir / f"{output_path.stem}_line.txt"
    line_text = scene.line.split(". ", 1)[1] if ". " in scene.line else scene.line
    wrap_width = 64 if content_type == "long" else 32
    line_text = "\n".join(textwrap.wrap(line_text, width=wrap_width)) or line_text
    line_file.write_text(line_text, encoding="utf-8")
    if image_path:
        input_args = ["-loop", "1", "-i", str(image_path)]
        base_filter = _motion_filter(duration, scene, content_type, edge_fades=edge_fades)
    else:
        input_args = [
            "-f",
            "lavfi",
            "-i",
            f"color=c={scene_color(scene)}:s={_dimensions(content_type)[0]}x{_dimensions(content_type)[1]}:d={duration}",
        ]
        base_filter = "format=yuv420p"

    # Word-by-word karaoke caption when timings are available; otherwise the
    # original static caption. Scene-title boards are never drawn.
    caption_filter = None
    if not settings.enable_burned_captions:
        # Only the platform's own caption track is shown, so the text never
        # appears twice on screen.
        caption_filter = ""
    elif settings.enable_karaoke_captions and marks:
        ass_file = run_dir / f"{output_path.stem}_caption.ass"
        if _write_karaoke_ass(marks, duration, ass_file, content_type):
            ass_path = escape_filter_path(str(ass_file))
            fonts_dir = escape_filter_path("C:/Windows/Fonts")
            caption_filter = f"ass=filename='{ass_path}':fontsdir='{fonts_dir}'"
    if caption_filter is None:
        caption_filter = _drawtext_filter(line_file, duration, content_type)

    vf = f"{base_filter},{caption_filter}" if caption_filter else base_filter
    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
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
        *_video_encode_args(),
    ])
    if audio_path:
        command.extend([*_audio_encode_args(), "-shortest"])
    else:
        command.append("-an")
    command.append(str(output_path))
    subprocess.run(command, check=True)
    return output_path


_TRANSITIONS = ["fade", "smoothleft", "fade", "smoothup", "fade", "smoothright", "fade", "circleopen"]


def _crossfade_join(
    scene_files: list[Path],
    scene_audio_paths: list[Path],
    holds: list[float],
    transition: float,
    output_path: Path,
) -> Path:
    """Join clips with crossfades while keeping every word on its own picture.

    Each clip is rendered `transition` seconds longer than the time its audio is
    on screen, and the crossfade consumes exactly that tail. So clip i still
    starts at sum(holds before i) — the same instant its narration starts — and
    nothing drifts, no matter how many scenes there are.
    """
    command = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y"]
    for path in scene_files:
        command.extend(["-i", str(path)])
    for path in scene_audio_paths:
        command.extend(["-i", str(path)])

    count = len(scene_files)
    parts: list[str] = []
    if transition > 0:
        previous = "0:v"
        for index in range(1, count):
            offset = sum(holds[:index])
            label = f"vx{index}"
            transition_name = _TRANSITIONS[(index - 1) % len(_TRANSITIONS)]
            parts.append(
                f"[{previous}][{index}:v]xfade=transition={transition_name}:"
                f"duration={transition:.3f}:offset={offset:.3f}[{label}]"
            )
            previous = label
    else:
        # Same clips, plain cuts — used when a crossfade graph fails so a finished
        # render is never thrown away. The clips still carry the transition tail,
        # so trim it back off here or every scene would drift past its narration.
        for index in range(count):
            parts.append(
                f"[{index}:v]trim=duration={holds[index]:.3f},setpts=PTS-STARTPTS[vc{index}]"
            )
        parts.append("".join(f"[vc{i}]" for i in range(count)) + f"concat=n={count}:v=1:a=0[vout]")
        previous = "vout"

    audio_labels: list[str] = []
    for index, path in enumerate(scene_audio_paths):
        pad = max(0.0, holds[index] - _audio_duration(path))
        label = f"a{index}"
        parts.append(f"[{count + index}:a]aresample=48000,apad=pad_dur={pad:.3f}[{label}]")
        audio_labels.append(f"[{label}]")
    parts.append(f"{''.join(audio_labels)}concat=n={len(audio_labels)}:v=0:a=1[aout]")

    command.extend([
        "-filter_complex", ";".join(parts),
        "-map", "0:v" if previous == "0:v" else f"[{previous}]",
        "-map", "[aout]",
        "-r", str(settings.video_fps),
        *_video_encode_args(),
        *_audio_encode_args(),
        str(output_path),
    ])
    subprocess.run(command, check=True)
    return output_path


def render_video(lesson: Lesson, audio_path: Path, output_path: Path, run_dir: Path, scene_audio_paths: list[Path] | None = None, channel=None, scene_marks: list | None = None, content_type: str = "short") -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene_count = max(1, len(lesson.scenes))

    # Per-scene on-screen time, decided up front so the crossfade maths below
    # can line every clip up with its own narration.
    holds: list[float] = []
    if scene_audio_paths:
        holds = [max(2.5, _audio_duration(path) + 0.35) for path in scene_audio_paths]
    transition = 0.0
    if settings.enable_transitions and len(holds) > 1:
        # Never let a transition eat more than a third of the shortest scene.
        transition = max(0.0, min(settings.transition_seconds, min(holds) * 0.35))

    scene_files = []
    for index, scene in enumerate(lesson.scenes, start=1):
        scene_file = run_dir / f"scene_{index:02}.mp4"
        if scene_file.exists() and scene_file.stat().st_size > 100_000:
            scene_files.append(scene_file)
            continue
        scene_audio = scene_audio_paths[index - 1] if scene_audio_paths else None
        marks = scene_marks[index - 1] if scene_marks else None
        if scene_audio:
            scene_duration = holds[index - 1] + transition
        else:
            total_duration = max(2.5 * scene_count, _audio_duration(audio_path))
            scene_duration = total_duration / scene_count
        # With crossfades the audio is added once at the join, so the clips stay
        # silent here and carry a transition-length tail instead.
        embedded_audio = None if transition else scene_audio
        scene_files.append(_render_scene(
            scene, scene_duration, scene_file, run_dir, embedded_audio, channel, marks,
            content_type, edge_fades=not transition,
        ))

    if transition and len(scene_files) > 1:
        for attempt_transition in (transition, 0.0):
            try:
                return _crossfade_join(
                    scene_files, list(scene_audio_paths), holds,
                    attempt_transition, output_path,
                )
            except subprocess.CalledProcessError as exc:
                # Never lose a finished render over a filter-graph problem: retry
                # with plain cuts, which only costs the transitions.
                if attempt_transition:
                    print(f"[video] crossfade join failed ({exc}); joining without transitions")
                else:
                    raise

    concat_file = run_dir / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{path.as_posix()}'" for path in scene_files),
        encoding="utf-8",
    )
    silent_video = run_dir / "joined_video.mp4"
    subprocess.run(
        [
            ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "error",
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
            "-hide_banner",
            "-loglevel",
            "error",
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
