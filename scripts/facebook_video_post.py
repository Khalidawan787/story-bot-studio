"""Post one finished video to a channel's Facebook Page, right now.

Runs on GitHub (the "Facebook video post" workflow), so a video made on the PC
still goes to Facebook from the one place that posts there. The workflow takes
the video and its post text from a GitHub release:

    python scripts/facebook_video_post.py --channel motivation \
        --video video.mp4 --post post.txt --content-type short

The first line of post.txt is the title; the whole file is the description.
A title already on the Page is not posted again.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.page_posts import _on_page, _page_messages, _squash  # noqa: E402
from src.social_accounts import facebook_elsewhere_reason, facebook_posts_here  # noqa: E402
from src.social_publish import upload_facebook  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--post", required=True, help="text file: title on the first line")
    parser.add_argument("--content-type", default="short", choices=["short", "long"])
    args = parser.parse_args()

    if not facebook_posts_here():
        raise SystemExit(facebook_elsewhere_reason())
    description = Path(args.post).read_text(encoding="utf-8").strip()
    title = description.splitlines()[0]
    if _on_page(title, _squash(" ".join(_page_messages(args.channel)))):
        print(f"Already on the Page, not posted again: {title}")
        return
    _remote_id, url = upload_facebook(args.channel, Path(args.video), title, args.content_type, description)
    print(f"Posted: {url}")


if __name__ == "__main__":
    main()
