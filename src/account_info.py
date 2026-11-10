"""Which accounts and apps the bot runs on, for the dashboard (Connections tab).

    GitHub     the repository, the account that pushes and owns the secrets,
               the Actions and status-page links
    Facebook   per Page: the Meta app its token belongs to (name, ID, link to
               developers.facebook.com) and the Facebook account that connected it
               (Graph API debug_token; no token is ever shown)
    YouTube    per channel: its Google Cloud project (from client_secret_<id>.json)
    TikTok     per channel: the connected TikTok username

Facebook is asked only on "Refresh" and kept in data/account_info.json (a
machine file, not committed): names and IDs only, never a token.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import settings

ROOT = settings.root
CACHE = ROOT / "data" / "account_info.json"
SOCIAL = ROOT / "credentials" / "social_accounts.json"


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def github() -> dict:
    origin = _git("remote", "get-url", "origin")
    match = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)", origin)
    owner, repo = (match.group(1), match.group(2)) if match else ("", "")
    helper = _git("config", "--local", "--get-all", "credential.helper")
    pusher = re.search(r"username=([A-Za-z0-9-]+)", helper)
    base = f"https://github.com/{owner}/{repo}" if owner else ""
    return {
        "owner": owner,
        "repo": f"{owner}/{repo}" if owner else "",
        "repo_url": base,
        "owner_url": f"https://github.com/{owner}" if owner else "",
        # git push in this folder (local credential.helper), and Send to GitHub
        # (scripts/setup_github_actions.py sends secrets as the repo owner).
        "push_account": pusher.group(1) if pusher else owner,
        "secrets_account": owner,
        "actions_url": f"{base}/actions" if base else "",
        "secrets_url": f"{base}/settings/secrets/actions" if base else "",
        "pages_url": f"https://{owner.lower()}.github.io/{repo}/" if owner else "",
        "zip_url": f"{base}/archive/refs/heads/main.zip" if base else "",
        "commit_name": _git("config", "user.name"),
    }


def _social() -> dict:
    try:
        return json.loads(SOCIAL.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _graph(path: str, **query) -> dict:
    url = f"https://graph.facebook.com/v21.0/{path}?{urllib.parse.urlencode(query)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return {"error": json.load(exc).get("error", {}).get("message", str(exc))}
        except Exception:
            return {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}


def refresh_facebook() -> dict:
    """Ask Facebook about every Page token; saved to the cache."""
    pages = {}
    for channel_id, row in _social().items():
        fb = row.get("facebook") if isinstance(row, dict) else None
        if not isinstance(fb, dict) or not fb.get("page_id"):
            continue
        page_id = str(fb["page_id"])
        item = {"page_id": page_id, "page_name": fb.get("verified_name", ""),
                "page_url": f"https://www.facebook.com/{page_id}"}
        token = fb.get("page_access_token")
        if token:
            data = _graph("debug_token", input_token=token, access_token=token)
            if data.get("error"):
                item["error"] = data["error"][:160]
            else:
                info = data.get("data") or {}
                app_id = str(info.get("app_id") or "")
                item.update({
                    "app_id": app_id,
                    "app_name": info.get("application", ""),
                    "app_url": f"https://developers.facebook.com/apps/{app_id}/" if app_id else "",
                    "valid": bool(info.get("is_valid")),
                    "expires": "never" if not info.get("expires_at") else time.strftime(
                        "%Y-%m-%d", time.gmtime(int(info["expires_at"]))),
                })
                user_id = info.get("user_id")
                if user_id:
                    who = _graph(str(user_id), fields="name", access_token=token)
                    item["connected_by"] = who.get("name", "")
                    item["connected_by_id"] = str(user_id)
        else:
            item["error"] = "no Page token saved"
        pages[channel_id] = item
    result = {"checked_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "pages": pages}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def facebook() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {"checked_at": "", "pages": {}}


def _json(name: str) -> dict:
    try:
        return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))
    except Exception:
        return {}


def youtube_projects() -> dict:
    """channel id -> its Google Cloud project (client_secret_<id>.json, else the shared one),
    the Gmail that owns the project and the Gmail the channel logged in with
    (data/project_emails.json, data/account_emails.json, saved by youtube_accounts)."""
    project_emails, account_emails = _json("project_emails.json"), _json("account_emails.json")
    out = {}
    for path in sorted(ROOT.glob("client_secret*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            project = (next(iter(data.values())) or {}).get("project_id", "")
        except Exception:
            continue
        key = path.stem.replace("client_secret", "").lstrip("_") or "_shared"
        out[key] = {"project": project, "file": path.name,
                    "project_email": project_emails.get(project, ""),
                    "login_email": account_emails.get(f"{key}:main", ""),
                    "url": f"https://console.cloud.google.com/apis/credentials?project={project}" if project else ""}
    return out


def tiktok() -> dict:
    out = {}
    for channel_id, row in _social().items():
        tk = row.get("tiktok") if isinstance(row, dict) else None
        if isinstance(tk, dict):
            name = tk.get("zernio_username") or tk.get("verified_name") or ""
            out[channel_id] = {"name": name, "via": "Zernio" if tk.get("mode") == "zernio" else "TikTok app",
                               "url": f"https://www.tiktok.com/@{name}" if tk.get("zernio_username") else ""}
    return out


def overview() -> dict:
    return {"github": github(), "facebook": facebook(), "youtube": youtube_projects(), "tiktok": tiktok()}
