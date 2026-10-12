"""Build the public status page that GitHub Pages serves.

The dashboard is a live server and cannot run on GitHub, so this writes a
static page instead: the last run and how each channel did in it (failures
included, with the reason), every video of the last day and where it stands,
what goes public next, the last seven days, each channel's daily budget, and
anything that needs a look. The daily workflow regenerates it after every run;
the "Status page" workflow rebuilds it on demand without making videos.

The page is public, so it shows no email addresses, keys or file paths.

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
from src import run_log, slots  # noqa: E402

PENDING_STATUSES = ("rendered", "upload_failed", "daily_upload_pending", "thumbnail_pending")
LOCAL_TZ_OFFSET = timedelta(hours=5)  # Pakistan, for human-readable times

# Database status -> (plain words, colour class) for the public page.
STATUS_WORDS = {
    "scheduled": ("Scheduled on YouTube", "ok"),
    "uploaded": ("Public on YouTube", "ok"),
    "uploading": ("Uploading", "warn"),
    "daily_upload_pending": ("Built · waiting for upload quota", "warn"),
    "rendered": ("Built · not uploaded", "warn"),
    "thumbnail_pending": ("Uploaded · thumbnail pending", "warn"),
    "upload_failed": ("Upload failed", "bad"),
    "quality_failed": ("Failed the quality check", "bad"),
    "duplicate_blocked": ("Blocked: duplicate", "muted"),
    "remote_missing": ("Missing on YouTube", "bad"),
    "awaiting_approval": ("Waiting for approval", "warn"),
}
VISUAL_WORDS = {"stock": "real video clips", "cartoon": "animated cartoon", "still": "AI pictures"}


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


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _clean(text: str | None, limit: int = 200) -> str:
    return run_log._clean(text or "")[:limit]


def _visuals(channel) -> str:
    if getattr(channel, "visuals", ""):
        return VISUAL_WORDS.get(channel.visuals, channel.visuals)
    if channel.id in settings.stock_video_channels:
        return VISUAL_WORDS["stock"]
    if channel.id in settings.animated_channels:
        return VISUAL_WORDS["cartoon"]
    return VISUAL_WORDS["still"]


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
        now = datetime.now(timezone.utc)
        attempts = [a for a in run_log.read() if (_parse(a.get("at")) or now) > now - timedelta(days=3)]
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
            mine = [a for a in attempts if a.get("channel") == channel.id]
            channels.append({
                "id": channel.id,
                "name": channel.name,
                "accounts": accounts,
                "pending": int(pending[0]["n"]) if pending else 0,
                "visuals": _visuals(channel),
                "slot": slots.label(slots.channel_slot(channel)),
                "last": mine[-1] if mine else None,
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
        # Every video of the last 24 hours, uploaded or not.
        window = now - timedelta(hours=24)
        latest_rows = _rows(
            conn,
            """SELECT title, channel, status, video_url, publish_at, created_at,
                      COALESCE(content_type,'short') AS content_type,
                      substr(COALESCE(error,''),1,300) AS error
               FROM videos WHERE hidden_at IS NULL ORDER BY id DESC LIMIT 80""",
        )
        last_day = [r for r in latest_rows if (_parse(r["created_at"]) or window) > window]

        upcoming = [
            r for r in _rows(
                conn,
                # Newest rows, then filtered and sorted here: hundreds of old
                # scheduled rows already in the past would otherwise fill any
                # "earliest first" limit and leave the list empty.
                """SELECT title, channel, video_url, publish_at,
                          COALESCE(content_type,'short') AS content_type
                   FROM videos WHERE status = 'scheduled' AND publish_at IS NOT NULL
                     AND hidden_at IS NULL ORDER BY id DESC LIMIT 200""",
            )
            if (_parse(r["publish_at"]) or now) > now
        ]
        upcoming = sorted(upcoming, key=lambda r: _parse(r["publish_at"]))[:10]

        # Uploads per day (Pakistan time) for the last seven days, per channel.
        week_start = (now + LOCAL_TZ_OFFSET).date() - timedelta(days=6)
        week: dict = {week_start + timedelta(days=i): {} for i in range(7)}
        for r in _rows(
            conn,
            """SELECT channel, upload_date FROM videos
               WHERE video_url IS NOT NULL AND upload_date IS NOT NULL
               ORDER BY id DESC LIMIT 400""",
        ):
            moment = _parse(r["upload_date"])
            if not moment:
                continue
            day = (moment.astimezone(timezone.utc) + LOCAL_TZ_OFFSET).date()
            if day in week:
                week[day][r["channel"]] = week[day].get(r["channel"], 0) + 1

        last_run = attempts[-1] if attempts else None
        return {
            "last_run": last_run,
            "run_attempts": [a for a in attempts if last_run and a.get("run") == last_run.get("run")],
            "failures": [a for a in reversed(attempts) if not a.get("ok")][:8],
            "last_day": last_day,
            "upcoming": upcoming,
            "week": week,
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
    last = channel.get("last")
    if last:
        state = "ok" if last.get("ok") else "bad"
        word = "Last video built" if last.get("ok") else "Last attempt failed"
        reason = "" if last.get("ok") else f"<div class='reason'>{html.escape(str(last.get('detail') or ''))}</div>"
        last_html = (f"<div class='last {state}'><b>{word}</b> · {_local(last.get('at'))}"
                     f"{reason}</div>")
    else:
        last_html = "<div class='muted'>No attempt in the last 3 days</div>"
    return f"""
    <section class="card">
      <h2>{html.escape(channel['name'])}</h2>
      {''.join(rows)}
      <div class="muted">Made daily at {html.escape(channel.get('slot', ''))} · pictures: {html.escape(channel.get('visuals', ''))} · {note}</div>
      {last_html}
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


def _status(status: str) -> str:
    words, cls = STATUS_WORDS.get(status, (status.replace("_", " "), "muted"))
    return f"<span class='st {cls}'>{html.escape(words)}</span>"


def _last_run(data: dict) -> str:
    last = data.get("last_run")
    if not last:
        return ""
    attempts = data.get("run_attempts") or []
    good = sum(1 for a in attempts if a.get("ok"))
    bad = len(attempts) - good
    cls = "bad" if bad and not good else ("warn" if bad else "ok")
    words = (f"{good} video{'s' if good != 1 else ''} built" + (f", {bad} failed" if bad else ""))
    link = (f" · <a href='{html.escape(last['run'])}' target='_blank' rel='noopener'>open the run log ↗</a>"
            if last.get("run") else "")
    where = "on GitHub" if last.get("where") == "github" else "on the PC"
    return f"""
    <section class="card run {cls}">
      <h2>Last run</h2>
      <div class="run-line"><b>{words}</b> · {_local(last.get('at'))} PKT, {where}{link}</div>
    </section>"""


def _last_day(rows: list) -> str:
    if not rows:
        return "<div class='muted'>No video was made in the last 24 hours.</div>"
    items = []
    for r in rows:
        when = _local(r["publish_at"]) if r["status"] == "scheduled" and r["publish_at"] else _local(r["created_at"])
        title = html.escape(str(r["title"] or "Untitled"))
        title = (f"<a href='{html.escape(r['video_url'])}' target='_blank' rel='noopener'>{title}</a>"
                 if r["video_url"] else title)
        error = (f"<div class='reason'>{html.escape(_clean(r['error']))}</div>"
                 if r["error"] and STATUS_WORDS.get(r["status"], ("", "bad"))[1] in {"bad", "warn"} else "")
        items.append(
            f"<tr><td>{title}<div class='vid-meta'><span class='chip'>{html.escape(r['channel'])}</span>"
            f"<span class='chip'>{html.escape(r['content_type'])}</span></div>{error}</td>"
            f"<td>{_status(r['status'])}<div class='muted'>{when}</div></td></tr>"
        )
    return f"<div class='tbl'><table>{''.join(items)}</table></div>"


def _upcoming(rows: list) -> str:
    if not rows:
        return "<div class='muted'>Nothing scheduled.</div>"
    return "<div class='tbl'><table>" + "".join(
        f"<tr><td><a href='{html.escape(r['video_url'] or '#')}' target='_blank' rel='noopener'>"
        f"{html.escape(str(r['title'] or 'Untitled'))}</a>"
        f"<div class='vid-meta'><span class='chip'>{html.escape(r['channel'])}</span></div></td>"
        f"<td class='when'>{_local(r['publish_at'])}</td></tr>"
        for r in rows
    ) + "</table></div>"


def _week(week: dict, channels: list) -> str:
    names = [c["id"] for c in channels]
    head = "".join(f"<th>{html.escape(n)}</th>" for n in names)
    body = []
    for day, counts in week.items():
        total = sum(counts.values())
        cells = "".join(f"<td>{counts.get(n, 0) or '·'}</td>" for n in names)
        body.append(f"<tr><td>{day.strftime('%a %d %b')}</td>{cells}<td><b>{total}</b></td></tr>")
    return (f"<div class='tbl'><table class='week'><tr><th>Day</th>{head}<th>Total</th></tr>"
            f"{''.join(body)}</table></div>")


def _failures(rows: list) -> str:
    if not rows:
        return ""
    items = "".join(
        f"<li><b>{html.escape(a.get('channel', ''))}</b> · {_local(a.get('at'))}"
        f"<div class='reason'>{html.escape(str(a.get('detail') or ''))}</div></li>"
        for a in rows
    )
    return f"""
    <section class="card problems">
      <h2>Failed attempts (last 3 days)</h2>
      <ul>{items}</ul>
    </section>"""


def render(data: dict) -> str:
    from pwa_files import HEAD_TAGS as pwa_head, INSTALL_BAR as install_bar
    now = datetime.now(timezone.utc)
    updated = (now + LOCAL_TZ_OFFSET).strftime("%d %b %Y, %I:%M %p")
    next_local = (slots.next_run(now) + LOCAL_TZ_OFFSET).strftime("%d %b, %I:%M %p")

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
{pwa_head}
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
  .run {{ border-left:4px solid var(--ok) }}
  .run.warn {{ border-left-color:var(--warn) }} .run.bad {{ border-left-color:var(--bad) }}
  .last {{ margin-top:10px; font-size:13px; padding:8px 10px; border-radius:8px; background:#0f1428 }}
  .last.ok b {{ color:var(--ok) }} .last.bad b {{ color:var(--bad) }}
  .reason {{ color:var(--muted); font-size:12px; margin-top:3px; word-break:break-word }}
  .tbl {{ overflow-x:auto }}
  table {{ width:100%; border-collapse:collapse; font-size:14px }}
  td, th {{ padding:8px 6px; border-top:1px solid var(--line); vertical-align:top; text-align:left }}
  tr:first-child td, tr:first-child th {{ border-top:0 }}
  td.when {{ white-space:nowrap; color:var(--muted); font-size:13px }}
  table.week td, table.week th {{ text-align:center; white-space:nowrap }}
  table.week td:first-child, table.week th:first-child {{ text-align:left }}
  .st {{ font-size:12px; font-weight:600; white-space:nowrap }}
  .st.ok {{ color:var(--ok) }} .st.warn {{ color:var(--warn) }} .st.bad {{ color:var(--bad) }} .st.muted {{ color:var(--muted) }}
  footer {{ max-width:900px; margin:22px auto 8px; color:var(--muted); font-size:13px }}
  a {{ color:#8fc7ff }}
</style></head><body>
{install_bar}
<header class="wrap">
  <h1>Story Bot Studio</h1>
  <div class="muted">Updated {updated} PKT · next run {next_local}</div>
</header>

<div class="wrap">
  {_last_run(data)}

  <section class="card">
    <h2>Uploaded today</h2>
    <div class="today">{data['today']}</div>
    <div class="muted">Since the YouTube quota reset (midnight Pacific)</div>
  </section>

  <h2>Channels</h2>
  <div class="grid">{''.join(_card(c) for c in data['channels'])}</div>

  {_failures(data.get('failures') or [])}
  {problems}

  <section class="card">
    <h2>Last 24 hours</h2>
    {_last_day(data.get('last_day') or [])}
  </section>

  <section class="card">
    <h2>Going public next</h2>
    {_upcoming(data.get('upcoming') or [])}
  </section>

  <section class="card">
    <h2>Last 7 days (uploads)</h2>
    {_week(data.get('week') or {{}}, data['channels'])}
  </section>

  <section class="card">
    <h2>Latest videos</h2>
    {videos}
  </section>
</div>

<footer class="wrap">
  Built automatically by the daily GitHub Actions run. This page only reports —
  to build or upload something, use <b>Actions → Daily videos → Run workflow</b>.<br>
  <a href="privacy.html">Privacy policy</a> · <a href="terms.html">Terms of service</a>
</footer>
</body></html>
"""


def markdown(data: dict) -> str:
    """The same picture as a table, for the run's summary page on GitHub."""
    lines = [f"## Uploaded today: **{data['today']}**", ""]
    attempts = data.get("run_attempts") or []
    if attempts:
        lines += ["| Channel | This run | Detail |", "|---|---|---|"]
        for a in attempts:
            lines.append(f"| {a.get('channel')} | {'✅ built' if a.get('ok') else '❌ failed'} "
                         f"| {str(a.get('title') or a.get('detail') or '')[:100]} |")
        lines.append("")
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
    # Home, privacy and terms links for the Google sign-in screen.
    import legal_pages

    legal_pages.write(out_dir)
    # Manifest, icons and service worker: installable as a phone app.
    import pwa_files

    pwa_files.write(out_dir)
    # Stops GitHub Pages running the file through Jekyll, which would mangle it.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Wrote {out_dir / 'index.html'} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
