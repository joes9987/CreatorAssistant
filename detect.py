"""
AI-based highlight detection for League of Legends gameplay videos.
Uses audio energy analysis + visual motion detection to find exciting moments.
Supports Riot Live Client Data API events (kills) for accurate clip extraction.
"""

import hashlib
import json
import subprocess
import tempfile
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from app_paths import project_root

import cv2
import numpy as np
import librosa
import yaml


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
    log: Callable[[str], None] | None = None,
) -> np.ndarray:
    """
    Sample frames and compute frame-to-frame difference (motion).
    Uses sequential grab/read: grab() skips frames without decoding, read() only
    decodes sampled frames. 5-10x faster than per-frame seeking on MP4 files.
    High motion = action, team fights, etc.
    Returns array of motion scores per window.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return np.zeros(int(duration / window_seconds) + 1)

    total_frames = int(duration * fps)
    step = max(1, int(fps * sample_interval_sec))
    expected_samples = total_frames // step
    if expected_samples < 2:
        cap.release()
        return np.array([0.0])

    prev_gray = None
    motions = []
    frames_grabbed = 0
    log_interval = max(1, expected_samples // 10)

    for sample_i in range(expected_samples):
        if sample_i == 0:
            ret, frame = cap.read()
            frames_grabbed += 1
        else:
            for _ in range(step - 1):
                cap.grab()
            ret, frame = cap.read()
            frames_grabbed += step

        if not ret:
            break

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (resize_width, resize_height))

        if prev_gray is not None:
            motions.append(float(np.mean(cv2.absdiff(prev_gray, gray))))
        prev_gray = gray

        if log and sample_i % log_interval == 0 and sample_i > 0:
            pct = int(sample_i / expected_samples * 100)
            log(f"  Motion analysis {pct}%...")

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
    root = project_root()
    log_dir = Path(ge_cfg.get("log_dir", "eventlogs"))
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    if not log_dir.exists():
        # Fallback: legacy single file
        legacy = ge_cfg.get("file", "game_events.json")
        p = root / Path(legacy).name
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
        p = root / Path(legacy).name
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
        if "#" in player_name:
            player_name = player_name.split("#")[0].strip()
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


def _cache_dir() -> Path:
    d = project_root() / ".highlight_cache"
    d.mkdir(exist_ok=True)
    return d


def _cache_key(video_path: str) -> str:
    """Stable short key from the absolute path."""
    return hashlib.sha1(video_path.encode()).hexdigest()[:16]


def _load_cached_highlights(video_path: str) -> list[dict] | None:
    """Return cached highlights if the video hasn't changed since the cache was written."""
    cache_file = _cache_dir() / f"{_cache_key(video_path)}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        cached_mtime = data.get("video_mtime")
        cached_size = data.get("video_size")
        stat = os.stat(video_path)
        if cached_mtime == stat.st_mtime and cached_size == stat.st_size:
            return data["highlights"]
    except Exception:
        pass
    return None


def _save_cached_highlights(video_path: str, highlights: list[dict]) -> None:
    try:
        stat = os.stat(video_path)
        data = {
            "video_path": video_path,
            "video_mtime": stat.st_mtime,
            "video_size": stat.st_size,
            "highlights": highlights,
        }
        cache_file = _cache_dir() / f"{_cache_key(video_path)}.json"
        cache_file.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def detect_highlights(
    video_path: str,
    config: dict | None = None,
    events_file: str | None = None,
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """
    Detect highlight moments in a video.
    If game events file exists (from game_events_logger.py), uses kill timestamps.
    Otherwise falls back to AI (audio + motion) analysis.
    Returns list of dicts with 'start', 'end' (seconds) and 'score'.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)
        else:
            print(msg)

    if config is None:
        config = load_config()

    video_path = str(Path(video_path).resolve())
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    ge_cfg = config.get("game_events", {})
    if ge_cfg.get("enabled", True):
        if events_file:
            events_path = events_file
            if not Path(events_path).is_absolute():
                script_dir = project_root()
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
                _log("  Using game events (kill timestamps from Live Client Data API)")
                return highlights
            if Path(events_path).exists():
                _log("  Events file found but no matching kills (check filter_my_kills_only / player_summoner_name)")
            else:
                _log("  No matching eventlogs found - using AI detection")

    # Check cache before expensive AI detection
    cached = _load_cached_highlights(video_path)
    if cached is not None:
        _log("  Using cached detection results (video unchanged)")
        return cached

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

    ffmpeg_path, ffprobe_path = _get_ffmpeg_bin(config)
    info = get_video_info(video_path, ffprobe_path)
    duration = info["duration"]
    fps = info["fps"]

    perf_cfg = config.get("performance", {})
    audio_sr = perf_cfg.get("audio_sample_rate", 11025)
    _log("  Extracting audio...")
    audio, sr = extract_audio(video_path, sample_rate=audio_sr, ffmpeg_path=ffmpeg_path)
    audio_energy = compute_audio_energy(audio, sr, window_sec)
    audio_norm = normalize_scores(audio_energy)

    n_windows = max(len(audio_norm), int(duration / window_sec))
    if len(audio_norm) < n_windows:
        audio_norm = np.pad(audio_norm, (0, n_windows - len(audio_norm)), mode="edge")
    audio_norm = audio_norm[:n_windows]

    _log("  Analyzing motion...")
    motion_sample_sec = perf_cfg.get("motion_sample_interval_sec", 2.0)
    motion_resize = perf_cfg.get("motion_resize", [160, 90])
    motion_resize = motion_resize if isinstance(motion_resize, (list, tuple)) else [160, 90]
    motion_scores = compute_motion_scores(
        video_path, duration, fps, window_sec,
        sample_interval_sec=motion_sample_sec,
        resize_width=motion_resize[0] if len(motion_resize) > 0 else 160,
        resize_height=motion_resize[1] if len(motion_resize) > 1 else 90,
        log=_log,
    )
    motion_norm = normalize_scores(motion_scores)
    if len(motion_norm) < n_windows:
        motion_norm = np.pad(motion_norm, (0, n_windows - len(motion_norm)), mode="edge")
    motion_norm = motion_norm[:n_windows]

    combined = audio_weight * audio_norm + motion_weight * motion_norm
    threshold = np.percentile(combined, 100 - (sensitivity * 40))

    candidates = []
    for i in range(1, len(combined) - 1):
        score = combined[i]
        if score < combined[i - 1] or score < combined[i + 1]:
            continue
        if score < threshold or score < min_score:
            continue
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

    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    for c in candidates:
        if len(selected) >= max_clips:
            break
        if any(abs(c["start"] - s["start"]) < min_between for s in selected):
            continue
        selected.append(c)

    selected.sort(key=lambda x: x["start"])

    _save_cached_highlights(video_path, selected)

    return selected
