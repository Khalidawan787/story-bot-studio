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

