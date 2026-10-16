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

from src.config import dashboard_automation_on, set_dashboard_automation, settings
from src.channels import load_channels, get_channel
from src import channel_factory, setup_guide, slots
from src.language import LANGUAGES
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
from src.db import active_upload_backoff, api_usage_today, approval_mode_enabled, cleanup_uploaded_videos, delete_video, set_approval_mode, set_drive_url, social_uploads_for_channel
from src import drive_storage, youtube_accounts, youtube_analytics, social_accounts, social_publish, upload_queue
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


def channel_rows(channel_id: str, content_type: str | None = None, view: str = "all",
                 account_id: str | None = None) -> list[sqlite3.Row]:
    if not settings.db_path.exists():
        return []
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        where = "channel = ? AND hidden_at IS NULL"
        params: list[object] = [channel_id]
        # With several YouTube channels connected, show one at a time.
        if account_id:
            where += " AND COALESCE(account, 'main') = ?"
            params.append(account_id)
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
                   COALESCE(upload_progress, 0) AS upload_progress,
                   COALESCE(account, 'main') AS account
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
        if dashboard_automation_on() and not active and upload_queue.due_video_id() is not None:
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
    # Google returns here with ?code=…&state=… after a channel is picked.
    if request.args.get("state") and (request.args.get("code") or request.args.get("error")):
        return _finish_oauth()
    channels = load_channels()
    ch = current_channel()
    topics = load_topics_for(ch)
    selected_type = request.args.get("type", "all")
    if selected_type not in {"all", "short", "long"}:
        selected_type = "all"
    selected_view = request.args.get("view", "all")
    if selected_view not in {"all", "unuploaded"}:
        selected_view = "all"
    account_ids = [a.id for a in youtube_accounts.list_accounts(ch.id)]
    selected_account = request.args.get("account", "all")
    if selected_account not in account_ids:
        selected_account = "all"
    vids = channel_rows(
        ch.id, None if selected_type == "all" else selected_type, selected_view,
        None if selected_account == "all" else selected_account,
    )
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
        selected_type=selected_type, selected_view=selected_view,
        selected_account=selected_account, analytics=analytics, approval_mode=approval_mode_enabled(ch.id),
        upload_queue=upload_queue.queue_snapshot(ch.id),
        accounts=[
            {
                "id": a.id, "name": a.label, "connected": a.connected,
                "email": youtube_accounts.account_email(ch.id, a.id),
                "used_today": uploads_today_count(ch.id, a.id),
                "limit": upload_limit_for_channel(ch.id, a.id),
            }
            for a in youtube_accounts.list_accounts(ch.id)
        ],
        channel_uploads_today=uploads_today_count(ch.id), channel_upload_limit=upload_limit_for_channel(ch.id),
        api=api_center(ch), calendar=upcoming_schedule(ch.id),
        setup=channel_factory.setup_status(ch), factory=ch.created_by == "factory",
        slot_now=slots.channel_slot(ch), slot_options=[
            (i, slots.label(i), len(members)) for i, members in slots.load().items()
        ],
        quick_links=setup_guide.quick_links(channel_factory.setup_status(ch).get("project_id", "")),
        quick_steps=setup_guide.QUICK_STEPS,
        branding=setup_guide.BRANDING, languages=LANGUAGES, automation_on=dashboard_automation_on(),
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


# The timed upload queue was removed: rendered videos upload straight away and
# YouTube holds each one private until its scheduled publish time. /queue-mode
# and /queue-one are gone with it.


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

    def job():
        results = run_long_batch(ch, count=1, upload=upload, scenes=20, queue=False)
        return _long_job_summary(results)

    start_job(f"[{ch.id}] Auto 5-minute long video", job)
    flash(f"Auto-creating a ~5-minute long video for {ch.name}...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/long-custom", methods=["POST"])
def long_custom():
    ch = current_channel()
    prompt = (request.form.get("prompt") or "").strip()
    upload = request.form.get("upload") == "on"
    if not prompt:
        flash("Type a topic for the new long video.", "error")
        return redirect(url_for("home", channel=ch.id))

    def job():
        results = run_long_batch(
            ch, count=1, upload=upload, prompts=[prompt], scenes=20, queue=False,
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
    topics = load_topics_for(ch)
    if topic_key not in topics:
        flash("Choose a valid topic for the long video.", "error")
        return redirect(url_for("home", channel=ch.id))
    title = topics[topic_key].get("title", topic_key)
    prompt = f"Create a complete original {ch.genre} long video inspired by: {title}."

    def job():
        results = run_long_batch(
            ch, count=1, upload=upload, prompts=[prompt], scenes=20, queue=False,
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

    def job():
        results = run_long_batch(ch, count=count, upload=upload, scenes=20, queue=False)
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
    # Only thumbnail repair reaches this route now; the video is already online.
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
            "language": request.form.get("fb_language", "same"),
            "daily_post": request.form.get("daily_post") == "on",
        }
        if request.form.get("fb_place"):
            social_accounts.set_facebook_place(request.form.get("fb_place"))
    elif platform == "tiktok":
        values = {
            "client_key": request.form.get("client_key", ""),
            "client_secret": request.form.get("client_secret", ""),
            "access_token": request.form.get("access_token", ""),
            "refresh_token": request.form.get("refresh_token", ""),
            "mode": request.form.get("mode", "draft"),
            "privacy": request.form.get("privacy", "SELF_ONLY"),
            "auto_upload": request.form.get("auto_upload") == "on",
            "zernio_api_key": request.form.get("zernio_api_key", ""),
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
        elif platform == "tiktok" and social_accounts.account(ch.id, "tiktok").get("mode") == "zernio":
            name = social_publish.verify_zernio(ch.id)
        elif platform == "tiktok":
            name = social_publish.verify_tiktok(ch.id)
        else:
            raise RuntimeError("Unknown social platform.")
        return f"{platform.title()} connected to: {name}"

    start_job(f"[{ch.id}] Verify {platform.title()}", job)
    flash(f"Verifying {platform.title()} connection...", "ok")
    return redirect(url_for("home", channel=ch.id))


# TikTok sign-ins waiting for TikTok to send the person back: state -> (channel, verifier).
_TIKTOK_LOGINS: dict[str, tuple[str, str]] = {}
# Must match, character for character, the Redirect URI saved in the TikTok app
# (Login Kit -> Desktop).
TIKTOK_REDIRECT_URI = f"http://localhost:{PORT}/tiktok/callback/"
app.jinja_env.globals.update(tiktok_redirect_uri=TIKTOK_REDIRECT_URI, tiktok_site=setup_guide.SITE)


@app.route("/tiktok-login", methods=["POST"])
def tiktok_login_route():
    ch = current_channel()
    try:
        url, verifier, state = social_publish.tiktok_login_url(ch.id, TIKTOK_REDIRECT_URI)
    except Exception as exc:
        flash(f"TikTok: {exc}", "error")
        return redirect(url_for("home", channel=ch.id))
    _TIKTOK_LOGINS[state] = (ch.id, verifier)
    return redirect(url)


@app.route("/tiktok/callback/")
def tiktok_callback_route():
    pending = _TIKTOK_LOGINS.pop(request.args.get("state", ""), None)
    if pending is None:
        return _tiktok_done("", "TikTok login expired or did not start from this dashboard. Click Login with TikTok again.", False)
    channel_id, verifier = pending
    if request.args.get("error"):
        reason = request.args.get("error_description") or request.args.get("error")
        return _tiktok_done(channel_id, f"TikTok login cancelled: {reason}", False)
    try:
        name = social_publish.tiktok_finish_login(channel_id, request.args.get("code", ""), verifier, TIKTOK_REDIRECT_URI)
        social_accounts.mark_verified(channel_id, "tiktok", name)
        return _tiktok_done(channel_id, f"TikTok connected to: {name}", True)
    except Exception as exc:
        return _tiktok_done(channel_id, f"TikTok login failed: {exc}", False)


def _tiktok_done(channel_id: str, message: str, ok: bool):
    # TikTok returns to "localhost", but the dashboard (and its message cookie)
    # lives on 127.0.0.1, so hand the result over in the address instead.
    from urllib.parse import urlencode

    query = urlencode({"channel": channel_id, "msg": message, "ok": "1" if ok else ""})
    return redirect(f"http://127.0.0.1:{PORT}/tiktok/done?{query}")


@app.route("/tiktok/done")
def tiktok_done_route():
    flash(request.args.get("msg", "")[:500], "ok" if request.args.get("ok") else "error")
    channel_id = request.args.get("channel", "")
    return redirect(url_for("home", channel=channel_id) if channel_id else url_for("home"))


@app.post("/facebook-user-token")
def facebook_user_token_route():
    ch = current_channel()
    try:
        flash(social_publish.facebook_from_user_token(
            ch.id, request.form.get("user_token", ""), request.form.get("page_id", ""),
        ), "ok")
    except Exception as exc:
        flash(f"Facebook: {exc}", "error")
    return redirect(url_for("home", channel=ch.id))


@app.get("/guide/app-icon.png")
def guide_app_icon():
    """A plain 1024x1024 icon for Meta's App settings, which require one."""
    import cv2
    import numpy as np

    size = 1024
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) / size
    t = (x * 0.6 + y * 0.4)[..., None]
    img = ((1 - t) * np.array([140, 70, 20]) + t * np.array([230, 110, 120])).astype(np.uint8)
    cv2.circle(img, (size // 2, size // 2), 330, (255, 255, 255), 18, cv2.LINE_AA)
    cv2.fillPoly(img, [np.array([[440, 360], [440, 664], [690, 512]], np.int32)], (255, 255, 255), cv2.LINE_AA)
    ok, png = cv2.imencode(".png", img)
    return app.response_class(png.tobytes(), mimetype="image/png", headers={
        "Content-Disposition": 'attachment; filename="app-icon-1024.png"'})


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


# Google sign-in runs in the browser you already have open. The old button
# started a background thread that tried to pop up a second browser window;
# when that silently failed nothing at all appeared to happen. Now the click
# navigates straight to Google and Google sends you back here.
_PENDING_OAUTH: dict[str, dict] = {}
_EMAIL_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]


def _signin_email(credentials) -> str:
    """The Gmail of a fresh sign-in, from the ID token Google sent back."""
    import base64

    token = getattr(credentials, "id_token", None) or ""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("email") or "")
    except Exception:
        return ""


def _oauth_flow(target, redirect_uri: str):
    from google_auth_oauthlib.flow import InstalledAppFlow

    from src.youtube_upload import SCOPES

    # The email scopes are asked for here only, so the dashboard can show which
    # Gmail a channel is signed in with. Uploads keep refreshing with SCOPES,
    # a subset of what is granted, which Google allows; older logins without
    # the email scopes keep working. Google may list the granted scopes in
    # another order or form; that is not an error.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(target.client_secret_path), [*SCOPES, *_EMAIL_SCOPES],
    )
    flow.redirect_uri = redirect_uri
    return flow


@app.route("/connect", methods=["POST"])
def connect():
    ch = current_channel()
    return connect_account(ch.id, request.form.get("account", "main"))


def _finish_oauth():
    """Complete a Google sign-in that came back to the dashboard's own URL.

    A Desktop OAuth client only accepts loopback redirects whose path is "/",
    which is why Google returns here rather than to a dedicated callback route.
    """
    state = request.args.get("state", "")
    pending = _PENDING_OAUTH.pop(state, None)
    if not pending:
        flash("That Google sign-in link expired. Press Connect again.", "error")
        return redirect(url_for("home"))
    channel_id, account_id = pending["channel"], pending["account"]
    if request.args.get("error"):
        flash(f"Google sign-in cancelled: {request.args['error']}", "error")
        return redirect(url_for("home", channel=channel_id))
    try:
        target = youtube_accounts.channel_for_account(channel_id, account_id)
        flow = _oauth_flow(target, pending["redirect_uri"])
        flow.code_verifier = pending.get("code_verifier")
        # oauthlib refuses to finish OAuth over plain http, and a loopback
        # redirect is always http. google-auth-oauthlib solves this the same
        # way in run_local_server: present the callback URL as https. Only this
        # local string changes — the token request to Google is real HTTPS.
        flow.fetch_token(
            authorization_response=request.url.replace("http://", "https://", 1),
        )
        target.token_path.write_text(flow.credentials.to_json(), encoding="utf-8")
        email = _signin_email(flow.credentials)
        youtube_accounts.set_account_email(channel_id, account_id, email)
    except Exception as exc:
        flash(f"Google sign-in failed: {exc}", "error")
        return redirect(url_for("home", channel=channel_id))
    via = f" (Google account: {email})" if email else ""

    # Say which YouTube channel this actually linked to, so a wrong pick is
    # obvious immediately instead of after videos land in the wrong place.
    try:
        from src.youtube_upload import _youtube

        service = _youtube(target.client_secret_path, target.token_path)
        items = service.channels().list(part="snippet", mine=True).execute().get("items", [])
        name = items[0]["snippet"]["title"] if items else "(no channel on this account)"
        # Show the real YouTube channel name in the row, so nobody has to type one.
        youtube_accounts.set_account_name(channel_id, account_id, name)
        flash(f"Connected '{account_id}' to YouTube channel: {name}{via}", "ok")
    except Exception as exc:
        flash(f"Connected{via}, but could not read the channel name: {exc}", "ok")
    return redirect(url_for("home", channel=channel_id))


@app.route("/disconnect", methods=["POST"])
def disconnect():
    ch = current_channel()
    account_id = request.form.get("account", "main")
    target = youtube_accounts.channel_for_account(ch.id, account_id)
    tok = target.token_path
    if tok.exists():
        tok.rename(tok.with_suffix(tok.suffix + ".bak"))
        flash(f"Disconnected {target.name}. Connect again to link a channel.", "ok")
    else:
        flash("This account was not connected.", "error")
    return redirect(url_for("home", channel=ch.id))


@app.post("/account-add")
def account_add():
    """One button: make room for another YouTube channel and sign in to it.

    The account id is generated and the display name is filled in from the
    YouTube channel itself once the sign-in finishes, so the only thing to do
    is pick the right channel in the Google window.
    """
    ch = current_channel()
    try:
        account = youtube_accounts.add_account(
            ch.id,
            request.form.get("account", "").strip() or youtube_accounts.next_account_id(ch.id),
            request.form.get("name", ""),
            request.form.get("client_secret", ""),
        )
    except Exception as exc:
        flash(str(exc), "error")
        return redirect(url_for("home", channel=ch.id))
    # Go straight to Google instead of making them find the Connect button.
    return connect_account(ch.id, account.id)


def connect_account(channel_id: str, account_id: str):
    """Send the browser to Google's channel picker for one account."""
    try:
        target = youtube_accounts.channel_for_account(channel_id, account_id)
    except Exception as exc:
        flash(str(exc), "error")
        return redirect(url_for("home", channel=channel_id))
    if not target.client_secret_path.is_file():
        flash(f"Missing OAuth JSON file: {target.client_secret}", "error")
        return redirect(url_for("home", channel=channel_id))
    try:
        redirect_uri = url_for("home", _external=True)
        flow = _oauth_flow(target, redirect_uri)
        # No include_granted_scopes: if this Google account already granted
        # extra scopes elsewhere, Google would hand back a wider scope set than
        # was asked for and the exchange fails with "Scope has changed".
        auth_url, state = flow.authorization_url(
            access_type="offline", prompt="consent",
        )
    except Exception as exc:
        flash(f"Could not start Google sign-in: {exc}", "error")
        return redirect(url_for("home", channel=channel_id))
    _PENDING_OAUTH[state] = {
        "channel": channel_id, "account": account_id, "redirect_uri": redirect_uri,
        # PKCE: authorization_url() generated this verifier and Google saw only
        # its hash. The callback builds a fresh Flow, so the verifier has to be
        # carried across or the exchange fails with "Missing code verifier".
        "code_verifier": flow.code_verifier,
    }
    return redirect(auth_url)


@app.post("/account-remove")
def account_remove():
    ch = current_channel()
    account_id = request.form.get("account", "")
    try:
        youtube_accounts.remove_account(ch.id, account_id)
        flash(f"Removed '{account_id}' from {ch.name}.", "ok")
    except Exception as exc:
        flash(str(exc), "error")
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


# ---------- new channel from a topic ----------

def _page_style() -> str:
    """The main dashboard's stylesheet, so the extra pages look the same."""
    import re

    match = re.search(r"<style>.*?</style>", PAGE, re.S)
    return match.group(0) if match else ""


@app.get("/channel-new")
def channel_new():
    return render_template_string(
        NEW_CHANNEL_PAGE, style=_page_style(), spec=None, channels=load_channels(),
        voices=channel_factory.VOICES, categories=channel_factory.CATEGORIES,
        visuals=channel_factory.VISUALS, languages=LANGUAGES,
    )


@app.post("/channel-new/design")
def channel_new_design():
    topic = request.form.get("topic", "")
    try:
        spec = channel_factory.design_channel(topic)
    except Exception as exc:
        flash(f"Could not plan the channel: {exc}", "error")
        return redirect(url_for("channel_new"))
    return render_template_string(
        NEW_CHANNEL_PAGE, style=_page_style(), spec=spec, channels=load_channels(),
        voices=channel_factory.VOICES, categories=channel_factory.CATEGORIES,
        visuals=channel_factory.VISUALS, languages=LANGUAGES,
    )


@app.post("/channel-new/create")
def channel_new_create():
    form = request.form
    spec = {
        "topic": form.get("topic", ""), "name": form.get("name", ""),
        "genre": form.get("genre", ""), "tone": form.get("tone", ""),
        "image_style": form.get("image_style", ""), "seo_suffix": form.get("seo_suffix", ""),
        "voice": form.get("voice", ""), "category_id": form.get("category_id", ""),
        "made_for_kids": form.get("made_for_kids") == "on", "visuals": form.get("visuals", ""),
        "keywords": form.get("keywords", ""), "first_topics": form.get("first_topics", ""),
        "language": form.get("language", "en"),
    }
    try:
        channel_id = channel_factory.create_channel(spec)
    except Exception as exc:
        flash(f"Could not create the channel: {exc}", "error")
        return redirect(url_for("channel_new"))
    flash("Channel created. Finish the three setup steps below and it runs every day by itself.", "ok")
    return redirect(url_for("home", channel=channel_id))


@app.post("/channel-delete")
def channel_delete():
    ch = current_channel()
    try:
        channel_factory.delete_channel(ch.id)
    except Exception as exc:
        flash(str(exc), "error")
        return redirect(url_for("home", channel=ch.id))
    flash(f"Deleted {ch.name}. Its login and topic files were kept as .bak copies. "
          "Press 'Send to GitHub' on any channel so the daily run drops it too.", "ok")
    return redirect(url_for("home"))


@app.get("/guide")
def guide():
    ch = current_channel()
    status = channel_factory.setup_status(ch)
    project_id = status.get("project_id", "")
    full = request.args.get("mode") == "full"
    return render_template_string(
        GUIDE_PAGE, style=_page_style(), ch=ch, channels=load_channels(), full=full,
        steps=setup_guide.steps_for(project_id) if full else setup_guide.quick_steps_for(project_id),
        who=setup_guide.WHO, project_id=project_id,
    )


@app.post("/guide/shot/<step_id>")
def guide_shot_save(step_id):
    uploaded = request.files.get("shot")
    try:
        if not uploaded:
            raise ValueError("No screenshot received.")
        setup_guide.save_shot(step_id, uploaded.read(6 * 1024 * 1024 + 1))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.post("/guide/shot/<step_id>/delete")
def guide_shot_delete(step_id):
    try:
        setup_guide.delete_shot(step_id)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


@app.get("/guide/shot/<step_id>")
def guide_shot(step_id):
    path = setup_guide.shot_path(step_id)
    if not path:
        abort(404)
    response = send_file(path)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/guide/client")
def guide_client():
    page = setup_guide.client_guide_html(request.args.get("email", ""))
    return app.response_class(
        page, mimetype="text/html",
        headers={"Content-Disposition": 'attachment; filename="connect-your-youtube-channel.html"'},
    )


@app.post("/channel-slot")
def channel_slot_change():
    ch = current_channel()
    try:
        channel_factory.set_slot(ch.id, int(request.form.get("slot", "0")))
        flash(f"{ch.name} now builds at {slots.label(int(request.form.get('slot', '0')))}. "
              "Press 'Send to GitHub' so the daily runs use the new time.", "ok")
    except Exception as exc:
        flash(f"Could not change the build time: {exc}", "error")
    return redirect(url_for("home", channel=ch.id))


@app.post("/channel-language")
def channel_language_change():
    ch = current_channel()
    language = request.form.get("language", "en")
    try:
        channel_factory.set_language(ch.id, language)
        words = "Urdu (Roman Urdu on screen, Urdu voice)" if language == "ur" else "English"
        flash(f"{ch.name}: new videos will be in {words}. Videos already made stay as they are. "
              "Press 'Send to GitHub' so the daily runs use it too.", "ok")
    except Exception as exc:
        flash(f"Could not change the language: {exc}", "error")
    return redirect(url_for("home", channel=ch.id))


@app.post("/automation")
def automation_toggle():
    ch = current_channel()
    on = request.form.get("on") == "1"
    set_dashboard_automation(on)
    flash("This PC now makes and uploads missing daily videos by itself." if on else
          "This PC's automation is off: only GitHub makes the daily videos; buttons still work.", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.post("/github-sync")
def github_sync():
    ch = current_channel()
    try:
        flash(channel_factory.sync_to_github(), "ok")
    except Exception as exc:
        flash(f"GitHub update failed: {exc}", "error")
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


NEW_CHANNEL_PAGE = r"""
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>New channel · Story Bot STUDIO</title>
{{ style|safe }}
<style>
  .newch{max-width:860px}
  .newch label.field textarea{background:#12162a;border:1px solid var(--line);color:var(--text);padding:10px 12px;border-radius:10px;font:inherit;min-height:70px;resize:vertical}
  .newch .grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-top:14px}
  .newch select{max-width:none;min-width:0}
</style></head><body>
<header><div class="logo">🎬</div><div><h1>New channel</h1>
  <div class="sub">Describe a topic. AI plans the channel; you check it and create it.</div></div></header>
<div class="wrap newch">
  <div class="tabs">{% for c in channels %}<a class="tab" href="/?channel={{c.id}}">{{c.name}}</a>{% endfor %}<a class="tab active" href="/channel-new">➕ New channel</a></div>
  {% with msgs = get_flashed_messages(with_categories=true) %}{% for cat,msg in msgs %}<div class="flash {{cat}}">{{msg}}</div>{% endfor %}{% endwith %}

  <div class="card" style="margin-bottom:18px">
    <form method="post" action="/channel-new/design" class="row" style="align-items:flex-end;gap:12px" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='Planning… (about 20 seconds)'">
      <label class="field">What is the channel about?
        <input type="text" name="topic" required minlength="3" maxlength="200" placeholder="e.g. cooking for beginners, space facts, cat stories" value="{{spec.topic if spec else ''}}">
      </label>
      <button class="green" type="submit">✨ {{'Plan again' if spec else 'Plan this channel'}}</button>
    </form>
  </div>

  {% if spec %}
  <form method="post" action="/channel-new/create" class="card">
    <strong>Check the plan</strong>
    <div class="hint">Change anything you like. Nothing is created until you press the button at the bottom.</div>
    <input type="hidden" name="topic" value="{{spec.topic}}">
    <div class="grid2">
      <label class="field">Channel name<input type="text" name="name" value="{{spec.name}}" required maxlength="40"></label>
      <label class="field">Niche (one or two words)<input type="text" name="genre" value="{{spec.genre}}" required maxlength="30"></label>
      <label class="field">Voice<select name="voice">{% for v,label in voices.items() %}<option value="{{v}}" {{'selected' if v==spec.voice else ''}}>{{label}}</option>{% endfor %}</select></label>
      <label class="field">Pictures<select name="visuals">{% for v,label in visuals.items() %}<option value="{{v}}" {{'selected' if v==spec.visuals else ''}}>{{label}}</option>{% endfor %}</select></label>
      <label class="field">YouTube category<select name="category_id">{% for v,label in categories.items() %}<option value="{{v}}" {{'selected' if v==spec.category_id else ''}}>{{label}}</option>{% endfor %}</select></label>
      <label class="field">Title ending<input type="text" name="seo_suffix" value="{{spec.seo_suffix}}" maxlength="40"></label>
      <label class="field">Video language<select name="language">{% for v,label in languages.items() %}<option value="{{v}}" {{'selected' if v==spec.language else ''}}>{{label}}</option>{% endfor %}</select></label>
    </div>
    <div class="grid2">
      <label class="field">How the scripts should sound<textarea name="tone" maxlength="300">{{spec.tone}}</textarea></label>
      <label class="field">Picture style<textarea name="image_style" maxlength="200">{{spec.image_style}}</textarea></label>
      <label class="field">Search keywords (comma separated)<textarea name="keywords">{{spec.keywords|join(', ')}}</textarea></label>
      <label class="field">First video ideas (one per line, one a day)<textarea name="first_topics" rows="5">{{spec.first_topics|join('\n')}}</textarea></label>
    </div>
    <label class="chk" style="margin-top:14px;display:block"><input type="checkbox" name="made_for_kids" {{'checked' if spec.made_for_kids else ''}}> Made for kids <span class="hint">(YouTube rule: tick only if the audience is young children)</span></label>
    <div class="row" style="margin-top:16px;gap:12px;align-items:center">
      <button class="green" type="submit">Create channel</button>
      <span class="hint">Next: give it its own Google Cloud project, connect YouTube, and send it to GitHub.</span>
    </div>
  </form>
  {% endif %}
</div></body></html>
"""


GUIDE_PAGE = r"""
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Setup guide · Story Bot STUDIO</title>
{{ style|safe }}
<style>
  .gd{max-width:900px}
  .gd h3{margin:26px 0 4px;font-size:18px}
  .gd .part-ur{color:var(--muted);margin:0 0 12px;font-size:13px}
  .step{display:grid;grid-template-columns:42px 1fr;gap:12px;padding:16px;border:1px solid var(--line);border-radius:14px;background:var(--panel);margin-bottom:12px}
  .num{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:700;background:linear-gradient(135deg,var(--accent),var(--accent2))}
  .step h4{margin:4px 0 6px;font-size:16px}
  .step p{margin:4px 0;line-height:1.55}
  .step .ur{color:var(--muted);font-size:14px}
  .step .tip{color:var(--muted);font-size:13px}
  .step .go{display:inline-block;margin:8px 0 2px;padding:8px 12px;border-radius:10px;background:rgba(108,140,255,.15);border:1px solid rgba(108,140,255,.4);font-weight:600;font-size:14px}
  .shot{margin-top:10px;border:2px dashed var(--line);border-radius:12px;padding:14px;text-align:center;color:var(--muted);font-size:13px;cursor:pointer;outline:none}
  .shot:focus{border-color:var(--accent);color:var(--text)}
  .shot img{display:block;max-width:100%;height:auto;border-radius:8px;margin:0 auto}
  .shot.has{border-style:solid;padding:6px}
  .shot-tools{display:flex;gap:8px;justify-content:flex-end;margin-top:6px}
  .client-box{border:1px solid rgba(61,220,151,.45);background:rgba(61,220,151,.07);border-radius:14px;padding:16px;margin:18px 0}
  .client-box textarea{width:100%;margin-top:8px;background:#12162a;border:1px solid var(--line);color:var(--text);border-radius:10px;padding:10px;font:inherit;font-size:13px}
</style></head><body>
<header><div class="logo">📘</div><div><h1>Google Cloud setup guide</h1>
  <div class="sub">For: {{ch.name}}{% if project_id %} · project <b>{{project_id}}</b> (links open on it){% endif %}</div></div></header>
<div class="wrap gd">
  <div class="tabs"><a class="tab" href="/?channel={{ch.id}}">← Back to {{ch.name}}</a>
    <a class="tab {{'' if full else 'active'}}" href="/guide?channel={{ch.id}}">⚡ Quick: I do everything</a>
    <a class="tab {{'active' if full else ''}}" href="/guide?channel={{ch.id}}&mode=full">Full: client does part 1</a></div>

  <div class="card">
    <strong>Screenshots: add them once, use them for every client</strong>
    <div class="hint">While you do one real setup, take a screenshot at each step (<b>Win + Shift + S</b>), click that step's dashed box below and press <b>Ctrl + V</b>. Or click the box to pick an image file. Screenshots are saved on this PC only, in <code>data/guide/</code>. Before sending them to a client, make sure they do not show anything private.</div>
  </div>

  {% if full %}
  <div class="client-box" id="client">
    <strong>📤 Send the client their part</strong>
    <div class="hint">The client only does Part 1 (4 steps). Type your Gmail once; it goes into the guide and the message.</div>
    <div class="row" style="margin-top:8px;align-items:flex-end;gap:10px">
      <label class="field" style="flex:0 1 280px">Your Gmail<input type="text" id="guide-email" placeholder="you@gmail.com"></label>
      <a class="btn-link green" id="client-download" href="/guide/client">⬇ Download client guide (one file, with screenshots)</a>
      <button type="button" class="ghost sm" id="guide-copy">📋 Copy WhatsApp message</button>
    </div>
    <textarea id="guide-message" readonly rows="7"></textarea>
    <div class="hint">Send the downloaded file with the message. It opens in any browser, on a phone too.</div>
  </div>
  {% endif %}

  {% set ns = namespace(part='', n=0) %}
  {% for step in steps %}
    {% if step.who != ns.part %}{% set ns.part = step.who %}
      <h3>{{ who[step.who][0] }}</h3><div class="part-ur">{{ who[step.who][1] }}</div>
    {% endif %}
    {% set ns.n = ns.n + 1 %}
    <div class="step">
      <div class="num">{{ns.n}}</div>
      <div>
        <h4>{{step.title}} <span class="hint">· {{step.urdu}}</span></h4>
        <p>{{step.do|safe}}</p>
        <p class="ur">{{step.do_urdu|safe}}</p>
        {% if step.link %}<a class="go" href="{{step.link}}" target="_blank" rel="noopener">{{step.link_label}} ↗</a>{% endif %}
        {% if step.tip %}<p class="tip">💡 {{step.tip|safe}}</p>{% endif %}
        <div class="shot {{'has' if step.has_shot else ''}}" tabindex="0" data-step="{{step.id}}">
          {% if step.has_shot %}<img src="/guide/shot/{{step.id}}" alt="Screenshot of step {{ns.n}}">
          {% else %}📷 No screenshot yet. Click here, then press <b>Ctrl + V</b> (or click again to choose a file).{% endif %}
        </div>
        {% if step.has_shot %}<div class="shot-tools"><button type="button" class="ghost sm" data-delete="{{step.id}}">Remove screenshot</button></div>{% endif %}
      </div>
    </div>
  {% endfor %}
  <input type="file" id="shot-file" accept="image/png,image/jpeg" hidden>
</div>
<script>
(function () {
  var email = document.getElementById('guide-email'), box = document.getElementById('guide-message'),
      copy = document.getElementById('guide-copy'), dl = document.getElementById('client-download');
  if (email) {
  try { email.value = localStorage.getItem('guide-email') || ''; } catch (e) {}
  function render() {
    var who = email.value.trim() || '[my Gmail]';
    dl.href = '/guide/client?email=' + encodeURIComponent(email.value.trim());
    box.value = "Assalam o Alaikum! Aap ka YouTube channel bot se jorne ke liye computer par 2 minute ka kaam hai. " +
      "Saath wali file (connect-your-youtube-channel.html) kholen, us mein screenshots ke saath 4 qadam hain.\n\n" +
      "Short version:\n" +
      "1. https://console.cloud.google.com/projectcreate kholen, naam \"My YouTube Bot\" likhen, Create dabayen.\n" +
      "2. https://console.cloud.google.com/iam-admin/iam kholen, \"Grant access\" dabayen.\n" +
      "3. " + who + " likhen, role \"Basic > Editor\", Save.\n" +
      "4. \"Project ID\" copy karke mujhe bhej dein.\n\n" +
      "Is se aap ka password ya Gmail mere paas nahi aata. Shukriya!";
  }
  email.addEventListener('input', function () { try { localStorage.setItem('guide-email', email.value.trim()); } catch (e) {} render(); });
  copy.addEventListener('click', function () {
    box.select();
    var done = function () { copy.textContent = '✅ Copied'; setTimeout(function () { copy.textContent = '📋 Copy WhatsApp message'; }, 1500); };
    if (navigator.clipboard) { navigator.clipboard.writeText(box.value).then(done, function () { document.execCommand('copy'); done(); }); }
    else { document.execCommand('copy'); done(); }
  });
  render();
  }

  document.querySelectorAll('code.cp').forEach(function (c) {
    c.addEventListener('click', function () {
      var v = c.textContent, back = function () { c.textContent = v; };
      if (navigator.clipboard) navigator.clipboard.writeText(v).then(function () { c.textContent = '✅ copied'; setTimeout(back, 1200); });
    });
  });

  // Screenshots: paste into the focused box, or click a focused box to pick a file.
  var picker = document.getElementById('shot-file'), target = null;
  function upload(step, file) {
    var fd = new FormData(); fd.append('shot', file);
    var el = document.querySelector('.shot[data-step="' + step + '"]');
    if (el) el.textContent = 'Saving…';
    fetch('/guide/shot/' + step, { method: 'POST', body: fd }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j.ok) { alert(j.error || 'Could not save'); }
      location.hash = ''; location.reload();
    });
  }
  document.querySelectorAll('.shot').forEach(function (el) {
    el.addEventListener('click', function () {
      if (document.activeElement === el && el.dataset.armed) { target = el.dataset.step; picker.click(); }
      el.dataset.armed = '1'; el.focus();
    });
    el.addEventListener('blur', function () { delete el.dataset.armed; });
  });
  picker.addEventListener('change', function () { if (picker.files[0] && target) upload(target, picker.files[0]); });
  document.addEventListener('paste', function (e) {
    var el = document.activeElement;
    if (!el || !el.classList || !el.classList.contains('shot')) return;
    var items = (e.clipboardData || {}).items || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].type.indexOf('image') === 0) { e.preventDefault(); upload(el.dataset.step, items[i].getAsFile()); return; }
    }
  });
  document.querySelectorAll('[data-delete]').forEach(function (b) {
    b.addEventListener('click', function () {
      fetch('/guide/shot/' + b.dataset.delete + '/delete', { method: 'POST' }).then(function () { location.reload(); });
    });
  });
})();
</script>
</body></html>
"""


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
  .st.scheduled,.st.facebook_only{background:#1a2b4d;color:#8fb4ff}
  .st.thumbnail_pending{background:#3a2f12;color:var(--warn)}.st.upload_failed,.st.quality_failed,.st.duplicate_blocked{background:#331617;color:var(--err)}
  .st.daily_upload_pending{background:#2d2754;color:#c9b7ff}.st.awaiting_approval{background:#3a2f12;color:var(--warn)}
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
  .setup-steps{margin:12px 0 0;padding:0;list-style:none;display:grid;gap:10px}
  .setup-steps li{display:flex;flex-direction:column;gap:3px;padding:10px 12px;border:1px solid var(--line);border-radius:10px}
  .setup-steps li.done{border-color:rgba(61,220,151,.4)}
  .fb-steps{line-height:1.7;font-size:14px;padding-left:22px}
  .fb-steps li{margin-bottom:10px}
  .fb-steps ul{margin:4px 0;padding-left:18px}
  .slot-row{margin-top:10px;gap:10px;align-items:center;font-size:14px}
  .slot-row select{min-width:0;max-width:280px;flex:0 1 auto}
  .email{font-family:ui-monospace,Consolas,monospace;font-size:13px;color:#cfe0ff}
  code.cp{cursor:pointer;background:#12162a;border:1px solid var(--line);border-radius:6px;padding:1px 6px;font-size:12.5px;color:#cfe0ff;word-break:break-all}
  code.cp:hover{border-color:var(--accent)}
  .setup-links{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
  .quick-bar{margin-top:12px;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}
  .quick-links{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;align-items:center}
  .quick-links a{padding:7px 11px;border-radius:9px;background:rgba(108,140,255,.14);border:1px solid rgba(108,140,255,.35);font-size:13px;color:var(--text)}
  .quick-links a b{color:var(--accent);margin-right:4px}
  .quick-steps{margin:10px 0;padding-left:0;list-style:none;display:grid;gap:6px}
  .quick-steps li{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
  .quick-steps a{padding:5px 10px;border-radius:9px;background:rgba(108,140,255,.14);border:1px solid rgba(108,140,255,.35);font-size:13px;color:var(--text);white-space:nowrap}
  .quick-steps a b{color:var(--accent);margin-right:4px}
  .btn-link{display:inline-block;padding:9px 14px;border-radius:10px;font-weight:600;font-size:14px;border:1px solid var(--line)}
  .btn-link.green{background:rgba(61,220,151,.15);border-color:rgba(61,220,151,.45);color:var(--ok)}
  .btn-link.ghost{color:var(--text)}
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
  .guide-box summary{cursor:pointer;font-weight:700}.guide-box h4{margin:16px 0 2px;font-size:14px;color:var(--text,#e8ebff)}.guide-box code.copy{display:block;margin:4px 0 6px;padding:6px 8px;border-radius:8px;background:#0b0f1e;border:1px solid var(--line);white-space:pre-wrap;word-break:break-all;user-select:all}.guide-box kbd{padding:0 5px;border:1px solid var(--line);border-radius:4px;font-size:12px}.guide-box ol{margin:10px 0 4px;padding-left:20px;color:var(--muted);font-size:13px;line-height:1.55}
  .social-state{display:flex;gap:6px;flex-wrap:wrap;margin:7px 0}.social-state .chip.failed{color:var(--err)}.social-state .chip.submitted{color:var(--ok)}  @keyframes sectionIn{from{opacity:.45;transform:translateY(3px)}to{opacity:1;transform:none}}  @media(max-width:800px){.action-grid,.stats,.analytics-grid{grid-template-columns:1fr}.action-card.full{grid-column:auto}.channel-banner .only{margin-left:0}}
</style></head><body>
<header><div class="logo">🎬</div><div>
  <h1>Story Bot STUDIO</h1>
  <div class="sub">Multi-channel • 1203 kids topics • daily batch • retry queue • free Gemini genre topics</div>
</div><div class="pro">PRO • port 8000</div></header>
<div class="wrap">

  <div class="tabs">
    {% for c in channels %}<a class="tab {{'active' if c.id==ch.id else ''}}" href="/?channel={{c.id}}">{{c.name}}</a>{% endfor %}
    <a class="tab" href="/channel-new" style="border-style:dashed">➕ New channel</a>
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

  <div class="card setup-card" style="margin-bottom:18px">
    <strong>Setup for {{ch.name}}</strong>
    <div class="hint">Three steps. When all three are green, GitHub makes and uploads this channel's videos every day, even with this PC off.</div>
    <form method="post" action="/channel-slot" class="row slot-row">
      <input type="hidden" name="channel" value="{{ch.id}}">
      <span>🕒 Daily video is made at</span>
      <select name="slot" onchange="this.form.submit()">
        {% for i, when, count in slot_options %}<option value="{{i}}" {{'selected' if i == slot_now else ''}}>{{when}} · {{count}} channel{{'' if count == 1 else 's'}}</option>{% endfor %}
      </select>
      <span class="hint">Up to about 7 channels fit in one time slot.</span>
    </form>
    <form method="post" action="/channel-language" class="row slot-row">
      <input type="hidden" name="channel" value="{{ch.id}}">
      <span>🗣 Video language</span>
      <select name="language" onchange="this.form.submit()">
        {% for code, name in languages.items() %}<option value="{{code}}" {{'selected' if code == (ch.language or 'en') else ''}}>{{name}}</option>{% endfor %}
      </select>
      <span class="hint">Urdu: captions and title in Roman Urdu, spoken by a Pakistani Urdu voice.</span>
    </form>
    <ol class="setup-steps">
      <li class="{{'done' if setup.project else ''}}">
        <b>{{'✅' if setup.project else '1️⃣'}} Own Google Cloud project</b>
        <span class="hint">{% if setup.project %}This channel has its own upload quota.{% elif setup.project_file %}Shares a project (and its ~6 uploads a day) with: {{api.youtube_project.shared_with|join(', ')}}. Upload the channel owner's OAuth JSON in the box below.{% else %}No OAuth JSON yet. Upload the channel owner's file in the box below.{% endif %}</span>
      </li>
      <li class="{{'done' if setup.youtube else ''}}">
        <b>{{'✅' if setup.youtube else '2️⃣'}} YouTube connected</b>
        <span class="hint">{% if setup.youtube %}Signed in{% if accounts and accounts[0].email %} with <b class="email">{{accounts[0].email}}</b>. Uploads go to the channel named in the table below.{% else %}. <span style="color:var(--warn)">Which Gmail is not known yet: this login is older than the email feature. Press <b>🔗 Reconnect</b> in the table below once (same Gmail), then <b>Send to GitHub</b>.</span>{% endif %}{% else %}Press "Connect &amp; verify" below and sign in with the Google account that owns the YouTube channel.{% endif %}</span>
      </li>
      <li class="{{'done' if setup.github else ''}}">
        <b>{{'✅' if setup.github else '3️⃣'}} Sent to GitHub</b>
        <span class="hint">{% if setup.github %}GitHub has every channel and login from this PC.{% else %}Something changed on this PC (a channel, login or key) that the daily run does not have yet.{% endif %}</span>
        {% if not setup.github %}<form method="post" action="/github-sync" style="margin-top:6px"><input type="hidden" name="channel" value="{{ch.id}}"><button class="green sm" type="submit">⬆ Send to GitHub</button></form>{% endif %}
      </li>
    </ol>
    <div class="quick-bar">
      <span class="hint"><b>Quick links</b> (open them in the Incognito window where the channel owner's Gmail is signed in):</span>
      <div class="row" style="margin-top:8px;gap:8px;align-items:center">
        <label class="field" style="flex:0 1 330px">Project ID of this channel's Cloud project
          <input type="text" id="project-id" placeholder="e.g. protean-atom-511418-d3" value="{{setup.project_id}}" autocomplete="off" spellcheck="false"></label>
        <span class="hint" id="project-hint">{% if setup.project_id %}Read from the uploaded JSON.{% else %}Paste it after step 1 — every link below then opens straight on that project, no project picking.{% endif %}</span>
      </div>
      <ol class="quick-steps">
        {% for label, url in quick_links %}<li><a href="{{url}}" target="_blank" rel="noopener" data-url="{{url}}" data-base="{{url.split('?')[0]}}" data-first="{{'1' if loop.first else ''}}"><b>{{loop.index}}</b> {{label}} ↗</a> <span class="hint">{{quick_steps[loop.index0][2]}}</span></li>{% endfor %}
        <li><a href="#own-project"><b>{{quick_links|length + 1}}</b> Upload JSON ↓</a> <span class="hint">Choose the downloaded file → Save Separate Project → Connect &amp; verify → ⬆ Send to GitHub.</span></li>
      </ol>
      <button type="button" class="ghost sm" id="copy-links">📋 Copy all links</button>
      <div class="hint" style="margin-top:10px"><b>For “Branding links”</b> (click to copy): {% for name, value in branding %}{{name}} <code class="cp" title="Click to copy">{{value}}</code>{{ ' · ' if not loop.last else '' }}{% endfor %}</div>
    </div>
    <div class="setup-links">
      <a class="btn-link green" href="/guide?channel={{ch.id}}">📘 Quick guide with screenshots</a>
      <a class="btn-link ghost" href="/guide?channel={{ch.id}}&mode=full">Full guide (client does part 1)</a>
    </div>
    <script>
    (function () {
      // Project ID box: rewrites links 2+ to open on that project, remembered per channel.
      var box = document.getElementById('project-id'), key = 'project-id:{{ch.id}}';
      if (box) {
        try { if (!box.value) box.value = localStorage.getItem(key) || ''; } catch (e) {}
        var apply = function () {
          var id = box.value.trim().replace(/[^a-z0-9-]/gi, '');
          document.querySelectorAll('.quick-steps a[data-base]').forEach(function (a) {
            var url = a.dataset.first || !id ? a.dataset.base : a.dataset.base + '?project=' + id;
            a.href = url; a.dataset.url = url;
          });
          try { localStorage.setItem(key, id); } catch (e) {}
        };
        box.addEventListener('input', apply); apply();
      }
    })();
    (function () {
      var b = document.getElementById('copy-links'); if (!b) return;
      b.addEventListener('click', function () {
        var text = Array.prototype.map.call(document.querySelectorAll('.quick-steps a[data-url]'), function (a, i) {
          return (i + 1) + '. ' + a.textContent.replace(/^\s*\d+\s*/, '').replace(' ↗', '') + ': ' + a.dataset.url;
        }).join('\n');
        var done = function () { b.textContent = '✅ Copied'; setTimeout(function () { b.textContent = '📋 Copy all links'; }, 1500); };
        if (navigator.clipboard) navigator.clipboard.writeText(text).then(done); else done();
      });
    })();
    document.querySelectorAll('code.cp').forEach(function (c) {
      c.addEventListener('click', function () {
        var v = c.textContent, back = function () { c.textContent = v; };
        if (navigator.clipboard) navigator.clipboard.writeText(v).then(function () { c.textContent = '✅ copied'; setTimeout(back, 1200); });
      });
    });
    </script>
    {% if factory %}
    <details style="margin-top:10px"><summary class="hint" style="cursor:pointer">Channel plan{% if ch.topic %}: "{{ch.topic}}"{% endif %}</summary>
      <div class="hint" style="margin-top:6px"><b>Look:</b> {{ {'stock':'real video clips','cartoon':'animated cartoon pictures','still':'AI pictures'}.get(ch.visuals, ch.visuals) }} &middot; <b>Voice:</b> {{ch.voice}} &middot; <b>Tone:</b> {{ch.tone}}</div>
      {% if ch.ideas %}<div class="hint"><b>First video ideas (one a day):</b> {{ch.ideas|join(' · ')}}</div>{% endif %}
      <form method="post" action="/channel-delete" onsubmit="return confirm('Delete {{ch.name}} from this bot? Its YouTube channel and videos are not touched.')" style="margin-top:8px"><input type="hidden" name="channel" value="{{ch.id}}"><button class="red sm" type="submit">Delete this channel</button></form>
    </details>
    {% endif %}
  </div>

  <div class="card" style="margin-bottom:18px">
    <div>
      <strong>YouTube channels for {{ch.name}}</strong>
      <div class="hint">{{ch.name}} can publish to as many YouTube channels as you like. Each one gets its <b>own topics</b> — the same video is never sent to two of them, so YouTube never sees reused content.</div>
      {% if api.youtube_project.independent %}
        <div class="hint" style="color:var(--ok)"><strong>Independent YouTube API project active</strong> for {{ch.name}}.</div>
      {% else %}
        <div class="hint" style="color:#ffd27a"><strong>Google quota is shared with:</strong> {{api.youtube_project.shared_with|join(', ')}}. Give an account its own OAuth JSON below for a truly separate quota.</div>
      {% endif %}
    </div>
    <table style="width:100%;margin-top:12px;border-collapse:collapse">
      <tr style="text-align:left;color:var(--muted);font-size:13px">
        <th style="padding:6px 8px">Account</th><th style="padding:6px 8px">YouTube channel</th>
        <th style="padding:6px 8px">Google email</th>
        <th style="padding:6px 8px">Today</th><th style="padding:6px 8px">Status</th><th></th>
      </tr>
      {% for a in accounts %}
      <tr style="border-top:1px solid var(--line)">
        <td style="padding:8px"><span class="chip">{{a.id}}</span></td>
        <td style="padding:8px">{{a.name}}</td>
        <td style="padding:8px">{% if a.email %}<span class="email">{{a.email}}</span>{% elif a.connected %}<span class="hint">not saved — press Reconnect once to show it</span>{% else %}<span class="hint">—</span>{% endif %}</td>
        <td style="padding:8px">{{a.used_today}} / {{a.limit}}</td>
        <td style="padding:8px">{% if a.connected %}<span style="color:var(--ok)">● Connected</span>{% else %}<span style="color:var(--warn)">● Not connected</span>{% endif %}</td>
        <td style="padding:8px"><div class="row" style="justify-content:flex-end">
          <form method="post" action="/connect"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="account" value="{{a.id}}"><button class="{{'ghost' if a.connected else 'green'}} sm" type="submit">🔗 {{'Reconnect' if a.connected else 'Connect & verify'}}</button></form>
          {% if a.connected %}<form method="post" action="/disconnect" onsubmit="return confirm('Disconnect {{a.name}}?')"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="account" value="{{a.id}}"><button class="ghost sm" type="submit">Disconnect</button></form>{% endif %}
          {% if a.id != 'main' %}<form method="post" action="/account-remove" onsubmit="return confirm('Remove {{a.id}} from {{ch.name}}?')"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="account" value="{{a.id}}"><button class="red sm" type="submit">Remove</button></form>{% endif %}
        </div></td>
      </tr>
      {% endfor %}
    </table>
    <div style="margin-top:14px;padding-top:14px;border-top:1px solid var(--line)">
      <form method="post" action="/account-add" class="row" style="align-items:center;gap:12px">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <button class="green" type="submit">➕ Connect another YouTube channel</button>
        <span class="hint">One click — Google opens, you pick the channel, and it appears in the list above. No names or ids to fill in.</span>
      </form>
      <details style="margin-top:10px">
        <summary class="hint" style="cursor:pointer">Advanced: give this new channel its own upload quota</summary>
        <form method="post" action="/account-add" class="row" style="margin-top:10px;align-items:flex-end;gap:10px">
          <input type="hidden" name="channel" value="{{ch.id}}">
          <label style="flex:0 0 150px">Account id <span class="hint">(optional)</span><input name="account" placeholder="auto" pattern="[a-z0-9][a-z0-9_-]*"></label>
          <label style="flex:1 1 260px">Own OAuth JSON file <span class="hint">(from a separate Cloud project)</span><input name="client_secret" placeholder="client_secret_{{ch.id}}_ch2.json"></label>
          <button type="submit">Add &amp; connect</button>
        </form>
        <div class="hint" style="margin-top:8px">Accounts sharing one OAuth JSON also share its daily upload quota. Put the JSON file in the app folder first, then type its filename here.</div>
      </details>
    </div>
  </div>

  <div class="card" style="margin-bottom:18px">
    <form method="post" action="/youtube-project" enctype="multipart/form-data" class="row" id="own-project">
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
      <div><strong>Direct YouTube Upload</strong><div class="hint">Videos upload to YouTube as soon as they are rendered — there is no waiting queue. YouTube then releases each one publicly at its own scheduled time.</div><div class="hint"><strong>{{ch.name}} upload budget today:</strong> {{channel_uploads_today}} / {{channel_upload_limit}} for this channel.{% if upload_queue.count %} <strong>{{upload_queue.count}}</strong> video(s) waiting for the next quota day.{% endif %}</div>{% if upload_pause %}<div class="hint" style="color:#ffd27a"><strong>Waiting for YouTube quota:</strong> resumes {{upload_pause[1]|localdt}}. Video creation remains active.</div>{% endif %}</div>
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
          <label class="chk"><input type="checkbox" name="upload" checked> Upload to YouTube</label>
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
      <div class="action-note">Creates vertical Shorts for {{ch.name}} only. Finished videos upload to YouTube straight away.</div>
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
          <label class="chk"><input type="checkbox" name="upload" checked> Upload to YouTube</label>
          <button class="pink" type="submit">Create One Long</button>
        </div>
      </form>

      <div class="row" style="margin-top:14px">
        <form method="post" action="/long-auto" class="row">
          <input type="hidden" name="channel" value="{{ch.id}}">
          <label class="chk"><input type="checkbox" name="upload" checked> Upload to YouTube</label>
          <button class="green" type="submit">Auto Long</button>
        </form>
        <form method="post" action="/long-batch" class="row">
          <input type="hidden" name="channel" value="{{ch.id}}">
          <input type="number" name="count" value="2" min="1" max="20" style="width:70px" title="long videos">
          <label class="chk"><input type="checkbox" name="upload" checked> Upload to YouTube</label>
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
          <label class="chk"><input type="checkbox" name="upload" checked> Upload to YouTube</label>
          <button class="pink" type="submit">Create From My Topic</button>
        </div>
      </form>
      <div class="action-note">Create as many long videos as needed for {{ch.name}}. <b>Upload to YouTube</b> off = the video stays in the dashboard only; on = it uploads to YouTube as soon as it is rendered.</div>
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
  <div class="card" style="margin-bottom:18px">
    <form method="post" action="/automation" class="row" style="justify-content:space-between;align-items:center;gap:12px">
      <input type="hidden" name="channel" value="{{ch.id}}">
      <div><strong>This PC's automation: {% if automation_on %}<span style="color:var(--ok)">ON</span>{% else %}<span style="color:var(--warn)">OFF</span>{% endif %}</strong>
        <div class="hint">ON: while this dashboard is open it makes any of today's missing videos and uploads them. OFF: only the daily GitHub run does that; every button here still works. Keep it OFF while GitHub runs the channels, or both upload and share the same quota.</div></div>
      <input type="hidden" name="on" value="{{'0' if automation_on else '1'}}">
      <button class="{{'ghost' if automation_on else 'green'}} sm" type="submit">{{'Switch OFF' if automation_on else 'Switch ON'}}</button>
    </form>
  </div>

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
      <div class="row" style="justify-content:space-between"><div><strong>Facebook Page</strong> {% if social.facebook.configured and social.facebook.verified_name %}<span class="chip" style="background:rgba(61,220,151,.18);color:var(--ok);border-color:rgba(61,220,151,.5)">✅ Connected: {{social.facebook.verified_name}}</span>{% if social.facebook.auto_upload %} <span class="chip" style="color:var(--ok)">auto-publish ON</span>{% else %} <span class="chip">auto-publish off</span>{% endif %}{% elif social.facebook.configured %}<span class="chip" style="color:var(--warn)">⚠ Saved — press Connect &amp; Verify</span>{% else %}<span class="chip">setup required</span>{% endif %}<div class="hint">Shorts publish as Page Reels; long videos publish as Page videos.</div></div><a href="{{social.guides.facebook_app}}" target="_blank"><button class="ghost sm" type="button">Create Meta App</button></a></div>
      <form method="post" action="/social-settings" style="margin-top:14px">
        <input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="platform" value="facebook">
        <div class="platform-grid">
          <label class="field">Facebook Page ID<input type="text" name="page_id" value="{{social.facebook.page_id}}" placeholder="numbers only, e.g. 123456789012345" autocomplete="off" inputmode="numeric" pattern="[0-9]+" title="The Page ID is a number"></label>
          <label class="field">Page Access Token<input type="password" name="page_access_token" autocomplete="new-password" placeholder="{{'Saved - leave blank to keep it' if social.facebook.configured else 'Paste Page Access Token'}}"></label>
        </div>
        <div class="row" style="margin-top:12px"><label class="field" style="flex:0 1 300px">Facebook video language<select name="fb_language"><option value="same" {{'selected' if social.facebook.language == 'same' else ''}}>Same as YouTube</option><option value="ur" {{'selected' if social.facebook.language == 'ur' else ''}}>Urdu (Roman Urdu, Urdu voice)</option><option value="en" {{'selected' if social.facebook.language == 'en' else ''}}>English</option></select><span class="hint">Different from YouTube: the bot also renders a copy in this language, over the same pictures, and posts that one.</span></label>
          <label class="field" style="flex:0 1 300px">Facebook posting runs from<select name="fb_place">{% for value, label in social.facebook_places.items() %}<option value="{{value}}" {{'selected' if social.facebook_place == value else ''}}>{{label}}</option>{% endfor %}</select><span class="hint">One place only, for every Page: posting from both made Facebook lock the account. After changing it, press Send to GitHub so the daily run knows.</span></label><label class="chk"><input type="checkbox" name="auto_upload" {{'checked' if social.facebook.auto_upload else ''}}> Automatically publish new videos</label><label class="chk"><input type="checkbox" name="daily_post" {{'checked' if social.facebook.daily_post else ''}}> Also post one picture a day</label><button class="green sm" type="submit">Save Facebook Settings</button></div>
      </form>
      <form method="post" action="/social-verify" style="margin-top:10px"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="platform" value="facebook"><button class="ghost sm" type="submit">Connect &amp; Verify Facebook Page</button></form>
      <form method="post" action="/facebook-user-token" class="row" style="margin-top:12px;align-items:flex-end;gap:10px;padding:12px;border:1px dashed var(--line);border-radius:10px">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <input type="hidden" name="page_id" value="{{social.facebook.page_id}}">
        <label class="field" style="flex:1 1 320px">⚡ Fast way: paste the token from Graph API Explorer (step 5 of the guide)
          <input type="password" name="user_token" autocomplete="new-password" placeholder="EAA… (user token)"></label>
        <button class="green sm" type="submit">Get Page token &amp; verify</button>
        <span class="hint" style="flex-basis:100%">The bot finds the Page, saves its ID and its non-expiring Page token, and verifies it. If you manage several Pages, put the Page ID above first.</span>
      </form>
      <details class="guide-box" {{'open' if not social.facebook.verified_name else ''}}><summary>📘 Facebook setup guide with direct links (one time per Page, ~10 minutes)</summary>
        <div class="hint" style="margin:8px 0">Sign in to Facebook as an <b>admin of the Page</b>. Click a grey value to copy it. Meta renames menus now and then; pick the closest one.</div>
        <ol class="fb-steps">
          <li><b>Create the app:</b> <a href="https://developers.facebook.com/apps/creation/" target="_blank">Create app ↗</a> → app name (e.g. the Page's name) and your email → <b>Next</b>.
            <br>Use cases: click <b>Content management</b> on the left → tick only <b>“Manage everything on your Page”</b> → <b>Next</b>. <span class="hint">Do not tick ads, Threads or anything else.</span>
            <br>Business: <b>“I don't want to connect a business portfolio yet”</b> → <b>Next</b> → <b>Next</b> → <b>Create app</b>.</li>
          <li><b>Permissions:</b> on the app's Dashboard → <b>“Customize the Manage everything on your Page use case”</b> → <b>Add</b> next to <code class="cp">pages_show_list</code> <code class="cp">pages_read_engagement</code> <code class="cp">pages_manage_posts</code> (and <code class="cp">publish_video</code> if listed).</li>
          <li><b>App settings → Basic</b> (left menu), then <b>Save changes</b>:
            <ul>
              <li>App domains: <code class="cp">{{branding[3][1]}}</code></li>
              <li>Privacy policy URL: <code class="cp">{{branding[1][1]}}</code></li>
              <li>Terms of Service URL: <code class="cp">{{branding[2][1]}}</code></li>
              <li>User data deletion: “Data deletion instructions URL” → <code class="cp">{{branding[1][1]}}</code></li>
              <li>Category: <b>Business and pages</b></li>
              <li>App icon: <a href="/guide/app-icon.png">⬇ download a ready 1024×1024 icon</a></li>
            </ul>
            <span class="hint">“Something went wrong” on Save: fill the data deletion link, save the links first and the icon separately, or try an Incognito window (extensions can break Meta's form).</span></li>
          <li><b>Do not press Publish.</b> Meta asks for a Business verification there. The app stays in Development mode, which can post to your own Page; check visibility with the test in step 7.</li>
          <li><b>Get a token:</b> <a href="https://developers.facebook.com/tools/explorer/" target="_blank">Graph API Explorer ↗</a> → Meta App: your app → <b>Permissions</b> tab → <b>Add a Permission</b>: the three from step 2 → <b>Generate Access Token</b> → tick the <b>Page</b> → Continue → copy the token with 📋.
            <br><span class="hint">“No configurations available” is fine; use the Permissions tab. For a token that never expires, first paste it in the <a href="https://developers.facebook.com/tools/debug/accesstoken/" target="_blank">Access Token Debugger ↗</a> → Debug → <b>Extend Access Token</b>, and use the long one.</span></li>
          <li><b>Here:</b> paste it in <b>⚡ Fast way</b> above → <b>Get Page token &amp; verify</b>. The badge turns <b>✅ Connected: Page name</b>.</li>
          <li><b>Test, then switch on:</b> Video Library → <b>Share Facebook</b> on one video → open the Page in an Incognito window, logged out. Visible? Tick <b>Automatically publish new videos</b> → <b>Save Facebook Settings</b> → <b>⬆ Send to GitHub</b> on the setup card. Each new video then goes to the Page too (Shorts as Reels, long videos as Page videos).</li>
        </ol>
        <div class="hint">Token stopped working (Facebook password changed, Page role removed)? Repeat steps 5–6. Never send a token in a chat or a screenshot. Reference: <a href="{{social.guides.facebook}}" target="_blank">Reels publishing API ↗</a></div>
      </details>
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
          <label class="field">Zernio API key (for auto public post)<input type="password" name="zernio_api_key" placeholder="{{'Saved - leave blank to keep it' if social.tiktok.zernio else 'sk_... from zernio.com > API Keys'}}"></label>
          <label class="field">Posting mode<select name="mode" style="width:100%;max-width:none"><option value="draft" {{'selected' if social.tiktok.mode=='draft' else ''}}>Send to TikTok drafts (recommended)</option><option value="zernio" {{'selected' if social.tiktok.mode=='zernio' else ''}}>Auto PUBLIC post via Zernio (free, no click)</option><option value="private" {{'selected' if social.tiktok.mode=='private' else ''}}>Private post (Only me) - make public in TikTok Studio</option><option value="direct" {{'selected' if social.tiktok.mode=='direct' else ''}}>Direct Post (audited app only)</option></select></label>
          <label class="field">Direct Post privacy<select name="privacy" style="width:100%;max-width:none"><option value="SELF_ONLY" {{'selected' if social.tiktok.privacy=='SELF_ONLY' else ''}}>Private / Self only</option><option value="PUBLIC_TO_EVERYONE" {{'selected' if social.tiktok.privacy=='PUBLIC_TO_EVERYONE' else ''}}>Public</option><option value="MUTUAL_FOLLOW_FRIENDS" {{'selected' if social.tiktok.privacy=='MUTUAL_FOLLOW_FRIENDS' else ''}}>Friends</option></select></label>
        </div>
        <div class="row" style="margin-top:12px"><label class="chk"><input type="checkbox" name="auto_upload" {{'checked' if social.tiktok.auto_upload else ''}}> Automatically send new videos to TikTok (drafts or private post)</label><button class="green sm" type="submit">Save TikTok Settings</button></div>
      </form>
      <div class="row" style="margin-top:10px">
        <form method="post" action="/tiktok-login"><input type="hidden" name="channel" value="{{ch.id}}"><button class="green sm" type="submit">Login with TikTok</button></form>
        <form method="post" action="/social-verify"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="platform" value="tiktok"><button class="ghost sm" type="submit">Verify TikTok</button></form>
      </div>
      <details class="guide-box"{{' open' if not social.tiktok.configured else ''}}><summary>TikTok connect karne ki poori guide (pehli dafa ~30 minute; URLs aur copy-paste text ke saath)</summary>
        <p class="hint" style="margin-top:8px">Tarteeb yaad rakhein: <strong>Sandbox se connect &rarr; demo video record &rarr; Production submit</strong>. Production form demo video ke baghair save bhi nahi hota, aur review mein kuch din lagte hain. Sandbox mein review nahi hota, us se aaj hi drafts chalne lagte hain.</p>

        <h4>A. App banayein</h4><ol>
        <li><a href="https://developers.tiktok.com/apps/" target="_blank">developers.tiktok.com/apps</a> &rarr; <strong>Connect an app</strong> (ya maujooda app kholein). Ownership: Individual.</li>
        <li><strong>App icon:</strong> 1024&times;1024 PNG. Channel ka logo na ho to koi bhi saaf icon chalega.</li>
        <li><strong>Category:</strong> Entertainment.</li>
        <li><strong>Description</strong> (120 akshar tak):<code class="copy">Posts my own daily {{ch.genre or 'short'}} videos to my TikTok account as drafts, so I can review them before posting.</code></li>
        <li><strong>Terms of Service URL:</strong><code class="copy">{{tiktok_site}}terms.html</code><strong>Privacy Policy URL:</strong><code class="copy">{{tiktok_site}}privacy.html</code></li>
        <li><strong>Platforms:</strong> sirf &#9745; <strong>Desktop</strong>. <strong>Web/Desktop URL</strong> mein website ka link jata hai, redirect nahi:<code class="copy">{{tiktok_site}}</code></li>
        </ol>

        <h4>B. "This URL is not verified" ho to</h4><ol>
        <li>Upar dayein <strong>URL properties</strong> &rarr; <strong>Verify properties</strong> &rarr; type <strong>URL prefix</strong> (Domain nahi) &rarr; URL: <code>{{tiktok_site}}</code></li>
        <li>TikTok ki <code>tiktok&hellip;.txt</code> file download karein. <strong>Verify abhi na dabayein.</strong></li>
        <li>File bot ke <code>site_extra</code> folder mein rakhein, commit + push karein, phir GitHub Actions mein <strong>Status page</strong> workflow chalayein (2&ndash;3 minute). File ka link browser mein khul jaye to TikTok mein <strong>Verify</strong> dabayein.</li>
        <li>Verification har app aur har mode (Production / Sandbox) ki alag hoti hai. URL properties ke dropdown mein "for Production" / "for Sandbox" badal kar dekh lein. File ek dafa website par ho to baad mein sirf Verify dabana hota hai.</li>
        </ol>

        <h4>C. Products aur Scopes</h4><ol>
        <li><strong>Add products</strong> &rarr; pehle <strong>Login Kit</strong>, phir <strong>Content Posting API</strong>. Share Kit, Webhooks, Data Portability add na karein.</li>
        <li><strong>Login Kit &rarr; Redirect URI &rarr; Desktop*</strong> tab (Web tab nahi) mein bilkul yeh, aakhri <code>/</code> samet:<code class="copy">{{tiktok_redirect_uri}}</code></li>
        <li>Content Posting API mein <strong>Direct Post off</strong> rakhein. <strong>Verify domains</strong> ki zaroorat nahi, bot video file khud bhejta hai.</li>
        <li><strong>Scopes</strong> mein sirf <code>user.info.basic</code> aur <code>video.upload</code>. TikTok khud <code>user.info.profile</code>, <code>video.list</code> jaise extra scopes jor deta hai; unhe <strong>(&minus;)</strong> se hata dein, warna review late hota hai.</li>
        </ol>

        <h4>D. Sandbox (aaj hi connect karne ke liye)</h4><ol>
        <li>Upar <strong>Sandbox</strong> tab &rarr; <strong>Create sandbox</strong>. Sandbox mein bhi A (icon, category, description, links, Desktop) aur C dobara bharein.</li>
        <li><strong>Sandbox settings &rarr; Target Users &rarr; Add account</strong> &rarr; channel ka TikTok account. Is ke baghair login nahi hota.</li>
        <li>Upar laal <strong>Apply changes</strong> dabayein jab tak "Saved" na aa jaye. "Unsaved changes" likha ho to kuch save nahi hua.</li>
        </ol>

        <h4>E. Bot se connect</h4><ol>
        <li>Sandbox ki <strong>Credentials</strong> mein aankh ke icon se <strong>Client key</strong> aur <strong>Client secret</strong> copy karein (sandbox aur production ki keys alag hoti hain).</li>
        <li>Upar Client Key / Client Secret paste karein, Access/Refresh Token khaali chhorein &rarr; <strong>Save TikTok Settings</strong>.</li>
        <li><strong>Login with TikTok</strong> &rarr; channel ke TikTok account se login &rarr; <strong>Authorize</strong>. Upar "TikTok connected to: &hellip;" aa jayega. Login ~1 saal chalta hai; roz ka token bot khud renew karta hai.</li>
        <li><strong>Automatically send new videos to drafts</strong> tick &rarr; Save. Test: Video Library mein kisi video par <strong>Send TikTok</strong>, phir TikTok app &rarr; Inbox / Profile &rarr; Drafts.</li>
        <li><strong>Roz ki videos GitHub banata hai</strong>, is liye connect ke baad yeh login GitHub ko bhejna zaroori hai:<code class="copy">.venv\Scripts\python.exe scripts\setup_github_actions.py</code>Jab bhi TikTok dobara login ho ya keys badlein, yeh command phir chalayein.</li>
        </ol>

        <h4>F. Production review (public app)</h4><ol>
        <li><strong>Demo video</strong> (MP4, 50MB se kam): <kbd>Win</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd> se screen record. Dikhayein: yeh dashboard &rarr; TikTok panel &rarr; Login with TikTok &rarr; authorize &rarr; "connected" &rarr; Send TikTok &rarr; phone mein draft. Koi purani YouTube video na lagayein; us se app reject hoti hai.</li>
        <li><strong>"Explain how each product and scope works"</strong> mein:<code class="copy">This is a personal desktop tool that I run on my own computer to publish the short videos I create for my own TikTok account.
Login Kit (user.info.basic): I sign in with my own TikTok account so the tool can show which account is connected.
Content Posting API (video.upload): the tool sends my finished videos to my TikTok inbox as drafts. I then open the TikTok app, review each draft, and post it myself. Nothing is posted automatically and no other users' data is collected.</code></li>
        <li><strong>Submit for review</strong>. Approve hone ke baad production ki Client Key/Secret yahan paste karein, dobara <strong>Login with TikTok</strong>, aur E-5 wali command chalayein.</li>
        </ol>

        <h4>G. Bina click ke PUBLIC auto post (Zernio, free)</h4><ol>
        <li>TikTok apni app ko har post par insaan ke click ke baghair public post nahi karne deta. Zernio ki app TikTok se audit shuda hai, is liye woh API se seedha public post kar sakti hai. Pehle 2 accounts free, posts unlimited, card nahi chahiye.</li>
        <li><a href="https://zernio.com" target="_blank">zernio.com</a> par free account banayein &rarr; <strong>Accounts / Connect</strong> &rarr; <strong>TikTok</strong> &rarr; channel ke TikTok account se login karke allow karein.</li>
        <li>Zernio dashboard &rarr; <strong>API Keys</strong> &rarr; <strong>Create key</strong> &rarr; <code>sk_&hellip;</code> wali key copy karein (yeh sirf ek dafa dikhti hai).</li>
        <li>Upar <strong>Zernio API key</strong> mein paste karein, <strong>Posting mode</strong> = "Auto PUBLIC post via Zernio", auto wala box tick &rarr; <strong>Save TikTok Settings</strong> &rarr; <strong>Verify TikTok</strong> ("&hellip; (via Zernio)" aana chahiye).</li>
        <li>GitHub ko bhejein:<code class="copy">.venv\Scripts\python.exe scripts\setup_github_actions.py</code>Ab har nayi video YouTube ke saath TikTok par bhi khud public hogi, "AI-generated" label ke saath.</li>
        <li>Yaad rahe: khabar bina dekhe public hogi. Ghalat video ho to TikTok Studio &rarr; Posts se delete karein.</li>
        </ol>

        <h4>Masle aur hal</h4><ol>
        <li><strong>redirect_uri</strong> error: Login Kit ke Desktop tab ka URI upar wale se harf-ba-harf match nahi.</li>
        <li><strong>scope</strong> error: Scopes mein <code>video.upload</code> nahi, ya changes Apply nahi hue.</li>
        <li>Login page par account qubool na ho: Sandbox ke Target Users mein woh account nahi.</li>
        <li>"Please correct all errors before you save": koi lazmi box khaali hai (aksar page refresh se Description/Links mit jate hain), ya production mein demo video nahi.</li>
        <li>Official docs: <a href="{{social.guides.tiktok}}" target="_blank">Content Posting API</a> &middot; <a href="https://developers.tiktok.com/doc/login-kit-desktop" target="_blank">Login Kit (Desktop)</a></li>
        </ol>
      </details>
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
      {% if accounts|length > 1 %}
      <select onchange="location.href=this.value" title="Show videos of one YouTube channel">
        <option value="/?channel={{ch.id}}&amp;type={{selected_type}}&amp;view={{selected_view}}&amp;account=all" {{'selected' if selected_account=='all' else ''}}>All YouTube channels</option>
        {% for a in accounts %}<option value="/?channel={{ch.id}}&amp;type={{selected_type}}&amp;view={{selected_view}}&amp;account={{a.id}}" {{'selected' if selected_account==a.id else ''}}>{{a.name}}</option>{% endfor %}
      </select>
      {% endif %}
      <a class="tab {{'active' if selected_view=='all' and selected_type=='all' else ''}}" href="/?channel={{ch.id}}&amp;type=all&amp;view=all&amp;account={{selected_account}}">All</a>
      <a class="tab {{'active' if selected_view=='unuploaded' else ''}}" href="/?channel={{ch.id}}&amp;type=all&amp;view=unuploaded&amp;account={{selected_account}}">Unuploaded ({{upload_queue.unuploaded_count}})</a>
      <a class="tab {{'active' if selected_view=='all' and selected_type=='short' else ''}}" href="/?channel={{ch.id}}&amp;type=short&amp;view=all&amp;account={{selected_account}}">Shorts</a>
      <a class="tab {{'active' if selected_view=='all' and selected_type=='long' else ''}}" href="/?channel={{ch.id}}&amp;type=long&amp;view=all&amp;account={{selected_account}}">Long Videos</a>
    </div>
  </div>
  <div class="grid vids" id="video-grid" style="margin-top:12px">
    {% for v in videos %}
    {% set vs=social_uploads.get(v['id'], {}) %}
    <div class="card vid" data-video-card="{{v['id']}}">
      {% if v['drive_url'] %}
        <div class="card" style="text-align:center;padding:28px"><a href="{{v['drive_url']}}" target="_blank">☁ On Google Drive ↗</a></div>
      {% elif v['video_path'] %}<video controls preload="metadata" src="/media?path={{v['video_path']|urlencode}}"></video>{% endif %}
      <div class="t">{{v['title']}} <span class="chip">{{v['content_type']}}</span>{% if accounts|length > 1 %} <span class="chip">{{v['account']}}</span>{% endif %}</div>
      <div class="hint">Created: {{v['created_at']|localdt}}</div>
      <div class="row" style="justify-content:space-between;margin:2px 0 8px">
        <span class="st {{v['status']}}" data-video-status="{{v['id']}}">{{v['status']}}</span>
        {% if v['video_url'] %}<a href="{{v['video_url']}}" target="_blank">YouTube ↗</a>{% endif %}
      </div>
      <div class="upload-meter" data-video-progress="{{v['id']}}" style="display:{{'block' if v['status']=='uploading' else 'none'}};margin:8px 0">
        <div class="row" style="justify-content:space-between"><span class="hint">Uploading to YouTube</span><strong class="upload-percent">{{v['upload_progress']}}%</strong></div>
        <div class="bar" style="margin-top:5px"><i style="width:{{v['upload_progress']}}%"></i></div>
      </div>
      {% if upload_pause and not v['video_url'] and v['status'] in ('rendered','upload_failed','daily_upload_pending') %}
      <div style="margin:8px 0;padding:9px 10px;border:1px solid #705b20;border-radius:9px;background:#302912">
        <div class="row" style="justify-content:space-between"><span class="hint">Waiting for YouTube quota reset</span><strong>0%</strong></div>
        <div class="bar" style="margin-top:5px"><i style="width:0%"></i></div>
        <div class="hint">Automatic retry: {{upload_pause[1]|localdt}}. Video creation continues while uploads wait.</div>
      </div>
      {% endif %}
      {% if v['status']=='daily_upload_pending' %}<div class="hint" style="margin:6px 0;color:#9fc4ff"><strong>Daily direct upload:</strong> {% if upload_pause %}YouTube quota paused. Automatic retry: {{upload_pause[1]|localdt}}.{% else %}Ready; automatic upload retry will start shortly.{% endif %}</div>{% endif %}      {% if vs %}<div class="social-state">{% for platform,item in vs.items() %}<span class="chip {{item.status}}">{{platform|title}}: {{item.status}}</span>{% if item.post_url %}<a href="{{item.post_url}}" target="_blank">Open</a>{% endif %}{% endfor %}</div>{% endif %}      {% if v['status']=='scheduled' and v['publish_at'] %}<div class="hint"><strong>Goes public on YouTube:</strong> {{v['publish_at']|localdt}}</div>{% endif %}
      <div class="row">
        <a href="/media?path={{v['video_path']|urlencode}}" download><button class="ghost sm" type="button">Download</button></a>
        {% if upload_pause and not v['video_url'] and v['status'] in ('rendered','upload_failed','daily_upload_pending') %}<button class="ghost sm" type="button" disabled>Waiting for YouTube quota</button>{% elif v['status']=='daily_upload_pending' %}<form method="post" action="/upload-now"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><input type="hidden" name="type" value="{{selected_type}}"><button class="green sm" type="submit">Upload Now</button></form>{% elif v['status'] in ('awaiting_approval','rendered','upload_failed') %}<form method="post" action="/upload-now"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><input type="hidden" name="type" value="{{selected_type}}"><button class="green sm" type="submit">Upload to YouTube</button></form>{% elif v['status']=='thumbnail_pending' %}<form method="post" action="/upload-one"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><button class="green sm" type="submit">Retry Thumbnail</button></form>{% endif %}
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
    if(!running && window.__was){ window.__was=false; setTimeout(refreshVideos,900); }
    if(running) window.__was=true;
  }catch(e){}
}
setInterval(poll,1500); poll();

// ---------------------------------------------------------------- no reloads
// Clicking Upload Now used to POST, redirect, and reload the whole page, which
// threw the reader back to the top of a long video list. Action forms are now
// sent in the background and only the video grid is refreshed afterwards, so
// the scroll position and the open tab both survive.
function toast(message, kind){
  var box=document.getElementById('inline-toast');
  if(!box){
    box=document.createElement('div');
    box.id='inline-toast';
    box.style.cssText='position:fixed;left:50%;bottom:22px;transform:translateX(-50%);z-index:99;'+
      'padding:11px 18px;border-radius:10px;font-size:14px;box-shadow:0 6px 22px rgba(0,0,0,.45);max-width:min(560px,92vw)';
    document.body.appendChild(box);
  }
  box.style.background = kind==='error' ? '#4a1f22' : '#173a24';
  box.style.color = kind==='error' ? '#ffb4b4' : '#b7f5cd';
  box.textContent=message;
  box.style.display='block';
  clearTimeout(window.__toastTimer);
  window.__toastTimer=setTimeout(function(){ box.style.display='none'; }, 4500);
}

async function refreshVideos(){
  var grid=document.getElementById('video-grid');
  if(!grid) return;
  try{
    const response=await fetch(window.location.href, {headers:{'X-Requested-With':'fetch'}});
    const parsed=new DOMParser().parseFromString(await response.text(), 'text/html');
    const fresh=parsed.getElementById('video-grid');
    // A playing <video> would restart if its card were replaced, so keep any
    // card the reader has open and only swap the rest.
    const playing=grid.querySelector('video:not([paused])');
    if(fresh && !(playing && !playing.paused)) grid.innerHTML=fresh.innerHTML;
  }catch(e){}
}

document.addEventListener('submit', function(event){
  const form=event.target;
  if(form.method.toLowerCase()!=='post') return;
  const action=(form.getAttribute('action')||'');
  // Only the per-video action buttons; creation forms keep their normal flow
  // so their job cards and flash messages appear as before.
  const inline=['/upload-now','/upload-one','/delete-one','/regenerate-one','/social-upload'];
  if(!inline.includes(action)) return;
  event.preventDefault();
  const button=form.querySelector('button');
  const label=button?button.textContent:'';
  if(button){ button.disabled=true; button.textContent='Working...'; }
  fetch(action, {method:'POST', body:new FormData(form), headers:{'X-Requested-With':'fetch'}})
    .then(function(response){
      if(!response.ok) throw new Error('HTTP '+response.status);
      if(action==='/delete-one'){
        const card=form.closest('[data-video-card]');
        if(card) card.remove();
        toast('Video deleted.');
        return;
      }
      toast(action==='/upload-now' ? 'Uploading to YouTube now. It stays private until its scheduled release time.'
                                   : 'Done.');
      window.__was=true;   // let the poller refresh the grid when the job ends
      poll();
    })
    .catch(function(error){ toast('Failed: '+error.message, 'error'); })
    .finally(function(){ if(button){ button.disabled=false; button.textContent=label; } });
});
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
        if not dashboard_automation_on():
            return  # switched off on the dashboard; the finally block re-arms the timer
        from src.daily_runner import _can_publish, run_daily_catchup, run_daily_long_all
        from src.channels import get_channel, load_channels
        from src.pending_uploads import has_daily_pending, retry_daily_uploads

        active = any(
            job.get("status") in {"queued", "running"}
            and ("Auto daily" in job.get("label", "") or "generation" in job.get("label", "").lower())
            for job in JOBS.values()
        )
        # A channel with only a Facebook Page counts too: its videos go there.
        connected_channels = [channel for channel in load_channels() if _can_publish(channel)]
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
                for channel in [item for item in load_channels() if _can_publish(item)]:
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
                    for channel in [item for item in load_channels() if _can_publish(item)]
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
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    # The timers always run; each tick does nothing while the dashboard's
    # automation switch is off, so the switch works without a restart.
    queue_timer = threading.Timer(8.0, _schedule_auto_upload_queue)
    queue_timer.daemon = True
    queue_timer.start()
    # Check shortly after launch and then hourly. The checker only creates
    # missing daily targets, so restarts never duplicate completed videos.
    daily_timer = threading.Timer(5.0, _daily_automation_check)
    daily_timer.daemon = True
    daily_timer.start()
    if not dashboard_automation_on():
        print("This PC's automation is OFF: the daily GitHub run makes and uploads the videos; "
              "the dashboard only does what you click. Switch it in System.")
    app.run(host="127.0.0.1", port=PORT, debug=False)
