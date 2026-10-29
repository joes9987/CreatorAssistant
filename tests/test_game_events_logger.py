"""Game-event logging. Live Client and OBS are mocked; nothing listens on the network."""

import json
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from game_events_logger import (
    _build_player_maps,
    _make_obs_handler,
    fetch_live_data,
    run_session,
)


class FastEvent(threading.Event):
    def wait(self, timeout=None):
        return self.is_set()


def _players():
    return [
        {"summonerName": "Me", "championName": "Ahri", "participantId": 1},
        {"summonerName": "Them", "championName": "Zed", "participantId": 2},
    ]


def _live(events, game_time=100, players=None, active="Me"):
    active_player = {"summonerName": active} if isinstance(active, str) else (active or {})
    return {
        "activePlayer": active_player,
        "allPlayers": _players() if players is None else players,
        "gameData": {"gameTime": game_time},
        "events": {"Events": events},
    }


def _kill(**extra):
    event = {
        "EventID": 2,
        "EventName": "ChampionKill",
        "EventTime": 80.5,
        "KillerID": 1,
        "VictimID": 2,
    }
    event.update(extra)
    return event


class PlayerMapTests(unittest.TestCase):
    def test_aliases_and_riot_id_tag(self):
        by_pid, by_summoner = _build_player_maps(
            {
                "all_players": [
                    {"participantID": 4, "riotId": "Player#EUW", "champion_name": "Jinx"},
                    {"participantId": 5, "gameName": "NoChamp"},
                    {"summoner_name": "", "championName": "Ghost"},
                ]
            }
        )
        self.assertEqual(by_pid[4], {"name": "Player", "champion": "Jinx"})
        self.assertEqual(by_pid[5], {"name": "NoChamp", "champion": ""})
        self.assertEqual(by_summoner["Player"], "Jinx")
        self.assertEqual(by_summoner["NoChamp"], "")
        self.assertNotIn("", by_summoner)

    def test_missing_player_lists(self):
        self.assertEqual(_build_player_maps({}), ({}, {}))


class ObsHandlerTests(unittest.TestCase):
    def test_stopped_sets_the_event_and_started_does_not(self):
        stop = threading.Event()
        logs = []
        callback = _make_obs_handler(stop, logs.append)
        self.assertEqual(callback.__name__, "on_record_state_changed")
        callback(SimpleNamespace(output_state="OBS_WEBSOCKET_OUTPUT_STARTED"))
        self.assertFalse(stop.is_set())
        callback(SimpleNamespace(output_state="OBS_WEBSOCKET_OUTPUT_STOPPED"))
        self.assertTrue(stop.is_set())
        self.assertTrue(any("recording stopped" in line for line in logs))
        self.assertTrue(any("recording started" in line for line in logs))

    def test_missing_state_is_ignored(self):
        stop = threading.Event()
        _make_obs_handler(stop, None)(SimpleNamespace())
        self.assertFalse(stop.is_set())


class FetchLiveDataTests(unittest.TestCase):
    def test_reads_local_api_and_swallows_errors(self):
        response = MagicMock()
        response.read.return_value = b'{"allPlayers": []}'
        response.__enter__.return_value = response
        with patch("game_events_logger.urllib.request.urlopen", return_value=response) as urlopen:
            data = fetch_live_data()
        self.assertEqual(data, {"allPlayers": []})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://127.0.0.1:2999/liveclientdata/allgamedata")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2)
        context = urlopen.call_args.kwargs["context"]
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)

        with patch("game_events_logger.urllib.request.urlopen", side_effect=TimeoutError("down")):
            self.assertIsNone(fetch_live_data())


class _Obs:
    def __init__(self, active=False):
        self.active = active
        self.starts = 0
        self.stops = 0

    def get_record_status(self):
        return SimpleNamespace(output_active=self.active)

    def start_record(self):
        self.starts += 1
        self.active = True

    def stop_record(self):
        self.stops += 1
        self.active = False


class RunSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self, summoner="", **obs):
        obs_cfg = {
            "enabled": True,
            "auto_start_recording": False,
            "auto_stop_recording_on_game_end": False,
            "live_client_disconnect_stop_seconds": 8,
            "mid_game_join_recording_game_time_sec": 5,
            "obs_recording_start_delay_seconds": 0,
        }
        obs_cfg.update(obs)
        return {
            "game_events": {
                "log_dir": str(self.log_dir),
                "player_summoner_name": summoner,
                "obs_websocket": obs_cfg,
            }
        }

    def _run(self, payloads, config, summoner=None, obs=None, clock=None):
        stop = FastEvent()
        state = {"i": 0}
        logs = []

        def fetch():
            index = state["i"]
            state["i"] += 1
            if index >= len(payloads):
                stop.set()
                return None
            if index == len(payloads) - 1:
                stop.set()
            return payloads[index]

        patches = [
            patch("game_events_logger._load_config", return_value=config),
            patch("game_events_logger._connect_obs", return_value=(obs, None)),
            patch("game_events_logger.fetch_live_data", side_effect=fetch),
            patch("game_events_logger.urllib.request.urlopen", side_effect=AssertionError("network")),
        ]
        if clock is not None:
            patches.append(patch("game_events_logger.time.time", side_effect=lambda: clock["t"]))
        for item in patches:
            item.start()
        try:
            path = run_session(stop_event=stop, log=logs.append, summoner_name=summoner)
        finally:
            for item in reversed(patches):
                item.stop()
        return path, logs

    def _saved(self, path):
        self.assertIsNotNone(path)
        self.assertEqual(path.parent, self.log_dir)
        self.assertTrue(path.name.startswith("events_"))
        return json.loads(path.read_text())

    def test_resolves_kill_names_and_strips_summoner_override(self):
        path, logs = self._run(
            [_live([
                {"EventID": 1, "EventName": "GameStart", "EventTime": 0},
                _kill(),
                _kill(EventID=3, KillerID=None, VictimID=None, KillerName="Me", VictimName="Them", EventTime=90),
            ])],
            self._config(summoner="FromConfig#EUW"),
            summoner="Me#EUW",
        )
        saved = self._saved(path)
        self.assertEqual(saved["player_summoner_name"], "Me")
        self.assertEqual(saved["local_player_champion"], "Ahri")
        self.assertEqual(saved["total_kills"], 2)
        first, second = saved["events"]
        self.assertEqual(first["killer_name"], "Me")
        self.assertEqual(first["victim_name"], "Them")
        self.assertEqual(first["killer_champion"], "Ahri")
        self.assertEqual(first["victim_champion"], "Zed")
        self.assertEqual(first["game_time"], 80.5)
        self.assertEqual(first["event_id"], 2)
        self.assertEqual(second["killer_name"], "Me")
        self.assertEqual(second["game_time"], 90)
        self.assertTrue(any("Tracking kills for: Me" in line for line in logs))
        self.assertFalse(any("GameStart" in event["type"] for event in saved["events"]))

    def test_unknown_ids_and_json_string_events(self):
        payload = _live([])
        payload["events"] = json.dumps(
            {
                "Events": [
                    {
                        "EventID": 7,
                        "EventName": "ChampionKill",
                        "EventTime": 12,
                        "KillerID": 9,
                        "VictimID": 8,
                    }
                ]
            }
        )
        path, _logs = self._run([payload], self._config())
        saved = self._saved(path)
        self.assertEqual(saved["events"][0]["killer_name"], "Unknown#9")
        self.assertEqual(saved["events"][0]["victim_name"], "Unknown#8")
        self.assertIsNone(saved["player_summoner_name"])

    def test_bad_events_blob_and_game_start_only_save_nothing(self):
        bad = _live([])
        bad["events"] = "not-json"
        path, logs = self._run([bad], self._config())
        self.assertIsNone(path)
        self.assertEqual(list(self.log_dir.glob("events_*.json")), [])
        self.assertTrue(any("No kill events" in line for line in logs))

        path, _logs = self._run(
            [_live([{"EventID": 1, "EventName": "GameStart", "EventTime": 1}])],
            self._config(),
        )
        self.assertIsNone(path)

    def test_duplicate_event_ids_are_ignored_until_reconnect(self):
        game = _live([_kill()])
        path, _logs = self._run([game, game], self._config())
        self.assertEqual(self._saved(path)["total_kills"], 1)

        path, logs = self._run([game, None, game], self._config())
        self.assertEqual(self._saved(path)["total_kills"], 2)
        self.assertTrue(any("New game detected" in line for line in logs))

    def test_config_summoner_is_used_when_the_argument_is_blank(self):
        path, _logs = self._run([_live([_kill()])], self._config(summoner="ConfigName#TAG"), summoner="  ")
        self.assertEqual(self._saved(path)["player_summoner_name"], "ConfigName")

    def test_alias_player_fields_fill_champion_and_local_player(self):
        players = [{"participantID": 3, "gameName": "Player#TAG", "champion_name": "Jinx"}]
        payload = _live(
            [_kill(KillerID=3, VictimID=None, VictimName="Them")],
            players=players,
            active={"riotId": "Player#TAG"},
        )
        path, _logs = self._run([payload], self._config())
        saved = self._saved(path)
        self.assertEqual(saved["events"][0]["killer_name"], "Player")
        self.assertEqual(saved["events"][0]["killer_champion"], "Jinx")
        self.assertEqual(saved["local_player_champion"], "Jinx")

    def test_game_start_and_end_drive_obs_once(self):
        obs = _Obs()
        payload = _live(
            [
                {"EventID": 1, "EventName": "GameStart", "EventTime": 0},
                _kill(),
                {"EventID": 4, "EventName": "GameEnd", "EventTime": 1200},
            ],
            game_time=30,
        )
        path, logs = self._run(
            [payload],
            self._config(auto_start_recording=True, auto_stop_recording_on_game_end=True),
            obs=obs,
        )
        self.assertEqual(obs.starts, 1)
        self.assertEqual(obs.stops, 1)
        self.assertEqual(self._saved(path)["total_kills"], 1)
        self.assertTrue(any("Stop Record (GameEnd event)" in line for line in logs))

    def test_already_recording_skips_auto_start(self):
        obs = _Obs(active=True)
        _path, logs = self._run(
            [_live([{"EventID": 1, "EventName": "GameStart", "EventTime": 0}], game_time=1)],
            self._config(auto_start_recording=True),
            obs=obs,
        )
        self.assertEqual(obs.starts, 0)
        self.assertTrue(any("already recording" in line for line in logs))

    def test_mid_game_join_starts_when_game_start_was_missed(self):
        obs = _Obs()
        _path, logs = self._run(
            [_live([], game_time=12)],
            self._config(auto_start_recording=True, mid_game_join_recording_game_time_sec=5),
            obs=obs,
        )
        self.assertEqual(obs.starts, 1)
        self.assertTrue(any("GameStart may have been missed" in line for line in logs))

    def test_delayed_start_waits_then_fires_while_the_client_is_up(self):
        obs = _Obs()
        clock = {"t": 1000.0}
        payload = _live([{"EventID": 1, "EventName": "GameStart", "EventTime": 0}, _kill()], game_time=20)

        def fetch_clock():
            return clock["t"]

        state = {"i": 0}
        stop = FastEvent()

        def fetch():
            index = state["i"]
            state["i"] += 1
            if index == 1:
                clock["t"] = 1006.0
            if index >= 1:
                stop.set()
            return payload

        logs = []
        with (
            patch("game_events_logger._load_config", return_value=self._config(
                auto_start_recording=True, obs_recording_start_delay_seconds=5
            )),
            patch("game_events_logger._connect_obs", return_value=(obs, None)),
            patch("game_events_logger.fetch_live_data", side_effect=fetch),
            patch("game_events_logger.urllib.request.urlopen", side_effect=AssertionError("network")),
            patch("game_events_logger.time.time", side_effect=fetch_clock),
        ):
            run_session(stop_event=stop, log=logs.append)
        self.assertEqual(obs.starts, 1)
        self.assertTrue(any("Start Record in 5s (GameStart)" in line for line in logs))

    def test_delayed_start_is_cancelled_when_the_client_drops(self):
        obs = _Obs()
        clock = {"t": 1000.0}
        state = {"i": 0}
        stop = FastEvent()
        game = _live([{"EventID": 1, "EventName": "GameStart", "EventTime": 0}], game_time=20)

        def fetch():
            index = state["i"]
            state["i"] += 1
            if index == 0:
                return game
            clock["t"] = 1010.0
            stop.set()
            return None

        logs = []
        with (
            patch("game_events_logger._load_config", return_value=self._config(
                auto_start_recording=True, obs_recording_start_delay_seconds=5
            )),
            patch("game_events_logger._connect_obs", return_value=(obs, None)),
            patch("game_events_logger.fetch_live_data", side_effect=fetch),
            patch("game_events_logger.urllib.request.urlopen", side_effect=AssertionError("network")),
            patch("game_events_logger.time.time", side_effect=lambda: clock["t"]),
        ):
            run_session(stop_event=stop, log=logs.append)
        self.assertEqual(obs.starts, 0)
        self.assertTrue(any("delayed Start Record skipped" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
