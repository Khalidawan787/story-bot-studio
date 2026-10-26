from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts
import edge_tts.communicate

from .config import settings

# Since 12 Oct 2026 Microsoft refuses speech on the old speech.platform.bing.com
# host with a 403 everywhere, which stopped every channel's videos. Edge itself
# now speaks through api.msedgeservices.com, which accepts the same token
# (rany2/edge-tts#490, #491). Only the old address is replaced, so a later
# edge-tts release that ships its own fix is left alone.
if "speech.platform.bing.com" in edge_tts.communicate.WSS_URL:
    from edge_tts.constants import TRUSTED_CLIENT_TOKEN

    edge_tts.communicate.WSS_URL = (
        "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
        f"?Ocp-Apim-Subscription-Key={TRUSTED_CLIENT_TOKEN}"
    )


# Microsoft answers "429 Too Many Requests" (and now and then 503) when one
# address asks for speech too fast. GitHub runners share addresses, so a whole
# video used to fail on the first refusal. Requests are now spaced out, and a
# refusal is retried after a growing pause before the video is given up.
_MIN_GAP_SECONDS = 2.0
# A long video asks for ~25 voices in a row; on a busy runner address the
# service refused one for over five minutes (a Crime long video was lost), so
# the last waits run to four minutes.
_RETRY_WAITS = (10, 20, 40, 60, 90, 120, 180, 240)
_last_request = 0.0


def _busy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(code in text for code in ("429", "503", "502", "504", "too many", "timed out", "timeout"))


def _with_retries(action, label: str):
    import random
    import time

    global _last_request
    for attempt in range(len(_RETRY_WAITS) + 1):
        wait = _MIN_GAP_SECONDS - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            return action()
        except Exception as exc:
            if attempt == len(_RETRY_WAITS) or not _busy_error(exc):
                raise
            pause = _RETRY_WAITS[attempt] + random.uniform(0, 3)
            print(f"[tts] {label}: speech service busy ({str(exc)[:60]}); retry {attempt + 1} in {pause:.0f}s")
            time.sleep(pause)


def prosody_for(channel) -> dict:
    """Speaking rate and pitch for a channel: the cinematic motivation look
    reads slower and deeper, with room between the lines."""
    from .video import is_cinematic

    if is_cinematic(channel):
        return {"rate": "-12%", "pitch": "-6Hz"}
    return {}


def _communicate(text: str, voice: str, rate: str | None, pitch: str | None):
    return edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate or settings.edge_rate,
        pitch=pitch or "+0Hz",
        volume=settings.edge_volume,
    )


async def _speak(text: str, output_path: Path, voice: str, rate: str | None = None, pitch: str | None = None) -> None:
    await _communicate(text, voice, rate, pitch).save(str(output_path))


def generate_voice(text: str, output_path: Path, voice: str | None = None,
                   rate: str | None = None, pitch: str | None = None) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _with_retries(lambda: asyncio.run(_speak(text, output_path, voice or settings.edge_voice, rate, pitch)), output_path.name)
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


async def _speak_with_marks(text: str, output_path: Path, voice: str, rate: str | None = None, pitch: str | None = None) -> list[WordMark]:
    communicate = _communicate(text, voice, rate, pitch)
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
    text: str, output_path: Path, voice: str | None = None,
    rate: str | None = None, pitch: str | None = None,
) -> tuple[Path, list[WordMark]]:
    """Generate the voice-over AND capture per-word timings for karaoke captions.

    Returns (audio_path, marks). If the service sends no word boundaries, marks is
    an empty list and callers should fall back to a plain caption.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        marks = _with_retries(
            lambda: asyncio.run(_speak_with_marks(text, output_path, voice or settings.edge_voice, rate, pitch)),
            output_path.name,
        )
    except Exception:
        # Never let caption timing break audio generation — fall back to a plain save.
        generate_voice(text, output_path, voice, rate, pitch)
        return output_path, []
    if not output_path.exists() or output_path.stat().st_size == 0:
        generate_voice(text, output_path, voice, rate, pitch)
        return output_path, []
    return output_path, marks

