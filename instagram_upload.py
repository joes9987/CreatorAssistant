"""
Instagram Reels upload for CreatorAssistant.
Uses Instagram Graph API. Meta downloads the clip from a URL, so the file is
hosted on a Vercel Blob store the project owner controls and deleted afterwards.
Requires a Meta Developer app with instagram_content_publish permission.
Uses Instagram Login OAuth flow (like TikTok) — no manual token copying needed.
"""

import json
import os
import re
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


# ── Clip hosting (Vercel Blob via the project site) ──────────────────────
#
# Instagram's content publishing API downloads the video from a URL. The desktop
# app uploads the clip to a Blob store the project owner controls, passes that
# URL to Meta, then deletes the object. The Blob read-write credential stays on
# Vercel. config.yaml only has a limited token for /api/ig-media.

class InstagramStorageError(RuntimeError):
    """Clip hosting is missing or the storage API rejected the request."""


_STORAGE_NOT_CONFIGURED = (
    "Instagram clip hosting is not configured. "
    "Set instagram.storage.api_url and instagram.storage.token in config.yaml "
    "(see README and config.example.yaml). "
    "Clips must be hosted on a Vercel Blob store you control. "
    "Anonymous public file hosts are not used."
)

_PLACEHOLDER_STORAGE_TOKENS = {"", "YOUR_IG_STORAGE_TOKEN"}
_PATHNAME_RE = re.compile(
    r"^ig-reels/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4$"
)
# Instagram Reels accepts up to about 1 GiB. The server enforces a lower cap.
_LOCAL_MAX_CLIP_BYTES = 1024 * 1024 * 1024
_STORAGE_ATTEMPTS = 3

_RETRYABLE_EXCEPTIONS = (
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
    TimeoutError,
    OSError,
)


def _hosted_upload_timeout(file_path: str, attempt: int = 1) -> tuple[float, float]:
    """Connect + read timeouts scaled by file size and retry attempt.

    The clip is one PUT to Blob storage. Slow uplinks hit
    ``TimeoutError: The write operation timed out`` if the read timeout is too low.
    Each retry lengthens the budget slightly.
    """
    size_mb = Path(file_path).stat().st_size / (1024 * 1024)
    base_read = 180.0 + size_mb * 75.0
    attempt_mult = 1.0 + 0.45 * (attempt - 1)
    read_sec = int(min(14_400, max(1200, base_read * attempt_mult)))  # min 20m, cap 4h
    connect_sec = min(300.0, 90.0 + size_mb * 2.0)
    return (connect_sec, float(read_sec))


def _resolve_instagram_storage(ig_cfg: dict) -> dict:
    """Return the hosting settings, or raise if they are missing or unusable."""
    storage = ig_cfg.get("storage")
    if not isinstance(storage, dict):
        raise InstagramStorageError(_STORAGE_NOT_CONFIGURED)

    provider = str(storage.get("provider") or "vercel_blob").strip()
    api_url = str(storage.get("api_url") or "").strip().rstrip("/")
    token = str(storage.get("token") or "").strip()

    if provider != "vercel_blob":
        raise InstagramStorageError(
            f'Instagram storage provider "{provider}" is not supported. '
            'Set instagram.storage.provider to "vercel_blob". '
            "Anonymous public file hosts are not used."
        )
    if not api_url or token in _PLACEHOLDER_STORAGE_TOKENS:
        raise InstagramStorageError(_STORAGE_NOT_CONFIGURED)

    parsed = urlparse(api_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise InstagramStorageError(
            "instagram.storage.api_url must be an https:// URL "
            "(the /api/ig-media endpoint on your Vercel project)."
        )
    return {"provider": provider, "api_url": api_url, "token": token}


def _retry_pause(attempt: int) -> None:
    time.sleep(min(60, 5 * (2 ** (attempt - 1))))


def _storage_error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("error"):
        detail = str(data["error"])[:500]
    else:
        detail = (resp.text or "")[:300]
    if resp.status_code == 401:
        return (
            "Instagram storage rejected the token. "
            "Set instagram.storage.token to the same value as IG_STORAGE_TOKEN "
            f"on the Vercel project. ({detail})"
        )
    if resp.status_code == 404:
        return (
            "Instagram storage API was not found. "
            "Deploy api/ig-media and check instagram.storage.api_url. "
            f"({detail})"
        )
    return f"Instagram storage API returned {resp.status_code}: {detail}"


def _storage_post(storage: dict, payload: dict) -> dict:
    """POST JSON to the hosting API. Retries network errors and HTTP 5xx."""
    url = storage["api_url"]
    headers = {
        "Authorization": f"Bearer {storage['token']}",
        "Accept": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(1, _STORAGE_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=(30.0, 60.0),
            )
        except _RETRYABLE_EXCEPTIONS as exc:
            last_error = exc
            if attempt < _STORAGE_ATTEMPTS:
                _retry_pause(attempt)
                continue
            raise InstagramStorageError(
                f"Could not reach Instagram storage at {url}. "
                "Check instagram.storage.api_url and that the Vercel project is deployed."
            ) from exc
        if resp.status_code >= 500 and attempt < _STORAGE_ATTEMPTS:
            last_error = InstagramStorageError(_storage_error_message(resp))
            _retry_pause(attempt)
            continue
        if resp.status_code >= 400:
            raise InstagramStorageError(_storage_error_message(resp))
        try:
            data = resp.json()
        except ValueError as exc:
            raise InstagramStorageError(
                "Instagram storage API returned a non-JSON response."
            ) from exc
        if not isinstance(data, dict):
            raise InstagramStorageError("Instagram storage API returned an unexpected response.")
        return data
    raise InstagramStorageError(str(last_error or "Instagram storage API request failed."))


def _validate_hosted_target(upload_url: str, video_url: str, pathname: str) -> None:
    """Refuse to send clip bytes anywhere except the Blob upload URL we expect."""
    upload = urlparse(upload_url)
    video = urlparse(video_url)
    upload_ok = (
        upload.scheme == "https"
        and upload.hostname == "vercel.com"
        and upload.path.startswith("/api/blob")
    )
    video_host = video.hostname or ""
    video_ok = video.scheme == "https" and video_host.endswith(".blob.vercel-storage.com")
    if not upload_ok or not video_ok or not _PATHNAME_RE.match(pathname or ""):
        raise InstagramStorageError(
            "Instagram storage API returned an unexpected upload target. "
            "The clip was not uploaded."
        )


def _delete_hosted_clip(
    storage: dict,
    pathname: str,
    log: Callable[[str], None] | None,
) -> None:
    """Delete a hosted clip. Failures are logged; they must not hide the publish result."""
    if not _PATHNAME_RE.match(pathname or ""):
        emit_log(log, "    Warning: skipped delete of a hosted clip with an unexpected pathname")
        return
    try:
        _storage_post(storage, {"op": "delete", "pathname": pathname})
    except InstagramStorageError as exc:
        if "returned 404" in str(exc):
            emit_log(log, f"    Hosted clip {pathname} was already gone")
            return
        emit_log(log, f"    Warning: could not delete hosted clip {pathname}: {exc}")
        return
    emit_log(log, f"    Removed hosted clip {pathname}")


def _put_clip_bytes(upload_url: str, path: Path) -> None:
    last_error: Exception | None = None
    for attempt in range(1, _STORAGE_ATTEMPTS + 1):
        timeout = _hosted_upload_timeout(str(path), attempt)
        try:
            with open(path, "rb") as handle:
                resp = requests.put(
                    upload_url,
                    data=handle,
                    headers={"Content-Type": "video/mp4"},
                    timeout=timeout,
                    allow_redirects=False,
                )
        except _RETRYABLE_EXCEPTIONS as exc:
            last_error = exc
            if attempt < _STORAGE_ATTEMPTS:
                _retry_pause(attempt)
                continue
            raise InstagramStorageError(
                f"Upload to clip storage failed after {_STORAGE_ATTEMPTS} attempts: {exc}"
            ) from exc
        if resp.is_redirect or resp.status_code in {301, 302, 303, 307, 308}:
            raise InstagramStorageError(
                "Clip storage returned a redirect. The clip was not uploaded."
            )
        if resp.status_code >= 400:
            detail = (resp.text or "")[:300]
            raise InstagramStorageError(
                f"Upload to clip storage failed ({resp.status_code}): {detail}"
            )
        return
    raise InstagramStorageError(
        f"Upload to clip storage failed after {_STORAGE_ATTEMPTS} attempts: {last_error}"
    )


def _host_clip_for_instagram(
    file_path: str,
    storage: dict,
    log: Callable[[str], None] | None,
) -> dict:
    """Upload one clip. Returns pathname and video_url; the caller must delete pathname."""
    path = Path(file_path)
    if not path.is_file():
        raise InstagramStorageError(f"Clip file not found: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise InstagramStorageError(f"Clip file is empty: {path.name}")
    if size > _LOCAL_MAX_CLIP_BYTES:
        raise InstagramStorageError(
            f"{path.name} is too large to host for Instagram "
            f"({size} bytes; limit is {_LOCAL_MAX_CLIP_BYTES} bytes)."
        )

    size_mb = size / (1024 * 1024)
    emit_log(log, f"    Hosting {path.name} ({size_mb:.1f} MB) for Instagram to fetch")
    created = _storage_post(
        storage,
        {
            "op": "create",
            "contentType": "video/mp4",
            "contentLength": size,
        },
    )
    upload_url = str(created.get("uploadUrl") or "")
    video_url = str(created.get("videoUrl") or "")
    pathname = str(created.get("pathname") or "")
    try:
        _validate_hosted_target(upload_url, video_url, pathname)
    except InstagramStorageError:
        if _PATHNAME_RE.match(pathname or ""):
            _delete_hosted_clip(storage, pathname, log)
        raise

    try:
        _put_clip_bytes(upload_url, path)
    except Exception:
        _delete_hosted_clip(storage, pathname, log)
        raise

    emit_log(log, f"    Hosted as {pathname}")
    return {"pathname": pathname, "video_url": video_url}


# ── Upload functions ─────────────────────────────────────────────────────

def _create_reel_container(ig_user_id: str, access_token: str, caption: str, video_url: str) -> str:
    """Create a media container with a video URL Instagram can fetch. Returns container_id.

    Parameters go in the form body. A signed Blob URL is too long to put on the query string.
    """
    resp = requests.post(
        f"{GRAPH_API_BASE}/{ig_user_id}/media",
        data={
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
    storage: dict | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    """
    Upload a single Reel from a local file. Returns the published media ID.

    Flow: host on Vercel Blob → create container with video_url → wait → publish.
    The hosted object is deleted after publish and if any step fails.
    """
    resolved = _resolve_instagram_storage({"storage": storage} if storage is not None else {})
    hosted: dict | None = None
    try:
        hosted = _host_clip_for_instagram(file_path, resolved, log)
        container_id = _create_reel_container(
            ig_user_id, access_token, caption, hosted["video_url"]
        )
        _wait_for_container(container_id, access_token)
        return _publish_container(ig_user_id, container_id, access_token)
    finally:
        if hosted is not None:
            _delete_hosted_clip(resolved, hosted["pathname"], log)


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

    # Fail before OAuth so a missing store is obvious and no clip leaves the machine.
    storage = _resolve_instagram_storage(ig_cfg)

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
                media_id = upload_reel(
                    path, ig_user_id, access_token, caption=caption, storage=storage, log=log
                )
            except Exception as e:
                if _is_instagram_session_expired(e):
                    emit_log(log, "    Instagram token expired — re-authenticating and retrying this clip...")
                    access_token = get_access_token(config, log=log, force_reauth=True)
                    saved = _load_token(str(project_root() / TOKEN_FILE))
                    if saved and saved.get("ig_user_id"):
                        ig_user_id = saved["ig_user_id"]
                    media_id = upload_reel(
                        path, ig_user_id, access_token, caption=caption, storage=storage, log=log
                    )
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
