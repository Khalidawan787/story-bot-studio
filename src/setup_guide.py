"""Step-by-step Google Cloud setup guide with screenshots.

The steps are fixed text with direct console links. Screenshots are added by
the operator once, by pasting them into the guide page while doing a real
setup, and are then shown for every channel and in the guide file sent to
clients. They live in data/guide/ (not committed: they can show email
addresses).
"""
from __future__ import annotations

import base64
import html
from pathlib import Path

from .config import settings

SHOT_DIR = settings.root / "data" / "guide"
_MAX_BYTES = 6 * 1024 * 1024
_FORMATS = {b"\x89PNG": ("png", "image/png"), b"\xff\xd8\xff": ("jpg", "image/jpeg")}

CONSOLE = "https://console.cloud.google.com"

# who: client = the YouTube channel owner, you = the operator, dashboard = this app.
# {q} becomes "?project=<id>" once the channel's project is known.
STEPS = [
    {
        "id": "c1", "who": "client", "title": "Make a new project",
        "urdu": "Naya project banayen",
        "link": f"{CONSOLE}/projectcreate", "link_label": "Open: New project",
        "do": "Open the link with your Gmail. In “Project name” type <b>My YouTube Bot</b>. Leave everything else as it is and press <b>Create</b>.",
        "do_urdu": "Link apne Gmail se kholen. “Project name” mein <b>My YouTube Bot</b> likhen, baqi kuch na badlen, <b>Create</b> dabayen.",
        "tip": "It takes a few seconds. A bell icon at the top says when it is ready.",
    },
    {
        "id": "c2", "who": "client", "title": "Open “IAM” and press “Grant access”",
        "urdu": "IAM kholen aur “Grant access” dabayen",
        "link": f"{CONSOLE}/iam-admin/iam", "link_label": "Open: IAM",
        "do": "At the top of the page, check that <b>My YouTube Bot</b> is selected. Then press <b>Grant access</b>.",
        "do_urdu": "Page ke upar dekh lein ke <b>My YouTube Bot</b> chuna hua hai. Phir <b>Grant access</b> dabayen.",
        "tip": "If another project is shown, click its name at the top and pick My YouTube Bot.",
    },
    {
        "id": "c3", "who": "client", "title": "Add the bot manager as Editor",
        "urdu": "Bot manager ki Gmail ko Editor banayen",
        "link": "", "link_label": "",
        "do": "In “New principals” type <b>{email}</b>. In “Select a role” choose <b>Basic → Editor</b>. Press <b>Save</b>.",
        "do_urdu": "“New principals” mein <b>{email}</b> likhen. “Select a role” mein <b>Basic → Editor</b> chunen. <b>Save</b> dabayen.",
        "tip": "This only lets the bot manager set up this one project. It gives no access to your Gmail, files or YouTube.",
    },
    {
        "id": "c4", "who": "client", "title": "Send the Project ID",
        "urdu": "Project ID bhej dein",
        "link": f"{CONSOLE}/home/dashboard", "link_label": "Open: Project dashboard",
        "do": "In the “Project info” box, copy the <b>Project ID</b> (for example <i>my-youtube-bot-123456</i>) and send it to the bot manager. That is all.",
        "do_urdu": "“Project info” box mein se <b>Project ID</b> copy karke bot manager ko bhej dein. Bas.",
        "tip": "",
    },
    {
        "id": "y1", "who": "you", "title": "Open the client's project",
        "urdu": "Client ka project kholen",
        "link": f"{CONSOLE}/home/dashboard{{q}}", "link_label": "Open: project",
        "do": "With <b>your</b> Gmail, pick the client's project in the dropdown at the top (search for the Project ID they sent).",
        "do_urdu": "<b>Apne</b> Gmail se, upar dropdown mein client ka project chunen (us ki bheji hui Project ID se dhoondhen).",
        "tip": "Every next link opens on the project selected at the top, so check it first.",
    },
    {
        "id": "y2", "who": "you", "title": "Turn on YouTube Data API v3",
        "urdu": "YouTube Data API v3 on karen",
        "link": f"{CONSOLE}/apis/library/youtube.googleapis.com{{q}}", "link_label": "Open: YouTube Data API v3",
        "do": "Press <b>Enable</b>.",
        "do_urdu": "<b>Enable</b> dabayen.",
        "tip": "If it says “Manage” instead, it is already on.",
    },
    {
        "id": "y3", "who": "you", "title": "Create the sign-in screen",
        "urdu": "Sign-in screen banayen",
        "link": f"{CONSOLE}/auth/overview{{q}}", "link_label": "Open: Google Auth Platform",
        "do": "Press <b>Get started</b>. App name: <b>Story Bot</b>; support email: yours → <b>Next</b>. Audience: <b>External</b> → <b>Next</b>. Contact email: yours → <b>Next</b>. Tick the agreement → <b>Create</b>.",
        "do_urdu": "<b>Get started</b> → App name <b>Story Bot</b>, email apna → Audience <b>External</b> → contact email apna → agreement tick → <b>Create</b>.",
        "tip": "",
    },
    {
        "id": "y4", "who": "you", "title": "Publish it (In production)",
        "urdu": "Publish karen (In production)",
        "link": f"{CONSOLE}/auth/audience{{q}}", "link_label": "Open: Audience",
        "do": "Under “Publishing status” press <b>Publish app</b> → <b>Confirm</b>. It must say <b>In production</b>.",
        "do_urdu": "“Publishing status” ke neeche <b>Publish app</b> → <b>Confirm</b>. <b>In production</b> likha aana chahiye.",
        "tip": "Important: left on “Testing”, Google ends the sign-in every 7 days and uploads stop.",
    },
    {
        "id": "y5", "who": "you", "title": "Create a Desktop client",
        "urdu": "Desktop client banayen",
        "link": f"{CONSOLE}/auth/clients/create{{q}}", "link_label": "Open: Create client",
        "do": "Application type: <b>Desktop app</b>. Name: anything. Press <b>Create</b>.",
        "do_urdu": "Application type <b>Desktop app</b>, naam kuch bhi, <b>Create</b>.",
        "tip": "",
    },
    {
        "id": "y6", "who": "you", "title": "Download the JSON file",
        "urdu": "JSON file download karen",
        "link": "", "link_label": "",
        "do": "In the window that opens, press <b>Download JSON</b>. The file is named like <i>client_secret_….json</i>.",
        "do_urdu": "Jo window khulegi us mein <b>Download JSON</b> dabayen.",
        "tip": "Closed the window? Open “Clients”, click the client's name and use the download icon.",
    },
    {
        "id": "d1", "who": "dashboard", "title": "Upload the JSON to the channel",
        "urdu": "JSON file channel mein lagayen",
        "link": "", "link_label": "",
        "do": "On the channel page, in <b>Separate Google OAuth JSON</b>, choose the downloaded file and press <b>Save Separate Project</b>.",
        "do_urdu": "Channel ke page par <b>Separate Google OAuth JSON</b> mein file chunen aur <b>Save Separate Project</b> dabayen.",
        "tip": "Step 1 of the setup card turns green.",
    },
    {
        "id": "d2", "who": "dashboard", "title": "Connect YouTube",
        "urdu": "YouTube connect karen",
        "link": "", "link_label": "",
        "do": "Press <b>Connect &amp; verify</b> and choose the Gmail that <b>owns the YouTube channel</b>.",
        "do_urdu": "<b>Connect &amp; verify</b> dabayen aur woh Gmail chunen jis ka YouTube channel hai.",
        "tip": "If the client is not with you, they can do this on a short screen-share call.",
    },
    {
        "id": "d3", "who": "dashboard", "title": "Pass the “unverified app” screen",
        "urdu": "“Google hasn't verified this app” screen",
        "link": "", "link_label": "",
        "do": "Press <b>Advanced</b>, then <b>Go to Story Bot (unsafe)</b>.",
        "do_urdu": "<b>Advanced</b> → <b>Go to Story Bot (unsafe)</b>.",
        "tip": "Normal for your own app that Google has not reviewed. It is your project, not someone else's.",
    },
    {
        "id": "d4", "who": "dashboard", "title": "Pick the channel and allow",
        "urdu": "Channel chunen aur Allow karen",
        "link": "", "link_label": "",
        "do": "If the account has several YouTube channels, pick the right one. Tick both permissions and press <b>Continue</b>.",
        "do_urdu": "Kai channels hon to sahi channel chunen, dono permissions tick karke <b>Continue</b>.",
        "tip": "The dashboard then shows the YouTube channel's name. Check that it is the right one.",
    },
    {
        "id": "d5", "who": "dashboard", "title": "Send to GitHub",
        "urdu": "GitHub par bhejein",
        "link": "", "link_label": "",
        "do": "Press <b>⬆ Send to GitHub</b> on the setup card.",
        "do_urdu": "Setup card par <b>⬆ Send to GitHub</b> dabayen.",
        "tip": "From the next morning's run, this channel makes and uploads videos by itself.",
    },
]

WHO = {
    "client": ("Part 1 — The client does this (2 minutes)", "Hissa 1 — Client ka kaam"),
    "you": ("Part 2 — You do this in the client's project (5 minutes)", "Hissa 2 — Aap ka kaam, client ke project mein"),
    "dashboard": ("Part 3 — In this dashboard (2 minutes)", "Hissa 3 — Dashboard mein"),
}
_IDS = {step["id"] for step in STEPS}


def shot_path(step_id: str) -> Path | None:
    for ext in ("png", "jpg"):
        path = SHOT_DIR / f"{step_id}.{ext}"
        if path.is_file():
            return path
    return None


def save_shot(step_id: str, data: bytes) -> None:
    if step_id not in _IDS:
        raise KeyError(f"Unknown step {step_id}")
    if len(data) > _MAX_BYTES:
        raise ValueError("Screenshot is larger than 6 MB.")
    kind = next((v for magic, v in _FORMATS.items() if data.startswith(magic)), None)
    if not kind:
        raise ValueError("Only PNG or JPG screenshots can be added.")
    delete_shot(step_id)
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    (SHOT_DIR / f"{step_id}.{kind[0]}").write_bytes(data)


def delete_shot(step_id: str) -> None:
    if step_id not in _IDS:
        raise KeyError(f"Unknown step {step_id}")
    path = shot_path(step_id)
    if path:
        path.unlink()


def steps_for(project_id: str = "", email: str = "") -> list[dict]:
    """Steps with links and the operator's email filled in."""
    q = f"?project={project_id}" if project_id else ""
    who = html.escape(email.strip()) if email.strip() else "the bot manager's Gmail"
    rows = []
    for step in STEPS:
        row = dict(step)
        row["link"] = step["link"].replace("{q}", q)
        row["do"] = step["do"].replace("{email}", who)
        row["do_urdu"] = step["do_urdu"].replace("{email}", who)
        row["has_shot"] = shot_path(step["id"]) is not None
        rows.append(row)
    return rows


def client_guide_html(email: str) -> str:
    """A single self-contained page for the client: their four steps, with the
    screenshots embedded, ready to send on WhatsApp or email."""
    blocks = []
    for number, step in enumerate((s for s in steps_for(email=email) if s["who"] == "client"), start=1):
        shot = shot_path(step["id"])
        image = ""
        if shot:
            mime = "image/png" if shot.suffix == ".png" else "image/jpeg"
            data = base64.b64encode(shot.read_bytes()).decode("ascii")
            image = f'<img src="data:{mime};base64,{data}" alt="Screenshot for step {number}">'
        link = (f'<a class="btn" href="{step["link"]}" target="_blank" rel="noopener">{step["link_label"]} ↗</a>'
                if step["link"] else "")
        tip = f'<p class="tip">💡 {step["tip"]}</p>' if step["tip"] else ""
        blocks.append(
            f'<section><h2><span>{number}</span>{step["title"]}</h2>'
            f'<p>{step["do"]}</p><p class="ur">{step["do_urdu"]}</p>{link}{tip}{image}</section>'
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect your YouTube channel</title>
<style>
body{{margin:0;font-family:system-ui,'Segoe UI',Arial,sans-serif;background:#f4f6fb;color:#1c2240;line-height:1.55}}
main{{max-width:760px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:24px;margin:8px 0 4px}} .lead{{color:#4a5275;margin:0 0 18px}}
section{{background:#fff;border:1px solid #dfe3f0;border-radius:14px;padding:16px 18px;margin:0 0 16px}}
h2{{font-size:18px;margin:0 0 8px;display:flex;gap:10px;align-items:center}}
h2 span{{flex:0 0 30px;height:30px;border-radius:50%;background:#3d5afe;color:#fff;display:grid;place-items:center;font-size:15px}}
p{{margin:6px 0}} .ur{{color:#4a5275;font-size:15px}} .tip{{font-size:14px;color:#5b6283}}
.btn{{display:inline-block;margin:8px 0 4px;background:#3d5afe;color:#fff;text-decoration:none;padding:9px 14px;border-radius:10px;font-weight:600}}
img{{display:block;max-width:100%;height:auto;margin-top:12px;border:1px solid #dfe3f0;border-radius:10px}}
</style></head><body><main>
<h1>Connect your YouTube channel</h1>
<p class="lead">Four short steps on a computer, about 2 minutes. Sign in to Google with the Gmail that owns your YouTube channel first.<br>
<span class="ur">Computer par 4 chhote qadam, taqreeban 2 minute. Pehle us Gmail se Google mein sign in karen jis ka YouTube channel hai.</span></p>
{''.join(blocks)}
<p class="tip">Nothing here shares your password. You can remove the access at any time from the same IAM page.</p>
</main></body></html>"""
