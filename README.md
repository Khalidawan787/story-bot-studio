# Story Bot STUDIO

A multi-channel, automated YouTube video bot. It writes scripts, generates a
voice-over, builds a captioned vertical (Shorts) video with motion, a thumbnail,
and uploads to YouTube — one isolated channel per genre.

## Channels (fully isolated)

Each channel has its own genre, visual style, narrator voice, and its **own
YouTube account** (separate token), so content never crosses channels.

| Channel | Content |
|---------|---------|
| Kids | 1203 built-in learning topics |
| Crime | AI-written true-crime stories (free Gemini) |
| Love | AI-written romance stories |
| Horror | AI-written horror stories |
| Motivation | AI-written motivational stories |

## Features

- **Free AI scripts** — Google Gemini (REST) writes genre stories; kids uses a
  built-in 1203-topic bank.
- **Free images** — Pollinations (genre-styled), optional OpenAI / Google.
- **Voice** — Microsoft Edge TTS (free), per-channel voice.
- **Video** — ffmpeg render, 1080×1920, motion, burned captions, procedural
  background music, subtitles, thumbnail.
- **Upload** — YouTube Data API, per-channel auth, retry queue for offline.
- **Web dashboard** (Flask, dark theme) — tabs per channel; generate / batch /
  daily / retry; "Connect & verify" YouTube; asset coverage; Google Drive
  storage to keep the app small.
- **Portable** — can be built into a single Windows `.exe`.

## Setup

Requirements: Python 3.11+ and **ffmpeg** on PATH (or set `FFMPEG_BIN`).

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env      # then fill in your keys
```

Put your Google OAuth *Desktop* client secret at `credentials/client_secret.json`.

## Run

```powershell
.venv\Scripts\python web_dashboard.py     # dashboard at http://127.0.0.1:8000
```

CLI:

```powershell
python -m src.cli daily --channel kids --count 5 --upload true
python -m src.cli gen-topics --channel crime --count 10
python -m src.cli authorize --channel crime      # link its OWN YouTube channel
python -m src.cli retry-uploads
```

## Build the portable Windows app

```powershell
.venv\Scripts\pyinstaller --noconfirm --name StoryBotStudio --console `
  --collect-all googleapiclient --collect-all google_auth_oauthlib `
  --collect-submodules edge_tts --hidden-import aiohttp web_dashboard.py
```

Then place `channels.json`, `data/`, `.env`, `credentials/`, and a `bin/`
folder with `ffmpeg.exe`/`ffprobe.exe` next to the built exe.

## Security

`.env`, `credentials/`, and all `*token*.json` are git-ignored — **never commit
your API keys or YouTube tokens.** Each genre channel needs its **own** YouTube
channel; authorize it deliberately and pick the correct channel in the browser.
