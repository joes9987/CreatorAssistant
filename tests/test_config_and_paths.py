"""Config, paths, and timing helpers."""

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml

import app_paths
import main
from app_paths import project_root
from detect import load_config
from gui.settings_store import get_resolved_default_input_dir, load_gui_settings, save_gui_settings
from main import default_recordings_dir
from timer_utils import emit_log, format_elapsed, iter_with_timer


class ConfigTests(unittest.TestCase):
    def test_example_config_has_the_clip_and_detection_contract(self):
        path = Path(__file__).resolve().parent.parent / "config.example.yaml"
        loaded = load_config(str(path))
        self.assertEqual(loaded["clip"]["aspect_ratio"], "9:16")
        self.assertEqual(loaded["clip"]["reframe_mode"], "crop")
        self.assertEqual(loaded["clip"]["crop_aspect"], "10:16")
        self.assertEqual(loaded["clip"]["padding_before"], 10)
        self.assertEqual(loaded["clip"]["padding_after"], 2)
        self.assertIn("min_score", loaded["detection"])
        self.assertIn("player_summoner_name", loaded["game_events"])
        self.assertEqual(loaded["paths"]["default_input_dir"], "input")

    def test_round_trip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(yaml.safe_dump({"detection": {"min_score": 0.4}}))
            self.assertEqual(load_config(str(path))["detection"]["min_score"], 0.4)
            with self.assertRaises(FileNotFoundError):
                load_config(str(Path(tmp) / "missing.yaml"))


class PathTests(unittest.TestCase):
    def test_project_root_follows_the_script_or_the_frozen_executable(self):
        self.assertEqual(project_root(), Path(app_paths.__file__).resolve().parent)
        with (
            patch("app_paths.sys.frozen", True, create=True),
            patch("app_paths.sys.executable", "/opt/CreatorAssistant.exe"),
        ):
            self.assertEqual(project_root(), Path("/opt").resolve())

    def test_default_recordings_dir_resolves_relative_paths_from_the_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("main.project_root", return_value=root):
                resolved = default_recordings_dir({"paths": {"default_input_dir": "input"}})
                absolute = default_recordings_dir({"paths": {"default_input_dir": str(root / "raw")}})
                fallback = default_recordings_dir({})
        self.assertEqual(resolved, (root / "input").resolve())
        self.assertEqual(absolute, (root / "raw").resolve())
        self.assertEqual(fallback, (root / "input").resolve())
        self.assertTrue(callable(main.default_recordings_dir))

    def test_gui_settings_ignore_corrupt_files_and_resolve_relative_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("gui.settings_store.project_root", return_value=root):
                self.assertEqual(
                    load_gui_settings(),
                    {"default_input_dir": str(root / "input")},
                )
                (root / "gui_settings.json").write_text("{", encoding="utf-8")
                self.assertEqual(load_gui_settings()["default_input_dir"], str(root / "input"))
                (root / "gui_settings.json").write_text("[]", encoding="utf-8")
                self.assertEqual(load_gui_settings()["default_input_dir"], str(root / "input"))
                save_gui_settings({"default_input_dir": "captures"})
                self.assertEqual(
                    json.loads((root / "gui_settings.json").read_text(encoding="utf-8"))["default_input_dir"],
                    "captures",
                )
                self.assertEqual(get_resolved_default_input_dir(), (root / "captures").resolve())


class TimerTests(unittest.TestCase):
    def test_format_elapsed(self):
        self.assertEqual(format_elapsed(0), "0:00")
        self.assertEqual(format_elapsed(59), "0:59")
        self.assertEqual(format_elapsed(65), "1:05")
        self.assertEqual(format_elapsed(3600), "1:00:00")
        self.assertEqual(format_elapsed(3661), "1:01:01")

    def test_emit_log_uses_callback_or_stdout(self):
        logs = []
        buffer = StringIO()
        with patch("sys.stdout", buffer):
            emit_log(logs.append, "hidden")
            emit_log(None, "shown")
        self.assertEqual(logs, ["hidden"])
        self.assertNotIn("hidden", buffer.getvalue())
        self.assertIn("shown", buffer.getvalue())

    def test_iter_with_timer_yields_every_item(self):
        buffer = StringIO()
        with patch("timer_utils.sys.stdout", buffer):
            self.assertEqual(list(iter_with_timer(["a", "b"], "Scan")), ["a", "b"])
        self.assertIn("Done", buffer.getvalue())
        self.assertIn("2 samples", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
