#!/usr/bin/env python3
"""Launch the game events logger window (separate from the clip workflow)."""

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.logger_app import main

if __name__ == "__main__":
    main()
