# Kids Learning YouTube Bot - Future Chat Follow-Up

Project folder:

`C:\Users\user\Documents\Codex\2026-07-01\kids-learning-youtube-bot-complete-workflow\outputs\kids_learning_youtube_bot`

Dashboard:

`http://127.0.0.1:8501`

Dashboard command:

```powershell
cd C:\Users\user\Documents\Codex\2026-07-01\kids-learning-youtube-bot-complete-workflow\outputs\kids_learning_youtube_bot
.\.venv\Scripts\python.exe dashboard_local.py
```

## Current Status

- Local dashboard is working.
- YouTube API is connected.
- Upload mode is public.
- Daily automation is installed.
- Daily 5 videos are scheduled for Pakistan 8:00 PM.
- On this PC timezone, that is 8:00 AM local.
- Auto retry task is installed for pending uploads and thumbnails.
- Manual upload button exists in dashboard.
- Already uploaded videos show disabled upload button.
- Old local videos can be deleted from dashboard.
- Video validation blocks blank/silent videos.

## Main Features Done

- 1200+ topics generated.
- Different-topic daily selection is active.
- Edge TTS voice generation is working.
- Background kids music is enabled.
- Video motion/zoom/pan animation is enabled.
- SEO title, description, tags, and hashtags are generated.
- YouTube upload supports public visibility.
- Kids audience flag is set in upload status.
- Thumbnail upload is attempted after video upload.
- Thumbnail rate-limit is handled as `thumbnail_pending`, not full upload failure.
- Pending uploads retry automatically every 30 minutes.
- If daily run is missed because PC was off, catch-up can generate missing videos later when PC is on.

## Image System

Image provider order:

1. OpenAI image generation
2. Pollinations AI free image fallback
3. Google Image API fallback, optional
4. Local illustrated fallback

Important:

- Pollinations AI does not need an API key.
- OpenAI currently has billing hard-limit errors, so Pollinations is the practical fallback.
- Google API fields in dashboard are optional.
- Low-quality color/number/generated images can be auto-replaced.
- Animals currently have high-quality local cartoon images.

## Scheduler Tasks

Daily 5 videos task:

`KidsLearningBotDaily5`

Runs:

`08:00 AM local time = 08:00 PM Pakistan time`

Retry task:

`KidsLearningBotRetryUploads`

Runs every 30 minutes.

Purpose:

- Retry rendered videos.
- Retry failed uploads.
- Retry pending thumbnails later.
- Catch up missing daily videos after 8 AM local time.

## Important Limits

- If PC is off, videos cannot be generated at that moment.
- If PC turns on later, catch-up can generate missing videos.
- Laptop must be on, awake, logged in, and internet connected.
- VS Code does not need to stay open.
- Dashboard does not need to stay open.
- YouTube may temporarily rate-limit thumbnails.
- YouTube thumbnail may show grey for some time after upload.

## Useful Commands

Generate one video without upload:

```powershell
.\.venv\Scripts\python.exe -m src.cli make --topic animal_sounds_001 --upload false
```

Generate one video and upload:

```powershell
.\.venv\Scripts\python.exe -m src.cli make --topic animal_sounds_001 --upload true
```

Daily dry run:

```powershell
.\.venv\Scripts\python.exe -m src.cli daily --count 5 --dry-run true
```

Run daily 5 manually:

```powershell
.\.venv\Scripts\python.exe -m src.cli daily --count 5 --upload true
```

Retry pending uploads:

```powershell
.\.venv\Scripts\python.exe -m src.cli retry-uploads --limit 20
```

Catch up daily missing videos:

```powershell
.\.venv\Scripts\python.exe -m src.cli catch-up-daily --target 5 --upload true --start-hour 8
```

Verify scheduled tasks:

```powershell
schtasks /Query /TN KidsLearningBotDaily5 /FO LIST /V
schtasks /Query /TN KidsLearningBotRetryUploads /FO LIST /V
```

## Move To Another PC

Use:

`MOVE_TO_NEW_PC.md`

Setup command on new PC:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\setup_new_pc.ps1
```

Setup with daily automation:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\setup_new_pc.ps1 -InstallDailyTask
```

## Key Files

- `dashboard_local.py` - local dashboard
- `src/cli.py` - command line runner
- `src/pipeline.py` - main video pipeline
- `src/video.py` - rendering and validation
- `src/visuals.py` - thumbnails and fallback illustrations
- `src/image_assets.py` - OpenAI/Pollinations/Google image logic
- `src/youtube_upload.py` - YouTube upload and thumbnail upload
- `src/pending_uploads.py` - retry pending uploads/thumbnails
- `src/daily_runner.py` - daily topic selection and catch-up
- `src/music.py` - background music
- `src/seo.py` - title, description, tags, hashtags
- `src/db.py` - SQLite video records
- `.env` - API/config settings

## Current Known Issue

YouTube thumbnail API may return:

`uploadRateLimitExceeded`

This means YouTube is temporarily blocking too many thumbnail uploads. The video itself is uploaded. The bot now stores this as:

`thumbnail_pending`

and retries later.

## Future Improvements

- Add better UI filtering/search for 1200+ topics.
- Add real analytics pull from YouTube.
- Add a mobile-friendly remote dashboard.
- Add VPS deployment for true 24/7 automation.
- Improve local fallback images further.
- Add per-category video templates.
