"""
League of Legends Game Events Logger for CreatorAssistant.
Polls Riot's Live Client Data API during a match and logs kill events with timestamps.
Run this while playing + recording - it writes events to a JSON file used for clip extraction.

Uses the same data source as Overwolf's game events (Riot's local API).

When OBS WebSocket is available, detects RecordingStarted/RecordingStopped and
automatically exits when recording stops (no Ctrl+C needed).

Optional (game_events.obs_websocket): auto_start_recording calls OBS Start Record when the
Live Client reports GameStart (with a mid-game fallback if GameStart was missed). Pair with
auto_stop_recording_on_game_end to Stop Record on GameEnd or after Live Client disconnects.
OBS must be running with WebSocket enabled for either control path.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import urllib.request
import ssl

from app_paths import project_root
from timer_utils import emit_log

# Live Client Data API - runs on port 2999 during an active LoL match
API_BASE = "https://127.0.0.1:2999"
EVENTS_LOG_DIR = "eventlogs"


def _connect_obs(
    config: dict, log: Callable[[str], None] | None = None
) -> tuple["obsws_python.ReqClient | None", "obsws_python.EventClient | None"]:
    """
    Connect to OBS WebSocket. Returns (ReqClient, EventClient) or (None, None) on failure.
    ReqClient used for polling recording status; EventClient for RecordingStopped events.
    """
    try:
        import obsws_python as obs
    except ImportError:
        emit_log(log, "  OBS auto-stop: install obsws-python (pip install obsws-python)")
        return None, None

    cfg = (config or {}).get("game_events", {}).get("obs_websocket", {})
    if cfg.get("enabled", True) is False:
        return None, None

    host = cfg.get("host", "localhost")
    port = cfg.get("port", 4455)
    password = cfg.get("password", "")

    req_client = None
    evt_client = None

    try:
        req_client = obs.ReqClient(host=host, port=port, password=password)
        _ = req_client.get_record_status()
    except Exception as e:
        emit_log(
            log,
            f"  OBS auto-stop: could not connect ({e}). Enable WebSocket in OBS (Tools → WebSocket Server Settings).",
        )
        return None, None

    try:
        evt_client = obs.EventClient(host=host, port=port, password=password)
        return req_client, evt_client
    except Exception:
        return req_client, None


def _make_obs_handler(stop_event: threading.Event, log: Callable[[str], None] | None):
    """Return a correctly-named callback for obsws-python's RecordStateChanged event.

    obsws-python dispatches events by matching the registered function's __name__
    to on_<event_in_snake_case>.  RecordStateChanged → on_record_state_changed.
    """

    def on_record_state_changed(data):
        state = getattr(data, "output_state", "")
        if state == "OBS_WEBSOCKET_OUTPUT_STOPPED":
            emit_log(log, f"  OBS recording stopped at {datetime.now().strftime('%H:%M:%S')} - exiting")
            stop_event.set()
        elif state == "OBS_WEBSOCKET_OUTPUT_STARTED":
            emit_log(log, f"  OBS recording started at {datetime.now().strftime('%H:%M:%S')}")

    return on_record_state_changed


def _build_player_maps(data):
    """Build participantId -> {name, champion} and summonerName -> champion from allPlayers."""
    players = data.get("allPlayers") or data.get("all_players") or []
    by_pid = {}
    by_summoner = {}
    for p in players:
        pid = p.get("participantId") or p.get("participantID")
        name = p.get("summonerName") or p.get("summoner_name") or p.get("gameName") or p.get("riotId") or ""
        if "#" in name:
            name = name.split("#")[0]
        champion = p.get("championName") or p.get("champion_name") or ""
        if name:
            by_summoner[name] = champion
        if pid is not None and name:
            by_pid[pid] = {"name": name, "champion": champion}
    return by_pid, by_summoner


def fetch_live_data():
    """Fetch all game data from Live Client Data API. Returns None if game not running."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(f"{API_BASE}/liveclientdata/allgamedata")
        with urllib.request.urlopen(req, timeout=2, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _load_config() -> dict:
    """Load config from config.yaml if present."""
    try:
        import yaml
        path = project_root() / "config.yaml"
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def run_session(
    stop_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    summoner_name: str | None = None,
) -> Path | None:
    """
    Run the logger. Polls the API every 1 second, logs new ChampionKill events.
    Stops when stop_event is set, Ctrl+C, or OBS recording stops (if WebSocket works).
    Optional OBS WebSocket: auto-start on GameStart, auto-stop on GameEnd / client disconnect.
    Saves to eventlogs/events_YYYY-MM-DD_HH-MM-SS.json by default.
    Returns path to saved JSON if kills were logged, else None.

    summoner_name: if given, overrides config game_events.player_summoner_name
                   and also written into the output JSON for detect.py filtering.
    """
    config = _load_config()
    effective_name = (summoner_name or "").strip()
    if not effective_name:
        effective_name = (config.get("game_events", {}).get("player_summoner_name") or "").strip()
    if "#" in effective_name:
        effective_name = effective_name.split("#")[0].strip()
    root = project_root()
    log_dir = Path(config.get("game_events", {}).get("log_dir", EVENTS_LOG_DIR))
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    seen_event_ids = set()
    events_log = []
    session_start = None
    game_start_time = None
    was_disconnected = False
    local_player_champion = None
    if stop_event is None:
        stop_event = threading.Event()
    obs_req, obs_evt = _connect_obs(config, log)
    was_recording = False
    obs_ws_cfg = (config.get("game_events") or {}).get("obs_websocket") or {}
    auto_start_recording = bool(obs_ws_cfg.get("auto_start_recording", False))
    auto_stop_recording = bool(obs_ws_cfg.get("auto_stop_recording_on_game_end", False))
    disconnect_threshold = float(obs_ws_cfg.get("live_client_disconnect_stop_seconds", 8))
    mid_game_join_sec = float(obs_ws_cfg.get("mid_game_join_recording_game_time_sec", 5))

    lol_match_in_progress = False  # GameStart seen or game clock passed mid_game_join threshold
    obs_start_done = False
    obs_stop_done = False
    disconnect_since: float | None = None

    def try_obs_start_once():
        nonlocal obs_start_done
        if not auto_start_recording or not obs_req or obs_start_done:
            return
        try:
            status = obs_req.get_record_status()
            active = getattr(status, "output_active", getattr(status, "outputActive", False))
            if active:
                emit_log(
                    log,
                    f"  OBS: already recording at {datetime.now().strftime('%H:%M:%S')} — skipping auto-start",
                )
                obs_start_done = True
                return
            obs_req.start_record()
            emit_log(
                log,
                f"  OBS: started recording automatically at {datetime.now().strftime('%H:%M:%S')}",
            )
        except Exception as e:
            emit_log(log, f"  OBS: Start Record failed ({e})")
        obs_start_done = True

    def try_obs_stop(reason: str):
        nonlocal obs_stop_done
        if not auto_stop_recording or not obs_req or obs_stop_done:
            return
        try:
            status = obs_req.get_record_status()
            active = getattr(status, "output_active", getattr(status, "outputActive", False))
            if not active:
                obs_stop_done = True
                return
            obs_req.stop_record()
            emit_log(log, f"  OBS: Stop Record ({reason})")
        except Exception as e:
            emit_log(log, f"  OBS: Stop Record failed ({e})")
        obs_stop_done = True

    if obs_evt:
        obs_evt.callback.register(_make_obs_handler(stop_event, log))
        emit_log(log, "  OBS event listener registered (RecordStateChanged)")

    emit_log(log, "CreatorAssistant Game Events Logger")
    emit_log(log, "=" * 40)
    if effective_name:
        emit_log(log, f"Tracking kills for: {effective_name}")
    else:
        emit_log(log, "No summoner name set — logging ALL kills.")
    emit_log(log, "Make sure League of Legends is in an active match.")
    if obs_req and auto_start_recording:
        emit_log(
            log,
            "OBS WebSocket connected — recording will start on GameStart "
            f"(or once game clock ≥ {mid_game_join_sec:.0f}s if you joined mid-game).",
        )
    elif obs_req:
        emit_log(log, "OBS WebSocket connected — start recording manually when ready (or set auto_start_recording in config).")
    else:
        emit_log(log, "Start OBS recording when the game loads in (if OBS WebSocket is unavailable).")
    if obs_req and auto_stop_recording:
        emit_log(
            log,
            "OBS will stop recording on GameEnd or if Live Client disconnects for "
            f"~{disconnect_threshold:.0f}s after the match was live.",
        )
    if obs_req:
        emit_log(log, "Will auto-exit when OBS recording stops.")
    else:
        emit_log(log, "Use Stop and save in the app (or Ctrl+C in terminal) when the game ends.")
    emit_log(log, "")

    output_file: Path | None = None
    try:
        while not stop_event.is_set():
            data = fetch_live_data()
            if data is None:
                was_disconnected = True
                if obs_req and auto_stop_recording and lol_match_in_progress and not obs_stop_done:
                    if disconnect_since is None:
                        disconnect_since = time.time()
                    elif time.time() - disconnect_since >= disconnect_threshold:
                        try_obs_stop("Live Client disconnected — match likely ended")
                        disconnect_since = None
            else:
                disconnect_since = None
                if was_disconnected and session_start is not None:
                    seen_event_ids.clear()
                    lol_match_in_progress = False
                    obs_start_done = False
                    obs_stop_done = False
                    emit_log(log, f"  New game detected at {datetime.now().strftime('%H:%M:%S')}")
                was_disconnected = False

                if session_start is None:
                    session_start = time.time()
                    emit_log(log, f"Connected to game at {datetime.now().strftime('%H:%M:%S')}")

                events_data = data.get("events")
                if isinstance(events_data, str):
                    try:
                        events_data = json.loads(events_data)
                    except json.JSONDecodeError:
                        events_data = {}
                events_data = events_data or {}
                events_list = events_data.get("Events") or []

                game_data = data.get("gameData") or {}
                current_game_time = game_data.get("gameTime", 0)
                if game_start_time is None and current_game_time > 0:
                    game_start_time = current_game_time

                if local_player_champion is None:
                    _, champion_by_summoner = _build_player_maps(data)
                    active = data.get("activePlayer") or data.get("active_player") or {}
                    if isinstance(active, dict):
                        my_name = active.get("summonerName") or active.get("summoner_name") or active.get("gameName") or active.get("riotId") or ""
                        if "#" in my_name:
                            my_name = my_name.split("#")[0]
                        if my_name:
                            local_player_champion = champion_by_summoner.get(my_name) or ""

                for ev in events_list:
                    eid = ev.get("EventID")
                    if eid is None or eid in seen_event_ids:
                        continue
                    seen_event_ids.add(eid)

                    event_name = ev.get("EventName", "")
                    event_time = ev.get("EventTime", 0)

                    if event_name == "GameStart":
                        game_start_time = event_time
                        lol_match_in_progress = True
                        emit_log(log, f"  GameStart at {event_time:.1f}s")
                        try_obs_start_once()
                    elif event_name == "GameEnd":
                        lol_match_in_progress = True
                        emit_log(log, f"  GameEnd at {event_time:.1f}s")
                        try_obs_stop("GameEnd event")
                    elif event_name == "ChampionKill":
                        player_by_pid, champion_by_summoner = _build_player_maps(data)
                        killer_id = ev.get("KillerID") or ev.get("killerId")
                        victim_id = ev.get("VictimID") or ev.get("victimId")
                        killer_name = ev.get("KillerName") or ev.get("killerName")
                        if not killer_name:
                            info = player_by_pid.get(killer_id) if killer_id is not None else None
                            killer_name = info["name"] if info else (f"Unknown#{killer_id}" if killer_id is not None else "Unknown")
                        victim_name = ev.get("VictimName") or ev.get("victimName") or "?"
                        if not victim_name or victim_name == "?":
                            info = player_by_pid.get(victim_id) if victim_id is not None else None
                            victim_name = info["name"] if info else (f"Unknown#{victim_id}" if victim_id is not None else "?")
                        info = player_by_pid.get(killer_id) if killer_id is not None else None
                        killer_champion = (info["champion"] if info else champion_by_summoner.get(killer_name)) or ""
                        info = player_by_pid.get(victim_id) if victim_id is not None else None
                        victim_champion = (info["champion"] if info else champion_by_summoner.get(victim_name)) or ""
                        kill_data = {
                            "type": "ChampionKill",
                            "game_time": event_time,
                            "wall_clock": time.time(),
                            "event_id": eid,
                            "killer_name": killer_name,
                            "victim_name": victim_name,
                            "killer_champion": killer_champion or None,
                            "victim_champion": victim_champion or None,
                            "data": {k: v for k, v in ev.items() if k not in ("EventID", "EventName", "EventTime")},
                        }
                        events_log.append(kill_data)
                        kc = f" ({killer_champion})" if killer_champion else ""
                        vc = f" ({victim_champion})" if victim_champion else ""
                        emit_log(log, f"  Kill: {killer_name}{kc} -> {victim_name}{vc} @ {event_time:.1f}s")

                # Mid-game join: GameStart may have scrolled off the Events buffer — start once by clock.
                if (
                    auto_start_recording
                    and obs_req
                    and not obs_start_done
                    and data is not None
                ):
                    gt_join = float((data.get("gameData") or {}).get("gameTime") or 0)
                    if gt_join >= mid_game_join_sec:
                        lol_match_in_progress = True
                        emit_log(
                            log,
                            f"  Game clock ≥ {mid_game_join_sec:.0f}s (GameStart may have been missed) — starting OBS recording",
                        )
                        try_obs_start_once()

            # OBS polling runs every iteration, even when the game API is unavailable
            if obs_req:
                try:
                    status = obs_req.get_record_status()
                    active = getattr(status, "output_active", getattr(status, "outputActive", False))

                    if was_recording and not active:
                        emit_log(
                            log,
                            f"  OBS recording stopped (polled) at {datetime.now().strftime('%H:%M:%S')} - exiting",
                        )
                        stop_event.set()
                    was_recording = bool(active)
                except Exception:
                    pass

            if stop_event.wait(timeout=1 if data is not None else 2):
                break

    except KeyboardInterrupt:
        emit_log(log, "\nStopping...")

    if events_log:
        output = {
            "session_start": session_start,
            "game_start_time": game_start_time,
            "local_player_champion": local_player_champion,
            "player_summoner_name": effective_name or None,
            "events": events_log,
            "total_kills": len(events_log),
        }
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output_file = log_dir / f"events_{timestamp}.json"
        with open(output_file, "w") as f:
            json.dump(output, f, indent=2)
        emit_log(log, f"Saved {len(events_log)} kill events to {output_file}")
    else:
        emit_log(log, "No kill events recorded.")

    return output_file


if __name__ == "__main__":
    run_session()
