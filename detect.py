"""
AI-based highlight detection for League of Legends gameplay videos.
Uses audio energy analysis + visual motion detection to find exciting moments.
Supports Riot Live Client Data API events (kills) for accurate clip extraction.
"""

import json
import subprocess
import tempfile
import os
import shutil
from pathlib import Path

import cv2
import numpy as np
import librosa
import soundfile as sf
import yaml

from timer_utils import iter_with_timer


def _get_ffmpeg_bin(config: dict | None = None) -> tuple[str, str]:
    """Get paths to ffmpeg and ffprobe executables. Returns (ffmpeg_path, ffprobe_path)."""
    cfg_path = (config or {}).get("ffmpeg_path", "").strip()
    if cfg_path:
        base = Path(cfg_path)
        fmpeg = str(base / "ffmpeg.exe") if os.name == "nt" else str(base / "ffmpeg")
        fprobe = str(base / "ffprobe.exe") if os.name == "nt" else str(base / "ffprobe")
        if base.exists():
            return fmpeg, fprobe

    # Check PATH
    fmpeg = shutil.which("ffmpeg")
    fprobe = shutil.which("ffprobe")
    if fmpeg and fprobe:
        return fmpeg, fprobe

    # Common Windows install locations
    for folder in [
        Path(os.environ.get("LOCALAPPDATA", "")) / "ffmpeg" / "bin",
        Path("C:/ffmpeg/bin"),
        Path("C:/ffmpeg-essentials/bin"),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg" / "bin",
    ]:
        if not folder.exists():
            continue
        fmpeg = str(folder / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
        fprobe = str(folder / ("ffprobe.exe" if os.name == "nt" else "ffprobe"))
        if os.path.exists(fmpeg) and os.path.exists(fprobe):
            return fmpeg, fprobe

    raise FileNotFoundError(
        "FFmpeg not found. Add it to PATH, or set ffmpeg_path in config.yaml to the bin folder (e.g. C:\\ffmpeg\\bin)"
    )


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def extract_audio(video_path: str, sample_rate: int = 22050, ffmpeg_path: str = "ffmpeg") -> tuple[np.ndarray, int]:
    """
    Extract audio from video using FFmpeg and load with librosa.
    Returns (audio_array, sample_rate).
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        subprocess.run(
            [
                ffmpeg_path, "-y", "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", str(sample_rate), "-ac", "1",
                tmp_path
            ],
            capture_output=True,
            check=True,
        )
        audio, sr = librosa.load(tmp_path, sr=sample_rate, mono=True)
        return audio, sr
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_video_info(video_path: str, ffprobe_path: str = "ffprobe") -> dict:
    """Get video duration and FPS using FFmpeg."""
    # Get duration from format (most reliable)
    result = subprocess.run(
        [
            ffprobe_path, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ],
        capture_output=True,
        text=True,
    )
    duration = float(result.stdout.strip()) if result.stdout.strip() else 0.0

    # Get FPS
    result = subprocess.run(
        [
            ffprobe_path, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ],
        capture_output=True,
        text=True,
    )
    fps_str = result.stdout.strip()
    if "/" in fps_str:
        parts = fps_str.split("/")
        num, den = int(parts[0]), int(parts[1]) if len(parts) > 1 else 1
        fps = num / den if den else 30
    else:
        fps = float(fps_str) if fps_str else 30

    return {"duration": duration, "fps": fps}


def compute_audio_energy(audio: np.ndarray, sr: int, window_seconds: float = 5.0) -> np.ndarray:
    """
    Compute rolling RMS energy. High energy = likely action (team fights, kills, etc.).
    Returns array of energy values, one per window.
    """
    hop_length = int(sr * 0.5)  # 0.5 sec hops
    rms = librosa.feature.rms(y=audio, hop_length=hop_length)[0]

    # Resample to ~1 value per window_seconds
    n_windows = max(1, len(rms) // int(window_seconds * 2))  # 2 hops per sec
    if n_windows >= len(rms):
        return rms
    rms_downsampled = np.array([
        np.mean(rms[i * len(rms) // n_windows:(i + 1) * len(rms) // n_windows])
        for i in range(n_windows)
    ])
    return rms_downsampled


def compute_motion_scores(
    video_path: str,
    duration: float,
    fps: float,
    window_seconds: float = 5.0,
    sample_interval_sec: float = 1.0,
    resize_width: int = 128,
    resize_height: int = 72,
) -> np.ndarray:
    """
    Sample frames and compute frame-to-frame difference (motion).
    Uses seek-based sampling: only reads needed frames (much faster than sequential read).
    High motion = action, team fights, etc.
    Returns array of motion scores per window.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return np.zeros(int(duration / window_seconds) + 1)

    total_frames = int(duration * fps)
    sample_interval = max(1, int(fps * sample_interval_sec))  # e.g. 1 sample/sec = 30 frames apart at 30fps
    num_samples = total_frames // sample_interval
    if num_samples < 2:
        cap.release()
        return np.array([0.0])

    prev_frame = None
    motions = []
    frame_indices = list(range(0, total_frames, sample_interval))

    for i, frame_idx in enumerate(iter_with_timer(frame_indices, "Analyzing motion")):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (resize_width, resize_height))

        if prev_frame is not None:
            diff = cv2.absdiff(prev_frame, gray)
            motion = np.mean(diff)
            motions.append(motion)
        prev_frame = gray

    cap.release()

    if len(motions) < 2:
        return np.array([0.0])

    motions = np.array(motions)
    n_windows = max(1, int(duration / window_seconds))
    window_size = max(1, len(motions) // n_windows)
    motion_per_window = np.array([
        np.mean(motions[i * window_size:min((i + 1) * window_size, len(motions))])
        for i in range(n_windows)
    ])
    return motion_per_window


def normalize_scores(scores: np.ndarray) -> np.ndarray:
    """Normalize to 0-1 range."""
    if len(scores) == 0 or np.max(scores) == np.min(scores):
        return np.zeros_like(scores)
    return (scores - np.min(scores)) / (np.max(scores) - np.min(scores))


def get_matching_events_path(
    video_path: str,
    config: dict | None = None,
) -> str | None:
    """
    Find the eventlog file that best matches the video.
    Uses video ctime + duration to find events whose wall_clock falls in range.
    Returns path to the best-matching events file, or None.
    """
    if config is None:
        config = {}
    ge_cfg = config.get("game_events", {})
    base = Path(__file__).resolve().parent
    log_dir = Path(ge_cfg.get("log_dir", "eventlogs"))
    if not log_dir.is_absolute():
        log_dir = base / log_dir
    if not log_dir.exists():
        # Fallback: legacy single file
        legacy = ge_cfg.get("file", "game_events.json")
        p = base / Path(legacy).name
        return str(p) if p.exists() else None

    try:
        video_ctime = os.path.getctime(video_path)
    except OSError:
        video_ctime = 0
    ffmpeg_path, ffprobe_path = _get_ffmpeg_bin(config)
    info = get_video_info(video_path, ffprobe_path)
    video_end = video_ctime + info.get("duration", 0)

    best_path = None
    best_count = 0
    for p in sorted(log_dir.glob("events_*.json"), reverse=True):
        try:
            with open(p) as f:
                data = json.load(f)
        except Exception:
            continue
        kills = [e for e in data.get("events", []) if e.get("type") == "ChampionKill"]
        count = 0
        for k in kills:
            wc = k.get("wall_clock")
            if wc is None:
                wc = k.get("game_time", 0) + (data.get("session_start") or 0)
            if video_ctime <= wc <= video_end:
                count += 1
        if count > best_count:
            best_count = count
            best_path = str(p)
    if best_path is None and not list(log_dir.glob("events_*.json")):
        legacy = ge_cfg.get("file", "game_events.json")
        p = base / Path(legacy).name
        if p.exists():
            return str(p)
    return best_path


def load_highlights_from_events(
    events_path: str,
    video_path: str,
    config: dict,
) -> list[dict] | None:
    """
    Load highlights from a game_events.json file (from game_events_logger.py).
    Uses wall_clock timestamps for correct mapping across multiple games in one recording.
    """
    path = Path(events_path)
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        return None

    events = data.get("events", [])
    kills = [e for e in events if e.get("type") == "ChampionKill"]
    if not kills:
        return None

    ge_cfg = config.get("game_events", {})
    clip_cfg = config.get("clip", {})

    # Filter to only kills by the configured player (if enabled)
    if ge_cfg.get("filter_my_kills_only"):
        player_name = (ge_cfg.get("player_summoner_name") or "").strip().lower()
        if player_name:
            filtered = []
            for k in kills:
                killer = (k.get("killer_name") or k.get("data", {}).get("KillerName") or k.get("data", {}).get("killerName") or "").strip().lower()
                if killer == player_name:
                    filtered.append(k)
            kills = filtered
            if not kills:
                return None

    offset = ge_cfg.get("recording_start_offset", 0)
    padding_before = clip_cfg.get("padding_before", 10)
    padding_after = clip_cfg.get("padding_after", 8)
    min_between = config.get("detection", {}).get("min_seconds_between_clips", 120)
    max_clips = config.get("detection", {}).get("max_clips_per_video", 5)

    # Video timestamp = when recording started (file creation). Works for multi-game sessions.
    try:
        video_start_time = os.path.getctime(video_path)
    except OSError:
        video_start_time = 0

    candidates = []
    for k in kills:
        wall_clock = k.get("wall_clock")
        if wall_clock is None:
            wall_clock = k.get("game_time", 0) + (data.get("session_start") or 0)
        video_sec = wall_clock - video_start_time + offset
        start_sec = max(0, video_sec - padding_before)
        end_sec = video_sec + padding_after
        candidates.append({"start": start_sec, "end": end_sec, "score": 1.0, "source": "game_events"})

    # Respect min spacing and max clips
    candidates.sort(key=lambda x: x["start"])
    selected = []
    for c in candidates:
        if len(selected) >= max_clips:
            break
        if any(abs(c["start"] - s["start"]) < min_between for s in selected):
            continue
        selected.append(c)

    return selected


def detect_highlights(
    video_path: str,
    config: dict | None = None,
    events_file: str | None = None,
) -> list[dict]:
    """
    Detect highlight moments in a video.
    If game events file exists (from game_events_logger.py), uses kill timestamps.
    Otherwise falls back to AI (audio + motion) analysis.
    Returns list of dicts with 'start', 'end' (seconds) and 'score'.
    """
    if config is None:
        config = load_config()

    ge_cfg = config.get("game_events", {})
    if ge_cfg.get("enabled", True):
        if events_file:
            events_path = events_file
            if not Path(events_path).is_absolute():
                script_dir = Path(__file__).resolve().parent
                candidates = [script_dir / Path(events_path).name, Path(video_path).parent / Path(events_path).name]
                for p in candidates:
                    if p.exists():
                        events_path = str(p)
                        break
                else:
                    events_path = str(script_dir / Path(events_path).name)
        else:
            events_path = get_matching_events_path(video_path, config)
        if ge_cfg.get("prefer_events_over_ai", True) and events_path:
            highlights = load_highlights_from_events(events_path, video_path, config)
            if highlights:
                print("  Using game events (kill timestamps from Live Client Data API)")
                return highlights
            if Path(events_path).exists():
                print("  Events file found but no matching kills (check filter_my_kills_only / player_summoner_name)")
            else:
                print("  No matching eventlogs found - using AI detection")

    det_cfg = config.get("detection", {})
    clip_cfg = config.get("clip", {})
    audio_weight = det_cfg.get("audio_weight", 0.5)
    motion_weight = det_cfg.get("motion_weight", 0.5)
    sensitivity = det_cfg.get("sensitivity", 0.5)
    min_score = det_cfg.get("min_score", 0.6)
    min_prominence = det_cfg.get("min_prominence", 0.15)
    min_between = det_cfg.get("min_seconds_between_clips", 120)
    max_clips = det_cfg.get("max_clips_per_video", 5)
    window_sec = det_cfg.get("window_seconds", 4)
    padding_before = clip_cfg.get("padding_before", 10)
    padding_after = clip_cfg.get("padding_after", 8)

    video_path = str(Path(video_path).resolve())
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    ffmpeg_path, ffprobe_path = _get_ffmpeg_bin(config)
    info = get_video_info(video_path, ffprobe_path)
    duration = info["duration"]
    fps = info["fps"]

    # Audio analysis (lower sample rate for faster processing; 11025 sufficient for energy detection)
    perf_cfg = config.get("performance", {})
    audio_sr = perf_cfg.get("audio_sample_rate", 11025)
    print("  Extracting audio...")
    audio, sr = extract_audio(video_path, sample_rate=audio_sr, ffmpeg_path=ffmpeg_path)
    audio_energy = compute_audio_energy(audio, sr, window_sec)
    audio_norm = normalize_scores(audio_energy)

    # Pad/trim to match duration
    n_windows = max(len(audio_norm), int(duration / window_sec))
    if len(audio_norm) < n_windows:
        audio_norm = np.pad(audio_norm, (0, n_windows - len(audio_norm)), mode="edge")
    audio_norm = audio_norm[:n_windows]

    # Motion analysis (seek-based: only reads sampled frames for ~10–30x speedup)
    print("  Analyzing motion...")
    motion_sample_sec = perf_cfg.get("motion_sample_interval_sec", 1.0)
    motion_resize = perf_cfg.get("motion_resize", [128, 72])
    motion_resize = motion_resize if isinstance(motion_resize, (list, tuple)) else [128, 72]
    motion_scores = compute_motion_scores(
        video_path, duration, fps, window_sec,
        sample_interval_sec=motion_sample_sec,
        resize_width=motion_resize[0] if len(motion_resize) > 0 else 128,
        resize_height=motion_resize[1] if len(motion_resize) > 1 else 72,
    )
    motion_norm = normalize_scores(motion_scores)
    if len(motion_norm) < n_windows:
        motion_norm = np.pad(motion_norm, (0, n_windows - len(motion_norm)), mode="edge")
    motion_norm = motion_norm[:n_windows]

    # Combined score
    combined = audio_weight * audio_norm + motion_weight * motion_norm
    threshold = np.percentile(combined, 100 - (sensitivity * 40))  # Higher sensitivity = lower threshold

    # Find peaks: local maxima above threshold, with minimum score and prominence
    candidates = []
    for i in range(1, len(combined) - 1):
        score = combined[i]
        # Must be local maximum
        if score < combined[i - 1] or score < combined[i + 1]:
            continue
        # Above threshold and minimum score
        if score < threshold or score < min_score:
            continue
        # Prominence: peak should stand out from neighbors (avoids noise)
        valley = min(combined[i - 1], combined[i + 1])
        if score - valley < min_prominence:
            continue
        peak_sec = i * window_sec
        start_sec = max(0, peak_sec - padding_before)
        end_sec = min(duration, peak_sec + padding_after)
        if end_sec - start_sec >= clip_cfg.get("min_clip_length", 15):
            candidates.append({
                "start": start_sec,
                "end": end_sec,
                "score": float(score),
            })

    # Non-maximum suppression: keep best candidates, respect min_seconds_between_clips
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    for c in candidates:
        if len(selected) >= max_clips:
            break
        # Check if too close to existing
        if any(abs(c["start"] - s["start"]) < min_between for s in selected):
            continue
        selected.append(c)

    selected.sort(key=lambda x: x["start"])
    return selected
