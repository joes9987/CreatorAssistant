"""Pipeline wiring. Upload clients are fake modules, so no platform API is called."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import main
import pipeline
from pipeline import clip_nums_for_upload_count, process_one_video, process_videos, run_uploads


class ImportTests(unittest.TestCase):
    def test_pipeline_and_main_do_not_import_the_gui(self):
        self.assertNotIn("ui_dialogs", sys.modules)
        self.assertNotIn("customtkinter", sys.modules)
        self.assertTrue(callable(main.default_recordings_dir))
        self.assertTrue(callable(pipeline.process_videos))


def _upload_modules():
    youtube = types.ModuleType("youtube_upload")
    youtube.upload_clips = MagicMock(return_value=(["yt-id"], [1]))
    tiktok = types.ModuleType("tiktok_upload")
    tiktok.upload_clips = MagicMock(return_value=(["tt-id"], [1]))
    instagram = types.ModuleType("instagram_upload")
    instagram.upload_clips = MagicMock(return_value=["ig-id"])
    return youtube, tiktok, instagram


class ClipNumberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _nums(self, config, count=2):
        with patch("pipeline.project_root", return_value=self.root):
            return clip_nums_for_upload_count(config, count)

    def test_single_platform_does_not_reserve_numbers(self):
        self.assertIsNone(self._nums({"youtube": {"enabled": True}, "tiktok": {"enabled": False}}))
        self.assertIsNone(self._nums({}))
        self.assertIsNone(self._nums({"instagram": {"enabled": True}}))

    def test_two_platforms_read_the_counter_file(self):
        (self.root / "clip_counter.txt").write_text("  4 \n")
        config = {
            "tiktok": {"enabled": True},
            "instagram": {"enabled": True},
            "youtube": {"clip_counter_start": 9},
        }
        self.assertEqual(self._nums(config, 3), [4, 5, 6])

    def test_missing_or_corrupt_counter_uses_the_start_value(self):
        config = {"youtube": {"enabled": True, "clip_counter_start": 7}, "tiktok": {"enabled": True}}
        self.assertEqual(self._nums(config), [7, 8])
        (self.root / "clip_counter.txt").write_text("nope")
        self.assertEqual(self._nums(config), [7, 8])
        self.assertEqual(self._nums(config, 0), [])


class UploadWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.youtube, self.tiktok, self.instagram = _upload_modules()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, config, clip_nums, modules=None):
        logs = []
        modules = modules or {
            "youtube_upload": self.youtube,
            "tiktok_upload": self.tiktok,
            "instagram_upload": self.instagram,
        }
        with (
            patch("pipeline.project_root", return_value=self.root),
            patch.dict(sys.modules, modules),
        ):
            run_uploads(["/clips/a.mp4", "/clips/b.mp4"], config, clip_nums, logs.append)
        return logs

    def test_enabled_platforms_share_clip_numbers_and_advance_the_counter(self):
        config = {
            "youtube": {"enabled": True},
            "tiktok": {"enabled": True},
            "instagram": {"enabled": True},
        }
        logs = self._run(config, [3, 4])
        for module in (self.youtube, self.tiktok, self.instagram):
            module.upload_clips.assert_called_once()
            self.assertEqual(module.upload_clips.call_args.kwargs["clip_nums"], [3, 4])
        self.assertEqual((self.root / "clip_counter.txt").read_text(), "5")
        self.assertTrue(any("YouTube" in line for line in logs))
        self.assertTrue(any("TikTok" in line for line in logs))
        self.assertTrue(any("Instagram" in line for line in logs))

    def test_one_platform_does_not_write_the_shared_counter(self):
        self._run({"youtube": {"enabled": True}}, None)
        self.youtube.upload_clips.assert_called_once()
        self.tiktok.upload_clips.assert_not_called()
        self.instagram.upload_clips.assert_not_called()
        self.assertFalse((self.root / "clip_counter.txt").exists())

    def test_counter_advances_when_an_upload_raises(self):
        self.youtube.upload_clips.side_effect = RuntimeError("yt down")
        config = {"youtube": {"enabled": True}, "tiktok": {"enabled": True}}
        logs = self._run(config, [3, 4])
        self.tiktok.upload_clips.assert_called_once()
        self.assertEqual((self.root / "clip_counter.txt").read_text(), "5")
        self.assertTrue(any("YouTube upload failed" in line for line in logs))


class ProcessTests(unittest.TestCase):
    def test_process_one_video_returns_clip_paths_and_skips_extract_when_empty(self):
        video = Path("/tmp/rec.mp4")
        highlights = [{"start": 1.25, "end": 20.5, "score": 0.875}]
        logs = []
        with (
            patch("pipeline.detect_highlights", return_value=highlights) as detect,
            patch("pipeline.extract_all_clips", return_value=["/out/rec_clip_01.mp4"]) as extract,
        ):
            paths = process_one_video(video, {"clip": {"output_dir": "outputs"}}, log=logs.append)
        self.assertEqual(paths, ["/out/rec_clip_01.mp4"])
        self.assertEqual(detect.call_args.args[0], str(video.resolve()))
        extract.assert_called_once()
        self.assertTrue(any("1.2s - 20.5s" in line for line in logs))

        with (
            patch("pipeline.detect_highlights", return_value=[]),
            patch("pipeline.extract_all_clips") as extract,
        ):
            self.assertEqual(process_one_video(video, {"clip": {"output_dir": "outputs"}}), [])
        extract.assert_not_called()

    def test_process_videos_uploads_only_the_selection(self):
        config = {"youtube": {"enabled": True}, "tiktok": {"enabled": True}}
        selector = MagicMock(return_value=["b.mp4"])
        with (
            patch("pipeline.process_one_video", return_value=["a.mp4", "b.mp4"]) as once,
            patch("pipeline.run_uploads") as uploads,
            patch("pipeline.clip_nums_for_upload_count", return_value=[8]),
        ):
            process_videos([Path("rec.mp4")], config, upload_selector=selector)
        selector.assert_called_once_with(["a.mp4", "b.mp4"])
        uploads.assert_called_once()
        self.assertEqual(uploads.call_args.args[0], ["b.mp4"])
        self.assertEqual(uploads.call_args.args[2], [8])
        once.assert_called_once()

    def test_empty_selection_or_disabled_platforms_do_not_upload(self):
        logs = []
        with (
            patch("pipeline.process_one_video", return_value=["a.mp4"]),
            patch("pipeline.run_uploads") as uploads,
        ):
            process_videos(
                [Path("rec.mp4")],
                {"youtube": {"enabled": True}},
                log=logs.append,
                upload_selector=lambda _paths: [],
            )
        uploads.assert_not_called()
        self.assertTrue(any("Skipped upload" in line for line in logs))

        selector = MagicMock(return_value=["a.mp4"])
        with (
            patch("pipeline.process_one_video", return_value=[]),
            patch("pipeline.run_uploads") as uploads,
        ):
            process_videos([Path("rec.mp4")], {"youtube": {"enabled": True}}, upload_selector=selector)
        selector.assert_not_called()
        uploads.assert_not_called()

        with (
            patch("pipeline.process_one_video", return_value=["a.mp4"]),
            patch("pipeline.run_uploads") as uploads,
        ):
            process_videos(
                [Path("rec.mp4")],
                {"youtube": {"enabled": False}},
                upload_selector=selector,
            )
        selector.assert_not_called()
        uploads.assert_not_called()


if __name__ == "__main__":
    unittest.main()
