from __future__ import annotations

import argparse

from .ai import generate_ai_lesson
from .channels import get_channel, load_channels, default_channel
from .daily_runner import (
    run_buffer_batch, run_daily_batch, run_daily_catchup, run_daily_long_all,
    select_daily_topics,
)
from .genre_topics import generate_genre_topics
from .lessons import load_lesson, load_lesson_for
from .pending_uploads import retry_pending_uploads
from .pipeline import run_pipeline


def _bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "y"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Story YouTube Bot (multi-channel)")
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("make", help="Render a video from a topic key.")
    make.add_argument("--topic", required=True, help="Topic key")
    make.add_argument("--channel", default="kids", help="Channel id (kids/crime/love/horror/motivation)")
    make.add_argument("--upload", default="false", help="true or false")

    ai_make = sub.add_parser("ai-make", help="Generate a kids lesson with AI, then render it.")
    ai_make.add_argument("--category", required=True, help="Example: animals, colors, numbers")
    ai_make.add_argument("--upload", default="false", help="true or false")

    daily = sub.add_parser("daily", help="Generate a daily batch for one channel.")
    daily.add_argument("--count", default="2", help="Number of videos to create.")
    daily.add_argument("--channel", default="kids", help="Channel id")
    daily.add_argument("--upload", default="true", help="true or false")
    daily.add_argument("--dry-run", default="false", help="Show selected topics without rendering.")

    daily_all = sub.add_parser("daily-all", help="Run the daily batch for EVERY channel.")
    daily_all.add_argument("--upload", default="true", help="true or false")

    daily_long = sub.add_parser("daily-long-all", help="Create one ~5-minute video for every connected channel.")
    daily_long.add_argument("--upload", default="true", help="true or false")
    daily_long.add_argument("--scenes", default="20", help="Scenes per long video; 20 targets about 5 minutes.")

    gen = sub.add_parser("gen-topics", help="Generate new genre topics with free Gemini.")
    gen.add_argument("--channel", required=True, help="Channel id (crime/love/horror/motivation)")
    gen.add_argument("--count", default="10", help="How many new topics to create.")

    trends = sub.add_parser("trends", help="Show what is trending in the world right now.")
    trends.add_argument("--limit", default="15", help="How many trends to show.")
    trends.add_argument("--make", default="0", help="Also write this many trending scripts.")
    trends.add_argument("--channel", default="trending", help="Trending channel id")

    auth = sub.add_parser("authorize", help="Authorize ONE channel's YouTube account (one-time).")
    auth.add_argument("--channel", required=True, help="Channel id to authorize")

    retry = sub.add_parser("retry-uploads", help="Upload rendered/failed videos when internet is back.")
    retry.add_argument("--limit", default="20", help="Maximum pending videos to retry.")

    upg = sub.add_parser("upgrade-images", help="Replace tiny/low-quality asset images with fresh AI ones.")
    upg.add_argument("--limit", default="30", help="Maximum images to upgrade.")

    buf = sub.add_parser("buffer", help="Build many videos ahead and schedule them (keeps publishing while PC is off).")
    buf.add_argument("--count", default="30", help="How many videos to build into the buffer.")
    buf.add_argument("--channel", default="kids", help="Channel id")
    buf.add_argument("--upload", default="true", help="true or false")

    fixthumbs = sub.add_parser("fix-thumbnails", help="Make fresh AI thumbnails for videos already on YouTube.")
    fixthumbs.add_argument("--limit", default="20", help="Maximum videos to update.")
    fixthumbs.add_argument("--channel", default="", help="Only this channel id (blank = all).")

    enthumbs = sub.add_parser("enable-thumbnails", help="Turn custom thumbnails back on after verifying a channel.")
    enthumbs.add_argument("--channel", required=True, help="Channel id")

    prevthumb = sub.add_parser("preview-thumbnail", help="Build one thumbnail locally to look at it (no upload).")
    prevthumb.add_argument("--topic", required=True, help="Topic key")
    prevthumb.add_argument("--channel", default="kids", help="Channel id")
    prevthumb.add_argument("--out", default="", help="Output .jpg path (default: runs/preview_thumbnail.jpg)")

    catchup = sub.add_parser("catch-up-daily", help="Create today's missing daily videos after schedule time.")
    catchup.add_argument("--target", default="2", help="Daily target video count.")
    catchup.add_argument("--channel", default="kids", help="Channel id")
    catchup.add_argument("--upload", default="true", help="true or false")
    catchup.add_argument("--start-hour", default="8", help="Local hour after which catch-up can run.")

    args = parser.parse_args()

    if args.command == "make":
        channel = get_channel(args.channel)
        lesson = load_lesson_for(channel, args.topic)
        assets = run_pipeline(lesson, upload=_bool(args.upload), channel=channel)
    elif args.command == "ai-make":
        lesson = generate_ai_lesson(args.category)
        assets = run_pipeline(lesson, upload=_bool(args.upload))
    elif args.command == "daily":
        channel = get_channel(args.channel)
        count = max(1, int(args.count))
        if _bool(args.dry_run):
            for topic in select_daily_topics(count, channel):
                print(topic)
            return
        for topic, status in run_daily_batch(count=count, upload=_bool(args.upload), channel=channel):
            print(f"[{channel.id}] {topic}: {status}")
        return
    elif args.command == "daily-all":
        for channel in load_channels():
            n = channel.topics_per_day
            print(f"=== Channel: {channel.name} ({n}/day) ===")
            for topic, status in run_daily_batch(count=n, upload=_bool(args.upload), channel=channel):
                print(f"[{channel.id}] {topic}: {status}")
        return
    elif args.command == "daily-long-all":
        for channel_id, status in run_daily_long_all(
            upload=_bool(args.upload), scenes=max(12, min(30, int(args.scenes))),
        ):
            print(f"[{channel_id}] {status}")
        return
    elif args.command == "gen-topics":
        channel = get_channel(args.channel)
        keys = generate_genre_topics(channel, count=max(1, int(args.count)))
        print(f"[{channel.id}] added {len(keys)} topics:")
        for k in keys:
            print(f"  - {k}")
        return
    elif args.command == "trends":
        from .trending import fetch_world_trends, generate_trending_topics
        for rank, trend in enumerate(fetch_world_trends(limit=max(1, int(args.limit))), start=1):
            sources = ", ".join(sorted(trend.sources))
            print(f"{rank:2}. {trend.title}")
            print(f"    score {trend.score:.1f} | sources: {sources}")
        wanted = max(0, int(args.make))
        if wanted:
            channel = get_channel(args.channel)
            keys = generate_trending_topics(channel, count=wanted, scenes=8)
            print(f"\n[{channel.id}] wrote {len(keys)} trending script(s):")
            for k in keys:
                print(f"  - {k}")
        return
    elif args.command == "authorize":
        from src.youtube_upload import _youtube as _get_service
        channel = get_channel(args.channel)
        print("=" * 60)
        print(f"Authorizing channel: {channel.name}  (genre: {channel.genre})")
        print("A browser will open. IMPORTANT: on the Google screen, pick the")
        print(f"YouTube channel that is meant for '{channel.genre}' content —")
        print("NOT your kids channel. Each genre needs its OWN YouTube channel.")
        print("=" * 60)
        _get_service(channel.client_secret_path, channel.token_path)
        print(f"Done. Token saved: {channel.token}")
        return
    elif args.command == "buffer":
        channel = get_channel(args.channel)
        count = max(1, int(args.count))
        print(f"Building a {count}-video buffer for [{channel.id}] and scheduling into the future...")
        for topic, status in run_buffer_batch(count=count, upload=_bool(args.upload), channel=channel):
            print(f"[{channel.id}] {topic}: {status}")
        return
    elif args.command == "retry-uploads":
        for message in retry_pending_uploads(limit=max(1, int(args.limit))):
            print(message)
        return
    elif args.command == "fix-thumbnails":
        from .pending_uploads import refresh_thumbnails
        for message in refresh_thumbnails(
            limit=max(1, int(args.limit)), channel_id=args.channel or None,
        ):
            print(message)
        return
    elif args.command == "enable-thumbnails":
        from .pending_uploads import enable_thumbnail_upload
        channel = get_channel(args.channel)
        enable_thumbnail_upload(channel.id)
        print(f"Custom thumbnails are enabled again for [{channel.id}] {channel.name}.")
        print("If YouTube still refuses, verify that channel at https://youtube.com/verify first.")
        return
    elif args.command == "preview-thumbnail":
        from pathlib import Path as _Path
        from .thumbnails import create_thumbnail
        from .config import settings as _settings
        channel = get_channel(args.channel)
        lesson = load_lesson_for(channel, args.topic)
        out = _Path(args.out) if args.out else _settings.runs_dir / "preview_thumbnail.jpg"
        path = create_thumbnail(lesson, None, out, channel)
        print(f"Thumbnail: {path}")
        return
    elif args.command == "upgrade-images":
        from .image_assets import upgrade_low_quality_assets
        upgraded, messages = upgrade_low_quality_assets(limit=max(1, int(args.limit)))
        for message in messages:
            print(message)
        print(f"Upgraded {upgraded} images.")
        return
    else:
        channel = get_channel(args.channel)
        results = run_daily_catchup(
            target_count=max(1, int(args.target)),
            upload=_bool(args.upload),
            start_hour=max(0, min(23, int(args.start_hour))),
            channel=channel,
        )
        for topic, status in results:
            print(f"{topic}: {status}")
        return

    print(f"Job: {assets.job_id}")
    print(f"Video: {assets.video_path}")
    print(f"Thumbnail: {assets.thumbnail_path}")
    print(f"Subtitles: {assets.subtitle_path}")


if __name__ == "__main__":
    main()
