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
- **Story shape on every channel** (the owner's rule, 2026-11-03): hook, then the question the video answers, then the full details, then the answer (`genre_topics.STORY_ARC`; the fact-checked prompt in `verified_facts` keeps it with facts only).
- **An AI script is never half** (the owner's rule, 2026-11-03): a draft with missing scenes, a cut-off sentence, thin lines or too few words is written again (`genre_topics.script_problem`), and if it never comes back whole no video is made. Never pad a script with stock filler sentences.
- **Long videos end with a 20-second "WATCH NEXT / SUBSCRIBE" outro** (`video.add_end_screen`), appended after the narration. The owner places YouTube's end-screen elements on it in Studio (the API cannot).
- **Every description has "WATCH NEXT" links** to the same channel's 3 latest public videos (`seo.watch_next_block`); scheduled or private ones are never linked.
- **New channels** (`channel_factory`): 2 Shorts a day and a long video on Mon/Wed/Fri.
- **No video goes public without its thumbnail** (the owner's rule, 2026-11-04). If the thumbnail cannot be set at upload (YouTube 429 rate limit, no permission), the video is held private with no publish time (`youtube_upload.set_release`) and marked `thumbnail_pending`. Once the thumbnail is on, `pending_uploads` releases it at its booked time or the next free one. Before that, the first guard needs no new login: while a channel's thumbnail limit was hit in the last 24 hours, or it may not set thumbnails, its new videos are not uploaded at all and wait as pending (`pending_uploads.thumbnail_wait_reason`). Holding needs the full YouTube scope: logins made before 2026-11-04 lack it, so those channels must be reconnected on the dashboard (then Send to GitHub). Until then such a video keeps its time and gets the thumbnail as soon as YouTube allows. Don't burn the per-channel thumbnail limit on bulk re-thumbnailing of old videos: on 2026-11-04 that left 3 new Ain videos without one.
- **Older videos get the current thumbnail style slowly** (`src/thumbnail_backfill.py`, the `thumbnail-backfill` step of the daily run, the owner's choice 2026-11-04). Each channel gets at most 2 a day, newest first, and only 3 hours after its own build slot. Nothing runs while a new video waits for its thumbnail or the channel's limit is hit, and a channel stops at its first 429. Done videos are kept in the `thumbnail_backfill` table.
- **Shorts get an upright 1080x1920 thumbnail** in the channel's own style (`src/short_thumbnail.py`, the owner's request 2026-11-03): YouTube shows Shorts as tall cards, so a 16:9 thumbnail lost its sides. The badge sits at the top, the picture fills the card, and the headline is smaller and centred, fitted whole at the bottom (SnuggleMoon: at the top). It uses the same words as the wide layout. Long videos keep the 16:9 layouts.
- **Thumbnails: the same empty space at the top and the bottom** (the owner's rule, 2026-11-02). The top element (logo, badge) starts `thumbnails.SAFE_MARGIN` (36 px) from the top, and the bottom element (headline bar, stripe, last line) ends 36 px from the bottom. Nothing may hug an edge. Every layout uses SAFE_MARGIN.

## Channel: Ain News Alert (`ain_news_alert`), YouTube "Ain The Explainer"
- Genre `trending`, `news_regions: ["US", "EU"]` (US news first), voice `en-US-AriaNeural`, music in `data/music/ain_news_alert/`.
- 2 Shorts a day **without the anchor**. Long videos on Mon/Wed/Fri, 65 scenes (12-18 minutes), written in parts.
- Titles are plain: no "Reels From" or "Amidst" filler, and never the words Reels or Shorts.
- Thumbnails (`src/ain_thumbnail.py`, the owner's design 2026-11-02): long videos show the anchor on the left over navy with a dotted map, a slanted red-and-white edge and the news picture on the right; Shorts have no anchor and the picture fills the frame. An "Ain The Explainer" mark, a slanted red "WORLD NEWS" badge, and the headline in three slanted bars: white on red (context), big navy on white (key word), black on yellow (rest or "What It Means"). The words come from the title only and in its own order. The older BBC-style framed layout (`src/news_thumbnail.py`) is only the fallback.
- Long-video thumbnails also show the anchor in a box on the right (`src/news_thumbnail.py`, `"news_thumbnail": true`); Shorts do not. She is the one allowed exception to the "Real people" rule on a crime thumbnail (the owner's decision, 2026-10-30), so a crime headline drops any name the script gives.
- The anchor's motion: head, eyes and lips only, with NO hand movement (the owner's decision, 2026-10-30). Of the six idle
  clips (Kaggle kernel `ain-anchor-idles`), use 1, 2, 3, 4 and 6. Never use clip 5, the hand gesture.
- The AI anchor (a fictional woman, navy blazer, news desk) appears only in long videos, automatically (`src/anchor_lipsync.py`, `"anchor_lipsync": true`): 5 scenes are lip-synced on Kaggle (LatentSync 1.5, free T4 GPU, kernel `ain-anchor-lipsync`) while the video renders, then laid full screen over each scene from its narration start with an "AIN NEWS" lower third. It needs `KAGGLE_USERNAME` and `KAGGLE_API_TOKEN` in `.env`; GitHub installs the kaggle CLI. If Kaggle fails or takes over 150 minutes, the video goes out without her. A video with her is uploaded with YouTube's "altered or synthetic content" flag (`containsSyntheticMedia`).

## Channel: SnuggleMoon Stories (`snugglemoon_stories`)
- A sleep-story channel, not a cartoon-only one: it keeps calm stock clips (night sky, moon) as well as drawn pictures (the owner's decision, 2026-11-02). On 2026-10-30 a Facebook Reel with a Pexels moon clip was blocked worldwide by a copyright claim from "Roya"; the owner disputed it.
- Thumbnail (`src/sleep_thumbnail.py`, `"sleep_thumbnail": true`): a cosy painted scene made for the story (Cloudflare FLUX first, no people, no lettering; `sleep_thumbnail.art_prompt`), darker at the top, with the title in cream serif capitals with a soft gold glow and a smaller line under it (the title's own subtitle or "A Calming Bedtime Story"), like "The Honey House: A Gentle Victorian Sleep Story" (the owner's design, 2026-11-02). YouTube shows it only after the channel is verified (youtube.com/verify) and "Thumbnails Are Verified" is pressed.

## GitHub account
- This bot always uses the GitHub account **Khalidawan787**. gh on this PC is also signed in to another bot's account, so `scripts/setup_github_actions.py` sends secrets as the repo owner, and git push in this folder uses Khalidawan787 (local `credential.helper`).
- Facebook posting runs from GitHub only (the `facebook-video-post` and `facebook-picture-post` workflows).

## Channel: Crime Files (`crime`), YouTube "Crime Chronicles"
- **What works** (real views up to 2026-11-04): Shorts about famous American outlaws and gangsters with a known face do best (Butch Cassidy 648, Belle Starr 622, Frank Lucas 452, Billy the Kid 269). Heists do about 80 and frauds 20-50. So the subject list and `verified_facts._SUGGEST_BRIEF` favour Wild West outlaws, Prohibition gangsters, bank robbers, con men and drug kingpins. Titles put the name first with a short epithet ("Belle Starr, Outlaw Queen"). Long videos average about 1 view.
- Long videos had two release times (19:00 and 22:30 ET) so the week-long backlog from the 23-29 Oct stuck videos could catch up.
- Thumbnail (`src/crime_thumbnail.py`, `"crime_thumbnail": true`): the case's own free Wikipedia photo on the left, fading into a dark red backdrop; a "CRIME CHRONICLES" mark in red corner brackets top left; a slanted red "CRIME FILES" badge top right; the headline in white Anton capitals with the person's name (or the last words) in black on a tilted yellow stripe (the owner's design, 2026-11-02). Never a stranger's photo, and never the old layout (the owner's decision): without the subject's own photo or art, one of the no-people crime backdrops in `data/thumbnail_backdrops/crime/` is used.

## Channel: Daily Motivation (`motivation`), YouTube "Risevion"
- Thumbnail (`src/motivation_thumbnail.py`, `"motivation_thumbnail": true`): the picture in a thin gold frame, darker on the left; a slanted gold "DAILY MOTIVATION" badge; a 2-4 word slogan ("PAIN CREATES POWER") one word a line in heavy white slanted capitals with an orange edge and glow (the owner's design, 2026-11-02).

## Channel: World Trending (`trending`), "TrendNerve Global" on Facebook and TikTok
- Thumbnail (`src/trend_thumbnail.py`, `"trend_thumbnail": true`): the picture in a thin blue frame, a slanted blue "WORLD TRENDING" badge with a red slash top left, and a navy band at the bottom with the headline in white slanted capitals, a dotted map texture and a red line (the owner's design, 2026-11-02).

## Channel: World Valor Chronicles
- Heroes country by country: 100 Shorts for each country, USA first (`verified_facts.VIDEOS_PER_COUNTRY`).
- Thumbnail (`src/story_thumbnail.py`, `"story_thumbnail": true`): dark left side, the hero's own free Wikipedia photo on the right, then "The story of", the short name (ROOSEVELT) and one 2-5 word fact from the script, in a gold-and-cream serif. YouTube shows it only once the channel is verified (youtube.com/verify) and "Thumbnails Are Verified" is pressed on the dashboard.
