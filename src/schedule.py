"""Reserve globally-spaced YouTube publish times.

Videos may upload to YouTube as private as soon as rendering finishes, but their
public publishAt times are globally separated across every configured channel.
This prevents simultaneous releases even when dashboard and retry jobs overlap.
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from .config import settings
from .db import release_publish_slot, reserve_publish_slot


def next_publish_at(
    channel_id: str = "kids",
    reservation_key: str | None = None,
) -> datetime | None:
    """Atomically reserve the next global slot, always 2-3 hours after the last."""
    if not settings.youtube_schedule_uploads:
        return None
    key = reservation_key or f"auto:{uuid4().hex}"
    publish_at = reserve_publish_slot(
        reservation_key=key,
        channel=channel_id,
        interval_hours=settings.youtube_schedule_interval_hours,
        first_delay_hours=settings.youtube_schedule_first_delay_hours,
    )
    return datetime.fromisoformat(publish_at)


def cancel_publish_at(reservation_key: str) -> None:
    """Release a reservation only when its video never reached YouTube."""
    release_publish_slot(reservation_key)
