from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

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


class ThumbnailUploadError(RuntimeError):
    def __init__(self, video_url: str, message: str) -> None:
        super().__init__(message)
        self.video_url = video_url


def _credentials(client_secret_file: Path | None = None, token_file: Path | None = None) -> Credentials:
    client_secret_file = client_secret_file or settings.youtube_client_secret_file
    token_file = token_file or settings.youtube_token_file
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not client_secret_file.exists():
            raise FileNotFoundError(f"Missing YouTube client secret: {client_secret_file}")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), SCOPES)
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
                if "quotaexceeded" in reason or ("exceeded your" in reason and "quota" in reason):
                    break
            if attempt < attempts:
                time.sleep(8 * attempt)
    raise RuntimeError(f"Thumbnail upload failed after {attempts} attempts: {last_error}")


def upload_video(video_path: Path, thumbnail_path: Path, metadata: dict[str, object],
                 channel=None, publish_at: datetime | None = None) -> str:
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

    youtube = _youtube(client_secret_file, token_file)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata["tags"],
                "categoryId": category_id,
            },
            "status": status_body,
        },
        media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True),
    )
    response = request.execute()
    video_id = response["id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    try:
        set_thumbnail(video_id, thumbnail_path, client_secret_file=client_secret_file, token_file=token_file)
    except Exception as exc:
        raise ThumbnailUploadError(video_url, str(exc)) from exc
    return video_url


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
