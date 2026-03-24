"""
CreatorAssistant - AI-powered clip extraction for League of Legends videos.
Detects highlights automatically, extracts clips, converts to 9:16 for Shorts/TikTok/Reels.

CLI: python main.py [video ...]  (no args opens file picker; default folder from config paths)
GUI: python run_main_gui.py
"""

import sys
from pathlib import Path

from app_paths import project_root
from detect import load_config
from pipeline import process_videos
from ui_dialogs import select_video_files


def default_recordings_dir(config: dict) -> Path:
    """Folder used for file picker and fallback glob (config paths.default_input_dir or ./input)."""
    paths = config.get("paths") or {}
    raw = paths.get("default_input_dir", "input")
    p = Path(raw)
    if not p.is_absolute():
        p = project_root() / p
    return p.resolve()


def main() -> None:
    config_path = project_root() / "config.yaml"
    config = load_config(str(config_path))
    recordings = default_recordings_dir(config)
    recordings.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) >= 2:
        videos = [Path(p) for p in sys.argv[1:] if Path(p).exists()]
    else:
        videos = select_video_files(initial_dir=recordings)
        if not videos:
            videos = list(recordings.glob("*.mp4")) + list(recordings.glob("*.mkv"))
            if not videos:
                print("Usage: python main.py <video_path> [video_path2 ...]")
                print(f"   Or run with no args to browse (starts in {recordings}).")
                print("   Place .mp4/.mkv in that folder or pass paths on the command line.")
                sys.exit(1)

    process_videos(videos, config)


if __name__ == "__main__":
    main()
