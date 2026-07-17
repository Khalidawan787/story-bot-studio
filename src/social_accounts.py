from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import settings

STORE = settings.root / "credentials" / "social_accounts.json"
_LOCK = threading.Lock()
GUIDES = {
    "youtube": "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
    "facebook": "https://www.postman.com/meta/facebook/folder/simabyk/reels-publishing",
    "facebook_app": "https://developers.facebook.com/apps/",
    "tiktok": "https://developers.tiktok.com/doc/content-posting-api-get-started/",
    "tiktok_app": "https://developers.tiktok.com/apps/",
}


def _load_all() -> dict:
    if not STORE.exists():
        return {}
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    temp = STORE.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(STORE)


def account(channel_id: str, platform: str) -> dict:
    return dict(_load_all().get(channel_id, {}).get(platform, {}))


def save_account(channel_id: str, platform: str, values: dict[str, object]) -> dict:
    if platform not in {"facebook", "tiktok"}:
        raise ValueError("Unsupported social platform")
    allowed = {
        "facebook": {"page_id", "page_access_token", "auto_upload"},
        "tiktok": {"client_key", "client_secret", "access_token", "refresh_token", "mode", "privacy", "auto_upload"},
    }[platform]
    with _LOCK:
        data = _load_all()
        channel = data.setdefault(channel_id, {})
        current = dict(channel.get(platform, {}))
        for key, value in values.items():
            if key not in allowed:
                continue
            if isinstance(value, str):
                value = value.strip()
                if not value and key not in {"mode", "privacy"}:
                    continue  # blank secret/input keeps the existing value
            current[key] = value
        if platform == "tiktok":
            current.setdefault("mode", "draft")
            current.setdefault("privacy", "SELF_ONLY")
        current.setdefault("auto_upload", False)
        channel[platform] = current
        _save_all(data)
        return dict(current)


def public_status(channel_id: str) -> dict:
    fb = account(channel_id, "facebook")
    tt = account(channel_id, "tiktok")
    return {
        "guides": GUIDES,
        "facebook": {
            "configured": bool(fb.get("page_id") and fb.get("page_access_token")),
            "page_id": fb.get("page_id", ""),
            "auto_upload": bool(fb.get("auto_upload")),
        },
        "tiktok": {
            "configured": bool(tt.get("access_token")),
            "client_key": tt.get("client_key", ""),
            "mode": tt.get("mode", "draft"),
            "privacy": tt.get("privacy", "SELF_ONLY"),
            "auto_upload": bool(tt.get("auto_upload")),
        },
    }