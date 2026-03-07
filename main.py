"""
CreatorAssistant - AI-powered clip extraction for League of Legends videos.
Detects highlights automatically, extracts clips, converts to 9:16 for Shorts/TikTok/Reels.
"""

import sys
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

from detect import detect_highlights, load_config
from extract import extract_all_clips

# --- Theme colors (matching style.css) ---
_C_BG = "#0f1419"
_C_CARD = "#1a2332"
_C_TEXT = "#e8eaed"
_C_MUTED = "#9aa0a6"
_C_ACCENT = "#c9a227"
_C_ACCENT_HOVER = "#e4b93d"
_C_BORDER = "#2d3a4d"
_FONT = ("Segoe UI", 10)
_FONT_BOLD = ("Segoe UI", 10, "bold")
_FONT_HEADER = ("Segoe UI", 14, "bold")
_FONT_SMALL = ("Segoe UI", 9)


def _setup_dark_theme(style: ttk.Style):
    """Configure ttk styles to match the dark/gold website theme."""
    style.theme_use("clam")

    style.configure("TFrame", background=_C_BG)
    style.configure("Card.TFrame", background=_C_CARD)

    style.configure("TLabel", background=_C_BG, foreground=_C_TEXT, font=_FONT)
    style.configure("Header.TLabel", background=_C_BG, foreground=_C_TEXT, font=_FONT_HEADER)
    style.configure("Muted.TLabel", background=_C_BG, foreground=_C_MUTED, font=_FONT_SMALL)
    style.configure("CardMuted.TLabel", background=_C_CARD, foreground=_C_MUTED, font=_FONT_SMALL)

    style.configure(
        "TCheckbutton",
        background=_C_CARD,
        foreground=_C_TEXT,
        font=_FONT,
        indicatorbackground=_C_BORDER,
        indicatorforeground=_C_ACCENT,
    )
    style.map(
        "TCheckbutton",
        background=[("active", _C_CARD)],
        indicatorbackground=[("selected", _C_ACCENT)],
    )

    style.configure(
        "Gold.TButton",
        background=_C_ACCENT,
        foreground=_C_BG,
        font=_FONT_BOLD,
        borderwidth=0,
        padding=(16, 8),
    )
    style.map(
        "Gold.TButton",
        background=[("active", _C_ACCENT_HOVER), ("pressed", _C_ACCENT_HOVER)],
    )

    style.configure(
        "Secondary.TButton",
        background=_C_CARD,
        foreground=_C_MUTED,
        font=_FONT,
        borderwidth=1,
        bordercolor=_C_BORDER,
        padding=(16, 8),
    )
    style.map(
        "Secondary.TButton",
        background=[("active", _C_BORDER)],
        foreground=[("active", _C_TEXT)],
    )

    style.configure(
        "Small.TButton",
        background=_C_BG,
        foreground=_C_MUTED,
        font=_FONT_SMALL,
        borderwidth=1,
        bordercolor=_C_BORDER,
        padding=(8, 4),
    )
    style.map(
        "Small.TButton",
        background=[("active", _C_CARD)],
        foreground=[("active", _C_TEXT)],
    )


def _format_duration(seconds: float) -> str:
    """Format seconds as M:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _load_icon(root: tk.Tk):
    """Try to set the window icon from assets. Fails silently if missing."""
    try:
        from PIL import Image, ImageTk
        icon_path = Path(__file__).parent / "assets" / "CreatorAssistant-icon.png"
        if not icon_path.exists():
            return
        img = Image.open(icon_path).resize((32, 32), Image.LANCZOS)
        photo = ImageTk.PhotoImage(img)
        root.iconphoto(True, photo)
        root._icon_ref = photo  # prevent garbage collection
    except Exception:
        pass


def select_clips_to_upload(
    clip_paths: list[str],
    durations: list[float] | None = None,
) -> list[str]:
    """Show a themed dialog for the user to select which clips to upload. Returns selected paths."""
    if not clip_paths:
        return []

    root = tk.Tk()
    root.title("CreatorAssistant")
    root.configure(bg=_C_BG)
    root.minsize(520, 400)
    root.resizable(True, True)

    _load_icon(root)

    style = ttk.Style(root)
    _setup_dark_theme(style)

    # Center on screen
    root.update_idletasks()
    w, h = 540, max(420, 160 + len(clip_paths) * 36)
    h = min(h, 600)
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 2
    root.geometry(f"{w}x{h}+{x}+{y}")

    # --- Header ---
    header_frame = ttk.Frame(root)
    header_frame.pack(fill="x", padx=24, pady=(20, 0))

    ttk.Label(header_frame, text="Select clips to upload", style="Header.TLabel").pack(anchor="w")
    ttk.Label(
        header_frame,
        text=f"{len(clip_paths)} clip{'s' if len(clip_paths) != 1 else ''} detected",
        style="Muted.TLabel",
    ).pack(anchor="w", pady=(2, 0))

    # --- Select All / Deselect All ---
    controls_frame = ttk.Frame(root)
    controls_frame.pack(fill="x", padx=24, pady=(12, 0))

    vars_list: list[tk.BooleanVar] = []
    for _ in clip_paths:
        vars_list.append(tk.BooleanVar(value=True))

    def _select_all():
        for v in vars_list:
            v.set(True)

    def _deselect_all():
        for v in vars_list:
            v.set(False)

    ttk.Button(controls_frame, text="Select All", style="Small.TButton", command=_select_all).pack(side="left", padx=(0, 6))
    ttk.Button(controls_frame, text="Deselect All", style="Small.TButton", command=_deselect_all).pack(side="left")

    # --- Scrollable clip list ---
    list_outer = ttk.Frame(root)
    list_outer.pack(fill="both", expand=True, padx=24, pady=(10, 0))

    canvas = tk.Canvas(
        list_outer,
        bg=_C_CARD,
        highlightthickness=1,
        highlightbackground=_C_BORDER,
        bd=0,
    )
    scrollbar = ttk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
    inner_frame = ttk.Frame(canvas, style="Card.TFrame")

    inner_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.create_window((0, 0), window=inner_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    # Mouse wheel scrolling
    def _on_mousewheel(event):
        canvas.yview_scroll(-1 * (event.delta // 120), "units")

    canvas.bind_all("<MouseWheel>", _on_mousewheel)

    for i, path in enumerate(clip_paths):
        row = ttk.Frame(inner_frame, style="Card.TFrame")
        row.pack(fill="x", padx=12, pady=4)

        cb = ttk.Checkbutton(row, text=Path(path).name, variable=vars_list[i])
        cb.pack(side="left", anchor="w")

        if durations and i < len(durations):
            ttk.Label(row, text=_format_duration(durations[i]), style="CardMuted.TLabel").pack(
                side="right", padx=(0, 8)
            )

    # --- Action buttons ---
    selected: list[str] = []

    def on_upload():
        nonlocal selected
        selected = [clip_paths[i] for i, v in enumerate(vars_list) if v.get()]
        canvas.unbind_all("<MouseWheel>")
        root.destroy()

    def on_skip():
        nonlocal selected
        selected = []
        canvas.unbind_all("<MouseWheel>")
        root.destroy()

    btn_frame = ttk.Frame(root)
    btn_frame.pack(fill="x", padx=24, pady=(16, 20))

    ttk.Button(btn_frame, text="Upload Selected", style="Gold.TButton", command=on_upload).pack(side="left", padx=(0, 10))
    ttk.Button(btn_frame, text="Skip Upload", style="Secondary.TButton", command=on_skip).pack(side="left")

    root.protocol("WM_DELETE_WINDOW", on_skip)
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
            durations = [h["end"] - h["start"] for h in highlights]
            to_upload = select_clips_to_upload(outputs, durations=durations)
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
