"""One-time setup so GitHub can run the daily videos without this PC.

Reads the private files on this machine (.env, OAuth clients, YouTube tokens),
packs them, and hands them to `gh secret set` through a pipe — the values are
never printed and never touch shell history.

    .venv\\Scripts\\python.exe scripts\\setup_github_actions.py

Re-run it whenever a token or key changes on this PC.
"""
from __future__ import annotations

import os
import re
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


def origin_repo() -> str:
    """'owner/name' of this folder's origin remote."""
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
    if not match:
        raise SystemExit(f"The origin remote is not a GitHub repository: {remote or '(none)'}")
    return match.group(1)


def owner_env(gh: str) -> dict[str, str]:
    """Environment that makes gh act as the repository's owner.

    gh is signed in with more than one GitHub account on the owner's PC, and the
    active one may belong to another bot. The secret must always go to this
    repository's owner (the account in `git remote get-url origin`), whichever
    account is active. Falls back to the active account when the owner's token
    is not in gh.
    """
    env = dict(os.environ)
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/", remote)
    if match:
        token = subprocess.run([gh, "auth", "token", "--user", match.group(1)],
                               capture_output=True, text=True)
        if token.returncode == 0 and token.stdout.strip():
            env["GH_TOKEN"] = token.stdout.strip()
    return env


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
        # The repository named explicitly: with a second git remote in the
        # folder, gh refused to guess ("multiple remotes detected").
        [gh, "secret", "set", SECRET_NAME, "-R", origin_repo()],
        input=blob, text=True, cwd=ROOT, capture_output=True, env=owner_env(gh),
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
