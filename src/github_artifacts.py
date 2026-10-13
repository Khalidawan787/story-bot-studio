"""Get a finished video back from the artifacts a GitHub run kept.

A runner starts empty: the videos of an earlier run are gone, and only the
database remembers them. A video that could not be uploaded when it was made
(quota, a refused login) therefore had nothing left to upload on the next run
and failed for ever with "Thumbnail missing". The daily workflow keeps every
run's video.mp4 and thumbnail.jpg as an artifact for a week, so the retry
fetches them from there.

Only works inside GitHub Actions (GITHUB_TOKEN and GITHUB_REPOSITORY set, and
`actions: read` permission); anywhere else it finds nothing and says so.
"""
from __future__ import annotations

import io
import json
import os
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

KEPT_FILES = ("video.mp4", "thumbnail.jpg")


class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """An artifact download redirects to a signed storage URL that rejects
    the GitHub token, so the token must not follow the redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_DropAuthOnRedirect)


def available() -> bool:
    return bool(os.getenv("GITHUB_TOKEN") and os.getenv("GITHUB_REPOSITORY"))


def github(path: str) -> bytes:
    token = os.environ["GITHUB_TOKEN"]
    url = path if path.startswith("http") else f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "StoryBotStudio",
    })
    with _OPENER.open(request, timeout=600) as response:
        return response.read()


def restore_run_files(run_dir: Path) -> list[str]:
    """Put a run's kept video and thumbnail back into `run_dir`; returns what was restored."""
    if not available():
        return []
    job_id = run_dir.name
    restored: list[str] = []
    artifacts = json.loads(github("actions/artifacts?per_page=100")).get("artifacts", [])
    for artifact in sorted(artifacts, key=lambda a: a["created_at"], reverse=True):
        if artifact.get("expired") or not artifact["name"].startswith("videos-"):
            continue
        # A run's artifact holds only that run's videos, and a job id starts
        # with its date, so artifacts made before the video cannot hold it.
        if artifact["created_at"][:10].replace("-", "") < job_id[:8]:
            break
        with zipfile.ZipFile(io.BytesIO(github(artifact["archive_download_url"]))) as bundle:
            for name in bundle.namelist():
                parts = Path(name).parts
                if len(parts) >= 2 and parts[-2] == job_id and parts[-1] in KEPT_FILES:
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / parts[-1]).write_bytes(bundle.read(name))
                    restored.append(parts[-1])
        if restored:
            break
    return restored
