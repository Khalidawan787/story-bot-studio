from __future__ import annotations

from datetime import datetime
import json

from pathlib import Path

from .config import settings
from .db import active_upload_backoff, apply_upload_error_backoff, approval_mode_enabled, auto_upload_queue_enabled, save_video, set_drive_url
from .models import Lesson, RenderedAssets, Scene
from .music import add_background_music
from .quality import inspect_video
from .seo import build_metadata
from .subtitles import generate_subtitles
from .tts import generate_voice, generate_voice_with_marks
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
    if credits:
        base = str(metadata.get("description") or "")
        block = "\n\nImage credits:\n" + "\n".join(credits)
        metadata["description"] = (base + block)[:5000]


def _interactive_kids_lesson(lesson: Lesson) -> Lesson:
    """Turn passive Kids Shorts into a quick question-and-answer game."""
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
    return Lesson(
        topic_key=lesson.topic_key, title=lesson.title, category=lesson.category,
        intro=lesson.intro, outro=lesson.outro, scenes=scenes,
    )


def run_pipeline(
    lesson: Lesson, upload: bool = False, channel=None, content_type: str = "short",
    queue: bool | None = None, resume_run_dir: Path | None = None,
    is_daily: bool = False,
) -> RenderedAssets:
    # queue: None = use the channel's auto-upload-queue setting (default for the
    # kids/other flows). True = force the timed drip queue. False = upload
    # immediately now (skip the queue). Only matters when upload is True.
    # channel=None keeps the original kids behavior (voice/seo/upload unchanged).
    voice = channel.voice if channel is not None else None
    channel_id = channel.id if channel is not None else "kids"

    if content_type not in {"short", "long"}:
        raise ValueError(f"Unsupported content type: {content_type}")
    if content_type == "short" and (channel is None or getattr(channel, "builtin", False)):
        lesson = _interactive_kids_lesson(lesson)
    run_dir = Path(resume_run_dir).resolve() if resume_run_dir else settings.runs_dir / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job_id = run_dir.name
    run_dir.mkdir(parents=True, exist_ok=True)

    scene_audio_paths = []
    scene_marks: list = []
    for index, scene in enumerate(lesson.scenes, start=1):
        out = run_dir / f"voice_scene_{index:02}.mp3"
        completed_clip = run_dir / f"scene_{index:02}.mp4"
        if out.exists() and completed_clip.exists() and completed_clip.stat().st_size > 100_000:
            path, marks = out, None
        elif settings.enable_karaoke_captions:
            path, marks = generate_voice_with_marks(scene.line, out, voice=voice)
        else:
            path, marks = generate_voice(scene.line, out, voice=voice), None
        scene_audio_paths.append(path)
        scene_marks.append(marks)
    full_voice = run_dir / "voice.mp3"
    audio_path = full_voice if full_voice.exists() and full_voice.stat().st_size > 10_000 else generate_voice(lesson.narration, full_voice, voice=voice)
    video_path = render_video(
        lesson, audio_path, run_dir / "video.mp4", run_dir,
        scene_audio_paths=scene_audio_paths, channel=channel,
        scene_marks=scene_marks, content_type=content_type,
    )
    video_path = add_background_music(video_path, run_dir, seed_text=lesson.topic_key)
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
    thumbnail_path = create_thumbnail(
        lesson, thumbnail_source, run_dir / "thumbnail.jpg", channel, content_type,
    )
    metadata = build_metadata(lesson, channel, content_type=content_type)
    _append_image_credits(metadata, run_dir)

    approval_required = upload and quality_report["passed"] and not is_daily and approval_mode_enabled(channel_id)
    use_queue = auto_upload_queue_enabled(channel_id) if queue is None else queue
    queue_requested = (
        upload and quality_report["passed"] and not approval_required
        and use_queue
    )
    status = (
        "quality_failed" if not quality_report["passed"]
        else "awaiting_approval" if approval_required
        else "queued_for_upload" if queue_requested
        else "daily_upload_pending" if is_daily
        else "rendered"
    )
    video_url = None
    error = None if quality_report["passed"] else "; ".join(quality_report["issues"])
    publish_at = None
    reservation_key = f"job:{job_id}"
    if upload and quality_report["passed"] and not approval_required and not queue_requested:
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
                    elif is_daily and daily_upload_cap_reached(channel_id):
                        status = "daily_upload_pending"
                        error = (
                            f"{channel.name} app upload cap reached "
                            f"({upload_limit_for_channel(channel_id)}/day for this channel); "
                            "automatic upload resumes after the YouTube quota day resets"
                        )
                    else:
                        publish_at = next_publish_at(channel_id, reservation_key=reservation_key)
                        video_url = upload_video(
                            video_path, thumbnail_path, metadata, channel=channel,
                            publish_at=publish_at,
                            upload_thumbnail=thumbnail_upload_allowed(channel_id),
                        )
                        status = "scheduled" if publish_at else "uploaded"
            except ThumbnailUploadError as exc:
                video_url = exc.video_url
                error = str(exc)
                if thumbnail_permission_denied(error):
                    disable_thumbnail_upload(channel_id)
                    status = "scheduled" if publish_at else "uploaded"
                    error = "Video uploaded; custom thumbnail skipped because this channel does not have thumbnail permission."
                else:
                    status = "thumbnail_pending"
                    apply_upload_error_backoff(channel_id, error)
            except Exception as exc:
                from .schedule import cancel_publish_at

                cancel_publish_at(reservation_key)
                status = "daily_upload_pending" if is_daily else "upload_failed"
                error = str(exc)
                apply_upload_error_backoff(channel_id, error)
                publish_at = None  # nothing reached YouTube, so release the slot

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
        )

    # Cross-platform publishing is independent from YouTube. A social failure
    # is recorded per platform and never marks a valid rendered video failed.
    try:
        from .social_publish import auto_publish_video
        social_messages = auto_publish_video(video_db_id, channel_id)
        for message in social_messages:
            print(f"[social] {message}")
    except Exception as exc:
        print(f"[social] automatic publish skipped: {exc}")
    # Optional: push the final video to Google Drive and free the local copy.
    if settings.enable_drive_storage and status not in {"queued_for_upload", "daily_upload_pending", "duplicate_blocked", "quality_failed"}:
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
