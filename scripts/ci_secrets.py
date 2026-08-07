"""Move the private files this bot needs in and out of one GitHub secret.

The bot needs .env, the Google OAuth client(s) and the YouTube tokens. None of
them may ever be committed, so they travel as a single base64 blob stored in
the repository secret BOT_SECRETS_B64.

    pack    - read the private files on this PC and print the blob
    unpack  - write the files back, used by the workflow on the runner

`pack` prints a very long line that IS a full set of credentials. Prefer
scripts/setup_github_actions.py, which pipes it straight into `gh secret set`
so it never appears on screen or in shell history.
"""
from __future__ import annotations

import base64
import io
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Everything the bot needs to authenticate. Missing entries are skipped, so a
# machine that never connected "love" simply ships no token for it.
SECRET_FILES = [
    ".env",
    "client_secret.json",
    "credentials/drive_token.json",
    "data/pexels_api_key.txt",
    "data/pollinations_api_key.txt",
    "data/cloudflare_account_id.txt",
    "data/cloudflare_api_token.txt",
]
SECRET_GLOBS = ["client_secret_*.json", "token*.json"]


def _collect() -> list[Path]:
    found: list[Path] = []
    for name in SECRET_FILES:
        path = ROOT / name
        if path.is_file():
            found.append(path)
    for pattern in SECRET_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            # .bak copies are old logins; shipping them would only confuse.
            if path.is_file() and not path.name.endswith(".bak"):
                found.append(path)
    return found


def pack() -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in _collect():
            archive.add(path, arcname=str(path.relative_to(ROOT)).replace("\\", "/"))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def unpack(blob: str) -> list[str]:
    raw = base64.b64decode(blob)
    written: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            # Never let an archive write outside the project folder.
            target = (ROOT / member.name).resolve()
            if not str(target).startswith(str(ROOT.resolve())):
                raise ValueError(f"Refusing to write outside the project: {member.name}")
            archive.extract(member, ROOT, filter="data")
            written.append(member.name)
    return written


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "pack":
        names = [str(p.relative_to(ROOT)) for p in _collect()]
        print("\n".join(f"# packed: {n}" for n in names), file=sys.stderr)
        print(pack())
        return
    if action == "unpack":
        import os

        blob = os.environ.get("BOT_SECRETS_B64", "").strip()
        if not blob:
            raise SystemExit(
                "BOT_SECRETS_B64 is empty. Add it with:\n"
                "  python scripts/setup_github_actions.py"
            )
        for name in unpack(blob):
            print(f"restored {name}")
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
