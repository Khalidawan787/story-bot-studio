from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import settings


CONFIG_FILE = settings.root / "channels.json"


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    genre: str
    builtin: bool
    lessons_file: str
    voice: str
    made_for_kids: bool
    privacy: str
    category_id: str
    seo_suffix: str
    image_style: str
    client_secret: str
    token: str
    topics_per_day: int

    @property
    def client_secret_path(self) -> Path:
        return settings.root / self.client_secret

    @property
    def token_path(self) -> Path:
        return settings.root / self.token

    @property
    def lessons_path(self) -> Path | None:
        return (settings.root / self.lessons_file) if self.lessons_file else None


def _default_kids() -> Channel:
    """Fallback kids channel that reproduces the original single-channel behavior."""
    return Channel(
        id="kids", name="Kids Learning", genre="kids", builtin=True, lessons_file="",
        voice=settings.edge_voice, made_for_kids=True,
        privacy=settings.youtube_privacy_status, category_id="27",
        seo_suffix="for Kids | Fun Learning Shorts",
        image_style="cute colorful cartoon for kids, bright, friendly",
        client_secret="client_secret.json", token="token.json", topics_per_day=5,
    )


def load_channels() -> list[Channel]:
    if not CONFIG_FILE.exists():
        return [_default_kids()]
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return [Channel(**c) for c in data["channels"]]


def get_channel(channel_id: str) -> Channel:
    for ch in load_channels():
        if ch.id == channel_id:
            return ch
    raise KeyError(f"No channel with id '{channel_id}'")


# The default channel used everywhere a channel isn't specified — keeps the
# original kids behavior byte-for-byte when callers pass nothing.
def default_channel() -> Channel:
    try:
        return get_channel("kids")
    except Exception:
        return _default_kids()
