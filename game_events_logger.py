"""
League of Legends Game Events Logger for CreatorAssistant.
Polls Riot's Live Client Data API during a match and logs kill events with timestamps.
Run this while playing + recording - it writes events to a JSON file used for clip extraction.

Uses the same data source as Overwolf's game events (Riot's local API).
"""

import json
import time
from datetime import datetime
from pathlib import Path

import urllib.request
import ssl

# Live Client Data API - runs on port 2999 during an active LoL match
API_BASE = "https://127.0.0.1:2999"
EVENTS_FILE = "game_events.json"


def _build_player_map(data):
    """Build participantId -> summonerName map from allPlayers."""
    players = data.get("allPlayers") or data.get("all_players") or []
    result = {}
    for p in players:
        pid = p.get("participantId") or p.get("participantID")
        name = p.get("summonerName") or p.get("summoner_name")
        if pid is not None and name:
            result[pid] = name
    return result


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


def run_session(output_path: str = EVENTS_FILE):
    """
    Run the logger. Polls the API every 1 second, logs new ChampionKill events.
    Press Ctrl+C to stop.
    """
    output_path = Path(output_path)
    seen_event_ids = set()
    events_log = []
    session_start = None
    game_start_time = None
    was_disconnected = False

    print("CreatorAssistant Game Events Logger")
    print("=" * 40)
    print("Make sure League of Legends is in an active match.")
    print("Start OBS recording when the game loads in.")
    print("Press Ctrl+C when the game ends to save and exit.")
    print()

    try:
        while True:
            data = fetch_live_data()
            if data is None:
                was_disconnected = True
                time.sleep(2)
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
                    # Resolve killer name: API may provide KillerName or we map killerId via allPlayers
                    killer_name = ev.get("KillerName") or ev.get("killerName")
                    if not killer_name:
                        killer_id = ev.get("KillerID") or ev.get("killerId")
                        player_map = _build_player_map(data)
                        killer_name = player_map.get(killer_id, f"Unknown#{killer_id}") if killer_id is not None else "Unknown"
                    victim_name = ev.get("VictimName") or ev.get("victimName") or "?"
                    kill_data = {
                        "type": "ChampionKill",
                        "game_time": event_time,
                        "wall_clock": time.time(),
                        "event_id": eid,
                        "killer_name": killer_name,
                        "victim_name": victim_name,
                        "data": {k: v for k, v in ev.items() if k not in ("EventID", "EventName", "EventTime")},
                    }
                    events_log.append(kill_data)
                    print(f"  Kill: {killer_name} -> {victim_name} @ {event_time:.1f}s")

            time.sleep(1)

    except KeyboardInterrupt:
        print("\nStopping...")

    # Save events
    if events_log:
        output = {
            "session_start": session_start,
            "game_start_time": game_start_time,
            "events": events_log,
            "total_kills": len(events_log),
        }
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        print(f"Saved {len(events_log)} kill events to {output_path}")
    else:
        print("No kill events recorded.")


if __name__ == "__main__":
    run_session()
