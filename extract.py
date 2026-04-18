"""
Clip extraction and vertical reframing using FFmpeg.
Produces Shorts/TikTok/Reels-ready clips from detected highlights.
"""

import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from detect import _get_ffmpeg_bin
from timer_utils import emit_log, format_elapsed


def _parse_aspect(spec: str) -> tuple[int, int]:
    """Parse 'W:H' (e.g. 9:16, 10:16) into positive integers."""
    s = spec.strip().lower().replace(" ", "")
    if ":" not in s:
        raise ValueError(f"aspect must look like '9:16', got {spec!r}")
    a, b = s.split(":", 1)
    w, h = int(a), int(b)
    if w <= 0 or h <= 0:
        raise ValueError(f"aspect parts must be positive, got {spec!r}")
    return w, h


def _output_dimensions(aspect_ratio: str, base_width: int = 1080) -> tuple[int, int]:
    """Pixel size for the final frame: fixed width, height from aspect (even dimensions)."""
    w, h = _parse_aspect(aspect_ratio)
    out_w = base_width - (base_width % 2)
    out_h = int(round(out_w * h / w))
    out_h -= out_h % 2
    return out_w, out_h


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


def _build_vertical_filter(
    mode: str,
    out_w: int,
    out_h: int,
    crop_w: int,
    crop_h: int,
) -> str:
    """Build the FFmpeg video filter for vertical output.

    mode="fit" — scale the full frame to fit inside out_w x out_h, pad (full FOV).
    mode="crop" — center-crop source to crop_w:crop_h, then scale-to-fit + pad
                  into out_w x out_h. Wider crop_aspect (e.g. 10:16 vs 9:16) keeps
                  more horizontal UI (minimap) with small top/bottom bars.
    """
    scale_pad = (
        f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p"
    )
    if mode == "fit":
        return scale_pad
    if mode != "crop":
        raise ValueError(f"reframe_mode must be 'fit' or 'crop', got {mode!r}")
    crop = (
        f"crop='min(iw,ih*{crop_w}/{crop_h})':'min(ih,iw*{crop_h}/{crop_w})':"
        f"'max(0,(iw-min(iw,ih*{crop_w}/{crop_h}))/2)':'max(0,(ih-min(ih,iw*{crop_h}/{crop_w}))/2)',"
    )
    return crop + scale_pad


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
    reframe_mode: str = "fit",
    crop_aspect: str = "9:16",
) -> bool:
    """Extract a clip and reframe to aspect_ratio (output frame size). crop_aspect applies when reframe_mode is crop."""
    duration = end_sec - start_sec
    output_path = str(Path(output_path).resolve())

    try:
        out_w, out_h = _output_dimensions(aspect_ratio)
        cw, ch = _parse_aspect(crop_aspect)
        vf = _build_vertical_filter(reframe_mode, out_w, out_h, cw, ch)
    except ValueError as e:
        emit_log(log, f"  Invalid clip aspect settings: {e}")
        return False

    encoder = video_encoder or "libx264"
    if encoder == "h264_nvenc":
        vcodec_args = ["-c:v", "h264_nvenc", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    else:
        vcodec_args = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]

    cmd = (
        [ffmpeg_path, "-y", "-ss", str(start_sec), "-i", video_path, "-t", str(duration)]
        + ["-vf", vf]
        + vcodec_args
        + ["-c:a", "aac", "-b:a", "192k", output_path]
    )

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0 and result.stderr:
            last_lines = result.stderr.strip().split("\n")[-3:]
            emit_log(log, f"  FFmpeg: {' '.join(last_lines)}")
        return result.returncode == 0
    except Exception as e:
        emit_log(log, f"  Error extracting clip: {e}")
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
    crop_asp = clip_cfg.get("crop_aspect", "9:16")
    crf = clip_cfg.get("crf", 18)
    preset = clip_cfg.get("preset", "medium")
    reframe = clip_cfg.get("reframe_mode", "fit")
    parallel_workers = perf_cfg.get("extract_parallel_workers", 2)

    ffmpeg_path, _ = _get_ffmpeg_bin(config)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    video_encoder = None
    if perf_cfg.get("use_hw_encoder", "auto") in ("auto", True):
        video_encoder = _detect_hw_encoder(ffmpeg_path)
        if video_encoder:
            emit_log(log, "  Using NVENC hardware encoder for faster extraction")

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
            emit_log(log, f"  Skipping clip {i+1}/{len(highlights)} (already exists): {out_name}")
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
            aspect, ffmpeg_path, crf, preset, enc, log=log,
            reframe_mode=reframe, crop_aspect=crop_asp,
        )
        if not ok and enc == "h264_nvenc":
            emit_log(log, "  NVENC failed, retrying with software encoder...")
            ok = extract_clip(
                video_path, hl["start"], hl["end"], path,
                aspect, ffmpeg_path, crf, preset, None, log=log,
                reframe_mode=reframe, crop_aspect=crop_asp,
            )
        return idx, path, ok

    start_time = time.time()
    emit_log(log, f"  Extracting {len(to_extract)} clip(s)...")

    if len(to_extract) > 1 and parallel_workers > 1:
        with ThreadPoolExecutor(max_workers=min(parallel_workers, len(to_extract))) as ex:
            futures = {ex.submit(_extract_one, t): t for t in to_extract}
            for fut in as_completed(futures):
                idx, path, ok = fut.result()
                output_paths[idx] = path if ok else None
                emit_log(log, f"    -> {Path(path).name}" if ok else f"    -> Failed: {Path(path).name}")
    else:
        for idx, h, out_path in to_extract:
            emit_log(log, f"  Extracting clip: {h['start']:.1f}s - {h['end']:.1f}s")
            ok = extract_clip(
                video_path, h["start"], h["end"], out_path,
                aspect, ffmpeg_path, crf, preset, video_encoder, log=log,
                reframe_mode=reframe, crop_aspect=crop_asp,
            )
            if not ok and video_encoder == "h264_nvenc":
                emit_log(log, "  NVENC failed, retrying with software encoder...")
                ok = extract_clip(
                    video_path, h["start"], h["end"], out_path,
                    aspect, ffmpeg_path, crf, preset, None, log=log,
                    reframe_mode=reframe, crop_aspect=crop_asp,
                )
            output_paths[idx] = out_path if ok else None
            emit_log(log, f"    -> {Path(out_path).name}" if ok else "    -> Failed")

    elapsed = format_elapsed(time.time() - start_time)
    emit_log(log, f"  Extraction done in {elapsed}")

    return [p for p in output_paths if p]
