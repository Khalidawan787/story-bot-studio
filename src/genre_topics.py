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
    "trending": (
        "clear, factual world-news explainer — neutral and balanced, no political "
        "side, no invented quotes or numbers, no graphic violence"
    ),
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
    for attempt in range(1):
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
    for attempt in range(1):
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
    schema = (
        f"Return ONLY JSON: {{\"title\": str, \"intro\": str, \"outro\": str, "
        f"\"scenes\": [{{\"label\": str, \"line\": str, \"image_prompt\": str}}]}}.\n"
        f"Use exactly {scenes} scenes. 'line' = one narrated sentence. "
        f"'label' = a 2-4 word on-screen caption. 'image_prompt' = a visual "
        f"description of that scene (scene only, no art-style words)."
    )
    if channel.builtin:
        # Preschool needs a different brief from the story channels: a real
        # challenge with a pause before the answer, a little story, and no
        # brands or existing cartoon characters — using those on a kids channel
        # is a copyright strike waiting to happen.
        prompt = (
            "You write short videos for a preschool learning YouTube channel "
            "(ages 2-6). Tone: cheerful, warm, completely safe.\n"
            "Write ONE fresh episode teaching a single simple idea: a colour, a "
            "number, a shape, an animal, an opposite, a daily habit, a season, a "
            "feeling or a sound.\n"
            "Rules that matter:\n"
            "- Open with a CHALLENGE the child can act on within two seconds.\n"
            "- Do NOT answer your own question in the same sentence. Build a "
            "short moment of searching or counting FIRST, reveal the answer LATER.\n"
            "- Give it a tiny story or a funny moment, not a list of facts.\n"
            "- Never name a brand, a company, or any existing cartoon character. "
            "Invent your own friendly animal helper if you want one.\n"
            "- Short sentences, simple words, nothing frightening.\n"
            f"Do NOT reuse any of these titles: {avoid}.\n" + schema
        )
    else:
        prompt = (
            f"You are a scriptwriter for a {channel.genre} YouTube channel. Tone: {tone}.\n"
            f"Write ONE fresh {channel.genre} short video script about an original, "
            f"click-worthy topic. Do NOT reuse any of these titles: {avoid}.\n" + schema
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


def _story_subject(user_prompt: str, genre: str) -> str:
    """Turn an automation instruction into a publishable story title."""
    text = " ".join(str(user_prompt or "").split()).strip(' "')
    inspired = re.search(r"inspired\s+by\s*:\s*(.+?)(?:\.\s*Date seed\s*:|\.\s*Creator guideline\s*:|$)", text, re.I)
    if inspired:
        text = inspired.group(1).strip(" .:-")
    text = re.sub(r"^(?:create|write|make)\s+(?:a\s+)?(?:complete,?\s*)?(?:original\s+)?(?:\w+\s+)?(?:video|story|episode)\s*(?:about|on)?\s*:?\s*", "", text, flags=re.I)
    text = re.split(r"\b(?:Date seed|Creator guideline|intended for)\s*:", text, maxsplit=1, flags=re.I)[0]
    text = text.strip(" .:-") or f"A New {genre.title()} Story"
    return text[:82]


def _looks_like_instruction_title(title: str) -> bool:
    lowered = str(title or "").lower()
    return any(token in lowered for token in (
        "create a ", "write a ", "date seed", "creator guideline",
        "video inspired by", "intended for",
    ))


def _crime_long_fallback_rows(subject: str) -> list[dict]:
    """Coherent, visually concrete fallback when every script API is unavailable."""
    stages = [
        ("Manor at Dusk", "the isolated Victorian manor at dusk behind iron gates and misty trees"),
        ("Detective Arrives", "a professional detective arriving at the manor with a case folder under moody evening light"),
        ("Sealed Study", "a locked antique study with a heavy wooden door, brass keyhole, and police evidence marker"),
        ("Silent Clock", "an old grandfather clock stopped at a precise time inside a shadowy manor hallway"),
        ("Dusty Footprints", "fresh shoeprints crossing a dusty wooden floor toward the locked study"),
        ("Broken Latch", "close view of a damaged window latch examined with gloved hands and a flashlight"),
        ("Hidden Letter", "an aged handwritten letter discovered inside a secret drawer beside sealed evidence bags"),
        ("Witness Interview", "a detective calmly interviewing a worried manor employee in a dim sitting room"),
        ("Timeline Board", "an investigation board with photographs, clock times, thread, and handwritten evidence notes"),
        ("Missing Key", "an empty velvet key hook inside the manor office photographed as crime evidence"),
        ("Garden Path", "a narrow moonlit garden path with one set of footprints leading toward a stone greenhouse"),
        ("Glasshouse Clue", "a detective finding a dropped silver cufflink on the greenhouse floor under a flashlight"),
        ("Archive Records", "old property records and floor plans spread across a desk under a focused reading lamp"),
        ("Secret Passage", "a concealed passage behind a manor bookshelf revealed by the original architectural plan"),
        ("Second Entrance", "a narrow hidden doorway connecting the passage to the supposedly locked study"),
        ("Evidence Matches", "gloved investigators comparing the cufflink, key, letter, and footprint photographs"),
        ("Final Interview", "the detective presenting verified evidence to a tense suspect across an interrogation table"),
        ("Motive Revealed", "the hidden inheritance letter beside a family photograph and official estate documents"),
        ("Case Reconstructed", "a clear visual reconstruction of the manor route marked on an architectural floor plan"),
        ("Mystery Resolved", "the detective leaving the quiet manor at sunrise as police secure the final evidence"),
    ]
    rows = []
    for index, (label, visual) in enumerate(stages, start=1):
        rows.append({
            "label": label,
            "line": (
                f"In {subject}, part {index} follows the investigation through {label.lower()}, "
                "where a verified detail reshapes the timeline and helps separate physical evidence from rumor."
            ),
            "image_prompt": f"True-crime documentary scene showing {visual}, no text, no poster, no politics, no gore",
        })
    return rows

def _complete_long_script(raw: dict, channel: Channel, user_prompt: str, scenes: int) -> dict:
    """Complete short free-provider drafts locally instead of failing a video."""
    raw = dict(raw or {})
    rows = [dict(row) for row in raw.get("scenes", []) if str(row.get("line", "")).strip()]
    target = max(16, min(24, int(scenes or 20)))
    words_per_scene = (800 + target - 1) // target
    generated_title = str(raw.get("title") or "").strip()
    title = _story_subject(user_prompt, channel.genre) if not generated_title or _looks_like_instruction_title(generated_title) else generated_title
    topic = title
    crime_fallback = _crime_long_fallback_rows(title) if channel.genre == "crime" else []
    if not rows and crime_fallback:
        rows = [dict(row) for row in crime_fallback[:target]]
    elif not rows:
        rows = [{
            "label": "The Beginning",
            "line": f"Today we explore {topic}, beginning with the most important idea and the setting around it.",
            "image_prompt": f"A clear visual scene about {topic}",
        }]

    source = [dict(row) for row in rows]
    while len(rows) < target:
        if crime_fallback and len(rows) < len(crime_fallback):
            rows.append(dict(crime_fallback[len(rows)]))
            continue
        base = source[len(rows) % len(source)]
        number = len(rows) + 1
        rows.append({
            "label": f"Part {number}",
            "line": (
                f"As {topic} continues, this next moment builds on {base.get('line', '')} "
                "We look at what changes, why it matters, and how it connects with the main idea before moving forward."
            ),
            "image_prompt": base.get("image_prompt") or f"A distinct scene illustrating {topic}, part {number}",
        })
    rows = rows[:target]

    genre_tail = {
        "kids": "Look closely at the details, think about what they teach us, and notice how this example connects to something we may see in everyday life.",
        "crime": "This detail matters because it changes how the evidence, timeline, and witness accounts fit together, while reminding us to separate verified facts from assumptions.",
        "horror": "The atmosphere grows more unsettling as small details gain meaning, adding tension without revealing everything too soon and guiding us toward the next discovery.",
        "love": "This moment reveals more about the characters, their feelings, and the choices shaping their relationship, giving the story warmth and a natural emotional progression.",
        "motivation": "The lesson becomes practical when we connect it to one clear action, a realistic challenge, and a small step that can be repeated consistently over time.",
        "trending": "This part matters because it shows how the situation developed, who it affects, and what still remains unconfirmed, so viewers can follow the story without guesswork.",
    }.get(channel.genre, "This moment adds useful context, explains why the detail matters, and connects it clearly to the next part of the story.")

    for index, row in enumerate(rows, start=1):
        line = " ".join(str(row.get("line", "")).split())
        if len(line.split()) < words_per_scene:
            line = f"{line} {genre_tail}"
        if len(line.split()) < words_per_scene:
            line += f" In part {index}, keep the central subject, {topic}, in mind as the explanation continues step by step."
        words = line.split()
        if len(words) > words_per_scene:
            line = " ".join(words[:words_per_scene]).rstrip(",;:") + "."
        row["line"] = line
        row["label"] = str(row.get("label") or f"Part {index}")
        row["image_prompt"] = str(row.get("image_prompt") or f"A distinct visual scene about {topic}, part {index}")

    total = sum(len(str(row["line"]).split()) for row in rows)
    index = 0
    while total < 720:
        addition = f" This scene also reinforces the central idea of {topic} with a fresh, easy-to-follow detail for the viewer."
        rows[index % len(rows)]["line"] += addition
        total += len(addition.split())
        index += 1

    raw["title"] = title
    raw["intro"] = raw.get("intro") or f"Let us begin our complete look at {topic}."
    raw["outro"] = raw.get("outro") or f"That completes our journey through {topic}."
    raw["scenes"] = rows
    return raw


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
    best_candidate = None
    for attempt in range(1):
        attempt_prompt = prompt
        if attempt:
            attempt_prompt += (
                "\nIMPORTANT: The previous draft was too short or had the wrong scene count. "
                f"Return exactly {scenes} scenes and at least 720 narrated words across scene lines."
            )
        try:
            candidate = _script_json(attempt_prompt)
        except Exception:
            continue
        if best_candidate is None or len(candidate.get("scenes", [])) > len(best_candidate.get("scenes", [])):
            best_candidate = candidate
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
    if long_form:
        # Free providers often stop early. Complete the best draft locally so a
        # valid five-minute job is not discarded only because of word count.
        raw = _complete_long_script(raw or best_candidate or {}, channel, user_prompt, scenes)
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
    # The kids channel has a 1,200-topic bank, but those entries are bare words
    # that all render into the same four sentences. Fresh written topics go
    # alongside them in custom_kids_lessons.json and keep their own narration.
    if channel.builtin:
        lessons = load_custom_lessons(channel)
    else:
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

    path = channel.custom_lessons_path if channel.builtin else channel.lessons_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(lessons, indent=2, ensure_ascii=False), encoding="utf-8")
    return added


def added_titles(lessons: dict, keys: list[str]) -> list[str]:
    return [lessons[k]["title"] for k in keys if k in lessons]
