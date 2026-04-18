"""
Shared logging and timing utilities for CreatorAssistant.
"""

import sys
import time
from collections.abc import Callable


def _format_elapsed(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_elapsed(secs: float) -> str:
    """Human-readable duration string (e.g. '1:23' or '1:02:03')."""
    return _format_elapsed(secs)


def emit_log(log: Callable[[str], None] | None, msg: str) -> None:
    """Print to stdout or forward to a GUI/thread log callback."""
    if log:
        log(msg)
    else:
        print(msg)


def iter_with_timer(iterable, desc: str):
    """Wrap an iterable and show elapsed time while iterating (single line, updates in place)."""
    start = time.time()
    count = 0
    for item in iterable:
        count += 1
        elapsed = time.time() - start
        sys.stdout.write(f"\r  {desc}... {_format_elapsed(elapsed)} elapsed ({count} samples)    ")
        sys.stdout.flush()
        yield item
    elapsed = time.time() - start
    sys.stdout.write(f"\r  {desc}... Done in {_format_elapsed(elapsed)}    \n")
    sys.stdout.flush()
