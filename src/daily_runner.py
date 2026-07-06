from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .config import settings
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
        rows = conn.execute("SELECT topic, created_at FROM videos WHERE channel = ?", (channel_id,)).fetchall()
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


def videos_generated_today_count(channel_id: str = "kids") -> int:
    if not settings.db_path.exists():
        return 0
    today = datetime.now().date()
    conn = sqlite3.connect(settings.db_path)
    try:
        rows = conn.execute("SELECT created_at FROM videos WHERE channel = ?", (channel_id,)).fetchall()
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


def select_daily_topics(count: int = 5, channel: Channel | None = None) -> list[str]:
    channel = _resolve(channel)
    lessons = load_topics_for(channel)
    history = _topic_history(channel.id)
    today_topics = _topics_generated_today(channel.id)

    candidates = [
        (key, raw.get("category", "General"))
        for key, raw in lessons.items()
        if key not in today_topics
    ]
    candidates.sort(
        key=lambda item: (
            history.get(item[0], TopicHistory(0, "")).count,
            history.get(item[0], TopicHistory(0, "")).last_created_at,
            item[0],
        )
    )

    selected: list[str] = []
    selected_categories: set[str] = set()

    for topic_key, category in candidates:
        if len(selected) >= count:
            break
        if category in selected_categories:
            continue
        selected.append(topic_key)
        selected_categories.add(category)

    for topic_key, _category in candidates:
        if len(selected) >= count:
            break
        if topic_key not in selected:
            selected.append(topic_key)

    return selected[:count]


def run_daily_batch(count: int = 5, upload: bool = True, channel: Channel | None = None) -> list[tuple[str, str]]:
    channel = _resolve(channel)
    results: list[tuple[str, str]] = []
    for topic_key in select_daily_topics(count, channel):
        try:
            assets = run_pipeline(load_lesson_for(channel, topic_key), upload=upload, channel=channel)
            results.append((topic_key, f"OK {assets.job_id}"))
        except Exception as exc:
            results.append((topic_key, f"FAILED {exc}"))
    return results


def run_daily_catchup(target_count: int = 5, upload: bool = True, start_hour: int = 8,
                      channel: Channel | None = None) -> list[tuple[str, str]]:
    channel = _resolve(channel)
    if datetime.now().hour < start_hour:
        return [("catchup", f"SKIPPED before {start_hour:02}:00 local time")]
    missing = max(0, target_count - videos_generated_today_count(channel.id))
    if missing == 0:
        return [("catchup", "OK daily target already reached")]
    return run_daily_batch(count=missing, upload=upload, channel=channel)
