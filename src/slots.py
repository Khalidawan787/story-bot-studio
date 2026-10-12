"""Daily build slots: each channel makes its video at its own time of day.

One GitHub job may run for at most a few hours, and every channel adds about
20-25 minutes of rendering. Instead of one ever-longer morning run, the day has
four slots and each channel belongs to one; the workflow runs once per slot and
builds only that slot's channels. The cron lines in
.github/workflows/daily-videos.yml must match SLOTS_UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# (hour, minute) in UTC. Pakistan is UTC+5: 07:30, 13:30, 19:30, 01:30.
SLOTS_UTC = [(2, 30), (8, 30), (14, 30), (20, 30)]
LOCAL_OFFSET = timedelta(hours=5)
# Roughly what fits in one run before it gets uncomfortably long.
SLOT_CAPACITY = 7


def label(slot: int) -> str:
    """'07:30 AM PKT' for slot 0."""
    hour, minute = SLOTS_UTC[slot % len(SLOTS_UTC)]
    moment = datetime(2000, 1, 1, hour, minute) + LOCAL_OFFSET
    return moment.strftime("%I:%M %p PKT")


def channel_slot(channel) -> int:
    try:
        return int(getattr(channel, "slot", 0) or 0) % len(SLOTS_UTC)
    except (TypeError, ValueError):
        return 0


def current_slot(now: datetime | None = None) -> int:
    """The slot whose start time most recently passed.

    GitHub's scheduler can start a run late, sometimes by an hour or more, so
    the slot is the latest one already begun rather than the nearest one.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minutes = now.hour * 60 + now.minute
    starts = [h * 60 + m for h, m in SLOTS_UTC]
    passed = [i for i, start in enumerate(starts) if start <= minutes]
    return passed[-1] if passed else len(SLOTS_UTC) - 1


def next_run(now: datetime | None = None) -> datetime:
    """When the next slot starts (UTC)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for day in (0, 1):
        for hour, minute in SLOTS_UTC:
            moment = (now + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if moment > now:
                return moment
    return now + timedelta(days=1)


def load() -> dict[int, list]:
    """Connected channels per slot."""
    from .channels import load_channels

    from .social_accounts import facebook_auto_ready

    counts: dict[int, list] = {i: [] for i in range(len(SLOTS_UTC))}
    for channel in load_channels():
        if (channel.token_path.exists() or getattr(channel, "created_by", "") == "factory"
                or facebook_auto_ready(channel.id)):
            counts[channel_slot(channel)].append(channel)
    return counts


def least_busy() -> int:
    """The slot with the fewest channels; earlier slots win a tie."""
    counts = load()
    return min(counts, key=lambda slot: (len(counts[slot]), slot))
