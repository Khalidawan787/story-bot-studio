"""Story Bot STUDIO — the upgraded multi-channel dashboard (Codex bot).

Runs on port 8000 (the old simple bot uses 5000 — don't confuse them).
Dark theme, one tab per channel, and the full power features: 1203-topic kids
bank, batch, daily, retry queue, asset coverage, per-video upload/delete, and
free-Gemini genre topics. Reuses the mature backend.

Run:  .venv\\Scripts\\python.exe web_dashboard.py   -> opens http://127.0.0.1:8000
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, redirect, url_for, send_file, render_template_string, flash, jsonify, abort

from src.config import settings
from src.channels import load_channels, get_channel
from src.daily_runner import (
    run_daily_batch, run_long_batch, run_long_video, run_offline_buffer,
    videos_generated_today_count, short_videos_generated_today_count, long_videos_generated_today_count,
)
from src.pipeline import run_pipeline
from src.lessons import load_lesson_for, load_topics_for
from src.genre_topics import generate_genre_topics
from src.pending_uploads import retry_pending_uploads, upload_one, uploads_today_count, upload_limit_for_channel
from src.image_assets import (asset_stats, generate_missing_assets, upgrade_low_quality_assets,
                              pollinations_key_configured, save_pollinations_api_key)
from src.db import active_upload_backoff, api_usage_today, approval_mode_enabled, auto_upload_queue_enabled, cleanup_uploaded_videos, delete_video, mark_queued_for_upload, set_approval_mode, set_auto_upload_queue, set_drive_url, social_uploads_for_channel
from src import drive_storage, youtube_analytics, social_accounts, social_publish, upload_queue
from src.notifier import is_configured as notifications_configured, notify

app = Flask(__name__)
app.secret_key = "story-bot-studio"


@app.template_filter("localdt")
def local_datetime(value):
    if not value:
        return "Unknown time"
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%A, %d %b %Y, %I:%M %p")
    except (TypeError, ValueError):
        return str(value)


def _dashboard_port() -> int:
    try:
        return max(1, min(65535, int(os.getenv("DASHBOARD_PORT", "8000"))))
    except ValueError:
        return 8000


PORT = _dashboard_port()
JOBS = {}


def channel_rows(channel_id: str, content_type: str | None = None, view: str = "all") -> list[sqlite3.Row]:
    if not settings.db_path.exists():
        return []
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        where = "channel = ? AND hidden_at IS NULL"
        params: list[object] = [channel_id]
        if content_type in {"short", "long"}:
            where += " AND COALESCE(content_type, 'short') = ?"
            params.append(content_type)
        if view == "unuploaded":
            where += " AND video_url IS NULL"
        return conn.execute(
            f"""
            SELECT id, job_id, topic, title, video_path, thumbnail_path,
                   video_url, status, error, created_at, drive_url, publish_at,
                   COALESCE(content_type, 'short') AS content_type, quality_report,
                   COALESCE(upload_progress, 0) AS upload_progress
            FROM videos WHERE {where} ORDER BY id DESC
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _dir_mb(path: Path) -> int:
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return round(total / (1024 * 1024))


def upcoming_schedule(channel_id: str, limit: int = 30) -> list[sqlite3.Row]:
    if not settings.db_path.exists():
        return []
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """SELECT title, publish_at, status, COALESCE(content_type,'short') AS content_type
               FROM videos WHERE channel = ? AND publish_at IS NOT NULL
               ORDER BY publish_at ASC LIMIT ?""",
            (channel_id, limit),
        ).fetchall()
    finally:
        conn.close()


def storage_info() -> dict:
    return {
        "runs_mb": _dir_mb(settings.runs_dir),
        "assets_mb": _dir_mb(settings.root / "assets"),
        "drive": drive_storage.is_connected(),
    }


def youtube_project_status(channel) -> dict:
    try:
        fingerprint = hashlib.sha256(channel.client_secret_path.read_bytes()).hexdigest()[:12]
    except OSError:
        return {"fingerprint": "missing", "shared_with": [], "independent": False}
    shared = []
    for other in load_channels():
        try:
            other_fingerprint = hashlib.sha256(other.client_secret_path.read_bytes()).hexdigest()[:12]
        except OSError:
            continue
        if other_fingerprint == fingerprint:
            shared.append(other.name)
    return {"fingerprint": fingerprint, "shared_with": shared, "independent": len(shared) == 1}


def api_center(channel) -> dict:
    used = api_usage_today("openai", "image")
    return {
        "youtube_pause": active_upload_backoff(channel.id),
        "youtube_project": youtube_project_status(channel),
        "gemini": bool(settings.gemini_api_key),
        "openai": bool(settings.openai_api_key),
        "openai_images_used": used,
        "openai_images_limit": settings.openai_image_daily_limit,
        "pollinations": settings.enable_pollinations_images,
        "pollinations_key": pollinations_key_configured(),
        "openverse": True, "wikimedia": True,
        "notifications": notifications_configured(),
        "thumbnails_allowed": _thumbnails_allowed(channel.id),
        "pexels_key": _pexels_key_saved(),
    }


def _pexels_key_saved() -> bool:
    try:
        from src.thumbnails import pexels_key_configured

        return pexels_key_configured()
    except Exception:
        return False


def _thumbnails_allowed(channel_id: str) -> bool:
    try:
        from src.pending_uploads import thumbnail_upload_allowed

        return thumbnail_upload_allowed(channel_id)
    except Exception:
        return True


def current_channel():
    cid = request.args.get("channel") or request.form.get("channel")
    channels = load_channels()
    if cid:
        for c in channels:
            if c.id == cid:
                return c
    return channels[0]


def _run_job(job_id, fn):
    JOBS[job_id]["status"] = "running"
    try:
        detail = fn() or "Completed"
        JOBS[job_id]["detail"] = detail
        # Batch helpers return per-topic status strings rather than raising.
        # Do not paint an all-failed batch green and make it look created.
        if "FAILED" in detail and "OK " not in detail:
            raise RuntimeError(detail)
        JOBS[job_id]["status"] = "done"
        notify(f"DONE: {JOBS[job_id]['label']}\n{JOBS[job_id]['detail']}")
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        detail = str(e).strip() or f"{type(e).__name__}: no additional details were returned"
        lower = detail.lower()
        if "accessnotconfigured" in lower or ("youtube analytics api" in lower and "disabled" in lower):
            detail = ("YouTube Analytics API is not enabled yet. Click Enable YouTube Analytics API, "
                      "wait a few minutes, then Refresh Analytics.")
        elif "quotaexceeded" in lower or ("exceeded your" in lower and "quota" in lower):
            detail = "YouTube API daily quota is finished. Creation continues; upload retries after the quota resets."
        JOBS[job_id]["detail"] = detail
        notify(f"ERROR: {JOBS[job_id]['label']}\n{e}")


def start_job(label, fn):
    jid = f"job_{len(JOBS) + 1}"
    JOBS[jid] = {
        "label": label, "status": "queued", "detail": "",
        "created_at": datetime.now().astimezone().strftime("%A, %d %b %Y, %I:%M %p"),
    }
    threading.Thread(target=_run_job, args=(jid, fn), daemon=True).start()


def _long_job_summary(results: list[tuple[str, str]]) -> str:
    summary = "; ".join(f"{key}:{status}" for key, status in results)
    if not results or not any(status.startswith("OK") for _key, status in results):

        raise RuntimeError(
            "No long video was created, so nothing new will appear in Videos. "
            "The script service was busy; please retry. Details: " + (summary or "no result")
        )
    return summary


def _schedule_history_cleanup() -> None:
    """Clean uploaded dashboard history now, then repeat every six hours."""
    try:
        removed = cleanup_uploaded_videos(days=7)
        if removed:
            print(f"[cleanup] removed {removed} uploaded dashboard record(s) older than 7 days")
    except Exception as exc:
        print(f"[cleanup] skipped: {exc}")
    timer = threading.Timer(6 * 60 * 60, _schedule_history_cleanup)
    timer.daemon = True
    timer.start()


def _schedule_auto_upload_queue() -> None:
    """Check every minute and upload at most one globally due video."""
    try:
        active = any(
            job.get("label") == "Auto YouTube upload queue"
            and job.get("status") in {"queued", "running"}
            for job in JOBS.values()
        )
        if not active and upload_queue.due_video_id() is not None:
            start_job("Auto YouTube upload queue", upload_queue.process_next_upload)
    except Exception as exc:
        print(f"[upload-queue] check skipped: {exc}")
    timer = threading.Timer(60, _schedule_auto_upload_queue)
    timer.daemon = True
    timer.start()

def _safe_media(raw_path: str) -> Path:
    p = Path(raw_path).resolve()
    if settings.root.resolve() not in p.parents or not p.exists():
        abort(404)
    return p


# ---------- routes ----------

@app.route("/")
def home():
    channels = load_channels()
    ch = current_channel()
    topics = load_topics_for(ch)
    selected_type = request.args.get("type", "all")
    if selected_type not in {"all", "short", "long"}:
        selected_type = "all"
    selected_view = request.args.get("view", "all")
    if selected_view not in {"all", "unuploaded"}:
        selected_view = "all"
    vids = channel_rows(ch.id, None if selected_type == "all" else selected_type, selected_view)
    uploaded = sum(1 for v in vids if v["status"] in ("uploaded", "scheduled"))

    analytics = youtube_analytics.snapshot(ch)

    assets = None
    if ch.builtin:  # asset coverage only applies to the kids topic bank
        try:
            a = asset_stats()
            assets = {"total": a.total, "existing": a.existing, "missing": a.missing}
        except Exception:
            assets = None

    return render_template_string(
        PAGE, channels=channels, ch=ch,
        topics=[(k, v.get("title", k)) for k, v in topics.items()],
        videos=vids, jobs=JOBS, assets=assets, storage=storage_info(),
        connected=ch.token_path.exists(), upload_pause=active_upload_backoff(ch.id),
        selected_type=selected_type, selected_view=selected_view, analytics=analytics, approval_mode=approval_mode_enabled(ch.id),
        upload_queue=upload_queue.queue_snapshot(ch.id),
        channel_uploads_today=uploads_today_count(ch.id), channel_upload_limit=upload_limit_for_channel(ch.id),
        queue_times=upload_queue.expected_upload_times(),
        api=api_center(ch), calendar=upcoming_schedule(ch.id),
        social=social_accounts.public_status(ch.id), social_uploads=social_uploads_for_channel(ch.id),
        stats={"topics": len(topics), "videos": len(vids), "uploaded": uploaded,
               "today": videos_generated_today_count(ch.id)},
    )


@app.route("/approval-mode", methods=["POST"])
def approval_mode_route():
    ch = current_channel()
    enabled = request.form.get("enabled") == "on"
    set_approval_mode(ch.id, enabled)
    flash(f"Approval Mode {'enabled' if enabled else 'disabled'} for {ch.name}.", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/queue-mode", methods=["POST"])
def queue_mode_route():
    ch = current_channel()
    enabled = request.form.get("enabled") == "on"
    set_auto_upload_queue(ch.id, enabled)
    flash(f"Auto-upload queue {'enabled' if enabled else 'paused'} for {ch.name}.", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/queue-one", methods=["POST"])
def queue_one_route():
    ch = current_channel()
    video_id = int(request.form.get("id", "0"))
    mark_queued_for_upload(video_id)
    flash(f"Video #{video_id} added to the timed YouTube queue.", "ok")
    return redirect(url_for("home", channel=ch.id, view="unuploaded"))


@app.route("/regenerate-one", methods=["POST"])
def regenerate_one_route():
    ch = current_channel()
    video_id = int(request.form.get("id"))
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT topic, COALESCE(content_type, 'short') AS content_type FROM videos WHERE id = ? AND channel = ?",
            (video_id, ch.id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        flash("Video record not found.", "error")
        return redirect(url_for("home", channel=ch.id))

    def job():
        topic = row["topic"]
        lesson = load_lesson_for(ch, topic)
        if row["content_type"] == "long" and not ch.builtin:
            from src.genre_topics import _looks_like_instruction_title, generate_from_prompt
            if _looks_like_instruction_title(lesson.title):
                topic = generate_from_prompt(ch, lesson.title, scenes=20, long_form=True)
                lesson = load_lesson_for(ch, topic)
        assets = run_pipeline(
            lesson, upload=False, channel=ch,
            content_type=row["content_type"],
        )
        return f"Regenerated as {assets.job_id}; review the new copy below"

    start_job(f"[{ch.id}] Regenerate video #{video_id}", job)
    flash("Regenerating a fresh copy for review...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/generate", methods=["POST"])
def generate():
    ch = current_channel()
    topic = request.form.get("topic")
    upload = request.form.get("upload") == "on"
    if not topic:
        flash("Pick a topic first.", "error")
        return redirect(url_for("home", channel=ch.id))

    def job():
        assets = run_pipeline(load_lesson_for(ch, topic), upload=upload, channel=ch)
        return f"Done: {assets.job_id}"

    start_job(f"[{ch.id}] Video: {topic}", job)
    flash(f"Rendering video for \"{topic}\"...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/custom", methods=["POST"])
def custom():
    ch = current_channel()
    prompt = (request.form.get("prompt") or "").strip()
    scenes = int(request.form.get("scenes", "6"))
    upload = request.form.get("upload") == "on"
    if not prompt:
        flash("Type your topic/prompt first.", "error")
        return redirect(url_for("home", channel=ch.id))

    def job():
        from src.genre_topics import generate_from_prompt
        key = generate_from_prompt(ch, prompt, scenes=scenes)
        assets = run_pipeline(load_lesson_for(ch, key), upload=upload, channel=ch)
        return f"Made video: {key} ({assets.job_id})"

    start_job(f"[{ch.id}] Custom prompt: {prompt[:30]}", job)
    flash(f"Writing a script + video for your prompt: \"{prompt[:40]}\"...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/batch", methods=["POST"])
def batch():
    ch = current_channel()
    count = int(request.form.get("count", "3"))
    upload = request.form.get("upload") == "on"

    def job():
        results = run_daily_batch(count=count, upload=upload, channel=ch)
        return "; ".join(f"{t}:{s}" for t, s in results)

    start_job(f"[{ch.id}] Batch of {count}", job)
    flash(f"Building {count} videos for {ch.name}...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/daily", methods=["POST"])
def daily():
    ch = current_channel()
    upload = request.form.get("upload") == "on"

    def job():
        results = run_daily_batch(count=3, upload=upload, channel=ch, queue=False)
        return "; ".join(f"{t}:{s}" for t, s in results)

    start_job(f"[{ch.id}] Daily (3 Shorts direct + scheduled)", job)
    flash("Building today's 3 Shorts; they will upload directly to YouTube scheduling.", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/long-auto", methods=["POST"])
def long_auto():
    ch = current_channel()
    upload = request.form.get("upload") == "on"
    queue = request.form.get("queue") == "on"

    def job():
        results = run_long_batch(ch, count=1, upload=upload, scenes=20, queue=queue)
        return _long_job_summary(results)

    start_job(f"[{ch.id}] Auto 5-minute long video", job)
    flash(f"Auto-creating a ~5-minute long video for {ch.name}...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/long-custom", methods=["POST"])
def long_custom():
    ch = current_channel()
    prompt = (request.form.get("prompt") or "").strip()
    upload = request.form.get("upload") == "on"
    queue = request.form.get("queue") == "on"
    if not prompt:
        flash("Type a topic for the new long video.", "error")
        return redirect(url_for("home", channel=ch.id))

    def job():
        results = run_long_batch(
            ch, count=1, upload=upload, prompts=[prompt], scenes=20, queue=queue,
        )
        return _long_job_summary(results)

    start_job(f"[{ch.id}] New long video: {prompt[:30]}", job)
    flash(f"Creating a new ~5-minute long video for {ch.name}...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/long-topic", methods=["POST"])
def long_topic():
    ch = current_channel()
    topic_key = (request.form.get("topic") or "").strip()
    upload = request.form.get("upload") == "on"
    queue = request.form.get("queue") == "on"
    topics = load_topics_for(ch)
    if topic_key not in topics:
        flash("Choose a valid topic for the long video.", "error")
        return redirect(url_for("home", channel=ch.id))
    title = topics[topic_key].get("title", topic_key)
    prompt = f"Create a complete original {ch.genre} long video inspired by: {title}."

    def job():
        results = run_long_batch(
            ch, count=1, upload=upload, prompts=[prompt], scenes=20, queue=queue,
        )
        return _long_job_summary(results)

    start_job(f"[{ch.id}] Long topic: {title[:35]}", job)
    flash(f"Creating a ~5-minute long video from '{title}'...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/long-batch", methods=["POST"])
def long_batch():
    ch = current_channel()
    count = max(1, min(20, int(request.form.get("count", "2"))))
    upload = request.form.get("upload") == "on"
    queue = request.form.get("queue") == "on"

    def job():
        results = run_long_batch(ch, count=count, upload=upload, scenes=20, queue=queue)
        return _long_job_summary(results)

    start_job(f"[{ch.id}] Long batch of {count}", job)
    flash(f"Creating {count} long videos for {ch.name}...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/gen-topics", methods=["POST"])
def gen_topics():
    ch = current_channel()
    count = int(request.form.get("count", "10"))
    scenes = int(request.form.get("scenes", "8"))

    from src.trending import generate_trending_topics, is_trending_channel

    trending = is_trending_channel(ch)

    def job():
        if trending:
            # This channel takes its topics from live world trends, not from an
            # invented list. Fall back to the generator only if every source is
            # unreachable, so the button never silently does nothing.
            keys = generate_trending_topics(ch, count=count, scenes=scenes)
            if keys:
                return f"Added {len(keys)} trending topics"
        keys = generate_genre_topics(ch, count=count, scenes=scenes)
        return f"Added {len(keys)} topics"

    start_job(f"[{ch.id}] Writing {count} topics", job)
    if trending:
        flash(f"Finding today's top {count} world trends and writing them...", "ok")
    else:
        flash(f"Writing {count} new {ch.genre} topics with free Gemini...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/offline-buffer", methods=["POST"])
def offline_buffer():
    ch = current_channel()
    active_label = f"[{ch.id}] Offline buffer:"
    if any(
        job.get("status") in {"queued", "running"}
        and str(job.get("label", "")).startswith(active_label)
        for job in JOBS.values()
    ):
        flash(f"An Offline Buffer job is already running for {ch.name}.", "error")
        return redirect(url_for("home", channel=ch.id))
    days = max(1, min(14, int(request.form.get("days", "3"))))
    guideline = (request.form.get("guideline") or "").strip()
    content_mode = request.form.get("content_mode", "both")
    if content_mode not in {"short", "long", "both"}:
        content_mode = "both"

    def job():
        results = run_offline_buffer(
            days=days, guideline=guideline, content_mode=content_mode, channel=ch,
        )
        return "; ".join(f"{channel_id}:{status}" for channel_id, status in results)

    start_job(f"[{ch.id}] Offline buffer: {days} day(s), {content_mode}", job)
    flash(
        f"Creating and scheduling a {days}-day {content_mode} buffer for {ch.name}. Keep this PC online until the job finishes.",
        "ok",
    )
    return redirect(url_for("home", channel=ch.id))


@app.route("/retry", methods=["POST"])
def retry():
    ch = current_channel()
    content_type = request.form.get("content_type")
    if content_type not in {"short", "long"}:
        content_type = None

    def job():
        return " | ".join(
            retry_pending_uploads(limit=20, channel_id=ch.id, content_type=content_type)
        ) or f"Nothing pending for {content_type or 'this channel'}"

    label = content_type.title() if content_type else "All"
    start_job(f"[{ch.id}] Retry {label} uploads", job)
    flash(f"Retrying pending {label.lower()} uploads...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.post("/upload-now")
def upload_now_route():
    ch = current_channel()
    vid = int(request.form.get("id"))
    conn = sqlite3.connect(settings.db_path)
    row = conn.execute("SELECT id FROM videos WHERE id = ? AND channel = ? AND video_url IS NULL", (vid, ch.id)).fetchone()
    conn.close()
    if not row:
        flash("Video was not found or is already on YouTube.", "error")
        return redirect(url_for("home", channel=ch.id, type=request.form.get("type", "all")))

    def job():
        # Explicit action bypasses only the app safety cap; YouTube quota still applies.
        result = upload_one(vid, bypass_safety_cap=True)
        if ": failed " in result.lower() or "skipped" in result.lower():
            raise RuntimeError(result)
        return result

    start_job(f"[{ch.id}] Upload now video #{vid}", job)
    flash("Uploading this video to YouTube now. Its public release still follows the safe schedule.", "ok")
    return redirect(url_for("home", channel=ch.id, type=request.form.get("type", "all")))


@app.route("/upload-one", methods=["POST"])
def upload_one_route():
    ch = current_channel()
    vid = int(request.form.get("id"))
    # Approval and normal rendered videos enter the timed queue. Thumbnail
    # repair keeps its immediate retry behavior because the video is online.
    conn = sqlite3.connect(settings.db_path)
    row = conn.execute("SELECT status FROM videos WHERE id = ? AND channel = ?", (vid, ch.id)).fetchone()
    conn.close()
    if row and row[0] in {"awaiting_approval", "rendered", "upload_failed"}:
        mark_queued_for_upload(vid)
        flash("Video approved and added to the timed YouTube queue.", "ok")
        return redirect(url_for("home", channel=ch.id, view="unuploaded"))

    def job():
        return upload_one(vid)

    start_job(f"[{ch.id}] Upload video #{vid}", job)
    flash("Retrying the YouTube thumbnail...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.post("/fix-thumbnails")
def fix_thumbnails_route():
    """Make a fresh AI thumbnail for this channel's already-published videos."""
    ch = current_channel()
    try:
        limit = max(1, min(50, int(request.form.get("limit") or 10)))
    except ValueError:
        limit = 10

    def job():
        from src.pending_uploads import refresh_thumbnails

        messages = refresh_thumbnails(limit=limit, channel_id=ch.id)
        return "\n".join(messages) or "No published videos to update."

    start_job(f"[{ch.id}] Fix thumbnails on YouTube ({limit})", job)
    flash(
        f"Building fresh thumbnails for up to {limit} published {ch.name} videos. "
        "The videos themselves are not re-uploaded.",
        "ok",
    )
    return redirect(url_for("home", channel=ch.id))


@app.post("/enable-thumbnails")
def enable_thumbnails_route():
    """Re-enable custom thumbnails after verifying the channel on YouTube."""
    ch = current_channel()
    from src.pending_uploads import enable_thumbnail_upload

    enable_thumbnail_upload(ch.id)
    flash(
        f"Custom thumbnails are switched back on for {ch.name}. If YouTube still "
        "refuses, verify that channel at youtube.com/verify first.",
        "ok",
    )
    return redirect(url_for("home", channel=ch.id))


@app.route("/delete-one", methods=["POST"])
def delete_one_route():
    ch = current_channel()
    vid = int(request.form.get("id"))
    delete_video(vid)
    flash(f"Deleted video #{vid}.", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/asset-missing", methods=["POST"])
def asset_missing():
    ch = current_channel()
    count = int(request.form.get("count", "10"))

    def job():
        created, msgs = generate_missing_assets(limit=count)
        return f"Created {created} images"

    start_job(f"[{ch.id}] Generate {count} missing images", job)
    flash(f"Generating up to {count} missing images...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/asset-upgrade", methods=["POST"])
def asset_upgrade():
    ch = current_channel()
    count = int(request.form.get("count", "30"))

    def job():
        upgraded, msgs = upgrade_low_quality_assets(limit=count)
        return f"Upgraded {upgraded} low-quality images"

    start_job(f"[{ch.id}] Upgrade {count} low-quality images", job)
    flash(f"Upgrading up to {count} low-quality images with fresh AI...", "ok")
    return redirect(url_for("home", channel=ch.id))



@app.route("/social-settings", methods=["POST"])
def social_settings_route():
    ch = current_channel()
    platform = (request.form.get("platform") or "").lower()
    if platform == "facebook":
        values = {
            "page_id": request.form.get("page_id", ""),
            "page_access_token": request.form.get("page_access_token", ""),
            "auto_upload": request.form.get("auto_upload") == "on",
        }
    elif platform == "tiktok":
        values = {
            "client_key": request.form.get("client_key", ""),
            "client_secret": request.form.get("client_secret", ""),
            "access_token": request.form.get("access_token", ""),
            "refresh_token": request.form.get("refresh_token", ""),
            "mode": request.form.get("mode", "draft"),
            "privacy": request.form.get("privacy", "SELF_ONLY"),
            "auto_upload": request.form.get("auto_upload") == "on",
        }
    else:
        flash("Unknown social platform.", "error")
        return redirect(url_for("home", channel=ch.id))
    social_accounts.save_account(ch.id, platform, values)
    flash(f"{platform.title()} settings saved for {ch.name}. Use Verify Connection next.", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/social-verify", methods=["POST"])
def social_verify_route():
    ch = current_channel()
    platform = (request.form.get("platform") or "").lower()

    def job():
        if platform == "facebook":
            name = social_publish.verify_facebook(ch.id)
        elif platform == "tiktok":
            name = social_publish.verify_tiktok(ch.id)
        else:
            raise RuntimeError("Unknown social platform.")
        return f"{platform.title()} connected to: {name}"

    start_job(f"[{ch.id}] Verify {platform.title()}", job)
    flash(f"Verifying {platform.title()} connection...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/social-upload", methods=["POST"])
def social_upload_route():
    ch = current_channel()
    platform = (request.form.get("platform") or "").lower()
    video_id = int(request.form.get("id", "0"))
    consent = request.form.get("consent") == "on"

    def job():
        return social_publish.publish_video(
            video_id, platform, ch.id, direct_consent=consent,
        )

    start_job(f"[{ch.id}] {platform.title()} video #{video_id}", job)
    flash(f"Sending video to {platform.title()}...", "ok")
    return redirect(url_for("home", channel=ch.id, type=request.form.get("type", "all")))

@app.route("/analytics-connect", methods=["POST"])
def analytics_connect():
    ch = current_channel()

    def job():
        name = youtube_analytics.connect_analytics(ch)
        youtube_analytics.refresh(ch, days=28)
        return f"Analytics permission connected for {name}. Now click Refresh Analytics."

    start_job(f"[{ch.id}] Connect Analytics", job)
    flash("Opening Google permission for read-only YouTube Analytics...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/analytics-refresh", methods=["POST"])
def analytics_refresh():
    ch = current_channel()

    def job():
        data = youtube_analytics.refresh(ch, days=28)
        return f"Analytics refreshed: {data['start_date']} to {data['end_date']}"

    start_job(f"[{ch.id}] Refresh Analytics", job)
    flash("Refreshing the latest 28-day YouTube Analytics report...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/connect", methods=["POST"])
def connect():
    ch = current_channel()

    def job():
        from src.youtube_upload import connect_and_verify
        name = connect_and_verify(ch.client_secret_path, ch.token_path)
        return f"Connected to YouTube channel: {name}"

    start_job(f"[{ch.id}] Connect YouTube", job)
    flash("Opening Google sign-in in your browser — pick the CORRECT channel for this genre!", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/disconnect", methods=["POST"])
def disconnect():
    ch = current_channel()
    tok = ch.token_path
    if tok.exists():
        tok.rename(tok.with_suffix(tok.suffix + ".bak"))
        flash(f"Disconnected {ch.name}. You can Connect again to link a channel.", "ok")
    else:
        flash("This channel was not connected.", "error")
    return redirect(url_for("home", channel=ch.id))


@app.route("/youtube-project", methods=["POST"])
def youtube_project():
    ch = current_channel()
    uploaded = request.files.get("client_secret_file")
    if not uploaded or not uploaded.filename:
        flash("Select the Google OAuth client JSON file first.", "error")
        return redirect(url_for("home", channel=ch.id))
    raw = uploaded.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        flash("OAuth JSON file is too large.", "error")
        return redirect(url_for("home", channel=ch.id))
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
        client = payload.get("installed") or payload.get("web")
        if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
            raise ValueError("Missing client_id/client_secret")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        flash(f"Invalid Google OAuth JSON: {exc}", "error")
        return redirect(url_for("home", channel=ch.id))

    filename = f"client_secret_{ch.id}.json"
    target = settings.root / filename
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    config_path = settings.root / "channels.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for row in config.get("channels", []):
        if row.get("id") == ch.id:
            row["client_secret"] = filename
            break
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    if ch.token_path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ch.token_path.rename(ch.token_path.with_suffix(ch.token_path.suffix + f".{stamp}.bak"))
    flash(f"Separate Google project saved for {ch.name}. Now click Connect & verify.", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/drive-connect", methods=["POST"])
def drive_connect():
    ch = current_channel()

    def job():
        return f"Google Drive connected: {drive_storage.connect()}"

    start_job("Connect Google Drive", job)
    flash("Opening Google Drive sign-in...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/drive-backup", methods=["POST"])
def drive_backup():
    ch = current_channel()

    def job():
        conn = sqlite3.connect(settings.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, video_path, drive_url FROM videos").fetchall()
        conn.close()
        moved = 0
        for r in rows:
            vp = Path(r["video_path"]) if r["video_path"] else None
            if r["drive_url"] or not vp or not vp.exists():
                continue
            try:
                link = drive_storage.upload_file(vp)
                set_drive_url(int(r["id"]), link)
                vp.unlink(missing_ok=True)
                moved += 1
            except Exception as e:
                return f"Stopped after {moved}: {e}"
        # also clear leftover intermediate files in every run folder
        for run in settings.runs_dir.glob("*"):
            if run.is_dir():
                for f in run.iterdir():
                    if f.is_file() and f.name not in {"thumbnail.jpg", "subtitles.srt"}:
                        f.unlink(missing_ok=True)
        return f"Backed up {moved} videos to Drive and freed local space."

    start_job("Backup videos to Drive & free space", job)
    flash("Uploading local videos to Google Drive...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.post("/pollinations-key")
def pollinations_key_save():
    ch = current_channel()
    try:
        save_pollinations_api_key(request.form.get("api_key", ""))
        flash("Free Pollinations image key saved. New image jobs will use it immediately.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("home", channel=ch.id))


@app.post("/pexels-key")
def pexels_key_save():
    ch = current_channel()
    from src.thumbnails import save_pexels_api_key

    try:
        save_pexels_api_key(request.form.get("api_key", ""))
        flash("Free Pexels key saved. New thumbnails will use free stock photos.", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(url_for("home", channel=ch.id))


@app.route("/status")
def status():
    return jsonify(JOBS)


@app.get("/video-progress")
def video_progress():
    ch = current_channel()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, status, COALESCE(upload_progress, 0) AS progress
               FROM videos WHERE channel = ? AND hidden_at IS NULL""",
            (ch.id,),
        ).fetchall()
        return jsonify({str(row["id"]): {"status": row["status"], "progress": int(row["progress"] or 0)} for row in rows})
    finally:
        conn.close()


@app.route("/media")
def media():
    return send_file(_safe_media(request.args.get("path", "")))


PAGE = r"""
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Story Bot STUDIO</title>
<style>
  :root{--bg:#0f1220;--panel:#191d33;--panel2:#20263f;--line:#2b3251;--text:#e8ebf5;
    --muted:#98a0c0;--accent:#6c8cff;--accent2:#ff6ea9;--ok:#3ddc97;--warn:#ffcf5c;--err:#ff6b6b}
  *{box-sizing:border-box}
  body{margin:0;font-family:'Segoe UI',system-ui,Arial,sans-serif;color:var(--text);min-height:100vh;
    background:radial-gradient(1200px 600px at 80% -10%, #262c4d 0%, transparent 60%), var(--bg)}
  a{color:var(--accent);text-decoration:none}
  header{padding:20px 32px;display:flex;align-items:center;gap:16px;border-bottom:1px solid var(--line);
    background:rgba(15,18,32,.6);backdrop-filter:blur(6px);position:sticky;top:0;z-index:10}
  .logo{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;
    background:linear-gradient(135deg,var(--accent),var(--accent2));font-size:24px}
  header h1{font-size:20px;margin:0}.sub{color:var(--muted);font-size:13px;margin-top:2px}
  .pro{margin-left:auto;font-size:12px;color:var(--ok);border:1px solid #1f6b45;background:#12301f;padding:5px 12px;border-radius:999px;font-weight:700}
  .wrap{max-width:1200px;margin:0 auto;padding:22px 24px 80px}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
  .tab{padding:9px 16px;border-radius:999px;border:1px solid var(--line);background:#161a2e;color:var(--muted);font-weight:600;font-size:14px}
  .tab.active{background:linear-gradient(135deg,var(--accent),#4d6dff);color:#fff;border-color:transparent}
  .grid{display:grid;gap:16px}.stats{grid-template-columns:repeat(4,1fr);margin-bottom:18px}
  .card{background:linear-gradient(180deg,var(--panel),var(--panel2));border:1px solid var(--line);border-radius:16px;padding:18px}
  .stat .n{font-size:30px;font-weight:700}.stat .l{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  h2{font-size:15px;margin:22px 0 12px;color:#cdd4f0}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  select,input[type=number]{background:#12162a;border:1px solid var(--line);color:var(--text);padding:10px 12px;border-radius:10px;outline:none;font:inherit}
  select{flex:1;min-width:240px;max-width:560px}
  label.chk{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:14px}
  button{cursor:pointer;border:none;border-radius:10px;padding:10px 15px;font-weight:600;color:#fff;font:inherit;background:linear-gradient(135deg,var(--accent),#4d6dff)}
  button.ghost{background:#232a45;color:var(--text);border:1px solid var(--line)}
  button.pink{background:linear-gradient(135deg,var(--accent2),#ff4f97)}
  button.green{background:linear-gradient(135deg,#2fbf71,#20a35c)}
  button.red{background:linear-gradient(135deg,#ff6b6b,#e03131)}
  button.sm{padding:7px 11px;font-size:13px}button:hover{filter:brightness(1.08)}button:disabled{opacity:.62;cursor:not-allowed;filter:none}
  .chip{display:inline-block;padding:2px 9px;border-radius:6px;background:#2a3152;color:#c7cff0;font-size:12px;margin-left:6px}
  .flash{padding:12px 16px;border-radius:12px;margin-bottom:14px;border:1px solid}
  .flash.ok{background:#12301f;border-color:#1f6b45;color:#a9f0cd}.flash.error{background:#331617;border-color:#7a2b2f;color:#ffb9bd}
  .jobs-box .job{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 14px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px;background:#161a2e}
  .badge{font-size:12px;padding:3px 10px;border-radius:999px;font-weight:600}
  .badge.queued{background:#2a2f4d;color:#b9c1e6}.badge.running{background:#3a2f12;color:var(--warn)}
  .badge.done{background:#123122;color:var(--ok)}.badge.error{background:#331617;color:var(--err)}
  .vids{grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
  .vid video{width:100%;max-height:360px;border-radius:12px;background:#000;display:block}
  .vid .t{font-weight:600;margin:8px 0 4px;font-size:14px}
  .st{font-size:12px;font-weight:600;padding:2px 8px;border-radius:6px}
  .st.uploaded{background:#123122;color:var(--ok)}.st.rendered{background:#2a2f4d;color:#b9c1e6}
  .st.scheduled{background:#1a2b4d;color:#8fb4ff}
  .st.thumbnail_pending{background:#3a2f12;color:var(--warn)}.st.upload_failed,.st.quality_failed,.st.duplicate_blocked{background:#331617;color:var(--err)}
  .st.daily_upload_pending{background:#2d2754;color:#c9b7ff}.st.awaiting_approval{background:#3a2f12;color:var(--warn)}.st.queued_for_upload{background:#162d4d;color:#8fc7ff}
  .muted{color:var(--muted);font-size:13px}.err{color:var(--err);font-size:11px;margin-top:6px;max-height:48px;overflow:auto}
  .hint{font-size:12px;color:var(--muted);margin-top:8px}
  .bar{height:8px;background:#12162a;border-radius:999px;overflow:hidden;margin:8px 0}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--ok))}
  .channel-banner{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:18px;padding:14px 16px;
    border:1px solid #40538d;border-radius:14px;background:linear-gradient(135deg,#1a2342,#20294a)}
  .channel-banner .name{font-size:18px;font-weight:750}.channel-banner .only{margin-left:auto;color:#a9f0cd;
    background:#12301f;border:1px solid #1f6b45;padding:5px 10px;border-radius:999px;font-size:12px;font-weight:700}
  .workspace-title{margin:22px 0 12px;font-size:13px;color:#98a0c0;text-transform:uppercase;letter-spacing:.9px;font-weight:700}
  .action-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}
  .action-card{height:100%;border-top:3px solid var(--accent)}
  .action-card.long{border-top-color:var(--accent2)}.action-card.buffer{border-top-color:var(--ok)}
  .action-card.custom{border-top-color:var(--warn)}.action-card.full{grid-column:1/-1}
  .action-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px}
  .action-head h2{font-size:17px;margin:0;color:#edf0ff}.scope{font-size:11px;color:#b9c1e6;background:#252d4b;
    border:1px solid #354064;border-radius:999px;padding:4px 9px;white-space:nowrap}
  .field{display:flex;flex-direction:column;gap:6px;color:var(--muted);font-size:12px;font-weight:600;flex:1}
  input[type=text],input[type=password]{background:#12162a;border:1px solid var(--line);color:var(--text);padding:10px 12px;border-radius:10px;outline:none;font:inherit}
  .action-note{margin-top:12px;padding:10px 12px;border-radius:10px;background:#14192d;color:var(--muted);font-size:12px;line-height:1.45}
  .analytics-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:14px 0}
  .metric{padding:14px;background:#14192d;border:1px solid var(--line);border-radius:12px}.metric .n{font-size:23px;font-weight:750}.metric .l{font-size:11px;color:var(--muted);margin-top:3px}
  .analytics-table{width:100%;border-collapse:collapse;font-size:13px}.analytics-table th,.analytics-table td{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left}.analytics-table th{color:var(--muted);font-size:11px;text-transform:uppercase}
  .section-nav{position:sticky;top:85px;z-index:8;display:flex;gap:8px;overflow-x:auto;margin:0 0 18px;padding:10px;
    border:1px solid var(--line);border-radius:14px;background:rgba(15,18,32,.94);backdrop-filter:blur(10px)}
  .section-nav button{white-space:nowrap;background:transparent;color:var(--muted);border:1px solid transparent;padding:9px 14px}
  .section-nav button.active{color:#fff;border-color:#5268a5;background:linear-gradient(135deg,#283455,#202945)}
  .dash-section{display:none}.dash-section.active{display:block;animation:sectionIn .16s ease-out}
  .section-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:4px 0 14px}
  .section-head h2{font-size:19px;color:#f1f3ff;margin:0}.section-head .muted{max-width:620px;text-align:right}
  details.advanced-card{height:auto}details.advanced-card>summary{cursor:pointer;list-style:none;font-size:16px;font-weight:700;color:#edf0ff}
  details.advanced-card>summary::-webkit-details-marker{display:none}details.advanced-card>summary:after{content:'+';float:right;color:var(--muted);font-size:20px}
  details.advanced-card[open]>summary:after{content:'−'}details.advanced-card .advanced-body{padding-top:16px;margin-top:14px;border-top:1px solid var(--line)}
  .platform-tabs{display:flex;gap:8px;margin:12px 0 16px;padding:6px;background:#12162a;border:1px solid var(--line);border-radius:12px}
  .platform-tabs button{flex:1;background:transparent;color:var(--muted);border:1px solid transparent}
  .platform-tabs button.active{background:#283455;color:#fff;border-color:#5268a5}
  .platform-panel{display:none}.platform-panel.active{display:block}.platform-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
  .guide-box{margin-top:14px;padding:12px 14px;border-radius:12px;background:#14192d;border:1px solid var(--line)}
  .guide-box summary{cursor:pointer;font-weight:700}.guide-box ol{margin:10px 0 4px;padding-left:20px;color:var(--muted);font-size:13px;line-height:1.55}
  .social-state{display:flex;gap:6px;flex-wrap:wrap;margin:7px 0}.social-state .chip.failed{color:var(--err)}.social-state .chip.submitted{color:var(--ok)}  @keyframes sectionIn{from{opacity:.45;transform:translateY(3px)}to{opacity:1;transform:none}}  @media(max-width:800px){.action-grid,.stats,.analytics-grid{grid-template-columns:1fr}.action-card.full{grid-column:auto}.channel-banner .only{margin-left:0}}
</style></head><body>
<header><div class="logo">🎬</div><div>
  <h1>Story Bot STUDIO</h1>
  <div class="sub">Multi-channel • 1203 kids topics • daily batch • retry queue • free Gemini genre topics</div>
</div><div class="pro">PRO • port 8000</div></header>
<div class="wrap">

  <div class="tabs">
    {% for c in channels %}<a class="tab {{'active' if c.id==ch.id else ''}}" href="/?channel={{c.id}}">{{c.name}}</a>{% endfor %}
  </div>

  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% for cat,msg in msgs %}<div class="flash {{cat}}">{{msg}}</div>{% endfor %}
  {% endwith %}

  <div class="channel-banner">
    <span class="muted">Active channel</span><span class="name">{{ch.name}}</span>
    <span class="chip">{{ch.genre}}</span><span class="chip">{{'made-for-kids' if ch.made_for_kids else 'general'}}</span>
    <span class="chip">{{ch.privacy}}</span><span class="only">All buttons: {{ch.name}} only</span>
  </div>

  <div class="hint" style="margin:-8px 0 16px">Uploaded/scheduled videos automatically disappear from this dashboard after 7 days. Your YouTube videos are not deleted.</div>

  <nav class="section-nav" aria-label="Dashboard sections">
    <button type="button" data-section="overview" class="active">Overview</button>
    <button type="button" data-section="create">Create Videos</button>
    <button type="button" data-section="analytics">Analytics &amp; Queue</button>
    <button type="button" data-section="library">Video Library</button>
    <button type="button" data-section="system">System</button>
  </nav>

  <section class="dash-section active" id="section-overview">
  <div class="section-head"><h2>{{ch.name}} Overview</h2><div class="muted">Connection, automation mode and today&rsquo;s progress</div></div>

  <div class="card" style="margin-bottom:18px">
    <form method="post" action="/approval-mode" class="row" style="justify-content:space-between">
      <input type="hidden" name="channel" value="{{ch.id}}">
      <div><strong>Preview &amp; Approval Mode</strong><div class="hint">When ON, requested uploads wait for your approval after the quality check.</div></div>
      <label class="chk"><input type="checkbox" name="enabled" {{'checked' if approval_mode else ''}} onchange="this.form.submit()"> {{'ON ? review before upload' if approval_mode else 'OFF ? automatic upload'}}</label>
    </form>
  </div>

  {% if upload_pause %}
  <div class="flash error"><strong>YouTube uploads are temporarily paused.</strong> Video creation still continues and finished videos appear below; YouTube upload/retry will wait until <strong>{{upload_pause[1]|localdt}}</strong>. Reason: {{upload_pause[0]}}</div>
  {% endif %}

  <div class="card" style="margin-bottom:18px">
    <div class="row" style="justify-content:space-between">
      <div>
        <strong>YouTube connection</strong> —
        {% if connected %}<span style="color:var(--ok)">● Connected</span>{% else %}<span style="color:var(--warn)">● Not connected</span>{% endif %}
        <div class="hint">Click "Connect &amp; verify" and it will show which YouTube channel this is linked to. Make sure a genre channel is NOT linked to your kids channel.</div>
        {% if api.youtube_project.independent %}
          <div class="hint" style="color:var(--ok)"><strong>Independent YouTube API project active</strong> for {{ch.name}}.</div>
        {% else %}
          <div class="hint" style="color:#ffd27a"><strong>Google quota is shared with:</strong> {{api.youtube_project.shared_with|join(', ')}}. Upload a separate OAuth JSON below for a truly separate quota.</div>
        {% endif %}
      </div>
      <div class="row">
        <form method="post" action="/connect"><input type="hidden" name="channel" value="{{ch.id}}"><button class="green sm" type="submit">🔗 Connect &amp; verify</button></form>
        {% if connected %}<form method="post" action="/disconnect" onsubmit="return confirm('Disconnect this channel?')"><input type="hidden" name="channel" value="{{ch.id}}"><button class="ghost sm" type="submit">Disconnect</button></form>{% endif %}
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:18px">
    <form method="post" action="/youtube-project" enctype="multipart/form-data" class="row">
      <input type="hidden" name="channel" value="{{ch.id}}">
      <label class="field">Separate Google OAuth JSON for {{ch.name}}
        <input type="file" name="client_secret_file" accept=".json,application/json" required>
      </label>
      <button class="ghost sm" type="submit">Save Separate Project</button>
      <a href="https://console.cloud.google.com/apis/credentials" target="_blank" class="hint">Open Google Cloud credentials guide ↗</a>
    </form>
  </div>

  <div class="grid stats">
    <div class="card stat"><div class="n">{{stats.topics}}</div><div class="l">Topics</div></div>
    <div class="card stat"><div class="n">{{stats.videos}}</div><div class="l">Videos</div></div>
    <div class="card stat"><div class="n">{{stats.uploaded}}</div><div class="l">Uploaded</div></div>
    <div class="card stat"><div class="n">{{stats.today}}</div><div class="l">Today</div></div>
  </div>

  </section>
  <section class="dash-section" id="section-analytics">
  <div class="section-head"><h2>Analytics &amp; Queue</h2><div class="muted">Performance report and upcoming publish schedule</div></div>

  <div class="row" style="justify-content:space-between;margin-top:22px">
    <h2 style="margin:0">YouTube Analytics ? Last 28 Days</h2>
    {% if analytics.connected %}
    <form method="post" action="/analytics-refresh"><input type="hidden" name="channel" value="{{ch.id}}"><button class="ghost sm" type="submit">Refresh Analytics</button></form>
    {% endif %}
  </div>
  <div class="card" style="margin-top:12px">
    {% if not analytics.connected %}
      <strong>Analytics is not connected for {{ch.name}}.</strong>
      <div class="hint">This uses a separate read-only permission and does not change the upload connection.</div>
      <form method="post" action="/analytics-connect" style="margin-top:12px"><input type="hidden" name="channel" value="{{ch.id}}"><button class="green" type="submit">Connect Analytics</button></form>
    {% elif not analytics.data %}
      <strong>Analytics permission is connected.</strong>
      <div class="hint">Enable the YouTube Analytics API in Google Cloud once, wait a few minutes, then click Refresh Analytics.</div>
      <a href="https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com" target="_blank"><button class="green" type="button" style="margin-top:12px">Enable YouTube Analytics API</button></a>
    {% else %}
      {% set a=analytics.data %}{% set m=a.summary %}
      <div class="row" style="justify-content:space-between"><div class="muted">{{a.start_date}} to {{a.end_date}}{% if analytics.stale %} ? cached report needs refresh{% endif %}</div></div>
      <div class="analytics-grid">
        <div class="metric"><div class="n">{{m.get('views',0)|int}}</div><div class="l">Views</div></div>
        <div class="metric"><div class="n">{{m.get('estimatedMinutesWatched',0)|round(0)|int}}</div><div class="l">Minutes watched</div></div>
        <div class="metric"><div class="n">{{m.get('averageViewDuration',0)|round(1)}}s</div><div class="l">Average view duration</div></div>
        <div class="metric"><div class="n">{{m.get('averageViewPercentage',0)|round(1)}}%</div><div class="l">Average viewed</div></div>
        <div class="metric"><div class="n">+{{m.get('subscribersGained',0)|int}}</div><div class="l">Subscribers gained</div></div>
      </div>
      {% if a.content_types %}
      <h2 style="margin-top:12px">Shorts vs Long</h2>
      <table class="analytics-table"><tr><th>Type</th><th>Views</th><th>Watch minutes</th><th>Avg duration</th><th>Avg viewed</th></tr>
      {% for row in a.content_types %}<tr><td>{{row.creatorContentType}}</td><td>{{row.views|int}}</td><td>{{row.estimatedMinutesWatched|round(0)|int}}</td><td>{{row.averageViewDuration|round(1)}}s</td><td>{{row.averageViewPercentage|round(1)}}%</td></tr>{% endfor %}</table>
      {% endif %}
      <h2 style="margin-top:16px">Top Videos</h2>
      {% if a.top_videos %}<table class="analytics-table"><tr><th>Video</th><th>Views</th><th>Watch minutes</th><th>Avg duration</th><th>Subscribers</th></tr>
      {% for row in a.top_videos %}<tr><td><a href="https://youtu.be/{{row.video}}" target="_blank">{{row.title}}</a></td><td>{{row.views|int}}</td><td>{{row.estimatedMinutesWatched|round(0)|int}}</td><td>{{row.averageViewDuration|round(1)}}s</td><td>+{{row.subscribersGained|int}}</td></tr>{% endfor %}</table>{% else %}<div class="muted">No analytics rows returned for this period.</div>{% endif %}
    {% endif %}
  </div>

  <div class="row" style="justify-content:space-between;margin-top:22px"><h2 style="margin:0">Content Calendar &amp; Queue</h2><span class="chip">{{ch.name}} only</span></div>
  <div class="card" style="margin-top:12px">
    {% if calendar %}<table class="analytics-table"><tr><th>Publish time (UTC)</th><th>Type</th><th>Video</th><th>Status</th></tr>
    {% for row in calendar %}<tr><td>{{row.publish_at[:16].replace('T',' ')}}</td><td>{{row.content_type}}</td><td>{{row.title}}</td><td>{{row.status}}</td></tr>{% endfor %}</table>
    {% else %}<div class="muted">No scheduled videos in this channel queue yet.</div>{% endif %}
  </div>

  </section>
  <section class="dash-section" id="section-create">
  <div class="section-head"><h2>Create Videos</h2><div class="muted">Every action below applies to {{ch.name}} only</div></div>

  <div class="card" style="margin-bottom:16px;border-left:4px solid var(--ok)">
    <div class="row" style="justify-content:space-between">
      <div><strong>Timed YouTube Upload Queue</strong><div class="hint"><strong>{{upload_queue.count}}</strong> video(s) waiting. Public releases stay {{upload_queue.gap_hours|round(0)|int}} hours apart. Next queue check: {{upload_queue.next_at[:16].replace('T',' ')}} UTC.</div><div class="hint"><strong>{{ch.name}} upload budget today:</strong> {{channel_uploads_today}} / {{channel_upload_limit}} for this channel. Manual Upload Now remains available.</div>{% if upload_pause %}<div class="hint" style="color:#ffd27a"><strong>Queue is ON but waiting for YouTube quota:</strong> 0% — resumes {{upload_pause[1]|localdt}}. Video creation remains active.</div>{% endif %}</div>
      <div class="row">
        <form method="post" action="/queue-mode"><input type="hidden" name="channel" value="{{ch.id}}"><label class="chk"><input type="checkbox" name="enabled" {{'checked' if upload_queue.enabled else ''}} onchange="this.form.submit()"> {{'Queue ON' if upload_queue.enabled else 'Queue PAUSED'}}</label></form>
      </div>
    </div>
  </div>
  <div class="workspace-title">Create videos for {{ch.name}}</div>
  <div class="action-grid">
    <div class="card action-card">
      <div class="action-head"><h2>Short Videos</h2><span class="scope">{{ch.name}} only</span></div>
      <form method="post" action="/generate">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <label class="field">Choose topic
          <select name="topic" required style="max-width:none;width:100%">
            <option value="" disabled selected>Select from {{stats.topics}} topics...</option>
            {% for k,t in topics %}<option value="{{k}}">{{t}}</option>{% endfor %}
          </select>
        </label>
        <div class="row" style="margin-top:12px">
          <label class="chk"><input type="checkbox" name="upload" checked> Add to timed queue</label>
          <button type="submit">Create One Short</button>
        </div>
      </form>
      <div class="row" style="margin-top:14px">
        <form method="post" action="/daily">
          <input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="upload" value="on">
          <button class="pink" type="submit">Daily Shorts ({{ch.topics_per_day}})</button>
        </form>
        <form method="post" action="/batch" class="row">
          <input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="upload" value="on">
          <input type="number" name="count" value="3" min="1" max="50" style="width:70px">
          <button class="ghost" type="submit">Create Batch</button>
        </form>
        <form method="post" action="/retry">
          <input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="content_type" value="short"><button class="ghost" type="submit">Retry Shorts</button>
        </form>
      </div>
      {% if not ch.builtin %}
      <div class="row" style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">
        <form method="post" action="/gen-topics" class="row">
          <input type="hidden" name="channel" value="{{ch.id}}">
          <input type="number" name="count" value="10" min="1" max="30" style="width:70px" title="topics">
          <input type="hidden" name="scenes" value="5"><button class="ghost" type="submit">Add Short Topics</button>
        </form>
      </div>
      {% endif %}
      <div class="action-note">Creates vertical Shorts for {{ch.name}} only. Finished videos enter Unuploaded Videos and the shared timed queue.</div>
    </div>

    <div class="card action-card long">
      <div class="action-head"><h2>Long Videos (~5 min)</h2><span class="scope">{{ch.name}} only</span></div>

      <form method="post" action="/long-topic">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <label class="field">Choose topic
          <select name="topic" required style="max-width:none;width:100%">
            <option value="" disabled selected>Select from {{stats.topics}} topics...</option>
            {% for key,title in topics %}<option value="{{key}}">{{title}}</option>{% endfor %}
          </select>
        </label>
        <div class="row" style="margin-top:12px">
          <label class="chk"><input type="checkbox" name="upload" checked onchange="syncQueue(this)"> Upload to YouTube</label>
          <label class="chk"><input type="checkbox" name="queue" checked> Add to timed queue</label>
          <button class="pink" type="submit">Create One Long</button>
        </div>
      </form>

      <div class="row" style="margin-top:14px">
        <form method="post" action="/long-auto" class="row">
          <input type="hidden" name="channel" value="{{ch.id}}">
          <label class="chk"><input type="checkbox" name="upload" checked onchange="syncQueue(this)"> Upload to YouTube</label>
          <label class="chk"><input type="checkbox" name="queue" checked> Add to timed queue</label>
          <button class="green" type="submit">Auto Long</button>
        </form>
        <form method="post" action="/long-batch" class="row">
          <input type="hidden" name="channel" value="{{ch.id}}">
          <input type="number" name="count" value="2" min="1" max="20" style="width:70px" title="long videos">
          <label class="chk"><input type="checkbox" name="upload" checked onchange="syncQueue(this)"> Upload to YouTube</label>
          <label class="chk"><input type="checkbox" name="queue" checked> Add to timed queue</label>
          <button class="ghost" type="submit">Create Batch</button>
        </form>
        <form method="post" action="/retry">
          <input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="content_type" value="long">
          <button class="ghost" type="submit">Retry Long</button>
        </form>
      </div>

      <div style="margin:16px 0;border-top:1px solid var(--line)"></div>
      <form method="post" action="/long-custom">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <label class="field">New long-video topic
          <input type="text" name="prompt" placeholder="e.g. A complete story about a brave little lion" required>
        </label>
        <div class="row" style="margin-top:12px">
          <label class="chk"><input type="checkbox" name="upload" checked onchange="syncQueue(this)"> Upload to YouTube</label>
          <label class="chk"><input type="checkbox" name="queue" checked> Add to timed queue</label>
          <button class="pink" type="submit">Create From My Topic</button>
        </div>
      </form>
      <div class="action-note">Create as many long videos as needed for {{ch.name}}. <b>Upload to YouTube</b> off = video stays in the dashboard only (no upload). On + <b>timed queue</b> = uploads one-by-one with the shared 3-hour gap; on without the queue = uploads right away.</div>
    </div>

    <details class="card action-card buffer full advanced-card">
      <summary>Offline Buffer <span class="scope">{{ch.name}} only</span></summary><div class="advanced-body">
      <form method="post" action="/offline-buffer" onsubmit="return confirm('Prepare this offline buffer for {{ch.name}} only? Keep the PC online until it finishes.')">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <div class="row">
          <label class="field" style="max-width:130px">Days offline (1-14)
            <input type="number" name="days" value="3" min="1" max="14">
          </label>
          <label class="field">Video type
            <select name="content_mode" style="max-width:none;width:100%">
              <option value="both" selected>Shorts + Long videos</option>
              <option value="long">Long videos only</option>
              <option value="short">Shorts only</option>
            </select>
          </label>
        </div>
        <label class="field" style="margin-top:12px">Optional content guideline
          <input type="text" name="guideline" placeholder="e.g. Focus on animal stories and do not repeat topics">
        </label>
        <button class="green" type="submit" style="margin-top:12px">Prepare {{ch.name}} Offline Buffer</button>
      </form>
      <div class="action-note">Only {{ch.name}} videos are prepared. Keep this PC online until the job finishes; afterward YouTube publishes the private queue automatically every 3 hours.</div>
      </div>
    </details>

    <details class="card action-card custom full advanced-card">
      <summary>Custom Short <span class="scope">{{ch.name}} only</span></summary><div class="advanced-body">
      <form method="post" action="/custom" class="row">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <input type="text" name="prompt" placeholder="Type your short-video idea..." required style="flex:1;min-width:260px">
        <select name="scenes" style="max-width:160px"><option value="5">~30 sec</option><option value="6" selected>6 scenes</option><option value="8">8 scenes</option><option value="12">12 scenes</option></select>
        <label class="chk"><input type="checkbox" name="upload" checked> Add to queue</label>
        <button type="submit">Create Custom Short</button>
      </form>
      </div>
    </details>
  </div>

  <div class="row" style="justify-content:space-between;margin-top:18px">
    <h2 style="margin:0">Live Creation Status</h2>
    <span class="scope">{{ch.name}} only</span>
  </div>
  <div class="card jobs-box" data-job-scope="{{ch.id}}" style="margin-top:12px">
    <div class="muted jobs-empty">No creation jobs yet.</div>
  </div>

  </section>
  <section class="dash-section" id="section-system">
  <div class="section-head"><h2>System &amp; Storage</h2><div class="muted">Images, API limits, backup and background jobs</div></div>

  <h2>Publishing Platforms</h2>
  <div class="card">
    <div class="row" style="justify-content:space-between"><div><strong>Cross-platform publishing</strong><div class="hint">Settings below apply to {{ch.name}} only. Secrets stay on this PC and are never shown again.</div></div><span class="scope">{{ch.name}} only</span></div>
    <div class="platform-tabs">
      <button type="button" data-platform="youtube" class="active">YouTube</button>
      <button type="button" data-platform="facebook">Facebook Page</button>
      <button type="button" data-platform="tiktok">TikTok</button>
    </div>

    <div class="platform-panel active" id="platform-youtube">
      <div class="row" style="justify-content:space-between"><div><strong>YouTube</strong> <span class="chip">{{'connected' if connected else 'not connected'}}</span><div class="hint">Existing YouTube connection, scheduling and 3-hour safety gap remain unchanged.</div></div><a href="{{social.guides.youtube}}" target="_blank"><button class="ghost sm" type="button">Open YouTube API Guide</button></a></div>
    </div>

    <div class="platform-panel" id="platform-facebook">
      <div class="row" style="justify-content:space-between"><div><strong>Facebook Page</strong> <span class="chip">{{'configured' if social.facebook.configured else 'setup required'}}</span><div class="hint">Shorts publish as Page Reels; long videos publish as Page videos.</div></div><a href="{{social.guides.facebook_app}}" target="_blank"><button class="ghost sm" type="button">Create Meta App</button></a></div>
      <form method="post" action="/social-settings" style="margin-top:14px">
        <input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="platform" value="facebook">
        <div class="platform-grid">
          <label class="field">Facebook Page ID<input type="text" name="page_id" value="{{social.facebook.page_id}}" placeholder="e.g. 123456789012345"></label>
          <label class="field">Page Access Token<input type="password" name="page_access_token" placeholder="{{'Saved - leave blank to keep it' if social.facebook.configured else 'Paste Page Access Token'}}"></label>
        </div>
        <div class="row" style="margin-top:12px"><label class="chk"><input type="checkbox" name="auto_upload" {{'checked' if social.facebook.auto_upload else ''}}> Automatically publish new videos</label><button class="green sm" type="submit">Save Facebook Settings</button></div>
      </form>
      <form method="post" action="/social-verify" style="margin-top:10px"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="platform" value="facebook"><button class="ghost sm" type="submit">Connect &amp; Verify Facebook Page</button></form>
      <details class="guide-box"><summary>Facebook setup guide + official URLs</summary><ol><li>Open <a href="{{social.guides.facebook_app}}" target="_blank">Meta for Developers Apps</a> and create a Business app.</li><li>Add Facebook Login/Pages access and request <strong>pages_manage_posts</strong> plus Page access permissions.</li><li>Get the Page ID and Page Access Token for the Page you manage.</li><li>Paste both above, Save, then click Connect &amp; Verify.</li><li>Official Reel upload reference: <a href="{{social.guides.facebook}}" target="_blank">Meta Reels Publishing API</a>.</li></ol></details>
    </div>

    <div class="platform-panel" id="platform-tiktok">
      <div class="row" style="justify-content:space-between"><div><strong>TikTok</strong> <span class="chip">{{'configured' if social.tiktok.configured else 'setup required'}}</span><div class="hint">Safe default sends videos to TikTok drafts. Public Direct Post needs TikTok audit and per-post consent.</div></div><a href="{{social.guides.tiktok_app}}" target="_blank"><button class="ghost sm" type="button">Create TikTok App</button></a></div>
      <form method="post" action="/social-settings" style="margin-top:14px">
        <input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="platform" value="tiktok">
        <div class="platform-grid">
          <label class="field">Client Key<input type="text" name="client_key" value="{{social.tiktok.client_key}}" placeholder="TikTok Client Key"></label>
          <label class="field">Client Secret<input type="password" name="client_secret" placeholder="Saved securely - leave blank to keep"></label>
          <label class="field">Access Token<input type="password" name="access_token" placeholder="{{'Saved - leave blank to keep it' if social.tiktok.configured else 'Paste user Access Token'}}"></label>
          <label class="field">Refresh Token<input type="password" name="refresh_token" placeholder="Optional but recommended"></label>
          <label class="field">Posting mode<select name="mode" style="width:100%;max-width:none"><option value="draft" {{'selected' if social.tiktok.mode=='draft' else ''}}>Send to TikTok drafts (recommended)</option><option value="direct" {{'selected' if social.tiktok.mode=='direct' else ''}}>Direct Post (audited app only)</option></select></label>
          <label class="field">Direct Post privacy<select name="privacy" style="width:100%;max-width:none"><option value="SELF_ONLY" {{'selected' if social.tiktok.privacy=='SELF_ONLY' else ''}}>Private / Self only</option><option value="PUBLIC_TO_EVERYONE" {{'selected' if social.tiktok.privacy=='PUBLIC_TO_EVERYONE' else ''}}>Public</option><option value="MUTUAL_FOLLOW_FRIENDS" {{'selected' if social.tiktok.privacy=='MUTUAL_FOLLOW_FRIENDS' else ''}}>Friends</option></select></label>
        </div>
        <div class="row" style="margin-top:12px"><label class="chk"><input type="checkbox" name="auto_upload" {{'checked' if social.tiktok.auto_upload else ''}}> Automatically send new videos to drafts</label><button class="green sm" type="submit">Save TikTok Settings</button></div>
      </form>
      <form method="post" action="/social-verify" style="margin-top:10px"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="platform" value="tiktok"><button class="ghost sm" type="submit">Connect &amp; Verify TikTok</button></form>
      <details class="guide-box"><summary>TikTok setup guide + official URLs</summary><ol><li>Open <a href="{{social.guides.tiktok_app}}" target="_blank">TikTok Developer Apps</a> and create an app.</li><li>Add the Content Posting API product.</li><li>Request <strong>video.upload</strong> for drafts; add <strong>video.publish</strong> only for Direct Post. Include <strong>user.info.basic</strong> for verification.</li><li>Authorize your TikTok account and paste the returned tokens above.</li><li>Save, then click Connect &amp; Verify. Full official guide: <a href="{{social.guides.tiktok}}" target="_blank">TikTok Content Posting API</a>.</li><li>Unaudited apps can only Direct Post privately; use drafts until TikTok approves the app.</li></ol></details>
    </div>
  </div>
  {% if assets %}
  <h2>🖼 Asset Coverage</h2>
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div>Images ready: <strong>{{assets.existing}}</strong> / {{assets.total}} &nbsp; Missing: <strong style="color:var(--warn)">{{assets.missing}}</strong></div>
    </div>
    <div class="bar"><i style="width:{{ (100*assets.existing/assets.total)|round(0,'floor') if assets.total else 0 }}%"></i></div>
    <div class="row" style="margin-top:8px">
      <form method="post" action="/asset-missing" class="row"><input type="hidden" name="channel" value="{{ch.id}}"><input type="number" name="count" value="10" min="1" max="50" style="width:76px"><button class="ghost sm" type="submit">Generate missing (AI/free)</button></form>
      <form method="post" action="/asset-upgrade" class="row"><input type="hidden" name="channel" value="{{ch.id}}"><input type="number" name="count" value="30" min="1" max="200" style="width:76px"><button class="green sm" type="submit">⬆ Upgrade low-quality images</button></form>
    </div>
  </div>
  {% endif %}

  <h2>API &amp; Quota Center</h2>
  <div class="card">
    <div class="row">
      <span class="chip">YouTube: {{'paused' if api.youtube_pause else 'ready'}}</span>
      <span class="chip">Gemini: {{'ready' if api.gemini else 'not configured'}}</span>
      <span class="chip">OpenAI: {{'ready' if api.openai else 'not configured'}}</span>
      <span class="chip">Pollinations: {{'ready' if api.pollinations else 'off'}}</span>
      <span class="chip">Openverse: ready</span><span class="chip">Wikimedia: ready</span><span class="chip">Notifications: {{'ready' if api.notifications else 'optional setup'}}</span>
    </div>
    <div class="hint" style="margin-top:12px">Paid OpenAI images today: <strong>{{api.openai_images_used}} / {{api.openai_images_limit}}</strong>. When OpenAI billing/quota is unavailable, the next free source is selected automatically.</div>
    <div class="row" style="margin-top:10px"><strong>Image fallback order:</strong><span class="chip">1. OpenAI</span><span class="chip">2. Openverse (free)</span><span class="chip">3. Wikimedia (free)</span><span class="chip">Kids cartoon fallback: Pollinations</span></div>
    <div class="card" style="margin-top:12px;padding:14px">
      <div class="row" style="justify-content:space-between"><div><strong>Free AI images</strong> <span class="chip">{{'key connected' if api.pollinations_key else 'limited without key'}}</span><div class="hint">Add a free Pollinations key to reduce anonymous HTTP 429 errors. No text-board placeholder is used when a real image cannot be found.</div></div><a href="https://enter.pollinations.ai" target="_blank"><button class="ghost sm" type="button">Get Free Key</button></a></div>
      <form method="post" action="/pollinations-key" class="row" style="margin-top:10px"><input type="hidden" name="channel" value="{{ch.id}}"><input type="password" name="api_key" placeholder="{{'Key saved - paste a new key to replace' if api.pollinations_key else 'Paste pk_ or sk_ key'}}" style="flex:1;min-width:240px"><button class="green sm" type="submit">Save Image Key</button></form>
    </div>
    <div class="card" style="margin-top:12px;padding:14px">
      <div class="row" style="justify-content:space-between">
        <div><strong>Thumbnails</strong>
          <span class="chip">{{'custom thumbnails ON' if api.thumbnails_allowed else 'blocked by YouTube'}}</span>
          <span class="chip">{{'Pexels connected (free)' if api.pexels_key else 'Pexels key not set'}}</span>
          <div class="hint">Every new video gets its own 16:9 thumbnail about its topic, with the title on it. Free Pexels stock photos are used first, then AI.
          {% if not api.thumbnails_allowed %}YouTube refused custom thumbnails for {{ch.name}} — verify that channel at <a href="https://youtube.com/verify" target="_blank">youtube.com/verify</a>, then press "Thumbnails Are Verified".{% endif %}</div>
        </div>
      </div>
      <div class="row" style="margin-top:10px">
        <form method="post" action="/fix-thumbnails"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="limit" value="10"><button class="green sm" type="submit">🖼 Fix Thumbnails On YouTube (10)</button></form>
        {% if not api.thumbnails_allowed %}<form method="post" action="/enable-thumbnails"><input type="hidden" name="channel" value="{{ch.id}}"><button class="ghost sm" type="submit">✅ Thumbnails Are Verified</button></form>{% endif %}
        <a href="https://www.pexels.com/api/" target="_blank"><button class="ghost sm" type="button">Get Free Pexels Key</button></a>
      </div>
      <form method="post" action="/pexels-key" class="row" style="margin-top:10px"><input type="hidden" name="channel" value="{{ch.id}}"><input type="password" name="api_key" placeholder="{{'Key saved - paste a new key to replace' if api.pexels_key else 'Paste your free Pexels API key'}}" style="flex:1;min-width:240px"><button class="green sm" type="submit">Save Pexels Key</button></form>
    </div>
    {% if api.youtube_pause %}<div class="hint">YouTube retry after: {{api.youtube_pause[1]}}</div>{% endif %}
  </div>

  <h2>💾 Storage (keep the app small)</h2>
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div class="muted">Local: <strong>runs {{storage.runs_mb}} MB</strong> + <strong>assets {{storage.assets_mb}} MB</strong>
        &nbsp;•&nbsp; Google Drive: {% if storage.drive %}<span style="color:var(--ok)">● connected</span>{% else %}<span style="color:var(--warn)">● not connected</span>{% endif %}</div>
      <div class="row">
        <form method="post" action="/drive-connect"><input type="hidden" name="channel" value="{{ch.id}}"><button class="ghost sm" type="submit">🔗 Connect Google Drive</button></form>
        <form method="post" action="/drive-backup" onsubmit="return confirm('Upload local videos to Drive and delete the local copies?')"><input type="hidden" name="channel" value="{{ch.id}}"><button class="green sm" type="submit">☁ Backup videos to Drive &amp; free space</button></form>
      </div>
    </div>
    <div class="hint">Final videos move to your Drive folder and the local copies are removed, so the app stays small. Connect Drive once first.</div>
  </div>

  <h2>⚙️ Jobs</h2>
  <div class="card jobs-box" data-job-scope="all"><div class="muted jobs-empty">No jobs yet.</div></div>

  </section>
  <section class="dash-section" id="section-library">
  <div class="section-head"><h2>Video Library</h2><div class="muted">Preview, upload, retry or remove generated videos</div></div>

  <div class="row" style="justify-content:space-between;margin-top:22px">
    <h2 style="margin:0">Videos ? {{ch.name}}</h2>
    <div class="row">
      <a class="tab {{'active' if selected_view=='all' and selected_type=='all' else ''}}" href="/?channel={{ch.id}}&amp;type=all&amp;view=all">All</a>
      <a class="tab {{'active' if selected_view=='unuploaded' else ''}}" href="/?channel={{ch.id}}&amp;type=all&amp;view=unuploaded">Unuploaded ({{upload_queue.unuploaded_count}})</a>
      <a class="tab {{'active' if selected_view=='all' and selected_type=='short' else ''}}" href="/?channel={{ch.id}}&amp;type=short&amp;view=all">Shorts</a>
      <a class="tab {{'active' if selected_view=='all' and selected_type=='long' else ''}}" href="/?channel={{ch.id}}&amp;type=long&amp;view=all">Long Videos</a>
    </div>
  </div>
  <div class="grid vids" style="margin-top:12px">
    {% for v in videos %}
    {% set vs=social_uploads.get(v['id'], {}) %}
    <div class="card vid">
      {% if v['drive_url'] %}
        <div class="card" style="text-align:center;padding:28px"><a href="{{v['drive_url']}}" target="_blank">☁ On Google Drive ↗</a></div>
      {% elif v['video_path'] %}<video controls preload="metadata" src="/media?path={{v['video_path']|urlencode}}"></video>{% endif %}
      <div class="t">{{v['title']}} <span class="chip">{{v['content_type']}}</span></div>
      <div class="hint">Created: {{v['created_at']|localdt}}</div>
      <div class="row" style="justify-content:space-between;margin:2px 0 8px">
        <span class="st {{v['status']}}" data-video-status="{{v['id']}}">{{v['status']}}</span>
        {% if v['video_url'] %}<a href="{{v['video_url']}}" target="_blank">YouTube ↗</a>{% endif %}
      </div>
      <div class="upload-meter" data-video-progress="{{v['id']}}" style="display:{{'block' if v['status']=='uploading' else 'none'}};margin:8px 0">
        <div class="row" style="justify-content:space-between"><span class="hint">Uploading to YouTube</span><strong class="upload-percent">{{v['upload_progress']}}%</strong></div>
        <div class="bar" style="margin-top:5px"><i style="width:{{v['upload_progress']}}%"></i></div>
      </div>
      {% if upload_pause and not v['video_url'] and v['status'] in ('rendered','upload_failed','daily_upload_pending','queued_for_upload') %}
      <div style="margin:8px 0;padding:9px 10px;border:1px solid #705b20;border-radius:9px;background:#302912">
        <div class="row" style="justify-content:space-between"><span class="hint">Waiting for YouTube quota reset</span><strong>0%</strong></div>
        <div class="bar" style="margin-top:5px"><i style="width:0%"></i></div>
        <div class="hint">Automatic retry: {{upload_pause[1]|localdt}}. Video creation continues while uploads wait.</div>
      </div>
      {% endif %}
      {% if v['status']=='queued_for_upload' and queue_times.get(v['id']) %}<div class="hint" style="margin:6px 0;color:#9fc4ff"><strong>Expected YouTube upload:</strong> {{queue_times.get(v['id'])|localdt}}</div>{% elif v['status']=='daily_upload_pending' %}<div class="hint" style="margin:6px 0;color:#9fc4ff"><strong>Daily direct upload:</strong> {% if upload_pause %}YouTube quota paused. Automatic retry: {{upload_pause[1]|localdt}}.{% else %}Ready; automatic upload retry will start shortly.{% endif %}</div>{% endif %}      {% if vs %}<div class="social-state">{% for platform,item in vs.items() %}<span class="chip {{item.status}}">{{platform|title}}: {{item.status}}</span>{% if item.post_url %}<a href="{{item.post_url}}" target="_blank">Open</a>{% endif %}{% endfor %}</div>{% endif %}      {% if v['status']=='scheduled' and v['publish_at'] %}<div class="hint"><strong>Goes public on YouTube:</strong> {{v['publish_at']|localdt}}</div>{% endif %}
      <div class="row">
        <a href="/media?path={{v['video_path']|urlencode}}" download><button class="ghost sm" type="button">Download</button></a>
        {% if upload_pause and not v['video_url'] and v['status'] in ('rendered','upload_failed','daily_upload_pending','queued_for_upload') %}<button class="ghost sm" type="button" disabled>Waiting for YouTube quota</button>{% elif v['status']=='daily_upload_pending' %}<form method="post" action="/upload-now"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><input type="hidden" name="type" value="{{selected_type}}"><button class="green sm" type="submit">Upload Now</button></form>{% elif v['status'] in ('awaiting_approval','rendered','upload_failed') %}<form method="post" action="/upload-now"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><input type="hidden" name="type" value="{{selected_type}}"><button class="green sm" type="submit">Upload to YouTube</button></form><form method="post" action="/upload-one"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><button class="ghost sm" type="submit">Add to Timed Queue</button></form>{% elif v['status']=='thumbnail_pending' %}<form method="post" action="/upload-one"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><button class="green sm" type="submit">Retry Thumbnail</button></form>{% endif %}
        {% if v['status'] in ('quality_failed','awaiting_approval','rendered','thumbnail_pending','uploaded','scheduled') %}<form method="post" action="/regenerate-one"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><button class="ghost sm" type="submit">Regenerate</button></form>{% endif %}
        {% if social.facebook.configured and not (vs.get('facebook') and vs.get('facebook').status=='submitted') %}<form method="post" action="/social-upload"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><input type="hidden" name="platform" value="facebook"><input type="hidden" name="type" value="{{selected_type}}"><input type="hidden" name="view" value="{{selected_view}}"><button class="ghost sm" type="submit">Share Facebook</button></form>{% endif %}
        {% if social.tiktok.configured and not (vs.get('tiktok') and vs.get('tiktok').status=='submitted') %}<form method="post" action="/social-upload" onsubmit="return confirm('Send this video to TikTok? Direct Post mode confirms your consent for this upload.')"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><input type="hidden" name="platform" value="tiktok"><input type="hidden" name="consent" value="on"><input type="hidden" name="type" value="{{selected_type}}"><input type="hidden" name="view" value="{{selected_view}}"><button class="ghost sm" type="submit">Send TikTok</button></form>{% endif %}        <form method="post" action="/delete-one" onsubmit="return confirm('Delete this video?')"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><button class="red sm" type="submit">Delete</button></form>
      </div>
      {% if v['error'] %}<div class="err">{% if 'quotaexceeded' in v['error']|lower %}YouTube daily quota is finished. The video is already online; its thumbnail will retry after quota reset.{% else %}{{v['error'][:180]}}{% endif %}</div>{% endif %}
    </div>
    {% else %}<div class="muted">No videos yet for {{ch.name}}.</div>{% endfor %}
  </div>
  </section>
</div>
<script>
// Long-video upload controls: when "Upload to YouTube" is off, the video is
// only rendered into the dashboard (no upload), so the timed-queue choice no
// longer applies — grey it out. When on again, restore the queue default.
function syncQueue(cb){
  var q = cb.form ? cb.form.querySelector('input[name=queue]') : null;
  if(!q) return;
  q.disabled = !cb.checked;
  q.checked = cb.checked;
}
document.querySelectorAll('input[name=upload]').forEach(function(cb){
  if(cb.form && cb.form.querySelector('input[name=queue]')) syncQueue(cb);
});
function showPlatform(name){
  const valid=['youtube','facebook','tiktok'];
  if(!valid.includes(name)) name='youtube';
  document.querySelectorAll('.platform-panel').forEach(x=>x.classList.toggle('active',x.id==='platform-'+name));
  document.querySelectorAll('.platform-tabs button').forEach(x=>x.classList.toggle('active',x.dataset.platform===name));
  localStorage.setItem('storybot-platform',name);
}
document.querySelectorAll('.platform-tabs button').forEach(x=>x.addEventListener('click',()=>showPlatform(x.dataset.platform)));
showPlatform(localStorage.getItem('storybot-platform')||'youtube');function showSection(name){
  const valid=['overview','create','analytics','library','system'];
  if(!valid.includes(name)) name='overview';
  document.querySelectorAll('.dash-section').forEach(x=>x.classList.toggle('active',x.id==='section-'+name));
  document.querySelectorAll('.section-nav button').forEach(x=>x.classList.toggle('active',x.dataset.section===name));
  localStorage.setItem('storybot-section',name);
  window.scrollTo({top:0,behavior:'instant'});
}
document.querySelectorAll('.section-nav button').forEach(x=>x.addEventListener('click',()=>showSection(x.dataset.section)));
showSection(localStorage.getItem('storybot-section')||'overview');
async function poll(){
  try{
    const r=await fetch('/status'); const jobs=await r.json();
    const keys=Object.keys(jobs).reverse(); let running=false;
    document.querySelectorAll('.jobs-box').forEach(box=>{
      const scope=box.dataset.jobScope||'all';
      const scoped=keys.filter(k=>scope==='all'||String(jobs[k].label||'').startsWith('['+scope+']'));
      if(!scoped.length){ box.innerHTML='<div class="muted jobs-empty">No creation jobs yet.</div>'; return; }
      box.innerHTML=scoped.map(k=>{const j=jobs[k];
        if(j.status==='running'||j.status==='queued') running=true;
        let detail=String(j.detail||'');
        const d=detail?' — <span class="muted">'+detail.slice(0,260)+'</span>':'';
        return '<div class="job"><div>'+j.label+(j.created_at?' <span class="muted">• '+j.created_at+'</span>':'')+d+'</div><span class="badge '+j.status+'">'+j.status+'</span></div>';}).join('');
    });
    const pr=await fetch('/video-progress?channel={{ch.id}}');
    const videos=await pr.json();
    Object.keys(videos).forEach(id=>{
      const item=videos[id], meter=document.querySelector('[data-video-progress="'+id+'"]');
      if(meter){
        const active=item.status==='uploading';
        meter.style.display=active?'block':'none';
        const pct=meter.querySelector('.upload-percent'), fill=meter.querySelector('.bar i');
        if(pct) pct.textContent=item.progress+'%';
        if(fill) fill.style.width=item.progress+'%';
      }
      const badge=document.querySelector('[data-video-status="'+id+'"]');
      if(badge && badge.textContent.trim()!==item.status){ badge.textContent=item.status; badge.className='st '+item.status; }
    });
    if(!running && window.__was){ window.__was=false; setTimeout(()=>location.reload(),900); }
    if(running) window.__was=true;
  }catch(e){}
}
setInterval(poll,1500); poll();
</script>
</body></html>
"""


def _online() -> bool:
    import socket
    try:
        socket.create_connection(("www.google.com", 80), timeout=4).close()
        return True
    except OSError:
        return False


def _daily_automation_check():
    """Create missing valid daily videos; uploads may wait for YouTube quota."""
    try:
        from src.daily_runner import run_daily_catchup, run_daily_long_all
        from src.channels import get_channel, load_channels
        from src.pending_uploads import has_daily_pending, retry_daily_uploads

        active = any(
            job.get("status") in {"queued", "running"}
            and ("Auto daily" in job.get("label", "") or "generation" in job.get("label", "").lower())
            for job in JOBS.values()
        )
        connected_channels = [channel for channel in load_channels() if channel.token_path.exists()]
        shorts_missing = any(
            short_videos_generated_today_count(channel.id) < 3
            for channel in connected_channels
        )
        long_missing = any(
            long_videos_generated_today_count(channel.id) < 1
            for channel in connected_channels
        )
        daily_uploads_waiting = has_daily_pending()
        if not active and (daily_uploads_waiting or shorts_missing or long_missing):
            online = _online()

            def job():
                messages: list[str] = []
                if online and has_daily_pending():
                    messages.extend(f"direct-upload:{item}" for item in retry_daily_uploads(limit=20))
                elif has_daily_pending():
                    messages.append("Direct daily uploads are waiting for internet")
                for channel in [item for item in load_channels() if item.token_path.exists()]:
                    if short_videos_generated_today_count(channel.id) < 3:
                        short_results = run_daily_catchup(
                            target_count=3, upload=online, start_hour=0,
                            channel=channel, queue=False,
                        )
                        messages.extend(
                            f"short:{channel.id}:{topic}:{status}"
                            for topic, status in short_results
                        )
                if any(
                    long_videos_generated_today_count(channel.id) < 1
                    for channel in [item for item in load_channels() if item.token_path.exists()]
                ):
                    long_results = run_daily_long_all(upload=online, scenes=20)
                    messages.extend(f"long:{channel_id}:{status}" for channel_id, status in long_results)
                return "; ".join(messages) or "Daily targets already complete"

            start_job(f"Auto daily creation{' + upload' if online else ' (offline render only)'}", job)
    except Exception as exc:
        print("daily automation check skipped:", exc)
    finally:
        timer = threading.Timer(2 * 60, _daily_automation_check)
        timer.daemon = True
        timer.start()

def _port_is_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


if __name__ == "__main__":
    import webbrowser
    # Starting a second copy used to fail in a way that looked like the app had
    # opened, while the browser actually showed the OLD instance still holding
    # the port. Say so plainly instead.
    if not _port_is_free(PORT):
        print(f"Port {PORT} is already in use — Story Bot Studio is probably already running.")
        print(f"Open http://127.0.0.1:{PORT} , close the other window first, or")
        print(f"start this copy on another port:  set DASHBOARD_PORT=8010")
        input("Press Enter to close...")
        raise SystemExit(1)
    print(f"Story Bot STUDIO: http://127.0.0.1:{PORT}  (close this window to stop)")
    _schedule_history_cleanup()
    queue_timer = threading.Timer(8.0, _schedule_auto_upload_queue)
    queue_timer.daemon = True
    queue_timer.start()
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    # Check shortly after launch and then hourly. The checker only creates
    # missing daily targets, so restarts never duplicate completed videos.
    daily_timer = threading.Timer(5.0, _daily_automation_check)
    daily_timer.daemon = True
    daily_timer.start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
