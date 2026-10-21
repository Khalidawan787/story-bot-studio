"""Put a channel's uploads into a YouTube playlist ("USA Heroes").

The upload login (youtube_upload.py) may only upload, and widening its scope
would log every channel out, so playlists use a second login of their own,
saved next to the upload token as token_<channel>_playlist.json. Connect it
once on the PC:

    .venv\\Scripts\\python.exe -m src.playlists connect world_valor_chronicles

then re-run scripts/setup_github_actions.py so the daily run has it too.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube"]


def playlist_token_path(channel) -> Path:
    token = channel.token_path
    return token.with_name(f"{token.stem}_playlist{token.suffix}")


def playlist_title(channel) -> str:
    country = str(getattr(channel, "focus_country", "") or "").strip()
    return f"{country} Heroes" if country else ""


def _service(channel, interactive: bool = False):
    path = playlist_token_path(channel)
    creds = Credentials.from_authorized_user_file(str(path), SCOPES) if path.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        if not interactive or os.getenv("NO_INTERACTIVE_AUTH", "").strip().lower() in {"1", "true", "yes"}:
            raise RuntimeError(f"playlist login missing; run: python -m src.playlists connect {channel.id}")
        flow = InstalledAppFlow.from_client_secrets_file(str(channel.client_secret_path), SCOPES)
        creds = flow.run_local_server(port=0)
        path.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def _playlist_id(youtube, title: str, description: str) -> str:
    """The channel's playlist with this title, made public if it does not exist yet."""
    request = youtube.playlists().list(part="snippet", mine=True, maxResults=50)
    while request is not None:
        response = request.execute()
        for item in response.get("items", []):
            if item["snippet"]["title"].strip().lower() == title.lower():
                return item["id"]
        request = youtube.playlists().list_next(request, response)
    created = youtube.playlists().insert(
        part="snippet,status",
        body={"snippet": {"title": title, "description": description},
              "status": {"privacyStatus": "public"}},
    ).execute()
    print(f"[playlist] created '{title}'")
    return created["id"]


def add_to_playlist(channel, video_url: str) -> str:
    """Add an uploaded video to the channel's country playlist. Returns a note."""
    title = playlist_title(channel)
    if not title or not video_url or "v=" not in video_url:
        return ""
    video_id = video_url.split("v=", 1)[1].split("&", 1)[0]
    youtube = _service(channel)
    country = channel.focus_country.strip()
    playlist = _playlist_id(
        youtube, title, f"True stories of real people from {country}: heroes, legends and famous lives.",
    )
    youtube.playlistItems().insert(
        part="snippet",
        body={"snippet": {"playlistId": playlist,
                          "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
    ).execute()
    return f"added to playlist '{title}'"


def connect(channel_id: str) -> str:
    from .channels import get_channel

    channel = get_channel(channel_id)
    youtube = _service(channel, interactive=True)
    name = youtube.channels().list(part="snippet", mine=True).execute()["items"][0]["snippet"]["title"]
    return f"Playlist login saved for YouTube channel '{name}'"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "connect":
        print(connect(sys.argv[2]))
    else:
        print("usage: python -m src.playlists connect <channel_id>")
