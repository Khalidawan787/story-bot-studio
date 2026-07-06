"""Generate genre story topics (crime/love/horror/motivation) with free Gemini.

Produces lessons in the SAME shape the render pipeline already understands
(title, category, intro, outro, scenes[label,line,image,image_prompt]) and
stores them per channel in data/<genre>_lessons.json. Kids topics still come
from the built-in topic bank — this only feeds the new genre channels.
"""
from __future__ import annotations

import re
import json
import time
import urllib.request
import urllib.error

from .config import settings
from .channels import Channel


GENRE_TONE = {
    "crime": "gripping true-crime style, suspenseful and dramatic, tasteful, NO gore or real victim names",
    "love": "warm, emotional, heartfelt romance, clean and tasteful",
    "horror": "eerie, suspenseful, atmospheric dread — NOT graphic gore, advertiser-friendly",
    "motivation": "powerful, inspiring, uplifting, action-driving",
    "mystery": "intriguing, clue-driven, keeps viewers guessing",
}


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return s or "topic"


def _extract_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b != -1:
        text = text[a:b + 1]
    return json.loads(text)


def _gemini(prompt: str) -> dict:
    key = settings.gemini_api_key
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set in .env")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent?key={key}"
    )
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read())
            return _extract_json(payload["candidates"][0]["content"]["parts"][0]["text"])
        except urllib.error.HTTPError as e:
            if e.code in (503, 429) and attempt < 3:
                time.sleep(4 * (attempt + 1))
                continue
            raise
    raise RuntimeError("Gemini failed after retries")


def _generate_one(channel: Channel, avoid_titles: list[str], scenes: int = 8) -> dict:
    tone = GENRE_TONE.get(channel.genre, "engaging and well-paced")
    avoid = "; ".join(avoid_titles[-40:]) if avoid_titles else "none yet"
    prompt = (
        f"You are a scriptwriter for a {channel.genre} YouTube channel. Tone: {tone}.\n"
        f"Write ONE fresh {channel.genre} short video script about an original, "
        f"click-worthy topic. Do NOT reuse any of these titles: {avoid}.\n"
        f"Return ONLY JSON: {{\"title\": str, \"intro\": str, \"outro\": str, "
        f"\"scenes\": [{{\"label\": str, \"line\": str, \"image_prompt\": str}}]}}.\n"
        f"Use exactly {scenes} scenes. 'line' = one narrated sentence. "
        f"'label' = a 2-4 word on-screen caption. 'image_prompt' = a visual "
        f"description of that scene (scene only, no art-style words)."
    )
    raw = _gemini(prompt)

    title = raw.get("title", "Untitled").strip()
    key = _slug(title)
    lesson_scenes = []
    for i, sc in enumerate(raw.get("scenes", []), start=1):
        lesson_scenes.append({
            "label": sc.get("label", f"Part {i}"),
            "line": sc.get("line", ""),
            "image": f"assets/generated/{key}_{i:02}.jpg",
            "image_prompt": f"{sc.get('image_prompt', title)}, {channel.image_style}",
        })

    return {
        "_key": key,
        "title": title,
        "category": channel.genre.title(),
        "intro": raw.get("intro", ""),
        "outro": raw.get("outro", ""),
        "scenes": lesson_scenes,
    }


def load_genre_lessons(channel: Channel) -> dict:
    path = channel.lessons_path
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def generate_genre_topics(channel: Channel, count: int = 5, scenes: int = 8) -> list[str]:
    """Create `count` new genre topics for a channel and save them. Returns new keys.

    scenes controls video length: ~5 scenes ≈ 30 sec, ~40 scenes ≈ 5-7 min.
    """
    if channel.builtin:
        raise ValueError("The kids channel uses the built-in topic bank, not Gemini.")

    lessons = load_genre_lessons(channel)
    existing_titles = [v.get("title", "") for v in lessons.values()]
    added: list[str] = []

    for _ in range(count):
        try:
            one = _generate_one(channel, existing_titles + added_titles(lessons, added), scenes=scenes)
        except Exception as e:
            print(f"[genre_topics] {channel.id}: generation failed ({e})")
            break
        key = one.pop("_key")
        if key in lessons:
            key = f"{key}_{len(lessons) + 1}"
        lessons[key] = one
        existing_titles.append(one["title"])
        added.append(key)

    path = channel.lessons_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lessons, indent=2, ensure_ascii=False), encoding="utf-8")
    return added


def added_titles(lessons: dict, keys: list[str]) -> list[str]:
    return [lessons[k]["title"] for k in keys if k in lessons]
