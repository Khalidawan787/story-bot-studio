"""Post a channel's recent long videos to its Facebook Page, spread over set times.

Runs on GitHub (the "Facebook backfill" workflow), where the finished videos
are kept as run artifacts for a week and the network is fast. It reads the
bot's database to know which videos belong to the channel, pulls each video
out of its run's artifact, and uploads it to the Page: the first time "now"
publishes at once, every other time is handed to Facebook's own scheduler,
so nothing needs to stay online afterwards.

    python scripts/facebook_backfill.py --channel trending --count 5 \
        --times "now,2026-10-13 10:00,2026-10-13 13:00"

Times are Pakistan time. A video whose title is already on the Page is skipped,
so running it twice does not post twice.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.github_artifacts import github as _github  # noqa: E402
from src.social_accounts import account  # noqa: E402
from src.social_publish import FB_API_VERSION, _facebook_long, _request_json  # noqa: E402

PKT = timezone(timedelta(hours=5))


def _parse_times(raw: str) -> list[datetime | None]:
    """'now,2026-10-13 10:00,...' -> [None, aware datetime, ...] (None = now)."""
    times: list[datetime | None] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if part.lower() == "now":
            times.append(None)
            continue
        moment = datetime.strptime(part, "%Y-%m-%d %H:%M").replace(tzinfo=PKT)
        if moment < datetime.now(PKT) + timedelta(minutes=15):
            raise SystemExit(f"{part} PKT is too soon: Facebook needs at least 10 minutes' notice.")
        times.append(moment)
    return times


def _candidates(channel: str, days: int) -> list[sqlite3.Row]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT job_id, title, created_at FROM videos
           WHERE channel = ? AND content_type = 'long' AND created_at >= ?
             AND status IN ('scheduled', 'uploaded', 'daily_upload_pending')
           ORDER BY id DESC""",
        (channel, since),
    ).fetchall()
    conn.close()
    # A title that is the writing instruction itself is not worth posting.
    return [r for r in rows if not str(r["title"] or "").lower().startswith(("create a ", "write a "))]


def _find_videos(job_ids: set[str], out_dir: Path) -> dict[str, Path]:
    """Extract <job_id>/video.mp4 for the wanted jobs from the kept artifacts."""
    found: dict[str, Path] = {}
    artifacts = json.loads(_github("actions/artifacts?per_page=100")).get("artifacts", [])
    for artifact in sorted(artifacts, key=lambda a: a["created_at"], reverse=True):
        if found.keys() >= job_ids:
            break
        if artifact.get("expired") or not artifact["name"].startswith("videos-"):
            continue
        with zipfile.ZipFile(io.BytesIO(_github(artifact["archive_download_url"]))) as bundle:
            for name in bundle.namelist():
                parts = Path(name).parts
                if name.endswith("video.mp4") and len(parts) >= 2 and parts[-2] in job_ids - found.keys():
                    target = out_dir / f"{parts[-2]}.mp4"
                    target.write_bytes(bundle.read(name))
                    found[parts[-2]] = target
                    print(f"  found {parts[-2]} in {artifact['name']}")
    return found


def _titles_on_page(page_id: str, token: str) -> set[str]:
    query = urllib.parse.urlencode({"fields": "title", "limit": "100", "access_token": token})
    data = _request_json(f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/videos?{query}")
    return {str(v.get("title") or "").strip().lower() for v in data.get("data") or []}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="trending")
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--times", required=True, help="Comma-separated PKT times; 'now' posts at once.")
    args = parser.parse_args()

    times = _parse_times(args.times)[: args.count]
    cfg = account(args.channel, "facebook")
    page_id, token = str(cfg.get("page_id") or ""), str(cfg.get("page_access_token") or "")
    if not page_id or not token:
        raise SystemExit(f"No Facebook Page is set up for {args.channel}. Connect it on the dashboard and Send to GitHub.")

    already = _titles_on_page(page_id, token)
    rows = [r for r in _candidates(args.channel, args.days) if str(r["title"]).strip().lower() not in already]
    print(f"{len(rows)} video(s) of {args.channel} not yet on the Page; posting {min(len(rows), len(times))}.")
    rows = rows[: len(times)]
    if not rows:
        return

    out_dir = Path("fb_videos")
    out_dir.mkdir(exist_ok=True)
    files = _find_videos({r["job_id"] for r in rows}, out_dir)

    failures = 0
    for row, when in zip(rows, times):
        title = str(row["title"])
        path = files.get(row["job_id"])
        if not path:
            print(f"- SKIPPED (video file no longer kept on GitHub): {title}")
            continue
        try:
            video_id, url = _facebook_long(
                path, title, page_id, token,
                publish_at=int(when.timestamp()) if when else None,
            )
            moment = "now" if when is None else when.strftime("%d %b %I:%M %p PKT")
            print(f"- {'POSTED' if when is None else 'SCHEDULED'} {moment}: {title} -> {url}")
        except Exception as exc:
            failures += 1
            print(f"- FAILED: {title}: {exc}")
    if failures:
        raise SystemExit(f"{failures} upload(s) failed")


if __name__ == "__main__":
    main()
