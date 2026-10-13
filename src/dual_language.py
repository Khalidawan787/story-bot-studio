"""A second-language copy of a finished video, for Facebook.

A channel can publish in English on YouTube and in Urdu on its Facebook Page
(or the other way round). After the YouTube video is made, its script is
translated, voiced again, and re-rendered over the very same pictures and
stock clips, so the Facebook copy costs one more render but no new images.
"""
from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

from .language import LANGUAGES, PURE_URDU, channel_language, display_marks, voice_for
from .models import Lesson, Scene


def facebook_language(channel) -> str | None:
    """The language the Facebook copy needs, or None when it is the same video."""
    from .social_accounts import account, facebook_posts_here

    cfg = account(channel.id, "facebook")
    if not (cfg.get("auto_upload") and cfg.get("page_id") and cfg.get("page_access_token")):
        return None
    if not facebook_posts_here():
        return None
    wanted = str(cfg.get("language") or "same").lower()
    if wanted not in LANGUAGES or wanted == channel_language(channel):
        return None
    return wanted


def translate_lesson(lesson: Lesson, target: str, strict: bool = False) -> Lesson:
    """The same scenes, narrated in `target` ("ur" = Roman Urdu + Urdu script).

    `strict` is for fact-checked scripts: a translation that drops, adds or
    changes a number is refused, and the caller posts the original instead.
    """
    import json

    from .genre_topics import _script_json

    source = {
        "title": lesson.title,
        "scenes": [{"line": scene.line} for scene in lesson.scenes],
    }
    if target == "ur":
        rules = (
            "Translate this YouTube video script into natural, everyday URDU for a Pakistani audience.\n"
            "For every scene return 'line' in ROMAN URDU (Urdu written in English letters, the way "
            "people type on WhatsApp) and 'speech' = the same sentence in Urdu script (اردو) for the voice.\n"
            "Translate 'title' into Roman Urdu, and give 'title_speech' = the same title in Urdu script.\n"
            + PURE_URDU
        )
        shape = '{"title": str, "title_speech": str, "scenes": [{"line": str, "speech": str}]}'
    else:
        rules = "Translate this video script into natural, clear ENGLISH. Keep the meaning and tone.\n"
        shape = '{"title": str, "scenes": [{"line": str}]}'
    prompt = (
        rules
        + f"Keep exactly {len(lesson.scenes)} scenes in the same order; translate, do not summarise.\n"
        + ("Every fact must stay exactly the same: write each number as the same digits, keep each "
           "name unchanged, and add nothing.\n" if strict else "")
        + f"Return ONLY JSON: {shape}\n\n"
        + json.dumps(source, ensure_ascii=False)
    )
    raw = _script_json(prompt)
    rows = raw.get("scenes") or []
    if len(rows) != len(lesson.scenes):
        raise RuntimeError(f"translation returned {len(rows)} scenes for {len(lesson.scenes)}")
    scenes = []
    for scene, row in zip(lesson.scenes, rows):
        line = " ".join(str(row.get("line") or "").split())
        if not line:
            raise RuntimeError("translation left a scene empty")
        speech = " ".join(str(row.get("speech") or "").split()) if target == "ur" else ""
        if strict:
            from .verified_facts import numbers_in

            for text in filter(None, (line, speech)):
                if numbers_in(text) != numbers_in(scene.line):
                    raise RuntimeError(f"translation changed the numbers in: {scene.line[:80]}")
        scenes.append(dataclasses.replace(scene, line=line, speech=speech))
    return Lesson(
        topic_key=lesson.topic_key, title=str(raw.get("title") or lesson.title).strip(),
        category=lesson.category, intro="", outro="", scenes=scenes,
        title_speech=" ".join(str(raw.get("title_speech") or "").split()) if target == "ur" else "",
    )


def build_copy(lesson: Lesson, channel, run_dir: Path, content_type: str, target: str) -> tuple[Path, str]:
    """Render the `target`-language copy next to the original. Returns (video, title)."""
    from .music import add_background_music
    from .seo import build_metadata
    from .tts import generate_voice_with_marks
    from .video import render_video, validate_rendered_video

    from .verified_facts import is_verified_channel

    translated = translate_lesson(lesson, target, strict=is_verified_channel(channel))
    copy_channel = dataclasses.replace(channel, language=target)
    voice = voice_for(copy_channel)
    out_dir = run_dir / f"facebook_{target}"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    audios, marks = [], []
    for index, scene in enumerate(translated.scenes, start=1):
        path, scene_marks = generate_voice_with_marks(
            scene.speech or scene.line, out_dir / f"voice_scene_{index:02}.mp3", voice=voice,
        )
        if scene.speech and scene.speech != scene.line:
            scene_marks = display_marks(scene_marks, scene.line)
        audios.append(path)
        marks.append(scene_marks)
    video = render_video(
        translated, audios[0], out_dir / "video.mp4", out_dir, scene_audio_paths=audios,
        channel=copy_channel, scene_marks=marks, content_type=content_type, reuse_from=run_dir,
    )
    video = add_background_music(
        video, out_dir, seed_text=translated.topic_key, channel=channel, lesson=lesson,
    )
    validate_rendered_video(video, content_type=content_type)
    # An Urdu post gets its title in Urdu script, without the English channel suffix.
    title = translated.title_speech or str(
        build_metadata(translated, copy_channel, content_type=content_type)["title"]
    )
    return video, title


def reused_image(scene: Scene, reuse_from: Path) -> Path | None:
    """The picture the original render used for this scene, if it is still there."""
    stem = Path(scene.image).stem
    images = [
        p for pattern in (f"{stem}_count_*", f"{stem}_fresh_*")
        for p in sorted((reuse_from / "gen").glob(pattern))
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]
    return images[0] if images else None
