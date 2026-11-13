from __future__ import annotations

from datetime import datetime
import json

from pathlib import Path

from .config import settings
from .db import active_upload_backoff, apply_upload_error_backoff, approval_mode_enabled, save_video, set_drive_url
from .models import Lesson, RenderedAssets, Scene
from .music import add_background_music
from .quality import inspect_video
from .seo import build_metadata
from .subtitles import generate_subtitles
from .tts import generate_voice, generate_voice_with_marks, prosody_for
from .video import render_video, validate_rendered_video
from .visuals import create_thumbnail, resolve_scene_image


def _cleanup_run(run_dir: Path) -> None:
    """Delete big disposable render files; keep the final video, thumbnail, subtitles."""
    keep = {"video.mp4", "thumbnail.jpg", "subtitles.srt"}
    try:
        for f in run_dir.iterdir():
            if f.is_file() and f.name not in keep:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def _append_image_credits(metadata: dict, run_dir: Path) -> None:
    """Add required attribution for free CC images to the upload description."""
    credits: list[str] = []
    for path in sorted((run_dir / "gen").glob("*.openverse.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            title = str(row.get("title") or "Image").strip()
            creator = str(row.get("creator") or "Unknown creator").strip()
            license_name = str(row.get("license") or "CC").upper()
            source = str(row.get("source_page") or row.get("license_url") or "").strip()
            credit = f"- {title} — {creator} ({license_name})"
            if source:
                credit += f": {source}"
            if credit not in credits:
                credits.append(credit)
        except Exception:
            continue
    for path in sorted((run_dir / "gen").glob("*.wikimedia.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            title = str(row.get("title") or "Image").strip()
            creator = str(row.get("creator") or "Wikimedia contributor").strip()
            license_name = str(row.get("license") or "CC").strip()
            source = str(row.get("source_page") or row.get("license_url") or "").strip()
            credit = f"- {title} — {creator} ({license_name})"
            if source:
                credit += f": {source}"
            if credit not in credits:
                credits.append(credit)
        except Exception:
            continue
    # Pexels does not require attribution, but the photographer is credited anyway.
    for path in sorted(run_dir.glob("thumbnail_art.*.pexels.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            creator = str(row.get("creator") or "").strip()
            source = str(row.get("source_page") or "").strip()
            if creator:
                credit = f"- Thumbnail photo — {creator} (Pexels)"
                if source:
                    credit += f": {source}"
                if credit not in credits:
                    credits.append(credit)
        except Exception:
            continue
    for path in sorted(run_dir.glob("clip_scene_*.pexels.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            creator = str(row.get("creator") or "").strip()
            if creator:
                credit = f"- Stock footage — {creator} (Pexels)"
                if credit not in credits:
                    credits.append(credit)
        except Exception:
            continue
    if credits:
        base = str(metadata.get("description") or "")
        block = "\n\nImage credits:\n" + "\n".join(credits)
        metadata["description"] = (base + block)[:5000]


def _interactive_kids_lesson(lesson: Lesson) -> Lesson:
    """Turn passive Kids Shorts into a quick question-and-answer game."""
    import dataclasses

    scenes: list[Scene] = []
    for index, scene in enumerate(lesson.scenes):
        if index == 0:
            line = f"Quick! Can you find {scene.label}? Yes! {scene.line}"
        elif index % 2:
            line = f"What do you see? {scene.label}! {scene.line}"
        else:
            line = f"Say {scene.label} with me! {scene.line}"
        scenes.append(Scene(
            label=scene.label, line=line, image=scene.image,
            image_prompt=scene.image_prompt,
        ))
    # A bank lesson alone runs about 20 seconds. Quiz rounds over the same
    # pictures bring the Short to a minute, and repetition is how small
    # children learn the words.
    rounds = (
        lambda s: f"Do you remember this one? Think... Yes! It is {s.label}! {s.line}",
        lambda s: f"One more time, say it loud with me: {s.label}! Great job!",
    )
    first = list(scenes)
    for make in rounds:
        if sum(len(s.line.split()) for s in scenes) >= 150:
            break
        scenes += [Scene(label=s.label, line=make(s), image=s.image, image_prompt=s.image_prompt)
                   for s in lesson.scenes]
    if len(scenes) > len(first):
        scenes[len(first)] = dataclasses.replace(
            scenes[len(first)], line="Now let's play a memory game! " + scenes[len(first)].line,
        )
    return Lesson(
        topic_key=lesson.topic_key, title=lesson.title, category=lesson.category,
        intro=lesson.intro, outro=lesson.outro, scenes=scenes,
    )


def _needs_interactive_rewrite(lesson: Lesson, channel) -> bool:
    """True only for the built-in kids bank, whose scenes are bare words.

    Those entries carry a label ("Red") and a stub line, so the pipeline turns
    them into spoken question-and-answer. A written script — an AI-generated
    kids topic, for instance — already has real narration; rewriting it would
    flatten every video back into the same four sentences, which is exactly the
    sameness that makes a channel look mass-produced.
    """
    if channel is not None and not getattr(channel, "builtin", False):
        return False
    words = [len(str(scene.line or "").split()) for scene in lesson.scenes]
    return not words or max(words) <= 8


def style_matched(thumbnail_path, redraw, run_dir: Path, channel, tries: int = 2):
    """Compare the card with the channel's own examples; redraw it with a new picture if it is off.

    The best of up to three draws is kept and its check is saved beside it
    (style_match.py). A card built on the subject's own photo is only checked:
    the photo cannot be swapped for another.
    """
    if channel is None:
        return thumbnail_path
    try:
        from .style_match import compare, save_check
        from .thumbnail_check import check_thumbnail

        best_path, best = Path(thumbnail_path), compare(thumbnail_path, channel.id)
        if best is None:
            return thumbnail_path
        if not best["ok"] and not (run_dir / "article_photo.jpg").exists():
            for number in range(1, tries + 1):
                print(f"[style] score {best['score']} ({'; '.join(best['issues'])}); drawing it again")
                # A new picture each time: the stock search would return the same
                # photo, so the redraws use generated art (random seed).
                from .config import settings as _settings

                provider = _settings.thumbnail_provider
                object.__setattr__(_settings, "thumbnail_provider", ("cloudflare", "pollinations")[(number - 1) % 2])
                try:
                    alt = Path(redraw(f"thumbnail_style{number}.jpg"))
                finally:
                    object.__setattr__(_settings, "thumbnail_provider", provider)
                if not check_thumbnail(alt).ok:
                    continue
                result = compare(alt, channel.id)
                if result and result["score"] > best["score"]:
                    best_path, best = alt, result
                if best["ok"]:
                    break
        print(f"[style] {channel.id}: score {best['score']} against {best['examples']} example(s)")
        # The winner is always thumbnail.jpg: GitHub keeps only that name for a
        # later retry (github_artifacts.KEPT_FILES).
        final = run_dir / "thumbnail.jpg"
        if best_path.resolve() != final.resolve():
            final.write_bytes(best_path.read_bytes())
        save_check(final, best)
        return final
    except Exception as exc:
        print(f"[style] check skipped ({exc})")
        return thumbnail_path


def style_problem(thumbnail_path, channel) -> str:
    """Why this thumbnail is not approved against the channel's examples, or ""."""
    if channel is None:
        return ""
    try:
        from .style_match import compare, load_check, required

        if not required():
            return ""
        result = load_check(thumbnail_path) or compare(thumbnail_path, channel.id)
    except Exception as exc:
        print(f"[style] check skipped ({exc})")
        return ""
    if result is None or result["ok"]:
        return ""  # no examples for this channel, or it matches them
    return (f"thumbnail does not look like the channel's examples yet (style {result['score']}: "
            + "; ".join(result["issues"]) + ")")


def _real_subject_thumbnail(lesson, channel, run_dir: Path, content_type: str) -> Path | None:
    """The thumbnail of a true story about a real person: that person's own photo.

    AI or stock artwork under a real name would show a stranger as that
    person. Returns None (the ordinary thumbnail is made) for other channels,
    when you dropped in a thumbnail of your own, or when the article has no
    photo that is free to reuse.
    """
    try:
        from .page_posts import real_photo
        from .thumbnails import compose_thumbnail, manual_thumbnail_for
        from .verified_facts import _bank_rows, is_verified_channel

        if channel is None or not is_verified_channel(channel) or manual_thumbnail_for(lesson):
            return None
        subject = _bank_rows(channel).get(lesson.topic_key, {}).get("source_title")
        if not subject:
            return None
        found = real_photo(subject, run_dir)
        if not found:
            # No photo of them that may be reused: the name on a plain card,
            # rather than artwork of somebody else.
            print(f"[thumbnail] no free photo of {subject}; plain title card")
            return compose_thumbnail(lesson, None, run_dir / "thumbnail.jpg", channel, content_type)
        print(f"[thumbnail] real photo of {subject}. {found[1]}")
        return compose_thumbnail(lesson, found[0], run_dir / "thumbnail.jpg", channel, content_type)
    except Exception as exc:
        print(f"[thumbnail] real photo skipped: {exc}")
        return None


def _connected(channel) -> bool:
    """True when this channel/account already has a saved YouTube login."""
    try:
        return bool(channel is not None and channel.token_path.is_file())
    except Exception:
        return False


_END_CALL = {
    "en": "If you enjoyed this, hit like, share it with a friend, subscribe for more, "
          "and tell us in the comments what you think.",
    "sleep": "If this story helped you rest, like it, share it with someone who needs sleep, "
             "subscribe, and tell us in the comments which story you want next. Sleep well.",
}
_END_CALL_UR = (
    "Video achi lagi ho to like karein, share karein, channel subscribe karein, aur comment mein apni raye zaroor batayein.",
    "ویڈیو اچھی لگی ہو تو لائک کریں، شیئر کریں، چینل سبسکرائب کریں، اور کمنٹ میں اپنی رائے ضرور بتائیں۔",
)


def end_call_text(channel) -> tuple[str, str]:
    """(on-screen line, spoken text) of the like / share / subscribe / comment call."""
    from .language import is_urdu

    if is_urdu(channel):
        return _END_CALL_UR
    sleepy = "sleep" in f"{channel.id} {getattr(channel, 'genre', '')}".lower() or "snuggle" in channel.id
    call = _END_CALL["sleep" if sleepy else "en"]
    return call, call


def _end_call_voice(channel, run_dir: Path) -> Path | None:
    """The call spoken in the channel's voice, for a long video's end screen."""
    if channel is None or getattr(channel, "made_for_kids", False):
        return None
    try:
        from .language import voice_for

        prosody = prosody_for(channel)
        path = run_dir / "end_call.mp3"
        generate_voice(end_call_text(channel)[1], path, voice_for(channel),
                       prosody.get("rate"), prosody.get("pitch"))
        return path
    except Exception as exc:
        print(f"[video] end screen voice left out: {exc}")
        return None


def _with_end_call(lesson: Lesson, channel) -> Lesson:
    """The narrator closes by asking for a like, a subscribe and a comment.

    Not on made-for-kids channels (comments are off there).
    """
    import dataclasses

    from .language import is_urdu

    if channel is None or getattr(channel, "made_for_kids", False) or not lesson.scenes:
        return lesson
    last = lesson.scenes[-1]
    if "subscribe" in f"{last.line} {last.speech}".lower():
        return lesson
    if is_urdu(channel):
        line, speech = f"{last.line} {_END_CALL_UR[0]}", f"{last.speech or last.line} {_END_CALL_UR[1]}"
    else:
        sleepy = "sleep" in f"{channel.id} {getattr(channel, 'genre', '')}".lower() or "snuggle" in channel.id
        call = _END_CALL["sleep" if sleepy else "en"]
        line, speech = f"{last.line} {call}", (f"{last.speech} {call}" if last.speech else "")
    scenes = [*lesson.scenes[:-1], dataclasses.replace(last, line=line, speech=speech)]
    return dataclasses.replace(lesson, scenes=scenes)


def run_pipeline(
    lesson: Lesson, upload: bool = False, channel=None, content_type: str = "short",
    queue: bool | None = None, resume_run_dir: Path | None = None,
    is_daily: bool = False,
) -> RenderedAssets:
    # queue: None = use the channel's auto-upload-queue setting (default for the
    # kids/other flows). True = force the timed drip queue. False = upload
    # immediately now (skip the queue). Only matters when upload is True.
    # channel=None keeps the original kids behavior (voice/seo/upload unchanged).
    from .language import display_marks, voice_for

    voice = voice_for(channel) if channel is not None else None
    prosody = prosody_for(channel)
    channel_id = channel.id if channel is not None else "kids"
    # Which of the channel's connected YouTube accounts this video is for. The
    # caller passes an account-specific channel object, so the upload below
    # already uses that account's token.
    account_id = str(getattr(channel, "account", "main") or "main")

    if content_type not in {"short", "long"}:
        raise ValueError(f"Unsupported content type: {content_type}")
    if content_type == "short" and _needs_interactive_rewrite(lesson, channel):
        lesson = _interactive_kids_lesson(lesson)
    if content_type != "long":
        # A long video says it over its end screen instead (add_end_screen).
        lesson = _with_end_call(lesson, channel)
    run_dir = Path(resume_run_dir).resolve() if resume_run_dir else settings.runs_dir / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job_id = run_dir.name
    run_dir.mkdir(parents=True, exist_ok=True)
    if channel is not None:
        from .verified_facts import is_verified_channel, source_url

        if is_verified_channel(channel):
            from urllib.parse import unquote

            from .stock_video import write_subject

            article = source_url(channel, lesson.topic_key)
            write_subject(run_dir, unquote(article.rsplit("/", 1)[-1]).replace("_", " ") if article else "")

    scene_audio_paths = []
    scene_marks: list = []
    for index, scene in enumerate(lesson.scenes, start=1):
        out = run_dir / f"voice_scene_{index:02}.mp3"
        completed_clip = run_dir / f"scene_{index:02}.mp4"
        if out.exists() and completed_clip.exists() and completed_clip.stat().st_size > 100_000:
            path, marks = out, None
        elif settings.enable_karaoke_captions:
            path, marks = generate_voice_with_marks(scene.speech or scene.line, out, voice=voice, **prosody)
            if scene.speech and scene.speech != scene.line:
                # The voice read Urdu script; the caption shows Roman Urdu.
                marks = display_marks(marks, scene.line)
        else:
            path, marks = generate_voice(scene.speech or scene.line, out, voice=voice, **prosody), None
        scene_audio_paths.append(path)
        scene_marks.append(marks)
    full_voice = run_dir / "voice.mp3"
    audio_path = full_voice if full_voice.exists() and full_voice.stat().st_size > 10_000 else generate_voice(lesson.spoken_narration, full_voice, voice=voice, **prosody)
    from .anchor_lipsync import finish_anchor_job, start_anchor_job

    # Kaggle lip-syncs the anchor while the video renders.
    anchor_job = start_anchor_job(channel, run_dir, scene_audio_paths, content_type)
    video_path = render_video(
        lesson, audio_path, run_dir / "video.mp4", run_dir,
        scene_audio_paths=scene_audio_paths, channel=channel,
        scene_marks=scene_marks, content_type=content_type,
    )
    if anchor_job:
        try:
            video_path = finish_anchor_job(anchor_job, video_path, run_dir, lesson, scene_audio_paths)
        except Exception as exc:
            print(f"[anchor] left out of this video: {exc}")
    from .presenter import add_presenter, presenter_enabled

    if presenter_enabled(channel):
        try:
            video_path = add_presenter(video_path, run_dir, channel, content_type)
        except Exception as exc:
            print(f"[presenter] left out of this video: {exc}")
    if content_type != "long":
        # The last seconds of every Short: LIKE / SHARE / SUBSCRIBE / COMMENT
        # buttons on screen (the owner's request, 2026-11-12). A long video gets
        # them on its end screen.
        from .video import add_cta_overlay

        try:
            video_path = add_cta_overlay(video_path, run_dir, kids=bool(getattr(channel, "made_for_kids", False)))
        except Exception as exc:
            print(f"[video] like/share/subscribe buttons left out: {exc}")
    if content_type == "long":
        # 20 s "WATCH NEXT / SUBSCRIBE" outro after every long video (owner's rule).
        from .video import add_end_screen

        try:
            video_path = add_end_screen(video_path, run_dir, voice_path=_end_call_voice(channel, run_dir))
        except Exception as exc:
            print(f"[video] end screen left out: {exc}")
    video_path = add_background_music(
        video_path, run_dir, seed_text=lesson.topic_key, channel=channel, lesson=lesson,
    )
    validate_rendered_video(video_path, content_type=content_type)
    quality_report = inspect_video(video_path, content_type)
    subtitle_path = generate_subtitles(audio_path, run_dir / "subtitles.srt", lesson.narration)
    first_stem = Path(lesson.scenes[0].image).stem
    generated_first_scene = [
        path for path in sorted((run_dir / "gen").glob(f"{first_stem}_fresh_*"))
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]
    if generated_first_scene:
        thumbnail_source = generated_first_scene[0]
    else:
        # Only a fallback now — the thumbnail's own AI artwork comes first — so a
        # missing scene image must not fail an otherwise finished video.
        try:
            thumbnail_source = resolve_scene_image(
                lesson.scenes[0], run_dir, channel, require_real_image=True,
                landscape=content_type == "long",
            )
        except Exception as exc:
            print(f"[thumbnail] no scene image available as fallback: {exc}")
            thumbnail_source = None
    thumbnail_path = _real_subject_thumbnail(lesson, channel, run_dir, content_type) or create_thumbnail(
        lesson, thumbnail_source, run_dir / "thumbnail.jpg", channel, content_type,
    )
    # Size, the 2 MB limit, not blank or black (thumbnail_check.py); remade once.
    from .thumbnail_check import ThumbnailNotReady, ensure_ready, thumbnail_required

    thumbnail_problem = ""
    if thumbnail_required():
        try:
            thumbnail_path = ensure_ready(thumbnail_path, regenerate=lambda: create_thumbnail(
                lesson, thumbnail_source, run_dir / "thumbnail_remade.jpg", channel, content_type))
        except ThumbnailNotReady as exc:
            thumbnail_problem = str(exc)
            print(f"[thumbnail] not ready, the video waits: {exc}")
    if not thumbnail_problem:
        thumbnail_path = style_matched(thumbnail_path, lambda name: create_thumbnail(
            lesson, thumbnail_source, run_dir / name, channel, content_type), run_dir, channel)
        # Approved only when it looks like the channel's examples (owner's rule):
        # otherwise the video waits and the retry queue draws it again.
        thumbnail_problem = style_problem(thumbnail_path, channel)
        if thumbnail_problem:
            print(f"[style] the video waits: {thumbnail_problem}")
    metadata = build_metadata(lesson, channel, content_type=content_type)
    _append_image_credits(metadata, run_dir)
    if channel is not None:
        from .verified_facts import source_url

        source = source_url(channel, lesson.topic_key)
        if source:
            metadata["description"] = f"{metadata['description']}\n\nSource: Wikipedia — {source}"

    approval_required = upload and quality_report["passed"] and not is_daily and approval_mode_enabled(channel_id)
    # There is no timed upload queue: a rendered video uploads straight away,
    # limited only by this channel's own daily budget. YouTube then keeps it
    # private until its scheduled publish time. The `queue` argument is kept so
    # older callers and saved scripts still work, but it no longer holds videos
    # back.
    status = (
        "quality_failed" if not quality_report["passed"]
        else "awaiting_approval" if approval_required
        else "daily_upload_pending" if is_daily
        else "rendered"
    )
    video_url = None
    error = None if quality_report["passed"] else "; ".join(quality_report["issues"])
    publish_at = None
    reservation_key = f"job:{job_id}"
    if upload and quality_report["passed"] and not approval_required and not _connected(channel):
        # Rendered fine, but this YouTube account was never authorized. Keep the
        # video and say so, instead of opening a sign-in nobody may be there to
        # answer (which is what hangs an unattended run).
        from .social_accounts import facebook_auto_ready

        if facebook_auto_ready(channel_id):
            # A Facebook-only channel: the video is finished once it is on the
            # Page, so it must not wait in the YouTube upload queue. Videos
            # built after YouTube is connected go to both.
            status = "facebook_only"
        else:
            status = "daily_upload_pending" if is_daily else "rendered"
            error = (f"{getattr(channel, 'name', channel_id)} is not connected to YouTube. "
                     f"Connect it in the dashboard, then this video uploads by itself.")
    elif upload and quality_report["passed"] and not approval_required:
        backoff = active_upload_backoff(channel_id)
        if backoff:
            reason, retry_after = backoff
            error = f"Upload paused until {retry_after}: {reason}"
            if is_daily:
                status = "daily_upload_pending"
        else:
            try:
                from .youtube_upload import ThumbnailUploadError, upload_video
                from .schedule import next_publish_at
                from .pending_uploads import (
                    _UPLOAD_GATE, daily_upload_cap_reached, upload_limit_for_channel, find_uploaded_duplicate,
                    disable_thumbnail_upload, thumbnail_permission_denied, thumbnail_upload_allowed,
                    thumbnail_wait_reason,
                )

                with _UPLOAD_GATE:
                    duplicate = find_uploaded_duplicate(
                        channel_id, lesson.topic_key, content_type, Path(video_path),
                    )
                    if duplicate:
                        status = "duplicate_blocked"
                        error = (
                            f"Duplicate upload blocked ({duplicate[2]}); "
                            f"existing video #{duplicate[0]}: {duplicate[1]}"
                        )
                    elif thumbnail_problem:
                        # Never uploaded without a good thumbnail; the retry
                        # queue checks it again.
                        status = "daily_upload_pending"
                        error = f"Thumbnail not ready: {thumbnail_problem}"
                    elif thumbnail_wait_reason(channel_id):
                        # Uploaded later, when a thumbnail can be set with it.
                        status = "daily_upload_pending"
                        error = ("Waiting so the video never goes public without its thumbnail: "
                                 + thumbnail_wait_reason(channel_id))
                    elif daily_upload_cap_reached(channel_id, account_id):
                        # This channel already used its own daily budget. The
                        # video waits as pending and the worker uploads it as
                        # soon as the quota day resets.
                        status = "daily_upload_pending"
                        error = (
                            f"{channel.name} app upload cap reached "
                            f"({upload_limit_for_channel(channel_id, account_id)}/day "
                            "for this YouTube account); "
                            "automatic upload resumes after the YouTube quota day resets"
                        )
                    else:
                        publish_at = next_publish_at(channel_id, reservation_key=reservation_key,
                                                     content_type=content_type)
                        video_url = upload_video(
                            video_path, thumbnail_path, metadata, channel=channel,
                            publish_at=publish_at,
                            upload_thumbnail=thumbnail_upload_allowed(channel_id),
                        )
                        status = "scheduled" if publish_at else "uploaded"
            except ThumbnailUploadError as exc:
                video_url = exc.video_url
                error = str(exc)
                # Held private until the thumbnail is set (youtube_upload), so a
                # video never goes public without it (owner's rule, 2026-11-04).
                status = "thumbnail_pending"
                if thumbnail_permission_denied(error):
                    disable_thumbnail_upload(channel_id)
                else:
                    apply_upload_error_backoff(channel_id, error)
            except Exception as exc:
                from .schedule import cancel_publish_at

                cancel_publish_at(reservation_key)
                status = "daily_upload_pending" if is_daily else "upload_failed"
                error = str(exc)
                apply_upload_error_backoff(channel_id, error)
                publish_at = None  # nothing reached YouTube, so release the slot

    if video_url and channel is not None and getattr(channel, "focus_country", ""):
        try:
            from .playlists import add_to_playlist

            note = add_to_playlist(channel, video_url, lesson.topic_key)
            if note:
                print(f"[playlist] {note}")
        except Exception as exc:
            print(f"[playlist] skipped ({exc})")

    video_db_id = save_video(
        job_id=job_id,
        topic=lesson.topic_key,
        title=str(metadata["title"]),
        video_path=video_path,
        thumbnail_path=thumbnail_path,
        status=status,
        video_url=video_url,
        error=error,
        channel=channel_id,
        publish_at=publish_at.isoformat() if publish_at else None,
        content_type=content_type,
        quality_report=str(quality_report),
        is_daily=is_daily,
        account=account_id,
        )

    # Cross-platform publishing is independent from YouTube. A social failure
    # is recorded per platform and never marks a valid rendered video failed.
    facebook_video = facebook_title = None
    if channel is not None and status not in {"quality_failed", "duplicate_blocked"}:
        try:
            from .dual_language import build_copy, facebook_language

            target = facebook_language(channel)
            if target:
                facebook_video, facebook_title = build_copy(lesson, channel, run_dir, content_type, target)
                print(f"[social] {target} copy for Facebook: {facebook_title}")
        except Exception as exc:
            print(f"[social] second-language copy failed ({exc}); Facebook gets the original")
    # Cross-posting follows the same rule as YouTube: a build-only run, a video
    # held for approval, or one that failed its checks is never posted anywhere.
    if upload and quality_report["passed"] and not approval_required \
            and status not in {"quality_failed", "duplicate_blocked"}:
        try:
            from .social_publish import auto_publish_video
            social_messages = auto_publish_video(
                video_db_id, channel_id, facebook_video=facebook_video, facebook_title=facebook_title,
            )
            for message in social_messages:
                print(f"[social] {message}")
        except Exception as exc:
            print(f"[social] automatic publish skipped: {exc}")
    else:
        print("[social] not posted: this run does not upload")
    # Optional: push the final video to Google Drive and free the local copy.
    if settings.enable_drive_storage and status not in {"daily_upload_pending", "duplicate_blocked", "quality_failed"}:
        try:
            from .drive_storage import upload_file
            link = upload_file(Path(video_path))
            set_drive_url(video_db_id, link)
            Path(video_path).unlink(missing_ok=True)
        except Exception as exc:
            print(f"[drive] backup failed: {exc}")

    # Always delete the big disposable intermediates to keep the app small.
    if settings.enable_run_cleanup:
        _cleanup_run(run_dir)
        if facebook_video:
            import shutil

            shutil.rmtree(Path(facebook_video).parent, ignore_errors=True)

    if status == "quality_failed":
        raise RuntimeError(
            f"Quality check failed for {job_id}: {error}. "
            "The video was kept for review and was not added to the upload queue."
        )

    return RenderedAssets(
        job_id=job_id,
        run_dir=run_dir,
        audio_path=audio_path,
        video_path=video_path,
        subtitle_path=subtitle_path,
        thumbnail_path=thumbnail_path,
    )
