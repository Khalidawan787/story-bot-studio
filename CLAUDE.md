# Story Bot Studio: rules for every AI assistant (Claude Code and Cline)

This file and `.clinerules/project-rules.md` hold the same text. When you change one, copy the change to the other.

## What this project is
- A Python bot that writes, renders and uploads videos to several YouTube channels and Facebook Pages. It runs daily on **GitHub Actions** (`.github/workflows/daily-videos.yml`) from the `main` branch. Its state (DB and data files) lives on the `bot-state` branch.
- The dashboard is `web_dashboard.py`. The channels are in `channels.json`. The code is in `src/`.
- The owner writes in Roman Urdu. Answer in Roman Urdu, short and clear.

## Before every commit
1. Run `git diff --cached` and read the WHOLE diff of every file. A whole-file add also ships local experiments.
2. Never commit code that imports a module that is not committed (an untracked `src/*.py`). GitHub runs only what is on `main`: on 2026-10-30 every thumbnail failed this way.
3. Run `python -m compileall -q src` and import the changed modules.
4. Never commit secrets or machine files: `.env`, `.env.*`, `client_secret*.json`, `*token*.json`, `credentials/`, `data/run_log.json`, `data/display_timezone.txt`, `bot.sqlite3`.
5. After a code change, push to `main` and give the owner the zip link:
   https://github.com/Khalidawan787/story-bot-studio/archive/refs/heads/main.zip

## Settings and secrets
- `.env`, the tokens and the client secrets reach GitHub ONLY through the dashboard button **"Send to GitHub"**. After changing any of them, ask the owner to press it.
- Each connected channel has its own Google Cloud project, so about 6 uploads a day. The per-channel caps are `YOUTUBE_UPLOAD_DAILY_LIMIT_<CHANNEL>` in `.env` (5 each).
- Never install packages into the project `.venv` for side tools (Kaggle and similar). Use the system Python.

## Rules for content
- **Old channels:** don't change their behaviour unless the owner asks. A new feature for one channel is scoped to that channel (a field in `channels.json`).
- **Real people:** never show a stranger (a stock clip or photo, or an AI person) next to a real criminal's or suspect's name. Lines and titles about crimes use footage and thumbnails with no people (`stock_video.crime_text`, `_PEOPLE_WORDS`).
- Clips must match the sentence. No cartoons or graphics on real-world channels. A scene with no match shows its own picture.
- **Kids** channels stay made-for-kids. News stays factual and neutral, with no invented quotes or numbers.
- YouTube rejects a description over 5000 bytes; `youtube_upload.youtube_description` trims it.

## Channel: Ain News Alert (`ain_news_alert`), YouTube "Ain The Explainer"
- Genre `trending`, `news_regions: ["US", "EU"]` (US news first), voice `en-US-AriaNeural`, music in `data/music/ain_news_alert/`.
- 2 Shorts a day **without the anchor**. Long videos on Mon/Wed/Fri, 65 scenes (12-18 minutes), written in parts.
- Titles are plain: no "Reels From" or "Amidst" filler, and never the words Reels or Shorts.
- Thumbnails are BBC style: a red frame (breaking news) or a purple one (explainer), a white two-line headline and an "AIN NEWS" badge. No BBC logo or font.
- Long-video thumbnails also show the anchor in a box on the right (`src/news_thumbnail.py`, `"news_thumbnail": true`); Shorts do not. She is the one allowed exception to the "Real people" rule on a crime thumbnail (the owner's decision, 2026-10-30), so a crime headline drops any name the script gives.
- The anchor's motion: head, eyes and lips only, with NO hand movement (the owner's decision, 2026-10-30). Of the six idle
  clips (Kaggle kernel `ain-anchor-idles`), use 1, 2, 3, 4 and 6. Never use clip 5, the hand gesture.
- The AI anchor (a fictional woman, navy blazer, news desk) appears only in long videos: 5 parts lip-synced on Kaggle (LatentSync 1.5, free T4 GPU) and overlaid at each scene's start time. A video with the anchor needs YouTube's "altered or synthetic content" label, which the owner sets in Studio.
- Facebook posting runs from GitHub only (the `facebook-video-post` and `facebook-picture-post` workflows).

## Channel: World Valor Chronicles
- Heroes country by country: 100 Shorts for each country, USA first (`verified_facts.VIDEOS_PER_COUNTRY`).
