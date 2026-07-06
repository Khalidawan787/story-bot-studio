from __future__ import annotations

from pathlib import Path

from .config import settings


def _timestamp(seconds: float) -> str:
    millis = int((seconds - int(seconds)) * 1000)
    total = int(seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def generate_subtitles(audio_path: Path, output_path: Path, fallback_text: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if settings.enable_whisper:
        from faster_whisper import WhisperModel

        model = WhisperModel(settings.whisper_model_size, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(audio_path), beam_size=5)
        lines = []
        for index, segment in enumerate(segments, start=1):
            lines.append(str(index))
            lines.append(f"{_timestamp(segment.start)} --> {_timestamp(segment.end)}")
            lines.append(segment.text.strip())
            lines.append("")
        output_path.write_text("\n".join(lines), encoding="utf-8")
        return output_path

    sentences = [line.strip() for line in fallback_text.splitlines() if line.strip()]
    lines = []
    cursor = 0.0
    for index, sentence in enumerate(sentences, start=1):
        duration = max(2.0, min(5.0, len(sentence) / 18))
        lines.append(str(index))
        lines.append(f"{_timestamp(cursor)} --> {_timestamp(cursor + duration)}")
        lines.append(sentence)
        lines.append("")
        cursor += duration
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path

