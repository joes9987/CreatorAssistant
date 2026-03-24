"""
League of Legends Game Events Logger for CreatorAssistant.
Polls Riot's Live Client Data API during a match and logs kill events with timestamps.
Run this while playing + recording - it writes events to a JSON file used for clip extraction.

Uses the same data source as Overwolf's game events (Riot's local API).

When OBS WebSocket is available, detects RecordingStarted/RecordingStopped and
automatically exits when recording stops (no Ctrl+C needed).
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

# Live Client Data API - runs on port 2999 during an active LoL match
API_BASE = "https://127.0.0.1:2999"
EVENTS_LOG_DIR = "eventlogs"


def _log(log: Callable[[str], None] | None, msg: str) -> None:
    if log:
        log(msg)
    else:
        print(msg)


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
        _log(log, "  OBS auto-stop: install obsws-python (pip install obsws-python)")
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
        _log(
            log,
            f"  OBS auto-stop: could not connect ({e}). Enable WebSocket in OBS (Tools → WebSocket Server Settings).",
        )
        return None, None

    try:
        evt_client = obs.EventClient(host=host, port=port, password=password)
        return req_client, evt_client
    except Exception:
        return req_client, None


def _on_recording_stopped(stop_event: threading.Event, log: Callable[[str], None] | None):
    def _handler(_data):
        _log(log, f"  OBS recording stopped at {datetime.now().strftime('%H:%M:%S')} - exiting")
        stop_event.set()

    return _handler


def _on_recording_started(log: Callable[[str], None] | None):
    def _handler(_data):
        _log(log, f"  OBS recording started at {datetime.now().strftime('%H:%M:%S')}")

    return _handler


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
    if obs_evt:
        obs_evt.callback.register(_on_recording_started(log))
        obs_evt.callback.register(_on_recording_stopped(stop_event, log))

    _log(log, "CreatorAssistant Game Events Logger")
    _log(log, "=" * 40)
    if effective_name:
        _log(log, f"Tracking kills for: {effective_name}")
    else:
        _log(log, "No summoner name set — logging ALL kills.")
    _log(log, "Make sure League of Legends is in an active match.")
    _log(log, "Start OBS recording when the game loads in.")
    if obs_req:
        _log(log, "OBS WebSocket connected - will auto-exit when recording stops.")
    else:
        _log(log, "Use Stop and save in the app (or Ctrl+C in terminal) when the game ends.")
    _log(log, "")

    output_file: Path | None = None
    try:
        while not stop_event.is_set():
            data = fetch_live_data()
            if data is None:
                was_disconnected = True
                if stop_event.wait(timeout=2):
                    break
                continue

            if was_disconnected and session_start is not None:
                seen_event_ids.clear()
                _log(log, f"  New game detected at {datetime.now().strftime('%H:%M:%S')}")
            was_disconnected = False

            if session_start is None:
                session_start = time.time()
                _log(log, f"Connected to game at {datetime.now().strftime('%H:%M:%S')}")

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
                    _log(log, f"  GameStart at {event_time:.1f}s")
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
                    _log(log, f"  Kill: {killer_name}{kc} -> {victim_name}{vc} @ {event_time:.1f}s")

            if obs_req:
                try:
                    status = obs_req.get_record_status()
                    active = getattr(status, "output_active", getattr(status, "outputActive", False))
                    if was_recording and not active:
                        _log(
                            log,
                            f"  OBS recording stopped (polled) at {datetime.now().strftime('%H:%M:%S')} - exiting",
                        )
                        stop_event.set()
                    was_recording = bool(active)
                except Exception:
                    pass

            if stop_event.wait(timeout=1):
                break

    except KeyboardInterrupt:
        _log(log, "\nStopping...")

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
        _log(log, f"Saved {len(events_log)} kill events to {output_file}")
    else:
        _log(log, "No kill events recorded.")

    return output_file


if __name__ == "__main__":
    run_session()
