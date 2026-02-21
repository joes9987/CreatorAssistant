"""
TikTok upload for CreatorAssistant.
Uses TikTok Content Posting API. Requires developer account + app approval for video.publish.
Note: Unverified apps may only post to private accounts.
"""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TIKTOK_TOKEN_FILE = "tiktok_token.json"


def get_tiktok_client(config: dict):
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

    base = Path(__file__).parent
    token_path = base / TIKTOK_TOKEN_FILE

    # Try loading existing token
    if token_path.exists():
        try:
            with open(token_path) as f:
                data = json.load(f)
            tik = TikTok(client_key=client_key, client_secret=client_secret, redirect_uri=redirect_uri)
            tik.access_token = data.get("access_token")
            tik.refresh_token = data.get("refresh_token")
            tik.open_id = data.get("open_id")
            return tik
        except Exception:
            pass

    # OAuth flow
    tik = TikTok(
        client_key=client_key,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=["video.publish", "user.info.basic"],
    )
    auth_url = tik.get_authorization_url()
    print("  Open this URL in your browser to authorize:")
    print(f"  {auth_url}")

    # Simple local server to catch callback
    from http.server import HTTPServer, BaseHTTPRequestHandler

    code_received = []

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path in ("/callback", "/"):
                params = parse_qs(parsed.query)
                code_received.append(params.get("code", [None])[0])
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization complete. You can close this tab.</h1>")

        def log_message(self, *args):
            pass

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    print("  Waiting for authorization (visit the URL above)...")
    server.handle_request()
    code = code_received[0] if code_received else None
    if not code:
        raise RuntimeError("No authorization code received. Ensure redirect_uri in config matches http://localhost:8080/callback in your TikTok app settings.")

    token_data = tik.exchange_code_for_token(code=code)
    with open(token_path, "w") as f:
        json.dump({
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "open_id": token_data["open_id"],
        }, f, indent=2)
    tik.access_token = token_data["access_token"]
    tik.refresh_token = token_data["refresh_token"]
    tik.open_id = token_data["open_id"]
    return tik


def upload_clips(clip_paths: list[str], config: dict, clip_nums: list[int] | None = None) -> list[str]:
    """Upload clips to TikTok. Returns list of publish_ids. Pass clip_nums to share numbering with YouTube."""
    ttk_cfg = config.get("tiktok", {})
    if not ttk_cfg.get("enabled", False):
        return []

    title_template = ttk_cfg.get("title_template", "joes9987 | League Clip {num}")

    base = Path(__file__).parent
    counter_path = base / "clip_counter.txt"
    if clip_nums is not None:
        clip_numbers = clip_nums
    else:
        counter_start = config.get("youtube", {}).get("clip_counter_start", 1)
        try:
            start = int(counter_path.read_text().strip()) if counter_path.exists() else counter_start
        except (ValueError, OSError):
            start = counter_start
        clip_numbers = [start + i for i in range(len(clip_paths))]

    tik = get_tiktok_client(config)
    privacy = ttk_cfg.get("privacy", "PUBLIC_TO_EVERYONE")

    uploaded = []
    for i, path in enumerate(clip_paths):
        clip_num = clip_numbers[i] if i < len(clip_numbers) else clip_numbers[-1] + i
        title = title_template.format(num=clip_num, n=i + 1, total=len(clip_paths))
        print(f"  Uploading to TikTok {i+1}/{len(clip_paths)} (#{clip_num}): {Path(path).name}")
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
                print(f"    -> Posted to TikTok")
            else:
                err = resp.get("error", resp.get("initial_response", {}).get("error", {})) if isinstance(resp, dict) else {}
                print(f"    -> Failed: {err.get('message', resp)}")
        except Exception as e:
            print(f"    -> Error: {e}")
    if clip_nums is None and uploaded and clip_numbers:
        with open(counter_path, "w") as f:
            f.write(str(clip_numbers[-1] + 1))
    return uploaded
