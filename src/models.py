from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scene:
    label: str
    line: str
    image: str
    image_prompt: str = ""


@dataclass(frozen=True)
class Lesson:
    topic_key: str
    title: str
    category: str
    intro: str
    outro: str
    scenes: list[Scene]

    @property
    def narration(self) -> str:
        parts = [self.intro, *[scene.line for scene in self.scenes], self.outro]
        return "\n".join(parts)


@dataclass(frozen=True)
class RenderedAssets:
    job_id: str
    run_dir: Path
    audio_path: Path
    video_path: Path
    subtitle_path: Path | None
    thumbnail_path: Path

