"""Clip geometry and FFmpeg arguments. subprocess is mocked, so nothing is encoded."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extract import (
    _build_vertical_filter,
    _detect_hw_encoder,
    _output_dimensions,
    _parse_aspect,
    extract_all_clips,
    extract_clip,
)


class AspectTests(unittest.TestCase):
    def test_parse_aspect(self):
        self.assertEqual(_parse_aspect("9:16"), (9, 16))
        self.assertEqual(_parse_aspect(" 10 : 16 "), (10, 16))
        for bad in ("9/16", "0:16", "-1:16", "9:16:9", "", "wide"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _parse_aspect(bad)

    def test_output_dimensions_are_even(self):
        self.assertEqual(_output_dimensions("9:16"), (1080, 1920))
        self.assertEqual(_output_dimensions("10:16"), (1080, 1728))
        self.assertEqual(_output_dimensions("9:16", base_width=1081), (1080, 1920))
        self.assertEqual(_output_dimensions("2:3", base_width=10), (10, 14))

    def test_vertical_filter(self):
        fit = _build_vertical_filter("fit", 1080, 1920, 9, 16)
        self.assertTrue(fit.startswith("scale=1080:1920:"))
        self.assertNotIn("crop=", fit)
        self.assertIn("format=yuv420p", fit)

        crop = _build_vertical_filter("crop", 1080, 1920, 10, 16)
        self.assertIn("ih*10/16", crop)
        self.assertIn("iw*16/10", crop)
        self.assertTrue(crop.startswith("crop="))
        self.assertIn("scale=1080:1920:", crop)
        with self.assertRaises(ValueError):
            _build_vertical_filter("Fit", 1080, 1920, 9, 16)


class ExtractClipCommandTests(unittest.TestCase):
    def test_crop_command_uses_seek_duration_and_libx264(self):
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "clip.mp4")
            with patch("extract.subprocess.run") as run:
                run.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
                ok = extract_clip(
                    "source.mp4",
                    12.5,
                    40,
                    output,
                    aspect_ratio="9:16",
                    ffmpeg_path="/bin/ffmpeg",
                    crf=23,
                    preset="fast",
                    reframe_mode="crop",
                    crop_aspect="10:16",
                    log=logs.append,
                )
        self.assertTrue(ok)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "/bin/ffmpeg")
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-ss") + 1], "12.5")
        self.assertEqual(cmd[cmd.index("-i") + 1], "source.mp4")
        self.assertEqual(cmd[cmd.index("-t") + 1], "27.5")
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("scale=1080:1920:", vf)
        self.assertIn("ih*10/16", vf)
        self.assertIn("-c:v", cmd)
        self.assertIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-preset") + 1], "fast")
        self.assertEqual(cmd[cmd.index("-crf") + 1], "23")
        self.assertIn("aac", cmd)
        self.assertEqual(cmd[cmd.index("-b:a") + 1], "192k")
        self.assertEqual(cmd[-1], str(Path(output).resolve()))
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_nvenc_args_and_failure_logging(self):
        logs = []
        with patch("extract.subprocess.run") as run:
            run.return_value = type("R", (), {"returncode": 1, "stderr": "a\nb\nc\nd\n"})()
            ok = extract_clip(
                "source.mp4",
                0,
                20,
                "out.mp4",
                video_encoder="h264_nvenc",
                crf=18,
                log=logs.append,
            )
        self.assertFalse(ok)
        cmd = run.call_args.args[0]
        self.assertIn("h264_nvenc", cmd)
        self.assertNotIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-cq") + 1], "18")
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "0")
        self.assertTrue(any("b c d" in line for line in logs))

    def test_invalid_aspect_and_ffmpeg_errors_return_false(self):
        logs = []
        with patch("extract.subprocess.run") as run:
            ok = extract_clip("source.mp4", 0, 10, "out.mp4", aspect_ratio="nope", log=logs.append)
        self.assertFalse(ok)
        run.assert_not_called()
        self.assertTrue(any("Invalid clip aspect" in line for line in logs))

        with patch("extract.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 120)):
            ok = extract_clip("source.mp4", 0, 10, "out.mp4", log=logs.append)
        self.assertFalse(ok)
        self.assertTrue(any("Error extracting clip" in line for line in logs))


class HardwareEncoderTests(unittest.TestCase):
    def test_detects_nvenc_and_swallows_probe_failures(self):
        with patch("extract.subprocess.run") as run:
            run.return_value = type("R", (), {"stdout": "V.... h264_nvenc\n", "stderr": ""})()
            self.assertEqual(_detect_hw_encoder("ffmpeg"), "h264_nvenc")
            run.return_value = type("R", (), {"stdout": "libx264\n", "stderr": ""})()
            self.assertIsNone(_detect_hw_encoder("ffmpeg"))
            run.side_effect = subprocess.TimeoutExpired("ffmpeg", 5)
            self.assertIsNone(_detect_hw_encoder("ffmpeg"))


class ExtractAllTests(unittest.TestCase):
    def _config(self, out, **perf):
        performance = {"extract_parallel_workers": 2, "use_hw_encoder": False}
        performance.update(perf)
        return {
            "clip": {
                "output_dir": out,
                "aspect_ratio": "9:16",
                "crop_aspect": "10:16",
                "crf": 20,
                "preset": "slow",
                "reframe_mode": "crop",
            },
            "performance": performance,
        }

    def test_names_clips_skips_existing_and_keeps_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "outputs")
            existing = Path(out)
            existing.mkdir()
            (existing / "game_clip_01.mp4").write_bytes(b"done")
            seen = []

            def fake_extract(*args, **kwargs):
                seen.append((args[1], args[3], kwargs["reframe_mode"], kwargs["crop_aspect"], args[6], args[7]))
                Path(args[3]).write_bytes(b"new")
                return True

            highlights = [{"start": 1, "end": 20}, {"start": 200, "end": 220}]
            with (
                patch("extract._get_ffmpeg_bin", return_value=("/bin/ffmpeg", "/bin/ffprobe")),
                patch("extract.extract_clip", side_effect=fake_extract),
            ):
                paths = extract_all_clips(
                    "/videos/source.mp4",
                    highlights,
                    base_name="game",
                    config=self._config(out),
                    log=lambda _message: None,
                )
        self.assertEqual([Path(p).name for p in paths], ["game_clip_01.mp4", "game_clip_02.mp4"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 200)
        self.assertEqual(seen[0][2:], ("crop", "10:16", 20, "slow"))

    def test_parallel_success_preserves_highlight_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            highlights = [{"start": 1, "end": 20}, {"start": 80, "end": 100}, {"start": 200, "end": 230}]

            def fake_extract(*args, **_kwargs):
                Path(args[3]).write_bytes(b"ok")
                return True

            with (
                patch("extract._get_ffmpeg_bin", return_value=("/bin/ffmpeg", "/bin/ffprobe")),
                patch("extract.extract_clip", side_effect=fake_extract),
            ):
                paths = extract_all_clips(
                    "match.mkv",
                    highlights,
                    config=self._config(tmp),
                    log=lambda _message: None,
                )
        self.assertEqual([Path(p).name for p in paths], ["match_clip_01.mp4", "match_clip_02.mp4", "match_clip_03.mp4"])

    def test_failed_clip_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fake_extract(*args, **_kwargs):
                return args[1] == 1

            with (
                patch("extract._get_ffmpeg_bin", return_value=("/bin/ffmpeg", "/bin/ffprobe")),
                patch("extract.extract_clip", side_effect=fake_extract),
            ):
                paths = extract_all_clips(
                    "match.mp4",
                    [{"start": 1, "end": 20}, {"start": 80, "end": 100}],
                    config=self._config(tmp, extract_parallel_workers=1),
                    log=lambda _message: None,
                )
        self.assertEqual([Path(p).name for p in paths], ["match_clip_01.mp4"])

    def test_nvenc_failure_retries_with_software(self):
        calls = []

        def fake_extract(*args, **_kwargs):
            encoder = args[8]
            calls.append(encoder)
            return encoder is None

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("extract._get_ffmpeg_bin", return_value=("/bin/ffmpeg", "/bin/ffprobe")),
                patch("extract._detect_hw_encoder", return_value="h264_nvenc"),
                patch("extract.extract_clip", side_effect=fake_extract),
            ):
                paths = extract_all_clips(
                    "match.mp4",
                    [{"start": 5, "end": 25}],
                    config=self._config(tmp, use_hw_encoder="auto", extract_parallel_workers=1),
                    log=lambda _message: None,
                )
        self.assertEqual(calls, ["h264_nvenc", None])
        self.assertEqual(len(paths), 1)


if __name__ == "__main__":
    unittest.main()
