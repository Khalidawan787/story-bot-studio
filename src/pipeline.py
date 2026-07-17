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
    queue: bool | None = None,
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
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = settings.runs_dir / job_id
    run_dir.mkdir(parents=True, exist_ok=True)

    scene_audio_paths = []
    scene_marks: list = []
    for index, scene in enumerate(lesson.scenes, start=1):
        out = run_dir / f"voice_scene_{index:02}.mp3"
        if settings.enable_karaoke_captions:
            path, marks = generate_voice_with_marks(scene.line, out, voice=voice)
        else:
            path, marks = generate_voice(scene.line, out, voice=voice), None
        scene_audio_paths.append(path)
        scene_marks.append(marks)
    audio_path = generate_voice(lesson.narration, run_dir / "voice.mp3", voice=voice)
    video_path = render_video(
        lesson, audio_path, run_dir / "video.mp4", run_dir,
        scene_audio_paths=scene_audio_paths, channel=channel,
        scene_marks=scene_marks, content_type=content_type,
    )
    video_path = add_background_music(video_path, run_dir, seed_text=lesson.topic_key)
    validate_rendered_video(video_path, content_type=content_type)
    quality_report = inspect_video(video_path, content_type)
    subtitle_path = generate_subtitles(audio_path, run_dir / "subtitles.srt", lesson.narration)
    thumbnail_source = resolve_scene_image(
        lesson.scenes[0], run_dir, channel, require_real_image=True,
        landscape=content_type == "long",
    )
    thumbnail_path = create_thumbnail(lesson, thumbnail_source, run_dir / "thumbnail.jpg", channel)
    metadata = build_metadata(lesson, channel, content_type=content_type)

    approval_required = upload and quality_report["passed"] and approval_mode_enabled(channel_id)
    use_queue = auto_upload_queue_enabled(channel_id) if queue is None else queue
    queue_requested = (
        upload and quality_report["passed"] and not approval_required
        and use_queue
    )
    status = (
        "quality_failed" if not quality_report["passed"]
        else "awaiting_approval" if approval_required
        else "queued_for_upload" if queue_requested
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
        else:
            try:
                from .youtube_upload import ThumbnailUploadError, upload_video
                from .schedule import next_publish_at

                publish_at = next_publish_at(channel_id, reservation_key=reservation_key)
                video_url = upload_video(video_path, thumbnail_path, metadata, channel=channel,
                                         publish_at=publish_at)
                status = "scheduled" if publish_at else "uploaded"
            except ThumbnailUploadError as exc:
                status = "thumbnail_pending"
                video_url = exc.video_url
                error = str(exc)
                apply_upload_error_backoff(channel_id, error)
            except Exception as exc:
                from .schedule import cancel_publish_at

                cancel_publish_at(reservation_key)
                status = "upload_failed"
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
    if settings.enable_drive_storage and status != "queued_for_upload":
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

    return RenderedAssets(
        job_id=job_id,
        run_dir=run_dir,
        audio_path=audio_path,
        video_path=video_path,
        subtitle_path=subtitle_path,
        thumbnail_path=thumbnail_path,
    )
