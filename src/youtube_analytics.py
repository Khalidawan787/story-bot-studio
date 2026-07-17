from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .db import connect

SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]

class AnalyticsPermissionRequired(RuntimeError):
    pass

def analytics_token_path(channel) -> Path:
    token = channel.token_path
    return token.with_name(f"{token.stem}_analytics{token.suffix}")

def is_connected(channel) -> bool:
    return analytics_token_path(channel).exists()

def _credentials(channel, interactive: bool = False) -> Credentials:
    token = analytics_token_path(channel)
    creds = None
    if token.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except Exception:
            creds = None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            creds = None
    if creds and creds.valid and creds.has_scopes(SCOPES):
        return creds
    if not interactive:
        raise AnalyticsPermissionRequired("Connect Analytics once for this channel.")
    if not channel.client_secret_path.exists():
        raise FileNotFoundError(f"Missing client secret: {channel.client_secret_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(channel.client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds

def connect_analytics(channel) -> str:
    creds = _credentials(channel, interactive=True)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    result = youtube.channels().list(part="snippet", mine=True).execute()
    items = result.get("items", [])
    return items[0]["snippet"]["title"] if items else "Connected channel"

def _rows(response: dict) -> list[dict[str, object]]:
    headers = [header["name"] for header in response.get("columnHeaders", [])]
    return [dict(zip(headers, row)) for row in response.get("rows", [])]

def _query(service, start_date: str, end_date: str, metrics: str, **kwargs) -> list[dict[str, object]]:
    response = service.reports().query(
        ids="channel==MINE", startDate=start_date, endDate=end_date,
        metrics=metrics, **kwargs,
    ).execute()
    return _rows(response)

def _video_titles(creds: Credentials, video_ids: list[str]) -> dict[str, str]:
    if not video_ids:
        return {}
    try:
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        response = youtube.videos().list(part="snippet", id=",".join(video_ids[:50])).execute()
        return {item["id"]: item["snippet"]["title"] for item in response.get("items", [])}
    except Exception:
        return {}

def refresh(channel, days: int = 28) -> dict[str, object]:
    creds = _credentials(channel, interactive=False)
    service = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    end = date.today() - timedelta(days=1)
    days = max(1, min(90, int(days)))
    start = end - timedelta(days=days - 1)
    start_text, end_text = start.isoformat(), end.isoformat()
    metrics = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained"
    try:
        summary_rows = _query(service, start_text, end_text, metrics)
    except HttpError as exc:
        message = str(exc)
        if "accessNotConfigured" in message or "API has not been used" in message or "it is disabled" in message:
            raise RuntimeError(
                "YouTube Analytics API is disabled in Google Cloud. Enable it at "
                "https://console.cloud.google.com/apis/library/youtubeanalytics.googleapis.com "
                "then wait a few minutes and click Refresh Analytics."
            ) from exc
        raise
    summary = summary_rows[0] if summary_rows else {}
    try:
        content_types = _query(
            service, start_text, end_text,
            "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage",
            dimensions="creatorContentType", sort="-views",
        )
    except Exception:
        content_types = []
    top_videos = _query(
        service, start_text, end_text,
        "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
        dimensions="video", sort="-views", maxResults=10,
    )
    titles = _video_titles(creds, [str(row.get("video", "")) for row in top_videos])
    for row in top_videos:
        video_id = str(row.get("video", ""))
        row["title"] = titles.get(video_id, video_id)
    payload: dict[str, object] = {
        "channel": channel.id, "days": days, "start_date": start_text,
        "end_date": end_text, "summary": summary,
        "content_types": content_types, "top_videos": top_videos,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO analytics_cache (channel, payload, fetched_at) VALUES (?, ?, ?)",
            (channel.id, json.dumps(payload), payload["fetched_at"]),
        )
        conn.commit()
    finally:
        conn.close()
    return payload

def snapshot(channel, max_age_hours: float = 6.0) -> dict[str, object]:
    connected = is_connected(channel)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT payload, fetched_at FROM analytics_cache WHERE channel = ?", (channel.id,)
        ).fetchone()
    finally:
        conn.close()
    payload = json.loads(row[0]) if row else None
    stale = True
    if row:
        fetched = datetime.fromisoformat(row[1])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        stale = datetime.now(timezone.utc) - fetched > timedelta(hours=max_age_hours)
    return {"connected": connected, "data": payload, "stale": stale}
