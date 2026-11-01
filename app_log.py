"""Local log file next to the app. Never write secrets or config values."""

from __future__ import annotations

import re
import sys
import threading
import time
import traceback

_LOCK = threading.Lock()
_HOOKS_INSTALLED = False

_USER_PATH = re.compile(
    r"(?i)([A-Za-z]:[\\/]Users[\\/])[^\\/]+|(/Users/|/home/)[^/]+"
)
_SECRET_ASSIGN = re.compile(
    r"(?i)([\"']?(?:client_secret|instagram_app_secret|app_secret|access_token|"
    r"refresh_token|client_key|api_key|authorization)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\"'\s,}&]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:code|access_token|refresh_token|client_secret|app_secret)=)[^&\s]+"
)
_LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-])")


def redact(msg: str) -> str:
    """Strip secrets and Windows/macOS home usernames from a log line."""
    text = _USER_PATH.sub(lambda m: (m.group(1) or m.group(2) or "") + "<user>", str(msg))
    text = _SECRET_ASSIGN.sub(r"\1[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _QUERY_SECRET.sub(r"\1[redacted]", text)
    text = _LONG_TOKEN.sub("[redacted]", text)
    return text


def log_path():
    from app_paths import project_root

    return project_root() / "creatorassistant.log"


def write(msg: str) -> bool:
    """Append one redacted record. Failures are ignored by callers that check the bool."""
    try:
        text = redact(msg).replace("\r\n", "\n").replace("\r", "\n")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        lines = text.split("\n")
        block = "\n".join(f"{stamp} {line}" if line else f"{stamp}" for line in lines)
        path = log_path()
        with _LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(block + "\n")
        return True
    except Exception:
        return False


def setup() -> None:
    """Write a startup line and keep uncaught errors in the log."""
    global _HOOKS_INSTALLED
    from version import __version__

    frozen = bool(getattr(sys, "frozen", False))
    py = sys.version.split()[0]
    write(f"start version={__version__} frozen={frozen} py={py}")
    if _HOOKS_INSTALLED:
        return
    _HOOKS_INSTALLED = True

    def _excepthook(exc_type, exc, tb) -> None:
        write("uncaught " + "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        def _thread_hook(args) -> None:
            write(
                "thread "
                + "".join(
                    traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
                )
            )

        threading.excepthook = _thread_hook
