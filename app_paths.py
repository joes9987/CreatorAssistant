"""
Resolve the project/application root correctly for both dev (python script.py) and
frozen (PyInstaller --onefile) environments.

Frozen onefile: __file__ points into a temp extraction dir (_MEIxxxxx).
User files (config.yaml, eventlogs/, outputs/, etc.) live next to the .exe,
so we use Path(sys.executable).parent in that case.
"""

import sys
from pathlib import Path


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
