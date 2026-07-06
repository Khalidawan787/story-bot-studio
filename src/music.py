from __future__ import annotations

import math
import random
import struct
import subprocess
import wave
from pathlib import Path

from .config import settings
from .visuals import ffmpeg_bin, ffprobe_bin


MELODIES = [
    [523.25, 659.25, 783.99, 659.25, 587.33, 698.46, 783.99, 659.25],
    [392.00, 493.88, 587.33, 659.25, 587.33, 493.88, 440.00, 523.25],
    [440.00, 523.25, 659.25, 783.99, 659.25, 587.33, 523.25, 493.88],
    [349.23, 440.00, 523.25, 587.33, 659.25, 587.33, 523.25, 440.00],
    [261.63, 329.63, 392.00, 523.25, 493.88, 392.00, 329.63, 392.00],
    [587.33, 659.25, 783.99, 880.00, 783.99, 659.25, 587.33, 523.25],
]

CHORDS = [
    [261.63, 329.63, 392.00],
    [293.66, 369.99, 440.00],
    [349.23, 440.00, 523.25],
    [392.00, 493.88, 587.33],
]


def _media_duration(path: Path) -> float:
    result = subprocess.run(
        [
            ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _write_music_bed(output_path: Path, duration: float, seed_text: str) -> Path:
    rng = random.Random(seed_text)
    sample_rate = 44100
    melody = rng.choice(MELODIES)
    chord = rng.choice(CHORDS)
    beat_seconds = rng.choice([0.58, 0.62, 0.66, 0.72])
    swing = rng.choice([0.0, 0.025, 0.045])
    root = chord[0] / 2.0
    total_samples = max(1, int(duration * sample_rate))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(output_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)

        for sample_index in range(total_samples):
            time_value = sample_index / sample_rate
            step = int(time_value / beat_seconds)
            note_index = step % len(melody)
            freq = melody[note_index]
            local_beat = beat_seconds + (swing if step % 2 else 0.0)
            beat_pos = (time_value % local_beat) / local_beat
            attack = min(1.0, beat_pos * 9.0)
            decay = max(0.0, 1.0 - beat_pos * 0.82)
            pluck_env = attack * decay
            bell_env = attack * max(0.0, 1.0 - beat_pos * 1.35)
            fade = min(1.0, time_value / 1.4, max(0.0, (duration - time_value) / 1.4))
            pan = math.sin(step * 0.9) * 0.18

            toy_piano = (
                math.sin(2.0 * math.pi * freq * time_value)
                + 0.22 * math.sin(2.0 * math.pi * freq * 2.0 * time_value)
                + 0.08 * math.sin(2.0 * math.pi * freq * 3.0 * time_value)
            )
            music_box = math.sin(2.0 * math.pi * (freq * 2.0) * time_value) * bell_env
            pad = sum(math.sin(2.0 * math.pi * tone * time_value) for tone in chord) / len(chord)
            bass = math.sin(2.0 * math.pi * root * time_value)
            shaker = 0.0
            if beat_pos < 0.10:
                shaker = (rng.random() * 2.0 - 1.0) * (1.0 - beat_pos / 0.10)

            value = toy_piano * pluck_env * 0.10
            value += music_box * 0.035
            value += pad * 0.045
            value += bass * 0.035
            value += shaker * 0.012
            value *= fade
            value = math.tanh(value * 1.25) * 0.85
            left = value * (1.0 - pan)
            right = value * (1.0 + pan)
            wav.writeframes(
                struct.pack(
                    "<hh",
                    int(max(-1.0, min(1.0, left)) * 32767),
                    int(max(-1.0, min(1.0, right)) * 32767),
                )
            )

    return output_path


def add_background_music(video_path: Path, run_dir: Path, seed_text: str = "") -> Path:
    if not settings.enable_background_music:
        return video_path

    duration = _media_duration(video_path)
    music_path = _write_music_bed(run_dir / "background_music.wav", duration, seed_text or run_dir.name)
    mixed_path = run_dir / "video_with_music.mp4"

    subprocess.run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(music_path),
            "-filter_complex",
            f"[1:a]volume={settings.background_music_volume}[music];[0:a][music]amix=inputs=2:duration=first:dropout_transition=0[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(mixed_path),
        ],
        check=True,
    )

    video_path.unlink(missing_ok=True)
    mixed_path.replace(video_path)
    return video_path
