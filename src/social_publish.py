from __future__ import annotations

import hashlib
import json
import re
import mimetypes
import secrets
import time
import uuid
from datetime import timedelta, timezone
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
                # Keep TikTok's error code: its message is often only a link
                # to the guidelines, and the code says what actually went wrong.
                code, message = err.get("code"), err.get("message")
                detail = f"{code}: {message}" if code and message else (message or code or detail)
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


def tiktok_login_url(channel_id: str, redirect_uri: str) -> tuple[str, str, str]:
    """(TikTok sign-in URL, code verifier, state) for the dashboard's Login button.

    Uses TikTok's Desktop Login Kit, which accepts an http://localhost redirect,
    so the local dashboard needs no public https address. TikTok wants the PKCE
    challenge as the HEX SHA-256 of the verifier, not base64url.
    """
    cfg = account(channel_id, "tiktok")
    if not (cfg.get("client_key") and cfg.get("client_secret")):
        raise RuntimeError("Save the Client Key and Client Secret first, then click Login with TikTok.")
    verifier = secrets.token_urlsafe(48)  # 64 chars from A-Z a-z 0-9 - _
    state = secrets.token_urlsafe(16)
    scopes = "user.info.basic,video.upload"
    if cfg.get("mode") in {"direct", "private"}:
        scopes += ",video.publish"
    query = urlencode({
        "client_key": cfg["client_key"], "scope": scopes, "response_type": "code",
        "redirect_uri": redirect_uri, "state": state,
        "code_challenge": hashlib.sha256(verifier.encode("ascii")).hexdigest(),
        "code_challenge_method": "S256",
    })
    return f"https://www.tiktok.com/v2/auth/authorize/?{query}", verifier, state


def tiktok_finish_login(channel_id: str, code: str, verifier: str, redirect_uri: str) -> str:
    """Swap the code TikTok sent back for tokens, save them, return the account name."""
    cfg = account(channel_id, "tiktok")
    result = _request_form(f"{TT_API}/v2/oauth/token/", {
        "client_key": str(cfg.get("client_key", "")), "client_secret": str(cfg.get("client_secret", "")),
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    })
    access_token = str(result.get("access_token", ""))
    if not access_token:
        raise RuntimeError("TikTok did not return an access token.")
    save_account(channel_id, "tiktok", {
        "access_token": access_token, "refresh_token": str(result.get("refresh_token", "")),
    })
    return _verify_tiktok_token(access_token)

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
    from .social_accounts import mark_verified

    query = urlencode({"fields": "id,name", "access_token": token})
    try:
        data = _request_json(f"https://graph.facebook.com/{FB_API_VERSION}/{page_id}?{query}")
        # A personal token can read a Page (or an app) by its ID, so that
        # lookup alone proves nothing. A real Page token is the Page itself.
        me = _request_json(
            f"https://graph.facebook.com/{FB_API_VERSION}/me?" + urlencode({"fields": "id", "access_token": token})
        )
        if str(me.get("id")) != str(page_id):
            raise RuntimeError(
                "The saved token is not this Page's own token, so nothing can be posted with it. "
                "Paste the Graph API Explorer token into the 'Fast way' box instead; it finds the "
                "right Page ID and Page token by itself."
            )
        # A person's own token with their own user ID also passes the check
        # above, and then every post fails. Only a PAGE token can post.
        debug = _request_json(
            f"https://graph.facebook.com/{FB_API_VERSION}/debug_token?"
            + urlencode({"input_token": token, "access_token": token})
        ).get("data") or {}
        if str(debug.get("type", "")).upper() != "PAGE":
            raise RuntimeError(
                f"This is the personal account '{data.get('name', '')}', not a Page: its USER token "
                "and user ID were saved. Paste that same token into the 'Fast way' box below; it "
                "finds the Page and saves the Page's own ID and token."
            )
    except Exception:
        mark_verified(channel_id, "facebook", "")
        raise
    name = str(data.get("name") or data.get("id") or "Facebook Page")
    mark_verified(channel_id, "facebook", name)
    return name


def facebook_from_user_token(channel_id: str, user_token: str, page_id: str = "") -> str:
    """Save a Page's ID and token found through the user's own token.

    Spares the me/accounts step in the Graph API Explorer: the person pastes
    the user token the Explorer gave them and picks nothing else, unless they
    manage several Pages (then the Page ID field picks one). Returns a message
    for the dashboard; warns when the token will expire.
    """
    from .social_accounts import save_account

    user_token = user_token.strip()
    if not user_token:
        raise RuntimeError("Paste the token from the Graph API Explorer first.")
    query = urlencode({"fields": "id,name,access_token", "access_token": user_token, "limit": "100"})
    pages = _request_json(f"https://graph.facebook.com/{FB_API_VERSION}/me/accounts?{query}").get("data") or []
    if not pages:
        raise RuntimeError(
            "This token sees no Pages. In the Explorer, tick the Page when Facebook asks, "
            "and add pages_show_list, pages_read_engagement and pages_manage_posts."
        )
    wanted = str(page_id or "").strip()
    page = next((p for p in pages if p.get("id") == wanted), None) if wanted else (pages[0] if len(pages) == 1 else None)
    if page is None:
        listing = "; ".join(f"{p.get('name')} = {p.get('id')}" for p in pages)
        raise RuntimeError(f"This account manages several Pages. Put the right one's ID in Facebook Page ID and press again: {listing}")
    save_account(channel_id, "facebook", {"page_id": page["id"], "page_access_token": page["access_token"]})
    name = verify_facebook(channel_id)
    debug = _request_json(
        f"https://graph.facebook.com/{FB_API_VERSION}/debug_token?"
        + urlencode({"input_token": page["access_token"], "access_token": page["access_token"]})
    ).get("data") or {}
    if int(debug.get("expires_at") or 0):
        return (f"Saved {name}, but this Page token expires soon because the pasted token was short-lived. "
                "Extend the user token in the Access Token Debugger and paste the long one here again.")
    return f"Saved and verified: {name}. The Page token does not expire."


def _tag(text: object) -> str:
    """'#WorldValorChronicles' from 'World Valor Chronicles'; '' when nothing is left."""
    words = re.findall(r"[^\W_]+", str(text), flags=re.UNICODE)
    joined = "".join(word if word.isupper() else word.capitalize() for word in words)[:40]
    return f"#{joined}" if joined else ""


def facebook_tags(channel_id: str, topic: str, content_type: str, title: str) -> str:
    """The hashtags every Facebook video post carries.

    A fact-only channel is tagged with the person the video is about rather
    than the channel's search keywords, which would label most of them wrongly
    ("#AncientCivilizations" under a twentieth-century doctor).
    """
    tags: list[str] = []
    try:
        from .channels import get_channel
        from .verified_facts import _bank_rows, is_verified_channel

        channel = get_channel(channel_id)
        if is_verified_channel(channel):
            subject = _bank_rows(channel).get(topic, {}).get("source_title")
            tags += [_tag(subject or ""), "#TrueStory", "#RealHeroes"]
        else:
            from .seo import _GENRE_KEYWORDS

            words = list(getattr(channel, "keywords", ()) or ()) or _GENRE_KEYWORDS.get(channel.genre, [])
            tags += [_tag(word) for word in words[:4]]
        tags += [_tag(channel.genre), _tag(channel.name)]
    except Exception:
        pass
    if re.search(r"[؀-ۿ]", title):  # Urdu script
        tags += ["#اردو", "#Urdu", "#Pakistan"]
    tags.append("#Reels" if content_type == "short" else "#Video")
    unique = list(dict.fromkeys(tag for tag in tags if tag))
    return " ".join(unique[:10])


def _facebook_reel(video: Path, title: str, token: str, description: str = "") -> tuple[str, str]:
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
        "video_state": "PUBLISHED", "description": description or title, "title": title,
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


def _multipart_bytes(fields: dict[str, str], file_field: str = "", filename: str = "",
                     data: bytes | None = None) -> tuple[bytes, str]:
    """A multipart/form-data body; the file part is optional."""
    boundary = "----StoryBot" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"), b"\r\n",
        ])
    if data is not None:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n", data, b"\r\n",
        ])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _facebook_long(video: Path, title: str, page_id: str, token: str,
                   publish_at: int | None = None, description: str = "") -> tuple[str, str]:
    """Upload a Page video in chunks (Facebook's resumable upload).

    Sending a 100 MB+ file in one request gets the connection cut midway, so
    the file goes up in the pieces Facebook asks for, and the title, text and
    optional schedule are set when the upload is finished.
    """
    url = f"https://graph-video.facebook.com/{FB_API_VERSION}/{page_id}/videos"
    payload = video.read_bytes()

    def post(fields: dict[str, str], chunk: bytes | None = None) -> dict:
        body, ctype = _multipart_bytes(fields, "video_file_chunk", "chunk.mp4", chunk)
        for attempt in range(4):
            try:
                return _send_binary(url, body, {"Content-Type": ctype}, "POST")
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(3 * (attempt + 1))
        return {}

    started = post({"access_token": token, "upload_phase": "start", "file_size": str(len(payload))})
    session, video_id = str(started.get("upload_session_id") or ""), str(started.get("video_id") or "")
    if not session or not video_id:
        raise RuntimeError(f"Facebook did not open an upload session: {started}")
    begin, end = int(started.get("start_offset", 0)), int(started.get("end_offset", 0))
    while begin < end:
        step = post({"access_token": token, "upload_phase": "transfer",
                     "upload_session_id": session, "start_offset": str(begin)}, payload[begin:end])
        begin, end = int(step.get("start_offset", end)), int(step.get("end_offset", end))
    finish = {"access_token": token, "upload_phase": "finish", "upload_session_id": session,
              "title": title, "description": description or title}
    if publish_at:
        # Facebook publishes it by itself at this Unix time (10+ minutes ahead).
        finish.update({"published": "false", "scheduled_publish_time": str(int(publish_at))})
    done = post(finish)
    if done.get("success") is False:
        raise RuntimeError("Facebook received the video but did not accept it.")
    return video_id, f"https://www.facebook.com/{video_id}"


def upload_facebook(channel_id: str, video: Path, title: str, content_type: str,
                    description: str = "") -> tuple[str, str]:
    cfg = account(channel_id, "facebook")
    page_id, token = str(cfg.get("page_id", "")), str(cfg.get("page_access_token", ""))
    if not page_id or not token:
        raise RuntimeError("Facebook is not configured for this channel.")
    if content_type == "short":
        return _facebook_reel(video, title, token, description)
    return _facebook_long(video, title, page_id, token, description=description)


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
    # TikTok's rules: a file up to 64 MB goes whole, with chunk_size equal to
    # the file size ("The chunk size is invalid" otherwise). Bigger files go in
    # 5-64 MB chunks, counted as size // chunk_size ROUNDED DOWN; the leftover
    # bytes ride in the last chunk (which may grow to 128 MB). Rounding up is
    # rejected with "The total chunk count is invalid".
    chunk_size = size if size <= 64 * 1024 * 1024 else 32 * 1024 * 1024
    count = _tiktok_chunk_count(size, chunk_size)
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


def _tiktok_chunk_count(size: int, chunk_size: int) -> int:
    return max(1, size // chunk_size)


def _tiktok_transfer(video: Path, upload_url: str, chunk_size: int) -> None:
    total = video.stat().st_size
    count = _tiktok_chunk_count(total, chunk_size)
    start = 0
    with video.open("rb") as handle:
        for index in range(count):
            # The last chunk takes everything that is left.
            chunk = handle.read() if index == count - 1 else handle.read(chunk_size)
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
    if mode == "private":
        # A post only the creator can see: it waits in TikTok Studio until the
        # creator sets it to Everyone, so the creator still decides what goes
        # public. Used instead of drafts, which do not reach a sandbox app's inbox.
        mode, privacy = "direct", "SELF_ONLY"
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



# ------------------------------------------------------------------ Zernio
# Zernio (zernio.com, formerly getlate.dev) is a posting service whose own
# TikTok app is already audited, so it can publish PUBLIC posts by API with no
# click per video. Free for the first 2 connected accounts, unlimited posts.
ZERNIO_API = "https://zernio.com/api/v1"


def _zernio_headers(channel_id: str) -> dict[str, str]:
    key = str(account(channel_id, "tiktok").get("zernio_api_key", ""))
    if not key:
        raise RuntimeError("Zernio API key is not saved for this channel.")
    return {"Authorization": f"Bearer {key}"}


def _zernio_tiktok_account(channel_id: str) -> tuple[str, str]:
    """(accountId, @username) of THIS channel's TikTok account in Zernio.

    One Zernio key can hold several TikTok accounts (one per channel), so the
    channel names its @username. With a single TikTok account connected that
    one is used; with several and no username saved, nothing is guessed.
    """
    cfg = account(channel_id, "tiktok")
    data = _request_json(f"{ZERNIO_API}/accounts", headers=_zernio_headers(channel_id))
    tiktoks = [a for a in data.get("accounts", []) if a.get("platform") == "tiktok" and a.get("isActive", True)]
    if not tiktoks:
        raise RuntimeError("No TikTok account is connected in Zernio. Connect it at zernio.com first.")
    wanted = str(cfg.get("zernio_username", "")).strip().lstrip("@").lower()
    if wanted:
        matches = [a for a in tiktoks if str(a.get("username", "")).lstrip("@").lower() == wanted]
        if not matches:
            names = ", ".join("@" + str(a.get("username", "?")) for a in tiktoks)
            raise RuntimeError(f"@{wanted} is not connected in Zernio (connected: {names}).")
        chosen = matches[0]
    elif len(tiktoks) == 1:
        chosen = tiktoks[0]
    else:
        names = ", ".join("@" + str(a.get("username", "?")) for a in tiktoks)
        raise RuntimeError(f"Zernio has several TikTok accounts ({names}). "
                           "Write this channel's one in 'Zernio TikTok username' and Save.")
    if chosen.get("_id") != cfg.get("zernio_account_id"):
        save_account(channel_id, "tiktok", {"zernio_account_id": str(chosen.get("_id", ""))})
    return str(chosen.get("_id", "")), str(chosen.get("username") or chosen.get("displayName") or "TikTok")


def verify_zernio(channel_id: str) -> str:
    _, username = _zernio_tiktok_account(channel_id)
    return f"{username} (via Zernio)"


def tiktok_caption(row) -> str:
    """Title plus the channel's hashtags, within TikTok's 2,200 characters."""
    tags = facebook_tags(row["channel"], str(row["topic"] or ""), row["content_type"], row["title"])
    return f"{row['title']}\n\n{tags}".strip()[:2200]


def _entry_account_id(entry: dict) -> str:
    """Zernio lists the account as an object; posts may also carry the bare id."""
    account_ref = entry.get("accountId")
    return str(account_ref.get("_id", "")) if isinstance(account_ref, dict) else str(account_ref or "")


def _parse_time(value) -> "datetime | None":
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def tiktok_slot(channel_id: str, account_id: str, youtube_time=None) -> "datetime":
    """When this video should go live on TikTok.

    Same moment as its scheduled YouTube release when there is one, so both
    platforms drip out together. Otherwise the next free slot: at least the
    YouTube gap (3 h) after the latest TikTok post of this account, already
    published or still waiting, and never in the past. Zernio drops uploaded
    media after 7 days, so a slot is never more than 6 days ahead.
    """
    from datetime import datetime, timedelta, timezone

    from .config import settings

    now = datetime.now(timezone.utc)
    latest_allowed = now + timedelta(days=6)
    wanted = _parse_time(youtube_time)
    if wanted and wanted > now:
        return min(wanted, latest_allowed)
    gap = timedelta(hours=settings.youtube_schedule_interval_hours)
    latest = None
    data = _request_json(f"{ZERNIO_API}/posts?limit=50", headers=_zernio_headers(channel_id))
    for post in data.get("posts", []):
        mine = [e for e in post.get("platforms", []) or []
                if e.get("platform") == "tiktok" and _entry_account_id(e) == account_id]
        if not mine:
            continue
        if any(e.get("status") == "failed" for e in mine):
            continue
        when = _parse_time(mine[0].get("publishedAt") or mine[0].get("scheduledFor") or post.get("scheduledFor"))
        if when and (latest is None or when > latest):
            latest = when
    slot = now + timedelta(minutes=2)
    if latest and latest + gap > slot:
        slot = latest + gap
    return min(slot, latest_allowed)


def upload_tiktok_zernio(channel_id: str, video: Path, caption: str,
                         youtube_time=None) -> tuple[str, str | None, "datetime"]:
    """Put the video on TikTok publicly through Zernio, at its own time slot.

    Videos made in one run used to go live together; each now waits for its
    slot (see tiktok_slot), so the account posts one at a time through the day.
    """
    headers = _zernio_headers(channel_id)
    account_id, _ = _zernio_tiktok_account(channel_id)
    slot = tiktok_slot(channel_id, account_id, youtube_time)
    presign = _request_json(f"{ZERNIO_API}/media/presign", method="POST", headers=headers, payload={
        "filename": video.name, "contentType": "video/mp4", "size": video.stat().st_size,
    })
    upload_url, public_url = presign.get("uploadUrl"), presign.get("publicUrl")
    if not (upload_url and public_url):
        raise RuntimeError("Zernio did not return an upload address.")
    # Goes to storage, not the API: no Authorization header.
    _send_binary(upload_url, video.read_bytes(), {"Content-Type": "video/mp4"}, "PUT")
    result = _request_json(f"{ZERNIO_API}/posts", method="POST", headers=headers, timeout=300, payload={
        "content": caption,
        "mediaItems": [{"type": "video", "url": public_url}],
        "platforms": [{"platform": "tiktok", "accountId": account_id}],
        "tiktokSettings": {
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "allow_comment": True, "allow_duet": True, "allow_stitch": True,
            "video_cover_timestamp_ms": 1000,
            # AI pictures and an AI voice: TikTok asks for the label.
            "video_made_with_ai": True,
            "draft": False,
            "content_preview_confirmed": True, "express_consent_given": True,
        },
        "scheduledFor": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    post = result.get("post", result)
    post_id = str(post.get("_id") or post.get("id") or "")
    url = None
    for entry in post.get("platforms", []) or []:
        if entry.get("platform") == "tiktok":
            url = entry.get("platformPostUrl") or url
            if entry.get("status") == "failed":
                raise RuntimeError(f"Zernio: TikTok rejected the post: {entry.get('errorMessage') or entry}")
    return post_id, url, slot



def recent_zernio_posts(channel_id: str, limit: int = 8) -> list[dict]:
    """The latest posts Zernio made for this account, wherever they were made.

    Zernio publishes after the bot has moved on, and the daily videos are made
    on GitHub, so this asks Zernio directly instead of the local database.
    """
    data = _request_json(f"{ZERNIO_API}/posts?limit={int(limit)}", headers=_zernio_headers(channel_id))
    rows = []
    for post in data.get("posts", []):
        for entry in post.get("platforms", []) or []:
            if entry.get("platform") != "tiktok":
                continue
            rows.append({
                "title": str(post.get("content", "")).split("\n", 1)[0][:120],
                "status": entry.get("status") or post.get("status") or "?",
                "url": entry.get("platformPostUrl") or "",
                "error": entry.get("errorMessage") or "",
                # A waiting post shows when it will go live.
                "time": entry.get("publishedAt") or entry.get("scheduledFor") or post.get("scheduledFor") or post.get("createdAt") or "",
            })
    return rows


def publish_video(video_id: int, platform: str, channel_id: str | None = None,
                  direct_consent: bool = False, force_tiktok_mode: str | None = None,
                  video_override: Path | None = None, title_override: str | None = None) -> str:
    row = video_for_social_upload(video_id, channel_id)
    if not row:
        raise RuntimeError("Video record not found.")
    if video_override or title_override:
        # The Facebook copy in another language (dual_language.py).
        row = {**dict(row), "video_path": str(video_override or row["video_path"]),
               "title": title_override or row["title"]}
    path = Path(row["video_path"])
    if not path.exists():
        raise RuntimeError("Local video file is missing; restore it from Drive first.")
    platform = platform.lower()
    if platform == "facebook":
        from .social_accounts import facebook_elsewhere_reason, facebook_posts_here

        if not facebook_posts_here():
            raise RuntimeError(facebook_elsewhere_reason())
    set_social_upload(video_id, platform, "uploading")
    try:
        if platform == "facebook":
            # Title, the story in full (a second-language copy writes it to
            # post.txt), the source of a fact-checked story, then the tags.
            from .verified_facts import source_url_for

            source = source_url_for(row["channel"], str(row["topic"] or ""))
            body_file = path.parent / "post.txt"
            body = body_file.read_text(encoding="utf-8").strip() if video_override and body_file.exists() else ""
            urdu = bool(re.search(r"[؀-ۿ]", row["title"]))
            description = "\n\n".join(filter(None, [
                row["title"],
                body,
                (f"ماخذ: ویکیپیڈیا — {source}" if urdu else f"Source: Wikipedia — {source}") if source else "",
                ("کہانی اچھی لگی ہو تو لائک کریں، پیج فالو کریں اور کمنٹ میں اپنی رائے دیں۔"
                 if urdu else "Like, follow the page and tell us in the comments what you think.") if body else "",
                facebook_tags(row["channel"], str(row["topic"] or ""), row["content_type"], row["title"]),
            ]))
            remote_id, post_url = upload_facebook(
                row["channel"], path, row["title"], row["content_type"], description,
            )
            message = f"Facebook upload complete: {post_url}"
        elif platform == "tiktok" and (force_tiktok_mode or account(row["channel"], "tiktok").get("mode")) == "zernio":
            remote_id, post_url, slot = upload_tiktok_zernio(
                row["channel"], path, tiktok_caption(row), row["publish_at"],
            )
            pkt = slot.astimezone(timezone(timedelta(hours=5))).strftime("%d %b %I:%M %p PKT")
            message = f"TikTok public post via Zernio, goes live {pkt}"
        elif platform == "tiktok":
            mode = force_tiktok_mode or account(row["channel"], "tiktok").get("mode", "draft")
            try:
                remote_id, post_url = upload_tiktok(
                    row["channel"], path, row["title"], direct_consent, force_tiktok_mode,
                )
            except RuntimeError as exc:
                # Until TikTok audits the app it only posts to PRIVATE accounts;
                # a public account still gets the video, as a draft.
                if mode != "private" or "unaudited_client_can_only_post_to_private_accounts" not in str(exc):
                    raise
                remote_id, post_url = upload_tiktok(row["channel"], path, row["title"], force_mode="draft")
                mode = "draft"
            message = {
                "direct": "TikTok Direct Post submitted",
                "private": "Posted privately to TikTok; set it to Everyone in TikTok Studio > Posts",
            }.get(mode, "Sent to TikTok drafts")
        else:
            raise RuntimeError("Unsupported social platform.")
        set_social_upload(video_id, platform, "submitted", remote_id, post_url)
        return message
    except Exception as exc:
        set_social_upload(video_id, platform, "failed", error=str(exc))
        raise


def auto_publish_video(video_id: int, channel_id: str,
                       facebook_video: Path | None = None, facebook_title: str | None = None) -> list[str]:
    messages: list[str] = []
    fb = account(channel_id, "facebook")
    tt = account(channel_id, "tiktok")
    from .social_accounts import facebook_elsewhere_reason, facebook_posts_here

    if fb.get("auto_upload") and fb.get("page_id") and fb.get("page_access_token") and not facebook_posts_here():
        messages.append(f"Facebook left to the other machine: {facebook_elsewhere_reason()}")
    elif fb.get("auto_upload") and fb.get("page_id") and fb.get("page_access_token"):
        try:
            messages.append(publish_video(
                video_id, "facebook", channel_id,
                video_override=facebook_video, title_override=facebook_title,
            ))
        except Exception as exc:
            messages.append(f"Facebook failed: {exc}")
    if tt.get("auto_upload") and tt.get("mode") == "zernio" and tt.get("zernio_api_key"):
        # Zernio's audited app posts publicly with no click per video.
        try:
            messages.append(publish_video(video_id, "tiktok", channel_id, force_tiktok_mode="zernio"))
        except Exception as exc:
            messages.append(f"TikTok (Zernio) failed: {exc}")
    elif tt.get("auto_upload") and tt.get("access_token"):
        try:
            # TikTok guidelines require per-post consent before anything goes
            # public, so automatic cross-posting only ever lands as a draft or
            # as a private post the creator makes public themselves.
            auto_mode = "private" if tt.get("mode") == "private" else "draft"
            messages.append(publish_video(video_id, "tiktok", channel_id, force_tiktok_mode=auto_mode))
        except Exception as exc:
            messages.append(f"TikTok failed: {exc}")
    return messages