"""Free stock video clips (Pexels) used as moving scene footage.

Real-world channels (crime, trending) look far better with actual footage than
with a still picture and a slow camera move. Pexels clips are free to use, need
no attribution, and share the same key and rate limit as Pexels photos.

Every failure here raises, and the renderer answers by falling back to the
normal still image for that one scene, so a missing clip never costs a video.
"""
from __future__ import annotations

import json
import random
import re
import urllib.parse
import urllib.request
from pathlib import Path

from .config import settings
from .models import Scene

_USER_AGENT = "StoryBotStudio/1.0"
_MIN_CLIP_BYTES = 300_000
# A 4K file is hundreds of megabytes and is scaled down to 1080p anyway.
_MAX_CLIP_BYTES = 120_000_000
# One search per distinct query per process: a 20-scene video repeats words, and
# Pexels allows 200 requests an hour across photos and videos together.
_SEARCH_CACHE: dict[tuple[str, str], list[dict]] = {}
# Filler that would waste one of the few words a video search can match on.
_FILLER_WORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "was", "were",
    "his", "her", "their", "its", "who", "what", "when", "where", "why", "how",
    "are", "has", "had", "not", "but", "over", "under", "behind", "after",
}


def stock_video_enabled(channel=None) -> bool:
    """True when this channel is set to use stock footage and a key exists."""
    from .thumbnails import pexels_api_key

    channel_id = str(getattr(channel, "id", "") or "").lower()
    return bool(channel_id) and channel_id in settings.stock_video_channels and bool(pexels_api_key())


def _scene_queries(scene: Scene, channel=None) -> list[str]:
    """Search phrases from most to least specific.

    Video search is much stricter than photo search: five words usually match
    nothing. So the long phrase is tried first and then shortened, ending on the
    channel's mood so a scene always gets footage that at least fits the genre.
    """
    from .image_assets import _STOCK_STOP_WORDS
    from .thumbnails import _GENRE_STOCK_HINT

    words: list[str] = []
    for word in re.findall(r"[A-Za-z]{3,}", f"{scene.label} {scene.image_prompt or ''}"):
        word = word.lower()
        if word not in _STOCK_STOP_WORDS and word not in _FILLER_WORDS and word not in words:
            words.append(word)
    genre = str(getattr(channel, "genre", "") or "").lower()
    mood = " ".join(_GENRE_STOCK_HINT.get(genre, "").split()[:2])

    queries: list[str] = []
    for size in (4, 3, 2):
        if len(words) >= size:
            queries.append(" ".join(words[:size]))
    if words:
        queries.append(words[0])
    if mood:
        queries.append(mood)
    seen: list[str] = []
    for query in queries:
        if query and query not in seen:
            seen.append(query)
    return seen


def _search(query: str, landscape: bool, key: str) -> list[dict]:
    orientation = "landscape" if landscape else "portrait"
    cache_key = (query, orientation)
    if cache_key in _SEARCH_CACHE:
        return _SEARCH_CACHE[cache_key]
    params = urllib.parse.urlencode({
        "query": query, "orientation": orientation, "per_page": "30",
    })
    request = urllib.request.Request(
        f"https://api.pexels.com/videos/search?{params}",
        headers={"Authorization": key, "User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    videos = list(payload.get("videos") or [])
    _SEARCH_CACHE[cache_key] = videos
    return videos


def _best_file(video: dict, landscape: bool) -> dict | None:
    """The mp4 rendition closest to 1080p, never smaller than 720p."""
    target = 1080
    best: tuple[int, dict] | None = None
    for item in video.get("video_files") or []:
        if "mp4" not in str(item.get("file_type") or "").lower() or not item.get("link"):
            continue
        width, height = int(item.get("width") or 0), int(item.get("height") or 0)
        if not width or not height or (width > height) != landscape:
            continue
        short_side = min(width, height)
        if short_side < 720:
            continue
        distance = abs(short_side - target)
        if best is None or distance < best[0]:
            best = (distance, item)
    return best[1] if best else None


def _banned(video: dict) -> bool:
    from .image_assets import _STOCK_BANNED_WORDS

    # The page URL carries the clip's title as a slug; it is the only text the
    # video API returns to judge the content by.
    slug = str(video.get("url") or "").lower().replace("-", " ")
    return any(term in slug for term in _STOCK_BANNED_WORDS)


def _used_ids(run_dir: Path) -> set[int]:
    used: set[int] = set()
    for path in run_dir.glob("clip_scene_*.pexels.json"):
        try:
            used.add(int(json.loads(path.read_text(encoding="utf-8")).get("id") or 0))
        except Exception:
            continue
    return used


def fetch_scene_clip(
    scene: Scene, output_path: Path, run_dir: Path, channel=None,
    landscape: bool = False, min_seconds: float = 0.0,
) -> Path:
    """Download one stock clip for this scene and return its path.

    Clips already used earlier in the same video are skipped, so twenty scenes
    do not show the same footage twice. A clip shorter than the narration is
    still accepted when nothing longer exists: the renderer loops it.
    """
    from .thumbnails import pexels_api_key

    key = pexels_api_key()
    if not key:
        raise RuntimeError("No Pexels API key. Get a free one at pexels.com/api.")
    used = _used_ids(run_dir)
    errors: list[str] = []
    for query in _scene_queries(scene, channel):
        try:
            videos = _search(query, landscape, key)
        except Exception as exc:
            errors.append(f"{query}: {exc}")
            # A rate limit or outage will not be fixed by a shorter query.
            break
        candidates = [
            video for video in videos
            if int(video.get("id") or 0) not in used and not _banned(video)
            and _best_file(video, landscape)
        ]
        if not candidates:
            errors.append(f"{query}: no usable clip")
            continue
        random.shuffle(candidates)
        # Long enough to cover the narration first; shorter ones only as a fallback.
        candidates.sort(key=lambda video: float(video.get("duration") or 0) < min_seconds)
        for video in candidates[:4]:
            item = _best_file(video, landscape)
            try:
                request = urllib.request.Request(
                    str(item["link"]), headers={"User-Agent": _USER_AGENT},
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    data = response.read(_MAX_CLIP_BYTES + 1)
            except Exception as exc:
                errors.append(f"{query}: {exc}")
                continue
            if not (_MIN_CLIP_BYTES <= len(data) <= _MAX_CLIP_BYTES):
                errors.append(f"{query}: clip size {len(data)} bytes rejected")
                continue
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(data)
            creator = video.get("user") or {}
            output_path.with_suffix(".pexels.json").write_text(
                json.dumps({
                    "provider": "pexels",
                    "id": video.get("id"),
                    "query": query,
                    "creator": creator.get("name"),
                    "creator_url": creator.get("url"),
                    "source_page": video.get("url"),
                    "duration": video.get("duration"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return output_path
    detail = " | ".join(errors[-3:]) if errors else "no search phrase for this scene"
    raise RuntimeError(f"No stock clip for scene '{scene.label}': {detail}")
