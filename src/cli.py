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
    daily_all.add_argument("--count", default="",
                           help="Videos per channel; blank = each channel's own topics_per_day.")

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
    auth.add_argument("--account", default="main",
                      help="Which connected YouTube account of that channel (default: main)")

    acc = sub.add_parser(
        "accounts",
        help="Connect several YouTube channels to one dashboard channel.",
    )
    acc.add_argument("action", choices=["list", "add", "remove"], help="What to do")
    acc.add_argument("--channel", required=True, help="Dashboard channel id, e.g. kids")
    acc.add_argument("--account", default="", help="Account id for add/remove, e.g. abc")
    acc.add_argument("--name", default="", help="Display name of that YouTube channel")
    acc.add_argument("--client-secret", default="",
                     help="Own client_secret file for a separate upload quota (optional)")

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

    imgtest = sub.add_parser(
        "test-images",
        help="Check every image provider for one channel and say which ones work.",
    )
    imgtest.add_argument("--channel", default="kids", help="Channel id")

    sub.add_parser(
        "quota-status",
        help="Show which Cloud project each channel uploads through, and today's usage.",
    )

    args = parser.parse_args()

    if args.command == "test-images":
        import tempfile
        from pathlib import Path

        from .image_assets import (
            cloudflare_image_ready, generate_cloudflare_scene_image,
            generate_openverse_scene_image, generate_pexels_scene_image,
            generate_pollinations_scene_image, generate_openai_scene_image,
            image_api_ready, openverse_image_ready, pexels_image_ready,
            pollinations_image_ready, _stock_fallback_allowed,
        )
        from .models import Scene

        channel = get_channel(args.channel)
        scene = Scene(
            label="Red", line="Red is a bright colour.", image="test_red.jpg",
            image_prompt="a cheerful red balloon in a bright classroom",
        )
        stock_ok = _stock_fallback_allowed(scene, channel)
        providers = [
            ("OpenAI (paid)", image_api_ready(), generate_openai_scene_image, True),
            ("Cloudflare (free)", cloudflare_image_ready(), generate_cloudflare_scene_image, True),
            ("Pollinations (free)", pollinations_image_ready(), generate_pollinations_scene_image, True),
            ("Pexels (free stock)", pexels_image_ready(), generate_pexels_scene_image, stock_ok),
            ("Openverse (free stock)", openverse_image_ready(), generate_openverse_scene_image, stock_ok),
        ]
        print(f"Image providers for channel '{channel.id}':\n")
        working = 0
        with tempfile.TemporaryDirectory() as folder:
            for name, configured, generate, allowed in providers:
                if not allowed:
                    print(f"  {name:24} n/a  (stock photos are not used on this channel)")
                    continue
                if not configured:
                    print(f"  {name:24} OFF  (not configured)")
                    continue
                target = Path(folder) / f"{name.split()[0].lower()}.jpg"
                try:
                    result = generate(scene, target, channel)
                    print(f"  {name:24} OK   ({result.stat().st_size:,} bytes)")
                    working += 1
                except Exception as exc:
                    print(f"  {name:24} FAIL {str(exc)[:110]}")
        print()
        if working:
            print(f"{working} provider(s) working - videos can be created.")
        else:
            print("No provider is working, so every video will stop before rendering.")
            if not _stock_fallback_allowed(scene, channel):
                print("This channel needs generated cartoon art, so add a free "
                      "Cloudflare Workers AI token (see FREE_IMAGE_SETUP.md).")
        return
    if args.command == "quota-status":
        import json as _json
        from collections import defaultdict

        # load_channels is already imported at module level. Importing it again
        # here made it a local of main(), so every OTHER branch — daily-all
        # included — died with UnboundLocalError before doing any work.
        from .pending_uploads import upload_limit_for_channel, uploads_today_count
        from .youtube_accounts import list_accounts

        by_project: dict[str, list[tuple[str, str]]] = defaultdict(list)
        print(f"{'CHANNEL':<12} {'ACCOUNT':<10} {'TODAY':<8} {'CLOUD PROJECT':<28} CONNECTED")
        for channel in load_channels():
            for account in list_accounts(channel.id):
                secret = account.client_secret_path
                try:
                    blob = _json.loads(secret.read_text(encoding="utf-8"))
                    config = blob.get("installed") or blob.get("web") or {}
                    project = str(config.get("project_id") or "unknown")
                except Exception:
                    project = "MISSING FILE"
                used = uploads_today_count(channel.id, account.id)
                limit = upload_limit_for_channel(channel.id, account.id)
                # Only authorized accounts actually spend the project's quota.
                if account.connected:
                    by_project[project].append((channel.id, account.id))
                print(f"{channel.id:<12} {account.id:<10} {f'{used}/{limit}':<8} {project:<28} "
                      f"{'yes' if account.connected else 'no (not authorized)'}")
        print()
        # One project = 10,000 units/day and one upload costs 1,600 units.
        for project, pairs in sorted(by_project.items()):
            budget = sum(upload_limit_for_channel(cid, aid) for cid, aid in pairs)
            names = ", ".join(f"{cid}/{aid}" for cid, aid in pairs)
            note = "OK" if budget <= 6 else "OVER API QUOTA (max 6/day) - uploads will fail with HTTP 429"
            print(f"{project}: {names} = {budget} uploads/day configured [{note}]")
        return
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
        override = str(getattr(args, "count", "") or "").strip()
        for channel in load_channels():
            n = max(1, int(override)) if override.isdigit() else channel.topics_per_day
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
        from src.youtube_accounts import channel_for_account

        channel = channel_for_account(args.channel, args.account)
        print("=" * 60)
        print(f"Authorizing: {channel.name}  (channel: {args.channel}, "
              f"account: {channel.account}, genre: {channel.genre})")
        print("A browser will open. IMPORTANT: on the Google screen, pick the")
        print("YouTube channel this account is meant for. Every account must")
        print("point at its OWN YouTube channel, or videos land on the wrong one.")
        print("=" * 60)
        _get_service(channel.client_secret_path, channel.token_path)
        print(f"Done. Token saved: {channel.token}")
        return
    elif args.command == "accounts":
        from src.youtube_accounts import add_account, list_accounts, remove_account
        from src.pending_uploads import upload_limit_for_channel, uploads_today_count

        if args.action == "add":
            account = add_account(
                args.channel, args.account, args.name, args.client_secret,
            )
            print(f"Added '{account.id}' ({account.label}) to channel '{args.channel}'.")
            print("Now authorize it against its own YouTube channel:")
            print(f"  python -m src.cli authorize --channel {args.channel} --account {account.id}")
            return
        if args.action == "remove":
            remove_account(args.channel, args.account)
            print(f"Removed '{args.account}' from channel '{args.channel}'. "
                  "Its token file was left on disk; delete it yourself if you want it gone.")
            return
        print(f"YouTube accounts for channel '{args.channel}':")
        print()
        print(f"  {'ACCOUNT':<12} {'TODAY':<8} {'NAME':<28} {'TOKEN':<28} CONNECTED")
        for account in list_accounts(args.channel):
            used = uploads_today_count(args.channel, account.id)
            limit = upload_limit_for_channel(args.channel, account.id)
            print(f"  {account.id:<12} {f'{used}/{limit}':<8} {account.label[:27]:<28} "
                  f"{account.token[:27]:<28} {'yes' if account.connected else 'no'}")
        print()
        print("Add another YouTube channel here with:")
        print(f"  python -m src.cli accounts add --channel {args.channel} --account <id> --name \"<name>\"")
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
