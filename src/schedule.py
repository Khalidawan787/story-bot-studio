"""Work out WHEN each upload should go public.

Publishing a whole daily batch to 'public' at the same second buries every
video — the algorithm sees them as one lump and none of them get a fair push.
Instead we upload each video as private with a future `publishAt`, spaced
`interval` hours apart, so YouTube drips them out and each gets its own window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import settings
from .db import latest_scheduled_publish_at


def _daily_slots() -> list[int]:
    """Parse the configured local publish hours, sorted and de-duplicated."""
    hours: set[int] = set()
    for part in settings.youtube_schedule_daily_slots.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour = int(part)
        except ValueError:
            continue
        if 0 <= hour <= 23:
            hours.add(hour)
    return sorted(hours)


def _earliest_after(channel_id: str) -> datetime:
    """The earliest moment a new video may go public: after the first delay AND
    strictly after the most-future slot already reserved for this channel."""
    now = datetime.now(timezone.utc)
    base = now + timedelta(hours=settings.youtube_schedule_first_delay_hours)

    last_iso = latest_scheduled_publish_at(channel_id)
    if last_iso:
        try:
            last = datetime.fromisoformat(last_iso)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            return max(base, last + timedelta(minutes=1))
        except ValueError:
            pass
    return base


def next_publish_at(channel_id: str = "kids") -> datetime | None:
    """Return the next free publish slot for a channel, or None when scheduling
    is turned off (then the video publishes immediately as before).

    With YOUTUBE_SCHEDULE_DAILY_SLOTS set, videos land on those local hours
    across the coming days — ideal for a multi-day buffer that keeps publishing
    even while the PC is off. Otherwise videos are simply spaced INTERVAL hours
    apart (the original behaviour)."""
    if not settings.youtube_schedule_uploads:
        return None

    earliest = _earliest_after(channel_id)
    slots = _daily_slots()

    if not slots:
        # Simple interval spacing off the most-future reserved slot.
        last_iso = latest_scheduled_publish_at(channel_id)
        if last_iso:
            try:
                last = datetime.fromisoformat(last_iso)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                after_last = last + timedelta(hours=settings.youtube_schedule_interval_hours)
                base = datetime.now(timezone.utc) + timedelta(hours=settings.youtube_schedule_first_delay_hours)
                return max(base, after_last)
            except ValueError:
                pass
        return earliest

    # Slot mode: find the next allowed local hour at or after `earliest`.
    earliest_local = earliest.astimezone()  # naive-free local time
    day = earliest_local.date()
    for _ in range(400):  # search up to ~400 slots (well over a year) ahead
        for hour in slots:
            candidate = earliest_local.replace(
                year=day.year, month=day.month, day=day.day,
                hour=hour, minute=0, second=0, microsecond=0,
            )
            if candidate >= earliest_local:
                return candidate.astimezone(timezone.utc)
        day = day + timedelta(days=1)
    return earliest
