"""
League of Legends Game Events Logger for CreatorAssistant.
Polls Riot's Live Client Data API during a match and logs kill events with timestamps.
Run this while playing + recording - it writes events to a JSON file used for clip extraction.

Uses the same data source as Overwolf's game events (Riot's local API).

When OBS WebSocket is available, detects recording state changes and
automatically exits when recording stops (no Ctrl+C needed).
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

import urllib.request
import ssl

# Live Client Data API - runs on port 2999 during an active LoL match
API_BASE = "https://127.0.0.1:2999"
EVENTS_LOG_DIR = "eventlogs"


def _connect_obs(config: dict) -> tuple["obsws_python.ReqClient | None", "obsws_python.EventClient | None"]:
    """
    Connect to OBS WebSocket. Returns (ReqClient, EventClient) or (None, None) on failure.
    ReqClient used for polling recording status; EventClient for RecordStateChanged events.
    """
    try:
        import obsws_python as obs
    except ImportError:
        print("  OBS auto-stop: install obsws-python (pip install obsws-python)")
        return None, None

    cfg = (config or {}).get("game_events", {}).get("obs_websocket", {})
    if cfg.get("enabled", True) is False:
        return None, None

    host = cfg.get("host", "localhost")
    port = cfg.get("port", 4455)
    password = cfg.get("password", "")

    req_client = None
    evt_client = None

    # ReqClient for polling - most reliable way to detect recording stop
    try:
        req_client = obs.ReqClient(host=host, port=port, password=password)
        _ = req_client.get_record_status()
    except Exception as e:
        print(f"  OBS auto-stop: could not connect ({e}). Enable WebSocket in OBS (Tools > WebSocket Server Settings).")
        if password == "":
            print("  Hint: OBS has authentication enabled - set your password in config.yaml under game_events.obs_websocket.password")
        return None, None

    # EventClient for RecordStateChanged - backup for polling
    try:
        evt_client = obs.EventClient(host=host, port=port, password=password)
        return req_client, evt_client
    except Exception:
        return req_client, None


def _register_obs_callbacks(evt_client, stop_event: threading.Event):
    """Register OBS event callbacks. Function names must match obsws-python's dispatch pattern."""

    # obsws-python dispatches by matching fn.__name__ against "on_{snake_case(event)}"
    # OBS WebSocket v5 fires "RecordStateChanged" -> needs "on_record_state_changed"
    def on_record_state_changed(data):
        active = getattr(data, "output_active", False)
        state = getattr(data, "output_state", "")
        if not active or state == "OBS_WEBSOCKET_OUTPUT_STOPPED":
            print(f"  OBS recording stopped at {datetime.now().strftime('%H:%M:%S')} - exiting")
            stop_event.set()
        elif state == "OBS_WEBSOCKET_OUTPUT_STARTED":
            print(f"  OBS recording started at {datetime.now().strftime('%H:%M:%S')}")

    evt_client.callback.register(on_record_state_changed)


def _check_obs_recording(obs_req, was_recording: bool, stop_event: threading.Event) -> bool:
    """Poll OBS recording status. Returns updated was_recording state."""
    if not obs_req:
        return was_recording
    try:
        status = obs_req.get_record_status()
        active = getattr(status, "output_active", False)
        if was_recording and not active:
            print(f"  OBS recording stopped (polled) at {datetime.now().strftime('%H:%M:%S')} - exiting")
            stop_event.set()
        return bool(active)
    except Exception:
        return was_recording


def _build_player_maps(data):
    """
    Build participantId -> {name, champion} and summonerName -> champion from allPlayers.
    Overwolf/Riot all_players has championName and summonerName; participantId may exist.
    """
    players = data.get("allPlayers") or data.get("all_players") or []
    by_pid = {}
    by_summoner = {}
    for p in players:
        pid = p.get("participantId") or p.get("participantID")
        name = p.get("summonerName") or p.get("summoner_name")
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
        path = Path(__file__).resolve().parent / "config.yaml"
        if path.exists():
            with open(path) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def run_session():
    """
    Run the logger. Polls the API every 1 second, logs new ChampionKill events.
    Exits on Ctrl+C or when OBS recording stops (if OBS WebSocket is available).
    Saves to eventlogs/events_YYYY-MM-DD_HH-MM-SS.json by default.
    """
    config = _load_config()
    base = Path(__file__).resolve().parent
    log_dir = Path(config.get("game_events", {}).get("log_dir", EVENTS_LOG_DIR))
    if not log_dir.is_absolute():
        log_dir = base / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    seen_event_ids = set()
    events_log = []
    session_start = None
    game_start_time = None
    was_disconnected = False
    local_player_champion = None
    stop_event = threading.Event()
    obs_req, obs_evt = _connect_obs(config)
    was_recording = False
    if obs_evt:
        _register_obs_callbacks(obs_evt, stop_event)

    print("CreatorAssistant Game Events Logger")
    print("=" * 40)
    print("Make sure League of Legends is in an active match.")
    print("Start OBS recording when the game loads in.")
    if obs_req:
        print("OBS WebSocket connected - will auto-exit when recording stops.")
    else:
        print("Press Ctrl+C when the game ends to save and exit.")
    print()

    try:
        while not stop_event.is_set():
            # Always poll OBS even when the game API is disconnected (game ended but still recording)
            was_recording = _check_obs_recording(obs_req, was_recording, stop_event)
            if stop_event.is_set():
                break

            data = fetch_live_data()
            if data is None:
                was_disconnected = True
                if stop_event.wait(timeout=2):
                    break
                continue

            # Reconnected after disconnect (e.g. new game) - reset event tracking
            if was_disconnected and session_start is not None:
                seen_event_ids.clear()
                print(f"  New game detected at {datetime.now().strftime('%H:%M:%S')}")
            was_disconnected = False

            # First connection - record session start
            if session_start is None:
                session_start = time.time()
                print(f"Connected to game at {datetime.now().strftime('%H:%M:%S')}")

            # Get events (Riot may return as object or JSON string)
            events_data = data.get("events")
            if isinstance(events_data, str):
                try:
                    events_data = json.loads(events_data)
                except json.JSONDecodeError:
                    events_data = {}
            events_data = events_data or {}
            events_list = events_data.get("Events") or []

            # Get game time from gameData for reference
            game_data = data.get("gameData") or {}
            current_game_time = game_data.get("gameTime", 0)
            if game_start_time is None and current_game_time > 0:
                game_start_time = current_game_time

            # Resolve local player champion (champion you're playing)
            if local_player_champion is None:
                _, champion_by_summoner = _build_player_maps(data)
                active = data.get("activePlayer") or data.get("active_player") or {}
                if isinstance(active, dict):
                    my_name = active.get("summonerName") or active.get("summoner_name")
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
                    print(f"  GameStart at {event_time:.1f}s")
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
                    print(f"  Kill: {killer_name}{kc} -> {victim_name}{vc} @ {event_time:.1f}s")

            if stop_event.wait(timeout=1):
                break

    except KeyboardInterrupt:
        print("\nStopping...")

    # Save events
    if events_log:
        output = {
            "session_start": session_start,
            "game_start_time": game_start_time,
            "local_player_champion": local_player_champion,
            "events": events_log,
            "total_kills": len(events_log),
        }
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output_file = log_dir / f"events_{timestamp}.json"
        with open(output_file, "w") as f:
            json.dump(output, f, indent=2)
        print(f"Saved {len(events_log)} kill events to {output_file}")
    else:
        print("No kill events recorded.")


if __name__ == "__main__":
    run_session()
