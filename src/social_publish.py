from __future__ import annotations

import json
import math
import mimetypes
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .db import set_social_upload, video_for_social_upload
from .social_accounts import account, save_account

FB_API_VERSION = "v24.0"
TT_API = "https://open.tiktokapis.com"
USER_AGENT = "StoryBotStudio/1.0"


def _decode_response(response) -> dict:
    raw = response.read().decode("utf-8", errors="replace")
    if not raw:
        return {}
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("error"):
        error = data["error"]
        if isinstance(error, dict) and error.get("code") not in {None, "ok"}:
            raise RuntimeError(error.get("message") or error.get("code"))
    return data


def _request_json(url: str, method: str = "GET", payload: dict | None = None,
                  headers: dict[str, str] | None = None, timeout: int = 90) -> dict:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    merged = {"User-Agent": USER_AGENT, **(headers or {})}
    if body is not None:
        merged.setdefault("Content-Type", "application/json; charset=UTF-8")
    request = Request(url, data=body, headers=merged, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return _decode_response(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            err = parsed.get("error", parsed)
            if isinstance(err, dict):
                detail = err.get("message") or err.get("code") or detail
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _request_form(url: str, payload: dict[str, str]) -> dict:
    body = urlencode(payload).encode("utf-8")
    request = Request(url, data=body, headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
    }, method="POST")
    try:
        with urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    if data.get("error"):
        raise RuntimeError(str(data.get("error_description") or data.get("error")))
    return data


def _refresh_tiktok(channel_id: str) -> str:
    cfg = account(channel_id, "tiktok")
    required = [str(cfg.get(key, "")) for key in ("client_key", "client_secret", "refresh_token")]
    if not all(required):
        raise RuntimeError("TikTok token expired. Save Client Key, Client Secret and Refresh Token to renew it automatically.")
    result = _request_form(f"{TT_API}/v2/oauth/token/", {
        "client_key": required[0], "client_secret": required[1],
        "grant_type": "refresh_token", "refresh_token": required[2],
    })
    access_token = str(result.get("access_token", ""))
    if not access_token:
        raise RuntimeError("TikTok refresh response did not include a new access token.")
    save_account(channel_id, "tiktok", {
        "access_token": access_token,
        "refresh_token": str(result.get("refresh_token") or required[2]),
    })
    return access_token

def _send_binary(url: str, data: bytes, headers: dict[str, str], method: str) -> dict:
    request = Request(url, data=data, headers={"User-Agent": USER_AGENT, **headers}, method=method)
    try:
        with urlopen(request, timeout=600) as response:
            return _decode_response(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def verify_facebook(channel_id: str) -> str:
    cfg = account(channel_id, "facebook")
    page_id, token = cfg.get("page_id"), cfg.get("page_access_token")
    if not page_id or not token:
        raise RuntimeError("Save Facebook Page ID and Page Access Token first.")
    query = urlencode({"fields": "id,name", "access_token": token})
    data = _request_json(f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}?{query}")
    return str(data.get("name") or data.get("id") or "Facebook Page")


def _facebook_reel(video: Path, title: str, token: str) -> tuple[str, str]:
    start_query = urlencode({"access_token": token, "upload_phase": "start"})
    started = _request_json(
        f"https://graph.facebook.com/{FB_API_VERSION}/me/video_reels?{start_query}", method="POST"
    )
    video_id = str(started.get("video_id") or "")
    upload_url = str(started.get("upload_url") or "")
    if not video_id or not upload_url:
        raise RuntimeError("Facebook did not return a Reel upload session.")
    payload = video.read_bytes()
    uploaded = _send_binary(upload_url, payload, {
        "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(len(payload)),
        "Content-Type": "application/octet-stream",
    }, "POST")
    if uploaded.get("success") is False:
        raise RuntimeError("Facebook Reel file transfer failed.")
    finish_query = urlencode({
        "access_token": token, "video_id": video_id, "upload_phase": "finish",
        "video_state": "PUBLISHED", "description": title, "title": title,
    })
    finished = _request_json(
        f"https://graph.facebook.com/{FB_API_VERSION}/me/video_reels?{finish_query}", method="POST"
    )
    if finished.get("success") is False:
        raise RuntimeError("Facebook accepted the file but did not publish the Reel.")
    return video_id, f"https://www.facebook.com/reel/{video_id}"


def _multipart(fields: dict[str, str], file_field: str, path: Path) -> tuple[bytes, str]:
    boundary = "----StoryBot" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"), b"\r\n",
        ])
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(), path.read_bytes(), b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _facebook_long(video: Path, title: str, page_id: str, token: str) -> tuple[str, str]:
    body, content_type = _multipart(
        {"access_token": token, "title": title, "description": title}, "source", video,
    )
    data = _send_binary(
        f"https://graph-video.facebook.com/{FB_API_VERSION}/{page_id}/videos",
        body, {"Content-Type": content_type}, "POST",
    )
    video_id = str(data.get("id") or "")
    if not video_id:
        raise RuntimeError("Facebook did not return a video ID.")
    return video_id, f"https://www.facebook.com/{video_id}"


def upload_facebook(channel_id: str, video: Path, title: str, content_type: str) -> tuple[str, str]:
    cfg = account(channel_id, "facebook")
    page_id, token = str(cfg.get("page_id", "")), str(cfg.get("page_access_token", ""))
    if not page_id or not token:
        raise RuntimeError("Facebook is not configured for this channel.")
    if content_type == "short":
        return _facebook_reel(video, title, token)
    return _facebook_long(video, title, page_id, token)


def _tiktok_token(channel_id: str) -> str:
    cfg = account(channel_id, "tiktok")
    token = str(cfg.get("access_token", ""))
    if not token:
        raise RuntimeError("TikTok Access Token is not configured for this channel.")
    return token


def _verify_tiktok_token(token: str) -> str:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        data = _request_json(
            f"{TT_API}/v2/post/publish/creator_info/query/", method="POST", payload={}, headers=headers,
        )
        creator = data.get("data", {})
        return str(creator.get("creator_nickname") or creator.get("creator_username") or "TikTok creator")
    except Exception as first:
        try:
            data = _request_json(
                f"{TT_API}/v2/user/info/?fields=open_id,display_name", headers=headers,
            )
            user = data.get("data", {}).get("user", {})
            return str(user.get("display_name") or user.get("open_id") or "TikTok account")
        except Exception:
            raise first


def verify_tiktok(channel_id: str) -> str:
    try:
        return _verify_tiktok_token(_tiktok_token(channel_id))
    except Exception as first:
        try:
            return _verify_tiktok_token(_refresh_tiktok(channel_id))
        except Exception as refresh_error:
            raise RuntimeError(f"TikTok token could not be verified: {first}. Refresh failed: {refresh_error}") from first


def _tiktok_init(video: Path, title: str, token: str, mode: str, privacy: str) -> tuple[str, str, int]:
    size = video.stat().st_size
    chunk_size = size if size < 5 * 1024 * 1024 else min(32 * 1024 * 1024, size)
    count = max(1, math.ceil(size / chunk_size))
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": count}
    headers = {"Authorization": f"Bearer {token}"}
    if mode == "direct":
        url = f"{TT_API}/v2/post/publish/video/init/"
        payload = {
            "post_info": {
                "title": title[:2200], "privacy_level": privacy,
                "disable_duet": False, "disable_comment": False, "disable_stitch": False,
                "brand_content_toggle": False, "brand_organic_toggle": False, "is_aigc": True,
            },
            "source_info": source,
        }
    else:
        url = f"{TT_API}/v2/post/publish/inbox/video/init/"
        payload = {"source_info": source}
    result = _request_json(url, method="POST", payload=payload, headers=headers)
    data = result.get("data", {})
    publish_id, upload_url = str(data.get("publish_id", "")), str(data.get("upload_url", ""))
    if not publish_id or not upload_url:
        raise RuntimeError("TikTok did not return an upload session.")
    return publish_id, upload_url, chunk_size


def _tiktok_transfer(video: Path, upload_url: str, chunk_size: int) -> None:
    total = video.stat().st_size
    start = 0
    with video.open("rb") as handle:
        while start < total:
            chunk = handle.read(chunk_size)
            end = start + len(chunk) - 1
            _send_binary(upload_url, chunk, {
                "Content-Type": "video/mp4", "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            }, "PUT")
            start = end + 1


def upload_tiktok(channel_id: str, video: Path, title: str,
                  direct_consent: bool = False, force_mode: str | None = None) -> tuple[str, str | None]:
    cfg = account(channel_id, "tiktok")
    token = _tiktok_token(channel_id)
    mode = force_mode or str(cfg.get("mode", "draft"))
    if mode == "direct" and not direct_consent:
        raise RuntimeError("TikTok Direct Post requires explicit confirmation for this upload.")
    privacy = str(cfg.get("privacy", "SELF_ONLY"))
    try:
        publish_id, upload_url, chunk_size = _tiktok_init(video, title, token, mode, privacy)
    except Exception as exc:
        lower = str(exc).lower()
        if "401" not in lower and "access_token_invalid" not in lower and "token" not in lower:
            raise
        token = _refresh_tiktok(channel_id)
        publish_id, upload_url, chunk_size = _tiktok_init(video, title, token, mode, privacy)
    _tiktok_transfer(video, upload_url, chunk_size)
    return publish_id, None


def publish_video(video_id: int, platform: str, channel_id: str | None = None,
                  direct_consent: bool = False, force_tiktok_mode: str | None = None) -> str:
    row = video_for_social_upload(video_id, channel_id)
    if not row:
        raise RuntimeError("Video record not found.")
    path = Path(row["video_path"])
    if not path.exists():
        raise RuntimeError("Local video file is missing; restore it from Drive first.")
    platform = platform.lower()
    set_social_upload(video_id, platform, "uploading")
    try:
        if platform == "facebook":
            remote_id, post_url = upload_facebook(row["channel"], path, row["title"], row["content_type"])
            message = f"Facebook upload complete: {post_url}"
        elif platform == "tiktok":
            remote_id, post_url = upload_tiktok(
                row["channel"], path, row["title"], direct_consent, force_tiktok_mode,
            )
            mode = force_tiktok_mode or account(row["channel"], "tiktok").get("mode", "draft")
            message = "Sent to TikTok drafts" if mode != "direct" else "TikTok Direct Post submitted"
        else:
            raise RuntimeError("Unsupported social platform.")
        set_social_upload(video_id, platform, "submitted", remote_id, post_url)
        return message
    except Exception as exc:
        set_social_upload(video_id, platform, "failed", error=str(exc))
        raise


def auto_publish_video(video_id: int, channel_id: str) -> list[str]:
    messages: list[str] = []
    fb = account(channel_id, "facebook")
    tt = account(channel_id, "tiktok")
    if fb.get("auto_upload") and fb.get("page_id") and fb.get("page_access_token"):
        try:
            messages.append(publish_video(video_id, "facebook", channel_id))
        except Exception as exc:
            messages.append(f"Facebook failed: {exc}")
    if tt.get("auto_upload") and tt.get("access_token"):
        try:
            # TikTok guidelines require per-post consent for Direct Post. Automatic
            # cross-posting therefore safely exports to the creator's drafts.
            messages.append(publish_video(video_id, "tiktok", channel_id, force_tiktok_mode="draft"))
        except Exception as exc:
            messages.append(f"TikTok failed: {exc}")
    return messages