"""The news anchor, lip-synced on Kaggle, full screen on a few scenes of a long video.

She is one fictional woman at a news desk. Six short idle clips of her were made
once on Kaggle (kernel "ain-anchor-idles", Wan 2.2); clips 1, 2, 3, 4 and 6 are
in data/presenter/anchor_idles/. Clip 5 has a hand gesture and is never used:
the owner wants head, eyes and lips only.

For each long video:
  1. start_anchor_job(), right after the voices are made: five scenes are
     picked, each gets a silent base clip (idle clips joined to the scene's
     voice length) and its voice as 16 kHz WAV. They go to Kaggle as a private
     dataset, and the kernel "<user>/ain-anchor-lipsync" runs LatentSync 1.5 on
     the free T4 GPU (about an hour). The video renders meanwhile.
  2. finish_anchor_job(), after the render: waits for the kernel, downloads
     lips_NN.mp4 and lays each one full screen over its scene, from the moment
     its narration starts, with an "AIN NEWS" lower third. The dataset is then
     deleted.

Switched on per channel with "anchor_lipsync": true in channels.json, and only
when KAGGLE_API_TOKEN and KAGGLE_USERNAME are set and the kaggle CLI is
installed (GitHub installs it; the PC needs nothing). Every failure is
non-fatal: the video is then uploaded without her.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import settings

IDLE_DIR = settings.root / "data" / "presenter" / "anchor_idles"
IDLE_CLIPS = (1, 2, 3, 4, 6)
KERNEL_SLUG = "ain-anchor-lipsync"
PARTS = 5
# Kaggle needs ~15 minutes to set up and ~10 per part; past this the video goes
# out without her rather than holding the whole daily run.
WAIT_MINUTES = float(os.getenv("ANCHOR_WAIT_MINUTES", "150"))
# Written next to the video when she is in it, so the upload (also a later
# retry) declares YouTube's "altered or synthetic content".
SYNTHETIC_MARKER = "synthetic_media.txt"

# Runs on Kaggle. The marker file proves the outputs are from this job and not
# left over from the kernel's previous version.
_KERNEL_CODE = '''import glob, os, subprocess, time
t0 = time.time()
def sh(cmd, check=True):
    print(f"[{time.time()-t0:6.0f}s] $ {cmd}", flush=True)
    return subprocess.run(cmd, shell=True, check=check).returncode
job = glob.glob("/kaggle/input/**/job.txt", recursive=True)[0]
src = os.path.dirname(job)
sh("pip install -q uv && uv python install 3.10 -q && uv venv -q /tmp/ls --python 3.10")
PY = "/tmp/ls/bin/python"
sh("git clone -q https://github.com/bytedance/LatentSync /tmp/LatentSync")
sh(f"cd /tmp/LatentSync && uv pip install -q --python {PY} -r requirements.txt huggingface_hub")
sh(f"cd /tmp/LatentSync && {PY} -c \\"from huggingface_hub import hf_hub_download as d; "
   "d('ByteDance/LatentSync-1.5','whisper/tiny.pt',local_dir='checkpoints'); "
   "d('ByteDance/LatentSync-1.5','latentsync_unet.pt',local_dir='checkpoints')\\"")
for base in sorted(glob.glob(f"{src}/base_*.mp4")):
    n = base[-6:-4]
    sh(f"cd /tmp/LatentSync && {PY} -m scripts.inference --unet_config_path configs/unet/stage2.yaml "
       f"--inference_ckpt_path checkpoints/latentsync_unet.pt --inference_steps 20 --guidance_scale 1.5 "
       f"--video_path {base} --audio_path {src}/voice_{n}.wav --video_out_path /kaggle/working/lips_{n}.mp4 "
       "--enable_deepcache", check=False)
open("/kaggle/working/job.txt", "w").write(open(job).read())
sh("ls -la /kaggle/working")
'''


def _kaggle() -> str | None:
    return os.getenv("KAGGLE_BIN") or shutil.which("kaggle")


def anchor_enabled(channel, content_type: str) -> bool:
    return (content_type == "long" and bool(getattr(channel, "anchor_lipsync", False))
            and bool(os.getenv("KAGGLE_API_TOKEN")) and bool(os.getenv("KAGGLE_USERNAME"))
            and _kaggle() is not None and IDLE_DIR.is_dir())


def anchor_scenes(count: int, parts: int = PARTS) -> list[int]:
    """Scene numbers (1-based) spread evenly through the video.

    Never scene 1 (the preview montage covers its picture) or the last scene
    (the end call and end card).
    """
    if count < 4:
        return []
    first, last = 2, count - 1
    if last - first + 1 <= parts:
        return list(range(first, last + 1))
    return sorted({round(first + k * (last - first) / (parts - 1)) for k in range(parts)})


def _run(command: list[str], timeout: float = 600) -> str:
    done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f"{' '.join(command[1:3])} failed: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout


def _duration(path: Path) -> float:
    from .video import _audio_duration

    return _audio_duration(path)


def _base_clip(seconds: float, out: Path, seed: str) -> None:
    """Idle clips one after another, 25 fps, silent, cut to `seconds`."""
    order = list(IDLE_CLIPS)
    random.Random(seed).shuffle(order)
    clips, total = [], 0.0
    while total < seconds + 0.5:
        number = order[len(clips) % len(order)]
        clips.append(IDLE_DIR / f"idle_{number}.mp4")
        total += 3.375
    from .visuals import ffmpeg_bin

    inputs = [arg for clip in clips for arg in ("-i", str(clip))]
    joined = "".join(f"[{i}:v]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0,fps=25[v]"
    _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", *inputs,
          "-filter_complex", joined, "-map", "[v]", "-t", f"{seconds + 0.3:.3f}",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-an", str(out)])


def start_anchor_job(channel, run_dir: Path, scene_audio_paths: list[Path], content_type: str) -> dict | None:
    """Send this video's anchor parts to Kaggle; None when she is not made."""
    if not anchor_enabled(channel, content_type) or not scene_audio_paths:
        return None
    try:
        return _start(run_dir, [Path(path) for path in scene_audio_paths])
    except Exception as exc:
        print(f"[anchor] not started, the video goes out without her: {exc}")
        return None


def _start(run_dir: Path, voices: list[Path]) -> dict:
    from .visuals import ffmpeg_bin

    kaggle = _kaggle()
    user = os.environ["KAGGLE_USERNAME"].strip().lower()
    scenes = anchor_scenes(len(voices))
    if not scenes:
        raise RuntimeError("too few scenes")
    job_dir = run_dir / "anchor_job"
    shutil.rmtree(job_dir, ignore_errors=True)
    data = job_dir / "data"
    data.mkdir(parents=True)
    for number in scenes:
        voice = voices[number - 1]
        _run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(voice),
              "-ar", "16000", "-ac", "1", str(data / f"voice_{number:02}.wav")])
        _base_clip(_duration(voice), data / f"base_{number:02}.mp4", f"{run_dir.name}:{number}")
    stamp = re.sub(r"[^0-9]", "", run_dir.name)[:14] or str(int(time.time()))
    dataset = f"{user}/ain-anchor-{stamp}"
    (data / "job.txt").write_text(dataset, encoding="utf-8")
    (data / "dataset-metadata.json").write_text(json.dumps({
        "title": f"ain anchor {stamp}", "id": dataset, "licenses": [{"name": "CC0-1.0"}],
    }), encoding="utf-8")
    _run([kaggle, "datasets", "create", "-p", str(data), "-q"], timeout=900)
    for _ in range(60):  # a new dataset takes a minute or two to be usable
        if "ready" in _run([kaggle, "datasets", "status", dataset]).lower():
            break
        time.sleep(10)
    else:
        raise RuntimeError(f"dataset {dataset} never became ready")
    kernel_dir = job_dir / "kernel"
    kernel_dir.mkdir()
    (kernel_dir / "lipsync.py").write_text(_KERNEL_CODE, encoding="utf-8")
    kernel = f"{user}/{KERNEL_SLUG}"
    (kernel_dir / "kernel-metadata.json").write_text(json.dumps({
        "id": kernel, "title": KERNEL_SLUG, "code_file": "lipsync.py", "language": "python",
        "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_internet": True,
        "dataset_sources": [dataset], "kernel_sources": [], "competition_sources": [],
    }), encoding="utf-8")
    _run([kaggle, "kernels", "push", "-p", str(kernel_dir), "--accelerator", "NvidiaTeslaT4"], timeout=300)
    handle = {"kernel": kernel, "dataset": dataset, "scenes": scenes, "started": time.time()}
    (job_dir / "job.json").write_text(json.dumps(handle), encoding="utf-8")
    print(f"[anchor] Kaggle is lip-syncing scenes {scenes} ({kernel})")
    return handle


def finish_anchor_job(handle: dict, video_path: Path, run_dir: Path, lesson,
                      scene_audio_paths: list[Path]) -> Path:
    """Wait for Kaggle and lay the anchor over her scenes. Raises on any failure."""
    kaggle = _kaggle()
    try:
        out = run_dir / "anchor_job" / "out"
        _wait_and_download(kaggle, handle, out)
        clips = {number: out / f"lips_{number:02}.mp4" for number in handle["scenes"]}
        clips = {n: path for n, path in clips.items() if path.is_file() and path.stat().st_size > 50_000}
        if not clips:
            raise RuntimeError("Kaggle made no lip-sync clip")
        result = _overlay(video_path, run_dir, lesson, clips, [Path(p) for p in scene_audio_paths])
        (video_path.parent / SYNTHETIC_MARKER).write_text(
            "The AI news anchor is in this video.", encoding="utf-8")
        print(f"[anchor] on scenes {sorted(clips)}")
        return result
    finally:
        try:
            _run([kaggle, "datasets", "delete", handle["dataset"], "-y"], timeout=120)
        except Exception as exc:
            print(f"[anchor] could not delete the Kaggle dataset {handle['dataset']} ({exc})")


def _wait_and_download(kaggle: str, handle: dict, out: Path) -> None:
    deadline = handle["started"] + WAIT_MINUTES * 60
    while True:
        status = _run([kaggle, "kernels", "status", handle["kernel"]]).lower()
        if "error" in status or "cancel" in status:
            raise RuntimeError(f"Kaggle kernel stopped: {status.strip()}")
        if "complete" in status:
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            _run([kaggle, "kernels", "output", handle["kernel"], "-p", str(out), "-o", "-q"], timeout=900)
            marker = out / "job.txt"
            if marker.is_file() and marker.read_text(encoding="utf-8").strip() == handle["dataset"]:
                return
            # The status still belongs to the kernel's previous version.
        if time.time() > deadline:
            raise RuntimeError(f"Kaggle did not finish in {WAIT_MINUTES:.0f} minutes")
        time.sleep(60)


def _headline(lesson) -> str:
    title = re.split(r"\s*\|\s*", str(getattr(lesson, "title", "") or ""))[0].strip().upper()
    return title if len(title) <= 48 else title[:47].rsplit(" ", 1)[0] + "…"


def _overlay(video_path: Path, run_dir: Path, lesson, clips: dict[int, Path], voices: list[Path]) -> Path:
    """Each clip full screen from its scene's narration start, with a lower third."""
    from .video import _dimensions, _video_encode_args
    from .visuals import escape_filter_path, ffmpeg_bin

    width, height = _dimensions("long")
    # Scene i starts at the sum of the holds before it (video._crossfade_join).
    holds = [max(2.5, _duration(path) + 0.35) for path in voices]
    headline = run_dir / "anchor_headline.txt"
    headline.write_text(_headline(lesson), encoding="utf-8")
    badge = run_dir / "anchor_badge.txt"
    badge.write_text("AIN NEWS", encoding="utf-8")
    font = settings.root / "fonts" / "ArchivoBlack-Regular.ttf"
    font_arg = f":fontfile='{escape_filter_path(str(font))}'" if font.is_file() else ""
    bar_y, bar_h = int(height * 0.835), int(height * 0.085)
    badge_w = int(width * 0.155)
    inputs: list[str] = []
    parts: list[str] = []
    current = "0:v"
    windows: list[str] = []
    for index, (number, clip) in enumerate(sorted(clips.items()), start=1):
        start = sum(holds[:number - 1])
        end = start + min(_duration(clip), holds[number - 1] - 0.2)
        window = f"between(t,{start:.3f},{end:.3f})"
        windows.append(window)
        inputs += ["-i", str(clip)]
        parts.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={settings.video_fps},setpts=PTS-STARTPTS+{start:.3f}/TB[a{index}]"
        )
        parts.append(f"[{current}][a{index}]overlay=0:0:enable='{window}':eof_action=pass[v{index}]")
        current = f"v{index}"
    shown = "+".join(windows)
    text_size = int(bar_h * 0.48)
    parts.append(
        f"[{current}]"
        f"drawbox=x=0:y={bar_y}:w={width}:h={bar_h}:color=0x0b1f4d@0.92:t=fill:enable='{shown}',"
        f"drawbox=x=0:y={bar_y}:w={badge_w}:h={bar_h}:color=0xd71920:t=fill:enable='{shown}',"
        f"drawtext=textfile='{escape_filter_path(str(badge))}'{font_arg}:fontsize={text_size}:fontcolor=white:"
        f"x=({badge_w}-text_w)/2:y={bar_y}+({bar_h}-text_h)/2:enable='{shown}',"
        f"drawtext=textfile='{escape_filter_path(str(headline))}'{font_arg}:fontsize={text_size}:fontcolor=white:"
        f"x={badge_w + 36}:y={bar_y}+({bar_h}-text_h)/2:enable='{shown}',format=yuv420p[v]"
    )
    output = run_dir / "video_anchor.mp4"
    subprocess.run(
        [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path), *inputs,
         "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "0:a?",
         *_video_encode_args(), "-c:a", "copy", str(output)],
        check=True,
    )
    if abs(_duration(output) - _duration(video_path)) > 1.0:
        output.unlink(missing_ok=True)
        raise RuntimeError("the anchor overlay changed the video length")
    output.replace(video_path)
    return video_path
