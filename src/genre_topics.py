"""Generate genre story topics (crime/love/horror/motivation) with free Gemini.

Produces lessons in the SAME shape the render pipeline already understands
(title, category, intro, outro, scenes[label,line,image,image_prompt]) and
stores them per channel in data/<genre>_lessons.json. Kids topics still come
from the built-in topic bank — this only feeds the new genre channels.
"""
from __future__ import annotations

import re
import json
import sqlite3
import time
import urllib.request
import urllib.error

from .language import is_urdu, script_rules
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
    """Gemini with the first key; when its daily quota is full (429), the second key."""
    keys = [key for key in (settings.gemini_api_key, settings.gemini_api_key_2) if key]
    if not keys:
        raise RuntimeError("GEMINI_API_KEY not set in .env")
    for index, key in enumerate(keys):
        try:
            # The second key is a new account: it gets its own, newer model.
            return _gemini_with_key(prompt, model if index == 0 else settings.gemini_model_2, key)
        except urllib.error.HTTPError as e:
            if e.code == 429 and index + 1 < len(keys):
                print("[gemini] first key's quota is full; using the second key")
                continue
            raise
    raise RuntimeError("Gemini failed")


def _gemini_with_key(prompt: str, model: str | None, key: str) -> dict:
    model = model or settings.gemini_model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 1.0},
    }).encode("utf-8")
    # The key in a header works for both key formats (AIza... and the newer AQ....).
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "x-goog-api-key": key})
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
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and "choices" in payload:
                # A chat reply. A reasoning model can run out of room and send
                # only its reasoning with no content; the wrapper itself must
                # never be taken for a script.
                text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
                if not str(text or "").strip():
                    raise RuntimeError("Pollinations sent an empty reply")
            else:
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
        # Each free Gemini model has its own daily quota, so the next one in
        # the list often still answers when the first is used up.
        for fallback_model in [m.strip() for m in settings.gemini_fallback_model.split(",") if m.strip()]:
            if fallback_model == settings.gemini_model:
                continue
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


# Most viewers left Shorts at 0:02 (YouTube Studio, 21 Oct 2026): the first
# line set the scene ("At precisely 2:14 AM, a lone figure slipped into a
# cafe") instead of giving a reason to stay.
HOOK_RULE = (
    "The FIRST scene is the hook, heard in the first two seconds: under 12 words, the most "
    "gripping, surprising or emotional moment of this story, or a question it answers, said "
    "straight away. No scene-setting, no greeting, no 'in this video'. Then tell the story."
)


# The owner's story shape for every channel (2026-11-03): a hook, then the
# question the video answers, then the full details, then the answer.
STORY_ARC = (
    "STORY SHAPE, in this order: (1) HOOK: the first scene, as above. (2) QUESTION: the next "
    "scene asks the one question this video answers (why, how, what really happened). "
    "(3) DETAILS: the middle scenes tell the whole story step by step with concrete details, "
    "nothing skipped. (4) ANSWER: the last scenes answer that question clearly, then close. "
    "Write the WHOLE script: every scene complete, every sentence finished, never a half script."
)


# A Short runs 60 to 85 seconds: 12 scenes of one 12-20 word sentence each
# (about 190 words). Facebook takes Reels only up to 90 seconds.
SHORT_SCENES = 12
SHORT_LINE_WORDS = "12-20"


# A script whose title retells one the channel already has is written again this
# many times before the run gives up and makes no video. Before this check the
# long-video path never looked at earlier titles: one crime story came back eleven
# times under four names, and the kids channel kept writing the "date seed" story.
_FRESH_TRIES = 3
# Two titles are the same story when this share of the shorter one's words is
# shared. Series titles ("Barnaby Bear's Bright Blue Sky" / "Barnaby Bear's Bright
# Brown Boots") share 3 of 5 words and stay allowed; renamings of one story share
# nearly all of them.
_TITLE_REPEAT_SHARE = 0.75
_TITLE_FILLER = frozenset({
    "the", "and", "for", "with", "from", "how", "why", "what", "who", "this", "that",
    "its", "your", "you", "his", "her", "their", "our", "are", "was", "were",
    # words that most titles of a genre share without being the same story
    "true", "crime", "story", "stories", "mystery", "mysteries", "tale", "tales",
    "case", "video", "explained", "trending", "world", "news", "kids", "learning",
    "fun", "learn", "adventure",
})


def _title_head(title: str) -> str:
    """The title without the " | True Crime Story" style suffix added at upload."""
    return re.split(r"\s+[|–—-]\s+", str(title or ""), maxsplit=1)[0]


def _title_words(title: str) -> frozenset[str]:
    words: set[str] = set()
    for word in re.findall(r"[a-z0-9]+", _title_head(title).lower()):
        if len(word) <= 2 or word in _TITLE_FILLER:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.add(word)
    return frozenset(words)


def _recent_titles(channel: Channel) -> list[str]:
    """Every title this channel has written or published, newest videos first."""
    titles: list[str] = []
    try:
        rows = {} if channel.builtin else dict(load_genre_lessons(channel))
        rows.update(load_custom_lessons(channel))
        titles += [str(raw.get("title") or "") for raw in rows.values()]
    except (OSError, ValueError) as exc:
        print(f"[genre_topics] {channel.id}: could not read the saved scripts for the repeat check ({exc})")
    try:
        if settings.db_path.exists():
            conn = sqlite3.connect(settings.db_path)
            try:
                titles += [str(title or "") for (title,) in conn.execute(
                    "SELECT title FROM videos WHERE channel = ? AND title IS NOT NULL "
                    "ORDER BY id DESC LIMIT 400", (channel.id,),
                )]
            finally:
                conn.close()
    except sqlite3.Error as exc:
        print(f"[genre_topics] {channel.id}: could not read the video history for the repeat check ({exc})")
    return [title for title in titles if title.strip()]


def title_clash(channel: Channel, title: str, extra_titles=()) -> str | None:
    """The existing title that `title` retells, or None when it is a new story.

    `extra_titles` are titles not saved yet (the other scripts of the same run).
    Titles with fewer than three telling words are only compared as a whole: a
    one-word title would otherwise match every story that uses the word.
    """
    plain = " ".join(re.findall(r"[a-z0-9]+", _title_head(title).lower()))
    if not plain:
        return None
    words = _title_words(title)
    for other in [*extra_titles, *_recent_titles(channel)]:
        if plain == " ".join(re.findall(r"[a-z0-9]+", _title_head(other).lower())):
            return str(other)
        other_words = _title_words(other)
        if len(words) < 3 or len(other_words) < 3:
            continue
        if len(words & other_words) / min(len(words), len(other_words)) >= _TITLE_REPEAT_SHARE:
            return str(other)
    return None


def already_exist_note(titles: list[str]) -> str:
    """Prompt text that names the stories a rewrite must stay away from."""
    if not titles:
        return ""
    named = "; ".join(f'"{str(title)[:80]}"' for title in titles[-5:])
    return (
        "\nIMPORTANT: these stories already exist, so write a completely different story "
        f"with a new title, new characters and a new plot: {named}."
    )


def _generate_one(channel: Channel, avoid_titles: list[str], scenes: int = SHORT_SCENES) -> dict:
    from .verified_facts import is_verified_channel, write_next_lesson

    if is_verified_channel(channel):
        # A fact-only channel never gets a free-written story.
        key, lesson = write_next_lesson(channel, scenes, long_form=False)
        return {"_key": key, **lesson}
    tone = getattr(channel, "tone", "") or GENRE_TONE.get(channel.genre, "engaging and well-paced")
    avoid = "; ".join(avoid_titles[-40:]) if avoid_titles else "none yet"
    schema = (
        f"Return ONLY JSON: {{\"title\": str, \"intro\": str, \"outro\": str, "
        f"\"scenes\": [{{\"label\": str, \"line\": str, {_speech_field(channel)}\"image_prompt\": str}}]}}.\n"
        f"Use exactly {scenes} scenes. 'line' = one narrated sentence of {SHORT_LINE_WORDS} words "
        f"(the Short must run 60 to 85 seconds). {HOOK_RULE} {STORY_ARC} "
        f"'label' = a 2-4 word on-screen caption. 'image_prompt' = a visual "
        f"description of that scene (scene only, no art-style words)."
        + script_rules(channel)
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
    # A competitor's topic that is working right now (competitors.py): the
    # subject only; the script, title and pictures stay our own.
    inspiration = None
    if not channel.builtin:
        from . import competitors

        try:
            inspiration = competitors.next_idea(channel, "short")
        except Exception as exc:
            print(f"[competitors] {channel.id}: no idea ({exc})")
        if inspiration:
            prompt += competitors.inspiration_brief(inspiration)
            print(f"[competitors] {channel.id}: subject from '{inspiration['title'][:60]}' "
                  f"({inspiration['ratio']:.1f}x)")
    raw: dict = {}
    clash: str | None = None
    clashes: list[str] = []
    insights = _insights_note(channel)
    problem: str | None = None
    own_title_note = ""
    for _try in range(_FRESH_TRIES + 1):
        note = (f"\nThe previous draft was rejected because it was not whole ({problem}). "
                f"Write all {scenes} scenes in full.") if problem else ""
        raw = _script_json(prompt + insights + already_exist_note(clashes) + own_title_note + note)
        clash = title_clash(channel, str(raw.get("title") or ""), avoid_titles)
        if not clash and inspiration:
            from .competitors import too_similar

            if too_similar(str(raw.get("title") or ""), inspiration["title"]):
                # Their title, reworded: same subject, but a title in our own words.
                print(f"[competitors] {channel.id}: '{str(raw.get('title'))[:60]}' is too close to "
                      f"the other channel's title; writing it again")
                own_title_note = (
                    f"\nIMPORTANT: your last title \"{str(raw.get('title'))[:80]}\" was too close to the "
                    "other channel's title. Keep the subject, but write a completely different title "
                    "in your own words."
                )
                problem = None
                continue
        if clash:
            print(f"[genre_topics] {channel.id}: '{str(raw.get('title'))[:60]}' repeats "
                  f"'{str(clash)[:60]}'; writing it again")
            clashes.append(clash)
            problem = None
            continue
        problem = script_problem(raw, scenes, long_form=False)
        if not problem:
            break
        print(f"[genre_topics] {channel.id}: draft not whole ({problem}); writing it again")
    else:
        raise RuntimeError(
            f"No whole, fresh script came back (last: {problem or 'a repeat of ' + str(clash)[:60]}); "
            "no half video is made"
        )

    title = raw.get("title", "Untitled").strip()
    key = _slug(title)
    if inspiration:
        from .competitors import mark_written

        mark_written(channel.id, inspiration["video_id"], title)
    lesson_scenes = []
    for i, sc in enumerate(raw.get("scenes", []), start=1):
        lesson_scenes.append({
            "label": sc.get("label", f"Part {i}"),
            "line": sc.get("line", ""),
            "speech": _speech_of(channel, sc),
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


def _insights_note(channel: Channel) -> str:
    """Best-viewed titles and real search phrases (youtube_insights.py) for a story channel.

    Not for news: a news title must say what happened, not what people search.
    """
    if str(channel.genre).lower() == "trending":
        return ""
    try:
        from .youtube_insights import prompt_note

        return prompt_note(channel)
    except Exception as exc:
        print(f"[insights] skipped ({exc})")
        return ""


def _speech_field(channel: Channel) -> str:
    """The extra JSON field Urdu scripts carry (see language.py)."""
    return '\"speech\": str, ' if is_urdu(channel) else ""


def _speech_of(channel: Channel, scene: dict) -> str:
    """Urdu-script text for the voice; empty for English channels."""
    if not is_urdu(channel):
        return ""
    speech = " ".join(str(scene.get("speech") or "").split())
    # Without Urdu script the Urdu voice would spell out the Roman letters;
    # reading the Roman line is still the better fallback than silence.
    return speech or " ".join(str(scene.get("line") or "").split())


def _build_lesson(channel: Channel, raw: dict, fallback_title: str) -> tuple[str, dict]:
    title = (raw.get("title") or fallback_title).strip()
    key = _slug(title)
    lesson_scenes = []
    for i, sc in enumerate(raw.get("scenes", []), start=1):
        lesson_scenes.append({
            "label": sc.get("label", f"Part {i}"),
            "line": sc.get("line", ""),
            "speech": _speech_of(channel, sc),
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


def script_problem(raw: dict, scenes: int, long_form: bool) -> str | None:
    """Why a draft is not a whole script, or None when it is.

    The owner's rule (2026-11-03): an AI script is never half. A draft with
    missing scenes, a sentence cut off, thin lines or no ending is written again,
    and if it never comes back whole no video is made. Short drafts used to be
    padded with stock sentences ("This scene also reinforces the central idea
    of ...") and the narrator read those out.
    """
    rows = [row for row in (raw or {}).get("scenes") or [] if str(row.get("line") or "").strip()]
    low = scenes - (2 if long_form else 1)
    if len(rows) < low:
        return f"only {len(rows)} of {scenes} scenes"
    # 16 words is still a full narrated sentence; on 8-10 Nov drafts were thrown
    # away for one 17-21 word scene. Length overall is checked just below.
    min_words = 16 if long_form else 6
    for number, row in enumerate(rows, start=1):
        line = " ".join(str(row.get("line")).split())
        if not re.search(r"[.!?۔][\"'”’)]*$", line):
            return f"scene {number} stops mid-sentence"
        if len(line.split()) < min_words:
            return f"scene {number} has only {len(line.split())} words"
    if long_form:
        words = sum(len(str(row.get("line")).split()) for row in rows)
        if words < int(scenes * 38 * 0.85):
            return f"{words} words, far short of about {scenes * 38}"
    return None


def _is_length_problem(problem: str | None) -> bool:
    return bool(problem) and ("words" in problem) and "scenes" not in problem.split("(")[0]


def lengthen_script(draft: dict, scenes: int, long_form: bool) -> dict | None:
    """The same script with its thin lines written out in full, or None.

    A draft whose only fault is length (a scene of 17-21 words, or 70-80% of the
    words wanted) used to be thrown away, and after three tries no video was
    made (8-10 Nov: kids, Ain, SnuggleMoon, motivation, trending). The model now
    rewrites those lines itself, keeping every scene, its order and its facts,
    with no new names, numbers or quotes. Never stock filler (the owner's rule).
    The result must pass script_problem, or None is returned.
    """
    rows = [row for row in (draft or {}).get("scenes") or [] if str(row.get("line") or "").strip()]
    if not rows or any(row.get("speech") for row in rows):
        # Urdu scripts carry a separate spoken copy that a rewrite would not match.
        return None
    target = "35-45" if long_form else "18-26"
    numbered = "\n".join(f"{i}. {' '.join(str(r.get('line')).split())}" for i, r in enumerate(rows, start=1))
    try:
        answer = _script_json(
            "Below are the narrated lines of a video script, numbered. Rewrite them so that EVERY line "
            f"is {target} words: two or three full spoken sentences. Keep each line's meaning, its "
            "place in the story and every fact; add depth by explaining, describing and connecting "
            "what is already there. Do NOT add any new names, numbers, dates, places, quotes or claims, "
            "do not greet or repeat earlier lines, and never use filler like 'this scene shows'. "
            f"Return exactly {len(rows)} lines in the same order.\n{numbered}\n"
            'Return ONLY JSON: {"lines": [str]}'
        )
    except Exception as exc:
        print(f"[script] lengthening failed ({exc})")
        return None
    lines = [" ".join(str(x).split()) for x in answer.get("lines") or []]
    if len(lines) != len(rows):
        print(f"[script] lengthening returned {len(lines)} of {len(rows)} lines")
        return None
    def numbers(text: str) -> set:
        return set(re.findall(r"\d[\d,.]*", text))

    fixed = dict(draft)
    fixed["scenes"] = []
    for row, line in zip(rows, lines):
        old = str(row.get("line"))
        # A rewrite that brings in a number the line did not have is not trusted.
        if numbers(line) - numbers(old) or len(line.split()) < len(old.split()):
            line = old
        new_row = dict(row)
        new_row["line"] = line
        fixed["scenes"].append(new_row)
    problem = script_problem(fixed, scenes, long_form)
    if problem:
        print(f"[script] still not whole after lengthening ({problem})")
        return None
    print("[script] thin lines written out in full; script is whole")
    return fixed


# A free model asked for 55 scenes in one answer wrote 24 (a 6-minute "15-minute"
# video), so a script longer than this is written in parts that continue
# each other.
_PART_SCENES = 24


def _long_script_in_parts(prompt: str, scenes: int) -> dict:
    """One continuous long script from several answers of about 18 scenes each.

    `prompt` is the full single-answer prompt for `scenes` scenes; each part
    keeps its rules and only changes the scene count and its place in the
    story. Returns what was written (possibly fewer scenes if a part failed).
    """
    parts = -(-scenes // 19)
    sizes = [scenes // parts + (1 if i < scenes % parts else 0) for i in range(parts)]
    merged: dict = {"scenes": []}
    for index, size in enumerate(sizes, start=1):
        part_prompt = re.sub(r"Use exactly \d+ scenes", f"Use exactly {size} scenes", prompt)
        part_prompt = re.sub(r"\(about \d+-\d+ narrated words total\)",
                             f"(about {size * 38}-{size * 43} narrated words in this part)", part_prompt)
        part_prompt += f"\n\nThis is PART {index} of {parts} of one continuous video script."
        if index == 1:
            part_prompt += " Write only the opening part: the hook and the background. Do not conclude yet."
        else:
            done = "\n".join(f"- {row.get('label')}: {row.get('line')}" for row in merged["scenes"][-6:])
            covered = ", ".join(str(row.get("label")) for row in merged["scenes"])
            part_prompt += (
                " Continue straight on from the scenes already written; do not greet, re-introduce "
                f"the topic or repeat a point. Already covered: {covered}.\nThe last scenes so far:\n{done}\n"
                + ("This is the LAST part: end with the conclusion and the takeaway; 'outro' closes the whole video."
                   if index == parts else "Do not conclude yet.")
            )
        rows: list = []
        answer: dict = {}
        for attempt in range(3):
            try:
                answer = _script_json(part_prompt)
            except Exception as exc:
                print(f"[script] part {index}/{parts} try {attempt + 1} failed: {exc}")
                continue
            problem = script_problem(answer, size, long_form=True)
            if _is_length_problem(problem):
                longer = lengthen_script(answer, size, long_form=True)
                if longer is not None:
                    answer, problem = longer, None
            rows = [row for row in answer.get("scenes") or [] if str(row.get("line") or "").strip()]
            if not problem:
                break
            print(f"[script] part {index}/{parts} try {attempt + 1} not whole ({problem})")
            part_prompt += f"\nThe previous answer was not whole ({problem}): write all {size} scenes in full."
            rows = []
        if not rows:
            # A story without its middle or its ending is never published.
            raise RuntimeError(f"Part {index} of {parts} of the script never came back whole; "
                               "no half video is made")
        if index == 1:
            merged.update({k: answer.get(k) for k in ("title", "intro") if answer.get(k)})
        merged["outro"] = answer.get("outro") or merged.get("outro")
        merged["scenes"].extend(rows)
    print(f"[script] long script in {parts} parts: {len(merged['scenes'])}/{scenes} scenes")
    return merged


def generate_from_prompt(
    channel: Channel,
    user_prompt: str,
    scenes: int = 8,
    long_form: bool = False,
) -> str:
    """Write and save a script from a user prompt for any configured channel.

    A script whose title retells one the channel already has is written again,
    up to _FRESH_TRIES times, with the clashing titles named in the prompt. If it
    is still a repeat nothing is saved and no video is made.
    """
    from .verified_facts import is_verified_channel, write_next_lesson

    if is_verified_channel(channel):
        key, lesson = write_next_lesson(channel, scenes, long_form, user_prompt=user_prompt)
        return _save_custom_lesson(channel, f"long_{key}" if long_form else key, lesson)
    raw: dict = {}
    clash: str | None = None
    clashes: list[str] = []
    # A brief from competitors.inspiration_brief names the other channel's title;
    # ours must not repeat it.
    theirs = re.search(r'has a video titled "(.+?)" that is getting', user_prompt)
    for _try in range(_FRESH_TRIES):
        raw = _draft_script(channel, user_prompt, scenes, long_form, clashes)
        clash = title_clash(channel, str(raw.get("title") or ""))
        if not clash and theirs:
            from .competitors import too_similar

            if too_similar(str(raw.get("title") or ""), theirs.group(1)):
                print(f"[competitors] {channel.id}: '{str(raw.get('title'))[:60]}' is too close to "
                      "the other channel's title; writing it again")
                user_prompt += ("\nIMPORTANT: your last title was too close to the other channel's title. "
                                "Keep the subject, but write a completely different title in your own words.")
                clash = "competitor title"
                continue
        if not clash:
            break
        print(f"[genre_topics] {channel.id}: '{str(raw.get('title'))[:60]}' repeats "
              f"'{str(clash)[:60]}'; writing it again")
        clashes.append(clash)
    else:
        raise RuntimeError(
            f"Every draft repeated an existing story (last: '{str(clash)[:60]}'); no video made"
        )
    key, lesson = _build_lesson(channel, raw, fallback_title=user_prompt[:60])
    if long_form:
        key = f"long_{key}"
    return _save_custom_lesson(channel, key, lesson)


def _draft_script(channel: Channel, user_prompt: str, scenes: int, long_form: bool,
                  avoid: list[str]) -> dict:
    """One finished script draft as JSON; `avoid` lists titles it must not retell."""
    tone = getattr(channel, "tone", "") or GENRE_TONE.get(channel.genre, "engaging and well-paced")
    if long_form:
        format_instructions = (
            f"Write a YouTube long-form script targeting about {max(1, round(scenes * 40 / 160))} minutes of narration. "
            f"Use exactly {scenes} scenes. Each scene line must contain 38-43 words "
            f"(about {scenes * 38}-{scenes * 43} narrated words total). Open with a hook: the first sentence is the most gripping moment or question of the story, no scene-setting, "
            f"keep a clear story or teaching arc, and end with a satisfying takeaway. {STORY_ARC}"
        )
    else:
        format_instructions = (
            f"Write a short video script. Use exactly {scenes} scenes. "
            "Each scene line must be one narrated sentence. " + HOOK_RULE + " " + STORY_ARC
        )
    prompt = (
        f"You are a scriptwriter for a {channel.genre} YouTube channel. Tone: {tone}.\n"
        f"{format_instructions}\nWrite about this idea from the user:\n"
        f"\"{user_prompt}\"\n"
        f"Return ONLY JSON: {{\"title\": str, \"intro\": str, \"outro\": str, "
        f"\"scenes\": [{{\"label\": str, \"line\": str, \"image_prompt\": str}}]}}.\n"
        f"'label' = a 2-4 word on-screen caption. 'image_prompt' = a visual description "
        f"of that scene (scene only, no art-style words)."
        + script_rules(channel)
        + _insights_note(channel)
        + already_exist_note(avoid)
    )
    if is_urdu(channel):
        prompt = prompt.replace(
            '\"line\": str, \"image_prompt\"', '\"line\": str, \"speech\": str, \"image_prompt\"',
        )
    if long_form and scenes > _PART_SCENES:
        # Raises when a part never comes back whole: no half video.
        raw = _long_script_in_parts(prompt, scenes)
    else:
        raw = None
        problem = "no answer"
        # Three tries for a whole draft. Short drafts are no longer padded with
        # stock sentences (the owner's rule): a draft is whole, or no video is made.
        for attempt in range(3):
            attempt_prompt = prompt
            if attempt:
                attempt_prompt += (
                    f"\nIMPORTANT: the previous draft was not whole ({problem}). Return exactly "
                    f"{scenes} scenes, every line complete"
                    + (f", about {scenes * 40} narrated words in all." if long_form else ".")
                )
            try:
                candidate = _script_json(attempt_prompt)
            except Exception as exc:
                problem = f"provider error: {exc}"
                continue
            problem = script_problem(candidate, scenes, long_form)
            if _is_length_problem(problem):
                longer = lengthen_script(candidate, scenes, long_form)
                if longer is not None:
                    candidate, problem = longer, None
            if not problem:
                raw = candidate
                break
            print(f"[script] draft {attempt + 1} not whole ({problem}); writing it again")
        if raw is None:
            raise RuntimeError(f"The script never came back whole ({problem}); no half video is made.")
    if long_form and len(raw.get("scenes") or []) > scenes + 4:
        raw["scenes"] = raw["scenes"][:scenes + 4]
    if _looks_like_instruction_title(str(raw.get("title") or "")) or not str(raw.get("title") or "").strip():
        raw["title"] = _story_subject(user_prompt, channel.genre)
    return raw


def _save_custom_lesson(channel: Channel, key: str, lesson: dict) -> str:
    """Add a written script to the channel's custom bank; returns its final key."""
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


def generate_genre_topics(channel: Channel, count: int = 5, scenes: int = SHORT_SCENES) -> list[str]:
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
    path = channel.custom_lessons_path if channel.builtin else channel.lessons_path
    path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(count):
        try:
            one = _generate_one(channel, existing_titles + added_titles(lessons, added), scenes=scenes)
        except Exception as e:
            print(f"[genre_topics] {channel.id}: generation failed ({e})")
            break
        key = one.pop("_key")
        source = str(one.get("source_title") or "").lower()
        if source and any(str(v.get("source_title") or "").lower() == source for v in lessons.values()):
            print(f"[genre_topics] {channel.id}: {one.get('source_title')} is already in the bank; skipped")
            continue
        if key in lessons:
            key = f"{key}_{len(lessons) + 1}"
        lessons[key] = one
        existing_titles.append(one["title"])
        added.append(key)
        # Saved after every script: a fact-only channel picks its next person
        # from what the bank file holds, and saving once at the end gave both
        # of a day's Shorts the same person (two "Sojourner Truth" Shorts).
        path.write_text(json.dumps(lessons, indent=2, ensure_ascii=False), encoding="utf-8")

    path.write_text(json.dumps(lessons, indent=2, ensure_ascii=False), encoding="utf-8")
    return added


def added_titles(lessons: dict, keys: list[str]) -> list[str]:
    return [lessons[k]["title"] for k in keys if k in lessons]
