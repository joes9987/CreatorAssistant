"""Core clip workflow: detect → extract → optional uploads. Used by CLI and GUI."""

from collections.abc import Callable
from pathlib import Path

from app_paths import project_root
from detect import detect_highlights, load_config
from extract import extract_all_clips
from ui_dialogs import select_clips_to_upload


def _log(log: Callable[[str], None] | None, msg: str) -> None:
    if log:
        log(msg)
    else:
        print(msg)


def clip_nums_for_upload_count(config: dict, num_selected: int) -> list[int] | None:
    """Multi-platform sync: return sequential clip numbers, or None if only one platform."""
    yt_enabled = config.get("youtube", {}).get("enabled")
    ttk_enabled = config.get("tiktok", {}).get("enabled")
    ig_enabled = config.get("instagram", {}).get("enabled")
    if sum(bool(x) for x in [yt_enabled, ttk_enabled, ig_enabled]) < 2:
        return None
    counter_path = project_root() / "clip_counter.txt"
    counter_start = config.get("youtube", {}).get("clip_counter_start", 1)
    try:
        start = int(counter_path.read_text().strip()) if counter_path.exists() else counter_start
    except (ValueError, OSError):
        start = counter_start
    return [start + i for i in range(num_selected)]


def process_one_video(
    video_path: str | Path,
    config: dict,
    log: Callable[[str], None] | None = None,
) -> list[str]:
    """
    Detect highlights and extract clips for one file.
    Returns list of output clip paths (strings); empty if none.
    """
    video_path = str(Path(video_path).resolve())
    _log(log, f"\nProcessing: {video_path}")

    _log(log, "Detecting highlights...")
    highlights = detect_highlights(video_path, config, log=log)

    if not highlights:
        _log(log, "  No highlights detected. Try increasing 'sensitivity' in config.yaml")
        return []

    _log(log, f"  Found {len(highlights)} potential highlights")
    for i, h in enumerate(highlights):
        _log(log, f"    {i+1}. {h['start']:.1f}s - {h['end']:.1f}s (score: {h['score']:.3f})")

    _log(log, "\nExtracting clips...")
    outputs = extract_all_clips(video_path, highlights, config=config)
    _log(log, f"\nDone! {len(outputs)} clips saved to {config['clip']['output_dir']}/")
    return outputs


def run_uploads(
    to_upload: list[str],
    config: dict,
    clip_nums: list[int] | None,
    log: Callable[[str], None] | None,
) -> None:
    """Run YouTube / TikTok / Instagram uploads for selected clip paths."""
    yt_enabled = config.get("youtube", {}).get("enabled")
    ttk_enabled = config.get("tiktok", {}).get("enabled")
    ig_enabled = config.get("instagram", {}).get("enabled")

    if yt_enabled:
        _log(log, f"\nUploading {len(to_upload)} clips to YouTube Shorts...")
        try:
            from youtube_upload import upload_clips

            uploaded, _ = upload_clips(to_upload, config, clip_nums=clip_nums)
            if uploaded:
                _log(log, f"  Uploaded {len(uploaded)} clips to YouTube")
        except Exception as e:
            _log(log, f"  YouTube upload failed: {e}")

    if ttk_enabled:
        _log(log, f"\nUploading {len(to_upload)} clips to TikTok...")
        try:
            from tiktok_upload import upload_clips as tiktok_upload_clips

            uploaded, _ = tiktok_upload_clips(to_upload, config, clip_nums=clip_nums)
            if uploaded:
                _log(log, f"  Uploaded {len(uploaded)} clips to TikTok")
        except Exception as e:
            _log(log, f"  TikTok upload failed: {e}")

    if ig_enabled:
        _log(log, f"\nUploading {len(to_upload)} clips to Instagram Reels...")
        try:
            from instagram_upload import upload_clips as instagram_upload_clips

            uploaded = instagram_upload_clips(to_upload, config, clip_nums=clip_nums)
            if uploaded:
                _log(log, f"  Uploaded {len(uploaded)} clips to Instagram")
        except Exception as e:
            _log(log, f"  Instagram upload failed: {e}")

    if clip_nums and (yt_enabled or ttk_enabled or ig_enabled):
        (project_root() / "clip_counter.txt").write_text(str(clip_nums[-1] + 1))


def process_videos(
    videos: list[Path],
    config: dict,
    log: Callable[[str], None] | None = None,
    upload_selector: Callable[[list[str]], list[str]] | None = None,
) -> None:
    """
    Full workflow for each video: detect highlights, extract clips, optional upload dialog.
    upload_selector: if None, uses select_clips_to_upload (blocking Tk on main thread).
    """
    selector = upload_selector or select_clips_to_upload

    for video_path in videos:
        outputs = process_one_video(video_path, config, log)
        if outputs and (
            config.get("youtube", {}).get("enabled")
            or config.get("tiktok", {}).get("enabled")
            or config.get("instagram", {}).get("enabled")
        ):
            to_upload = selector(outputs)
            if to_upload:
                clip_nums = clip_nums_for_upload_count(config, len(to_upload))
                run_uploads(to_upload, config, clip_nums, log)
            else:
                _log(log, "\nSkipped upload (none selected or cancelled)")
