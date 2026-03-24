"""Persist GUI preferences (e.g. default recordings folder) next to the app or project root."""

from __future__ import annotations

import json
from pathlib import Path

from app_paths import project_root


def settings_path() -> Path:
    return project_root() / "gui_settings.json"


def default_input_dir_value() -> str:
    return str(project_root() / "input")


def load_gui_settings() -> dict:
    p = settings_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
    return {"default_input_dir": default_input_dir_value()}


def save_gui_settings(data: dict) -> None:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_resolved_default_input_dir() -> Path:
    s = load_gui_settings()
    raw = s.get("default_input_dir") or default_input_dir_value()
    path = Path(raw)
    if not path.is_absolute():
        path = project_root() / path
    return path.resolve()
