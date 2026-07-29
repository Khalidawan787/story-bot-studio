from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import settings
from .db import acquire_generation_lock, release_generation_lock
from .channels import Channel, default_channel
from .lessons import load_lesson_for, load_topics_for
from .pipeline import run_pipeline


@dataclass(frozen=True)
class TopicHistory:
    count: int
    last_created_at: str


def _resolve(channel: Channel | None) -> Channel:
    return channel if channel is not None else default_channel()


def _topic_history(channel_id: str) -> dict[str, TopicHistory]:
    if not settings.db_path.exists():
        return {}
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute(
            """
            SELECT topic, COUNT(*) AS total, MAX(created_at) AS last_created_at
            FROM videos WHERE channel = ?
            GROUP BY topic
            """,
            (channel_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return {
        topic: TopicHistory(count=int(total), last_created_at=last_created_at or "")
        for topic, total, last_created_at in rows
    }


def _topics_generated_today(channel_id: str) -> set[str]:
    if not settings.db_path.exists():
        return set()
    today = datetime.now().date()
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute("SELECT topic, created_at FROM videos WHERE channel = ? AND status NOT IN ('quality_failed', 'duplicate_blocked')", (channel_id,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    topics: set[str] = set()
    for topic, created_at in rows:
        try:
            if datetime.fromisoformat(created_at).date() == today:
                topics.add(topic)
        except Exception:
            continue
    return topics


def short_videos_generated_today_count(channel_id: str = "kids") -> int:
    if not settings.db_path.exists():
        return 0
    today = datetime.now().date()
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute(
            """SELECT created_at FROM videos
               WHERE channel = ? AND COALESCE(content_type, 'short') = 'short'
                 AND status NOT IN ('quality_failed', 'duplicate_blocked')""",
            (channel_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return sum(
        1 for (created_at,) in rows
        if created_at and datetime.fromisoformat(created_at).date() == today
    )


def videos_generated_today_count(channel_id: str = "kids") -> int:
    if not settings.db_path.exists():
        return 0
    today = datetime.now().date()
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute("SELECT created_at FROM videos WHERE channel = ? AND status NOT IN ('quality_failed', 'duplicate_blocked')", (channel_id,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    count = 0
    for (created_at,) in rows:
        try:
            if datetime.fromisoformat(created_at).date() == today:
                count += 1
        except Exception:
            continue
    return count


def _analytics_category_scores(channel: Channel, lessons: dict) -> dict[str, float]:
    """Score categories from cached top-video views/retention; no live API call."""
    if not settings.db_path.exists():
        return {}
    conn = sqlite3.connect(settings.db_path)
    try:
        cached = conn.execute(
            "SELECT payload FROM analytics_cache WHERE channel = ?", (channel.id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT topic, video_url FROM videos WHERE channel = ? AND video_url IS NOT NULL",
            (channel.id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    if not cached:
        return {}
    try:
        top = json.loads(cached[0]).get("top_videos", [])
    except Exception:
        return {}
    topic_by_video: dict[str, str] = {}
    for topic, url in rows:
        video_id = str(url).split("v=")[-1].split("&")[0] if "v=" in str(url) else str(url).rstrip("/").split("/")[-1]
        topic_by_video[video_id] = topic
    scores: dict[str, float] = {}
    for rank, row in enumerate(top, start=1):
        topic = topic_by_video.get(str(row.get("video", "")))
        raw = lessons.get(topic or "")
        if not raw:
            continue
        category = raw.get("category", "General")
        views = float(row.get("views", 0) or 0)
        retention = float(row.get("averageViewPercentage", 0) or 0)
        scores[category] = scores.get(category, 0.0) + (views * (1.0 + retention / 100.0)) / rank
    return scores


def _lesson_signature(raw: dict) -> set[str]:
    """Semantic scene signature used to reject near-duplicate lesson variants."""
    signature: set[str] = set()
    for scene in raw.get("scenes", []):
        value = str(scene.get("label") or scene.get("line") or "").lower()
        value = "".join(ch if ch.isalnum() else " " for ch in value)
        normalized = " ".join(value.split())
        if normalized:
            signature.add(normalized)
    return signature


def _too_similar_signature(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    return len(left & right) / min(len(left), len(right)) >= 0.66

def select_daily_topics(count: int = 2, channel: Channel | None = None) -> list[str]:
    channel = _resolve(channel)
    lessons = load_topics_for(channel)
    history = _topic_history(channel.id)
    today_topics = _topics_generated_today(channel.id)

    # Genre channels start with a small seed bank. Replenish it before selection
    # and never auto-publish a genre topic that already exists in video history.
    if not channel.builtin:
        unused = [
            key for key in lessons
            if not key.startswith("long_") and key not in history and key not in today_topics
        ]
        shortage = max(0, count - len(unused))
        if shortage:
            from .trending import is_trending_channel
            try:
                if is_trending_channel(channel):
                    # This channel follows the world instead of inventing topics:
                    # today's live trends become today's videos. If every trend
                    # source is unreachable, fall through to the normal generator
                    # so the daily run still produces something.
                    from .trending import generate_trending_topics
                    if not generate_trending_topics(channel, count=shortage, scenes=8):
                        from .genre_topics import generate_genre_topics
                        generate_genre_topics(channel, count=shortage, scenes=8)
                else:
                    from .genre_topics import generate_genre_topics
                    generate_genre_topics(channel, count=shortage, scenes=8)
                lessons = load_topics_for(channel)
            except Exception as exc:
                print(f"[daily_runner] {channel.id}: could not replenish unique topics ({exc})")

    category_scores = _analytics_category_scores(channel, lessons)
    used_signatures = [
        _lesson_signature(lessons[key])
        for key in history
        if key in lessons and not key.startswith("long_")
    ]

    candidates = [
        (key, raw.get("category", "General"))
        for key, raw in lessons.items()
        if key not in today_topics and not key.startswith("long_")
        and (channel.builtin or key not in history)
        and not any(_too_similar_signature(_lesson_signature(raw), sig) for sig in used_signatures)
    ]
    from .trending import is_trending_channel

    if is_trending_channel(channel):
        # News goes stale fast: publish the freshest scripts (the ones written
        # last, which are today's trends) before any leftover from earlier days.
        order = {key: index for index, key in enumerate(lessons)}
        candidates.sort(key=lambda item: -order.get(item[0], 0))
    else:
        candidates.sort(
            key=lambda item: (
                history.get(item[0], TopicHistory(0, "")).count,
                -category_scores.get(item[1], 0.0),
                history.get(item[0], TopicHistory(0, "")).last_created_at,
                item[0],
            )
        )

    selected: list[str] = []
    selected_categories: set[str] = set()
    selected_signatures: list[set[str]] = []

    for topic_key, category in candidates:
        if len(selected) >= count:
            break
        if category in selected_categories:
            continue
        signature = _lesson_signature(lessons[topic_key])
        if any(_too_similar_signature(signature, other) for other in selected_signatures):
            continue
        selected.append(topic_key)
        selected_categories.add(category)
        selected_signatures.append(signature)

    for topic_key, _category in candidates:
        if len(selected) >= count:
            break
        if topic_key not in selected:
            signature = _lesson_signature(lessons[topic_key])
            if any(_too_similar_signature(signature, other) for other in selected_signatures):
                continue
            selected.append(topic_key)
            selected_signatures.append(signature)

    return selected[:count]


def _run_short_batch(
    count: int, upload: bool, channel: Channel, acquire_lock: bool,
    queue: bool | None = None, is_daily: bool = False,
) -> list[tuple[str, str]]:
    lock_name = f"generation:{channel.id}"
    locked = False
    if acquire_lock:
        locked = acquire_generation_lock(lock_name)
        if not locked:
            return [("lock", "SKIPPED another generation job is already running for this channel")]
    try:
        results: list[tuple[str, str]] = []
        for topic_key in select_daily_topics(count, channel):
            try:
                assets = run_pipeline(load_lesson_for(channel, topic_key), upload=upload, channel=channel, queue=queue, is_daily=is_daily)
                results.append((topic_key, f"OK {assets.job_id}"))
            except Exception as exc:
                results.append((topic_key, f"FAILED {exc}"))
        return results
    finally:
        if locked:
            release_generation_lock(lock_name)


def run_daily_batch(
    count: int = 2, upload: bool = True, channel: Channel | None = None,
    acquire_lock: bool = True, queue: bool | None = None,
) -> list[tuple[str, str]]:
    channel = _resolve(channel)
    return _run_short_batch(count, upload, channel, acquire_lock, queue=queue, is_daily=True)


def run_buffer_batch(
    count: int = 30, upload: bool = True, channel: Channel | None = None,
    acquire_lock: bool = True,
) -> list[tuple[str, str]]:
    """Build videos now and schedule them while the PC can later be offline."""
    channel = _resolve(channel)
    return _run_short_batch(count, upload, channel, acquire_lock)


def run_daily_catchup(target_count: int = 3, upload: bool = True, start_hour: int = 8,
                      channel: Channel | None = None,
                      queue: bool | None = None) -> list[tuple[str, str]]:
    channel = _resolve(channel)
    if datetime.now().hour < start_hour:
        return [("catchup", f"SKIPPED before {start_hour:02}:00 local time")]
    missing = max(0, target_count - short_videos_generated_today_count(channel.id))
    if missing == 0:
        return [("catchup", "OK daily target already reached")]
    return run_daily_batch(count=missing, upload=upload, channel=channel, queue=queue)

def long_videos_generated_today_count(channel_id: str) -> int:
    """Count today's completed/attempted long-form jobs independently of Shorts."""
    if not settings.db_path.exists():
        return 0
    today = datetime.now().date()
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT created_at FROM videos WHERE channel = ? AND content_type = 'long' AND status NOT IN ('quality_failed', 'duplicate_blocked')",
            (channel_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return sum(
        1 for (created_at,) in rows
        if created_at and datetime.fromisoformat(created_at).date() == today
    )


def _automatic_long_idea(channel: Channel) -> str:
    today_label = datetime.now().strftime("%B %d, %Y")
    from .trending import is_trending_channel, trending_long_idea

    if is_trending_channel(channel):
        try:
            idea = trending_long_idea(channel)
        except Exception as exc:
            print(f"[daily_runner] {channel.id}: trend lookup failed ({exc})")
            idea = None
        if idea:
            return idea
    topics = load_topics_for(channel)
    candidates = [
        (key, raw) for key, raw in topics.items()
        if not key.startswith("long_")
    ]
    if not candidates:
        return f"Create a fresh original {channel.genre} video for {today_label}."
    history = _topic_history(channel.id)
    candidates.sort(key=lambda item: (
        history.get(item[0], TopicHistory(0, "")).count,
        history.get(item[0], TopicHistory(0, "")).last_created_at,
    ))
    seed_title = candidates[0][1].get("title", candidates[0][0])
    return (
        f"Create a complete, original {channel.genre} video inspired by: "
        f"{seed_title}. Date seed: {today_label}."
    )


def run_long_video(
    channel: Channel,
    prompt: str | None = None,
    upload: bool = True,
    scenes: int = 20,
    queue: bool | None = None, is_daily: bool = False,
):
    """Create one new ~5-minute 16:9 video for a selected channel."""
    from .genre_topics import generate_from_prompt

    if upload and not channel.token_path.exists():
        raise RuntimeError(f"{channel.name} is not connected to YouTube")
    idea = (prompt or "").strip() or _automatic_long_idea(channel)
    key = generate_from_prompt(channel, idea, scenes=scenes, long_form=True)
    assets = run_pipeline(
        load_lesson_for(channel, key), upload=upload, channel=channel,
        content_type="long", queue=queue, is_daily=is_daily,
    )
    return key, assets


def run_long_batch(
    channel: Channel, count: int = 1, upload: bool = True,
    prompts: list[str | None] | None = None, scenes: int = 20,
    queue: bool | None = None,
) -> list[tuple[str, str]]:
    """Create one or more long videos for one channel without overlapping jobs."""
    count = max(1, min(20, int(count)))
    lock_name = f"generation:{channel.id}"
    if not acquire_generation_lock(lock_name):
        return [("lock", "SKIPPED another generation job is already running for this channel")]
    try:
        ideas = list(prompts or _offline_long_ideas(channel, count))[:count]
        while len(ideas) < count:
            ideas.append(None)
        results: list[tuple[str, str]] = []
        for idea in ideas:
            try:
                key, assets = run_long_video(
                    channel, prompt=idea, upload=upload, scenes=scenes,
                    queue=queue,
                )
                results.append((key, f"OK {assets.job_id}"))
            except Exception as exc:
                results.append((str(idea or "auto")[:50], f"FAILED {exc}"))
        return results
    finally:
        release_generation_lock(lock_name)


def run_daily_long_all(upload: bool = True, scenes: int = 20) -> list[tuple[str, str]]:
    """Create one ~5-minute 16:9 video for every connected YouTube channel."""
    from .channels import load_channels

    results: list[tuple[str, str]] = []
    for channel in load_channels():
        if not channel.token_path.exists():
            results.append((channel.id, "SKIPPED not connected"))
            continue
        if long_videos_generated_today_count(channel.id) >= 1:
            results.append((channel.id, "SKIPPED today's long video already exists"))
            continue
        lock_name = f"generation:{channel.id}"
        if not acquire_generation_lock(lock_name):
            results.append((channel.id, "SKIPPED another generation job is already running"))
            continue
        try:
            key, assets = run_long_video(
                channel, upload=upload, scenes=scenes, queue=False, is_daily=True,
            )
            results.append((channel.id, f"OK {key} {assets.job_id}"))
        except Exception as exc:
            results.append((channel.id, f"FAILED {exc}"))
        finally:
            release_generation_lock(lock_name)
    return results

def _offline_long_ideas(channel: Channel, days: int, guideline: str = "") -> list[str]:
    topics = [
        (key, raw) for key, raw in load_topics_for(channel).items()
        if not key.startswith("long_")
    ]
    history = _topic_history(channel.id)
    topics.sort(key=lambda item: (
        history.get(item[0], TopicHistory(0, "")).count,
        history.get(item[0], TopicHistory(0, "")).last_created_at,
        item[0],
    ))
    ideas: list[str] = []
    for index in range(days):
        target = (datetime.now() + timedelta(days=index + 1)).strftime("%B %d, %Y")
        seed = (
            topics[index % len(topics)][1].get("title", topics[index % len(topics)][0])
            if topics else f"a fresh original {channel.genre} topic"
        )
        direction = guideline.strip() or "Keep it original, engaging, and appropriate for this channel."
        ideas.append(
            f"Create a unique {channel.genre} episode intended for {target}, inspired by: "
            f"{seed}. Creator guideline: {direction} Do not repeat another episode in this buffer."
        )
    return ideas


def run_offline_buffer(
    days: int,
    guideline: str = "",
    content_mode: str = "both",
    channel: Channel | None = None,
) -> list[tuple[str, str]]:
    """Create and upload a multi-day private/scheduled buffer while online."""
    from .channels import load_channels

    days = max(1, min(14, int(days)))
    if content_mode not in {"short", "long", "both"}:
        raise ValueError("content_mode must be short, long, or both")
    results: list[tuple[str, str]] = []
    channels = [channel] if channel is not None else load_channels()
    for selected_channel in channels:
        channel = selected_channel
        if not channel.token_path.exists():
            results.append((channel.id, "SKIPPED not connected"))
            continue
        lock_name = f"generation:{channel.id}"
        if not acquire_generation_lock(lock_name):
            results.append((channel.id, "SKIPPED another generation job is already running"))
            continue
        try:
            if content_mode in {"short", "both"}:
                count = days * max(1, channel.topics_per_day)
                short_results = run_buffer_batch(
                    count=count, upload=True, channel=channel, acquire_lock=False,
                )
                ok = sum(1 for _topic, status in short_results if status.startswith("OK"))
                results.append((channel.id, f"short buffer {ok}/{count} queued"))
            if content_mode in {"long", "both"}:
                ideas = _offline_long_ideas(channel, days, guideline)
                ok = 0
                errors: list[str] = []
                for idea in ideas:
                    try:
                        run_long_video(channel, prompt=idea, upload=True, scenes=20)
                        ok += 1
                    except Exception as exc:
                        errors.append(str(exc))
                detail = f"long buffer {ok}/{days} queued"
                if errors:
                    detail += f"; last error: {errors[-1]}"
                results.append((channel.id, detail))
        finally:
            release_generation_lock(lock_name)
    return results
