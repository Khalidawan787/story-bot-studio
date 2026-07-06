"""Story Bot STUDIO — the upgraded multi-channel dashboard (Codex bot).

Runs on port 8000 (the old simple bot uses 5000 — don't confuse them).
Dark theme, one tab per channel, and the full power features: 1203-topic kids
bank, batch, daily, retry queue, asset coverage, per-video upload/delete, and
free-Gemini genre topics. Reuses the mature backend.

Run:  .venv\\Scripts\\python.exe web_dashboard.py   -> opens http://127.0.0.1:8000
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, redirect, url_for, send_file, render_template_string, flash, jsonify, abort

from src.config import settings
from src.channels import load_channels, get_channel
from src.daily_runner import run_daily_batch, videos_generated_today_count
from src.pipeline import run_pipeline
from src.lessons import load_lesson_for, load_topics_for
from src.genre_topics import generate_genre_topics
from src.pending_uploads import retry_pending_uploads, upload_one
from src.image_assets import asset_stats, generate_missing_assets, generate_free_asset_pack
from src.db import delete_video, set_drive_url
from src import drive_storage

app = Flask(__name__)
app.secret_key = "story-bot-studio"

PORT = 8000
JOBS = {}


def channel_rows(channel_id: str) -> list[sqlite3.Row]:
    if not settings.db_path.exists():
        return []
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            """
            SELECT id, job_id, topic, title, video_path, thumbnail_path,
                   video_url, status, error, created_at, drive_url
            FROM videos WHERE channel = ? ORDER BY id DESC
            """,
            (channel_id,),
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


def storage_info() -> dict:
    return {
        "runs_mb": _dir_mb(settings.runs_dir),
        "assets_mb": _dir_mb(settings.root / "assets"),
        "drive": drive_storage.is_connected(),
    }


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
        JOBS[job_id]["detail"] = fn() or "Completed"
        JOBS[job_id]["status"] = "done"
    except Exception as e:
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["detail"] = str(e)


def start_job(label, fn):
    jid = f"job_{len(JOBS) + 1}"
    JOBS[jid] = {"label": label, "status": "queued", "detail": ""}
    threading.Thread(target=_run_job, args=(jid, fn), daemon=True).start()


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
    vids = channel_rows(ch.id)
    uploaded = sum(1 for v in vids if v["status"] == "uploaded")

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
        connected=ch.token_path.exists(),
        stats={"topics": len(topics), "videos": len(vids), "uploaded": uploaded,
               "today": videos_generated_today_count(ch.id)},
    )


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
        results = run_daily_batch(count=ch.topics_per_day, upload=upload, channel=ch)
        return "; ".join(f"{t}:{s}" for t, s in results)

    start_job(f"[{ch.id}] Daily ({ch.topics_per_day})", job)
    flash(f"Building {ch.topics_per_day} daily video(s)...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/gen-topics", methods=["POST"])
def gen_topics():
    ch = current_channel()
    count = int(request.form.get("count", "10"))
    scenes = int(request.form.get("scenes", "8"))

    def job():
        keys = generate_genre_topics(ch, count=count, scenes=scenes)
        return f"Added {len(keys)} topics"

    start_job(f"[{ch.id}] Writing {count} topics", job)
    flash(f"Writing {count} new {ch.genre} topics with free Gemini...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/retry", methods=["POST"])
def retry():
    ch = current_channel()

    def job():
        return " | ".join(retry_pending_uploads(limit=20)) or "Nothing pending"

    start_job(f"[{ch.id}] Retry pending uploads", job)
    flash("Retrying pending uploads...", "ok")
    return redirect(url_for("home", channel=ch.id))


@app.route("/upload-one", methods=["POST"])
def upload_one_route():
    ch = current_channel()
    vid = int(request.form.get("id"))

    def job():
        return upload_one(vid)

    start_job(f"[{ch.id}] Upload video #{vid}", job)
    flash("Uploading to YouTube...", "ok")
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


@app.route("/asset-free", methods=["POST"])
def asset_free():
    ch = current_channel()
    count = int(request.form.get("count", "50"))

    def job():
        created, msgs = generate_free_asset_pack(limit=count)
        return f"Created {created} free asset cards"

    start_job(f"[{ch.id}] Free asset pack ({count})", job)
    flash(f"Generating up to {count} free asset cards...", "ok")
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


@app.route("/status")
def status():
    return jsonify(JOBS)


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
  button.sm{padding:7px 11px;font-size:13px}button:hover{filter:brightness(1.08)}
  .chip{display:inline-block;padding:2px 9px;border-radius:6px;background:#2a3152;color:#c7cff0;font-size:12px;margin-left:6px}
  .flash{padding:12px 16px;border-radius:12px;margin-bottom:14px;border:1px solid}
  .flash.ok{background:#12301f;border-color:#1f6b45;color:#a9f0cd}.flash.error{background:#331617;border-color:#7a2b2f;color:#ffb9bd}
  #jobs .job{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 14px;border:1px solid var(--line);border-radius:10px;margin-bottom:8px;background:#161a2e}
  .badge{font-size:12px;padding:3px 10px;border-radius:999px;font-weight:600}
  .badge.queued{background:#2a2f4d;color:#b9c1e6}.badge.running{background:#3a2f12;color:var(--warn)}
  .badge.done{background:#123122;color:var(--ok)}.badge.error{background:#331617;color:var(--err)}
  .vids{grid-template-columns:repeat(auto-fill,minmax(280px,1fr))}
  .vid video{width:100%;max-height:360px;border-radius:12px;background:#000;display:block}
  .vid .t{font-weight:600;margin:8px 0 4px;font-size:14px}
  .st{font-size:12px;font-weight:600;padding:2px 8px;border-radius:6px}
  .st.uploaded{background:#123122;color:var(--ok)}.st.rendered{background:#2a2f4d;color:#b9c1e6}
  .st.thumbnail_pending{background:#3a2f12;color:var(--warn)}.st.upload_failed{background:#331617;color:var(--err)}
  .muted{color:var(--muted);font-size:13px}.err{color:var(--err);font-size:11px;margin-top:6px;max-height:48px;overflow:auto}
  .hint{font-size:12px;color:var(--muted);margin-top:8px}
  .bar{height:8px;background:#12162a;border-radius:999px;overflow:hidden;margin:8px 0}
  .bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--ok))}
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

  <div class="row" style="margin-bottom:14px">
    <span class="muted">Active:</span><strong>{{ch.name}}</strong>
    <span class="chip">{{ch.genre}}</span><span class="chip">voice: {{ch.voice}}</span>
    <span class="chip">{{'made-for-kids' if ch.made_for_kids else 'general'}}</span>
    <span class="chip">upload: {{ch.privacy}}</span>
  </div>

  <div class="card" style="margin-bottom:18px">
    <div class="row" style="justify-content:space-between">
      <div>
        <strong>YouTube connection</strong> —
        {% if connected %}<span style="color:var(--ok)">● Connected</span>{% else %}<span style="color:var(--warn)">● Not connected</span>{% endif %}
        <div class="hint">Click "Connect &amp; verify" and it will show which YouTube channel this is linked to. Make sure a genre channel is NOT linked to your kids channel.</div>
      </div>
      <div class="row">
        <form method="post" action="/connect"><input type="hidden" name="channel" value="{{ch.id}}"><button class="green sm" type="submit">🔗 Connect &amp; verify</button></form>
        {% if connected %}<form method="post" action="/disconnect" onsubmit="return confirm('Disconnect this channel?')"><input type="hidden" name="channel" value="{{ch.id}}"><button class="ghost sm" type="submit">Disconnect</button></form>{% endif %}
      </div>
    </div>
  </div>

  <div class="grid stats">
    <div class="card stat"><div class="n">{{stats.topics}}</div><div class="l">Topics</div></div>
    <div class="card stat"><div class="n">{{stats.videos}}</div><div class="l">Videos</div></div>
    <div class="card stat"><div class="n">{{stats.uploaded}}</div><div class="l">Uploaded</div></div>
    <div class="card stat"><div class="n">{{stats.today}}</div><div class="l">Today</div></div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">🎬 Generate — {{ch.name}}</h2>
    <form method="post" action="/generate" class="row">
      <input type="hidden" name="channel" value="{{ch.id}}">
      <select name="topic" required>
        <option value="" disabled selected>Choose a topic ({{stats.topics}} available)...</option>
        {% for k,t in topics %}<option value="{{k}}">{{t}}</option>{% endfor %}
      </select>
      <label class="chk"><input type="checkbox" name="upload"> Upload</label>
      <button type="submit">Generate video</button>
    </form>
    <div class="row" style="margin-top:12px">
      <form method="post" action="/batch" class="row">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <input type="number" name="count" value="3" min="1" max="20" style="width:76px">
        <label class="chk"><input type="checkbox" name="upload"> upload</label>
        <button class="ghost" type="submit">⚡ Batch</button>
      </form>
      <form method="post" action="/daily"><input type="hidden" name="channel" value="{{ch.id}}"><button class="pink" type="submit">▶ Daily ({{ch.topics_per_day}})</button></form>
      <form method="post" action="/retry"><input type="hidden" name="channel" value="{{ch.id}}"><button class="ghost" type="submit">↻ Retry uploads</button></form>
      {% if not ch.builtin %}
      <form method="post" action="/gen-topics" class="row">
        <input type="hidden" name="channel" value="{{ch.id}}">
        <input type="number" name="count" value="10" min="1" max="30" style="width:70px" title="how many topics">
        <input type="number" name="scenes" value="5" min="4" max="40" style="width:70px" title="scenes per video (5≈30s)">
        <button type="submit">✨ Write topics</button>
      </form>
      {% endif %}
    </div>
    {% if ch.builtin %}<div class="hint">Kids topics come from the built-in 1203-topic bank.</div>
    {% else %}<div class="hint">Write topics first (5 scenes ≈ 30-sec video). Authorize this channel's YouTube before uploading.</div>{% endif %}
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
      <form method="post" action="/asset-free" class="row"><input type="hidden" name="channel" value="{{ch.id}}"><input type="number" name="count" value="50" min="1" max="200" style="width:76px"><button class="ghost sm" type="submit">Free asset pack (offline)</button></form>
    </div>
  </div>
  {% endif %}

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
  <div id="jobs" class="card"><div class="muted" id="jobs-empty">No jobs yet.</div></div>

  <h2>🎞 Videos — {{ch.name}}</h2>
  <div class="grid vids">
    {% for v in videos %}
    <div class="card vid">
      {% if v['drive_url'] %}
        <div class="card" style="text-align:center;padding:28px"><a href="{{v['drive_url']}}" target="_blank">☁ On Google Drive ↗</a></div>
      {% elif v['video_path'] %}<video controls preload="metadata" src="/media?path={{v['video_path']|urlencode}}"></video>{% endif %}
      <div class="t">{{v['title']}}</div>
      <div class="row" style="justify-content:space-between;margin:2px 0 8px">
        <span class="st {{v['status']}}">{{v['status']}}</span>
        {% if v['video_url'] %}<a href="{{v['video_url']}}" target="_blank">YouTube ↗</a>{% endif %}
      </div>
      <div class="row">
        <a href="/media?path={{v['video_path']|urlencode}}" download><button class="ghost sm" type="button">Download</button></a>
        <form method="post" action="/upload-one"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><button class="green sm" type="submit">Upload to YouTube</button></form>
        <form method="post" action="/delete-one" onsubmit="return confirm('Delete this video?')"><input type="hidden" name="channel" value="{{ch.id}}"><input type="hidden" name="id" value="{{v['id']}}"><button class="red sm" type="submit">Delete</button></form>
      </div>
      {% if v['error'] %}<div class="err">{{v['error'][:180]}}</div>{% endif %}
    </div>
    {% else %}<div class="muted">No videos yet for {{ch.name}}.</div>{% endfor %}
  </div>
</div>
<script>
async function poll(){
  try{
    const r=await fetch('/status'); const jobs=await r.json();
    const box=document.getElementById('jobs'); const keys=Object.keys(jobs);
    const empty=document.getElementById('jobs-empty');
    if(keys.length===0){ if(empty) empty.style.display='block'; return; }
    if(empty) empty.style.display='none';
    let running=false;
    box.innerHTML=keys.reverse().map(k=>{const j=jobs[k];
      if(j.status==='running'||j.status==='queued') running=true;
      const d=j.detail?' — <span class="muted">'+String(j.detail).slice(0,90)+'</span>':'';
      return '<div class="job"><div>'+j.label+d+'</div><span class="badge '+j.status+'">'+j.status+'</span></div>';}).join('');
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


def _startup_daily():
    """On launch, make sure today's 5 KIDS videos exist (and upload if online).

    Only the kids channel auto-runs — genre channels never auto-upload.
    Catch-up ensures a max of 5 per day no matter how often you open it.
    """
    try:
        from src.daily_runner import run_daily_catchup
        from src.channels import get_channel
        online = _online()

        def job():
            results = run_daily_catchup(target_count=5, upload=online, start_hour=0,
                                        channel=get_channel("kids"))
            return "; ".join(f"{t}:{s}" for t, s in results)

        start_job(f"Auto daily-5 (kids){'' if online else ' — offline, render only'}", job)
    except Exception as e:
        print("startup daily skipped:", e)


if __name__ == "__main__":
    import webbrowser
    print(f"Story Bot STUDIO: http://127.0.0.1:{PORT}  (close this window to stop)")
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    # Auto-build today's 5 kids videos on launch (set STUDIO_NO_AUTODAILY=1 to skip).
    if not os.environ.get("STUDIO_NO_AUTODAILY"):
        threading.Timer(3.0, _startup_daily).start()
    app.run(host="127.0.0.1", port=PORT, debug=False)
