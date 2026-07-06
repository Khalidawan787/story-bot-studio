"""Optional Google Drive storage so the local app stays small.

Final videos are uploaded to a Drive folder and can then be deleted locally.
Uses the drive.file scope (the app only sees files it created). Token is kept
separate from YouTube in credentials/drive_token.json.
"""
from __future__ import annotations

from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import settings

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_FILE = settings.root / "credentials" / "drive_token.json"


def is_connected() -> bool:
    return TOKEN_FILE.exists()


def _service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        secret = settings.youtube_client_secret_file
        if not secret.exists():
            raise FileNotFoundError(f"Missing client secret: {secret}")
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
        creds = flow.run_local_server(port=0)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)


def connect() -> str:
    """Authorize Drive (opens a browser once). Returns the account email if available."""
    service = _service()
    about = service.about().get(fields="user(emailAddress)").execute()
    return about.get("user", {}).get("emailAddress", "connected")


def _folder_id(service, name: str) -> str:
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         "and trashed=false")
    found = service.files().list(q=q, fields="files(id)", spaces="drive").execute().get("files", [])
    if found:
        return found[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    return service.files().create(body=meta, fields="id").execute()["id"]


def upload_file(local_path: Path, folder: str | None = None) -> str:
    """Upload one file to the Drive folder and return its shareable webViewLink."""
    service = _service()
    folder_id = _folder_id(service, folder or settings.drive_folder)
    meta = {"name": Path(local_path).name, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), resumable=True)
    f = service.files().create(body=meta, media_body=media,
                               fields="id, webViewLink").execute()
    return f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"
