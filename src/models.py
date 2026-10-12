from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Scene:
    label: str
    line: str
    image: str
    image_prompt: str = ""
    # What the narrator says when it differs from the shown `line`: Urdu
    # script for a Roman Urdu line (see language.py). Empty = say `line`.
    speech: str = ""


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

    @property
    def spoken_narration(self) -> str:
        """The text for the voice: Urdu-script `speech` lines when present."""
        if any(scene.speech for scene in self.scenes):
            return "\n".join(scene.speech or scene.line for scene in self.scenes)
        return self.narration


@dataclass(frozen=True)
class RenderedAssets:
    job_id: str
    run_dir: Path
    audio_path: Path
    video_path: Path
    subtitle_path: Path | None
    thumbnail_path: Path

