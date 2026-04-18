"""
YouTube Shorts upload for CreatorAssistant.
Uses YouTube Data API v3 with OAuth 2.0. Vertical videos are auto-detected as Shorts.
"""

import contextlib
import os
import random
import time
from collections.abc import Callable
from pathlib import Path

from app_paths import project_root

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from timer_utils import emit_log, format_elapsed

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CLIENT_SECRETS_FILE = "client_secrets.json"
TOKEN_FILE = "youtube_token.json"
CLIP_COUNTER_FILE = "clip_counter.txt"
UPLOADED_TRACKING_FILE = "youtube_uploaded.json"


class _StdoutLinesToLog:
    """File-like object: forward each line to emit_log (for OAuth library prints)."""

    def __init__(self, log: Callable[[str], None] | None) -> None:
        self._log = log
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip() and self._log:
                emit_log(self._log, f"  {line.strip()}")
        return len(s)

    def flush(self) -> None:
        if self._buf.strip() and self._log:
            emit_log(self._log, f"  {self._buf.strip()}")
            self._buf = ""


def get_youtube_service(
    secrets_path: str | None = None,
    token_path: str = TOKEN_FILE,
    script_dir: str | Path | None = None,
    log: Callable[[str], None] | None = None,
):
    """Authenticate and return a YouTube API service object."""
    base = Path(script_dir).resolve() if script_dir else project_root()
    secrets_path = secrets_path or CLIENT_SECRETS_FILE
    if not Path(secrets_path).is_absolute():
        secrets_path = str(base / Path(secrets_path).name)
    token_path = str(base / Path(token_path).name) if not Path(token_path).is_absolute() else token_path

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            emit_log(log, "  YouTube: refreshing access token...")
            creds.refresh(Request())
        else:
            if not os.path.exists(secrets_path):
                raise FileNotFoundError(
                    f"Missing {secrets_path}. Get OAuth credentials from "
                    "https://console.cloud.google.com/ → APIs & Services → Credentials → Create OAuth 2.0 Client ID "
                    "(Desktop app). Download JSON and save as client_secrets.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(secrets_path, SCOPES)
            emit_log(log, "  YouTube: starting OAuth (browser should open; if not, use the URL below).")
            out = _StdoutLinesToLog(log)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                creds = flow.run_local_server(port=0)
            out.flush()
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


def _load_uploaded_paths(tracking_path: Path) -> set[str]:
    """Load set of clip paths already uploaded to YouTube."""
    if not tracking_path.exists():
        return set()
    try:
        import json
        data = json.loads(tracking_path.read_text())
        return set(data.get("paths", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_uploaded(tracking_path: Path, path: str) -> None:
    """Add a clip path to the uploaded tracking file."""
    import json
    paths = _load_uploaded_paths(tracking_path)
    paths.add(str(Path(path).resolve()))
    tracking_path.write_text(json.dumps({"paths": sorted(paths)}, indent=2))


def upload_clips(
    clip_paths: list[str],
    config: dict,
    clip_nums: list[int] | None = None,
    champion: str = "",
    log: Callable[[str], None] | None = None,
) -> tuple[list[str], list[int]]:
    """
    Upload multiple clips. Returns (uploaded video IDs, successfully used clip numbers).
    Only successful uploads count toward clip numbering. Pass clip_nums to share numbering with TikTok.
    Skips clips that were already uploaded (tracked in youtube_uploaded.json).
    champion: from game_events.json local_player_champion; used in title_template as {champion}.
    """
    yt_cfg = config.get("youtube", {})
    if not yt_cfg.get("enabled", False):
        return [], []

    root = project_root()
    counter_path = root / CLIP_COUNTER_FILE
    tracking_path = root / UPLOADED_TRACKING_FILE
    uploaded_set = _load_uploaded_paths(tracking_path)

    # Filter out already-uploaded clips
    to_upload: list[tuple[str, int]] = []
    for i, path in enumerate(clip_paths):
        resolved = str(Path(path).resolve())
        if resolved in uploaded_set:
            emit_log(log, f"  Skipping (already uploaded to YouTube): {Path(path).name}")
            continue
        clip_num = (clip_nums[i] if clip_nums and i < len(clip_nums) else None) or (
            _get_next_clip_num(counter_path, yt_cfg.get("clip_counter_start", 1)) + len(to_upload)
        )
        to_upload.append((path, clip_num))

    if not to_upload:
        emit_log(log, "  All clips already uploaded to YouTube")
        return [], []

    secrets_file = yt_cfg.get("client_secrets_file", "").strip() or CLIENT_SECRETS_FILE
    title_template = yt_cfg.get("title_template", "{creator} | League clip {num}{champion_suffix}")
    creator = yt_cfg.get("creator_name", "joes9987")
    champ_suffix = f" with {champion}" if champion else ""
    description = yt_cfg.get("description", "")
    tags = yt_cfg.get("tags", ["League of Legends", "Gaming", "Shorts"])
    privacy = yt_cfg.get("privacy", "private")

    youtube = get_youtube_service(secrets_path=secrets_file, log=log)
    uploaded = []
    success_clip_nums = []
    for i, (path, clip_num) in enumerate(to_upload):
        n = i + 1
        total = len(to_upload)
        title = title_template.format(
            num=clip_num, n=n, total=total,
            champion=champion, ChampionName=champion,
            champion_suffix=champ_suffix,
            creator=creator, username=creator,
        )
        emit_log(log, f"  Uploading clip {n}/{total} (#{clip_num}): {Path(path).name}")
        clip_start = time.perf_counter()
        try:
            vid = upload_video(path, title=title, description=description, tags=tags, privacy=privacy, youtube=youtube)
            if vid:
                uploaded.append(vid)
                success_clip_nums.append(clip_num)
                _mark_uploaded(tracking_path, path)
                if clip_nums is None:
                    _save_clip_counter(counter_path, clip_num + 1)
                dt = time.perf_counter() - clip_start
                emit_log(log, f"    -> https://youtube.com/shorts/{vid}  ({format_elapsed(dt)})")
            else:
                emit_log(log, "    -> Failed")
        except Exception as e:
            emit_log(log, f"    -> Error: {e}")
    return uploaded, success_clip_nums
