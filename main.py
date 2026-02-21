"""
CreatorAssistant - AI-powered clip extraction for League of Legends videos.
Detects highlights automatically, extracts clips, converts to 9:16 for Shorts/TikTok/Reels.
"""

import sys
from pathlib import Path

import tkinter as tk
from tkinter import filedialog

from detect import detect_highlights, load_config
from extract import extract_all_clips


def select_clips_to_upload(clip_paths: list[str]) -> list[str]:
    """Show a dialog for the user to select which clips to upload. Returns selected paths."""
    if not clip_paths:
        return []

    root = tk.Tk()
    root.title("Select clips to upload to YouTube")
    root.geometry("500x400")

    vars_list = []
    for i, path in enumerate(clip_paths):
        var = tk.BooleanVar(value=True)
        vars_list.append(var)
        cb = tk.Checkbutton(root, text=Path(path).name, variable=var, anchor="w")
        cb.pack(fill="x", padx=10, pady=2)

    selected = []

    def on_upload():
        nonlocal selected
        selected = [clip_paths[i] for i, v in enumerate(vars_list) if v.get()]
        root.destroy()

    def on_skip():
        nonlocal selected
        selected = []
        root.destroy()

    btn_frame = tk.Frame(root)
    btn_frame.pack(pady=15)
    tk.Button(btn_frame, text="Upload selected", command=on_upload, width=15).pack(side="left", padx=5)
    tk.Button(btn_frame, text="Skip upload", command=on_skip, width=15).pack(side="left", padx=5)

    root.mainloop()
    return selected


def select_video_files():
    """Open a file browser for the user to select video file(s)."""
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    files = filedialog.askopenfilenames(
        title="Select video(s) to clip",
        initialdir=Path(__file__).parent,
        filetypes=[
            ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm"),
            ("MP4", "*.mp4"),
            ("MKV", "*.mkv"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return [Path(f) for f in files] if files else []


def main():
    config_path = Path(__file__).parent / "config.yaml"
    config = load_config(str(config_path))

    if len(sys.argv) >= 2:
        videos = [Path(p) for p in sys.argv[1:] if Path(p).exists()]
    else:
        videos = select_video_files()
        if not videos:
            video_dir = Path(__file__).parent
            videos = list(video_dir.glob("*.mp4")) + list(video_dir.glob("*.mkv"))
            if not videos:
                print("Usage: python main.py <video_path> [video_path2 ...]")
                print("   Or run with no args to browse for a file, or place .mp4/.mkv in this folder.")
                sys.exit(1)

    for video_path in videos:
        video_path = str(video_path.resolve())
        print(f"\nProcessing: {video_path}")

        print("Detecting highlights...")
        highlights = detect_highlights(video_path, config)

        if not highlights:
            print("  No highlights detected. Try increasing 'sensitivity' in config.yaml")
            continue

        print(f"  Found {len(highlights)} potential highlights")
        for i, h in enumerate(highlights):
            print(f"    {i+1}. {h['start']:.1f}s - {h['end']:.1f}s (score: {h['score']:.3f})")

        print("\nExtracting clips...")
        outputs = extract_all_clips(video_path, highlights, config=config)
        print(f"\nDone! {len(outputs)} clips saved to {config['clip']['output_dir']}/")

        if outputs and (config.get("youtube", {}).get("enabled") or config.get("tiktok", {}).get("enabled")):
            to_upload = select_clips_to_upload(outputs)
            if to_upload:
                yt_enabled = config.get("youtube", {}).get("enabled")
                ttk_enabled = config.get("tiktok", {}).get("enabled")
                clip_nums = None
                if yt_enabled and ttk_enabled:
                    base = Path(__file__).parent
                    counter_path = base / "clip_counter.txt"
                    counter_start = config.get("youtube", {}).get("clip_counter_start", 1)
                    try:
                        start = int(counter_path.read_text().strip()) if counter_path.exists() else counter_start
                    except (ValueError, OSError):
                        start = counter_start
                    clip_nums = [start + i for i in range(len(to_upload))]
                if yt_enabled:
                    print(f"\nUploading {len(to_upload)} clips to YouTube Shorts...")
                    try:
                        from youtube_upload import upload_clips
                        uploaded = upload_clips(to_upload, config, clip_nums=clip_nums)
                        if uploaded:
                            print(f"  Uploaded {len(uploaded)} clips to YouTube")
                    except Exception as e:
                        print(f"  YouTube upload failed: {e}")
                if ttk_enabled:
                    print(f"\nUploading {len(to_upload)} clips to TikTok...")
                    try:
                        from tiktok_upload import upload_clips as tiktok_upload_clips
                        uploaded = tiktok_upload_clips(to_upload, config, clip_nums=clip_nums)
                        if uploaded:
                            print(f"  Uploaded {len(uploaded)} clips to TikTok")
                    except Exception as e:
                        print(f"  TikTok upload failed: {e}")
                if clip_nums and (yt_enabled or ttk_enabled):
                    base = Path(__file__).parent
                    (base / "clip_counter.txt").write_text(str(clip_nums[-1] + 1))
            else:
                print("\nSkipped upload (none selected or cancelled)")


if __name__ == "__main__":
    main()
