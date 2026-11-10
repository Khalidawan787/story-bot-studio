from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .config import settings


SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",  # read the connected channel's name to verify it
]
# Asked for at a NEW login only: changing a video's status (holding it private
# until its thumbnail is set, set_release) needs the full YouTube scope. An
# existing login keeps the scopes it was granted, so nothing breaks; a channel
# gets the new right when it is reconnected on the dashboard.
MANAGE_SCOPE = "https://www.googleapis.com/auth/youtube"
LOGIN_SCOPES = [*SCOPES, MANAGE_SCOPE]


# YouTube refuses a description over 5000 bytes or with < or > in it
# ("invalidDescription"). A long video's script summary plus its clip credits
# and source line ran past the limit, and the video waited forever.
_DESCRIPTION_BYTES = 4900


def youtube_description(text: str) -> str:
    text = text.replace("<", "(").replace(">", ")")
    if len(text.encode("utf-8")) <= _DESCRIPTION_BYTES:
        return text
    # Drop whole lines from the middle (the script summary), keeping the
    # opening and the closing lines, which carry the hashtags, credits and source.
    lines = text.split("\n")
    head, tail = lines[:4], lines[4:]
    while tail and len("\n".join(head + ["..."] + tail).encode("utf-8")) > _DESCRIPTION_BYTES:
        tail.pop(0)
    trimmed = "\n".join(head + ["..."] + tail)
    return trimmed.encode("utf-8")[:_DESCRIPTION_BYTES].decode("utf-8", "ignore")


class HoldNotAllowed(RuntimeError):
    """The login cannot change a video's status (no full YouTube scope)."""


class ThumbnailUploadError(RuntimeError):
    def __init__(self, video_url: str, message: str) -> None:
        super().__init__(message)
        self.video_url = video_url


def _credentials(client_secret_file: Path | None = None, token_file: Path | None = None) -> Credentials:
    client_secret_file = client_secret_file or settings.youtube_client_secret_file
    token_file = token_file or settings.youtube_token_file
    creds = None
    if token_file.exists():
        # The scopes the login was granted (from the token file), so a token
        # made before LOGIN_SCOPES grew still refreshes.
        creds = Credentials.from_authorized_user_file(str(token_file))
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not client_secret_file.exists():
            raise FileNotFoundError(f"Missing YouTube client secret: {client_secret_file}")
        # Opening a browser is only ever right on a real desktop. On a server or
        # a CI runner there is nobody to click, so the flow would block until
        # the job is killed; say what is wrong instead.
        if os.getenv("NO_INTERACTIVE_AUTH", "").strip().lower() in {"1", "true", "yes"}:
            raise RuntimeError(
                f"No usable YouTube login in {token_file.name} and this machine "
                "cannot open a browser. Connect the channel on your PC first, "
                "then re-run scripts/setup_github_actions.py."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), LOGIN_SCOPES)
        creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _youtube(client_secret_file: Path | None = None, token_file: Path | None = None):
    return build("youtube", "v3", credentials=_credentials(client_secret_file, token_file))


def set_thumbnail(video_id: str, thumbnail_path: Path, attempts: int = 4,
                  client_secret_file: Path | None = None, token_file: Path | None = None) -> None:
    youtube = _youtube(client_secret_file, token_file)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
            ).execute()
            return
        except Exception as exc:
            last_error = exc
            # Daily quota errors cannot recover seconds later. The queue retries later.
            if isinstance(exc, HttpError):
                reason = str(exc).lower()
                if (
                    "quotaexceeded" in reason
                    or ("exceeded your" in reason and "quota" in reason)
                    or "doesn't have permissions to upload and set custom video thumbnails" in reason
                ):
                    break
            if attempt < attempts:
                time.sleep(8 * attempt)
    raise RuntimeError(f"Thumbnail upload failed after {attempts} attempts: {last_error}")


def _is_tall(path: Path) -> bool:
    try:
        import cv2
        import numpy as np

        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_4)
        return image is not None and image.shape[0] > image.shape[1]
    except Exception:
        return False


def wait_until_processed(video_id: str, client_secret_file: Path | None = None,
                         token_file: Path | None = None, timeout: int = 420) -> bool:
    """Wait (up to `timeout` s) until YouTube has finished processing the upload.

    A Short's thumbnail set straight after the upload is kept as the video's
    thumbnail, but the Shorts tab then shows a frame YouTube picked itself
    (no "sardefault" picture: Katherine Johnson, 9 Nov). Set after processing
    (as the backfill did for Jordan) the Shorts tab shows the card.
    """
    youtube = _youtube(client_secret_file, token_file)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            items = youtube.videos().list(part="status,processingDetails", id=video_id).execute().get("items") or []
        except Exception:
            items = []
        if items:
            status = (items[0].get("status") or {}).get("uploadStatus", "")
            processing = (items[0].get("processingDetails") or {}).get("processingStatus", "")
            if status == "processed" or processing == "succeeded":
                return True
            if status in {"failed", "rejected", "deleted"} or processing in {"failed", "terminated"}:
                return False
        time.sleep(20)
    return False


def upload_video(video_path: Path, thumbnail_path: Path, metadata: dict[str, object],
                 channel=None, publish_at: datetime | None = None,
                 progress_callback: Callable[[int], None] | None = None,
                 upload_thumbnail: bool = True) -> str:
    # Resolve per-channel credentials + settings; default = original kids behavior.
    if channel is not None:
        client_secret_file = channel.client_secret_path
        token_file = channel.token_path
        category_id = channel.category_id
        made_for_kids = channel.made_for_kids
        privacy = channel.privacy
    else:
        client_secret_file = None
        token_file = None
        category_id = "27"
        made_for_kids = True
        privacy = settings.youtube_privacy_status

    status_body: dict[str, object] = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": made_for_kids,
    }
    # Scheduled publish: YouTube requires the video be private until publishAt,
    # then it flips it public automatically at that timestamp (RFC3339 UTC).
    if publish_at is not None:
        status_body["privacyStatus"] = "private"
        status_body["publishAt"] = publish_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    # The AI news anchor (anchor_lipsync.py): YouTube's "altered or synthetic content".
    if (Path(video_path).parent / "synthetic_media.txt").exists():
        status_body["containsSyntheticMedia"] = True

    youtube = _youtube(client_secret_file, token_file)

    def insert():
        return youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": metadata["title"],
                    "description": youtube_description(str(metadata["description"])),
                    "tags": metadata["tags"],
                    "categoryId": category_id,
                    # Tells YouTube which language the title and the voice are in.
                    "defaultLanguage": str(metadata.get("language") or "en"),
                    "defaultAudioLanguage": str(metadata.get("language") or "en"),
                },
                "status": status_body,
            },
            media_body=MediaFileUpload(str(video_path), chunksize=8 * 1024 * 1024, resumable=True),
        )

    request = insert()
    response = None
    if progress_callback:
        progress_callback(0)
    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as exc:
            # The synthetic-media flag must never cost the upload: if YouTube
            # refuses it (the token's scope, say), upload again without it and
            # the owner sets the label in Studio.
            if not status_body.pop("containsSyntheticMedia", None):
                raise
            print(f"[youtube] upload refused with the synthetic-media label ({exc}); "
                  "uploading without it, set 'Altered content' in Studio")
            request, status = insert(), None
        if status is not None and progress_callback:
            progress_callback(max(1, min(99, int(status.progress() * 100))))
    if progress_callback:
        progress_callback(100)
    video_id = response["id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    # The owner's rule (2026-11-04): no video goes public without its
    # thumbnail. If it cannot be set now, the video is held private with no
    # publish time, and pending_uploads releases it once the thumbnail is on.
    reason = ""
    if upload_thumbnail:
        if _is_tall(Path(thumbnail_path)):
            # A Short: thumbnail after processing, so the Shorts tab shows the card.
            done = wait_until_processed(video_id, client_secret_file, token_file)
            print(f"[youtube] {video_id} {'processed' if done else 'still processing'}; setting the Short's thumbnail")
        try:
            set_thumbnail(video_id, thumbnail_path, client_secret_file=client_secret_file, token_file=token_file)
        except Exception as exc:
            reason = str(exc)
    else:
        reason = "custom thumbnails are not allowed on this channel yet (verify it at youtube.com/verify)"
    if reason:
        try:
            set_release(video_id, None, made_for_kids, client_secret_file=client_secret_file,
                        token_file=token_file)
            reason += " | video held private until its thumbnail is set"
        except Exception as hold_exc:
            reason += f" | could not hold the video private: {hold_exc}"
        raise ThumbnailUploadError(video_url, reason)
    return video_url


def set_release(video_id: str, publish_at: datetime | None, made_for_kids: bool,
                client_secret_file=None, token_file=None) -> None:
    """Hold a video private (publish_at None) or schedule it to go public at publish_at."""
    manage = {MANAGE_SCOPE, "https://www.googleapis.com/auth/youtube.force-ssl"}
    if not set(_credentials(client_secret_file, token_file).scopes or ()) & manage:
        # The playlist login (playlists.py) has the full scope where it exists.
        token = Path(token_file or settings.youtube_token_file)
        extra = token.with_name(f"{token.stem}_playlist{token.suffix}")
        if extra.is_file() and set(_credentials(client_secret_file, extra).scopes or ()) & manage:
            token_file = extra
        else:
            raise HoldNotAllowed("this channel's login may not change a video's status; reconnect "
                                 "it on the dashboard (Reconnect) and press Send to GitHub")
    status: dict[str, object] = {"privacyStatus": "private", "selfDeclaredMadeForKids": bool(made_for_kids)}
    if publish_at is not None:
        status["publishAt"] = publish_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    youtube = _youtube(client_secret_file, token_file)
    youtube.videos().update(part="status", body={"id": video_id, "status": status}).execute()


def connect_and_verify(client_secret_file, token_file) -> str:
    """Authorize one channel and return the connected YouTube channel's name.

    Powers the dashboard 'Connect & verify' button so you can confirm each
    channel is linked to the CORRECT YouTube channel before any upload.
    """
    service = _youtube(client_secret_file, token_file)
    resp = service.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        return "Connected, but no YouTube channel found on this account."
    return items[0]["snippet"]["title"]
