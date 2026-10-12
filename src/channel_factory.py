"""Create a whole new channel from one line of text ("cooking for beginners").

An AI writer proposes the channel's name, tone, look, voice, search words and
first video ideas;
the person reviews that on a form, and `create_channel` writes it into
channels.json like any hand-made channel. Everything downstream (scripts,
images, SEO, uploads, the daily GitHub run) already works per channel, so a
created channel needs nothing else in code.

Channels made here carry `"created_by": "factory"`. Only those can be deleted
from the dashboard, so the original channels can never be removed by mistake.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .config import settings

CONFIG_FILE = settings.root / "channels.json"

# Edge voices the bot can speak with, as the person sees them on the form.
VOICES = {
    "en-US-GuyNeural": "Guy (US, man, calm narrator)",
    "en-US-AndrewNeural": "Andrew (US, man, warm)",
    "en-US-ChristopherNeural": "Christopher (US, man, deep)",
    "en-US-BrianNeural": "Brian (US, man, casual)",
    "en-US-AriaNeural": "Aria (US, woman, bright)",
    "en-US-JennyNeural": "Jenny (US, woman, friendly)",
    "en-US-AvaNeural": "Ava (US, woman, soft)",
    "en-GB-RyanNeural": "Ryan (UK, man)",
    "en-GB-SoniaNeural": "Sonia (UK, woman)",
}

# YouTube's own category ids.
CATEGORIES = {
    "1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music",
    "15": "Pets & Animals", "17": "Sports", "19": "Travel & Events",
    "20": "Gaming", "22": "People & Blogs", "23": "Comedy",
    "24": "Entertainment", "25": "News & Politics", "26": "Howto & Style",
    "27": "Education", "28": "Science & Technology",
}

# How scenes are pictured. Real-world topics look best as moving stock footage,
# cartoon topics as animated drawings, and anything else as a generated still.
VISUALS = {
    "stock": "Real video clips (Pexels, free)",
    "cartoon": "Animated cartoon pictures (free)",
    "still": "AI pictures with camera movement",
}

_RESERVED_IDS = {"kids", "crime", "love", "horror", "motivation", "trending", "new", "all"}


def _slug(text: str, limit: int = 24) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return slug[:limit].strip("_") or "channel"


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {"channels": []}


def _save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _clean_text(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def design_channel(topic: str) -> dict:
    """Ask the AI writer for a channel plan. Returns a spec ready for the form."""
    from .genre_topics import _script_json

    topic = _clean_text(topic, 200)
    if len(topic) < 3:
        raise ValueError("Write a few words about the channel topic first.")
    prompt = (
        "You plan faceless, fully automated YouTube channels: AI-written narration, "
        "a text-to-speech voice and generated or stock visuals. Plan ONE channel "
        f"about: \"{topic}\".\n"
        "Return ONLY JSON with these keys:\n"
        '{"name": str, "genre": str, "tone": str, "image_style": str, '
        '"seo_suffix": str, "voice": str, "category_id": str, '
        '"made_for_kids": bool, "visuals": str, "keywords": [str], '
        '"first_topics": [str]}\n'
        "- name: 2-4 word catchy channel name, no emojis.\n"
        "- genre: 1-2 lowercase words naming the niche, e.g. \"cooking\".\n"
        "- tone: one sentence describing how every script should sound.\n"
        "- image_style: art direction appended to every picture prompt.\n"
        "- seo_suffix: short title ending such as \"| Easy Cooking\".\n"
        f"- voice: exactly one of {list(VOICES)}.\n"
        f"- category_id: the id (the key, as a string) of the best fitting category in {json.dumps(CATEGORIES)}.\n"
        "- made_for_kids: true only if the audience is young children.\n"
        "- visuals: \"stock\" for real-world subjects that stock video covers well "
        "(cooking, travel, news, nature, fitness), \"cartoon\" for children or "
        "fantasy, \"still\" otherwise.\n"
        "- keywords: 10 search phrases real people type for this niche.\n"
        "- first_topics: 5 specific, click-worthy video ideas for this channel.\n"
        "Keep it advertiser-friendly. Never name brands or real people."
    )
    raw = _script_json(prompt)
    if not isinstance(raw, dict):
        raise RuntimeError("The AI writer did not return a channel plan. Try again.")
    return normalize_spec({**raw, "topic": topic})


def normalize_spec(raw: dict) -> dict:
    """Clamp an AI plan or a submitted form to values the bot can use."""
    name = _clean_text(raw.get("name"), 40) or _clean_text(raw.get("topic"), 40).title() or "New Channel"
    genre = _clean_text(raw.get("genre"), 30).lower() or _clean_text(raw.get("topic"), 30).lower()
    voice = str(raw.get("voice") or "")
    category = str(raw.get("category_id") or "").strip()
    visuals = str(raw.get("visuals") or "").strip().lower()
    keywords = raw.get("keywords") or []
    if isinstance(keywords, str):
        keywords = keywords.split(",")
    first_topics = raw.get("first_topics") or []
    if isinstance(first_topics, str):
        first_topics = first_topics.splitlines()
    made_for_kids = raw.get("made_for_kids")
    if isinstance(made_for_kids, str):
        made_for_kids = made_for_kids.strip().lower() in {"1", "true", "yes", "on"}
    return {
        "topic": _clean_text(raw.get("topic"), 200),
        "name": name,
        "genre": genre or "general",
        "tone": _clean_text(raw.get("tone"), 300) or "engaging, clear and well-paced",
        "image_style": _clean_text(raw.get("image_style"), 200) or "cinematic, detailed, high quality",
        "seo_suffix": _clean_text(raw.get("seo_suffix"), 40) or f"| {name}",
        "voice": voice if voice in VOICES else "en-US-GuyNeural",
        "category_id": category if category in CATEGORIES else "24",
        "made_for_kids": bool(made_for_kids),
        "visuals": visuals if visuals in VISUALS else "still",
        "language": "ur" if str(raw.get("language") or "").lower() == "ur" else "en",
        "keywords": [k for k in (_clean_text(k, 60).lower() for k in keywords) if k][:15],
        "first_topics": [t for t in (_clean_text(t, 150) for t in first_topics) if t][:10],
    }


def _unique_id(name: str, taken: set[str]) -> str:
    base = _slug(name)
    if base in _RESERVED_IDS:
        base = f"{base}_ch"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def create_channel(spec: dict) -> str:
    """Add the channel to channels.json and return its id."""
    spec = normalize_spec(spec)
    from .slots import least_busy

    slot = least_busy()
    config = _load_config()
    rows = config.setdefault("channels", [])
    channel_id = _unique_id(spec["name"], {str(row.get("id")) for row in rows})
    rows.append({
        "id": channel_id,
        "name": spec["name"],
        "genre": spec["genre"],
        "builtin": False,
        "lessons_file": f"data/{channel_id}_lessons.json",
        "voice": spec["voice"],
        "made_for_kids": spec["made_for_kids"],
        "privacy": "public",
        "category_id": spec["category_id"],
        "seo_suffix": spec["seo_suffix"],
        "image_style": spec["image_style"],
        # Shared until the channel gets its own Cloud project on the dashboard.
        "client_secret": "client_secret.json",
        "token": f"token_{channel_id}.json",
        "topics_per_day": 1,
        # The daily build slot with the fewest channels (slots.py).
        "slot": slot,
        "language": spec["language"],
        "tone": spec["tone"],
        "keywords": spec["keywords"],
        "visuals": spec["visuals"],
        # The first daily videos follow these, one a day, before the channel
        # has a topic bank of its own (see _automatic_long_idea).
        "ideas": spec["first_topics"],
        "topic": spec["topic"],
        "created_by": "factory",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_config(config)
    return channel_id


def set_slot(channel_id: str, slot: int) -> None:
    """Move a channel to another daily build slot."""
    from .slots import SLOTS_UTC

    slot = int(slot)
    if not 0 <= slot < len(SLOTS_UTC):
        raise ValueError("Unknown build slot.")
    config = _load_config()
    for row in config.get("channels", []):
        if row.get("id") == channel_id:
            row["slot"] = slot
            _save_config(config)
            return
    raise KeyError(f"No channel '{channel_id}'")


def set_language(channel_id: str, language: str) -> None:
    """Switch a channel's videos between English and Urdu."""
    from .language import LANGUAGES

    if language not in LANGUAGES:
        raise ValueError("Unknown language.")
    config = _load_config()
    for row in config.get("channels", []):
        if row.get("id") == channel_id:
            row["language"] = language
            _save_config(config)
            return
    raise KeyError(f"No channel '{channel_id}'")


def delete_channel(channel_id: str) -> None:
    """Remove a factory-made channel. Its files are kept as .bak copies."""
    config = _load_config()
    rows = config.get("channels", [])
    row = next((r for r in rows if r.get("id") == channel_id), None)
    if row is None:
        raise KeyError(f"No channel '{channel_id}'")
    if row.get("created_by") != "factory":
        raise PermissionError("Only channels made with 'New channel' can be deleted here.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for name in (row.get("token"), row.get("lessons_file"), f"client_secret_{channel_id}.json"):
        path = settings.root / str(name or "")
        if name and path.is_file():
            path.rename(path.with_name(f"{path.name}.{stamp}.bak"))
    config["channels"] = [r for r in rows if r.get("id") != channel_id]
    _save_config(config)


def setup_status(channel) -> dict:
    """The three steps a channel needs before it can run on its own."""
    from .channels import load_channels

    # A project shared with another channel also shares its daily upload quota.
    shared = any(
        other.id != channel.id and other.client_secret_path == channel.client_secret_path
        for other in load_channels()
    )
    # The Cloud project id inside the OAuth JSON, so the guide's console links
    # open straight on this channel's project.
    try:
        payload = json.loads(channel.client_secret_path.read_text(encoding="utf-8-sig"))
        project_id = str((payload.get("installed") or payload.get("web") or {}).get("project_id") or "")
    except Exception:
        project_id = ""
    return {
        "project": channel.client_secret_path.is_file() and not shared,
        "project_id": project_id if not shared else "",
        "project_file": channel.client_secret_path.is_file(),
        "youtube": channel.token_path.is_file(),
        "github": github_synced(),
    }


# ----------------------------------------------------------------- GitHub sync
_SYNC_FILE = settings.root / "data" / "github_sync.json"


def _fingerprint() -> str:
    """Changes whenever a file that travels to GitHub changes."""
    import hashlib
    import sys

    sys.path.insert(0, str(settings.root / "scripts"))
    import ci_secrets

    digest = hashlib.sha256()
    for path in ci_secrets._collect():
        digest.update(str(path.relative_to(settings.root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def github_synced() -> bool:
    try:
        saved = json.loads(_SYNC_FILE.read_text(encoding="utf-8"))
        return saved.get("fingerprint") == _fingerprint()
    except Exception:
        return False


def sync_to_github() -> str:
    """Send channels, logins and keys to the repository secret the daily run reads."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(settings.root / "scripts" / "setup_github_actions.py")],
        cwd=settings.root, capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "GitHub update failed")
    _SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SYNC_FILE.write_text(json.dumps({
        "fingerprint": _fingerprint(),
        "synced_at": datetime.now().isoformat(timespec="seconds"),
    }), encoding="utf-8")
    return "GitHub updated: the daily run now has every channel and login on this PC."
