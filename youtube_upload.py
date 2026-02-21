"""
YouTube Shorts upload for CreatorAssistant.
Uses YouTube Data API v3 with OAuth 2.0. Vertical videos are auto-detected as Shorts.
"""

import os
import random
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_FILE = "youtube_token.json"
CLIP_COUNTER_FILE = "clip_counter.txt"


def get_youtube_service(secrets_path: str | None = None, token_path: str = TOKEN_FILE, script_dir: str | Path | None = None):
    """Authenticate and return a YouTube API service object."""
    base = Path(script_dir or __file__).parent.resolve()
    secrets_path = secrets_path or CLIENT_SECRETS_FILE
    if not Path(secrets_path).is_absolute():
        secrets_path = str(base / Path(secrets_path).name)
    token_path = str(base / Path(token_path).name) if not Path(token_path).is_absolute() else token_path

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(secrets_path):
                raise FileNotFoundError(
                    f"Missing {secrets_path}. Get OAuth credentials from "
                    "https://console.cloud.google.com/ → APIs & Services → Credentials → Create OAuth 2.0 Client ID "
                    "(Desktop app). Download JSON and save as client_secrets.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload_video(
    file_path: str,
    title: str = "League Highlight",
    description: str = "",
    tags: list | None = None,
    privacy: str = "private",
    category_id: str = "20",  # Gaming
    youtube=None,
) -> str | None:
    """
    Upload a video to YouTube. Returns the video ID on success, None on failure.
    Privacy: 'public', 'private', or 'unlisted'
    """
    if youtube is None:
        youtube = get_youtube_service()
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000] or "League of Legends highlight clip",
            "tags": tags or ["League of Legends", "Gaming", "Shorts"],
            "categoryId": category_id,
        },
        "status": {"privacyStatus": privacy},
    }

    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    retries = 0
    max_retries = 5
    while retries < max_retries:
        try:
            response = request.execute()
            return response.get("id")
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504):
                retries += 1
                time.sleep(random.uniform(1, 2**retries))
            else:
                raise
    return None


def _get_next_clip_num(counter_path: Path, start: int) -> int:
    """Read persistent clip counter, return next value and increment file."""
    if counter_path.exists():
        try:
            with open(counter_path) as f:
                return int(f.read().strip())
        except (ValueError, OSError):
            pass
    return start


def _save_clip_counter(counter_path: Path, value: int) -> None:
    with open(counter_path, "w") as f:
        f.write(str(value))


def upload_clips(clip_paths: list[str], config: dict, clip_nums: list[int] | None = None) -> list[str]:
    """
    Upload multiple clips. Returns list of uploaded video IDs.
    Uses persistent {num} for titles. Pass clip_nums to share numbering with TikTok.
    """
    yt_cfg = config.get("youtube", {})
    if not yt_cfg.get("enabled", False):
        return []

    base = Path(__file__).parent
    counter_path = base / CLIP_COUNTER_FILE
    if clip_nums is not None:
        clip_numbers = clip_nums
    else:
        counter_start = yt_cfg.get("clip_counter_start", 1)
        start = _get_next_clip_num(counter_path, counter_start)
        clip_numbers = [start + i for i in range(len(clip_paths))]

    secrets_file = yt_cfg.get("client_secrets_file", "").strip() or CLIENT_SECRETS_FILE
    title_template = yt_cfg.get("title_template", "League Highlight {num}")
    description = yt_cfg.get("description", "")
    tags = yt_cfg.get("tags", ["League of Legends", "Gaming", "Shorts"])
    privacy = yt_cfg.get("privacy", "private")

    youtube = get_youtube_service(secrets_path=secrets_file)
    uploaded = []
    for i, path in enumerate(clip_paths):
        n = i + 1
        clip_num = clip_numbers[i] if i < len(clip_numbers) else clip_numbers[-1] + i
        title = title_template.format(num=clip_num, n=n, total=len(clip_paths))
        print(f"  Uploading clip {n}/{len(clip_paths)} (#{clip_num}): {Path(path).name}")
        try:
            vid = upload_video(path, title=title, description=description, tags=tags, privacy=privacy, youtube=youtube)
            if vid:
                uploaded.append(vid)
                if clip_nums is None:
                    _save_clip_counter(counter_path, clip_num + 1)
                print(f"    -> https://youtube.com/shorts/{vid}")
            else:
                print(f"    -> Failed")
        except Exception as e:
            print(f"    -> Error: {e}")
    return uploaded
