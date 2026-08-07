"""One-time setup so GitHub can run the daily videos without this PC.

Reads the private files on this machine (.env, OAuth clients, YouTube tokens),
packs them, and hands them to `gh secret set` through a pipe — the values are
never printed and never touch shell history.

    .venv\\Scripts\\python.exe scripts\\setup_github_actions.py

Re-run it whenever a token or key changes on this PC.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ci_secrets  # noqa: E402

SECRET_NAME = "BOT_SECRETS_B64"


def _gh() -> str:
    path = shutil.which("gh")
    if not path:
        raise SystemExit(
            "GitHub CLI is not installed. Get it from https://cli.github.com/ , "
            "then run:  gh auth login"
        )
    return path


def _check_auth(gh: str) -> None:
    result = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("Not signed in to GitHub. Run:  gh auth login")


def main() -> None:
    gh = _gh()
    _check_auth(gh)

    files = [str(p.relative_to(ROOT)) for p in ci_secrets._collect()]
    if not files:
        raise SystemExit("No private files found — is this the project folder?")
    print("Packing these private files (contents are never shown):")
    for name in files:
        print(f"  - {name}")

    blob = ci_secrets.pack()
    print(f"\nUploading {len(blob) // 1024} KB to the repository secret {SECRET_NAME} ...")
    result = subprocess.run(
        [gh, "secret", "set", SECRET_NAME],
        input=blob, text=True, cwd=ROOT, capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"gh secret set failed:\n{result.stderr.strip()}")

    print("Done.\n")
    print("GitHub can now build and upload the daily videos on its own.")
    print("Start it by hand once to check:")
    print("  gh workflow run daily-videos.yml")
    print("Then watch it:")
    print("  gh run watch")


if __name__ == "__main__":
    main()
