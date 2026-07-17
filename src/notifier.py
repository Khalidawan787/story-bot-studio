from __future__ import annotations

import json
import urllib.parse
import urllib.request

from .config import settings


def is_configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def notify(message: str) -> bool:
    if not is_configured():
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": settings.telegram_chat_id, "text": message[:3500]}).encode()
    try:
        request = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            json.loads(response.read())
        return True
    except Exception:
        return False
