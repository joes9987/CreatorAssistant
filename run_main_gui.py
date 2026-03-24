#!/usr/bin/env python3
"""Launch the CreatorAssistant clip workflow window."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gui.main_app import main

if __name__ == "__main__":
    main()
