"""Build the StoryBotStudio release: PyInstaller exe + the files it needs.

Run from the project root:

    .venv\\Scripts\\python.exe scripts\\build_release.py

Secrets are deliberately left OUT of the zip. An earlier hand-made release
bundled .env, client_secret.json, every token*.json and bot.sqlite3, so anyone
who received that file also received the OpenAI key, the Google OAuth client
and full access to the YouTube channels. The zip now ships only the program and
its lesson banks; each machine adds its own keys once, following
START_HERE_NEW_PC.md.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "StoryBotStudio"
ZIP_PATH = ROOT / "dist" / "StoryBotStudio.zip"

# Copied next to the exe: content the app cannot run without, but nothing private.
INCLUDE_FILES = [
    "channels.json",
    ".env.example",
    "README.md",
    "START_HERE_NEW_PC.md",
    "MOVE_TO_NEW_PC.md",
    "FREE_IMAGE_SETUP.md",
    "ADD_MORE_UPLOAD_QUOTA.md",
    "MULTIPLE_YOUTUBE_CHANNELS.md",
]
# lessons.json is the kids topic bank and has no underscore, so it needs its
# own entry — without it the exe starts but every page returns 500.
INCLUDE_DATA_GLOBS = ["lessons.json", "*_lessons.json"]

# Never shipped, whatever else matches above.
SECRET_NAMES = {
    ".env", "bot.sqlite3", "client_secret.json", "credentials",
}


def _is_secret(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in SECRET_NAMES
        or "token" in name
        or name.startswith("client_secret")
        or name.endswith("_api_key.txt")
        or name.endswith("_api_token.txt")
        or name == "cloudflare_account_id.txt"
    )


def build_exe() -> None:
    print("Building the exe with PyInstaller...")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "StoryBotStudio.spec"],
        cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
    )


def copy_runtime_files() -> list[str]:
    copied: list[str] = []
    for name in INCLUDE_FILES:
        source = ROOT / name
        if source.is_file() and not _is_secret(source):
            shutil.copy2(source, DIST / name)
            copied.append(name)
    data_out = DIST / "data"
    data_out.mkdir(parents=True, exist_ok=True)
    for pattern in INCLUDE_DATA_GLOBS:
        for source in sorted((ROOT / "data").glob(pattern)):
            if _is_secret(source):
                continue
            shutil.copy2(source, data_out / source.name)
            copied.append(f"data/{source.name}")
    return copied


def make_zip() -> int:
    ZIP_PATH.unlink(missing_ok=True)
    count = 0
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(DIST.rglob("*")):
            if path.is_dir():
                continue
            if _is_secret(path):
                print(f"  SKIPPED (private): {path.relative_to(DIST)}")
                continue
            archive.write(path, Path("StoryBotStudio") / path.relative_to(DIST))
            count += 1
    return count


def main() -> None:
    build_exe()
    copied = copy_runtime_files()
    print(f"Copied {len(copied)} runtime file(s) next to the exe.")
    count = make_zip()
    size_mb = ZIP_PATH.stat().st_size / (1024 * 1024)
    print(f"\nRelease ready: {ZIP_PATH}  ({count} files, {size_mb:.1f} MB)")
    print("No .env, tokens, client secrets or database are inside — the new PC "
          "adds those itself.")


if __name__ == "__main__":
    main()
