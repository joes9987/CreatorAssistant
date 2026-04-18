"""
CreatorAssistant main workflow — modern dark UI.
Browse recordings, detect highlights, extract clips, optional uploads.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from app_paths import project_root
from detect import load_config
from gui.settings_store import get_resolved_default_input_dir, load_gui_settings, save_gui_settings
from pipeline import clip_nums_for_upload_count, process_one_video, run_uploads
from ui_dialogs import select_clips_to_upload

ACCENT = "#1DB954"
ACCENT_HOVER = "#1ed760"
BG_DARK = "#1a1a2e"
BG_CARD = "#16213e"
FG = "#e0e0e0"
FG_DIM = "#8a8a9a"
FONT_FAMILY = "Segoe UI"


class _UploadGate:
    """Worker thread waits on the gate; main thread shows the clip dialog, then uploads run in a background thread so the UI and event loop stay alive (required for OAuth and long uploads)."""

    def __init__(self, parent: ctk.CTk) -> None:
        self._parent = parent
        self._q: queue.Queue[tuple[list[str], dict]] = queue.Queue()
        self._done = threading.Event()

    def request(self, outputs: list[str], config: dict) -> None:
        self._done.clear()
        self._q.put((outputs, config))
        self._done.wait()

    def drain_if_pending(self, thread_log_fn) -> bool:
        try:
            outputs, config = self._q.get_nowait()
        except queue.Empty:
            return False

        to_upload = select_clips_to_upload(outputs, parent=self._parent)
        if to_upload:
            clip_nums = clip_nums_for_upload_count(config, len(to_upload))

            def _upload_worker() -> None:
                try:
                    run_uploads(to_upload, config, clip_nums, thread_log_fn)
                except Exception as exc:
                    thread_log_fn(f"\nUpload error: {exc}")
                finally:
                    self._done.set()

            threading.Thread(target=_upload_worker, daemon=True).start()
        else:
            thread_log_fn("\nSkipped upload (none selected or cancelled)")
            self._done.set()
        return True


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("green")

    app = ctk.CTk()
    app.title("CreatorAssistant")
    app.geometry("780x620")
    app.minsize(640, 500)
    app.configure(fg_color=BG_DARK)

    config_path = project_root() / "config.yaml"
    if not config_path.exists():
        messagebox.showerror(
            "Missing config",
            f"Copy config.example.yaml to config.yaml next to the app.\n\nExpected: {config_path}",
        )
        app.destroy()
        return

    gate = _UploadGate(app)
    log_q: queue.Queue[str] = queue.Queue()
    worker_running = threading.Event()

    # ── Header ──
    header = ctk.CTkFrame(app, fg_color="transparent", height=48)
    header.pack(fill="x", padx=20, pady=(16, 0))
    ctk.CTkLabel(
        header, text="CreatorAssistant", font=(FONT_FAMILY, 22, "bold"),
        text_color=ACCENT,
    ).pack(side="left")
    ctk.CTkLabel(
        header, text="clip workflow", font=(FONT_FAMILY, 13),
        text_color=FG_DIM,
    ).pack(side="left", padx=(10, 0), pady=(6, 0))

    # ── Folder picker ──
    folder_frame = ctk.CTkFrame(app, fg_color=BG_CARD, corner_radius=12)
    folder_frame.pack(fill="x", padx=20, pady=(12, 0))

    ctk.CTkLabel(
        folder_frame, text="Recordings folder", font=(FONT_FAMILY, 12, "bold"),
        text_color=FG_DIM,
    ).pack(anchor="w", padx=16, pady=(12, 4))

    path_row = ctk.CTkFrame(folder_frame, fg_color="transparent")
    path_row.pack(fill="x", padx=16, pady=(0, 6))

    folder_var = tk.StringVar(value=str(get_resolved_default_input_dir()))
    ent = ctk.CTkEntry(
        path_row, textvariable=folder_var, height=36,
        font=(FONT_FAMILY, 12), border_width=0,
    )
    ent.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def browse_folder() -> None:
        initial = folder_var.get().strip() or str(get_resolved_default_input_dir())
        d = filedialog.askdirectory(initialdir=initial, title="Select recordings folder")
        if d:
            folder_var.set(d)
            refresh_list()

    ctk.CTkButton(
        path_row, text="Browse", width=90, height=36, command=browse_folder,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#000",
        font=(FONT_FAMILY, 12, "bold"), corner_radius=8,
    ).pack(side="right")

    util_row = ctk.CTkFrame(folder_frame, fg_color="transparent")
    util_row.pack(fill="x", padx=16, pady=(0, 12))

    def save_default_folder() -> None:
        d = folder_var.get().strip()
        if not d or not Path(d).is_dir():
            messagebox.showwarning("Invalid folder", "Choose a valid folder first.")
            return
        data = load_gui_settings()
        data["default_input_dir"] = str(Path(d).resolve())
        save_gui_settings(data)
        messagebox.showinfo("Saved", "Default recordings folder saved for next launch.")

    ctk.CTkButton(
        util_row, text="Save as default", width=130, height=30,
        fg_color="transparent", border_width=1, border_color=FG_DIM,
        text_color=FG_DIM, hover_color="#2a2a4a", font=(FONT_FAMILY, 11),
        corner_radius=6, command=save_default_folder,
    ).pack(side="left", padx=(0, 8))

    ctk.CTkButton(
        util_row, text="Refresh list", width=110, height=30,
        fg_color="transparent", border_width=1, border_color=FG_DIM,
        text_color=FG_DIM, hover_color="#2a2a4a", font=(FONT_FAMILY, 11),
        corner_radius=6, command=lambda: refresh_list(),
    ).pack(side="left")

    # ── Video list ──
    list_frame = ctk.CTkFrame(app, fg_color=BG_CARD, corner_radius=12)
    list_frame.pack(fill="both", expand=True, padx=20, pady=(10, 0))

    ctk.CTkLabel(
        list_frame, text="Videos", font=(FONT_FAMILY, 12, "bold"),
        text_color=FG_DIM,
    ).pack(anchor="w", padx=16, pady=(12, 4))

    lb = tk.Listbox(
        list_frame, selectmode=tk.EXTENDED, height=8,
        bg="#0f0f23", fg=FG, selectbackground=ACCENT, selectforeground="#000",
        font=(FONT_FAMILY, 11), bd=0, highlightthickness=0,
        activestyle="none",
    )
    lb.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    def refresh_list() -> None:
        lb.delete(0, tk.END)
        d = Path(folder_var.get().strip())
        if not d.is_dir():
            append_log(f"(Not a folder: {d})")
            return
        exts = ("*.mp4", "*.mkv", "*.mov", "*.webm")
        vids: list[Path] = []
        for ext in exts:
            vids.extend(sorted(d.glob(ext)))
        for p in vids:
            lb.insert(tk.END, str(p))

    # ── Action buttons ──
    action_row = ctk.CTkFrame(app, fg_color="transparent")
    action_row.pack(fill="x", padx=20, pady=(10, 0))

    proc_btn = ctk.CTkButton(
        action_row, text="Process selected", height=40,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#000",
        font=(FONT_FAMILY, 13, "bold"), corner_radius=10,
    )
    proc_btn.pack(side="left", padx=(0, 8))

    proc_all_btn = ctk.CTkButton(
        action_row, text="Process all in folder", height=40,
        fg_color="#2a2a4a", hover_color="#3a3a5e", text_color=FG,
        font=(FONT_FAMILY, 13, "bold"), corner_radius=10,
    )
    proc_all_btn.pack(side="left", padx=(0, 8))

    upload_only_btn = ctk.CTkButton(
        action_row, text="Upload files (skip clipping)", height=40,
        fg_color="transparent", border_width=1, border_color=FG_DIM,
        text_color=FG_DIM, hover_color="#2a2a4a",
        font=(FONT_FAMILY, 13), corner_radius=10,
    )
    upload_only_btn.pack(side="left")

    # ── Log area ──
    log_frame = ctk.CTkFrame(app, fg_color=BG_CARD, corner_radius=12)
    log_frame.pack(fill="both", expand=True, padx=20, pady=(10, 16))

    ctk.CTkLabel(
        log_frame, text="Log", font=(FONT_FAMILY, 12, "bold"),
        text_color=FG_DIM,
    ).pack(anchor="w", padx=16, pady=(12, 4))

    log = ctk.CTkTextbox(
        log_frame, height=120, font=(FONT_FAMILY, 11),
        fg_color="#0f0f23", text_color=FG, corner_radius=8,
        border_width=0, wrap="word",
    )
    log.pack(fill="both", expand=True, padx=16, pady=(0, 12))

    def append_log(msg: str) -> None:
        log.insert(tk.END, msg + "\n")
        log.see(tk.END)
        app.update_idletasks()

    def thread_log(msg: str) -> None:
        log_q.put(msg)

    def poll_ui() -> None:
        try:
            while True:
                msg = log_q.get_nowait()
                append_log(msg)
        except queue.Empty:
            pass
        while gate.drain_if_pending(thread_log):
            pass
        if worker_running.is_set():
            app.after(120, poll_ui)
        else:
            proc_btn.configure(state="normal")
            proc_all_btn.configure(state="normal")
            upload_only_btn.configure(state="normal")

    def run_worker(paths: list[Path]) -> None:
        def worker() -> None:
            try:
                cfg = load_config(str(config_path))
                uploads_enabled = bool(
                    cfg.get("youtube", {}).get("enabled")
                    or cfg.get("tiktok", {}).get("enabled")
                    or cfg.get("instagram", {}).get("enabled")
                )
                for v in paths:
                    outputs = process_one_video(v, cfg, log=thread_log)
                    if outputs and uploads_enabled:
                        gate.request(outputs, cfg)
            except Exception as e:
                thread_log(f"\nError: {e}")
            finally:
                thread_log("\n--- Done ---")
                worker_running.clear()

        worker_running.set()
        proc_btn.configure(state="disabled")
        proc_all_btn.configure(state="disabled")
        upload_only_btn.configure(state="disabled")
        threading.Thread(target=worker, daemon=True).start()
        poll_ui()

    def process_selected() -> None:
        sel = lb.curselection()
        if not sel:
            messagebox.showinfo("Nothing selected", "Select one or more videos in the list.")
            return
        paths = [Path(lb.get(i)) for i in sel]
        log.delete("1.0", tk.END)
        run_worker(paths)

    def process_all() -> None:
        if lb.size() == 0:
            messagebox.showinfo("No videos", "Refresh the list or pick a folder with videos.")
            return
        paths = [Path(lb.get(i)) for i in range(lb.size())]
        log.delete("1.0", tk.END)
        run_worker(paths)

    def upload_files_only() -> None:
        initial = folder_var.get().strip() or str(get_resolved_default_input_dir())
        files = filedialog.askopenfilenames(
            title="Select files to upload (any video / clip)",
            initialdir=initial,
            filetypes=[
                ("Video files", "*.mp4 *.mkv *.avi *.mov *.webm"),
                ("All files", "*.*"),
            ],
        )
        if not files:
            return
        file_list = [str(Path(f).resolve()) for f in files]
        log.delete("1.0", tk.END)
        append_log(f"Selected {len(file_list)} file(s) for upload (no clipping):")
        for f in file_list:
            append_log(f"  {Path(f).name}")
        cfg = load_config(str(config_path))
        to_upload = select_clips_to_upload(file_list, parent=app)
        if to_upload:
            clip_nums = clip_nums_for_upload_count(cfg, len(to_upload))

            def _upload_only_worker() -> None:
                try:
                    run_uploads(to_upload, cfg, clip_nums, thread_log)
                except Exception as exc:
                    thread_log(f"\nUpload error: {exc}")
                finally:
                    thread_log("\n--- Upload complete ---")
                    worker_running.clear()

            worker_running.set()
            proc_btn.configure(state="disabled")
            proc_all_btn.configure(state="disabled")
            upload_only_btn.configure(state="disabled")
            threading.Thread(target=_upload_only_worker, daemon=True).start()
            poll_ui()
        else:
            append_log("\nSkipped upload (none selected or cancelled)")

    proc_btn.configure(command=process_selected)
    proc_all_btn.configure(command=process_all)
    upload_only_btn.configure(command=upload_files_only)

    refresh_list()
    app.mainloop()


if __name__ == "__main__":
    main()
