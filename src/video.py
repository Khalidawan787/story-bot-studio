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


FONTS_DIR = settings.root / "fonts"
CINEMATIC_FONT = "DM Serif Display"
# Muted colour, crushed blacks, cool shadows and a heavy vignette: the dark
# film look of the big motivation channels.
_CINEMATIC_GRADE = [
    "eq=saturation=0.68:contrast=1.16:brightness=-0.07",
    "colorbalance=rs=-0.04:bs=0.06:rh=0.04:bh=-0.02",
    "vignette=angle=PI/4",
]
# A new picture every few seconds, as in those videos; a scene's narration
# is often 6-20 s, which on one picture reads as a slideshow.
_CUT_SECONDS = 3.6
_MAX_CUTS = 4


def is_cinematic(channel) -> bool:
    """The dark cinematic look: centred serif captions, fast cuts, dark grade."""
    if channel is None:
        return False
    return getattr(channel, "look", "") == "cinematic" or str(getattr(channel, "genre", "") or "").lower() == "motivation"


def _polish_filters(cinematic: bool = False) -> list[str]:
    """Sharpen and grade the frame after the camera move."""
    if cinematic:
        sharpen = ["unsharp=5:5:0.9:5:5:0.3"] if settings.enable_image_polish and _quality()["polish"] else []
        return [*sharpen, *_CINEMATIC_GRADE]
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


def _write_karaoke_ass(marks: list[tuple[str, float, float]], duration: float, out_path: Path, content_type: str = "short", pop: bool = False) -> Path | None:
    """Build an ASS subtitle where each word lights up (white -> yellow) as it is
    spoken. Returns the file path, or None if there is nothing usable to show.

    With `pop`, each group of words springs in slightly oversized and settles,
    which suits the animated cartoon scenes."""
    words = [(w.strip(), s, d) for (w, s, d) in marks if w and w.strip()]
    if not words:
        return None

    W, H = _dimensions(content_type)
    # A few words on screen at a time, not the whole scene's narration: a long
    # line used to fill four rows and cover the picture. Each group stays up
    # until the next one starts, and breaks early at the end of a sentence.
    max_words = 7 if content_type == "long" else 4
    groups: list[list[tuple[str, float, float]]] = [[]]
    for item in words:
        groups[-1].append(item)
        sentence_end = item[0].rstrip("\"')").endswith((".", "!", "?"))
        if len(groups[-1]) >= max_words or (len(groups[-1]) >= 3 and sentence_end):
            groups.append([])
    groups = [group for group in groups if group]
    # Never leave one stray word alone on screen at the end.
    if len(groups) > 1 and len(groups[-1]) == 1:
        groups[-2].extend(groups.pop())

    events: list[tuple[float, float, str]] = []
    for g, group in enumerate(groups):
        begin = 0.0 if g == 0 else group[0][1]
        end = max(groups[g + 1][0][1] if g + 1 < len(groups) else duration, begin + 0.1)
        # Karaoke \k timings are in centiseconds, counted from the start of the
        # group; each word holds its highlight until the next word begins.
        parts: list[str] = []
        lead = int(round((group[0][1] - begin) * 100))
        if lead > 0:
            parts.append(f"{{\\k{lead}}}")
        for i, (word, start, dur) in enumerate(group):
            next_start = group[i + 1][1] if i + 1 < len(group) else min(end, start + dur)
            hold = max(1, int(round((next_start - start) * 100)))
            safe = word.replace("{", "(").replace("}", ")").replace("\\", "")
            # \kf sweeps the highlight across the word instead of snapping it on,
            # which reads as smooth motion rather than a blinking word.
            parts.append(f"{{\\kf{hold}}}{safe} ")
        spring = r"{\fscx70\fscy70\t(0,110,\fscx108\fscy108)\t(110,200,\fscx100\fscy100)}" if pop else ""
        events.append((begin, end, spring + "".join(parts).rstrip()))

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
    dialogue = "".join(
        f"Dialogue: 0,{_ass_timestamp(begin)},{_ass_timestamp(end)},Kids,,0,0,0,,{text}\n"
        for begin, end, text in events
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + dialogue, encoding="utf-8")
    return out_path


# Words never picked as the big word of a caption group.
_SMALL_WORDS = {
    "about", "after", "again", "because", "before", "being", "could", "every", "other",
    "should", "their", "there", "these", "thing", "things", "those", "through", "until",
    "where", "which", "while", "would", "you're", "they're", "don't", "can't",
}


def _write_cinematic_ass(marks: list[tuple[str, float, float]], duration: float, out_path: Path, content_type: str = "short") -> Path | None:
    """White serif words low in the frame, only the two or three being
    spoken, each fading in as it is said; the strongest word is set larger."""
    words = [(w.strip(), s, d) for (w, s, d) in marks if w and w.strip()]
    if not words:
        return None

    W, H = _dimensions(content_type)
    groups: list[list[tuple[str, float, float]]] = [[]]
    for item in words:
        groups[-1].append(item)
        pause = item[0].rstrip("\"')").endswith((".", "!", "?", ",", ";", ":"))
        if len(groups[-1]) >= 3 or pause:
            groups.append([])
    groups = [group for group in groups if group]

    size = 58 if content_type == "long" else 70
    big = int(size * 1.35)
    events: list[tuple[float, float, str]] = []
    for g, group in enumerate(groups):
        group_end = max(groups[g + 1][0][1] if g + 1 < len(groups) else duration, group[0][1] + 0.1)
        clean = [w.replace("{", "(").replace("}", ")").replace("\\", "") for w, _s, _d in group]
        bare = [w.strip(".,!?;:\"'").lower() for w in clean]
        options = [i for i, w in enumerate(bare) if len(w) >= 4 and w not in _SMALL_WORDS]
        key = max(options, key=lambda i: len(bare[i])) if options else -1
        for i, (_word, start, _dur) in enumerate(group):
            begin = 0.0 if g == 0 and i == 0 else start
            end = group[i + 1][1] if i + 1 < len(group) else group_end
            if end <= begin:
                continue
            parts: list[str] = []
            for j, word in enumerate(clean):
                # Words still to come are drawn invisible, so the line keeps its
                # final layout and nothing jumps as words appear.
                alpha = r"\alpha&H00&" if j < i else (r"\alpha&HFF&\t(0,180,\alpha&H00&)" if j == i else r"\alpha&HFF&")
                fs = big if j == key else size
                parts.append(f"{{{alpha}\\fs{fs}}}{word}")
            fade = r"{\fad(0,160)}" if i + 1 == len(group) else ""
            events.append((begin, end, fade + " ".join(parts)))

    margin = 200 if content_type == "long" else 110
    # Above the like/comment buttons and the title YouTube draws on a Short.
    bottom = 110 if content_type == "long" else 400
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
        # No box; a thin dark outline and soft shadow keep white legible on
        # bright footage too.
        f"Style: Film,{CINEMATIC_FONT},{size},&H00FFFFFF,&H00FFFFFF,&H40000000,&H64000000,"
        f"0,0,0,0,100,100,1,0,1,2.5,3,2,{margin},{margin},{bottom},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dialogue = "".join(
        f"Dialogue: 0,{_ass_timestamp(begin)},{_ass_timestamp(end)},Film,,0,0,0,,{text}\n"
        for begin, end, text in events
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + dialogue, encoding="utf-8")
    return out_path


def _motion_filter(
    duration: float,
    scene: Scene | None = None,
    content_type: str = "short",
    edge_fades: bool = True,
    cinematic: bool = False,
) -> str:
    """Dynamic, per-scene camera motion (free): each scene gets a different
    move — zoom in / zoom out / pan right / pan left / pan up — for variety.
    The cinematic look only ever pushes slowly in or out.

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
        filters = [base, *_polish_filters(cinematic)]
    else:
        # Supersampled working canvas -> smooth (sub-pixel) camera motion.
        work_w, work_h = (W + 160) * scale, (H + 280) * scale
        cover = (
            f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={work_w}:{work_h}"
        )
        presets = ["zoom_in", "zoom_out"] if cinematic else ["zoom_in", "zoom_out", "pan_right", "pan_left", "pan_up"]
        key = sum(ord(c) for c in (scene.label if scene else "scene"))
        motion = presets[key % len(presets)]
        # Ease in/out instead of a constant slide: a linear move reads as a
        # slideshow, an eased one reads as a camera. The cinematic push is
        # linear and small, a slow drift rather than a move.
        p = f"min(1,on/{frames})" if cinematic else f"(1-cos(PI*min(1,on/{frames})))/2"
        amount = 0.07 if cinematic else 0.16
        cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        if motion == "zoom_in":
            z, x, y = f"1+{amount}*{p}", cx, cy
        elif motion == "zoom_out":
            z, x, y = f"{1 + amount}-{amount}*{p}", cx, cy
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
        filters = [cover, zoompan, *_polish_filters(cinematic)]

    if edge_fades and settings.enable_fades and duration > 1.0:
        filters.append("fade=t=in:st=0:d=0.3")
        filters.append(f"fade=t=out:st={max(0.0, duration - 0.3):.2f}:d=0.3")
    return ",".join(filters)


def _fit_clip(duration: float, content_type: str, push: bool) -> list[str]:
    """Scale and crop footage to the frame; with `push`, a slow zoom in over
    `duration` (on a supersampled canvas, so the whole-pixel steps of zoompan
    do not shake)."""
    W, H = _dimensions(content_type)
    if not (push and settings.enable_motion):
        return [
            f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos",
            f"crop={W}:{H}",
            f"fps={settings.video_fps}",
            "setsar=1",
        ]
    scale = _quality()["supersample"]
    frames = max(1, int(duration * settings.video_fps))
    return [
        f"fps={settings.video_fps}",
        f"scale={W * scale}:{H * scale}:force_original_aspect_ratio=increase:flags=lanczos",
        f"crop={W * scale}:{H * scale}",
        f"zoompan=z='1+0.06*min(1,on/{frames})':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={W}x{H}:fps={settings.video_fps}",
        "setsar=1",
    ]


def _clip_filter(duration: float, content_type: str = "short", edge_fades: bool = True, cinematic: bool = False) -> str:
    """Fit real footage to the frame. No camera move: the clip already moves,
    except in the cinematic look, which adds a slow push in."""
    filters = [
        *_fit_clip(duration, content_type, push=cinematic),
        # Footage is already sharp, so only the grade and vignette are applied;
        # the sharpening meant for upscaled stills would add halos here.
        *(_CINEMATIC_GRADE if cinematic else ["eq=saturation=1.08:contrast=1.05", "vignette=angle=PI/6"]),
    ]
    if edge_fades and settings.enable_fades and duration > 1.0:
        filters.append("fade=t=in:st=0:d=0.3")
        filters.append(f"fade=t=out:st={max(0.0, duration - 0.3):.2f}:d=0.3")
    return ",".join(filters)


def _scene_clip(scene: Scene, duration: float, output_path: Path, run_dir: Path, channel, content_type: str) -> Path | None:
    """A stock video clip for this scene, or None to use a still image."""
    from .stock_video import fetch_scene_clip, stock_video_enabled

    if not stock_video_enabled(channel):
        return None
    clip_path = run_dir / f"clip_{output_path.stem}.mp4"
    if clip_path.exists() and clip_path.stat().st_size > 100_000:
        return clip_path
    try:
        return fetch_scene_clip(
            scene, clip_path, run_dir, channel,
            landscape=content_type == "long", min_seconds=duration,
        )
    except Exception as exc:
        # No clip of what this scene is about (or Pexels' hourly limit). Its own
        # picture, drawn from this very line, beats footage of another scene:
        # reusing earlier clips made the narration and the picture disagree.
        print(f"[video] {output_path.stem}: {exc}; using the scene's own picture")
        return None


def _extra_clips(scene: Scene, duration: float, output_path: Path, run_dir: Path, channel, content_type: str) -> list[Path]:
    """More clips for one scene, so the cinematic look cuts every few seconds.

    Fewer (or none) is fine: a Pexels limit only means longer shots."""
    from .stock_video import fetch_scene_clip

    wanted = min(_MAX_CUTS, max(1, round(duration / _CUT_SECONDS))) - 1
    clips: list[Path] = []
    for number in range(1, wanted + 1):
        path = run_dir / f"clip_{output_path.stem}_{number}.mp4"
        if not (path.exists() and path.stat().st_size > 100_000):
            try:
                fetch_scene_clip(
                    scene, path, run_dir, channel,
                    landscape=content_type == "long", min_seconds=_CUT_SECONDS,
                )
            except Exception as exc:
                print(f"[video] {output_path.stem}: {len(clips) + 1} shot(s) only ({exc})")
                break
        clips.append(path)
    return clips


def _render_scene(scene: Scene, duration: float, output_path: Path, run_dir: Path, audio_path: Path | None = None, channel=None, marks: list | None = None, content_type: str = "short", edge_fades: bool = True, reuse_from: Path | None = None) -> Path:
    if reuse_from:
        # Same footage as the original: copy its clip in, where _scene_clip
        # looks for an already downloaded one.
        import shutil

        for earlier in [reuse_from / f"clip_{output_path.stem}.mp4",
                        *sorted(reuse_from.glob(f"clip_{output_path.stem}_*.mp4"))]:
            if earlier.exists():
                shutil.copy2(earlier, run_dir / earlier.name)
                meta = earlier.with_suffix(".pexels.json")
                if meta.exists():
                    shutil.copy2(meta, run_dir / meta.name)
    # A fact video opens on its subject's real photo, and shows it again where
    # a scene names the subject (archive_images.py); the rest is video.
    real_photo = None
    if not reuse_from and channel is not None:
        from .verified_facts import is_verified_channel

        if is_verified_channel(channel):
            from .archive_images import photo_for_scene

            try:
                number = int(output_path.stem.rsplit("_", 1)[-1])
            except ValueError:
                number = 0
            try:
                real_photo = photo_for_scene(number, scene, run_dir)
            except Exception as exc:
                print(f"[video] {output_path.stem}: real photo unavailable ({exc})")
    cinematic = is_cinematic(channel)
    clip_path = None if real_photo else _scene_clip(scene, duration, output_path, run_dir, channel, content_type)
    if clip_path:
        clips = [clip_path]
        if cinematic:
            clips += _extra_clips(scene, duration, output_path, run_dir, channel, content_type)
        try:
            return _encode_scene(
                scene, duration, output_path, run_dir, audio_path, marks,
                content_type, edge_fades, clip_paths=clips, cinematic=cinematic,
            )
        except subprocess.CalledProcessError as exc:
            # A clip ffmpeg cannot read must not cost the scene: use an image.
            print(f"[video] {output_path.stem}: stock clip failed to render ({exc}); using a still image")
            output_path.unlink(missing_ok=True)
            for path in clips:
                path.unlink(missing_ok=True)
                path.with_suffix(".pexels.json").unlink(missing_ok=True)
    image_path = real_photo
    if reuse_from:
        from .dual_language import reused_image

        image_path = reused_image(scene, reuse_from)
    if not image_path:
        image_path = resolve_scene_image(
            scene, run_dir, channel, require_real_image=True,
            landscape=content_type == "long",
        )
    if not image_path or not image_path.exists():
        raise RuntimeError(f"Scene image missing for {scene.label}")
    layers = _scene_layers(image_path, output_path, run_dir, channel, content_type)
    if layers:
        try:
            return _encode_scene(
                scene, duration, output_path, run_dir, audio_path, marks,
                content_type, edge_fades, layers=layers, cinematic=cinematic,
            )
        except subprocess.CalledProcessError as exc:
            # The plain still render below is always available.
            print(f"[video] {output_path.stem}: layered animation failed ({exc}); using a still image")
            output_path.unlink(missing_ok=True)
    return _encode_scene(
        scene, duration, output_path, run_dir, audio_path, marks,
        content_type, edge_fades, image_path=image_path, cinematic=cinematic,
    )


def _scene_layers(image_path: Path, output_path: Path, run_dir: Path, channel, content_type: str) -> tuple[Path, Path] | None:
    """Character and background layers for an animated scene, or None."""
    from .animate import animation_enabled, prepare_layers

    if not (settings.enable_motion and animation_enabled(channel)):
        return None
    try:
        return prepare_layers(image_path, run_dir, output_path.stem, *_dimensions(content_type))
    except Exception as exc:
        print(f"[video] {output_path.stem}: not animated ({exc}); using a still image")
        return None


def _encode_scene(scene: Scene, duration: float, output_path: Path, run_dir: Path, audio_path: Path | None, marks: list | None, content_type: str, edge_fades: bool, image_path: Path | None = None, clip_paths: list[Path] | None = None, layers: tuple[Path, Path] | None = None, cinematic: bool = False) -> Path:
    line_file = run_dir / f"{output_path.stem}_line.txt"
    line_text = scene.line.split(". ", 1)[1] if ". " in scene.line else scene.line
    wrap_width = 64 if content_type == "long" else 32
    line_text = "\n".join(textwrap.wrap(line_text, width=wrap_width)) or line_text
    line_file.write_text(line_text, encoding="utf-8")
    graph = ""
    if layers:
        from .animate import build_animation

        # Several inputs (background, character, floating sprites) composed in
        # a filter graph; the polish and fades then run on the finished frame.
        W, H = _dimensions(content_type)
        input_args, graph = build_animation(
            scene, duration, layers[0], layers[1], run_dir, W, H,
            settings.video_fps, _quality()["supersample"],
        )
        finish = _polish_filters(cinematic)
        if edge_fades and settings.enable_fades and duration > 1.0:
            finish.append("fade=t=in:st=0:d=0.3")
            finish.append(f"fade=t=out:st={max(0.0, duration - 0.3):.2f}:d=0.3")
        base_filter = ",".join(finish)
    elif clip_paths and len(clip_paths) > 1:
        # Several shots in a row, each fitted with its own slow push in, then
        # graded and captioned as one piece.
        shot = duration / len(clip_paths)
        input_args, chains = [], []
        for index, path in enumerate(clip_paths):
            # Loop each clip so footage shorter than its shot still fills it.
            input_args += ["-stream_loop", "-1", "-i", str(path)]
            length = shot + (0.5 if index == len(clip_paths) - 1 else 0.0)
            chains.append(
                f"[{index}:v]{','.join(_fit_clip(length, content_type, push=True))},"
                f"trim=duration={length:.3f},setpts=PTS-STARTPTS[shot{index}]"
            )
        joined = "".join(f"[shot{index}]" for index in range(len(clip_paths)))
        graph = ";".join(chains) + f";{joined}concat=n={len(clip_paths)}:v=1:a=0[anim]"
        finish = list(_CINEMATIC_GRADE if cinematic else ["eq=saturation=1.08:contrast=1.05", "vignette=angle=PI/6"])
        if edge_fades and settings.enable_fades and duration > 1.0:
            finish.append("fade=t=in:st=0:d=0.3")
            finish.append(f"fade=t=out:st={max(0.0, duration - 0.3):.2f}:d=0.3")
        base_filter = ",".join(finish)
    elif clip_paths:
        # Loop the clip so footage shorter than the narration still fills it.
        input_args = ["-stream_loop", "-1", "-i", str(clip_paths[0])]
        base_filter = _clip_filter(duration, content_type, edge_fades=edge_fades, cinematic=cinematic)
    elif image_path:
        input_args = ["-loop", "1", "-i", str(image_path)]
        base_filter = _motion_filter(duration, scene, content_type, edge_fades=edge_fades, cinematic=cinematic)
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
        if cinematic:
            written = _write_cinematic_ass(marks, duration, ass_file, content_type)
        else:
            written = _write_karaoke_ass(marks, duration, ass_file, content_type, pop=bool(layers))
        if written:
            ass_path = escape_filter_path(str(ass_file))
            # The serif ships with the bot (fonts/), so it renders the same on
            # GitHub's Linux runner as on Windows.
            fonts_dir = escape_filter_path(str(FONTS_DIR) if cinematic else "C:/Windows/Fonts")
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
    audio_input = input_args.count("-i")
    if graph:
        command.extend(["-filter_complex", f"{graph};[anim]{vf}[vout]", "-map", "[vout]"])
    else:
        # Picture from the first input only. Some stock clips carry their own
        # sound, and without this ffmpeg may pick it over the narration.
        command.extend(["-map", "0:v:0", "-vf", vf])
    if audio_path:
        command.extend(["-map", f"{audio_input}:a:0"])
    command.extend([
        "-t",
        f"{duration:.3f}",
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


def render_video(lesson: Lesson, audio_path: Path, output_path: Path, run_dir: Path, scene_audio_paths: list[Path] | None = None, channel=None, scene_marks: list | None = None, content_type: str = "short", reuse_from: Path | None = None) -> Path:
    """Render the scenes, join them, then open the video on a preview montage."""
    result = _render_and_join(lesson, audio_path, output_path, run_dir, scene_audio_paths, channel,
                              scene_marks, content_type, reuse_from)
    try:
        add_end_card(result, run_dir, content_type)
    except Exception as exc:
        print(f"[video] end card skipped ({exc})")
    if settings.enable_preview and scene_audio_paths:
        first = max(2.5, _audio_duration(scene_audio_paths[0]) + 0.35)
        try:
            add_preview(result, run_dir, lesson.title, first, content_type,
                        question=preview_question(lesson, channel, run_dir))
        except Exception as exc:
            # The video is finished without it; a preview must never cost it.
            print(f"[video] preview skipped ({exc})")
    return result


def preview_question(lesson: Lesson, channel, run_dir: Path) -> str:
    """One curiosity question the video answers, shown big over the preview.

    "How did one man capture 132 soldiers?" makes a viewer stay to find out.
    Written from this script only, so it adds no claim of its own; on failure
    the preview shows the title instead.
    """
    cached = run_dir / "preview_question.txt"
    if cached.exists():
        return cached.read_text(encoding="utf-8").strip()
    from .genre_topics import _script_json
    from .language import is_urdu

    script = " ".join(scene.line for scene in lesson.scenes)[:2500]
    language = "Roman Urdu (Urdu in English letters)" if channel is not None and is_urdu(channel) else "English"
    try:
        raw = _script_json(
            "Write ONE short curiosity question for the opening of this video, in "
            f"{language}, at most 10 words, ending with '?'. The video must answer it. "
            "Use only what the script says: no new names, numbers or claims. "
            "Style only, never copy: 'What if the sun went dark for a day?', "
            "'Why did no one believe the warning?', 'How do you hide a whole ship?'\n"
            f"Title: {lesson.title}\nScript: {script}\n"
            'Return ONLY JSON: {"question": str}'
        )
        question = " ".join(str(raw.get("question") or "").split())
    except Exception as exc:
        print(f"[video] preview question failed ({exc})")
        question = ""
    import re as _re

    # A number in the question must be one the script says (it once wrote 132
    # where the script said "more than 130").
    numbers = set(_re.findall(r"\d[\d,.]*\d|\d", question))
    if (numbers - set(_re.findall(r"\d[\d,.]*\d|\d", script)) or not question.endswith("?")
            or len(question.split()) > 14):
        question = ""
    cached.write_text(question, encoding="utf-8")
    return question


def add_preview(video_path: Path, run_dir: Path, title: str, first_seconds: float, content_type: str,
                question: str = "") -> Path:
    """Replace the picture under the hook line with fast cuts from the whole video.

    The hook's voice stays as it is; only its picture changes, to 5-7 shots of
    0.6-0.8 s taken evenly from the rest of the video, full screen. Those shots
    carry other scenes' captions, so only the caption zone is softly blurred and
    the question is written over it. The video keeps its length.
    """
    import re as _re

    total = _audio_duration(video_path)
    span = min(first_seconds, 5.0)
    if total < first_seconds + 8:
        return video_path
    shots = max(4, min(7, round(span / 0.7)))
    shot = span / shots
    usable = total - first_seconds - shot - 0.5
    starts = [first_seconds + usable * (i + 0.5) / shots for i in range(shots)]
    W, H = _dimensions(content_type)
    # Where the karaoke captions sit (_write_karaoke_ass: bottom-aligned, MarginV).
    zone_y, zone_h = (int(H * 0.60), int(H * 0.20)) if content_type == "short" else (int(H * 0.72), int(H * 0.25))
    clean = (question or _re.sub(r"\s*\|.*$", "", str(title or ""))).strip().upper()
    width = 18 if content_type == "short" else 36
    title_file = run_dir / "preview_title.txt"
    title_file.write_text("\n".join(textwrap.wrap(clean, width=width)[:3]) or clean, encoding="utf-8")
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    title_size = 76 if content_type == "short" else 54
    # Each shot is read as its own seeked input. Splitting one input instead
    # made ffmpeg hold every frame up to the last shot in memory, which on a
    # 5-minute video starved the GitHub runner until it died.
    shot_inputs: list[str] = []
    parts = []
    for i, start in enumerate(starts):
        shot_inputs += ["-ss", f"{start:.3f}", "-t", f"{shot:.3f}", "-i", str(video_path)]
        parts.append(f"[{i + 1}:v]setpts=PTS-STARTPTS[c{i}]")
    parts.append(
        "".join(f"[c{i}]" for i in range(shots)) + f"concat=n={shots}:v=1:a=0,split[m][z]"
    )
    # Blurred, with edges that fade out (no hard band).
    feather = zone_h // 4
    parts.append(
        f"[z]crop=iw:{zone_h}:0:{zone_y},gblur=sigma=26,format=yuva420p,"
        f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='255*min(1,min(Y,H-1-Y)/{feather})'[zb]"
    )
    parts.append(
        f"[m][zb]overlay=0:{zone_y},"
        f"drawtext=textfile='{escape_filter_path(str(title_file))}'{font_arg}:fontsize={title_size}:"
        f"fontcolor=white:borderw=6:bordercolor=black@0.85:shadowx=0:shadowy=5:shadowcolor=black@0.6:"
        f"line_spacing=-14:x=(w-text_w)/2:y={zone_y}+({zone_h}-text_h)/2:"
        f"alpha='min(1,t/0.25)'[pre]"
    )
    parts.append(f"[0:v]trim=start={span:.3f},setpts=PTS-STARTPTS[rest]")
    parts.append("[pre][rest]concat=n=2:v=1:a=0,format=yuv420p[v]")
    out = run_dir / "with_preview.mp4"
    quality = _quality()
    subprocess.run([
        ffmpeg_bin(), "-y", "-v", "error", "-i", str(video_path), *shot_inputs,
        "-filter_complex", ";".join(parts),
        "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", quality["preset"], "-crf", quality["crf"],
        "-c:a", "copy", "-movflags", "+faststart", str(out),
    ], check=True)
    if abs(_audio_duration(out) - total) > 1.0:
        out.unlink(missing_ok=True)
        raise RuntimeError("preview changed the video length")
    out.replace(video_path)
    print(f"[video] preview: {shots} shots over the first {span:.1f}s")
    return video_path


def add_end_card(video_path: Path, run_dir: Path, content_type: str, seconds: float = 4.0) -> Path:
    """A small "LIKE • SUBSCRIBE • COMMENT" tag over the last seconds, gently bobbing.

    It goes near the top, clear of the captions and of the YouTube/TikTok
    buttons at the bottom; the narrator says the same (pipeline._with_end_call).
    """
    total = _audio_duration(video_path)
    if total < seconds + 6:
        return video_path
    start = total - seconds
    text_file = run_dir / "end_card.txt"
    text_file.write_text("LIKE  \u2022  SUBSCRIBE  \u2022  COMMENT", encoding="utf-8")
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    _, H = _dimensions(content_type)
    size, y = (50, int(H * 0.16)) if content_type == "short" else (38, int(H * 0.08))
    vf = (
        f"drawtext=textfile='{escape_filter_path(str(text_file))}'{font_arg}:fontsize={size}:"
        f"fontcolor=white:box=1:boxcolor=0xe53935@0.95:boxborderw=22:"
        f"x=(w-text_w)/2:y={y}+10*sin(2*PI*1.6*(t-{start:.2f})):"
        f"enable='gte(t,{start:.2f})':alpha='min(1,(t-{start:.2f})/0.3)'"
    )
    out = run_dir / "with_end_card.mp4"
    quality = _quality()
    subprocess.run([
        ffmpeg_bin(), "-y", "-v", "error", "-i", str(video_path), "-vf", vf,
        "-c:v", "libx264", "-preset", quality["preset"], "-crf", quality["crf"],
        "-c:a", "copy", "-movflags", "+faststart", str(out),
    ], check=True)
    out.replace(video_path)
    return video_path


END_SCREEN_SECONDS = 20.0


CTA_SECONDS = 4.0
# (word, button colour, text colour)
_CTA_BUTTONS = (("LIKE", "0x1e88e5", "white"), ("SHARE", "0x43a047", "white"),
                ("SUBSCRIBE", "0xe53935", "white"), ("COMMENT", "0xfdd835", "black"))


def _cta_filters(width: int, height: int, run_dir: Path, start: float, kids: bool, top: int,
                 words: tuple = (), unit: int = 1080) -> list[str]:
    """drawtext buttons, shown from `start` seconds with a short pop-in."""
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    s = width / unit
    size = int(40 * s)
    pad = int(18 * s)
    gap = int(16 * s)
    # Made-for-kids: no comments (they are off) and the grown-up is asked.
    buttons = ((("ASK A GROWN-UP TO LIKE & SUBSCRIBE", "0xe53935", "white"),) if kids else
               tuple(b for b in _CTA_BUTTONS if not words or b[0] in words))
    probe = run_dir / "_cta_probe.txt"
    rows: list[list[tuple[str, str, str, int]]] = [[]]
    used = 0
    for word, colour, ink in buttons:
        probe.write_text(word, encoding="utf-8")
        from .presenter import _text_width

        w = _text_width(probe, font or "", size) + 2 * pad if font else int(len(word) * size * 0.62) + 2 * pad
        if rows[-1] and used + gap + w > width * 0.9:
            rows.append([])
            used = 0
        rows[-1].append((word, colour, ink, w))
        used += (gap if used else 0) + w
    filters = []
    enable = f"enable='gte(t,{start:.2f})'"
    line_h = size + 2 * pad
    for r, row in enumerate(rows):
        total = sum(w for *_x, w in row) + gap * (len(row) - 1)
        x = (width - total) // 2
        y = top + r * (line_h + gap)
        for index, (word, colour, ink, w) in enumerate(row):
            file = run_dir / f"cta_{r}_{index}.txt"
            file.write_text(word, encoding="utf-8")
            filters.append(
                f"drawbox=x={x}:y={y}:w={w}:h={line_h}:color={colour}@0.95:t=fill:{enable}")
            filters.append(
                f"drawtext=textfile='{escape_filter_path(str(file))}'{font_arg}:fontsize={size}:"
                f"fontcolor={ink}:x={x}+({w}-text_w)/2:y={y}+({line_h}-text_h)/2:{enable}")
            x += w + gap
    return filters


def add_cta_overlay(video_path: Path, run_dir: Path, kids: bool = False,
                    seconds: float = CTA_SECONDS) -> Path:
    """LIKE / SHARE / SUBSCRIBE / COMMENT buttons over the last seconds of a Short.

    They sit in the upper part of the frame, clear of the captions and of
    YouTube's own buttons on the right; the audio is copied untouched.
    """
    import json

    probe = json.loads(subprocess.run(
        [settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
         "-of", "json", str(video_path)], capture_output=True, text=True, check=True).stdout)
    video = next((s for s in probe.get("streams") or [] if s.get("codec_type") == "video"), None)
    duration = float((probe.get("format") or {}).get("duration") or 0)
    if not video or duration < seconds + 3:
        return video_path
    width, height = int(video["width"]), int(video["height"])
    start = max(0.0, duration - seconds)
    filters = _cta_filters(width, height, run_dir, start, kids, top=int(height * 0.14))
    out = run_dir / "with_cta.mp4"
    subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(video_path), "-vf", ",".join(filters),
                    *_video_encode_args(), "-c:a", "copy", "-movflags", "+faststart", str(out)], check=True)
    out.replace(video_path)
    print(f"[video] last {seconds:.0f}s: {'grown-up like/subscribe' if kids else 'LIKE / SHARE / SUBSCRIBE / COMMENT'} buttons")
    return video_path


def add_end_screen(video_path: Path, run_dir: Path, seconds: float = END_SCREEN_SECONDS) -> Path:
    """Add a 20-second "WATCH NEXT / SUBSCRIBE" outro after a long video (owner's rule, 2026-11-03).

    The last frame, blurred and darkened, with two empty video frames under
    "WATCH NEXT" and a round "SUBSCRIBE" spot: where YouTube's end-screen
    elements (two videos, a subscribe button) go in Studio. It is appended
    after the narration, so it never covers the story's ending; the music
    added later runs on under it. Re-encoding only the outro and joining
    with stream copy keeps a 15-minute video from being encoded twice.
    """
    import json

    probe = json.loads(subprocess.run(
        [settings.ffprobe_bin, "-v", "error", "-show_entries",
         "stream=codec_type,width,height,r_frame_rate,sample_rate,channels,time_base",
         "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True,
    ).stdout).get("streams") or []
    video = next((s for s in probe if s.get("codec_type") == "video"), None)
    audio = next((s for s in probe if s.get("codec_type") == "audio"), None)
    if not video or not audio:
        raise RuntimeError("the video has no audio or video stream to match")
    width, height = int(video["width"]), int(video["height"])
    fps = video.get("r_frame_rate") or str(settings.video_fps)
    timescale = str(video.get("time_base", "1/15360")).split("/")[-1]
    channels = int(audio.get("channels") or 2)
    rate = str(audio.get("sample_rate") or "48000")

    still = run_dir / "end_screen_still.jpg"
    subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-sseof", "-0.2", "-i", str(video_path),
                    "-frames:v", "1", "-update", "1", "-q:v", "2", str(still)], check=True)
    font = font_path()
    font_arg = f":fontfile='{escape_filter_path(font)}'" if font else ""
    labels = {"watch": "WATCH NEXT", "sub": "SUBSCRIBE"}
    files = {}
    for key, text in labels.items():
        files[key] = run_dir / f"end_screen_{key}.txt"
        files[key].write_text(text, encoding="utf-8")
    # Where YouTube puts end-screen videos and the subscribe button, scaled to
    # this frame: two 16:9 boxes on the left, a circle on the right.
    s = width / 1920
    box_w, box_h = int(560 * s), int(315 * s)
    box_y = int(400 * s)
    boxes = [(int(140 * s), box_y), (int(140 * s) + box_w + int(60 * s), box_y)]
    cx, cy, radius = int(1580 * s), int(box_y + box_h / 2), int(150 * s)
    parts = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"boxblur=30:3,eq=brightness=-0.32:saturation=0.7",
        f"drawtext=textfile='{escape_filter_path(str(files['watch']))}'{font_arg}:fontsize={int(84 * s)}:"
        f"fontcolor=white:x={boxes[0][0]}:y={box_y - int(150 * s)}:shadowx=3:shadowy=3:shadowcolor=black@0.7",
        *(f"drawbox=x={x}:y={y}:w={box_w}:h={box_h}:color=black@0.45:t=fill,"
          f"drawbox=x={x}:y={y}:w={box_w}:h={box_h}:color=white@0.9:t={max(3, int(5 * s))}"
          for x, y in boxes),
        # The subscribe spot: a filled red ring (geq keeps it round).
        # In RGB, so the ring is drawn at full resolution in every channel.
        "format=rgb24",
        *(f"geq=r='if({ring}\\,229\\,r(X\\,Y))':g='if({ring}\\,57\\,g(X\\,Y))':b='if({ring}\\,53\\,b(X\\,Y))'"
          for ring in [f"lt(hypot(X-{cx}\\,Y-{cy})\\,{radius})*gt(hypot(X-{cx}\\,Y-{cy})\\,{radius - int(10 * s)})"]),
        f"drawtext=textfile='{escape_filter_path(str(files['sub']))}'{font_arg}:fontsize={int(58 * s)}:"
        f"fontcolor=white:box=1:boxcolor=0xe53935@0.95:boxborderw={int(18 * s)}:"
        f"x={cx}-text_w/2:y={cy + radius + int(50 * s)}",
        "fade=t=in:st=0:d=0.6",
    ]
    # LIKE / SHARE / COMMENT with the subscribe spot (the owner's request, 2026-11-12).
    try:
        # Under the video boxes; SUBSCRIBE is already the round spot.
        parts += _cta_filters(width, height, run_dir, 0.0, kids=False, top=int(880 * s),
                              words=("LIKE", "SHARE", "COMMENT"), unit=1280)
    except Exception as exc:
        print(f"[video] end screen buttons left out: {exc}")
    clip = run_dir / "end_screen.mp4"
    subprocess.run([
        ffmpeg_bin(), "-y", "-v", "error", "-loop", "1", "-framerate", fps, "-t", f"{seconds:.3f}",
        "-i", str(still), "-f", "lavfi", "-t", f"{seconds:.3f}",
        "-i", f"anullsrc=r={rate}:cl={'mono' if channels == 1 else 'stereo'}",
        "-vf", ",".join(parts) + ",format=yuv420p", "-r", fps,
        *_video_encode_args(), "-video_track_timescale", timescale,
        *_audio_encode_args(), "-ar", rate, "-ac", str(channels), "-shortest", str(clip),
    ], check=True)
    joined = run_dir / "with_end_screen.mp4"
    listing = run_dir / "end_screen_concat.txt"
    listing.write_text(f"file '{video_path.as_posix()}'\nfile '{clip.as_posix()}'\n", encoding="utf-8")
    subprocess.run([ffmpeg_bin(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(joined)], check=True)
    before = _audio_duration(video_path)
    if abs(_audio_duration(joined) - (before + seconds)) > 1.5:
        joined.unlink(missing_ok=True)
        raise RuntimeError("the end screen did not join cleanly")
    joined.replace(video_path)
    print(f"[video] end screen: {seconds:.0f}s WATCH NEXT / SUBSCRIBE")
    return video_path


def _render_and_join(lesson: Lesson, audio_path: Path, output_path: Path, run_dir: Path, scene_audio_paths: list[Path] | None = None, channel=None, scene_marks: list | None = None, content_type: str = "short", reuse_from: Path | None = None) -> Path:
    """Render the lesson. With `reuse_from` (an earlier run folder), each scene
    takes that run's picture and stock clip instead of making new ones; used
    for the second-language copy of a video (dual_language.py)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scene_count = max(1, len(lesson.scenes))
    from .stock_video import plan_clip_shots, stock_video_enabled

    if stock_video_enabled(channel) and reuse_from is None:
        # One AI call names what each scene's footage must show (stock_video.py).
        plan_clip_shots(list(lesson.scenes), run_dir)

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
            content_type, edge_fades=not transition, reuse_from=reuse_from,
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
