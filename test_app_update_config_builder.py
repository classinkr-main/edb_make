from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parent
BUILDER = PROJECT_ROOT / "scripts" / "build_app_update_config.py"


class TestAppUpdateConfigBuilder(unittest.TestCase):
    def test_builds_canonical_config_from_project_aliases(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "app_update_config.json"
            output = tmpdir / "generated" / "app_update_config.json"
            source.write_text(
                json.dumps({
                    "app_id": "ClassInEDBMVP",
                    "app_name": "ClassInEDBMVP",
                    "version": "0.2.0",
                    "update_feed_url": "https://example.test/update.json",
                    "download_url": "https://example.test/old/ClassInEDBMVP-macOS.zip",
                    "release_notes_url": "https://example.test/releases/0.2.0",
                }),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["EDB_PACKAGE_DOWNLOAD_URL"] = "https://example.test/new/ClassInEDBMVP-macOS.zip"

            subprocess.run(
                [sys.executable, str(BUILDER), str(source), str(output)],
                cwd=PROJECT_ROOT,
                env=env,
                check=True,
            )
            config = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("ClassInEDBMVP", config["appId"])
        self.assertEqual("ClassInEDBMVP", config["appName"])
        self.assertEqual("0.2.0", config["version"])
        self.assertEqual("https://example.test/update.json", config["updateFeedUrl"])
        self.assertEqual("https://example.test/new/ClassInEDBMVP-macOS.zip", config["downloadUrl"])
        self.assertEqual("https://example.test/releases/0.2.0", config["releaseNotesUrl"])
        self.assertNotIn("app_id", config)
        self.assertNotIn("download_url", config)

    def test_rejects_conflicting_project_aliases(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "app_update_config.json"
            output = tmpdir / "generated" / "app_update_config.json"
            source.write_text(
                json.dumps({
                    "appId": "ClassInEDBMVP",
                    "appName": "ClassInEDBMVP",
                    "downloadUrl": "https://example.test/old/ClassInEDBMVP-macOS.zip",
                    "download_url": "https://example.test/new/ClassInEDBMVP-macOS.zip",
                }),
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(BUILDER), str(source), str(output)],
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("app_update_config.json downloadUrl aliases conflict", result.stderr)
        self.assertFalse(output.exists())

    def test_hashes_packaging_pin_without_embedding_plaintext(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "app_update_config.json"
            output = tmpdir / "generated" / "app_update_config.json"
            source.write_text(
                json.dumps({"appId": "ClassInEDBMVP", "version": "0.2.0"}),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["EDB_PACKAGE_UPDATE_PIN"] = "4826"

            subprocess.run(
                [sys.executable, str(BUILDER), str(source), str(output)],
                cwd=PROJECT_ROOT,
                env=env,
                check=True,
            )
            config = json.loads(output.read_text(encoding="utf-8"))

        verifier = config["updatePinHash"]
        self.assertTrue(verifier.startswith("pbkdf2_sha256$210000$"))
        self.assertNotIn("4826", verifier)

    def test_rejects_non_numeric_packaging_pin(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "app_update_config.json"
            output = tmpdir / "generated" / "app_update_config.json"
            source.write_text("{}", encoding="utf-8")
            env = os.environ.copy()
            env["EDB_PACKAGE_UPDATE_PIN"] = "12ab"

            result = subprocess.run(
                [sys.executable, str(BUILDER), str(source), str(output)],
                cwd=PROJECT_ROOT,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("4-12 ASCII digits", result.stderr)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
