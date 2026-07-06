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
    edge_voice: str = os.getenv("EDGE_TTS_VOICE", "en-US-AriaNeural")
    edge_rate: str = os.getenv("EDGE_TTS_RATE", "+0%")
    edge_volume: str = os.getenv("EDGE_TTS_VOLUME", "+0%")
    ffmpeg_bin: str = _resolve_bin(os.getenv("FFMPEG_BIN"), "ffmpeg.exe")
    ffprobe_bin: str = _resolve_bin(os.getenv("FFPROBE_BIN"), "ffprobe.exe")
    enable_background_music: bool = os.getenv("ENABLE_BACKGROUND_MUSIC", "true").lower() == "true"
    background_music_volume: float = float(os.getenv("BACKGROUND_MUSIC_VOLUME", "0.08"))
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
    openai_image_model: str = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    openai_image_size: str = os.getenv("OPENAI_IMAGE_SIZE", "1024x1536")
    openai_image_quality: str = os.getenv("OPENAI_IMAGE_QUALITY", "medium")
    google_image_api_key: str = os.getenv("GOOGLE_IMAGE_API_KEY", "")
    google_image_cx: str = os.getenv("GOOGLE_IMAGE_CX", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    youtube_client_secret_file: Path = ROOT / os.getenv("YOUTUBE_CLIENT_SECRET_FILE", "client_secret.json")
    youtube_token_file: Path = ROOT / os.getenv("YOUTUBE_TOKEN_FILE", "token.json")
    youtube_privacy_status: str = os.getenv("YOUTUBE_PRIVACY_STATUS", "public")
    # Storage: clean big intermediate render files, and optionally push finals to Drive.
    enable_run_cleanup: bool = os.getenv("ENABLE_RUN_CLEANUP", "true").lower() == "true"
    # Generate fresh images every render (don't reuse the same cached images).
    enable_fresh_images: bool = os.getenv("ENABLE_FRESH_IMAGES", "true").lower() == "true"
    enable_drive_storage: bool = os.getenv("ENABLE_DRIVE_STORAGE", "false").lower() == "true"
    drive_folder: str = os.getenv("DRIVE_FOLDER", "StoryBot Videos")


settings = Settings()
