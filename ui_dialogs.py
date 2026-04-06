"""Shared dialogs for CLI and GUI — styled with customtkinter."""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

ACCENT = "#1DB954"
ACCENT_HOVER = "#1ed760"
BG_DARK = "#1a1a2e"
BG_CARD = "#16213e"
FG = "#e0e0e0"
FG_DIM = "#8a8a9a"
FONT_FAMILY = "Segoe UI"


def select_clips_to_upload(clip_paths: list[str], parent: tk.Misc | None = None) -> list[str]:
    """Show a dialog for the user to select which clips to upload. Returns selected paths.

    When parent is the main CTk window, use CTkToplevel so we do not create a second
    root (which breaks the main app's event loop and freezes uploads).
    """
    if not clip_paths:
        return []

    ctk.set_appearance_mode("dark")

    owns_root = parent is None
    if owns_root:
        dialog = ctk.CTk()
    else:
        dialog = ctk.CTkToplevel(parent)

    dialog.title("Upload clips")
    dialog.geometry("520x420")
    dialog.configure(fg_color=BG_DARK)

    if not owns_root:
        dialog.transient(parent)
        dialog.grab_set()
        dialog.focus_force()

    ctk.CTkLabel(
        dialog, text="Select clips to upload",
        font=(FONT_FAMILY, 18, "bold"), text_color=ACCENT,
    ).pack(padx=20, pady=(20, 12))

    scroll = ctk.CTkScrollableFrame(
        dialog, fg_color=BG_CARD, corner_radius=10,
    )
    scroll.pack(fill="both", expand=True, padx=20, pady=(0, 10))

    switches: list[tuple[ctk.CTkSwitch, ctk.StringVar]] = []
    for path in clip_paths:
        var = ctk.StringVar(value="on")
        sw = ctk.CTkSwitch(
            scroll, text=Path(path).name, variable=var, onvalue="on", offvalue="off",
            font=(FONT_FAMILY, 12), text_color=FG,
            button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            progress_color=ACCENT,
        )
        sw.pack(fill="x", padx=12, pady=4)
        switches.append((sw, var))

    selected: list[str] = []

    def _safe_destroy():
        try:
            for after_id in dialog.tk.call("after", "info"):
                dialog.after_cancel(after_id)
        except Exception:
            pass
        dialog.destroy()

    def on_upload():
        nonlocal selected
        selected = [clip_paths[i] for i, (_, v) in enumerate(switches) if v.get() == "on"]
        _safe_destroy()

    def on_skip():
        nonlocal selected
        selected = []
        _safe_destroy()

    btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
    btn_frame.pack(pady=(0, 18))
    ctk.CTkButton(
        btn_frame, text="Upload selected", width=150, height=38,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#000",
        font=(FONT_FAMILY, 13, "bold"), corner_radius=8, command=on_upload,
    ).pack(side="left", padx=6)
    ctk.CTkButton(
        btn_frame, text="Skip upload", width=130, height=38,
        fg_color="transparent", border_width=1, border_color=FG_DIM,
        text_color=FG_DIM, hover_color="#2a2a4a",
        font=(FONT_FAMILY, 13), corner_radius=8, command=on_skip,
    ).pack(side="left", padx=6)

    dialog.protocol("WM_DELETE_WINDOW", on_skip)

    if owns_root:
        dialog.mainloop()
        try:
            dialog.quit()
        except Exception:
            pass
    else:
        dialog.wait_window()

    return selected


def select_video_files(initial_dir: str | Path | None = None) -> list[Path]:
    """Open a native file browser for the user to select video file(s).

    Uses a plain tk.Tk root (hidden) instead of ctk.CTk so we don't create a
    CustomTkinter root with internal ``after`` callbacks that fire after destroy.
    """
    from app_paths import project_root

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    init = str(Path(initial_dir).resolve()) if initial_dir else str(project_root())
    files = filedialog.askopenfilenames(
        title="Select video(s) to clip",
        initialdir=init,
        filetypes=[
            ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm"),
            ("MP4", "*.mp4"),
            ("MKV", "*.mkv"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return [Path(f) for f in files] if files else []
