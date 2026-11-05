"""When a video went up, or goes up, on each platform.

Shared by the dashboard's Video Library and the public status page, so both
show the same thing: per platform, the steps done (uploaded, public / live)
and the steps still to come (goes public at, next automatic try).
"""
from __future__ import annotations

from datetime import datetime, timezone
def _when(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def platform_timeline(v, socials: dict, social_cfg: dict, upload_pause) -> list[dict]:
    """Per platform: when the video went up / goes live, or when the next try is.

    Each step is {"state": done|wait|fail, "text": str, "when": iso or None}.
    """
    from . import slots

    now = datetime.now(timezone.utc)
    status, url = v["status"], v["video_url"]
    publish = _when(v["publish_at"])
    next_try = upload_pause[1] if upload_pause else slots.next_run().isoformat()
    yt: list[dict] = []
    if url and status in ("uploaded", "scheduled", "thumbnail_pending", "remote_missing"):
        yt.append({"state": "done", "text": "Uploaded", "when": v["upload_date"]})
        if status == "thumbnail_pending":
            yt.append({"state": "wait", "text": "Private until its thumbnail is set · next try",
                       "when": next_try})
            if publish and publish > now:
                yt.append({"state": "wait", "text": "Booked to go public", "when": v["publish_at"]})
        elif publish and publish > now:
            yt.append({"state": "wait", "text": "Goes public", "when": v["publish_at"]})
        elif publish:
            yt.append({"state": "done", "text": "Public since", "when": v["publish_at"]})
        else:
            yt.append({"state": "done", "text": "Live", "when": v["upload_date"]})
    elif status == "uploading":
        yt.append({"state": "wait", "text": f"Uploading now ({v['upload_progress']}%)", "when": None})
    elif status in ("rendered", "daily_upload_pending", "upload_failed"):
        yt.append({"state": "wait", "text": "Not uploaded yet · next automatic try", "when": next_try})
        if publish and publish > now:
            yt.append({"state": "wait", "text": "Booked to go public", "when": v["publish_at"]})
    elif status == "awaiting_approval":
        yt.append({"state": "wait", "text": "Waiting for your approval", "when": None})
    elif status == "facebook_only":
        yt.append({"state": "fail", "text": "No YouTube channel connected", "when": None})
    else:
        yt.append({"state": "fail", "text": f"Will not upload ({status.replace('_', ' ')})", "when": None})
    rows = [{"name": "YouTube", "icon": "▶", "link": url, "steps": yt}]

    for platform, label, icon in (("facebook", "Facebook", "f"), ("tiktok", "TikTok", "♪")):
        item = socials.get(platform)
        cfg = social_cfg.get(platform, {})
        steps: list[dict] = []
        if item:
            live = _when(item.get("scheduled_for"))
            if item["status"] == "submitted":
                steps.append({"state": "done", "text": "Uploaded", "when": item["updated_at"]})
                if live and live > now:
                    steps.append({"state": "wait", "text": "Goes live", "when": item["scheduled_for"]})
                elif live:
                    steps.append({"state": "done", "text": "Live since", "when": item["scheduled_for"]})
            elif item["status"] == "uploading":
                steps.append({"state": "wait", "text": "Uploading now", "when": item["updated_at"]})
            else:
                steps.append({"state": "fail", "text": "Failed: " + str(item.get("error") or "")[:90],
                              "when": item["updated_at"]})
        elif cfg.get("configured") and cfg.get("auto_upload"):
            if status in ("quality_failed", "duplicate_blocked", "files_lost", "awaiting_approval"):
                continue
            steps.append({"state": "wait", "text": "Not posted yet · posts with YouTube at",
                          "when": v["publish_at"] or next_try})
        else:
            continue  # this platform is not set up to post automatically
        rows.append({"name": label, "icon": icon, "link": (item or {}).get("post_url"), "steps": steps})
    return rows

