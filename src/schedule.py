"""Reserve globally-spaced YouTube publish times.

Videos may upload to YouTube as private as soon as rendering finishes, but their
public publishAt times are globally separated across every configured channel.
This prevents simultaneous releases even when dashboard and retry jobs overlap.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import settings
from .db import release_publish_slot, reserve_publish_slot


# Fixed daily release times, US Eastern (the channels' audience): two Shorts
# and one long video a day, each at an hour that kind of viewer is watching.
# YouTube, Facebook and TikTok all go live at the same time (the Facebook post
# and the TikTok post are scheduled for the video's YouTube publishAt).
RELEASE_TZ = ZoneInfo("America/New_York")
RELEASE_TIMES = {
    #                  Shorts              long
    "kids":            (("08:00", "16:00"), ("12:00",)),
    "crime":           (("13:00", "21:00"), ("19:00",)),
    "trending":        (("07:00", "17:00"), ("12:00",)),
    "heroic history":  (("11:00", "18:00"), ("14:00",)),
    "bedtime stories": (("18:30", "20:30"), ("19:00",)),
}
_DEFAULT_RELEASE = (("12:00", "18:00"), ("15:00",))


def release_times(channel_id: str, content_type: str = "short") -> tuple[str, ...]:
    """This channel's daily "HH:MM" (US Eastern) release times for one kind of video."""
    try:
        from .channels import get_channel

        channel = get_channel(channel_id)
        key = "kids" if channel.builtin else str(channel.genre).lower()
    except Exception:
        key = ""
    shorts, longs = RELEASE_TIMES.get(key, _DEFAULT_RELEASE)
    return longs if content_type == "long" else shorts


def next_publish_at(
    channel_id: str = "kids",
    reservation_key: str | None = None,
    content_type: str = "short",
) -> datetime | None:
    """Atomically reserve this channel's next free daily release time."""
    if not settings.youtube_schedule_uploads:
        return None
    key = reservation_key or f"auto:{uuid4().hex}"
    publish_at = reserve_publish_slot(
        reservation_key=key,
        channel=channel_id,
        interval_hours=settings.youtube_schedule_interval_hours,
        first_delay_hours=settings.youtube_schedule_first_delay_hours,
        fixed_times=release_times(channel_id, content_type),
        fixed_tz=RELEASE_TZ,
    )
    return datetime.fromisoformat(publish_at)


def cancel_publish_at(reservation_key: str) -> None:
    """Release a reservation only when its video never reached YouTube."""
    release_publish_slot(reservation_key)
