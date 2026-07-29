from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .visuals import ffmpeg_bin, ffprobe_bin


def inspect_video(video_path: Path, content_type: str) -> dict[str, object]:
    """Return a machine-readable upload quality report without modifying media."""
    issues: list[str] = []
    probe = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video_path)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    data = json.loads(probe.stdout)
    streams = data.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0)
    width = int((video or {}).get("width") or 0)
    height = int((video or {}).get("height") or 0)
    size = video_path.stat().st_size if video_path.exists() else 0

    if not video:
        issues.append("missing video stream")
    if not audio:
        issues.append("missing audio stream")
    if size < 150_000:
        issues.append("video file is too small or incomplete")
    if content_type == "long":
        if not (270 <= duration <= 480):
            issues.append(f"long duration is {duration / 60:.1f} minutes; expected about 5 minutes")
        if not (width > height and 1.65 <= (width / max(1, height)) <= 1.90):
            issues.append(f"long video orientation is wrong ({width}x{height})")
    else:
        if not (5 <= duration <= 180):
            issues.append(f"Short duration is {duration:.1f} seconds")
        if height <= width:
            issues.append(f"Short must be vertical ({width}x{height})")

    black_seconds = 0.0
    try:
        black = subprocess.run(
            [ffmpeg_bin(), "-v", "info", "-i", str(video_path), "-vf",
             "scale=320:-2,blackdetect=d=3:pix_th=0.10:pic_th=0.98", "-an", "-f", "null", os.devnull],
            capture_output=True, text=True, timeout=180,
        )
        black_seconds = sum(float(value) for value in re.findall(r"black_duration:([0-9.]+)", black.stderr))
        if black_seconds > max(5.0, duration * 0.20):
            issues.append(f"too much black screen ({black_seconds:.1f}s)")
    except Exception:
        pass

    mean_volume = None
    try:
        volume = subprocess.run(
            [ffmpeg_bin(), "-v", "info", "-i", str(video_path), "-af", "volumedetect",
             "-vn", "-sn", "-dn", "-f", "null", os.devnull],
            capture_output=True, text=True, timeout=180,
        )
        match = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", volume.stderr)
        if match:
            mean_volume = float(match.group(1))
            if mean_volume < -40:
                issues.append(f"audio is too quiet ({mean_volume:.1f} dB)")
    except Exception:
        pass

    return {
        "passed": not issues, "issues": issues, "duration": round(duration, 2),
        "width": width, "height": height, "file_mb": round(size / 1048576, 2),
        "black_seconds": round(black_seconds, 2), "mean_volume_db": mean_volume,
    }
