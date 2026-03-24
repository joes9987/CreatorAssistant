#!/usr/bin/env python3
"""Launch the game events logger window (separate from the clip workflow)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.logger_app import main

if __name__ == "__main__":
    main()
