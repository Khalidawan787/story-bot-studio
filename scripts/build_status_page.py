"""Build the public status page that GitHub Pages serves.

The dashboard is a live server and cannot run on GitHub, so this writes a small
static page instead: what was built today, what reached YouTube, how much of
each channel's daily budget is left, and anything that needs a look. The daily
workflow regenerates it after every run.

    python scripts/build_status_page.py site

Reads the same bot.sqlite3 the bot uses, so the page can never disagree with it.
"""
from __future__ import annotations

import html
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.channels import load_channels  # noqa: E402
from src.config import settings  # noqa: E402
from src.db import youtube_quota_day_start  # noqa: E402
from src.pending_uploads import upload_limit_for_channel, uploads_today_count  # noqa: E402
from src.youtube_accounts import list_accounts  # noqa: E402

PENDING_STATUSES = ("rendered", "upload_failed", "daily_upload_pending", "thumbnail_pending")
LOCAL_TZ_OFFSET = timedelta(hours=5)  # Pakistan, for human-readable times


def _local(value: str | None) -> str:
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return "—"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment.astimezone(timezone.utc) + LOCAL_TZ_OFFSET).strftime("%d %b, %I:%M %p")


def _video_id(url: str | None) -> str:
    if not url:
        return ""
    if "watch?v=" in url:
        return url.split("watch?v=")[-1].split("&")[0]
    return url.rstrip("/").split("/")[-1]


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


def collect() -> dict:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        day_start = youtube_quota_day_start().isoformat()
        channels = []
        for channel in load_channels():
            accounts = []
            for account in list_accounts(channel.id):
                accounts.append({
                    "id": account.id,
                    "name": account.label,
                    "connected": account.connected,
                    "used": uploads_today_count(channel.id, account.id),
                    "limit": upload_limit_for_channel(channel.id, account.id),
                })
            if not any(a["connected"] for a in accounts):
                continue  # a channel nobody signed in to has nothing to report
            placeholders = ",".join("?" for _ in PENDING_STATUSES)
            pending = _rows(
                conn,
                f"""SELECT COUNT(*) AS n FROM videos
                    WHERE channel = ? AND status IN ({placeholders})
                      AND video_url IS NULL AND hidden_at IS NULL""",
                (channel.id, *PENDING_STATUSES),
            )
            channels.append({
                "id": channel.id,
                "name": channel.name,
                "accounts": accounts,
                "pending": int(pending[0]["n"]) if pending else 0,
            })

        recent = _rows(
            conn,
            """SELECT title, channel, COALESCE(account,'main') AS account, status,
                      video_url, publish_at, upload_date, created_at,
                      COALESCE(content_type,'short') AS content_type
               FROM videos
               WHERE video_url IS NOT NULL AND hidden_at IS NULL
               ORDER BY upload_date DESC LIMIT 12""",
        )
        today_uploads = _rows(
            conn,
            "SELECT COUNT(*) AS n FROM videos WHERE video_url IS NOT NULL AND upload_date >= ?",
            (day_start,),
        )
        problems = _rows(
            conn,
            """SELECT title, channel, status, substr(COALESCE(error,''),1,180) AS error
               FROM videos
               WHERE status IN ('upload_failed','quality_failed')
                 AND video_url IS NULL AND hidden_at IS NULL
               ORDER BY id DESC LIMIT 6""",
        )
        return {
            "channels": channels,
            "recent": recent,
            "today": int(today_uploads[0]["n"]) if today_uploads else 0,
            "problems": problems,
        }
    finally:
        conn.close()


def _card(channel: dict) -> str:
    rows = []
    for account in channel["accounts"]:
        used, limit = account["used"], account["limit"]
        pct = min(100, int(used / limit * 100)) if limit else 0
        state = "full" if limit and used >= limit else "ok"
        rows.append(f"""
        <div class="acct">
          <div class="acct-top">
            <span class="acct-name">{html.escape(account['name'])}</span>
            <span class="acct-count {state}">{used} / {limit or '∞'}</span>
          </div>
          <div class="bar"><i style="width:{pct}%"></i></div>
          {'' if account['connected'] else '<div class="warn">not connected</div>'}
        </div>""")
    pending = channel["pending"]
    note = f"{pending} waiting for tomorrow" if pending else "nothing waiting"
    return f"""
    <section class="card">
      <h2>{html.escape(channel['name'])}</h2>
      {''.join(rows)}
      <div class="muted">{note}</div>
    </section>"""


def _video(row: sqlite3.Row) -> str:
    vid = _video_id(row["video_url"])
    thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else ""
    scheduled = row["status"] == "scheduled" and row["publish_at"]
    when = _local(row["publish_at"]) if scheduled else _local(row["upload_date"])
    label = "goes public" if scheduled else "published"
    return f"""
    <a class="vid" href="{html.escape(row['video_url'] or '#')}" target="_blank" rel="noopener">
      {'<img loading="lazy" src="' + thumb + '" alt="">' if thumb else '<div class="noimg"></div>'}
      <div class="vid-body">
        <div class="vid-title">{html.escape(str(row['title'] or 'Untitled'))}</div>
        <div class="vid-meta">
          <span class="chip">{html.escape(row['channel'])}</span>
          <span class="chip">{html.escape(row['content_type'])}</span>
          <span class="muted">{label} {when}</span>
        </div>
      </div>
    </a>"""


def render(data: dict) -> str:
    now = datetime.now(timezone.utc)
    updated = (now + LOCAL_TZ_OFFSET).strftime("%d %b %Y, %I:%M %p")
    next_run = now.replace(hour=2, minute=30, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    next_local = (next_run + LOCAL_TZ_OFFSET).strftime("%d %b, %I:%M %p")

    problems = ""
    if data["problems"]:
        items = "".join(
            f"<li><b>{html.escape(p['channel'])}</b> — {html.escape(str(p['title'] or ''))[:60]}"
            f"<div class='muted'>{html.escape(str(p['error'] or p['status']))}</div></li>"
            for p in data["problems"]
        )
        problems = f"""
    <section class="card problems">
      <h2>Needs a look</h2>
      <ul>{items}</ul>
    </section>"""

    videos = "".join(_video(row) for row in data["recent"]) or \
        "<div class='muted'>Nothing uploaded yet.</div>"

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Story Bot Studio — status</title>
<style>
  :root {{ --bg:#0d1020; --card:#151a30; --line:#252c48; --text:#e8ecff;
           --muted:#8e97be; --ok:#4ade80; --warn:#ffd27a; --bad:#ff8a8a; }}
  * {{ box-sizing:border-box }}
  body {{ margin:0; padding:16px; background:var(--bg); color:var(--text);
          font:16px/1.5 -apple-system,Segoe UI,Roboto,system-ui,sans-serif; }}
  header {{ max-width:900px; margin:0 auto 18px }}
  h1 {{ font-size:22px; margin:0 0 4px }}
  h2 {{ font-size:16px; margin:0 0 12px; color:var(--muted);
        text-transform:uppercase; letter-spacing:.06em }}
  .wrap {{ max-width:900px; margin:0 auto }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px;
           padding:16px; margin-bottom:14px }}
  .grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)) }}
  .today {{ font-size:34px; font-weight:700; line-height:1 }}
  .acct {{ margin-bottom:12px }}
  .acct-top {{ display:flex; justify-content:space-between; gap:8px; margin-bottom:6px }}
  .acct-name {{ font-weight:600 }}
  .acct-count {{ font-variant-numeric:tabular-nums; color:var(--ok) }}
  .acct-count.full {{ color:var(--warn) }}
  .bar {{ height:6px; background:#0c1023; border-radius:99px; overflow:hidden }}
  .bar i {{ display:block; height:100%; background:linear-gradient(90deg,#4f7dff,#7ab8ff) }}
  .muted {{ color:var(--muted); font-size:13px }}
  .warn {{ color:var(--warn); font-size:13px; margin-top:4px }}
  .vid {{ display:flex; gap:12px; padding:10px; border-radius:12px;
          text-decoration:none; color:inherit }}
  .vid:hover {{ background:#1b2140 }}
  .vid img, .noimg {{ width:120px; height:68px; object-fit:cover; border-radius:8px;
                      background:#0c1023; flex:0 0 auto }}
  .vid-title {{ font-size:14px; font-weight:600; margin-bottom:4px }}
  .vid-meta {{ display:flex; flex-wrap:wrap; gap:6px; align-items:center }}
  .chip {{ background:#25305c; color:#bcd0ff; border-radius:99px;
           padding:1px 9px; font-size:12px }}
  .problems li {{ margin-bottom:10px }}
  footer {{ max-width:900px; margin:22px auto 8px; color:var(--muted); font-size:13px }}
  a {{ color:#8fc7ff }}
</style></head><body>
<header class="wrap">
  <h1>Story Bot Studio</h1>
  <div class="muted">Updated {updated} PKT · next run {next_local}</div>
</header>

<div class="wrap">
  <section class="card">
    <h2>Uploaded today</h2>
    <div class="today">{data['today']}</div>
    <div class="muted">Since the YouTube quota reset (midnight Pacific)</div>
  </section>

  <h2>Channels</h2>
  <div class="grid">{''.join(_card(c) for c in data['channels'])}</div>

  {problems}

  <section class="card">
    <h2>Latest videos</h2>
    {videos}
  </section>
</div>

<footer class="wrap">
  Built automatically by the daily GitHub Actions run. This page only reports —
  to build or upload something, use <b>Actions → Daily videos → Run workflow</b>.
</footer>
</body></html>
"""


def markdown(data: dict) -> str:
    """The same picture as a table, for the run's summary page on GitHub."""
    lines = [f"## Uploaded today: **{data['today']}**", ""]
    lines += ["| Channel | YouTube channel | Today | Waiting |", "|---|---|---|---|"]
    for channel in data["channels"]:
        for index, account in enumerate(channel["accounts"]):
            waiting = channel["pending"] if index == 0 else ""
            lines.append(
                f"| {channel['name'] if index == 0 else ''} | {account['name']} "
                f"| {account['used']} / {account['limit'] or '∞'} | {waiting} |"
            )
    if data["recent"]:
        lines += ["", "### Latest videos", ""]
        for row in data["recent"][:8]:
            when = _local(row["publish_at"]) if row["status"] == "scheduled" else _local(row["upload_date"])
            state = "goes public" if row["status"] == "scheduled" else "published"
            lines.append(f"- [{str(row['title'] or 'Untitled')[:70]}]({row['video_url']}) "
                         f"— `{row['channel']}` · {state} {when}")
    if data["problems"]:
        lines += ["", "### Needs a look", ""]
        for problem in data["problems"]:
            lines.append(f"- `{problem['channel']}` {str(problem['title'] or '')[:50]} — "
                         f"{str(problem['error'] or problem['status'])[:120]}")
    return "\n".join(lines) + "\n"


def main() -> None:
    if "--summary" in sys.argv:
        print(markdown(collect()))
        return
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "site")
    out_dir.mkdir(parents=True, exist_ok=True)
    page = render(collect())
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    # Stops GitHub Pages running the file through Jekyll, which would mangle it.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Wrote {out_dir / 'index.html'} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
