"""A short history of every video attempt, kept for the status page.

A failure that happens before a video is saved (no voice, no script, no
images) leaves no row in the database, so the status page used to show a
quiet "0 uploaded" on a day when every channel had failed. Each attempt now
writes one line here, success or not. The file sits in data/, so the daily
run carries it forward on the bot-state branch.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from .config import settings

LOG_FILE = settings.root / "data" / "run_log.json"
_KEEP = 300


def _clean(text: str) -> str:
    # Drop URL query strings (tokens, ids) and squeeze whitespace: this text is
    # shown on a public page.
    text = re.sub(r"([a-z]+://[^\s?'\"]+)\?[^\s'\"]*", r"\1", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:240]


def run_url() -> str:
    """Link to the GitHub Actions run doing this, when there is one."""
    server, repo, run = (os.getenv(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


def read() -> list[dict]:
    try:
        rows = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def record(channel_id: str, kind: str, ok: bool, detail: str = "", title: str = "") -> None:
    """Add one attempt. Never raises: logging must not break a video."""
    try:
        rows = read()
        rows.append({
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "channel": channel_id,
            "kind": kind,
            "ok": bool(ok),
            "title": _clean(title)[:120],
            "detail": _clean(detail),
            "run": run_url(),
            "where": "github" if os.getenv("GITHUB_ACTIONS") else "pc",
        })
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE.write_text(json.dumps(rows[-_KEEP:], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        print(f"[run_log] could not record: {exc}")
