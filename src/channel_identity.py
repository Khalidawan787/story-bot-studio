"""Each channel's own YouTube picture, name and numbers, for the dashboard tabs.

Read with the channel's own login (channels.list mine: 1 quota unit) and kept
for 12 hours in data/youtube_identity/<channel id>.json (not committed).
Taken from the Chronovanta bot's brand.youtube_identity (2026-11-05).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .config import settings

CACHE_DIR = settings.root / "data" / "youtube_identity"


def youtube_identity(channel, max_age_hours: float = 12) -> dict:
    cache = CACHE_DIR / f"{channel.id}.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        if datetime.now(timezone.utc) - datetime.fromisoformat(data["fetched_at"]) < timedelta(hours=max_age_hours):
            return data
    except Exception:
        data = {}
    try:
        from .youtube_accounts import channel_for_account, connected_accounts
        from .youtube_upload import _youtube

        accounts = connected_accounts(channel.id)
        if not accounts:
            return data
        own = channel_for_account(channel.id, accounts[0].id)
        found = _youtube(own.client_secret_path, own.token_path).channels().list(
            part="snippet,statistics,brandingSettings", mine=True).execute().get("items") or []
        if not found:
            return data
        item = found[0]
        thumbs = item["snippet"].get("thumbnails") or {}
        stats = item.get("statistics") or {}
        data = {
            "id": item["id"],
            "title": item["snippet"].get("title", ""),
            "handle": item["snippet"].get("customUrl", ""),
            "avatar": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""),
            "banner": ((item.get("brandingSettings") or {}).get("image") or {}).get("bannerExternalUrl", ""),
            "subs": int(stats.get("subscriberCount", 0) or 0),
            "videos": int(stats.get("videoCount", 0) or 0),
            "views": int(stats.get("viewCount", 0) or 0),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[identity] {channel.id}: YouTube picture and numbers not read ({exc})")
    return data


def short_number(n) -> str:
    """12345 -> '12.3K'."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    for size, mark in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + mark
    return str(n)


def identities(channels) -> dict:
    out = {}
    for channel in channels:
        try:
            data = dict(youtube_identity(channel))
        except Exception:
            data = {}
        if "subs" in data:
            data.update(subs_text=short_number(data["subs"]), videos_text=short_number(data["videos"]),
                        views_text=short_number(data["views"]))
        out[channel.id] = data
    return out
