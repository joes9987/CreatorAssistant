"""
Instagram Reels upload for CreatorAssistant.
Uses Instagram Graph API with resumable upload for local files.
Requires a Meta Developer app with instagram_content_publish permission.
Uses Instagram Login OAuth flow (like TikTok) — no manual token copying needed.
"""

import json
import os
import string
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app_paths import project_root
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from timer_utils import emit_log, format_elapsed


class _SafeFormatter(string.Formatter):
    """Formatter that replaces missing keys with empty string instead of raising KeyError."""
    def get_value(self, key, args, kwargs):
        if isinstance(key, str):
            return kwargs.get(key, "")
        return super().get_value(key, args, kwargs)

GRAPH_API_BASE = "https://graph.instagram.com/v22.0"
# Connect + read timeouts for Graph API (Instagram can be slow fetching video_url)
_GRAPH_RQ_TIMEOUT = (60.0, 300.0)
TOKEN_FILE = "instagram_token.json"
IG_UPLOADED_FILE = "instagram_uploaded.json"
IG_AUTH_URL = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"


def _load_uploaded_paths(tracking_path: Path) -> set[str]:
    """Load set of clip paths already uploaded to Instagram."""
    if not tracking_path.exists():
        return set()
    try:
        data = json.loads(tracking_path.read_text())
        return set(data.get("paths", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _mark_uploaded(tracking_path: Path, path: str) -> None:
    """Add a clip path to the Instagram uploaded tracking file."""
    paths = _load_uploaded_paths(tracking_path)
    paths.add(str(Path(path).resolve()))
    tracking_path.write_text(json.dumps({"paths": sorted(paths)}, indent=2))


def _raise_with_body(resp: requests.Response) -> None:
    """Like raise_for_status but includes the API error body in the message."""
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        try:
            body = resp.json()
        except Exception:
            body = resp.text[:500]
        raise requests.exceptions.HTTPError(
            f"{resp.status_code} for {resp.url.split('?')[0]}: {body}",
            response=resp,
        )


def _load_token(token_path: str) -> dict | None:
    """Load saved long-lived access token."""
    if os.path.exists(token_path):
        try:
            with open(token_path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_token(token_path: str, token_data: dict) -> None:
    """Persist access token to disk."""
    with open(token_path, "w") as f:
        json.dump(token_data, f, indent=2)


# ── OAuth flow (Instagram Login) ─────────────────────────────────────────

def _run_instagram_oauth(
    app_id: str, app_secret: str, redirect_uri: str, log: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Run Instagram Login OAuth flow. Opens browser URL, catches callback.
    Returns (short_lived_access_token, ig_user_id)."""

    auth_params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "instagram_basic,instagram_content_publish",
    }
    auth_url = f"{IG_AUTH_URL}?{urlencode(auth_params)}"
    emit_log(log, "  Instagram: open this URL in your browser to authorize:")
    emit_log(log, f"  {auth_url}")
    emit_log(log, f"  Redirect URI: {redirect_uri}")
    emit_log(log, "  If error: In Meta App Dashboard > Instagram > Instagram Login, add this exact redirect URI.")

    # Parse port from redirect_uri
    parsed = urlparse(redirect_uri)
    port = parsed.port or 8081

    code_received = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed_path = urlparse(self.path)
            params = parse_qs(parsed_path.query)
            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]
            error_desc = params.get("error_description", [None])[0]
            if error:
                emit_log(log, f"  Instagram callback error: {error} - {error_desc}")
            if code:
                code_received.append(code)
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization complete. You can close this tab.</h1>")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), CallbackHandler)
    emit_log(log, "  Instagram: waiting for authorization (visit the URL above)...")
    for _ in range(10):
        server.handle_request()
        if code_received:
            break
    server.server_close()

    code = code_received[0] if code_received else None
    if not code:
        raise RuntimeError(
            "No authorization code received from Instagram. "
            "Check that the redirect URI in Meta App Dashboard matches exactly."
        )

    # Instagram auth codes may have trailing #_ — strip it
    code = code.rstrip("#_").strip()

    # Exchange code for short-lived token
    resp = requests.post(
        IG_TOKEN_URL,
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=(60.0, 120.0),
    )
    _raise_with_body(resp)
    data = resp.json()
    short_token = data["access_token"]
    user_id = str(data.get("user_id", ""))
    return short_token, user_id


def _exchange_for_long_lived_token(short_token: str, app_secret: str) -> tuple[str, int]:
    """Exchange a short-lived IGAA token for a long-lived one (~60 days)."""
    resp = requests.get(
        f"{GRAPH_API_BASE}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
        timeout=_GRAPH_RQ_TIMEOUT,
    )
    _raise_with_body(resp)
    data = resp.json()
    return data["access_token"], data.get("expires_in", 5184000)


def _refresh_long_lived_token(token: str) -> tuple[str, int]:
    """Refresh a long-lived IGAA token before it expires."""
    resp = requests.get(
        f"{GRAPH_API_BASE}/refresh_access_token",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": token,
        },
        timeout=_GRAPH_RQ_TIMEOUT,
    )
    _raise_with_body(resp)
    data = resp.json()
    return data["access_token"], data.get("expires_in", 5184000)


# ── Token management ─────────────────────────────────────────────────────

def get_access_token(config: dict, log: Callable[[str], None] | None = None) -> str:
    """
    Get a valid Instagram access token.
    Priority: saved long-lived token → refresh → OAuth flow → config fallback.
    """
    ig_cfg = config.get("instagram", {})
    token_path = str(project_root() / TOKEN_FILE)

    app_id = ig_cfg.get("app_id", "").strip()
    app_secret = ig_cfg.get("app_secret", "").strip()
    redirect_uri = ig_cfg.get("redirect_uri", "http://localhost:8081/callback").strip()

    # 1. Try loading saved long-lived token
    saved = _load_token(token_path)
    if saved:
        token = saved.get("access_token")
        expires_at = saved.get("expires_at", 0)

        # Refresh if expiring within 7 days
        if time.time() > expires_at - (7 * 86400):
            try:
                new_token, expires_in = _refresh_long_lived_token(token)
                _save_token(token_path, {
                    "access_token": new_token,
                    "ig_user_id": saved.get("ig_user_id", ""),
                    "expires_at": time.time() + expires_in,
                })
                emit_log(log, "  Refreshed long-lived Instagram token")
                return new_token
            except Exception:
                emit_log(log, "  Saved Instagram token expired, re-authenticating...")
        else:
            return token

    # 2. Use token from config.yaml (app_token or access_token from Meta dashboard)
    config_token = (ig_cfg.get("app_token", "") or ig_cfg.get("access_token", "")).strip()
    # YAML / copy-paste sometimes wraps quotes or odd whitespace
    config_token = config_token.strip().strip('"').strip("'")
    if config_token and app_secret:
        # Short-lived → long-lived (ig_exchange_token). Fails with OAuth 190 if the token is
        # already long-lived or not an Instagram Login user token — then try refresh below.
        try:
            long_token, expires_in = _exchange_for_long_lived_token(config_token, app_secret)
            _save_token(token_path, {
                "access_token": long_token,
                "ig_user_id": ig_cfg.get("ig_user_id", ""),
                "expires_at": time.time() + expires_in,
            })
            emit_log(log, "  Exchanged short-lived token for long-lived Instagram token (valid ~60 days)")
            return long_token
        except Exception as exchange_err:
            try:
                new_token, expires_in = _refresh_long_lived_token(config_token)
                _save_token(token_path, {
                    "access_token": new_token,
                    "ig_user_id": ig_cfg.get("ig_user_id", ""),
                    "expires_at": time.time() + expires_in,
                })
                emit_log(
                    log,
                    "  Refreshed long-lived Instagram token (config token was already long-lived; "
                    "exchange only applies to short-lived tokens)",
                )
                return new_token
            except Exception:
                emit_log(
                    log,
                    f"  Note: Could not exchange or refresh Instagram token ({exchange_err}). "
                    "Using config token directly.",
                )
                return config_token

    if config_token:
        return config_token

    raise ValueError(
        "Instagram app_token missing from config.yaml. "
        "Generate one in Meta App Dashboard > Use cases > Instagram API > Generate access tokens."
    )


# ── Temporary file hosting ────────────────────────────────────────────────

_RETRYABLE_EXCEPTIONS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    TimeoutError,
    OSError,
)

_MAX_ATTEMPTS_PER_HOST = 3


def _temp_upload_timeout(file_path: str) -> tuple[float, float]:
    """Connect + read timeouts scaled by file size."""
    size_mb = Path(file_path).stat().st_size / (1024 * 1024)
    read_sec = max(600, min(7200, int(120 + size_mb * 40)))
    return (60.0, float(read_sec))


def _try_tmpfiles(path: Path, timeout: tuple[float, float]) -> str:
    """Upload to tmpfiles.org. Returns a direct-download URL."""
    with open(path, "rb") as f:
        resp = requests.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": (path.name, f, "video/mp4")},
            timeout=timeout,
        )
    _raise_with_body(resp)
    data = resp.json()
    if data.get("status") != "success":
        raise RuntimeError(f"tmpfiles.org upload failed: {data}")
    url = data["data"]["url"]
    return url.replace("tmpfiles.org/", "tmpfiles.org/dl/")


def _try_litterbox(path: Path, timeout: tuple[float, float]) -> str:
    """Upload to litterbox.catbox.moe (24h expiry). Returns a direct URL."""
    with open(path, "rb") as f:
        resp = requests.post(
            "https://litterbox.catbox.moe/resources/internals/api.php",
            data={"reqtype": "fileupload", "time": "24h"},
            files={"fileToUpload": (path.name, f, "video/mp4")},
            timeout=timeout,
        )
    _raise_with_body(resp)
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"litterbox upload returned unexpected response: {url[:200]}")
    return url


_TEMP_HOSTS = [
    ("tmpfiles.org", _try_tmpfiles),
    ("litterbox.catbox.moe", _try_litterbox),
]


def _upload_to_temp_host(file_path: str, log: Callable[[str], None] | None = None) -> str:
    """Upload a file to a temporary public host and return a direct-download URL.

    Tries each host up to ``_MAX_ATTEMPTS_PER_HOST`` times with exponential
    backoff before falling through to the next host.
    """
    path = Path(file_path)
    timeout = _temp_upload_timeout(str(path))
    all_errors: list[str] = []

    for host_name, uploader in _TEMP_HOSTS:
        last_err: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS_PER_HOST + 1):
            try:
                if attempt > 1:
                    emit_log(log, f"    Retrying {host_name} (attempt {attempt}/{_MAX_ATTEMPTS_PER_HOST})...")
                url = uploader(path, timeout)
                if attempt > 1 or host_name != _TEMP_HOSTS[0][0]:
                    emit_log(log, f"    Uploaded via {host_name}")
                return url
            except _RETRYABLE_EXCEPTIONS as e:
                last_err = e
                if attempt < _MAX_ATTEMPTS_PER_HOST:
                    delay = min(60, 5 * (2 ** (attempt - 1)))
                    time.sleep(delay)
                    continue
            except Exception as e:
                last_err = e
                break
        msg = f"{host_name}: {last_err}"
        all_errors.append(msg)
        emit_log(log, f"    {host_name} failed after {attempt} attempt(s), trying next host...")

    raise RuntimeError(
        f"All temp hosts failed for {path.name}. "
        f"Errors: {'; '.join(all_errors)}"
    )


# ── Upload functions ─────────────────────────────────────────────────────

def _create_reel_container(ig_user_id: str, access_token: str, caption: str, video_url: str) -> str:
    """Create a media container with a public video URL. Returns container_id."""
    resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media",
        params={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption[:2200],
            "access_token": access_token,
        },
        timeout=_GRAPH_RQ_TIMEOUT,
    )
    _raise_with_body(resp)
    data = resp.json()
    container_id = data.get("id")
    if not container_id:
        raise RuntimeError(f"Failed to create media container: {data}")
    return container_id


def _wait_for_container(container_id: str, access_token: str, timeout: int = 300, interval: int = 5) -> None:
    """Poll container status until processing is complete."""
    start = time.time()
    while time.time() - start < timeout:
        resp = requests.get(
            f"{GRAPH_API_BASE}/{container_id}",
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=_GRAPH_RQ_TIMEOUT,
        )
        _raise_with_body(resp)
        data = resp.json()
        status = data.get("status_code")

        if status == "FINISHED":
            return
        elif status == "ERROR":
            error_msg = data.get("status", "Unknown error")
            raise RuntimeError(f"Container processing failed: {error_msg}")

        time.sleep(interval)

    raise TimeoutError(f"Container {container_id} not ready after {timeout}s")


def _publish_container(ig_user_id: str, container_id: str, access_token: str) -> str:
    """Publish a finished media container. Returns the published media ID."""
    resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media_publish",
        params={"creation_id": container_id, "access_token": access_token},
        timeout=_GRAPH_RQ_TIMEOUT,
    )
    _raise_with_body(resp)
    media_id = resp.json().get("id")
    if not media_id:
        raise RuntimeError(f"Publish failed: {resp.json()}")
    return media_id


def upload_reel(
    file_path: str,
    ig_user_id: str,
    access_token: str,
    caption: str = "",
    log: Callable[[str], None] | None = None,
) -> str:
    """
    Upload a single Reel from a local file. Returns the published media ID.

    Flow: upload to temp host → create container with video_url → wait → publish.
    """
    # Step 1: Upload to temporary public host (Instagram needs a URL)
    video_url = _upload_to_temp_host(file_path, log=log)

    # Step 2: Create container with the public video URL
    container_id = _create_reel_container(ig_user_id, access_token, caption, video_url)

    # Step 3: Wait for Instagram to finish processing
    _wait_for_container(container_id, access_token)

    # Step 4: Publish
    return _publish_container(ig_user_id, container_id, access_token)


def upload_clips(
    clip_paths: list[str],
    config: dict,
    clip_nums: list[int] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """Upload clips to Instagram Reels. Returns list of media IDs. Pass clip_nums to share numbering."""
    ig_cfg = config.get("instagram", {})
    if not ig_cfg.get("enabled", False):
        return []

    # ig_user_id: from config, or from saved OAuth token
    ig_user_id = ig_cfg.get("ig_user_id", "").strip()

    title_template = ig_cfg.get("title_template", "League Clip {num}")

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

    access_token = get_access_token(config, log=log)

    # If ig_user_id wasn't in config, try to get it from saved token data
    if not ig_user_id:
        saved = _load_token(str(project_root() / TOKEN_FILE))
        if saved:
            ig_user_id = saved.get("ig_user_id", "")
    if not ig_user_id:
        raise ValueError(
            "Instagram ig_user_id missing. Add ig_user_id to config.yaml, "
            "or re-run to authenticate via OAuth (it will be saved automatically)."
        )

    tracking_path = project_root() / IG_UPLOADED_FILE
    uploaded_set = _load_uploaded_paths(tracking_path)

    # Filter out already-uploaded clips
    to_upload: list[tuple[str, int]] = []
    for i, path in enumerate(clip_paths):
        resolved = str(Path(path).resolve())
        clip_num = clip_numbers[i] if i < len(clip_numbers) else clip_numbers[-1] + i
        if resolved in uploaded_set:
            emit_log(log, f"  Skipping (already uploaded to Instagram): {Path(path).name}")
            continue
        to_upload.append((path, clip_num))

    if not to_upload:
        emit_log(log, "  All clips already uploaded to Instagram")
        return []

    uploaded = []
    for i, (path, clip_num) in enumerate(to_upload):
        caption = _SafeFormatter().format(title_template, num=clip_num, n=i + 1, total=len(to_upload))
        emit_log(log, f"  Uploading to Instagram {i+1}/{len(to_upload)} (#{clip_num}): {Path(path).name}")
        clip_start = time.perf_counter()
        try:
            media_id = upload_reel(path, ig_user_id, access_token, caption=caption, log=log)
            if media_id:
                uploaded.append(media_id)
                _mark_uploaded(tracking_path, path)
                dt = time.perf_counter() - clip_start
                emit_log(log, f"    -> Posted to Instagram (ID: {media_id})  ({format_elapsed(dt)})")
            else:
                emit_log(log, "    -> Failed")
        except Exception as e:
            emit_log(log, f"    -> Error: {e}")

    if clip_nums is None and uploaded and to_upload:
        last_num = max(num for _, num in to_upload)
        with open(counter_path, "w") as f:
            f.write(str(last_num + 1))

    return uploaded
