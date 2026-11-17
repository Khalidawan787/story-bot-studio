"""A fingerprint of every thumbnail the bot sets, to check YouTube shows that one.

The owner's request (2026-11-17): when a video goes up, the bot remembers the
thumbnail it made; the daily audit then looks at the picture YouTube really
shows and, if it is not ours (a frame YouTube picked, an older card, another
video's card) or was made for another channel, sets ours again.

Only a fingerprint is kept (bot.sqlite3, table thumbnail_prints, saved with
the bot's state): a 16x16 difference hash of the picture with black bars
trimmed, so YouTube's letterboxed / pillarboxed / rescaled copies still match.
The thumbnail itself lives only in the run folder, so a re-set draws the card
again (thumbnail_audit.thumbnail_for).
"""
from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Bits that may differ (of 256) and still be the same picture. Measured on
# 17 Nov 2026: our card against YouTube's copies 0-40, other cards 90+.
SAME_LIMIT = 60


def _table(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS thumbnail_prints (
        youtube_id TEXT PRIMARY KEY, channel TEXT, kind TEXT, print TEXT NOT NULL,
        set_at TEXT NOT NULL)""")


def _gray(image):
    import cv2

    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _trim(gray):
    """Cut black bars (YouTube pads a tall card into a wide frame and back)."""
    import numpy as np

    rows = np.where(gray.mean(axis=1) > 14)[0]
    cols = np.where(gray.mean(axis=0) > 14)[0]
    if len(rows) < 8 or len(cols) < 8:
        return gray
    return gray[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]


def _card(gray, short: bool | None):
    """The card itself: black bars cut, and for a Short shown in a wide frame
    (YouTube puts the tall card in the middle with a blurred copy on each side)
    only the middle 9:16 strip."""
    gray = _trim(gray)
    h, w = gray.shape[:2]
    if short and w > h:
        strip = int(round(h * 9 / 16))
        left = max(0, (w - strip) // 2)
        gray = gray[:, left:left + strip]
    return gray


def fingerprint_image(image, short: bool | None = None) -> str:
    import cv2

    small = cv2.resize(_card(_gray(image), short), (17, 16), interpolation=cv2.INTER_AREA).astype(int)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return "".join("1" if b else "0" for b in bits)


def fingerprint(path: str | Path) -> str:
    import cv2
    import numpy as np

    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"not an image: {path}")
    return fingerprint_image(image)


def distance(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def remember(youtube_id: str, thumbnail_path: str | Path, channel_id: str = "", kind: str = "") -> None:
    """Save the fingerprint of the thumbnail just set on this video. Never fails the caller."""
    try:
        from .db import connect

        print_ = fingerprint(thumbnail_path)
        if not kind:
            import cv2
            import numpy as np

            image = cv2.imdecode(np.fromfile(str(thumbnail_path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            kind = "short" if image is not None and image.shape[0] > image.shape[1] else "long"
        conn = connect()
        try:
            _table(conn)
            old = conn.execute("SELECT channel FROM thumbnail_prints WHERE youtube_id = ?", (youtube_id,)).fetchone()
            conn.execute(
                "INSERT OR REPLACE INTO thumbnail_prints (youtube_id, channel, kind, print, set_at) VALUES (?, ?, ?, ?, ?)",
                (youtube_id, channel_id or (old[0] if old else ""), kind, print_,
                 datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[thumbnail] fingerprint not saved for {youtube_id} ({exc})")


def recall(youtube_id: str) -> dict | None:
    from .db import connect

    conn = connect()
    try:
        _table(conn)
        row = conn.execute("SELECT channel, kind, print, set_at FROM thumbnail_prints WHERE youtube_id = ?",
                           (youtube_id,)).fetchone()
    finally:
        conn.close()
    return dict(zip(("channel", "kind", "print", "set_at"), row)) if row else None


def _download(url: str):
    import cv2
    import numpy as np

    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 story-bot-studio"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
    except urllib.error.HTTPError:
        return None
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def shown_on_youtube(youtube_id: str, short: bool):
    """The picture YouTube shows: a Short's Shorts-tab card when there is one, else the video's thumbnail."""
    names = ("sardefault", "hqdefault") if short else ("hq720", "hqdefault")
    for name in names:
        image = _download(f"https://i.ytimg.com/vi/{youtube_id}/{name}.jpg")
        if image is not None:
            return image
    return None


def on_youtube_now(youtube, youtube_id: str):
    """The video's current thumbnail as YouTube serves it to the channel owner.

    The API gives signed i.ytimg links that work while the video is still
    private or scheduled, so a fresh upload can be checked within seconds.
    """
    items = youtube.videos().list(part="snippet", id=youtube_id).execute().get("items") or []
    if not items:
        return None
    thumbs = items[0]["snippet"].get("thumbnails") or {}
    for size in ("maxres", "standard", "high", "medium"):
        url = (thumbs.get(size) or {}).get("url")
        if url:
            image = _download(url)
            if image is not None:
                return image
    return None


def confirm_after_upload(youtube, youtube_id: str, saved_card: str | Path, short: bool,
                         set_again=None, tries: int = 6, wait: float = 20.0) -> tuple[bool, str]:
    """Seconds after the upload: does YouTube show our saved card? If not, set the
    SAME saved card again once (set_again) and look again (the owner's request,
    2026-11-17). Returns (confirmed, words for the log)."""
    import time

    import cv2
    import numpy as np

    card = cv2.imdecode(np.fromfile(str(saved_card), dtype=np.uint8), cv2.IMREAD_COLOR)
    if card is None:
        return False, f"the saved card cannot be read: {saved_card}"
    ours = fingerprint_image(card, short)
    gap = None
    for attempt in range(tries):
        time.sleep(wait)
        try:
            image = on_youtube_now(youtube, youtube_id)
        except Exception as exc:
            image = None
            print(f"[thumbnail] {youtube_id}: could not read YouTube's thumbnail ({exc})")
        if image is not None:
            gap = distance(fingerprint_image(image, short), ours)
            if gap <= SAME_LIMIT:
                return True, f"confirmed on YouTube: our card is on (difference {gap})"
        if attempt == tries // 2 - 1 and set_again is not None:
            # Half the waiting is over and YouTube still shows something else:
            # the saved card once more, then keep looking.
            print(f"[thumbnail] {youtube_id}: YouTube still shows another picture"
                  + (f" (difference {gap})" if gap is not None else "") + "; setting the saved card again")
            try:
                set_again()
            except Exception as exc:
                return False, f"setting it again failed: {exc}"
    return False, ("YouTube still shows another picture"
                   + (f" (difference {gap})" if gap is not None else " (not readable yet)")
                   + "; the daily audit will set it again")


def check(youtube_id: str, channel_id: str, short: bool) -> tuple[str, str]:
    """('ok' | 'not_ours' | 'wrong_channel' | 'unknown', words for the log)."""
    saved = recall(youtube_id)
    if saved is None:
        return "unknown", "no fingerprint saved (set before 17 Nov)"
    if saved["channel"] and saved["channel"] != channel_id:
        return "wrong_channel", f"its card was made for {saved['channel']}"
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["set_at"])
    except ValueError:
        age = None
    if age is not None and age.total_seconds() < 12 * 3600:
        # YouTube's picture cache lags a fresh set by minutes to hours; judged
        # now, the bot would re-set its own card again and again.
        return "unknown", "set less than 12 hours ago; YouTube may still show the old picture"
    image = shown_on_youtube(youtube_id, short)
    if image is None:
        return "unknown", "YouTube shows no picture yet (not public)"
    gap = distance(fingerprint_image(image, short), saved["print"])
    if gap > SAME_LIMIT:
        return "not_ours", f"YouTube shows another picture (difference {gap})"
    return "ok", f"our card (difference {gap})"
