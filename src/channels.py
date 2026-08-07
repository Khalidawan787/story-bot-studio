from __future__ import annotations

import dataclasses
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
    # Which connected YouTube account of this channel a job is aimed at. One
    # channel may feed several YouTube channels, each publishing different
    # topics so the same video never appears twice across them. "main" is the
    # original single account described by the fields above.
    account: str = "main"

    @property
    def client_secret_path(self) -> Path:
        """This channel's own OAuth client, falling back to the shared one.

        YouTube's upload quota (10,000 units/day, 1,600 per upload) belongs to
        the Google Cloud PROJECT, not to the channel — so every channel sharing
        one client_secret.json shares ~6 uploads a day between them. Dropping a
        `client_secret_<id>.json` from a separate Cloud project into the project
        root moves that channel onto its own quota automatically; until then it
        keeps using the shared client and nothing breaks.
        """
        own = settings.root / f"client_secret_{self.id}.json"
        return own if own.is_file() else settings.root / self.client_secret

    @property
    def token_path(self) -> Path:
        return settings.root / self.token

    @property
    def lessons_path(self) -> Path | None:
        return (settings.root / self.lessons_file) if self.lessons_file else None

    @property
    def custom_lessons_path(self) -> Path:
        # User-written custom-prompt topics (works for every channel, incl. kids).
        return settings.root / "data" / f"custom_{self.id}_lessons.json"


def _default_kids() -> Channel:
    """Fallback kids channel that reproduces the original single-channel behavior."""
    return Channel(
        id="kids", name="Kids Learning", genre="kids", builtin=True, lessons_file="",
        voice=settings.edge_voice, made_for_kids=True,
        privacy=settings.youtube_privacy_status, category_id="27",
        seo_suffix="for Kids | Fun Learning Shorts",
        image_style="cute colorful cartoon for kids, bright, friendly",
        client_secret="client_secret.json", token="token.json", topics_per_day=2,
    )


def load_channels() -> list[Channel]:
    if not CONFIG_FILE.exists():
        return [_default_kids()]
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    # Unknown keys are ignored so an older or newer channels.json still loads.
    fields = {field.name for field in dataclasses.fields(Channel)}
    return [Channel(**{k: v for k, v in c.items() if k in fields}) for c in data["channels"]]


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
