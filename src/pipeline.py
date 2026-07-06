from __future__ import annotations

from datetime import datetime

from pathlib import Path

from .config import settings
from .db import save_video, set_drive_url
from .models import Lesson, RenderedAssets
from .music import add_background_music
from .seo import build_metadata
from .subtitles import generate_subtitles
from .tts import generate_voice
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


def run_pipeline(lesson: Lesson, upload: bool = False, channel=None) -> RenderedAssets:
    # channel=None keeps the original kids behavior (voice/seo/upload unchanged).
    voice = channel.voice if channel is not None else None
    channel_id = channel.id if channel is not None else "kids"

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = settings.runs_dir / job_id
    run_dir.mkdir(parents=True, exist_ok=True)

    scene_audio_paths = [
        generate_voice(scene.line, run_dir / f"voice_scene_{index:02}.mp3", voice=voice)
        for index, scene in enumerate(lesson.scenes, start=1)
    ]
    audio_path = generate_voice(lesson.narration, run_dir / "voice.mp3", voice=voice)
    video_path = render_video(lesson, audio_path, run_dir / "video.mp4", run_dir, scene_audio_paths=scene_audio_paths, channel=channel)
    video_path = add_background_music(video_path, run_dir, seed_text=lesson.topic_key)
    validate_rendered_video(video_path)
    subtitle_path = generate_subtitles(audio_path, run_dir / "subtitles.srt", lesson.narration)
    thumbnail_source = resolve_scene_image(lesson.scenes[0], run_dir, channel)
    thumbnail_path = create_thumbnail(lesson, thumbnail_source, run_dir / "thumbnail.jpg", channel)
    metadata = build_metadata(lesson, channel)

    status = "rendered"
    video_url = None
    error = None
    if upload:
        try:
            from .youtube_upload import ThumbnailUploadError, upload_video

            video_url = upload_video(video_path, thumbnail_path, metadata, channel=channel)
            status = "uploaded"
        except ThumbnailUploadError as exc:
            status = "thumbnail_pending"
            video_url = exc.video_url
            error = str(exc)
        except Exception as exc:
            status = "upload_failed"
            error = str(exc)

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
    )

    # Optional: push the final video to Google Drive and free the local copy.
    if settings.enable_drive_storage:
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
