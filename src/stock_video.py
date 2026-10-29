"""Free stock video clips (Pexels) used as moving scene footage.

Every channel except the kids ones looks far better with actual footage than
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

    return stock_video_wanted(channel) and bool(pexels_api_key())


def stock_video_wanted(channel=None) -> bool:
    """True when this channel is set to use stock footage (key not checked)."""
    channel_id = str(getattr(channel, "id", "") or "").lower()
    if not channel_id:
        return False
    if getattr(channel, "visuals", "") == "stock" or channel_id in settings.stock_video_channels:
        return True
    kids = channel_id == "kids" or bool(getattr(channel, "made_for_kids", False))
    return "all" in settings.stock_video_channels and not kids


# ------------------------------------------------------------ search phrases
# Clips used to be searched with the first words of the scene's title and
# picture prompt, accepted when their name shared any one word with the whole
# scene, and replaced by the channel's mood ("dark city") when nothing matched:
# the narration spoke of one thing while unrelated footage played. Now one AI
# call per video names, for every scene, a few filmable things and the word a
# clip's name must contain; a scene with no such clip shows its own picture.
SHOTS_FILE = "clip_shots.json"


def _scene_key(scene: Scene) -> str:
    return " ".join(str(scene.line or scene.label or "").split())[:160]


def plan_clip_shots(scenes: list[Scene], run_dir: Path) -> None:
    """Ask the script model for stock-footage searches for every scene, once."""
    path = run_dir / SHOTS_FILE
    if path.exists() or not scenes:
        return
    from .genre_topics import _script_json

    numbered = "\n".join(f"{i}. {_scene_key(scene)}" for i, scene in enumerate(scenes, start=1))
    prompt = (
        "You pick stock video footage (Pexels) for a narrated video. For EACH numbered line, give 3 "
        "search phrases of 2-3 words naming concrete things a camera can film today that show what "
        "the line is about (e.g. 'cargo ship sea', 'oil tanker', 'navy warship'). No names of people, "
        "brands or countries' leaders. For history, use things that still exist (old documents, "
        "castle ruins, museum armour, candle light) rather than modern people. Abstract lines get "
        "real-life scenes, never symbols or graphics: 'reports suggest' -> 'reporter writing notes', "
        "'mourning' -> 'memorial candles flowers', 'unconfirmed' -> 'newspaper headlines'. Never "
        "question marks, icons, emoji, animations or text. For each phrase also "
        "give 'must': the one main noun, singular, that the clip's title has to contain.\n"
        'Return JSON only: {"scenes": [{"n": 1, "shots": [{"q": "cargo ship sea", "must": "ship"}]}]}\n\n'
        f"Lines:\n{numbered}"
    )
    try:
        raw = _script_json(prompt)
    except Exception as exc:
        print(f"[clips] search planning skipped: {exc}")
        return
    plan: dict[str, list[dict]] = {}
    for row in raw.get("scenes") or []:
        try:
            scene = scenes[int(row.get("n")) - 1]
        except (TypeError, ValueError, IndexError):
            continue
        shots = [
            {"q": str(shot.get("q") or "").strip().lower(), "must": str(shot.get("must") or "").strip().lower()}
            for shot in row.get("shots") or [] if isinstance(shot, dict)
        ]
        shots = [shot for shot in shots if shot["q"] and re.fullmatch(r"[a-z]{3,}", shot["must"])]
        if shots:
            plan[_scene_key(scene)] = shots[:3]
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


def _planned_shots(scene: Scene, run_dir: Path) -> list[dict]:
    try:
        plan = json.loads((run_dir / SHOTS_FILE).read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(plan.get(_scene_key(scene)) or [])


def _subject_text(scene: Scene, channel=None) -> str:
    """The scene's title and picture prompt without the channel's style words
    ("news documentary photography"), which matched any clip at all."""
    prompt = str(scene.image_prompt or "")
    style = str(getattr(channel, "image_style", "") or "")
    if style and style in prompt:
        prompt = prompt.replace(style, " ")
    return f"{scene.label} {prompt}"


def _scene_queries(scene: Scene, channel=None) -> list[str]:
    """Search phrases from most to least specific, used when no AI plan exists.

    Video search is much stricter than photo search: five words usually match
    nothing. So the long phrase is tried first and then shortened. There is no
    general mood search at the end any more: footage that only fits the genre
    is footage of something else.
    """
    words = _subject_words(scene, channel)
    queries: list[str] = []
    for size in (4, 3, 2):
        if len(words) >= size:
            queries.append(" ".join(words[:size]))
    if words:
        queries.append(words[0])
    seen: list[str] = []
    for query in queries:
        if query and query not in seen:
            seen.append(query)
    return seen


def _subject_words(scene: Scene, channel=None) -> list[str]:
    from .image_assets import _STOCK_STOP_WORDS

    words: list[str] = []
    for word in re.findall(r"[A-Za-z]{3,}", _subject_text(scene, channel)):
        word = word.lower()
        if word not in _STOCK_STOP_WORDS and word not in _FILLER_WORDS and word not in words:
            words.append(word)
    return words


def _names_must(video: dict, must: str) -> bool:
    """The clip's page name contains the main noun (or its plural)."""
    words = _page_words(video)
    return bool({must, must + "s", must + "es"} & words) or (must.endswith("s") and must[:-1] in words)


# ------------------------------------------------------------ archive footage
# Wikimedia Commons holds real old film (newsreels, war footage, early cinema)
# under free licences. For a fact channel that is the honest picture of the
# past, so it is tried before modern stock footage.
_COMMONS = "https://commons.wikimedia.org/w/api.php"
_COMMONS_CACHE: dict[str, list[dict]] = {}
SUBJECT_FILE = "archive_subject.txt"


def write_subject(run_dir: Path, title: str) -> None:
    """Remember what the video is about, for the archive search."""
    if title:
        (run_dir / SUBJECT_FILE).write_text(title, encoding="utf-8")


def _commons_search(query: str) -> list[dict]:
    if query in _COMMONS_CACHE:
        return _COMMONS_CACHE[query]
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "formatversion": "2",
        "generator": "search", "gsrsearch": f"filetype:video {query}", "gsrnamespace": "6", "gsrlimit": "20",
        "prop": "videoinfo", "viprop": "url|size|mediatype|derivatives|extmetadata",
    })
    request = urllib.request.Request(f"{_COMMONS}?{params}", headers={
        "User-Agent": "StoryBotStudio/1.0 (https://github.com/Khalidawan787/story-bot-studio)"})
    with urllib.request.urlopen(request, timeout=30) as response:
        pages = json.loads(response.read().decode("utf-8")).get("query", {}).get("pages") or []
    _COMMONS_CACHE[query] = pages
    return pages


def _commons_queries(scene: Scene, run_dir: Path) -> list[str]:
    subject_file = run_dir / SUBJECT_FILE
    subject = subject_file.read_text(encoding="utf-8").strip() if subject_file.exists() else ""
    words = [w for w in re.findall(r"[A-Za-z]{4,}", f"{scene.label} {scene.image_prompt or ''}")
             if w.lower() not in _FILLER_WORDS][:3]
    queries = []
    if subject:
        queries.append(subject)
    if subject and words:
        queries.append(f"{subject} {words[0]}")
    if words:
        queries.append(" ".join(words[:2]))
    return [q for i, q in enumerate(queries) if q and q not in queries[:i]]


def _commons_clip(scene: Scene, output_path: Path, run_dir: Path, landscape: bool, min_seconds: float) -> Path:
    """A real archival film clip from Wikimedia Commons, cut to this scene."""
    import subprocess

    from .page_posts import _FREE_LICENCE
    from .visuals import ffmpeg_bin

    used_file = run_dir / "commons_used.json"
    used = json.loads(used_file.read_text(encoding="utf-8")) if used_file.exists() else {}
    tried: list[str] = []
    for query in _commons_queries(scene, run_dir):
        tried.append(query)
        query_words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", query)}
        for page in _commons_search(query):
            info = (page.get("videoinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            licence = str((meta.get("LicenseShortName") or {}).get("value") or "")
            title = str(page.get("title") or "")
            if not _FREE_LICENCE.match(licence) or (meta.get("NonFree") or {}).get("value"):
                continue
            # The file's own name must be about the query, not just mention it in a category.
            if not query_words & {w.lower() for w in re.findall(r"[A-Za-z]{3,}", title)}:
                continue
            seconds = float(meta.get("Duration", {}).get("value") or info.get("duration") or 0) or 60.0
            renditions = [d for d in info.get("derivatives") or []
                          if "webm" in str(d.get("type")) and 300 <= int(d.get("height") or 0) <= 720]
            source = (sorted(renditions, key=lambda d: -int(d.get("height") or 0)) or [{}])[0].get("src") \
                or info.get("url")
            if not source or int(info.get("size") or 0) > 400_000_000 and not renditions:
                continue
            # A different stretch of the same film for each scene that uses it.
            offset_seen = used.get(title, [])
            start = max(0.0, min(seconds - max(min_seconds, 4), seconds * random.uniform(0.1, 0.7)))
            if any(abs(start - o) < max(min_seconds, 6) for o in offset_seen):
                start = (max(offset_seen) + max(min_seconds, 6)) % max(seconds - min_seconds, 1)
            done = subprocess.run([
                ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{start:.2f}", "-i", source,
                "-t", f"{max(min_seconds, 3) + 1:.2f}", "-an", "-vf", "fps=30,format=yuv420p",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(output_path),
            ], capture_output=True, timeout=240)
            if done.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 50_000:
                output_path.unlink(missing_ok=True)
                continue
            used.setdefault(title, []).append(start)
            used_file.write_text(json.dumps(used), encoding="utf-8")
            artist = re.sub(r"<[^>]+>", "", str((meta.get("Artist") or {}).get("value") or "")).strip()
            gen = run_dir / "gen"
            gen.mkdir(exist_ok=True)
            (gen / f"{output_path.stem}.wikimedia.json").write_text(json.dumps({
                "title": title.replace("File:", "").rsplit(".", 1)[0][:90],
                "creator": " ".join(artist.split())[:80] or "Wikimedia Commons",
                "license": licence,
                "source_page": str(info.get("descriptionurl") or ""),
            }), encoding="utf-8")
            print(f"[archive] {output_path.stem}: {title} at {start:.0f}s")
            return output_path
    raise RuntimeError(f"no archive film for {', '.join(tried) or 'this scene'}")


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


# Words in a Pexels page name that mean a person is on screen. On a channel
# about real people of the past (heroes, crime cases) such a clip stands in for
# the real person: a 1918 hero was shown as a woman soldier of today.
_PEOPLE_WORDS = {
    "man", "men", "woman", "women", "girl", "girls", "boy", "boys", "person", "people",
    "child", "children", "kid", "kids", "guy", "lady", "couple", "family", "soldier",
    "soldiers", "officer", "military", "army", "student", "worker", "businessman",
    "teenager", "baby", "portrait", "face", "selfie", "model", "crowd", "male", "female",
    "patient", "nurse", "doctor", "elderly", "senior", "grandmother", "grandfather", "grandma",
    "grandpa", "mother", "father", "son", "daughter", "wife", "husband", "friend", "friends",
    "player", "athlete", "dancer", "actor", "actress", "police", "policeman", "cop", "firefighter",
    "pilot", "sailor", "veteran", "bride", "groom", "lover", "lovers", "hero", "someone", "guys",
    "judge", "lawyer", "attorney", "reporter", "journalist", "detective", "suspect", "prisoner",
    "climber", "hiker", "tourist", "fan", "fans", "audience", "speaker", "politician",
}


def _page_words(video: dict) -> set[str]:
    """Words of the clip's page name: pexels.com/video/old-church-bell-tower-123/."""
    slug = str(video.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
    return {word for word in re.findall(r"[a-z]{3,}", slug.lower())}


def _relevant(video: dict, wanted: set[str], strict_people: bool) -> bool:
    """True when the clip's own page name shares a word with what the scene is about.

    The clip was found by a search, but Pexels matches loosely: "130 prisoners"
    brought back a duck in a puddle. A clip whose name has none of the scene's
    words is not used; on fact channels neither is one with people in it.
    """
    words = _page_words(video)
    if strict_people and words & _PEOPLE_WORDS:
        return False
    if not words:
        return not strict_people
    return bool(words & wanted)


_CRIME_WORDS = re.compile(
    r"\b(?:killers?|murder\w*|suspects?|convict\w*|inmates?|fugitives?|accused|arrest\w*|charged|"
    r"shooters?|gunm[ae]n|offenders?|prison\w*|escap\w*|manhunt|crimes?|criminals?|jail\w*|"
    r"stabb\w*|kidnap\w*|assault\w*|robber\w*|homicide)\b", re.I)


def crime_text(text: str) -> bool:
    """True for a line about a crime, its suspects or its convicts."""
    return bool(_CRIME_WORDS.search(str(text or "")))


def _banned(video: dict) -> bool:
    from .image_assets import _STOCK_BANNED_WORDS

    # The page URL carries the clip's title as a slug; it is the only text the
    # video API returns to judge the content by.
    slug = str(video.get("url") or "").lower().replace("-", " ")
    return any(term in slug for term in _STOCK_BANNED_WORDS) or bool(_page_words(video) & _GRAPHIC_WORDS) or " 3d " in f" {slug} "


# Footage only: a news Short once said "reports suggest" over a cartoon pink
# question mark on a green screen.
_GRAPHIC_WORDS = {
    "animation", "animated", "cartoon", "illustration", "render", "rendering", "graphic",
    "graphics", "icon", "icons", "emoji", "symbol", "chroma", "greenscreen",
    "infographic", "abstract", "loop", "looping", "mark", "marks",
}


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
    from .verified_facts import is_verified_channel

    fact_channel = is_verified_channel(channel)
    if fact_channel:
        try:
            return _commons_clip(scene, output_path, run_dir, landscape, min_seconds)
        except Exception as exc:
            errors.append(f"archive: {exc}")
    # A stranger on screen while the narration names a killer or a suspect
    # reads as that person, so crime lines get footage without people.
    no_people = fact_channel or crime_text(f"{scene.label} {scene.line}")
    # Planned shots first: each names the noun a clip's title must contain.
    # Without a plan, the clip's title must share a word with the scene's own
    # title and picture prompt (not its whole narration, where "city" or
    # "night" matched almost anything).
    subject: set[str] = set()
    searches = [(shot["q"], shot["must"]) for shot in _planned_shots(scene, run_dir)]
    if not searches:
        subject = set(_subject_words(scene, channel))
        searches = [(query, "") for query in _scene_queries(scene, channel)]
    for query, must in searches:
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
            and (_names_must(video, must) and _relevant(video, {must}, no_people) if must
                 else _relevant(video, subject, no_people))
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
