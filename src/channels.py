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
    # Set on channels made from a topic on the dashboard (see channel_factory).
    # Empty on the original channels, which keep their built-in genre settings.
    tone: str = ""
    keywords: tuple = ()
    visuals: str = ""
    ideas: tuple = ()
    topic: str = ""
    created_by: str = ""
    created_at: str = ""
    # Which daily build slot (see slots.py) makes this channel's video.
    slot: int = 0
    # Video language: "en" (default) or "ur" for Roman Urdu (language.py).
    language: str = "en"
    # True stories about real people only: every script is written from a
    # Wikipedia article and checked against it (verified_facts.py).
    verified_facts: bool = False
    # A fact-only channel can stay on one country's people ("USA"), or walk
    # every country in turn ("world", verified_facts.current_country); each
    # video also goes into its "<country> Heroes" playlist (playlists.py).
    focus_country: str = ""
    # The fixed AI presenter (presenter.py) on the thumbnail and in a corner of
    # the video.
    presenter: bool = False
    # The framed news thumbnail, with the anchor on long videos (news_thumbnail.py).
    news_thumbnail: bool = False
    # A news channel's audience: ("US", "EU") reads American, then European
    # sources (trending._REGION_FEEDS). Empty keeps the world mix.
    news_regions: tuple = ()
    # Long videos only on these weekdays ("Mon", "Wed", "Fri"); empty = every
    # day. long_scenes sets their length (55 scenes ~ 14-16 minutes); 0 keeps
    # the daily run's default (~5 minutes).
    long_days: tuple = ()
    long_scenes: int = 0

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
    # JSON lists become tuples so a Channel stays immutable and hashable.
    return [
        Channel(**{k: tuple(v) if isinstance(v, list) else v for k, v in c.items() if k in fields})
        for c in data["channels"]
    ]


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
