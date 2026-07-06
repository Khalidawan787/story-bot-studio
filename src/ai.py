from __future__ import annotations

import json

from .config import settings
from .models import Lesson, Scene


PROMPT = """
Create one short educational YouTube Shorts lesson for preschool children.
Category: {category}
Return only JSON with:
title, category, intro, outro, scenes.
Each scene must include label, line, and image.
Use 3 to 5 scenes. Keep lines cheerful, simple, factual, and safe for children.
Set image to assets/generated/<lowercase-label>.jpg.
"""


def generate_ai_lesson(category: str) -> Lesson:
    if settings.ai_provider == "openai":
        return _openai_lesson(category)
    if settings.ai_provider == "gemini":
        return _gemini_lesson(category)
    raise ValueError("Set AI_PROVIDER to openai or gemini in .env before using ai-make.")


def _parse_lesson(category: str, content: str) -> Lesson:
    raw = json.loads(content)
    topic_key = raw["title"].lower().replace(" ", "_")
    return Lesson(
        topic_key=topic_key,
        title=raw["title"],
        category=raw.get("category", category.title()),
        intro=raw.get("intro", ""),
        outro=raw.get("outro", ""),
        scenes=[Scene(**scene) for scene in raw["scenes"]],
    )


def _openai_lesson(category: str) -> Lesson:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model=settings.openai_model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "You write safe, simple preschool learning scripts."},
            {"role": "user", "content": PROMPT.format(category=category)},
        ],
    )
    return _parse_lesson(category, response.choices[0].message.content or "{}")


def _gemini_lesson(category: str) -> Lesson:
    import google.generativeai as genai

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)
    response = model.generate_content(PROMPT.format(category=category))
    content = response.text.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    return _parse_lesson(category, content)

