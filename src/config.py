from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# When packaged as a .exe (PyInstaller), data lives next to the exe, not in src/.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def _resolve_bin(env_val: str | None, exe_name: str) -> str:
    """Prefer a valid env path, then a bundled bin/ next to the app, then PATH."""
    if env_val and Path(env_val).exists():
        return env_val
    bundled = ROOT / "bin" / exe_name
    if bundled.exists():
        return str(bundled)
    return exe_name.replace(".exe", "")


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    data_file: Path = ROOT / "data" / "lessons.json"
    assets_dir: Path = ROOT / "assets"
    runs_dir: Path = ROOT / "runs"
    db_path: Path = ROOT / "bot.sqlite3"
    video_width: int = int(os.getenv("VIDEO_WIDTH", "1080"))
    video_height: int = int(os.getenv("VIDEO_HEIGHT", "1920"))
    video_fps: int = int(os.getenv("VIDEO_FPS", "30"))
    enable_motion: bool = os.getenv("ENABLE_MOTION", "true").lower() == "true"
    enable_fades: bool = os.getenv("ENABLE_FADES", "true").lower() == "true"
    # Render quality preset: fast | balanced | best. Controls the x264 quality,
    # how much the camera-motion pass is supersampled (this is what removes the
    # jittery, cheap-looking drift), and the image polish pass. "balanced" looks
    # clearly better than the old output and still renders on a normal PC.
    video_quality: str = os.getenv("VIDEO_QUALITY", "balanced").lower()
    # Smooth crossfades between scenes instead of hard cuts. Audio stays exactly
    # in sync because each clip carries the transition as extra tail time.
    enable_transitions: bool = os.getenv("ENABLE_TRANSITIONS", "true").lower() == "true"
    transition_seconds: float = float(os.getenv("TRANSITION_SECONDS", "0.45"))
    # Sharpen + vignette + gentle grade. Free image providers cap at about half
    # of 1080p, so this pass is what stops the upscale from looking soft.
    enable_image_polish: bool = os.getenv("ENABLE_IMAGE_POLISH", "true").lower() == "true"
    audio_bitrate: str = os.getenv("AUDIO_BITRATE", "192k")
    edge_voice: str = os.getenv("EDGE_TTS_VOICE", "en-US-AriaNeural")
    edge_rate: str = os.getenv("EDGE_TTS_RATE", "+0%")
    edge_volume: str = os.getenv("EDGE_TTS_VOLUME", "+0%")
    ffmpeg_bin: str = _resolve_bin(os.getenv("FFMPEG_BIN"), "ffmpeg.exe")
    ffprobe_bin: str = _resolve_bin(os.getenv("FFPROBE_BIN"), "ffprobe.exe")
    enable_background_music: bool = os.getenv("ENABLE_BACKGROUND_MUSIC", "true").lower() == "true"
    background_music_volume: float = float(os.getenv("BACKGROUND_MUSIC_VOLUME", "0.08"))
    # Duck the music under the narrator's voice so speech stays crisp instead of
    # fighting a flat music bed (sidechain compression).
    enable_music_ducking: bool = os.getenv("ENABLE_MUSIC_DUCKING", "true").lower() == "true"
    # Word-by-word (karaoke) captions: each spoken word lights up as it is said,
    # using Edge-TTS word timings. Falls back to the plain caption if timings are
    # unavailable, so rendering never breaks.
    enable_karaoke_captions: bool = os.getenv("ENABLE_KARAOKE_CAPTIONS", "true").lower() == "true"
    # Burn the narration text into the picture. YouTube/Facebook also show their
    # OWN automatic captions when the viewer has CC on, which makes the text look
    # doubled. Set this to false to keep only the platform's caption track.
    enable_burned_captions: bool = os.getenv("ENABLE_BURNED_CAPTIONS", "true").lower() == "true"
    enable_whisper: bool = os.getenv("ENABLE_WHISPER", "false").lower() == "true"
    whisper_model_size: str = os.getenv("WHISPER_MODEL_SIZE", "base")
    ai_provider: str = os.getenv("AI_PROVIDER", "none").lower()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    image_provider: str = os.getenv("IMAGE_PROVIDER", "none").lower()
    auto_generate_missing_images: bool = os.getenv("AUTO_GENERATE_MISSING_IMAGES", "false").lower() == "true"
    auto_replace_generated_with_ai: bool = os.getenv("AUTO_REPLACE_GENERATED_WITH_AI", "true").lower() == "true"
    auto_replace_low_quality_images: bool = os.getenv("AUTO_REPLACE_LOW_QUALITY_IMAGES", "true").lower() == "true"
    low_quality_image_bytes: int = int(os.getenv("LOW_QUALITY_IMAGE_BYTES", "200000"))
    enable_pollinations_images: bool = os.getenv("ENABLE_POLLINATIONS_IMAGES", "true").lower() == "true"
    pollinations_api_key: str = os.getenv("POLLINATIONS_API_KEY", "")
    openai_image_model: str = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    openai_image_size: str = os.getenv("OPENAI_IMAGE_SIZE", "1024x1536")
    openai_image_quality: str = os.getenv("OPENAI_IMAGE_QUALITY", "medium")
    openai_image_daily_limit: int = int(os.getenv("OPENAI_IMAGE_DAILY_LIMIT", "25"))
    # Thumbnails: a dedicated click-worthy 16:9 image per video instead of a
    # cropped scene frame. openai = paid gpt-image (best), pollinations = free,
    # scene = old behavior (reuse the first scene image). Any provider failure
    # silently falls back to the next one, so a video is never blocked on it.
    # auto = genre-aware order: real-world channels lead with free Pexels stock
    # photos, the cartoon kids channel leads with generated art. Force one with
    # pexels / openai / pollinations, or "scene" to reuse the first scene image.
    enable_ai_thumbnails: bool = os.getenv("ENABLE_AI_THUMBNAILS", "true").lower() == "true"
    thumbnail_provider: str = os.getenv("THUMBNAIL_PROVIDER", "auto").lower()
    # Free, license-clear stock photos: 200 requests/hour, 20k/month, no cost.
    # Get a key at https://www.pexels.com/api/
    pexels_api_key: str = os.getenv("PEXELS_API_KEY", "")
    openai_thumbnail_model: str = os.getenv("OPENAI_THUMBNAIL_MODEL", os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"))
    openai_thumbnail_quality: str = os.getenv("OPENAI_THUMBNAIL_QUALITY", "high")
    thumbnail_daily_limit: int = int(os.getenv("THUMBNAIL_DAILY_LIMIT", "40"))
    google_image_api_key: str = os.getenv("GOOGLE_IMAGE_API_KEY", "")
    google_image_cx: str = os.getenv("GOOGLE_IMAGE_CX", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    # Second free Gemini model tried when the primary model's daily free quota
    # is exhausted. On the free tier each model has its OWN daily limit, so a
    # different model on the same key keeps scripts flowing. Blank = skip.
    gemini_fallback_model: str = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")
    # Free script fallbacks used before falling back to paid OpenAI.
    # Groq free tier is generous + fast (needs a free key). Pollinations text is
    # free and needs NO key, matching the free image path.
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    enable_pollinations_text: bool = os.getenv("ENABLE_POLLINATIONS_TEXT", "true").lower() == "true"
    youtube_client_secret_file: Path = ROOT / os.getenv("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
    youtube_token_file: Path = ROOT / os.getenv("YOUTUBE_TOKEN_FILE", "token.json")
    youtube_privacy_status: str = os.getenv("YOUTUBE_PRIVACY_STATUS", "public")
    # Scheduled publishing: instead of pushing every video public at the same
    # moment (which kills reach), upload as private with a future publishAt so
    # YouTube drips them out. One global queue covers every channel, and the
    # interval is clamped to 2-3 hours by the scheduler.
    youtube_schedule_uploads: bool = os.getenv("YOUTUBE_SCHEDULE_UPLOADS", "true").lower() == "true"
    youtube_schedule_interval_hours: float = float(os.getenv("YOUTUBE_SCHEDULE_INTERVAL_HOURS", "3"))
    youtube_schedule_first_delay_hours: float = float(os.getenv("YOUTUBE_SCHEDULE_FIRST_DELAY_HOURS", "1"))
    # Optional fixed daily publish times (local hours, comma-separated, e.g.
    # "10,13,16,19,21"). When set, buffered videos are dripped out at these
    # kid-friendly hours across the coming days instead of every N hours around
    # the clock. Empty = keep the simple interval spacing above.
    youtube_schedule_daily_slots: str = os.getenv("YOUTUBE_SCHEDULE_DAILY_SLOTS", "")
    # Safety cap: max NEW uploads per day across all channels, so the automatic
    # queue/retry stays within the configured shared app budget.
    # Extra videos stay in the dashboard and upload after the quota resets.
    # 0 = no cap. Only limits the unattended queue/retry, not a manual "upload now".
    youtube_upload_daily_limit: int = int(os.getenv("YOUTUBE_UPLOAD_DAILY_LIMIT", "8"))
    # Storage: clean big intermediate render files, and optionally push finals to Drive.
    enable_run_cleanup: bool = os.getenv("ENABLE_RUN_CLEANUP", "true").lower() == "true"
    # Generate fresh images every render (don't reuse the same cached images).
    enable_fresh_images: bool = os.getenv("ENABLE_FRESH_IMAGES", "true").lower() == "true"
    enable_drive_storage: bool = os.getenv("ENABLE_DRIVE_STORAGE", "false").lower() == "true"
    drive_folder: str = os.getenv("DRIVE_FOLDER", "StoryBot Videos")
    # Trending world-news channel: which countries' search trends to read.
    # Comma-separated Google Trends geo codes; the world news + Reddit sources
    # are always added on top. Blank-safe — a bad code just skips that source.
    trending_geos: str = os.getenv("TRENDING_GEOS", "US,GB,IN,PK,AE")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


settings = Settings()
