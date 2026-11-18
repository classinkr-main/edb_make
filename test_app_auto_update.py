from __future__ import annotations

import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app_server


class FakeDownloadResponse(io.BytesIO):
    def __init__(self, body: bytes, url: str) -> None:
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def update_status_for(body: bytes) -> dict:
    url = "https://example.test/ClassInEDBMVP-macOS.zip"
    return {
        "ok": True,
        "platform": "macos",
        "currentVersion": "0.1.0",
        "updateAvailable": True,
        "downloadUrl": url,
        "sha256": hashlib.sha256(body).hexdigest(),
        "sizeBytes": len(body),
        "latest": {
            "version": "0.2.0",
            "downloadUrl": url,
            "fileName": "ClassInEDBMVP-macOS.zip",
            "artifactType": "zip",
            "sha256": hashlib.sha256(body).hexdigest(),
            "sizeBytes": len(body),
        },
    }


class TestAppAutoUpdate(unittest.TestCase):
    def setUp(self) -> None:
        app_server.clear_app_update_status_cache()
        app_server.clear_update_pin_attempts()
        app_server.clear_update_install_state()

    def test_pin_verifier_accepts_only_matching_numeric_pin(self) -> None:
        verifier = app_server.build_update_pin_verifier("4826", salt=b"0123456789abcdef")

        self.assertTrue(app_server.verify_update_pin("4826", verifier))
        self.assertFalse(app_server.verify_update_pin("4827", verifier))
        self.assertFalse(app_server.verify_update_pin("abcd", verifier))
        self.assertNotIn("4826", verifier)

    def test_pin_attempts_rate_limit_repeated_failures(self) -> None:
        verifier = app_server.build_update_pin_verifier("4826", salt=b"0123456789abcdef")
        for _ in range(app_server.UPDATE_PIN_FAILURE_LIMIT):
            with self.assertRaisesRegex(app_server.AppUpdateError, "올바르지"):
                app_server._authorize_update_pin("0000", verifier)

        with self.assertRaises(app_server.AppUpdateError) as blocked:
            app_server._authorize_update_pin("4826", verifier)
        self.assertEqual("update_pin_rate_limited", blocked.exception.code)

    def test_update_status_exposes_capability_but_never_pin_verifier(self) -> None:
        verifier = app_server.build_update_pin_verifier("4826", salt=b"0123456789abcdef")
        config = {
            "appId": "ClassInEDBMVP",
            "appName": "ClassInEDBMVP",
            "platform": "macos",
            "version": "0.1.0",
            "updateFeedUrl": "https://example.test/update.json",
            "updatePinHash": verifier,
        }
        body = b"signed update artifact"
        status_fixture = update_status_for(body)
        platform_payload = dict(status_fixture["latest"])
        platform_payload["arch"] = "arm64"
        feed = {
            "appId": "ClassInEDBMVP",
            "appName": "ClassInEDBMVP",
            "platforms": {"macos": platform_payload},
        }
        with patch.object(app_server, "load_app_update_config", return_value=config), \
                patch.object(app_server, "_fetch_update_feed", return_value=feed), \
                patch.object(app_server, "is_frozen_app", return_value=True), \
                patch.object(app_server.platform, "machine", return_value="arm64"):
            status = app_server.build_app_update_status()

        self.assertTrue(status["automaticUpdateEnabled"])
        self.assertTrue(status["automaticUpdateSupported"])
        self.assertTrue(status["automaticUpdateReady"])
        self.assertNotIn("updatePinHash", status)
        self.assertNotIn(verifier, json.dumps(status))

    def test_download_verifies_size_and_sha256_before_promoting_file(self) -> None:
        body = b"verified installer bytes"
        status = update_status_for(body)
        with TemporaryDirectory() as raw_tmp, patch.object(
            app_server,
            "urlopen",
            return_value=FakeDownloadResponse(body, status["downloadUrl"]),
        ):
            artifact = app_server._download_update_artifact(status, runtime_dir=Path(raw_tmp))

            self.assertEqual(body, artifact.read_bytes())
            self.assertEqual("ClassInEDBMVP-macOS.zip", artifact.name)
            self.assertFalse((artifact.parent / f"{artifact.name}.part").exists())

    def test_automatic_update_infers_legacy_artifact_type_from_safe_suffix(self) -> None:
        status = update_status_for(b"verified installer bytes")
        status["latest"].pop("artifactType")

        details = app_server._update_artifact_details(status)

        self.assertEqual("zip", details[2])

    def test_download_rejects_checksum_mismatch_and_removes_partial_file(self) -> None:
        body = b"tampered installer bytes"
        status = update_status_for(body)
        status["sha256"] = "0" * 64
        status["latest"]["sha256"] = "0" * 64
        with TemporaryDirectory() as raw_tmp, patch.object(
            app_server,
            "urlopen",
            return_value=FakeDownloadResponse(body, status["downloadUrl"]),
        ):
            with self.assertRaises(app_server.AppUpdateError) as raised:
                app_server._download_update_artifact(status, runtime_dir=Path(raw_tmp))

            self.assertEqual("update_checksum_mismatch", raised.exception.code)
            self.assertEqual([], list((Path(raw_tmp) / "updates").iterdir()))

    def test_prepare_update_rejects_wrong_pin_before_download(self) -> None:
        body = b"verified installer bytes"
        status = update_status_for(body)
        verifier = app_server.build_update_pin_verifier("4826", salt=b"0123456789abcdef")
        with patch.object(app_server, "build_app_update_status", return_value=status), \
                patch.object(app_server, "load_app_update_config", return_value={"updatePinHash": verifier}), \
                patch.object(app_server, "_download_update_artifact") as download:
            with self.assertRaises(app_server.AppUpdateError) as raised:
                app_server.prepare_app_update("0000")

        self.assertEqual("invalid_update_pin", raised.exception.code)
        download.assert_not_called()

    def test_prepare_update_downloads_and_schedules_replacement_for_matching_pin(self) -> None:
        body = b"verified installer bytes"
        status = update_status_for(body)
        verifier = app_server.build_update_pin_verifier("4826", salt=b"0123456789abcdef")
        artifact = Path("/tmp/ClassInEDBMVP-macOS.zip")
        with patch.object(app_server, "build_app_update_status", return_value=status), \
                patch.object(app_server, "load_app_update_config", return_value={"updatePinHash": verifier}), \
                patch.object(app_server, "_download_update_artifact", return_value=artifact) as download, \
                patch.object(
                    app_server,
                    "_launch_update_helper",
                    return_value={"platform": "macos", "restartScheduled": True},
                ) as launch:
            result = app_server.prepare_app_update("4826")

        self.assertTrue(result["accepted"])
        self.assertEqual("0.2.0", result["targetVersion"])
        download.assert_called_once_with(status)
        launch.assert_called_once_with(artifact, status, "zip")

    def test_runtime_config_reads_pin_hash_from_environment_without_exposing_plain_pin(self) -> None:
        verifier = app_server.build_update_pin_verifier("4826", salt=b"0123456789abcdef")
        with TemporaryDirectory() as raw_tmp, patch.object(app_server, "RESOURCE_DIR", Path(raw_tmp)), \
                patch.object(app_server, "BASE_DIR", Path(raw_tmp)), \
                patch.dict(os.environ, {"EDB_UPDATE_PIN_HASH": verifier}):
            config = app_server.load_app_update_config()

        self.assertEqual(verifier, config["updatePinHash"])


if __name__ == "__main__":
    unittest.main()
