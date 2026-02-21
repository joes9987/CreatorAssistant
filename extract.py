"""
Clip extraction and 9:16 conversion using FFmpeg.
Takes detected highlights and produces Shorts/TikTok/Reels-ready vertical clips.
"""

import subprocess
from pathlib import Path

import yaml

from detect import _get_ffmpeg_bin


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def extract_clip(
    video_path: str,
    start_sec: float,
    end_sec: float,
    output_path: str,
    aspect_ratio: str = "9:16",
    ffmpeg_path: str = "ffmpeg",
    crf: int = 18,
    preset: str = "slow",
) -> bool:
    """
    Extract a clip from video and convert to vertical 9:16.
    Uses center crop to preserve the action.
    """
    duration = end_sec - start_sec
    output_path = str(Path(output_path).resolve())

    # Center crop to 9:16, scale with Lanczos for sharper output (better than default bicubic)
    crop_filter = "crop='min(iw,ih*9/16)':'ih':'max(0,(iw-ih*9/16)/2)':'0',scale=1080:1920:flags=lanczos"
    cmd = [
        ffmpeg_path, "-y",
        "-ss", str(start_sec),
        "-i", video_path,
        "-t", str(duration),
        "-vf", crop_filter,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "192k",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0
    except Exception as e:
        print(f"  Error extracting clip: {e}")
        return False


def extract_all_clips(
    video_path: str,
    highlights: list[dict],
    output_dir: str | None = None,
    base_name: str | None = None,
    config: dict | None = None,
) -> list[str]:
    """
    Extract all highlight clips from a video.
    Returns list of output file paths.
    """
    if config is None:
        config = load_config()

    clip_cfg = config.get("clip", {})
    out_dir = output_dir or clip_cfg.get("output_dir", "outputs")
    aspect = clip_cfg.get("aspect_ratio", "9:16")
    crf = clip_cfg.get("crf", 18)
    preset = clip_cfg.get("preset", "slow")

    ffmpeg_path, _ = _get_ffmpeg_bin(config)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    video_stem = Path(video_path).stem
    if base_name:
        video_stem = base_name

    output_paths = []
    for i, h in enumerate(highlights):
        out_name = f"{video_stem}_clip_{i+1:02d}.mp4"
        out_path = str(Path(out_dir) / out_name)
        out_file = Path(out_path)

        if out_file.exists():
            print(f"  Skipping clip {i+1}/{len(highlights)} (already exists): {out_name}")
            output_paths.append(out_path)
            continue

        print(f"  Extracting clip {i+1}/{len(highlights)}: {h['start']:.1f}s - {h['end']:.1f}s")
        if extract_clip(
            video_path, h["start"], h["end"], out_path, aspect, ffmpeg_path, crf, preset
        ):
            output_paths.append(out_path)
            print(f"    -> {out_path}")
        else:
            print(f"    -> Failed")

    return output_paths
