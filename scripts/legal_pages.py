"""Privacy policy and terms pages for the Google sign-in (OAuth) screen.

Google asks every app that signs people in for a home page and privacy policy
link before it can be published "In production". These two pages are written
next to the status page on GitHub Pages, so every Cloud project of every
channel can point at the same three links:

    https://<user>.github.io/<repo>/              home page
    https://<user>.github.io/<repo>/privacy.html  privacy policy
    https://<user>.github.io/<repo>/terms.html    terms of service

They describe what the bot really does with a Google account. Change them if
the bot starts doing more.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

APP = "Story Bot Studio"
UPDATED = date(2026, 10, 12).strftime("%d %B %Y")

_STYLE = """
body{margin:0;padding:24px 16px;background:#f6f7fb;color:#1d2340;font:16px/1.65 system-ui,'Segoe UI',Arial,sans-serif}
main{max-width:760px;margin:0 auto;background:#fff;border:1px solid #e1e5f0;border-radius:14px;padding:24px 28px}
h1{font-size:26px;margin:0 0 4px} h2{font-size:18px;margin:26px 0 6px} .muted{color:#5d6485;font-size:14px}
a{color:#2f55d4} li{margin:4px 0}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{title} · {APP}</title><style>{_STYLE}</style></head><body><main>"
            f"<h1>{title}</h1><div class='muted'>{APP} · last updated {UPDATED}</div>{body}"
            f"<p class='muted' style='margin-top:28px'><a href='./'>Status</a> · "
            f"<a href='privacy.html'>Privacy policy</a> · <a href='terms.html'>Terms of service</a></p>"
            f"</main></body></html>")


PRIVACY = _page("Privacy policy", f"""
<p>{APP} is an automation tool run by its operator for YouTube channels they
manage. It writes and renders videos and uploads them to a YouTube channel
whose owner has signed in with Google and allowed it to.</p>

<h2>What the app accesses</h2>
<ul>
  <li><b>YouTube upload</b> (<code>youtube.upload</code>): to upload videos, set their
      title, description, tags, thumbnail and publish time on the connected channel.</li>
  <li><b>YouTube read-only</b> (<code>youtube.readonly</code>): to read the connected
      channel's name, so the operator can confirm videos go to the right channel.</li>
  <li><b>Email address</b> (<code>openid</code>, <code>userinfo.email</code>): to show
      the operator which Google account a channel is connected with.</li>
</ul>
<p>The app does not read, change or delete existing videos, comments, playlists,
subscribers, messages or any other Google data, and never sees the account password.</p>

<h2>How the data is used and stored</h2>
<ul>
  <li>The sign-in token, channel name and email are stored privately by the
      operator (on the operator's computer and in an encrypted secret of the
      automation that runs the daily uploads) and used only to upload
      that channel's videos.</li>
  <li>The data is not sold, not shared with third parties and not used for
      advertising.</li>
  <li>Videos are uploaded through the YouTube API Services. By using the app you
      also agree to the <a href="https://www.youtube.com/t/terms">YouTube Terms of
      Service</a>; Google's handling of data is covered by the
      <a href="https://policies.google.com/privacy">Google Privacy Policy</a>.</li>
</ul>

<h2>Removing access</h2>
<p>Access can be removed at any time at
<a href="https://myaccount.google.com/permissions">myaccount.google.com/permissions</a>
or from the YouTube channel's security settings. After that, no further uploads
are possible and the stored token stops working; ask the operator to delete it
as well.</p>

<h2>Contact</h2>
<p>Questions or deletion requests: write to the support email shown on the
Google sign-in screen of this app.</p>
""")

TERMS = _page("Terms of service", f"""
<p>{APP} is provided by its operator to the owners of the YouTube channels it
manages, by agreement with each owner.</p>
<h2>Use</h2>
<ul>
  <li>The channel owner allows the app to upload videos to their channel and may
      withdraw that permission at any time (see the privacy policy).</li>
  <li>Uploaded videos must follow the <a href="https://www.youtube.com/t/terms">YouTube
      Terms of Service</a> and <a href="https://www.youtube.com/howyoutubeworks/policies/community-guidelines/">Community
      Guidelines</a>. The channel owner remains responsible for their channel.</li>
  <li>The app is provided as is, without a guarantee of views, income or
      uninterrupted service.</li>
</ul>
<h2>Changes</h2>
<p>These terms may change; the date above shows the latest version.</p>
""")


def write(out_dir: Path) -> None:
    (out_dir / "privacy.html").write_text(PRIVACY, encoding="utf-8")
    (out_dir / "terms.html").write_text(TERMS, encoding="utf-8")
