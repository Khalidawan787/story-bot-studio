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
import re
import shutil
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
from src.social_accounts import account as social_account, facebook_auto_ready  # noqa: E402
from src.youtube_accounts import list_accounts  # noqa: E402
from src import run_log, slots  # noqa: E402

PENDING_STATUSES = ("rendered", "upload_failed", "daily_upload_pending", "thumbnail_pending")
LOCAL_TZ_OFFSET = timedelta(hours=5)  # Pakistan, for human-readable times

# Database status -> (plain words, colour class) for the public page.
STATUS_WORDS = {
    "scheduled": ("Scheduled on YouTube", "ok"),
    "uploaded": ("Public on YouTube", "ok"),
    "uploading": ("Uploading", "warn"),
    "daily_upload_pending": ("Built · waiting to upload", "warn"),
    "files_lost": ("Not uploaded · video files expired", "bad"),
    "rendered": ("Built · not uploaded", "warn"),
    "facebook_only": ("Facebook only · no YouTube channel yet", "ok"),
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
    # The page is public: a folder path on the runner or the PC is reduced to
    # its file name ("Thumbnail missing: thumbnail.jpg").
    text = re.sub(r"(?:[A-Za-z]:)?(?:[\\/][\w.\- ]+){2,}[\\/]([\w.\-]+)", r"\1", text or "")
    return run_log._clean(text)[:limit]


def _visuals(channel) -> str:
    if getattr(channel, "visuals", ""):
        return VISUAL_WORDS.get(channel.visuals, channel.visuals)
    from src.stock_video import stock_video_wanted

    if stock_video_wanted(channel):
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
            youtube = any(a["connected"] for a in accounts)
            facebook = facebook_auto_ready(channel.id, anywhere=True)
            if not youtube and not facebook:
                continue  # a channel that publishes nowhere has nothing to report
            latest = _rows(
                conn,
                """SELECT id, title, status, created_at, error, video_url, publish_at FROM videos
                   WHERE channel = ? AND hidden_at IS NULL ORDER BY id DESC LIMIT 1""",
                (channel.id,),
            )
            tiktok_cfg = social_account(channel.id, "tiktok")
            tiktok = bool(tiktok_cfg.get("auto_upload") and (tiktok_cfg.get("zernio_api_key") or tiktok_cfg.get("access_token")))
            social = {
                r["platform"]: dict(r) for r in _rows(
                    conn, "SELECT platform, status, post_url, error FROM social_uploads WHERE video_id = ?",
                    (latest[0]["id"],),
                )
            } if latest else {}
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
                # The run log only reaches back three days and is empty after
                # a fresh state; the database always knows the newest video.
                "last_video": dict(latest[0]) if latest else None,
                "youtube": youtube,
                "facebook": facebook,
                "tiktok": tiktok,
                "social": social,
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
               WHERE status IN ('upload_failed','quality_failed','files_lost')
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

        # What went to the Facebook Pages: videos (Reels and Page videos) and
        # the daily picture posts, newest first, each with its link.
        names = {channel.id: channel.name for channel in load_channels()}
        facebook = [
            {"title": r["title"], "channel": r["channel"], "kind": "Reel" if r["content_type"] == "short" else "Video",
             "url": r["post_url"], "ok": r["status"] == "submitted", "error": r["error"], "at": r["updated_at"]}
            for r in _rows(
                conn,
                """SELECT v.title, v.channel, COALESCE(v.content_type,'short') AS content_type,
                          s.status, s.post_url, s.error, s.updated_at
                   FROM social_uploads s JOIN videos v ON v.id = s.video_id
                   WHERE s.platform = 'facebook' AND s.status IN ('submitted', 'failed')
                   ORDER BY s.updated_at DESC LIMIT 20""",
            )
        ] + [
            {"title": r["subject"], "channel": r["channel"], "kind": "Picture post",
             "url": r["post_url"], "ok": True, "error": "", "at": r["created_at"]}
            # The table appears with the first picture post; until then _rows gives [].
            for r in _rows(conn, "SELECT subject, channel, post_url, created_at FROM page_posts ORDER BY id DESC LIMIT 20")
        ]
        facebook = sorted(facebook, key=lambda item: str(item["at"]), reverse=True)[:20]
        for item in facebook:
            item["channel_name"] = names.get(item["channel"], item["channel"])

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
            "facebook": facebook,
            "week": week,
            "channels": channels,
            "recent": recent,
            "today": int(today_uploads[0]["n"]) if today_uploads else 0,
            "problems": problems,
        }
    finally:
        conn.close()


# Why a built video is not on YouTube yet, read from its error text. The state
# alone used to be shown as "waiting for upload quota", which was wrong for a
# refused login or a lost file and sent people looking for a quota problem.
_WAITING_REASONS = [
    (("cap reached", "quota", "uploadlimitexceeded", "upload paused"), ("Built · waiting for upload quota", "warn")),
    (("has been deleted", "invalid_grant", "invalid_client", "unauthorized", "401", "403"),
     ("Upload refused · connect YouTube again", "bad")),
    (("not connected", "not authorized"), ("Built · YouTube not connected", "warn")),
    (("missing:",), ("Not uploaded · video file is gone", "bad")),
]


def _status(status: str, error: str | None = None) -> str:
    words, cls = STATUS_WORDS.get(status, (status.replace("_", " "), "muted"))
    if status in PENDING_STATUSES and status != "thumbnail_pending":
        lowered = str(error or "").lower()
        for needles, label in _WAITING_REASONS:
            if any(needle in lowered for needle in needles):
                words, cls = label
                break
    return f"<span class='pill {cls}'>{html.escape(words)}</span>"


def _chips(*words: str) -> str:
    return "".join(f"<span class='chip'>{html.escape(str(word))}</span>" for word in words if word)


def _card(channel: dict) -> str:
    rows = []
    for account in channel["accounts"]:
        used, limit = account["used"], account["limit"]
        pct = min(100, int(used / limit * 100)) if limit else 0
        state = "full" if limit and used >= limit else "ok"
        # One account needs no name: it would only repeat the card's title.
        name = html.escape(account["name"]) if len(channel["accounts"]) > 1 else "Uploaded today"
        rows.append(f"""
        <div class="acct">
          <div class="acct-top">
            <span class="acct-name">{name}</span>
            <span class="acct-count {state}">{used} <small>/ {limit or '∞'}</small></span>
          </div>
          <div class="bar"><i style="width:{pct}%"></i></div>
        </div>""")
    if not channel.get("youtube"):
        rows = []  # nothing to count for a channel that only posts to Facebook
    platforms = "".join([
        "<span class='plat on'>YouTube</span>" if channel.get("youtube") else "<span class='plat'>YouTube off</span>",
        "<span class='plat on'>Facebook</span>" if channel.get("facebook") else "",
        "<span class='plat on'>TikTok</span>" if channel.get("tiktok") else "",
    ])
    pending = channel["pending"]
    facts = [
        ("Daily at", channel.get("slot", "")),
        ("Pictures", channel.get("visuals", "")),
        ("Waiting", f"{pending} to upload" if pending else "nothing"),
    ]
    facts_html = "".join(
        f"<div><dt>{label}</dt><dd>{html.escape(str(value))}</dd></div>" for label, value in facts if value
    )
    last, video = channel.get("last"), channel.get("last_video")
    if last and not last.get("ok"):
        last_html = (f"<div class='last bad'><b>Last attempt failed</b> · {_local(last.get('at'))}"
                     f"<div class='reason'>{html.escape(str(last.get('detail') or ''))}</div></div>")
    elif video:
        last_html = (f"<div class='last'><div class='last-top'><span class='muted'>Latest video · "
                     f"{_local(video['created_at'])}</span>{_status(video['status'], video.get('error'))}</div>"
                     f"<div class='last-title'>{html.escape(str(video['title'] or 'Untitled'))}</div>"
                     f"{_platform_line(channel, video)}</div>")
    else:
        last_html = "<div class='last'><span class='muted'>No video made yet</span></div>"
    return f"""
    <article class="card channel">
      <div class="channel-top"><h3>{html.escape(channel['name'])}</h3><div class="plats">{platforms}</div></div>
      {''.join(rows)}
      <dl class="facts">{facts_html}</dl>
      {last_html}
    </article>"""


def _platform_line(channel: dict, video: dict) -> str:
    """One chip per platform for the latest video: green once it is live there,
    otherwise the time it goes live (or why it has not)."""
    now = datetime.now(timezone.utc)
    when = _parse(video.get("publish_at"))
    later = when is not None and when > now

    def chip(name: str, state: str, note: str = "") -> str:
        mark = {"ok": "✓", "warn": "⏳", "bad": "✕"}.get(state, "")
        text = f"{name} {mark}" + (f" · {note}" if note else "")
        return f"<span class='pill {state}'>{html.escape(text)}</span>"

    chips = []
    if channel.get("youtube"):
        if video.get("video_url"):
            chips.append(chip("YouTube", "warn", _local(video["publish_at"])) if later else chip("YouTube", "ok"))
        elif video.get("status") in PENDING_STATUSES:
            # The next run uploads it if today's quota has room, else the first
            # run after YouTube's quota day resets.
            full = all(a["limit"] and a["used"] >= a["limit"] for a in channel["accounts"] if a["connected"])
            start = youtube_quota_day_start() + timedelta(days=1) if full else now
            chips.append(chip("YouTube", "warn", _local(slots.next_run(start).isoformat())))
        else:
            chips.append(chip("YouTube", "bad", "not uploaded"))
    for key, name, wanted in (("facebook", "Facebook", channel.get("facebook")), ("tiktok", "TikTok", channel.get("tiktok"))):
        if not wanted:
            continue
        row = (channel.get("social") or {}).get(key)
        if row and row.get("status") == "submitted":
            chips.append(chip(name, "warn", _local(video["publish_at"])) if later else chip(name, "ok"))
        elif row and row.get("status") == "failed":
            chips.append(chip(name, "bad", "failed"))
        elif later:
            chips.append(chip(name, "warn", _local(video["publish_at"])))
        else:
            chips.append(chip(name, "warn", "with the next upload"))
    return f"<div class='plat-line'>{''.join(chips)}</div>" if chips else ""


def _video(row: sqlite3.Row) -> str:
    vid = _video_id(row["video_url"])
    thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else ""
    scheduled = row["status"] == "scheduled" and row["publish_at"]
    when = _local(row["publish_at"]) if scheduled else _local(row["upload_date"])
    label = "Goes public" if scheduled else "Published"
    return f"""
    <a class="vid" href="{html.escape(row['video_url'] or '#')}" target="_blank" rel="noopener">
      {'<img loading="lazy" src="' + thumb + '" alt="">' if thumb else '<div class="noimg"></div>'}
      <div class="vid-body">
        <div class="vid-title">{html.escape(str(row['title'] or 'Untitled'))}</div>
        <div class="meta">{_chips(row['channel'], row['content_type'])}<span class="muted">{label} {when}</span></div>
      </div>
    </a>"""


def _last_run(data: dict) -> str:
    last = data.get("last_run")
    if not last:
        return ""
    attempts = data.get("run_attempts") or []
    good = sum(1 for a in attempts if a.get("ok"))
    bad = len(attempts) - good
    cls = "bad" if bad and not good else ("warn" if bad else "ok")
    words = (f"{good} video{'s' if good != 1 else ''} built" + (f", {bad} failed" if bad else ""))
    link = (f"<a class='btn' href='{html.escape(last['run'])}' target='_blank' rel='noopener'>Open the run log</a>"
            if last.get("run") else "")
    where = "on GitHub" if last.get("where") == "github" else "on the PC"
    return f"""
    <section class="banner {cls}">
      <div><div class="eyebrow">Last run</div>
      <div class="banner-line"><b>{words}</b> · {_local(last.get('at'))} PKT, {where}</div></div>
      {link}
    </section>"""


def _kpis(data: dict) -> str:
    day = data.get("last_day") or []
    failed = sum(1 for r in day if STATUS_WORDS.get(r["status"], ("", ""))[1] == "bad")
    waiting = sum(c["pending"] for c in data["channels"])
    tiles = [
        ("Uploaded today", data["today"], "since the YouTube quota reset", "ok" if data["today"] else ""),
        ("Made in 24 hours", len(day), "videos built, uploaded or not", ""),
        ("Waiting to upload", waiting, "go up when quota allows", "warn" if waiting else ""),
        ("Failed in 24 hours", failed, "see the list below" if failed else "nothing failed", "bad" if failed else ""),
    ]
    return "<section class='kpis'>" + "".join(
        f"<div class='kpi {cls}'><div class='eyebrow'>{label}</div><div class='num'>{value}</div>"
        f"<div class='muted'>{note}</div></div>"
        for label, value, note, cls in tiles
    ) + "</section>"


def _last_day(rows: list) -> str:
    if not rows:
        return "<div class='empty'>No video was made in the last 24 hours.</div>"
    items = []
    for r in rows:
        when = _local(r["publish_at"]) if r["status"] == "scheduled" and r["publish_at"] else _local(r["created_at"])
        title = html.escape(str(r["title"] or "Untitled"))
        title = (f"<a href='{html.escape(r['video_url'])}' target='_blank' rel='noopener'>{title}</a>"
                 if r["video_url"] else title)
        error = (f"<div class='reason'>{html.escape(_clean(r['error']))}</div>"
                 if r["error"] and STATUS_WORDS.get(r["status"], ("", "bad"))[1] in {"bad", "warn"} else "")
        items.append(
            f"<li class='row'><div class='row-main'><div class='row-title'>{title}</div>"
            f"<div class='meta'>{_chips(r['channel'], r['content_type'])}</div>{error}</div>"
            f"<div class='row-side'>{_status(r['status'], r['error'])}<span class='muted'>{when}</span></div></li>"
        )
    return f"<ul class='rows'>{''.join(items)}</ul>"


def _upcoming(rows: list) -> str:
    if not rows:
        return "<div class='empty'>Nothing scheduled.</div>"
    return "<ul class='rows'>" + "".join(
        f"<li class='row'><div class='row-main'><div class='row-title'>"
        f"<a href='{html.escape(r['video_url'] or '#')}' target='_blank' rel='noopener'>"
        f"{html.escape(str(r['title'] or 'Untitled'))}</a></div>"
        f"<div class='meta'>{_chips(r['channel'], r['content_type'])}</div></div>"
        f"<div class='row-side'><span class='when'>{_local(r['publish_at'])}</span></div></li>"
        for r in rows
    ) + "</ul>"


def _facebook(items: list) -> str:
    if not items:
        return "<div class='empty'>Nothing has been posted to a Facebook Page yet.</div>"
    rows = []
    for item in items:
        title = html.escape(str(item["title"] or "Untitled"))
        url = str(item["url"] or "")
        if item["ok"] and url:
            # The address is shown as well as linked, so it can be copied.
            shown = html.escape(url.replace("https://www.", "").replace("https://", ""))
            title = f"<a href='{html.escape(url)}' target='_blank' rel='noopener'>{title}</a>"
            link = f"<a class='url' href='{html.escape(url)}' target='_blank' rel='noopener'>{shown}</a>"
            state = "<span class='pill ok'>Live on Facebook</span>"
        else:
            link = f"<div class='reason'>{html.escape(_clean(item['error']))}</div>" if item["error"] else ""
            state = "<span class='pill bad'>Not posted to Facebook</span>"
        rows.append(
            f"<li class='row'><div class='row-main'><div class='row-title'>{title}</div>"
            f"<div class='meta'>{_chips(item['channel_name'], item['kind'])}</div>{link}</div>"
            f"<div class='row-side'>{state}<span class='muted'>{_local(item['at'])}</span></div></li>"
        )
    return f"<ul class='rows'>{''.join(rows)}</ul>"


def _week(week: dict, channels: list) -> str:
    names = [(c["id"], c["name"]) for c in channels]
    peak = max([count for counts in week.values() for count in counts.values()] or [1])
    head = "".join(f"<th>{html.escape(name)}</th>" for _id, name in names)
    body = []
    for day, counts in week.items():
        total = sum(counts.values())
        cells = "".join(
            # Darker cell = more uploads that day, so a thin day stands out.
            f"<td><span class='heat' style='--a:{counts.get(cid, 0) / peak:.2f}'>{counts.get(cid, 0) or '·'}</span></td>"
            for cid, _name in names
        )
        body.append(f"<tr><th scope='row'>{day.strftime('%a %d %b')}</th>{cells}<td class='total'>{total}</td></tr>")
    return (f"<div class='tbl'><table class='week'><thead><tr><th>Day</th>{head}<th>Total</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def _failures(rows: list) -> str:
    if not rows:
        return ""
    items = "".join(
        f"<li class='row'><div class='row-main'><div class='row-title'>{html.escape(a.get('channel', ''))}</div>"
        f"<div class='reason'>{html.escape(str(a.get('detail') or ''))}</div></div>"
        f"<div class='row-side'><span class='muted'>{_local(a.get('at'))}</span></div></li>"
        for a in rows
    )
    return f"""
    <section class="card alert">
      <h2>Failed attempts <small>last 3 days</small></h2>
      <ul class="rows">{items}</ul>
    </section>"""


# Plain CSS, kept out of the f-string below so its braces need no doubling.
# Phone first: one column, then two and four as the screen widens. Light and
# dark follow the device.
_CSS = """
  :root { --bg:#0b0f1e; --card:#141a2f; --card2:#0f1426; --line:#242c4a; --text:#e9edff;
          --muted:#909abf; --accent:#6d8dff; --ok:#3ddc97; --warn:#ffcc66; --bad:#ff7b7b;
          --ok-bg:rgba(61,220,151,.14); --warn-bg:rgba(255,204,102,.14); --bad-bg:rgba(255,123,123,.14);
          --chip:#232c52; --chip-text:#bccbff; --shadow:0 1px 2px rgba(0,0,0,.25); }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f3f5fb; --card:#ffffff; --card2:#f4f6fc; --line:#dfe4f2; --text:#151a2d;
            --muted:#5d6788; --accent:#3d5bd9; --ok:#0f8a56; --warn:#9a6700; --bad:#c62f2f;
            --ok-bg:#dff5ea; --warn-bg:#fdf0d0; --bad-bg:#fde3e3;
            --chip:#e7ebfa; --chip-text:#33427d; --shadow:0 1px 3px rgba(20,30,70,.08); }
  }
  * { box-sizing:border-box }
  html { -webkit-text-size-adjust:100% }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 -apple-system,"Segoe UI",Roboto,system-ui,sans-serif; }
  a { color:var(--accent); text-decoration:none } a:hover { text-decoration:underline }
  .wrap { max-width:1120px; margin:0 auto; padding:0 16px }
  .top { position:sticky; top:0; z-index:5; background:color-mix(in srgb, var(--bg) 88%, transparent);
         backdrop-filter:blur(10px); border-bottom:1px solid var(--line) }
  .top .wrap { display:flex; align-items:center; justify-content:space-between; gap:8px 16px;
               flex-wrap:wrap; padding-top:12px; padding-bottom:12px }
  .brand { display:flex; align-items:center; gap:10px; font-weight:700; font-size:18px }
  .brand i { width:28px; height:28px; border-radius:9px; flex:0 0 auto;
             background:linear-gradient(135deg,#6d8dff,#b07cff) }
  .top-meta { color:var(--muted); font-size:13px; display:flex; gap:4px 14px; flex-wrap:wrap }
  .top-meta b { color:var(--text); font-weight:600 }
  main.wrap { padding-top:16px; padding-bottom:8px }
  h2 { font-size:13px; margin:0 0 12px; color:var(--muted); text-transform:uppercase; letter-spacing:.07em }
  h2 small { text-transform:none; letter-spacing:0; font-weight:400 }
  h2.section { margin:22px 0 10px }
  h3 { font-size:17px; margin:0; line-height:1.25 }
  .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:16px;
          box-shadow:var(--shadow); min-width:0 }
  .muted { color:var(--muted); font-size:13px }
  .eyebrow { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.07em }
  .empty { color:var(--muted); font-size:14px; padding:6px 0 }

  /* Its inline display:flex would otherwise show the bar even while hidden. */
  #install-bar[hidden] { display:none !important }
  #install-bar { max-width:none !important; margin:0 0 14px !important; background:var(--card) !important;
                 border-color:var(--accent) !important; border-radius:14px !important }

  .banner { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;
            background:var(--card); border:1px solid var(--line); border-left:4px solid var(--ok);
            border-radius:14px; padding:14px 16px; margin-bottom:14px }
  .banner.warn { border-left-color:var(--warn) } .banner.bad { border-left-color:var(--bad) }
  .banner-line { margin-top:2px }
  .btn { display:inline-block; padding:8px 14px; border-radius:10px; border:1px solid var(--line);
         background:var(--card2); color:var(--text); font-weight:600; font-size:13px; white-space:nowrap }
  .btn:hover { text-decoration:none; border-color:var(--accent) }

  .kpis { display:grid; gap:12px; grid-template-columns:repeat(2,minmax(0,1fr)) }
  .kpi { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:14px 16px;
         box-shadow:var(--shadow) }
  .kpi .num { font-size:34px; font-weight:700; line-height:1.1; margin:4px 0 2px;
              font-variant-numeric:tabular-nums }
  .kpi.ok .num { color:var(--ok) } .kpi.warn .num { color:var(--warn) } .kpi.bad .num { color:var(--bad) }

  .grid { display:grid; gap:14px; grid-template-columns:1fr }
  .channel { display:flex; flex-direction:column; gap:12px }
  .channel-top { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap }
  .plats { display:flex; gap:6px; flex-wrap:wrap }
  .plat { font-size:11px; font-weight:600; padding:2px 8px; border-radius:99px;
          border:1px solid var(--line); color:var(--muted) }
  .plat.on { background:var(--ok-bg); border-color:transparent; color:var(--ok) }
  .acct-top { display:flex; justify-content:space-between; align-items:baseline; gap:8px; margin-bottom:6px }
  .acct-name { color:var(--muted); font-size:13px }
  .acct-count { font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; color:var(--text) }
  .acct-count small { font-size:13px; font-weight:500; color:var(--muted) }
  .acct-count.full { color:var(--warn) }
  .bar { height:8px; background:var(--card2); border-radius:99px; overflow:hidden }
  .bar i { display:block; height:100%; border-radius:99px; background:linear-gradient(90deg,#5b7bff,#9a7dff) }
  .facts { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin:0 }
  .facts dt { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.05em }
  .facts dd { margin:0; font-size:13px; font-weight:600; overflow-wrap:anywhere }
  .last { background:var(--card2); border-radius:12px; padding:10px 12px; margin-top:auto }
  .last-top { display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap }
  .last-title { font-size:14px; font-weight:600; margin-top:4px; overflow-wrap:anywhere;
                display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden }
  .last.bad b { color:var(--bad) }
  .reason { color:var(--muted); font-size:12px; margin-top:4px; overflow-wrap:anywhere }

  .pill { display:inline-block; font-size:12px; font-weight:600; padding:2px 9px; border-radius:99px;
          background:var(--chip); color:var(--chip-text) }
  .pill.ok { background:var(--ok-bg); color:var(--ok) } .pill.warn { background:var(--warn-bg); color:var(--warn) }
  .pill.bad { background:var(--bad-bg); color:var(--bad) }
  .plat-line { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px }
  .chip { background:var(--chip); color:var(--chip-text); border-radius:99px; padding:1px 9px; font-size:12px }
  .meta { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin-top:6px }

  .cols { display:grid; gap:14px; grid-template-columns:1fr; margin-top:14px }
  .stack { display:grid; gap:14px; margin-top:14px }
  .rows { list-style:none; margin:0; padding:0 }
  .row { display:flex; flex-direction:column; gap:8px; padding:12px 0; border-top:1px solid var(--line) }
  .row:first-child { border-top:0; padding-top:0 } .row:last-child { padding-bottom:0 }
  .row-main { min-width:0 } .row-title { font-weight:600; font-size:14px; overflow-wrap:anywhere }
  .row-side { display:flex; align-items:center; gap:10px; flex-wrap:wrap }
  .when { color:var(--muted); font-size:13px; white-space:nowrap }
  .url { display:block; margin-top:6px; font-size:12px; overflow-wrap:anywhere }
  .alert { border-left:4px solid var(--bad); margin-top:14px }

  .tbl { overflow-x:auto; margin:0 -16px; padding:0 16px }
  table { width:100%; border-collapse:collapse; font-size:14px }
  td, th { padding:8px 8px; border-top:1px solid var(--line); text-align:center; white-space:nowrap;
           font-variant-numeric:tabular-nums }
  thead th { border-top:0; color:var(--muted); font-size:12px; font-weight:600 }
  tbody th { text-align:left; font-weight:500; color:var(--muted) }
  thead th:first-child { text-align:left }
  .heat { display:inline-block; min-width:30px; padding:2px 6px; border-radius:8px;
          background:color-mix(in srgb, var(--accent) calc(var(--a) * 45%), transparent) }
  td.total { font-weight:700 }

  .vids { display:grid; gap:10px; grid-template-columns:1fr }
  .vid { display:flex; gap:12px; padding:8px; border-radius:12px; color:inherit; min-width:0 }
  .vid:hover { background:var(--card2); text-decoration:none }
  .vid img, .noimg { width:128px; aspect-ratio:16/9; object-fit:cover; border-radius:10px;
                     background:var(--card2); flex:0 0 auto }
  .vid-body { min-width:0 }
  .vid-title { font-size:14px; font-weight:600; overflow-wrap:anywhere;
               display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden }
  footer.wrap { color:var(--muted); font-size:13px; padding-top:22px; padding-bottom:28px }

  @media (min-width:640px) {
    .grid { grid-template-columns:repeat(2,minmax(0,1fr)) }
    .vids { grid-template-columns:repeat(2,minmax(0,1fr)) }
    .row { flex-direction:row; justify-content:space-between; align-items:flex-start; gap:16px }
    .row-side { flex-direction:column; align-items:flex-end; gap:4px; flex:0 0 auto; text-align:right }
  }
  @media (min-width:900px) {
    body { font-size:16px }
    .kpis { grid-template-columns:repeat(4,minmax(0,1fr)) }
    .grid { grid-template-columns:repeat(3,minmax(0,1fr)) }
    .cols { grid-template-columns:minmax(0,3fr) minmax(0,2fr); align-items:start }
    .vids { grid-template-columns:repeat(3,minmax(0,1fr)) }
    .vid { flex-direction:column; gap:8px } .vid img, .noimg { width:100% }
  }
"""


def render(data: dict) -> str:
    from pwa_files import HEAD_TAGS as pwa_head, INSTALL_BAR as install_bar
    now = datetime.now(timezone.utc)
    updated = (now + LOCAL_TZ_OFFSET).strftime("%d %b %Y, %I:%M %p")
    next_local = (slots.next_run(now) + LOCAL_TZ_OFFSET).strftime("%d %b, %I:%M %p")

    problems = ""
    if data["problems"]:
        items = "".join(
            f"<li class='row'><div class='row-main'><div class='row-title'>"
            f"{html.escape(str(p['title'] or 'Untitled'))[:80]}</div>"
            f"<div class='meta'>{_chips(p['channel'])}</div>"
            f"<div class='reason'>{html.escape(_clean(p['error'] or p['status']))}</div></div></li>"
            for p in data["problems"]
        )
        problems = f"""
    <section class="card alert">
      <h2>Needs a look</h2>
      <ul class="rows">{items}</ul>
    </section>"""

    videos = "".join(_video(row) for row in data["recent"])
    videos = f"<div class='vids'>{videos}</div>" if videos else "<div class='empty'>Nothing uploaded yet.</div>"

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>Story Bot Studio — status</title>
{pwa_head}
<style>{_CSS}</style></head><body>
<header class="top"><div class="wrap">
  <div class="brand"><i></i>Story Bot Studio</div>
  <div class="top-meta"><span>Updated <b>{updated} PKT</b></span><span>Next run <b>{next_local}</b></span></div>
</div></header>

<main class="wrap">
  {install_bar}
  {_last_run(data)}
  {_kpis(data)}

  <h2 class="section">Channels</h2>
  <div class="grid">{''.join(_card(c) for c in data['channels'])}</div>

  {_failures(data.get('failures') or [])}
  {problems}

  <div class="cols">
    <section class="card">
      <h2>Last 24 hours</h2>
      {_last_day(data.get('last_day') or [])}
    </section>
    <section class="card">
      <h2>Going public next</h2>
      {_upcoming(data.get('upcoming') or [])}
    </section>
  </div>

  <div class="stack">
    <section class="card">
      <h2>On Facebook <small>videos and posts, newest first</small></h2>
      {_facebook(data.get('facebook') or [])}
    </section>
    <section class="card">
      <h2>Uploads <small>last 7 days</small></h2>
      {_week(data.get('week') or {{}}, data['channels'])}
    </section>
    <section class="card">
      <h2>Latest videos</h2>
      {videos}
    </section>
  </div>
</main>

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
    # Files a website owner must host to prove the site is theirs (TikTok's
    # URL-property file, Google/Meta verification files) go in site_extra/
    # and are published as-is at the site root.
    extra = ROOT / "site_extra"
    if extra.is_dir():
        for item in extra.iterdir():
            if item.is_file() and item.name != "README.md":
                shutil.copy2(item, out_dir / item.name)
    # Stops GitHub Pages running the file through Jekyll, which would mangle it.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Wrote {out_dir / 'index.html'} ({len(page) // 1024} KB)")


if __name__ == "__main__":
    main()
