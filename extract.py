"""
Clip extraction and 9:16 conversion using FFmpeg.
Takes detected highlights and produces Shorts/TikTok/Reels-ready vertical clips.
"""

import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from detect import _get_ffmpeg_bin


def _log(log: Callable[[str], None] | None, msg: str) -> None:
    if log:
        log(msg)
    else:
        print(msg)


def _format_elapsed(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _detect_hw_encoder(ffmpeg_path: str) -> str | None:
    """Detect if NVENC (NVIDIA) is available. Returns encoder name or None."""
    try:
        r = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5
        )
        if "h264_nvenc" in r.stdout:
            return "h264_nvenc"
    except Exception:
        pass
    return None


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
    preset: str = "medium",
    video_encoder: str | None = None,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Extract a clip from video and convert to vertical 9:16."""
    duration = end_sec - start_sec
    output_path = str(Path(output_path).resolve())

    crop_filter = "crop='min(iw,ih*9/16)':'ih':'max(0,(iw-ih*9/16)/2)':'0',scale=1080:1920:flags=lanczos,format=yuv420p"

    encoder = video_encoder or "libx264"
    if encoder == "h264_nvenc":
        vcodec_args = ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    else:
        vcodec_args = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]

    cmd = (
        [ffmpeg_path, "-y", "-ss", str(start_sec), "-i", video_path, "-t", str(duration)]
        + ["-vf", crop_filter]
        + vcodec_args
        + ["-c:a", "aac", "-b:a", "192k", output_path]
    )

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 and result.stderr:
            last_lines = result.stderr.strip().split("\n")[-3:]
            _log(log, f"  FFmpeg: {' '.join(last_lines)}")
        return result.returncode == 0
    except Exception as e:
        _log(log, f"  Error extracting clip: {e}")
        return False


def extract_all_clips(
    video_path: str,
    highlights: list[dict],
    output_dir: str | None = None,
    base_name: str | None = None,
    config: dict | None = None,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """
    Extract all highlight clips from a video.
    Returns list of output file paths. Uses parallel extraction when multiple clips.
    """
    if config is None:
        config = load_config()

    clip_cfg = config.get("clip", {})
    perf_cfg = config.get("performance", {})
    out_dir = output_dir or clip_cfg.get("output_dir", "outputs")
    aspect = clip_cfg.get("aspect_ratio", "9:16")
    crf = clip_cfg.get("crf", 18)
    preset = clip_cfg.get("preset", "medium")
    parallel_workers = perf_cfg.get("extract_parallel_workers", 2)

    ffmpeg_path, _ = _get_ffmpeg_bin(config)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    video_encoder = None
    if perf_cfg.get("use_hw_encoder", "auto") in ("auto", True):
        video_encoder = _detect_hw_encoder(ffmpeg_path)
        if video_encoder:
            _log(log, "  Using NVENC hardware encoder for faster extraction")

    video_stem = Path(video_path).stem
    if base_name:
        video_stem = base_name

    output_paths = [None] * len(highlights)

    tasks = []
    for i, h in enumerate(highlights):
        out_name = f"{video_stem}_clip_{i+1:02d}.mp4"
        out_path = str(Path(out_dir) / out_name)
        out_file = Path(out_path)

        if out_file.exists():
            _log(log, f"  Skipping clip {i+1}/{len(highlights)} (already exists): {out_name}")
            output_paths[i] = out_path
            continue

        tasks.append((i, h, out_path))

    to_extract = [(i, h, p) for (i, h, p) in tasks if output_paths[i] is None]
    if not to_extract:
        return [p for p in output_paths if p]

    def _extract_one(args):
        idx, hl, path = args
        enc = video_encoder
        ok = extract_clip(
            video_path, hl["start"], hl["end"], path,
            aspect, ffmpeg_path, crf, preset, enc, log=log
        )
        if not ok and enc == "h264_nvenc":
            _log(log, "  NVENC failed, retrying with software encoder...")
            ok = extract_clip(
                video_path, hl["start"], hl["end"], path,
                aspect, ffmpeg_path, crf, preset, None, log=log
            )
        return idx, path, ok

    start_time = time.time()
    _log(log, f"  Extracting {len(to_extract)} clip(s)...")

    if len(to_extract) > 1 and parallel_workers > 1:
        with ThreadPoolExecutor(max_workers=min(parallel_workers, len(to_extract))) as ex:
            futures = {ex.submit(_extract_one, t): t for t in to_extract}
            for fut in as_completed(futures):
                idx, path, ok = fut.result()
                output_paths[idx] = path if ok else None
                _log(log, f"    -> {Path(path).name}" if ok else f"    -> Failed: {Path(path).name}")
    else:
        for idx, h, out_path in to_extract:
            _log(log, f"  Extracting clip: {h['start']:.1f}s - {h['end']:.1f}s")
            ok = extract_clip(
                video_path, h["start"], h["end"], out_path,
                aspect, ffmpeg_path, crf, preset, video_encoder, log=log
            )
            if not ok and video_encoder == "h264_nvenc":
                _log(log, "  NVENC failed, retrying with software encoder...")
                ok = extract_clip(
                    video_path, h["start"], h["end"], out_path,
                    aspect, ffmpeg_path, crf, preset, None, log=log
                )
            output_paths[idx] = out_path if ok else None
            _log(log, f"    -> {Path(out_path).name}" if ok else "    -> Failed")

    elapsed = _format_elapsed(time.time() - start_time)
    _log(log, f"  Extraction done in {elapsed}")

    return [p for p in output_paths if p]
