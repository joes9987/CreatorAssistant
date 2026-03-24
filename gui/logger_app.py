"""
Game events logger — modern dark UI with Start / Stop and save.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk

import customtkinter as ctk

from game_events_logger import run_session
from gui.settings_store import load_gui_settings, save_gui_settings

ACCENT = "#1DB954"
ACCENT_HOVER = "#1ed760"
BG_DARK = "#1a1a2e"
BG_CARD = "#16213e"
FG = "#e0e0e0"
FG_DIM = "#8a8a9a"
DANGER = "#e74c3c"
DANGER_HOVER = "#ff6b6b"
FONT_FAMILY = "Segoe UI"


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("green")

    app = ctk.CTk()
    app.title("CreatorAssistant — Game Logger")
    app.geometry("560x530")
    app.minsize(480, 460)
    app.configure(fg_color=BG_DARK)

    stop_event = threading.Event()
    log_q: queue.Queue[str | None] = queue.Queue()
    worker_running = threading.Event()

    # ── Header ──
    header = ctk.CTkFrame(app, fg_color="transparent", height=48)
    header.pack(fill="x", padx=20, pady=(16, 0))
    ctk.CTkLabel(
        header, text="CreatorAssistant", font=(FONT_FAMILY, 22, "bold"),
        text_color=ACCENT,
    ).pack(side="left")
    ctk.CTkLabel(
        header, text="game logger", font=(FONT_FAMILY, 13),
        text_color=FG_DIM,
    ).pack(side="left", padx=(10, 0), pady=(6, 0))

    # ── Summoner name ──
    name_frame = ctk.CTkFrame(app, fg_color=BG_CARD, corner_radius=12)
    name_frame.pack(fill="x", padx=20, pady=(10, 0))

    ctk.CTkLabel(
        name_frame, text="Your summoner name  (just the name, not the #tag)",
        font=(FONT_FAMILY, 12, "bold"), text_color=FG_DIM,
    ).pack(anchor="w", padx=16, pady=(12, 4))

    saved_name = load_gui_settings().get("summoner_name", "")
    name_var = tk.StringVar(value=saved_name)

    name_row = ctk.CTkFrame(name_frame, fg_color="transparent")
    name_row.pack(fill="x", padx=16, pady=(0, 12))

    ctk.CTkEntry(
        name_row, textvariable=name_var, height=36,
        font=(FONT_FAMILY, 12), border_width=0,
        placeholder_text="e.g. joes9987",
    ).pack(side="left", fill="x", expand=True, padx=(0, 8))

    def save_summoner_name() -> None:
        data = load_gui_settings()
        data["summoner_name"] = name_var.get().strip()
        save_gui_settings(data)
        append(f"Saved default summoner name: {name_var.get().strip()}")

    ctk.CTkButton(
        name_row, text="Save as default", width=130, height=36,
        fg_color="transparent", border_width=1, border_color=FG_DIM,
        text_color=FG_DIM, hover_color="#2a2a4a", font=(FONT_FAMILY, 11),
        corner_radius=6, command=save_summoner_name,
    ).pack(side="right")

    # ── Status indicator ──
    status_frame = ctk.CTkFrame(app, fg_color="transparent")
    status_frame.pack(fill="x", padx=20, pady=(10, 0))
    status_dot = ctk.CTkLabel(status_frame, text="\u25cf", text_color=FG_DIM, font=(FONT_FAMILY, 14))
    status_dot.pack(side="left")
    status_label = ctk.CTkLabel(
        status_frame, text="  Idle — press Start to begin logging",
        font=(FONT_FAMILY, 12), text_color=FG_DIM,
    )
    status_label.pack(side="left")

    # ── Log area ──
    log_frame = ctk.CTkFrame(app, fg_color=BG_CARD, corner_radius=12)
    log_frame.pack(fill="both", expand=True, padx=20, pady=(10, 0))

    text = ctk.CTkTextbox(
        log_frame, font=(FONT_FAMILY, 11),
        fg_color="#0f0f23", text_color=FG, corner_radius=8,
        border_width=0, wrap="word",
    )
    text.pack(fill="both", expand=True, padx=12, pady=12)

    # ── Buttons ──
    btn_row = ctk.CTkFrame(app, fg_color="transparent")
    btn_row.pack(fill="x", padx=20, pady=(12, 6))

    start_btn = ctk.CTkButton(
        btn_row, text="Start logging", height=42, width=160,
        fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color="#000",
        font=(FONT_FAMILY, 14, "bold"), corner_radius=10,
    )
    start_btn.pack(side="left", padx=(0, 10))

    stop_btn = ctk.CTkButton(
        btn_row, text="Stop and save", height=42, width=160,
        fg_color=DANGER, hover_color=DANGER_HOVER, text_color="#fff",
        font=(FONT_FAMILY, 14, "bold"), corner_radius=10, state="disabled",
    )
    stop_btn.pack(side="left")

    # ── Hint ──
    ctk.CTkLabel(
        app,
        text="Run while League is in a match. Use Stop and save when you finish recording if OBS did not auto-stop.",
        font=(FONT_FAMILY, 11), text_color=FG_DIM, wraplength=500,
    ).pack(padx=20, pady=(4, 14))

    def append(msg: str) -> None:
        text.insert(tk.END, msg + "\n")
        text.see(tk.END)

    def thread_log(msg: str) -> None:
        log_q.put(msg)

    def poll() -> None:
        try:
            while True:
                m = log_q.get_nowait()
                if m is None:
                    worker_running.clear()
                    start_btn.configure(state="normal")
                    stop_btn.configure(state="disabled")
                    status_dot.configure(text_color=FG_DIM)
                    status_label.configure(text="  Session ended")
                    append("\n--- Session ended ---")
                    return
                append(m)
        except queue.Empty:
            pass
        if worker_running.is_set():
            app.after(100, poll)

    def start_logging() -> None:
        if worker_running.is_set():
            return
        current_name = name_var.get().strip()
        stop_event.clear()
        start_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        status_dot.configure(text_color=ACCENT)
        status_label.configure(text="  Logging — waiting for game data…")
        worker_running.set()

        def worker() -> None:
            try:
                run_session(
                    stop_event=stop_event,
                    log=thread_log,
                    summoner_name=current_name or None,
                )
            except Exception as e:
                thread_log(f"Error: {e}")
            finally:
                log_q.put(None)

        threading.Thread(target=worker, daemon=True).start()
        poll()

    def stop_logging() -> None:
        append("\nStopping (saving if any kills were logged)…")
        status_label.configure(text="  Stopping…")
        stop_event.set()

    start_btn.configure(command=start_logging)
    stop_btn.configure(command=stop_logging)

    app.mainloop()


if __name__ == "__main__":
    main()
