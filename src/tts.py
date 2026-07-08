from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

from .config import settings


async def _speak(text: str, output_path: Path, voice: str) -> None:
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=settings.edge_rate,
        volume=settings.edge_volume,
    )
    await communicate.save(str(output_path))


def generate_voice(text: str, output_path: Path, voice: str | None = None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_speak(text, output_path, voice or settings.edge_voice))
    return output_path


# One spoken word with its start time and duration, both in SECONDS relative to
# the start of this audio clip. Used to drive karaoke-style caption highlighting.
WordMark = tuple[str, float, float]


def _split_boundary_into_words(text: str, start: float, length: float) -> list[WordMark]:
    """Spread a sentence/word boundary's time across its words in proportion to
    word length, so each word can be highlighted in turn. Edge-TTS 7.x usually
    reports SentenceBoundary (not per word), so this gives smooth word karaoke."""
    words = [w for w in text.split() if w]
    if not words:
        return []
    if len(words) == 1:
        return [(words[0], start, length)]
    total_chars = sum(len(w) for w in words) or len(words)
    marks: list[WordMark] = []
    cursor = start
    for word in words:
        share = length * (len(word) / total_chars)
        marks.append((word, cursor, share))
        cursor += share
    return marks


async def _speak_with_marks(text: str, output_path: Path, voice: str) -> list[WordMark]:
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=settings.edge_rate,
        volume=settings.edge_volume,
    )
    marks: list[WordMark] = []
    with open(output_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            ctype = chunk.get("type")
            if ctype == "audio":
                audio_file.write(chunk["data"])
            elif ctype in ("WordBoundary", "SentenceBoundary"):
                # Edge-TTS reports offset/duration in 100-nanosecond units.
                start = chunk["offset"] / 1e7
                length = chunk["duration"] / 1e7
                text_piece = chunk.get("text", "")
                if ctype == "WordBoundary":
                    marks.append((text_piece, start, length))
                else:
                    marks.extend(_split_boundary_into_words(text_piece, start, length))
    return marks


def generate_voice_with_marks(
    text: str, output_path: Path, voice: str | None = None
) -> tuple[Path, list[WordMark]]:
    """Generate the voice-over AND capture per-word timings for karaoke captions.

    Returns (audio_path, marks). If the service sends no word boundaries, marks is
    an empty list and callers should fall back to a plain caption.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        marks = asyncio.run(_speak_with_marks(text, output_path, voice or settings.edge_voice))
    except Exception:
        # Never let caption timing break audio generation — fall back to a plain save.
        generate_voice(text, output_path, voice)
        return output_path, []
    if not output_path.exists() or output_path.stat().st_size == 0:
        generate_voice(text, output_path, voice)
        return output_path, []
    return output_path, marks

