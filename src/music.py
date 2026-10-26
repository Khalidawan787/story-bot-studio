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


def _music_for(run_dir: Path, duration: float, seed_text: str, channel, lesson) -> tuple[Path, bool]:
    """The music file for this video and whether it must be looped to fit.

    The kids channel keeps its toy-piano tune. Every other video gets music in
    the mood of its channel and script (music_moods.py), or one of the owner's
    own tracks from data/music/ when there is one.
    """
    from .music_moods import mood_for, own_track, write_mood_music

    mood = mood_for(channel, lesson)
    track = own_track(channel, mood, seed_text)
    if track is not None:
        print(f"[music] {mood}: own track {track.name}")
        return track, True
    output = run_dir / "background_music.wav"
    if mood != "kids":
        try:
            print(f"[music] {mood}")
            return write_mood_music(output, duration, mood, seed_text), False
        except Exception as exc:
            print(f"[music] {mood} music failed ({exc}); using the plain tune")
    return _write_music_bed(output, duration, seed_text), False


# The first seconds carry the preview montage (video.add_preview): the music
# comes in strong there, then eases down to its normal level by 5.5 s.
_INTRO_LIFT = "volume='if(lt(t,4),1.8,if(lt(t,5.5),1.8-0.8*(t-4)/1.5,1))':eval=frame"


def add_background_music(video_path: Path, run_dir: Path, seed_text: str = "",
                         channel=None, lesson=None) -> Path:
    if not settings.enable_background_music:
        return video_path

    duration = _media_duration(video_path)
    music_path, looped = _music_for(run_dir, duration, seed_text or run_dir.name, channel, lesson)
    mixed_path = run_dir / "video_with_music.mp4"

    volume = settings.background_music_volume
    from .video import is_cinematic

    if is_cinematic(channel):
        # Motivation music drives the video: louder under the voice too.
        volume *= 1.5
    # An owner's tracks are mastered songs, each as loud as its maker liked:
    # bring every one to the same loudness first so no video blares.
    level = "loudnorm=I=-16:TP=-2," if looped else ""
    if settings.enable_music_ducking:
        # Duck the music whenever the narrator speaks: the voice ([0:a]) drives a
        # sidechain compressor on the music so speech stays clear, then the music
        # swells back up in the gaps. asplit keeps one clean copy of the voice for
        # the final mix and one to trigger the compressor.
        filter_complex = (
            f"[1:a]{level}volume={volume}[music];"
            "[0:a]asplit=2[voice][key];"
            # Ratio 6 (was 12): the music stays audible under the voice.
            "[music][key]sidechaincompress=threshold=0.015:ratio=6:attack=5:release=350,"
            f"{_INTRO_LIFT}[ducked];"
            # normalize=0: amix would otherwise halve the voice. loudnorm brings
            # the whole video to YouTube's -14 LUFS; videos came out at about
            # -26 dB, quiet on a phone next to other Shorts.
            "[voice][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        )
    else:
        filter_complex = (
            f"[1:a]{level}volume={volume},{_INTRO_LIFT}[music];"
            "[0:a][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        )

    subprocess.run(
        [
            ffmpeg_bin(),
            "-y",
            "-i",
            str(video_path),
            # An owner's track is repeated until the video ends (-shortest cuts it).
            *(["-stream_loop", "-1"] if looped else []),
            "-i",
            str(music_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            settings.audio_bitrate,
            "-ar",
            "48000",
            "-shortest",
            str(mixed_path),
        ],
        check=True,
    )

    video_path.unlink(missing_ok=True)
    mixed_path.replace(video_path)
    return video_path
