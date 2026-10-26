"""Post one picture with its caption to a channel's Facebook Page, right now.

Runs on GitHub (the "Facebook picture post" workflow), so a one-off post such
as a Page's welcome picture goes out from the same place as every other post.
The picture and caption are files in the repository:

    python scripts/facebook_picture_post.py --channel motivation \
        --image data/page_posts/welcome_motivation.jpg \
        --caption data/page_posts/welcome_motivation.txt

A caption whose first line is already on the Page is not posted again.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.page_posts import _on_page, _page_messages, _squash  # noqa: E402
from src.social_accounts import account, facebook_elsewhere_reason, facebook_posts_here  # noqa: E402
from src.social_publish import FB_API_VERSION, _multipart, _send_binary  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--caption", required=True, help="text file with the caption")
    args = parser.parse_args()

    if not facebook_posts_here():
        raise SystemExit(facebook_elsewhere_reason())
    cfg = account(args.channel, "facebook")
    page_id, token = str(cfg.get("page_id") or ""), str(cfg.get("page_access_token") or "")
    if not page_id or not token:
        raise SystemExit(f"Facebook is not connected for '{args.channel}' on GitHub: press Send to GitHub.")
    picture = ROOT / args.image
    caption = (ROOT / args.caption).read_text(encoding="utf-8").strip()
    first_line = caption.splitlines()[0]
    if _on_page(first_line, _squash(" ".join(_page_messages(args.channel)))):
        print(f"Already on the Page, not posted again: {first_line}")
        return
    body, content_type = _multipart({"caption": caption, "access_token": token}, "source", picture)
    result = _send_binary(
        f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}/photos", body,
        {"Content-Type": content_type}, "POST",
    )
    post_id = str(result.get("post_id") or result.get("id") or "")
    if not post_id:
        raise SystemExit(f"Facebook did not accept the picture: {result}")
    print(f"Posted: https://www.facebook.com/{post_id}")


if __name__ == "__main__":
    main()
