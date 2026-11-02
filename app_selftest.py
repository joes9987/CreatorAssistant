"""Startup checks that do not open the GUI or the network."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

from app_log import setup, write
from version import __version__


def run_version() -> int:
    print(__version__)
    write(f"version {__version__}")
    return 0


def run_selftest() -> int:
    """Exit 0 when the app folder is usable. FFmpeg missing is only a warning."""
    setup()
    errors: list[str] = []
    frozen = bool(getattr(sys, "frozen", False))

    if frozen:
        from app_paths import project_root

        dll = project_root() / "_internal" / "python313.dll"
        if not dll.is_file():
            errors.append(
                "Missing _internal folder. Please extract the full zip, don't copy only the .exe."
            )
        else:
            write("layout: _internal ok")
    else:
        write("layout: source tree")

    config_data = _load_config(errors)
    _note_ffmpeg(config_data)

    if errors:
        for msg in errors:
            write("selftest fail: " + msg)
            print(msg, file=sys.stderr)
        return 1

    if not write("selftest ok"):
        print("Could not write creatorassistant.log next to the app.", file=sys.stderr)
        return 1
    print(f"CreatorAssistant {__version__} selftest ok")
    return 0


def _load_config(errors: list[str]) -> dict | None:
    from app_paths import project_root

    path = project_root() / "config.yaml"
    if not path.is_file():
        errors.append("Missing config.yaml next to the app.")
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except Exception:
        # Parser errors can echo file contents. Do not log the exception text.
        errors.append("config.yaml could not be parsed.")
        return None
    if not isinstance(data, dict):
        errors.append("config.yaml could not be parsed.")
        return None
    write("config: ok")
    return data


def _note_ffmpeg(config_data: dict | None) -> None:
    found = False
    if isinstance(config_data, dict):
        raw = config_data.get("ffmpeg_path") or ""
        if isinstance(raw, str) and raw.strip():
            found = Path(raw.strip()).exists()
    if not found and shutil.which("ffmpeg"):
        found = True
    if found:
        write("ffmpeg: found")
    else:
        write("warning: FFmpeg not found (not a selftest failure)")
