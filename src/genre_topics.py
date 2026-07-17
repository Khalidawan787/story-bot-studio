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
    "kids": "simple, cheerful, gentle and 100% safe for young children",
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


def _gemini(prompt: str, model: str | None = None) -> dict:
    key = settings.gemini_api_key
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set in .env")
    model = model or settings.gemini_model
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
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
            # 429 = daily free quota exhausted: don't burn time retrying the same
            # model, let the caller fall through to the next free provider.
            if e.code == 429:
                raise
            if e.code == 503 and attempt < 3:
                time.sleep(4 * (attempt + 1))
                continue
            raise
    raise RuntimeError("Gemini failed after retries")


def _groq_json(prompt: str) -> dict:
    """Free, fast Groq fallback (OpenAI-compatible). Needs a free GROQ_API_KEY."""
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY not set")
    body = json.dumps({
        "model": settings.groq_model,
        "temperature": 1.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return valid JSON only. Follow exact scene and word-count requirements."},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.groq_api_key}",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read())
            return _extract_json(payload["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            if e.code == 503 and attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            raise
    raise RuntimeError("Groq failed after retries")


def _pollinations_text_json(prompt: str) -> dict:
    """Free, no-key text fallback (OpenAI-compatible endpoint)."""
    body = json.dumps({
        "model": "openai",
        "temperature": 1.0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return valid JSON only. Follow exact scene and word-count requirements."},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://text.pollinations.ai/openai",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "StoryBotStudio/1.0"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
                text = payload["choices"][0]["message"]["content"]
            except (json.JSONDecodeError, KeyError, IndexError):
                text = raw  # some responses come back as the raw JSON body
            return _extract_json(text)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < 2:
                time.sleep(4 * (attempt + 1))
                continue
            raise
    raise RuntimeError("Pollinations text failed after retries")


def _openai_json(prompt: str) -> dict:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model=settings.openai_model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Return valid JSON only. Follow exact scene and word-count requirements."},
            {"role": "user", "content": prompt},
        ],
    )
    return _extract_json(response.choices[0].message.content or "{}")


def _script_json(prompt: str) -> dict:
    """Try free providers first, then paid OpenAI as a last resort.

    Order: Gemini primary model -> Gemini fallback model (separate free quota)
    -> Groq (free key) -> Pollinations text (free, no key) -> OpenAI (paid).
    When one provider's daily limit is full, the next free one keeps scripts
    flowing instead of failing the whole video.
    """
    errors: list[str] = []
    if settings.gemini_api_key:
        try:
            return _gemini(prompt)
        except Exception as exc:
            errors.append(f"Gemini({settings.gemini_model}): {exc}")
        fallback_model = settings.gemini_fallback_model
        if fallback_model and fallback_model != settings.gemini_model:
            try:
                return _gemini(prompt, model=fallback_model)
            except Exception as exc:
                errors.append(f"Gemini({fallback_model}): {exc}")
    if settings.groq_api_key:
        try:
            return _groq_json(prompt)
        except Exception as exc:
            errors.append(f"Groq: {exc}")
    if settings.enable_pollinations_text:
        try:
            return _pollinations_text_json(prompt)
        except Exception as exc:
            errors.append(f"Pollinations: {exc}")
    if settings.openai_api_key:
        try:
            return _openai_json(prompt)
        except Exception as exc:
            errors.append(f"OpenAI: {exc}")
    raise RuntimeError("Script providers failed: " + " | ".join(errors))


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
    raw = _script_json(prompt)

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


def _build_lesson(channel: Channel, raw: dict, fallback_title: str) -> tuple[str, dict]:
    title = (raw.get("title") or fallback_title).strip()
    key = _slug(title)
    lesson_scenes = []
    for i, sc in enumerate(raw.get("scenes", []), start=1):
        lesson_scenes.append({
            "label": sc.get("label", f"Part {i}"),
            "line": sc.get("line", ""),
            "image": f"assets/generated/{key}_{i:02}.jpg",
            "image_prompt": f"{sc.get('image_prompt', title)}, {channel.image_style}",
        })
    return key, {
        "title": title,
        "category": channel.genre.title(),
        "intro": raw.get("intro", ""),
        "outro": raw.get("outro", ""),
        "scenes": lesson_scenes,
    }


def generate_from_prompt(
    channel: Channel,
    user_prompt: str,
    scenes: int = 8,
    long_form: bool = False,
) -> str:
    """Write and save a script from a user prompt for any configured channel."""
    tone = GENRE_TONE.get(channel.genre, "engaging and well-paced")
    if long_form:
        format_instructions = (
            f"Write a YouTube long-form script targeting about 5 minutes of narration. "
            f"Use exactly {scenes} scenes. Each scene line must contain 38-43 words "
            f"(about 760-860 narrated words total). Make the opening immediately engaging, "
            f"keep a clear story or teaching arc, and end with a satisfying takeaway."
        )
    else:
        format_instructions = (
            f"Write a short video script. Use exactly {scenes} scenes. "
            "Each scene line must be one narrated sentence."
        )
    prompt = (
        f"You are a scriptwriter for a {channel.genre} YouTube channel. Tone: {tone}.\n"
        f"{format_instructions}\nWrite about this idea from the user:\n"
        f"\"{user_prompt}\"\n"
        f"Return ONLY JSON: {{\"title\": str, \"intro\": str, \"outro\": str, "
        f"\"scenes\": [{{\"label\": str, \"line\": str, \"image_prompt\": str}}]}}.\n"
        f"'label' = a 2-4 word on-screen caption. 'image_prompt' = a visual description "
        f"of that scene (scene only, no art-style words)."
    )
    raw = None
    for attempt in range(3):
        attempt_prompt = prompt
        if attempt:
            attempt_prompt += (
                "\nIMPORTANT: The previous draft was too short or had the wrong scene count. "
                f"Return exactly {scenes} scenes and at least 720 narrated words across scene lines."
            )
        candidate = _script_json(attempt_prompt)
        if not long_form:
            raw = candidate
            break
        scene_rows = candidate.get("scenes", [])
        word_count = sum(len(str(scene.get("line", "")).split()) for scene in scene_rows)
        # Accept a scene-count range instead of an exact match: the 5-minute
        # target is really about narrated word count, and Gemini rarely lands
        # on the exact number. A script with 16-24 solid scenes is fine.
        scene_min = max(4, scenes - 4)
        scene_max = scenes + 4
        if scene_min <= len(scene_rows) <= scene_max and word_count >= 720:
            raw = candidate
            break
    if raw is None:
        raise RuntimeError(
            f"Long script did not reach the 5-minute target after 3 attempts "
            f"(needs {scene_min}-{scene_max} scenes and at least 720 narrated words)."
        )
    key, lesson = _build_lesson(channel, raw, fallback_title=user_prompt[:60])
    if long_form:
        key = f"long_{key}"

    path = channel.custom_lessons_path
    lessons = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if key in lessons:
        key = f"{key}_{len(lessons) + 1}"
    lessons[key] = lesson
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lessons, indent=2, ensure_ascii=False), encoding="utf-8")
    return key

def load_custom_lessons(channel: Channel) -> dict:
    path = channel.custom_lessons_path
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


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
