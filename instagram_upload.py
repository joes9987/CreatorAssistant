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
from urllib.parse import parse_qs, quote, urlencode, urlparse

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
IG_AUTH_URL = "https://api.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
# Instagram Login (2024+). Legacy instagram_basic / instagram_content_publish were deprecated.
DEFAULT_OAUTH_SCOPES = "instagram_business_basic,instagram_business_content_publish"


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

def _instagram_app_credentials(ig_cfg: dict) -> tuple[str, str]:
    """Return (app_id, app_secret) for Instagram Login OAuth.

    Prefer instagram_app_id / instagram_app_secret when set — these must come from
    Meta App Dashboard → Instagram → API setup with Instagram Login, NOT the top-level
    Facebook App ID on the app overview page.
    """
    app_id = (ig_cfg.get("instagram_app_id") or ig_cfg.get("app_id") or "").strip()
    app_secret = (ig_cfg.get("instagram_app_secret") or ig_cfg.get("app_secret") or "").strip()
    return app_id, app_secret


def _instagram_oauth_scopes(ig_cfg: dict) -> str:
    return (ig_cfg.get("oauth_scopes") or DEFAULT_OAUTH_SCOPES).strip()


def _run_instagram_oauth(
    app_id: str,
    app_secret: str,
    redirect_uri: str,
    scopes: str,
    local_oauth_port: int = 8081,
    log: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Run Instagram Login OAuth flow. Opens browser URL, catches callback on localhost.
    redirect_uri is the public HTTPS URI registered with Meta; local_oauth_port receives
    the code after callback.html forwards the browser from Vercel.
    Returns (short_lived_access_token, ig_user_id)."""

    auth_params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
    }
    auth_url = f"{IG_AUTH_URL}?{urlencode(auth_params)}"
    emit_log(log, "  Instagram: open this URL in your browser to authorize:")
    emit_log(log, f"  {auth_url}")
    emit_log(log, f"  Redirect URI (Meta): {redirect_uri}")
    emit_log(log, f"  Local listener: http://127.0.0.1:{local_oauth_port}/callback")
    emit_log(log, f"  Scopes: {scopes}")
    emit_log(
        log,
        "  After login, your browser will redirect through creator-assistant.vercel.app "
        "back to the app.",
    )
    emit_log(
        log,
        "  If you see 'Invalid platform app': use instagram_app_id + instagram_app_secret "
        "from Meta Dashboard → Instagram → API setup with Instagram Login.",
    )

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

    server = HTTPServer(("127.0.0.1", local_oauth_port), CallbackHandler)
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
    if isinstance(data, dict) and data.get("error_type") == "OAuthException":
        msg = data.get("error_message", str(data))
        if "Invalid platform app" in msg:
            raise RuntimeError(
                f"Instagram OAuth failed: {msg}. "
                "Use instagram_app_id and instagram_app_secret from Meta Dashboard → "
                "Instagram → API setup with Instagram Login (not the Facebook App ID)."
            )
        raise RuntimeError(f"Instagram OAuth token exchange failed: {data}")
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

def _save_long_lived_token(token_path: str, access_token: str, expires_in: int, ig_user_id: str) -> None:
    _save_token(token_path, {
        "access_token": access_token,
        "ig_user_id": ig_user_id,
        "expires_at": time.time() + expires_in,
    })


def _oauth_long_lived_token(
    config: dict,
    log: Callable[[str], None] | None = None,
) -> str:
    """Browser OAuth → short-lived token → long-lived token saved to instagram_token.json."""
    ig_cfg = config.get("instagram", {})
    app_id, app_secret = _instagram_app_credentials(ig_cfg)
    redirect_uri = ig_cfg.get(
        "redirect_uri", "https://creator-assistant.vercel.app/callback.html"
    ).strip()
    local_oauth_port = int(ig_cfg.get("local_oauth_port", 8081))
    scopes = _instagram_oauth_scopes(ig_cfg)
    if not app_id or not app_secret:
        raise ValueError(
            "Instagram app credentials required in config.yaml for browser login. "
            "Set instagram_app_id and instagram_app_secret (from Instagram Login setup)."
        )

    short_token, user_id = _run_instagram_oauth(
        app_id,
        app_secret,
        redirect_uri,
        scopes,
        local_oauth_port=local_oauth_port,
        log=log,
    )
    long_token, expires_in = _exchange_for_long_lived_token(short_token, app_secret)
    ig_user_id = user_id or ig_cfg.get("ig_user_id", "").strip()
    token_path = str(project_root() / TOKEN_FILE)
    _save_long_lived_token(token_path, long_token, expires_in, ig_user_id)
    emit_log(log, "  Instagram: saved new long-lived token (valid ~60 days)")
    return long_token


def _try_refresh_saved_token(
    token_path: str,
    token: str,
    ig_user_id: str,
    log: Callable[[str], None] | None,
) -> str | None:
    try:
        new_token, expires_in = _refresh_long_lived_token(token)
        _save_long_lived_token(token_path, new_token, expires_in, ig_user_id)
        emit_log(log, "  Refreshed long-lived Instagram token")
        return new_token
    except Exception:
        return None


def _is_instagram_session_expired(exc: BaseException) -> bool:
    """True for Meta OAuth 190 / expired session errors."""
    if not isinstance(exc, requests.exceptions.HTTPError):
        return False
    msg = str(exc)
    return (
        "'code': 190" in msg
        or '"code": 190' in msg
        or "Session has expired" in msg
        or "Error validating access token" in msg
    )


def get_access_token(
    config: dict,
    log: Callable[[str], None] | None = None,
    *,
    force_reauth: bool = False,
) -> str:
    """
    Get a valid Instagram access token.
    Uses instagram_token.json when valid; refreshes before expiry; opens browser OAuth when needed.
    """
    ig_cfg = config.get("instagram", {})
    token_path = str(project_root() / TOKEN_FILE)
    app_id, app_secret = _instagram_app_credentials(ig_cfg)

    if force_reauth:
        emit_log(log, "  Instagram session expired — complete browser login to continue...")
        return _oauth_long_lived_token(config, log=log)

    # 1. Saved long-lived token from a previous OAuth run
    saved = _load_token(token_path)
    if saved and saved.get("access_token"):
        token = saved["access_token"]
        expires_at = float(saved.get("expires_at") or 0)
        ig_user_id = saved.get("ig_user_id", "")

        if expires_at and time.time() < expires_at - (7 * 86400):
            return token

        refreshed = _try_refresh_saved_token(token_path, token, ig_user_id, log)
        if refreshed:
            return refreshed

        emit_log(log, "  Saved Instagram token expired — opening browser login...")
        return _oauth_long_lived_token(config, log=log)

    # 2. Token pasted in config.yaml (Meta dashboard) — exchange or refresh once, then OAuth
    config_token = (ig_cfg.get("app_token", "") or ig_cfg.get("access_token", "")).strip()
    config_token = config_token.strip().strip('"').strip("'")
    if config_token and app_secret:
        try:
            long_token, expires_in = _exchange_for_long_lived_token(config_token, app_secret)
            _save_long_lived_token(
                token_path,
                long_token,
                expires_in,
                ig_cfg.get("ig_user_id", "").strip(),
            )
            emit_log(log, "  Exchanged config token for long-lived Instagram token (~60 days)")
            return long_token
        except Exception:
            refreshed = _try_refresh_saved_token(
                token_path,
                config_token,
                ig_cfg.get("ig_user_id", "").strip(),
                log,
            )
            if refreshed:
                return refreshed

    if app_id and app_secret:
        emit_log(log, "  Instagram: no valid token — complete browser login...")
        return _oauth_long_lived_token(config, log=log)

    if config_token:
        emit_log(
            log,
            "  Warning: using access_token from config.yaml without refresh; "
            "add app_id/app_secret and use browser login if it expires.",
        )
        return config_token

    raise ValueError(
        "Instagram credentials missing. Set app_id, app_secret, and redirect_uri in config.yaml, "
        "then upload again to sign in via browser."
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


def _temp_upload_timeout(file_path: str, attempt: int = 1) -> tuple[float, float]:
    """Connect + read timeouts scaled by file size and retry attempt.

    Temp-host uploads are one big POST/PUT; slow uplinks often hit
    ``TimeoutError: The write operation timed out`` if read timeout is too low.
    Each retry lengthens the budget slightly.
    """
    size_mb = Path(file_path).stat().st_size / (1024 * 1024)
    # Base: generous seconds-per-MB for consumer upload links (hosts + TLS + buffering).
    base_read = 180.0 + size_mb * 75.0
    attempt_mult = 1.0 + 0.45 * (attempt - 1)
    read_sec = int(min(14_400, max(1200, base_read * attempt_mult)))  # min 20m, cap 4h
    connect_sec = min(300.0, 90.0 + size_mb * 2.0)
    return (connect_sec, float(read_sec))


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


def _try_transfer_sh(path: Path, timeout: tuple[float, float]) -> str:
    """Upload via transfer.sh (PUT). Returns a plain-text URL body."""
    with open(path, "rb") as f:
        resp = requests.put(
            f"https://transfer.sh/{quote(path.name)}",
            data=f,
            headers={"Content-Type": "video/mp4"},
            timeout=timeout,
        )
    _raise_with_body(resp)
    url = resp.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"transfer.sh returned unexpected response: {url[:200]}")
    return url


def _try_0x0(path: Path, timeout: tuple[float, float]) -> str:
    """Upload to 0x0.st (anonymous host). Returns a plain-text URL body."""
    with open(path, "rb") as f:
        resp = requests.post(
            "https://0x0.st",
            files={"file": (path.name, f, "video/mp4")},
            timeout=timeout,
        )
    _raise_with_body(resp)
    url = resp.text.strip().split()[0] if resp.text.strip() else ""
    if not url.startswith("http"):
        raise RuntimeError(f"0x0.st returned unexpected response: {resp.text[:200]}")
    return url


_TEMP_HOSTS = [
    ("tmpfiles.org", _try_tmpfiles),
    ("litterbox.catbox.moe", _try_litterbox),
    ("transfer.sh", _try_transfer_sh),
    ("0x0.st", _try_0x0),
]


def _upload_to_temp_host(file_path: str, log: Callable[[str], None] | None = None) -> str:
    """Upload a file to a temporary public host and return a direct-download URL.

    Tries each host up to ``_MAX_ATTEMPTS_PER_HOST`` times with exponential
    backoff before falling through to the next host.
    """
    path = Path(file_path)
    size_mb = path.stat().st_size / (1024 * 1024)
    emit_log(log, f"    Temp upload ({path.name}): {size_mb:.1f} MB → public URL for Instagram fetch")

    all_errors: list[str] = []

    for host_name, uploader in _TEMP_HOSTS:
        last_err: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS_PER_HOST + 1):
            timeout = _temp_upload_timeout(str(path), attempt)
            try:
                if attempt > 1:
                    emit_log(log, f"    Retrying {host_name} (attempt {attempt}/{_MAX_ATTEMPTS_PER_HOST})...")
                else:
                    emit_log(
                        log,
                        f"    {host_name}: connect={timeout[0]:.0f}s read={timeout[1]:.0f}s",
                    )
                url = uploader(path, timeout)
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
            try:
                media_id = upload_reel(path, ig_user_id, access_token, caption=caption, log=log)
            except Exception as e:
                if _is_instagram_session_expired(e):
                    emit_log(log, "    Instagram token expired — re-authenticating and retrying this clip...")
                    access_token = get_access_token(config, log=log, force_reauth=True)
                    saved = _load_token(str(project_root() / TOKEN_FILE))
                    if saved and saved.get("ig_user_id"):
                        ig_user_id = saved["ig_user_id"]
                    media_id = upload_reel(path, ig_user_id, access_token, caption=caption, log=log)
                else:
                    raise
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
