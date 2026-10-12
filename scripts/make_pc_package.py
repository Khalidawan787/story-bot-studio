"""Pack everything a second PC needs to run this dashboard, in one zip.

    .venv\\Scripts\\python.exe scripts\\make_pc_package.py

Unlike build_release.py (a program-only zip that is safe to share), this one
is a full copy of THIS setup: the code, every channel and topic bank, the
history database, and the private files (.env, Google projects and YouTube
logins, Facebook Page token, API keys). Treat the zip like a password: keep
it on your own drives, never send it to anyone or upload it anywhere.

On the new PC: unzip, run START_DASHBOARD.bat. It builds the Python
environment the first time. The packed .env has DASHBOARD_AUTOMATION=false,
so the second PC does not make or upload videos on its own while the daily
GitHub run is doing that; every button still works.
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ci_secrets  # noqa: E402

# Working files that are not in git but make the dashboard "yours".
EXTRA_GLOBS = [
    "bot.sqlite3",
    "channels.json",                 # the PC's copy, with channels made on the dashboard
    "data/*.json",                   # topic banks, accounts, emails, history
    "data/*_api_key.txt", "data/*_api_token.txt", "data/cloudflare_account_id.txt",
    "data/guide/*",                  # setup-guide screenshots
    "assets/**/*",                   # kids picture bank
]
# Re-created or downloaded on first use; would only make the zip huge.
SKIP_PARTS = {".venv", "runs", "logs", "dist", "build", "__pycache__", ".git", "models"}


def _tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line.strip()]


def main() -> None:
    files: dict[str, Path] = {}
    for path in [*_tracked_files(), *ci_secrets._collect()]:
        if path.is_file():
            files[path.relative_to(ROOT).as_posix()] = path
    for pattern in EXTRA_GLOBS:
        for path in ROOT.glob(pattern):
            if path.is_file() and not SKIP_PARTS & set(path.relative_to(ROOT).parts):
                files[path.relative_to(ROOT).as_posix()] = path

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    target = ROOT.parent / f"StoryBot_PRIVATE_full_setup_{stamp}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            if name == ".env":
                # The second PC follows GitHub instead of uploading on its own,
                # and finds its own ffmpeg (setup writes the path).
                lines = [
                    line for line in path.read_text(encoding="utf-8").splitlines()
                    if not line.startswith(("DASHBOARD_AUTOMATION=", "FFMPEG_BIN=", "FFPROBE_BIN="))
                ]
                lines.append("DASHBOARD_AUTOMATION=false")
                archive.writestr(name, "\n".join(lines) + "\n")
            else:
                archive.write(path, name)
        archive.writestr("OPEN_ME_FIRST.txt", README)
    size = target.stat().st_size / 1048576
    print(f"Wrote {target} ({len(files)} files, {size:.0f} MB)")
    print("PRIVATE: it holds your logins and keys. Copy it by USB or your own drive only.")


README = """STORY BOT STUDIO - full copy of your setup (PRIVATE)

This zip holds your YouTube logins, Google projects, Facebook Page token and
API keys. Do not send it to anyone and do not upload it anywhere.

On the new PC
1. Install Python 3.11 or newer from https://www.python.org/downloads/
   and tick "Add Python to PATH" on the first screen.
2. Unzip this file to a simple folder, e.g. C:\\StoryBot
3. Double-click START_DASHBOARD.bat
   The first start installs everything (a few minutes) and FFmpeg via winget,
   then opens http://127.0.0.1:8000 in the browser.
   If FFmpeg is missing, open PowerShell in the folder and run:
   powershell.exe -ExecutionPolicy Bypass -File .\\scripts\\setup_new_pc.ps1

Automation
The daily videos are made and uploaded by GitHub, so this copy starts with
DASHBOARD_AUTOMATION=false in .env: the dashboard does only what you click.
Set it to true only if GitHub stops doing the daily run, or both will upload.

Changes
A channel, login or key changed on one PC reaches GitHub only after
"Send to GitHub" on that PC. Use one PC for changes to keep things simple.
"""


if __name__ == "__main__":
    main()
