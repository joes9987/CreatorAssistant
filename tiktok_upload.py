"""
TikTok upload for CreatorAssistant.
Uses TikTok Content Posting API. Requires developer account + app approval for video.publish.
Note: Unverified apps may only post to private accounts.
"""

import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from app_paths import project_root
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from timer_utils import emit_log, format_elapsed

TIKTOK_TOKEN_FILE = "tiktok_token.json"
TIKTOK_UPLOADED_FILE = "tiktok_uploaded.json"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"


def _load_uploaded_paths(tracking_path: Path) -> set[str]:
    """Load set of clip paths already uploaded to TikTok."""
    if not tracking_path.exists():
        return set()
    try:
        data = json.loads(tracking_path.read_text())
        return set(data.get("paths", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_uploaded(tracking_path: Path, path: str) -> None:
    """Add a clip path to the TikTok uploaded tracking file."""
    paths = _load_uploaded_paths(tracking_path)
    paths.add(str(Path(path).resolve()))
    tracking_path.write_text(json.dumps({"paths": sorted(paths)}, indent=2))


def _pkce_pair():
    """Generate code_verifier and code_challenge. TikTok requires HEX encoding for code_challenge (not base64url)."""
    code_verifier = secrets.token_urlsafe(64)[:64]  # 64 chars
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = digest.hex()  # TikTok uses hex, not base64url
    return code_verifier, code_challenge


def get_tiktok_client(config: dict, log: Callable[[str], None] | None = None):
    """Get authenticated TikTok client. Runs OAuth flow if needed."""
    try:
        from tiktok_api_client import TikTok
    except ImportError:
        raise ImportError("Install tiktok-api-client: pip install tiktok-api-client")

    ttk_cfg = config.get("tiktok", {})
    client_key = ttk_cfg.get("client_key", "").strip()
    client_secret = ttk_cfg.get("client_secret", "").strip()
    redirect_uri = ttk_cfg.get("redirect_uri", "http://localhost:8080/callback").strip()

    if not client_key or not client_secret:
        raise ValueError(
            "TikTok credentials missing. Add client_key and client_secret to config.yaml. "
            "Get them from https://developers.tiktok.com/"
        )

    token_path = project_root() / TIKTOK_TOKEN_FILE

    # Try loading existing token
    if token_path.exists():
        try:
            with open(token_path) as f:
                data = json.load(f)
            tik = TikTok(client_key=client_key, client_secret=client_secret, redirect_uri=redirect_uri)
            tik.access_token = data.get("access_token")
            tik.refresh_token = data.get("refresh_token")
            tik.open_id = data.get("open_id")
            tik.token_data = data  # Library expects token_data for create_video, get_creator_info
            return tik
        except Exception:
            pass

    # OAuth flow - use our own PKCE (TikTok requires hex code_challenge; tiktok-api-client uses base64)
    code_verifier, code_challenge = _pkce_pair()
    auth_params = {
        "client_key": client_key,
        "response_type": "code",
        "scope": "user.info.basic,video.publish",
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "state": "",
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTH_URL}?{urlencode(auth_params)}"
    emit_log(log, "  TikTok: open this URL in your browser to authorize:")
    emit_log(log, f"  {auth_url}")
    emit_log(log, f"  Troubleshooting: client_key={client_key[:8]}... redirect_uri={redirect_uri}")
    emit_log(log, "  If client_key error: Add Login Kit product + this exact redirect_uri in Login Kit settings.")

    # Simple local server to catch callback
    from http.server import HTTPServer, BaseHTTPRequestHandler

    code_received = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]
            error_desc = params.get("error_description", [None])[0]
            if error:
                emit_log(log, f"  TikTok callback error: {error} - {error_desc}")
            if code:
                code_received.append(code)
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization complete. You can close this tab.</h1>")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 8080), CallbackHandler)
    emit_log(log, "  TikTok: waiting for authorization (visit the URL above)...")
    # Handle requests until we get one with code (ignore favicon, etc.)
    for _ in range(10):
        server.handle_request()
        if code_received:
            break
    server.server_close()

    code = code_received[0] if code_received else None
    if not code:
        raise RuntimeError(
            "No authorization code received. Check that redirect_uri in Login Kit exactly matches "
            "http://localhost:8080/callback/ (or /callback). If TikTok showed an error, it was printed above."
        )

    # parse_qs already URL-decodes; do NOT unquote again (double-decode corrupts codes with %)
    code = code.strip()
    emit_log(log, f"  TikTok: exchanging auth code (len={len(code)}) with PKCE verifier (len={len(code_verifier)})...")
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=15,
    )
    token_data = resp.json()

    if "error" in token_data:
        err = token_data.get("error_description", token_data.get("error", "Unknown error"))
        emit_log(log, f"  TikTok token exchange response: {token_data}")
        raise RuntimeError(f"TikTok token exchange failed: {err}")
    if "access_token" not in token_data:
        raise RuntimeError(f"TikTok token exchange failed (no access_token): {token_data}")

    # Create TikTok client for create_video (library handles upload)
    tik = TikTok(client_key=client_key, client_secret=client_secret, redirect_uri=redirect_uri)
    with open(token_path, "w") as f:
        json.dump({
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "open_id": token_data["open_id"],
        }, f, indent=2)
    tik.access_token = token_data["access_token"]
    tik.refresh_token = token_data["refresh_token"]
    tik.open_id = token_data["open_id"]
    tik.token_data = token_data
    return tik


def upload_clips(
    clip_paths: list[str],
    config: dict,
    clip_nums: list[int] | None = None,
    champion: str = "",
    log: Callable[[str], None] | None = None,
) -> tuple[list[str], list[int]]:
    """Upload clips to TikTok. Returns (publish_ids, successfully used clip numbers). Pass clip_nums to share numbering with YouTube. champion from game_events for title_template {champion}."""
    ttk_cfg = config.get("tiktok", {})
    if not ttk_cfg.get("enabled", False):
        return [], []

    title_template = ttk_cfg.get("title_template", "{creator} | League clip {num}{champion_suffix}")
    creator = ttk_cfg.get("creator_name", "joes9987")
    champ_suffix = f" with {champion}" if champion else ""

    counter_path = project_root() / "clip_counter.txt"
    if clip_nums is not None:
        clip_numbers = clip_nums
    else:
        counter_start = config.get("youtube", {}).get("clip_counter_start", 1)
        try:
            start = int(counter_path.read_text().strip()) if counter_path.exists() else counter_start
        except (ValueError, OSError):
            start = counter_start
        clip_numbers = [start + i for i in range(len(clip_paths))]

    tracking_path = project_root() / TIKTOK_UPLOADED_FILE
    uploaded_set = _load_uploaded_paths(tracking_path)

    # Filter out already-uploaded clips
    to_upload: list[tuple[str, int]] = []
    for i, path in enumerate(clip_paths):
        resolved = str(Path(path).resolve())
        clip_num = clip_numbers[i] if i < len(clip_numbers) else clip_numbers[-1] + i
        if resolved in uploaded_set:
            emit_log(log, f"  Skipping (already uploaded to TikTok): {Path(path).name}")
            continue
        to_upload.append((path, clip_num))

    if not to_upload:
        emit_log(log, "  All clips already uploaded to TikTok")
        return [], []

    tik = get_tiktok_client(config, log=log)
    privacy = ttk_cfg.get("privacy", "PUBLIC_TO_EVERYONE")

    uploaded = []
    success_clip_nums = []
    for i, (path, clip_num) in enumerate(to_upload):
        title = title_template.format(
            num=clip_num, n=i + 1, total=len(to_upload),
            champion=champion, ChampionName=champion,
            champion_suffix=champ_suffix,
            creator=creator, username=creator,
        )
        emit_log(log, f"  Uploading to TikTok {i+1}/{len(to_upload)} (#{clip_num}): {Path(path).name}")
        clip_start = time.perf_counter()
        try:
            resp = tik.create_video(
                title=title[:150],
                source="FILE_UPLOAD",
                upload_type="POST_VIDEO_FILE",
                privacy_level=privacy,
                video_path=path,
            )
            if resp and resp.get("initial_response", {}).get("data", {}).get("publish_id"):
                uploaded.append(resp["initial_response"]["data"]["publish_id"])
                success_clip_nums.append(clip_num)
                _mark_uploaded(tracking_path, path)
                dt = time.perf_counter() - clip_start
                emit_log(log, f"    -> Posted to TikTok  ({format_elapsed(dt)})")
            else:
                err = resp.get("error", resp.get("initial_response", {}).get("error", {})) if isinstance(resp, dict) else {}
                emit_log(log, f"    -> Failed: {err.get('message', resp)}")
        except Exception as e:
            emit_log(log, f"    -> Error: {e}")
    if clip_nums is None and success_clip_nums:
        with open(counter_path, "w") as f:
            f.write(str(max(success_clip_nums) + 1))
    return uploaded, success_clip_nums
