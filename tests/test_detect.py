"""Highlight detection and event-log matching. No video decode, GPU, or network."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import detect
from detect import (
    _get_ffmpeg_bin,
    compute_audio_energy,
    compute_motion_scores,
    detect_highlights,
    extract_audio,
    get_matching_events_path,
    get_video_info,
    load_highlights_from_events,
    normalize_scores,
)


def _clip_config(**overrides):
    clip = {"padding_before": 10, "padding_after": 8, "min_clip_length": 15}
    clip.update(overrides)
    return clip


def _detection_config(**overrides):
    detection = {
        "audio_weight": 1.0,
        "motion_weight": 0.0,
        "sensitivity": 1.0,
        "min_score": 0.6,
        "min_prominence": 0.15,
        "min_seconds_between_clips": 30,
        "max_clips_per_video": 5,
        "window_seconds": 10,
    }
    detection.update(overrides)
    return detection


class NormalizeTests(unittest.TestCase):
    def test_scales_to_unit_interval(self):
        scaled = normalize_scores(np.array([2.0, 4.0, 6.0]))
        np.testing.assert_allclose(scaled, [0.0, 0.5, 1.0])

    def test_flat_and_empty_become_zeros(self):
        np.testing.assert_allclose(normalize_scores(np.array([3.0, 3.0, 3.0])), [0.0, 0.0, 0.0])
        np.testing.assert_allclose(normalize_scores(np.array([])), [])


class AudioEnergyTests(unittest.TestCase):
    def test_requires_librosa(self):
        with patch.object(detect, "librosa", None):
            with self.assertRaises(ImportError):
                compute_audio_energy(np.zeros(8), 16000)

    def test_downsamples_rms_into_windows_and_passes_hop(self):
        seen = {}

        def rms(y, hop_length):
            seen["hop"] = hop_length
            seen["samples"] = len(y)
            return np.array([y], dtype=float)

        fake = type("Librosa", (), {})()
        fake.feature = type("Feature", (), {"rms": staticmethod(rms)})()
        audio = np.arange(20, dtype=float)
        with patch.object(detect, "librosa", fake):
            energy = compute_audio_energy(audio, sr=100, window_seconds=5.0)
        self.assertEqual(seen["hop"], 50)
        self.assertEqual(seen["samples"], 20)
        np.testing.assert_allclose(energy, [4.5, 14.5])

    def test_short_rms_is_averaged_when_shorter_than_one_window_hop_budget(self):
        def rms(y, hop_length):
            return np.array([y], dtype=float)

        fake = type("Librosa", (), {})()
        fake.feature = type("Feature", (), {"rms": staticmethod(rms)})()
        with patch.object(detect, "librosa", fake):
            energy = compute_audio_energy(np.array([0.0, 1.0, 2.0]), sr=100, window_seconds=5.0)
        np.testing.assert_allclose(energy, [1.0])

    def test_returns_rms_unchanged_when_already_one_frame(self):
        def rms(y, hop_length):
            return np.array([[7.0]])

        fake = type("Librosa", (), {})()
        fake.feature = type("Feature", (), {"rms": staticmethod(rms)})()
        with patch.object(detect, "librosa", fake):
            energy = compute_audio_energy(np.array([1.0]), sr=100, window_seconds=5.0)
        np.testing.assert_allclose(energy, [7.0])


class _FakeCap:
    def __init__(self, frames, opened=True):
        self.frames = frames
        self.opened = opened
        self.sets = []
        self.released = False
        self.pos = 0

    def isOpened(self):
        return self.opened

    def set(self, _prop, value):
        self.sets.append(value)
        self.pos = int(value)
        return True

    def read(self):
        if self.pos < 0 or self.pos >= len(self.frames):
            return False, None
        return True, self.frames[self.pos]

    def release(self):
        self.released = True


class _FakeCV2:
    CAP_PROP_POS_FRAMES = 1
    COLOR_BGR2GRAY = 0

    def __init__(self, frames, opened=True):
        self.frames = frames
        self.opened = opened
        self.path = None
        self.cap = None

    def VideoCapture(self, path):
        self.path = path
        self.cap = _FakeCap(self.frames, self.opened)
        return self.cap

    @staticmethod
    def cvtColor(frame, _code):
        return frame[..., 0]

    @staticmethod
    def resize(gray, _size):
        return gray

    @staticmethod
    def absdiff(a, b):
        return np.abs(a.astype(np.float64) - b.astype(np.float64))


class MotionScoreTests(unittest.TestCase):
    def test_requires_opencv(self):
        with patch.object(detect, "cv2", None):
            with self.assertRaises(ImportError):
                compute_motion_scores("video.mp4", 10, 30)

    def test_unopened_capture_returns_zero_windows(self):
        fake = _FakeCV2([], opened=False)
        with patch.object(detect, "cv2", fake):
            scores = compute_motion_scores("clip.mp4", duration=10, fps=30, window_seconds=5)
        self.assertEqual(fake.path, "clip.mp4")
        np.testing.assert_allclose(scores, [0.0, 0.0, 0.0])

    def test_too_few_samples_returns_a_single_zero(self):
        frame = np.zeros((2, 2, 3))
        fake = _FakeCV2([frame])
        with patch.object(detect, "cv2", fake):
            scores = compute_motion_scores("clip.mp4", duration=1, fps=1, window_seconds=5)
        np.testing.assert_allclose(scores, [0.0])
        self.assertTrue(fake.cap.released)

    def test_buckets_frame_differences_and_seeks_each_sample(self):
        frames = [
            np.zeros((2, 2, 3)),
            np.full((2, 2, 3), 10),
            np.full((2, 2, 3), 10),
            np.zeros((2, 2, 3)),
        ]
        fake = _FakeCV2(frames)
        with patch.object(detect, "cv2", fake):
            scores = compute_motion_scores(
                "clip.mp4",
                duration=4,
                fps=1,
                window_seconds=2,
                sample_interval_sec=1,
            )
        self.assertEqual(fake.cap.sets, [0, 1, 2, 3])
        # 3 motion samples over 2 windows: the tail sample is not its own window.
        np.testing.assert_allclose(scores, [10.0, 0.0])


class VideoInfoTests(unittest.TestCase):
    def _probe(self, duration_out, fps_out, ffprobe="ffprobe"):
        def run(cmd, **_kwargs):
            self.assertEqual(cmd[0], ffprobe)
            joined = " ".join(cmd)
            if "format=duration" in joined:
                return type("R", (), {"stdout": duration_out, "stderr": ""})()
            if "r_frame_rate" in joined:
                return type("R", (), {"stdout": fps_out, "stderr": ""})()
            self.fail(f"unexpected ffprobe command: {cmd}")

        with patch("detect.subprocess.run", side_effect=run):
            return get_video_info("video.mp4", ffprobe_path=ffprobe)

    def test_parses_duration_and_fractional_fps(self):
        info = self._probe("125.5\n", "30000/1001\n", ffprobe="/opt/ffprobe")
        self.assertEqual(info["duration"], 125.5)
        self.assertAlmostEqual(info["fps"], 30000 / 1001)

    def test_plain_fps_and_blank_probe_output(self):
        info = self._probe("30\n", "30\n")
        self.assertEqual(info, {"duration": 30.0, "fps": 30.0})
        blank = self._probe("", "")
        self.assertEqual(blank, {"duration": 0.0, "fps": 30.0})

    def test_zero_denominator_fps_falls_back_to_30(self):
        info = self._probe("1\n", "0/0\n")
        self.assertEqual(info["fps"], 30)


class FfmpegBinTests(unittest.TestCase):
    def test_uses_configured_directory_without_checking_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            found = _get_ffmpeg_bin({"ffmpeg_path": tmp})
        self.assertEqual(found, (str(Path(tmp) / "ffmpeg"), str(Path(tmp) / "ffprobe")))

    def test_falls_back_to_path_when_configured_dir_is_missing(self):
        def which(name):
            return {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"}.get(name)

        with patch("detect.shutil.which", side_effect=which):
            found = _get_ffmpeg_bin({"ffmpeg_path": "/missing/ffmpeg/bin"})
        self.assertEqual(found, ("/usr/bin/ffmpeg", "/usr/bin/ffprobe"))

    def test_raises_when_ffmpeg_cannot_be_found(self):
        with patch("detect.shutil.which", return_value=None), patch("detect.os.name", "posix"):
            with self.assertRaises(FileNotFoundError) as caught:
                _get_ffmpeg_bin({})
        self.assertIn("FFmpeg not found", str(caught.exception))

    def test_partial_path_match_is_not_enough(self):
        def which(name):
            return "/usr/bin/ffmpeg" if name == "ffmpeg" else None

        with (
            patch("detect.shutil.which", side_effect=which),
            patch("detect.os.name", "posix"),
            patch("detect.os.path.exists", return_value=False),
        ):
            with self.assertRaises(FileNotFoundError):
                _get_ffmpeg_bin({"ffmpeg_path": "   "})


class EventHighlightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "rec.mp4"
        self.video.write_bytes(b"video")

    def tearDown(self):
        self.tmp.cleanup()

    def _ctime(self):
        return os.path.getctime(self.video)

    def _write(self, name, events, session_start=None, directory=None):
        path = (directory or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session_start": session_start, "events": events}))
        return path

    def _config(self, **game_events):
        ge = {
            "filter_my_kills_only": False,
            "recording_start_offset": 0,
            "prefer_events_over_ai": True,
            "log_dir": str(self.root / "eventlogs"),
        }
        ge.update(game_events)
        return {
            "game_events": ge,
            "clip": _clip_config(),
            "detection": _detection_config(),
        }

    def test_maps_wall_clock_to_video_time_with_padding_and_offset(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [{"type": "ChampionKill", "wall_clock": ctime + 100, "killer_name": "Player"}],
        )
        config = self._config(recording_start_offset=5)
        clips = load_highlights_from_events(str(path), str(self.video), config)
        self.assertEqual(
            clips,
            [{"start": 95.0, "end": 113.0, "score": 1.0, "source": "game_events"}],
        )

    def test_missing_wall_clock_uses_game_time_plus_session_start(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [{"type": "ChampionKill", "game_time": 40}],
            session_start=ctime + 10,
        )
        clips = load_highlights_from_events(str(path), str(self.video), self._config())
        self.assertEqual(clips[0]["start"], 40.0)
        self.assertEqual(clips[0]["end"], 58.0)

    def test_kill_before_file_ctime_clamps_start_only(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [{"type": "ChampionKill", "wall_clock": ctime - 50}],
        )
        clips = load_highlights_from_events(str(path), str(self.video), self._config())
        self.assertEqual(clips[0]["start"], 0)
        self.assertLess(clips[0]["end"], 0)

    def test_filters_to_summoner_and_strips_config_tag(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [
                {"type": "ChampionKill", "wall_clock": ctime + 30, "killer_name": "Player"},
                {"type": "ChampionKill", "wall_clock": ctime + 400, "killer_name": "Other"},
                {"type": "TurretKill", "wall_clock": ctime + 40},
            ],
        )
        config = self._config(filter_my_kills_only=True, player_summoner_name="  Player#NA1  ")
        clips = load_highlights_from_events(str(path), str(self.video), config)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["start"], 20.0)

    def test_killer_name_falls_back_to_nested_payload(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [
                {
                    "type": "ChampionKill",
                    "wall_clock": ctime + 30,
                    "data": {"KillerName": "Player"},
                },
                {
                    "type": "ChampionKill",
                    "wall_clock": ctime + 400,
                    "killer_name": "",
                    "data": {"killerName": "Player"},
                },
            ],
        )
        config = self._config(filter_my_kills_only=True, player_summoner_name="player")
        clips = load_highlights_from_events(str(path), str(self.video), config)
        self.assertEqual(len(clips), 2)

    def test_null_or_non_dict_payload_does_not_crash_filter(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [
                {"type": "ChampionKill", "wall_clock": ctime + 30, "killer_name": "Player", "data": None},
                {"type": "ChampionKill", "wall_clock": ctime + 400, "data": "nope"},
            ],
        )
        config = self._config(filter_my_kills_only=True, player_summoner_name="Player")
        clips = load_highlights_from_events(str(path), str(self.video), config)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["start"], 20.0)

    def test_blank_summoner_with_filter_enabled_keeps_every_kill(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [
                {"type": "ChampionKill", "wall_clock": ctime + 30, "killer_name": "A"},
                {"type": "ChampionKill", "wall_clock": ctime + 400, "killer_name": "B"},
            ],
        )
        config = self._config(filter_my_kills_only=True, player_summoner_name="  ")
        clips = load_highlights_from_events(str(path), str(self.video), config)
        self.assertEqual(len(clips), 2)

    def test_no_matching_killer_returns_none(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [{"type": "ChampionKill", "wall_clock": ctime + 30, "killer_name": "Other"}],
        )
        config = self._config(filter_my_kills_only=True, player_summoner_name="Player")
        self.assertIsNone(load_highlights_from_events(str(path), str(self.video), config))

    def test_spacing_and_max_clips_keep_earlier_kills(self):
        ctime = self._ctime()
        path = self._write(
            "session.json",
            [
                {"type": "ChampionKill", "wall_clock": ctime + 100},
                {"type": "ChampionKill", "wall_clock": ctime + 150},
                {"type": "ChampionKill", "wall_clock": ctime + 250},
                {"type": "ChampionKill", "wall_clock": ctime + 400},
            ],
        )
        config = self._config()
        config["detection"]["min_seconds_between_clips"] = 120
        config["detection"]["max_clips_per_video"] = 2
        clips = load_highlights_from_events(str(path), str(self.video), config)
        self.assertEqual([c["start"] for c in clips], [90.0, 240.0])

    def test_missing_bad_and_killless_files_return_none(self):
        self.assertIsNone(load_highlights_from_events(str(self.root / "nope.json"), str(self.video), self._config()))
        bad = self.root / "bad.json"
        bad.write_text("{")
        self.assertIsNone(load_highlights_from_events(str(bad), str(self.video), self._config()))
        empty = self._write("empty.json", [{"type": "GameStart"}])
        self.assertIsNone(load_highlights_from_events(str(empty), str(self.video), self._config()))

    def _match(self, config, duration=100.0):
        with (
            patch("detect.project_root", return_value=self.root),
            patch("detect._get_ffmpeg_bin", return_value=("ffmpeg", "ffprobe")),
            patch("detect.get_video_info", return_value={"duration": duration, "fps": 30.0}),
        ):
            return get_matching_events_path(str(self.video), config)

    def test_picks_log_with_most_kills_inside_the_recording_window(self):
        ctime = self._ctime()
        log_dir = self.root / "eventlogs"
        self._write(
            "events_a.json",
            [
                {"type": "ChampionKill", "wall_clock": ctime + 10},
                {"type": "ChampionKill", "wall_clock": ctime + 20},
                {"type": "ChampionKill", "wall_clock": ctime + 500},
            ],
            directory=log_dir,
        )
        self._write(
            "events_b.json",
            [
                {"type": "ChampionKill", "wall_clock": ctime + 10},
                {"type": "ChampionKill", "wall_clock": ctime - 5},
            ],
            directory=log_dir,
        )
        corrupt = log_dir / "events_c.json"
        corrupt.write_text("{")
        chosen = self._match(self._config())
        self.assertEqual(Path(chosen).name, "events_a.json")

    def test_tie_keeps_the_lexicographically_latest_filename(self):
        ctime = self._ctime()
        log_dir = self.root / "eventlogs"
        for name in ("events_a.json", "events_b.json"):
            self._write(
                name,
                [{"type": "ChampionKill", "wall_clock": ctime + 10}],
                directory=log_dir,
            )
        chosen = self._match(self._config())
        self.assertEqual(Path(chosen).name, "events_b.json")

    def test_window_is_inclusive_and_session_start_fills_missing_wall_clock(self):
        ctime = self._ctime()
        log_dir = self.root / "eventlogs"
        self._write(
            "events_edge.json",
            [{"type": "ChampionKill", "wall_clock": ctime + 100}],
            directory=log_dir,
        )
        self._write(
            "events_outside.json",
            [{"type": "ChampionKill", "wall_clock": ctime + 100.001}],
            directory=log_dir,
        )
        chosen = self._match(self._config(), duration=100)
        self.assertEqual(Path(chosen).name, "events_edge.json")

        for path in log_dir.glob("events_*.json"):
            path.unlink()
        self._write(
            "events_fallback.json",
            [{"type": "ChampionKill", "game_time": 50}],
            session_start=ctime,
            directory=log_dir,
        )
        self._write(
            "events_outside.json",
            [{"type": "ChampionKill", "wall_clock": ctime - 1}],
            directory=log_dir,
        )
        chosen = self._match(self._config(), duration=100)
        self.assertEqual(Path(chosen).name, "events_fallback.json")

    def test_missing_log_dir_uses_legacy_file_beside_project_root(self):
        legacy = self.root / "game_events.json"
        legacy.write_text("{}")
        config = self._config(log_dir="missing-logs", file="nested/game_events.json")
        with patch("detect.project_root", return_value=self.root), patch(
            "detect.get_video_info", side_effect=AssertionError("probe")
        ):
            chosen = get_matching_events_path(str(self.video), config)
        self.assertEqual(Path(chosen), legacy)

        legacy.unlink()
        with patch("detect.project_root", return_value=self.root):
            self.assertIsNone(get_matching_events_path(str(self.video), config))

    def test_empty_log_dir_falls_back_to_legacy(self):
        (self.root / "eventlogs").mkdir()
        legacy = self.root / "game_events.json"
        legacy.write_text("{}")
        chosen = self._match(self._config(log_dir="eventlogs", file="game_events.json"))
        self.assertEqual(Path(chosen), legacy)

    def test_unmatched_event_files_do_not_fall_back_to_legacy(self):
        ctime = self._ctime()
        log_dir = self.root / "eventlogs"
        self._write(
            "events_old.json",
            [{"type": "ChampionKill", "wall_clock": ctime + 10_000}],
            directory=log_dir,
        )
        (self.root / "game_events.json").write_text("{}")
        self.assertIsNone(self._match(self._config()))

    def test_missing_video_uses_ctime_zero(self):
        log_dir = self.root / "eventlogs"
        self._write(
            "events_old.json",
            [{"type": "ChampionKill", "wall_clock": 10}],
            directory=log_dir,
        )
        missing = self.root / "gone.mp4"
        with (
            patch("detect.project_root", return_value=self.root),
            patch("detect._get_ffmpeg_bin", return_value=("ffmpeg", "ffprobe")),
            patch("detect.get_video_info", return_value={"duration": 100.0, "fps": 30.0}),
        ):
            chosen = get_matching_events_path(str(missing), self._config())
        self.assertEqual(Path(chosen).name, "events_old.json")

    def test_absolute_log_dir_is_not_joined_to_the_project_root(self):
        ctime = self._ctime()
        elsewhere = self.root / "elsewhere"
        self._write(
            "events_abs.json",
            [{"type": "ChampionKill", "wall_clock": ctime + 5}],
            directory=elsewhere,
        )
        other_root = self.root / "not-the-project"
        other_root.mkdir()
        with (
            patch("detect.project_root", return_value=other_root),
            patch("detect._get_ffmpeg_bin", return_value=("ffmpeg", "ffprobe")),
            patch("detect.get_video_info", return_value={"duration": 100.0, "fps": 30.0}),
        ):
            chosen = get_matching_events_path(str(self.video), self._config(log_dir=str(elsewhere)))
        self.assertEqual(Path(chosen).name, "events_abs.json")


class DetectHighlightsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "rec.mp4"
        self.video.write_bytes(b"video")

    def tearDown(self):
        self.tmp.cleanup()

    def _ai(self, energy, motion, config, duration=None, events_file=None):
        window = config["detection"]["window_seconds"]
        if duration is None:
            duration = len(energy) * window
        with (
            patch("detect._get_ffmpeg_bin", return_value=("ffmpeg", "ffprobe")),
            patch("detect.get_video_info", return_value={"duration": duration, "fps": 30.0}),
            patch("detect.extract_audio", return_value=(np.zeros(4), 11025)) as audio,
            patch("detect.compute_audio_energy", return_value=np.asarray(energy, dtype=float)),
            patch("detect.compute_motion_scores", return_value=np.asarray(motion, dtype=float)),
        ):
            clips = detect_highlights(str(self.video), config, events_file=events_file)
        return clips, audio

    def test_keeps_separated_peaks_and_respects_score_order_for_the_cap(self):
        energy = np.zeros(10)
        energy[2] = 0.7
        energy[6] = 1.0
        config = {
            "game_events": {"enabled": False},
            "detection": _detection_config(min_seconds_between_clips=1, max_clips_per_video=1),
            "clip": _clip_config(),
        }
        clips, _audio = self._ai(energy, np.zeros_like(energy), config)
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["start"], 50.0)
        self.assertEqual(clips[0]["end"], 68.0)
        self.assertEqual(clips[0]["score"], 1.0)

    def test_drops_nearby_lower_peak_and_low_prominence(self):
        energy = np.zeros(10)
        energy[2] = 1.0
        energy[4] = 0.8
        config = {
            "game_events": {"enabled": False},
            "detection": _detection_config(min_seconds_between_clips=30),
            "clip": _clip_config(),
        }
        clips, _audio = self._ai(energy, np.zeros_like(energy), config)
        self.assertEqual([c["start"] for c in clips], [10.0])

        flat = np.array([0.0, 0.5, 0.55, 0.5, 0.0])
        config["detection"]["min_score"] = 0.5
        clips, _audio = self._ai(flat, np.zeros_like(flat), config)
        self.assertEqual(clips, [])

    def test_sensitivity_zero_keeps_only_the_maximum(self):
        energy = np.zeros(10)
        energy[2] = 1.0
        energy[7] = 0.5
        config = {
            "game_events": {"enabled": False},
            "detection": _detection_config(sensitivity=0.0, min_seconds_between_clips=1),
            "clip": _clip_config(),
        }
        clips, _audio = self._ai(energy, np.zeros_like(energy), config)
        self.assertEqual([c["start"] for c in clips], [10.0])

    def test_mixes_audio_and_motion_weights(self):
        energy = np.array([0.0, 0.0, 1.0, 0.0, 0.0, 0.0])
        motion = np.array([0.0, 0.0, 0.0, 0.0, 1.0, 0.0])
        config = {
            "game_events": {"enabled": False},
            "detection": _detection_config(
                audio_weight=0.5,
                motion_weight=0.5,
                min_score=0.4,
                min_prominence=0.1,
                min_seconds_between_clips=5,
            ),
            "clip": _clip_config(),
        }
        clips, _audio = self._ai(energy, motion, config)
        self.assertEqual([(c["start"], c["score"]) for c in clips], [(10.0, 0.5), (30.0, 0.5)])

        # The scan skips the first and last windows, so a spike only there is invisible.
        endpoint = np.array([0.0, 0.0, 0.0, 1.0])
        config["detection"]["audio_weight"] = 1.0
        config["detection"]["motion_weight"] = 0.0
        clips, _audio = self._ai(endpoint, np.zeros_like(endpoint), config)
        self.assertEqual(clips, [])

    def test_short_clips_are_rejected_and_flat_padding_counts_as_a_peak(self):
        energy = np.zeros(10)
        energy[2] = 1.0
        config = {
            "game_events": {"enabled": False},
            "detection": _detection_config(),
            "clip": _clip_config(padding_before=2, padding_after=2, min_clip_length=15),
        }
        clips, _audio = self._ai(energy, np.zeros_like(energy), config)
        self.assertEqual(clips, [])

        config = {
            "game_events": {"enabled": False},
            "detection": _detection_config(min_seconds_between_clips=10_000),
            "clip": _clip_config(),
        }
        clips, _audio = self._ai(np.array([0.0, 1.0]), np.zeros(1), config, duration=50)
        self.assertEqual(clips, [{"start": 0.0, "end": 18.0, "score": 1.0}])

    def test_missing_video_raises_on_the_ai_path(self):
        config = {"game_events": {"enabled": False}, "detection": _detection_config(), "clip": _clip_config()}
        with self.assertRaises(FileNotFoundError):
            detect_highlights(str(self.root / "missing.mp4"), config)

    def test_event_kills_skip_audio_analysis(self):
        ctime = os.path.getctime(self.video)
        events = self.root / "events.json"
        events.write_text(
            json.dumps(
                {
                    "events": [{"type": "ChampionKill", "wall_clock": ctime + 40, "killer_name": "Player"}],
                }
            )
        )
        config = {
            "game_events": {
                "enabled": True,
                "prefer_events_over_ai": True,
                "filter_my_kills_only": True,
                "player_summoner_name": "Player",
            },
            "clip": _clip_config(),
            "detection": _detection_config(min_seconds_between_clips=120),
        }
        with patch("detect.extract_audio", side_effect=AssertionError("audio")), patch(
            "detect._get_ffmpeg_bin", side_effect=AssertionError("ffmpeg")
        ):
            clips = detect_highlights(str(self.video), config, events_file=str(events))
        self.assertEqual(clips[0]["source"], "game_events")
        self.assertEqual(clips[0]["start"], 30.0)

    def test_relative_events_file_is_found_beside_the_video(self):
        ctime = os.path.getctime(self.video)
        events = self.root / "session.json"
        events.write_text(json.dumps({"events": [{"type": "ChampionKill", "wall_clock": ctime + 40}]}))
        config = {
            "game_events": {"enabled": True, "prefer_events_over_ai": True, "filter_my_kills_only": False},
            "clip": _clip_config(),
            "detection": _detection_config(),
        }
        with patch("detect.project_root", return_value=self.root / "empty"):
            clips = detect_highlights(str(self.video), config, events_file="session.json")
        self.assertEqual(len(clips), 1)

    def test_killless_events_fall_through_to_ai(self):
        events = self.root / "session.json"
        events.write_text(json.dumps({"events": [{"type": "GameStart"}]}))
        energy = np.zeros(8)
        energy[3] = 1.0
        config = {
            "game_events": {"enabled": True, "prefer_events_over_ai": True},
            "detection": _detection_config(),
            "clip": _clip_config(),
        }
        clips, audio = self._ai(energy, np.zeros_like(energy), config, events_file=str(events))
        audio.assert_called_once()
        self.assertEqual(clips[0]["start"], 20.0)
        self.assertNotIn("source", clips[0])

        config["game_events"]["prefer_events_over_ai"] = False
        events.write_text(
            json.dumps(
                {
                    "events": [
                        {
                            "type": "ChampionKill",
                            "wall_clock": os.path.getctime(self.video) + 400,
                        }
                    ]
                }
            )
        )
        clips, audio = self._ai(energy, np.zeros_like(energy), config, events_file=str(events))
        audio.assert_called_once()
        self.assertEqual(clips[0]["start"], 20.0)


class ExtractAudioCommandTests(unittest.TestCase):
    def test_ffmpeg_command_and_temp_wav_cleanup(self):
        loaded = {}

        def load(path, sr, mono):
            loaded["exists_during_load"] = os.path.exists(path)
            loaded["path"] = path
            loaded["sr"] = sr
            loaded["mono"] = mono
            return np.array([0.25]), sr

        fake = type("Librosa", (), {"load": staticmethod(load)})()
        with (
            patch.object(detect, "librosa", fake),
            patch.object(detect, "sf", object()),
            patch("detect.subprocess.run", return_value=type("R", (), {"returncode": 0})()) as run,
        ):
            audio, sr = extract_audio("clip.mp4", sample_rate=11025, ffmpeg_path="/bin/ffmpeg")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], ["/bin/ffmpeg", "-y", "-i", "clip.mp4"])
        self.assertIn("pcm_s16le", cmd)
        self.assertEqual(cmd[cmd.index("-ar") + 1], "11025")
        self.assertEqual(cmd[cmd.index("-ac") + 1], "1")
        self.assertTrue(loaded["exists_during_load"])
        self.assertFalse(os.path.exists(loaded["path"]))
        self.assertEqual(sr, 11025)
        self.assertEqual(loaded["mono"], True)
        np.testing.assert_allclose(audio, [0.25])


if __name__ == "__main__":
    unittest.main()
