"""Instagram clip hosting. No live network calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import instagram_upload
from instagram_upload import (
    InstagramStorageError,
    upload_clips,
    upload_reel,
)

PATHNAME = "ig-reels/11111111-1111-4111-8111-111111111111.mp4"
UPLOAD_URL = (
    "https://vercel.com/api/blob/?pathname=ig-reels%2F"
    "11111111-1111-4111-8111-111111111111.mp4&sig=put"
)
VIDEO_URL = (
    "https://store123.private.blob.vercel-storage.com/"
    "ig-reels/11111111-1111-4111-8111-111111111111.mp4?sig=get"
)
API_URL = "https://creator-assistant.vercel.app/api/ig-media"
STORAGE = {
    "provider": "vercel_blob",
    "api_url": API_URL,
    "token": "test-storage-token",
}
BLOCKED_HOSTS = ("tmpfiles.org", "litterbox.catbox.moe", "transfer.sh", "0x0.st")


class FakeResp:
    def __init__(self, status, payload, url="https://example.test/unused"):
        self.status_code = status
        self._payload = payload
        self.url = url
        self.text = payload if isinstance(payload, str) else json.dumps(payload)
        self.ok = status < 400
        self.is_redirect = status in {301, 302, 303, 307, 308}

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error", response=self)


class StorageFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clip = Path(self.tmp.name) / "clip 01.mp4"
        self.clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
        self.calls = []
        self.container_mode = "ok"
        self.put_error = None
        self.delete_status = 200

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url == API_URL:
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-storage-token")
            body = kwargs["json"]
            if body["op"] == "delete":
                return FakeResp(self.delete_status, {"deleted": True, "error": "nope"} if self.delete_status >= 400 else {"deleted": True})
            self.assertEqual(body["contentType"], "video/mp4")
            self.assertEqual(body["contentLength"], self.clip.stat().st_size)
            return FakeResp(
                200,
                {
                    "uploadUrl": UPLOAD_URL,
                    "videoUrl": VIDEO_URL,
                    "pathname": PATHNAME,
                    "contentType": "video/mp4",
                    "access": "private",
                },
            )
        if url.endswith("/media"):
            data = kwargs.get("data") or {}
            self.assertIsNone(kwargs.get("params"))
            self.assertEqual(data.get("video_url"), VIDEO_URL)
            self.assertEqual(data.get("media_type"), "REELS")
            if self.container_mode == "expired" and not getattr(self, "_expired_once", False):
                self._expired_once = True
                return FakeResp(
                    400,
                    {"error": {"code": 190, "message": "Session has expired"}},
                    url=url,
                )
            if self.container_mode == "http":
                return FakeResp(400, {"error": {"message": "cannot download"}}, url=url)
            return FakeResp(200, {"id": "container-1"}, url=url)
        if url.endswith("/media_publish"):
            if self.container_mode == "publish_fail":
                return FakeResp(500, {"error": {"message": "publish failed"}}, url=url)
            return FakeResp(200, {"id": "media-1"}, url=url)
        self.fail(f"unexpected POST {url}")

    def _put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        self.assertEqual(url, UPLOAD_URL)
        self.assertEqual(kwargs["headers"]["Content-Type"], "video/mp4")
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertFalse(kwargs["allow_redirects"])
        if self.put_error == "status":
            return FakeResp(403, "denied", url=url)
        if self.put_error == "network":
            raise requests.exceptions.ConnectionError("reset")
        return FakeResp(200, {"ok": True}, url=url)

    def _get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self.container_mode == "processing_error":
            return FakeResp(200, {"status_code": "ERROR", "status": "media download failed"}, url=url)
        return FakeResp(200, {"status_code": "FINISHED"}, url=url)

    def _run_reel(self, storage=None):
        with (
            patch("instagram_upload.requests.post", side_effect=self._post),
            patch("instagram_upload.requests.put", side_effect=self._put),
            patch("instagram_upload.requests.get", side_effect=self._get),
            patch("instagram_upload.time.sleep"),
        ):
            return upload_reel(
                str(self.clip),
                "1789",
                "ig-token",
                caption="League Clip 1",
                storage=STORAGE if storage is None else storage,
                log=lambda _message: None,
            )

    def _ops(self):
        kinds = []
        for method, url, kwargs in self.calls:
            if url == API_URL:
                kinds.append(kwargs["json"]["op"])
            elif method == "PUT":
                kinds.append("put")
            elif url.endswith("/media_publish"):
                kinds.append("publish")
            elif url.endswith("/media"):
                kinds.append("container")
            elif method == "GET":
                kinds.append("status")
            else:
                kinds.append(url)
        return kinds

    def test_public_hosts_are_gone(self):
        source = Path(instagram_upload.__file__).read_text()
        self.assertFalse(hasattr(instagram_upload, "_upload_to_temp_host"))
        self.assertFalse(hasattr(instagram_upload, "_TEMP_HOSTS"))
        for host in BLOCKED_HOSTS:
            self.assertNotIn(host, source)

    def test_success_deletes_after_publish(self):
        media_id = self._run_reel()
        self.assertEqual(media_id, "media-1")
        self.assertEqual(
            self._ops(),
            ["create", "put", "container", "status", "publish", "delete"],
        )
        for _method, url, _kwargs in self.calls:
            for host in BLOCKED_HOSTS:
                self.assertNotIn(host, url)

    def test_processing_error_still_deletes(self):
        self.container_mode = "processing_error"
        with self.assertRaises(RuntimeError):
            self._run_reel()
        self.assertEqual(self._ops()[-1], "delete")
        self.assertNotIn("publish", self._ops())

    def test_publish_error_still_deletes(self):
        self.container_mode = "publish_fail"
        with self.assertRaises(requests.exceptions.HTTPError):
            self._run_reel()
        self.assertIn("delete", self._ops())

    def test_put_failure_deletes_and_does_not_call_instagram(self):
        self.put_error = "status"
        with self.assertRaises(InstagramStorageError) as caught:
            self._run_reel()
        self.assertIn("403", str(caught.exception))
        self.assertEqual(self._ops(), ["create", "put", "delete"])

    def test_put_network_error_retries_then_deletes(self):
        self.put_error = "network"
        with self.assertRaises(InstagramStorageError):
            self._run_reel()
        puts = [op for op in self._ops() if op == "put"]
        self.assertEqual(len(puts), 3)
        self.assertEqual(self._ops()[-1], "delete")
        self.assertNotIn("container", self._ops())

    def test_delete_failure_after_publish_still_returns_media_id(self):
        self.delete_status = 400
        logs = []
        with (
            patch("instagram_upload.requests.post", side_effect=self._post),
            patch("instagram_upload.requests.put", side_effect=self._put),
            patch("instagram_upload.requests.get", side_effect=self._get),
        ):
            media_id = upload_reel(
                str(self.clip),
                "1789",
                "ig-token",
                storage=STORAGE,
                log=logs.append,
            )
        self.assertEqual(media_id, "media-1")
        self.assertTrue(any("could not delete" in line for line in logs))
        self.assertFalse(any(VIDEO_URL in line for line in logs))

    def test_storage_401_does_not_upload_or_call_instagram(self):
        def post(url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            return FakeResp(401, {"error": "Invalid storage token."}, url=url)

        with (
            patch("instagram_upload.requests.post", side_effect=post),
            patch("instagram_upload.requests.put") as put,
        ):
            with self.assertRaises(InstagramStorageError) as caught:
                upload_reel(str(self.clip), "1789", "ig-token", storage=STORAGE)
        self.assertIn("IG_STORAGE_TOKEN", str(caught.exception))
        put.assert_not_called()
        self.assertEqual(self._ops(), ["create"])

    def test_refuses_unexpected_upload_target(self):
        def post(url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            body = kwargs["json"]
            if body["op"] == "delete":
                return FakeResp(200, {"deleted": True})
            return FakeResp(
                200,
                {
                    "uploadUrl": "https://evil.example/put",
                    "videoUrl": VIDEO_URL,
                    "pathname": PATHNAME,
                },
            )

        with (
            patch("instagram_upload.requests.post", side_effect=post),
            patch("instagram_upload.requests.put") as put,
        ):
            with self.assertRaises(InstagramStorageError) as caught:
                upload_reel(str(self.clip), "1789", "ig-token", storage=STORAGE)
        self.assertIn("unexpected upload target", str(caught.exception))
        put.assert_not_called()
        self.assertEqual(self._ops(), ["create", "delete"])

    def test_missing_storage_is_a_clear_error(self):
        for storage in (None, {}, {"provider": "vercel_blob", "api_url": API_URL, "token": ""}):
            with self.subTest(storage=storage):
                with self.assertRaises(InstagramStorageError) as caught:
                    upload_reel(str(self.clip), "1789", "ig-token", storage=storage)
                message = str(caught.exception)
                self.assertIn("config.yaml", message)
                self.assertIn("not used", message)

    def test_placeholder_token_http_url_and_unknown_provider(self):
        with self.assertRaises(InstagramStorageError) as placeholder:
            upload_reel(
                str(self.clip),
                "1789",
                "ig-token",
                storage={"provider": "vercel_blob", "api_url": API_URL, "token": "YOUR_IG_STORAGE_TOKEN"},
            )
        self.assertIn("not used", str(placeholder.exception))

        with self.assertRaises(InstagramStorageError) as insecure:
            upload_reel(
                str(self.clip),
                "1789",
                "ig-token",
                storage={"api_url": "http://creator-assistant.vercel.app/api/ig-media", "token": "x"},
            )
        self.assertIn("https://", str(insecure.exception))

        with self.assertRaises(InstagramStorageError) as provider:
            upload_reel(
                str(self.clip),
                "1789",
                "ig-token",
                storage={"provider": "tmpfiles", "api_url": API_URL, "token": "x"},
            )
        self.assertIn("not supported", str(provider.exception))
        self.assertIn("not used", str(provider.exception))

    def test_upload_clips_checks_storage_before_oauth(self):
        with (
            patch("instagram_upload.get_access_token") as auth,
            patch("instagram_upload.project_root", return_value=Path(self.tmp.name)),
        ):
            with self.assertRaises(InstagramStorageError):
                upload_clips(
                    [str(self.clip)],
                    {"instagram": {"enabled": True, "ig_user_id": "1789"}},
                )
        auth.assert_not_called()

    def test_disabled_instagram_does_not_require_storage(self):
        self.assertEqual(upload_clips([str(self.clip)], {"instagram": {"enabled": False}}), [])

    def test_expired_session_retries_and_deletes_both_hosts(self):
        self.container_mode = "expired"
        root = Path(self.tmp.name)
        with (
            patch("instagram_upload.requests.post", side_effect=self._post),
            patch("instagram_upload.requests.put", side_effect=self._put),
            patch("instagram_upload.requests.get", side_effect=self._get),
            patch("instagram_upload.time.sleep"),
            patch("instagram_upload.project_root", return_value=root),
            patch("instagram_upload.get_access_token", return_value="ig-token-2") as auth,
        ):
            uploaded = upload_clips(
                [str(self.clip)],
                {
                    "instagram": {
                        "enabled": True,
                        "ig_user_id": "1789",
                        "storage": STORAGE,
                        "title_template": "League Clip {num}",
                    }
                },
                clip_nums=[4],
                log=lambda _message: None,
            )
        self.assertEqual(uploaded, ["media-1"])
        self.assertEqual(auth.call_count, 2)
        self.assertEqual(auth.call_args_list[-1].kwargs.get("force_reauth"), True)
        self.assertEqual(self._ops().count("delete"), 2)
        self.assertEqual(self._ops().count("create"), 2)
        saved = json.loads((root / "instagram_uploaded.json").read_text())
        self.assertIn(str(self.clip.resolve()), saved["paths"])


if __name__ == "__main__":
    unittest.main()
