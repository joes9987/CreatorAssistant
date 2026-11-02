#!/usr/bin/env python3
"""Launch the CreatorAssistant clip workflow window."""

import os
import sys
from pathlib import Path


def _ensure_stdio() -> None:
    # Windowed PyInstaller builds have no console; some libraries assume stdout/stderr exist.
    if getattr(sys, "frozen", False):
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")


if __name__ == "__main__":
    _ensure_stdio()
    flags = set(sys.argv[1:])
    if "--selftest" in flags or "--version" in flags:
        from app_selftest import run_selftest, run_version

        if "--selftest" in flags:
            raise SystemExit(run_selftest())
        raise SystemExit(run_version())

    ROOT = Path(__file__).resolve().parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from app_log import setup

    setup()
    from gui.main_app import main

    main()
