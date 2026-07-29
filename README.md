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
| World Trending | Today's real world trends, found automatically and explained |

### World Trending channel

This channel picks **no fixed topics**. Before every run it reads what the world
is actually searching for and reporting right now — Google Trends (several
countries), Google News world + top headlines, BBC World, Al Jazeera, and
Reddit r/worldnews — merges the same story across sources, ranks it, and drops
anything the channel already covered (45-day memory in
`data/trending_history.json`). The winning topics become that day's videos, so
when the trend changes the channel changes with it.

All sources are free and need no API key. Every source is optional: if one is
unreachable the rest still work, and if all of them fail the run falls back to
the normal AI topic generator instead of failing.

Scripts are **grounded in the real headlines** that were fetched, and the prompt
forbids invented quotes, casualty numbers, and political sides — it stays a
neutral "what happened, why it matters" explainer.

```powershell
python -m src.cli trends --limit 15                  # see today's world trends
python -m src.cli trends --make 2                    # write 2 scripts from them
python -m src.cli authorize --channel trending       # link its OWN YouTube channel
python -m src.cli daily --channel trending --count 2 --upload true
```

Tune the countries with `TRENDING_GEOS` in `.env`.

## Features

- **Free AI scripts** — Google Gemini (REST) writes genre stories; kids uses a
  built-in 1203-topic bank.
- **Free images** — Pollinations (genre-styled), optional OpenAI / Google.
- **Voice** — Microsoft Edge TTS (free), per-channel voice.
- **Video** — ffmpeg render, 1080×1920, eased camera motion, crossfades between
  scenes, burned karaoke captions, procedural background music, subtitles,
  thumbnail.

### Render quality

`VIDEO_QUALITY` in `.env` picks the tier (`fast` / `balanced` / `best`,
default `balanced`). Compared to the old output, `balanced` gives:

- **Smooth motion** — the camera move runs on a 2× supersampled canvas, which
  removes the pixel-stepping judder, and eases in/out instead of sliding at a
  constant speed.
- **Crossfades instead of cuts** — clips carry a transition-length tail, so the
  fade never eats narration and audio stays exactly in sync.
- **Sharper picture** — lanczos scaling plus an unsharp/vignette grade, and
  x264 CRF 19 (~4 Mbit/s vs the old ~1.6 Mbit/s), audio at 192k/48 kHz.

Cost: rendering takes roughly **3× longer** (measured: 47 s → 140 s for a 36-second
clip). Set `VIDEO_QUALITY=fast` to get the old speed back.

> The free Pollinations endpoint caps images at 576×1024, so every scene is
> being upscaled to 1080×1920. The polish pass hides most of it, but the single
> biggest remaining win is a free key from https://enter.pollinations.ai in
> `POLLINATIONS_API_KEY` — that switches the code to the higher-resolution
> endpoint it already supports.
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
python -m src.cli daily --channel kids --count 2 --upload true
python -m src.cli daily-long-all --scenes 20 --upload true
python -m src.cli gen-topics --channel crime --count 10
python -m src.cli authorize --channel crime      # link its OWN YouTube channel
python -m src.cli retry-uploads
```

## Thumbnails

Every video gets its own 16:9 thumbnail: a topic-matched image with the title
burned on top. Compositing (resize/crop, dark panel, accent bar, wrapped
auto-sized title) is done with the **bundled ffmpeg** — no extra image library
and no Node runtime, so the single-file Windows build keeps working.

**Your own picture always wins.** Drop a file in `data/thumbnails/` named after
the topic key and the pipeline uses it instead of any generator — this is how a
thumbnail made in Bing Image Creator, Canva or Photopea joins the automatic run:

| file | behaviour |
|---|---|
| `data/thumbnails/<topic_key>.jpg` | used as the background, the title is drawn on top |
| `data/thumbnails/<topic_key>.final.jpg` | used exactly as-is, nothing is drawn |

Otherwise `THUMBNAIL_PROVIDER=auto` (default) picks the order per channel:

| channel | order |
|---|---|
| crime / horror / love / motivation / trending | **Pexels** (free stock) → OpenAI → Pollinations |
| kids (cartoon look) | OpenAI → Pollinations → Pexels |

With `ENABLE_COMFYUI_THUMBNAILS=true`, a local **ComfyUI** server leads every
order: unlimited images, no credits, no API bill. Start ComfyUI, put a
FLUX.1-schnell (Apache-2.0) or SDXL checkpoint in `ComfyUI/models/checkpoints/`,
and set `COMFYUI_CHECKPOINT`. The built-in graph is a standard FLUX-schnell
workflow; to use your own, export it from ComfyUI in **API format** to
`data/comfyui_workflow.json` — `{prompt}`, `{seed}`, `{width}` and `{height}`
are substituted. If the server is not answering the run falls through to the
next provider instead of stalling. **This needs a real GPU** (roughly 8-12 GB
VRAM); on integrated graphics a single image takes many minutes.

If all of them fail it falls back to the rendered scene image, then a generated
backdrop — a thumbnail is always produced and a thumbnail problem never stops an
upload. Force one source with `THUMBNAIL_PROVIDER=pexels|openai|pollinations`,
or `scene` for the old behavior.

**Pexels is the free, reliable default:** 200 requests/hour and 20,000/month at
no cost, license-clear, no attribution required (the photographer is credited in
the description anyway). Get a key at <https://www.pexels.com/api/> and paste it
into `PEXELS_API_KEY` in `.env`, or into the dashboard's **Save Pexels Key**
field. Pollinations stays as the AI-generated option but rate-limits (HTTP 429)
on the anonymous tier, so do not rely on it as the primary source.

```powershell
python -m src.cli preview-thumbnail --topic fun_colors_0331 --channel kids
python -m src.cli fix-thumbnails --channel kids --limit 10   # videos already on YouTube
python -m src.cli enable-thumbnails --channel crime          # after youtube.com/verify
```

YouTube only allows custom thumbnails on **phone-verified** channels. When it
refuses, the app records that and uploads without a thumbnail; verify the
channel at <https://youtube.com/verify>, then run `enable-thumbnails` (or press
"Thumbnails Are Verified" in the dashboard) and `fix-thumbnails`.

## SEO

Every upload is built for search: the title carries the hook plus a searched
keyword phrase, the tag list is packed strongest-first into YouTube's
500-character budget (usually 25-35 tags), and the description contains the
**full narration text** followed by chapters, an "In this video" list, a
"Topics covered" keyword line and hashtags. See `src/seo.py` — per-genre
keyword sets live in `_GENRE_KEYWORDS`.

## Captions

One caption layer is burned into the video. If text looks doubled on YouTube or
Facebook, the second one is the **platform's own automatic captions** (the CC
button), not part of the file — turn CC off in the player, or set
`ENABLE_BURNED_CAPTIONS=false` in `.env` to ship videos with no burned text and
rely on the platform's caption track only.

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
