import copy
import hashlib
import json
import os
import base64
import io
import shutil
import threading
import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import app_server
from structured_schema import Box


VALID_MANIFEST_SHA256 = "a" * 64
VALID_ARTIFACT_SHA256 = "b" * 64


class TestStaticAssetCaching(unittest.TestCase):
    def setUp(self):
        app_server.clear_app_update_status_cache()

    def test_app_version_comparison_handles_semver_like_versions(self):
        self.assertEqual(0, app_server.compare_app_versions("v0.1.0", "0.1"))
        self.assertGreater(app_server.compare_app_versions("0.1.0", "0.1.1"), 0)
        self.assertLess(app_server.compare_app_versions("0.2.0", "0.1.9"), 0)
        self.assertGreater(app_server.compare_app_versions("1.0.0-beta.1", "1.0.0"), 0)

    def test_update_urls_require_https_except_loopback(self):
        self.assertEqual("", app_server._normalize_update_url("http://example.test/update.json"))
        self.assertEqual("https://example.test/update.json", app_server._normalize_update_url("https://example.test/update.json"))
        self.assertEqual("http://127.0.0.1:9999/update.json", app_server._normalize_update_url("http://127.0.0.1:9999/update.json"))

    def test_persisted_noncontinuous_scale_marks_only_legacy_session_values(self):
        self.assertTrue(app_server._problem_has_persisted_legacy_placement_scale({
            "placementScaleRatio": 2.4,
            "inputIntent": "single-problem",
        }))
        self.assertFalse(app_server._problem_has_persisted_legacy_placement_scale({
            "placementScaleRatio": 1.6,
            "inputIntent": "single-problem",
        }))
        self.assertFalse(app_server._problem_has_persisted_legacy_placement_scale({
            "placementScaleRatio": 2.4,
            "inputIntent": "page-as-is",
        }))
    def test_frozen_app_home_uses_redirected_windows_documents_directory(self):
        redirected_documents = Path("C:/Users/test/OneDrive/문서")
        with (
            patch.object(app_server.sys, "platform", "win32"),
            patch.object(
                app_server,
                "_windows_documents_directory",
                return_value=redirected_documents,
            ),
            patch.dict(os.environ, {"EDB_APP_HOME": ""}),
        ):
            app_home = app_server.default_frozen_app_home()

        self.assertEqual(
            (redirected_documents / "ClassInEDBMVP").resolve(),
            app_home,
        )

    def test_frozen_app_home_expands_environment_override(self):
        with TemporaryDirectory() as raw_tmp:
            configured_root = Path(raw_tmp) / "사용자 지정"
            with patch.dict(
                os.environ,
                {
                    "EDB_APP_HOME": "$EDB_TEST_APP_HOME/runtime",
                    "EDB_TEST_APP_HOME": str(configured_root),
                },
            ):
                app_home = app_server.default_frozen_app_home()

        self.assertEqual((configured_root / "runtime").resolve(), app_home)

    def test_local_health_check_rejects_another_service(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return b'{"ok": true, "app": "another-service"}'

        with patch.object(app_server, "urlopen", return_value=FakeResponse()):
            self.assertFalse(app_server._local_server_is_healthy("127.0.0.1", 8765))

        FakeResponse.read = staticmethod(
            lambda: json.dumps({"ok": True, "app": app_server.APP_NAME}).encode("utf-8")
        )
        with patch.object(app_server, "urlopen", return_value=FakeResponse()):
            self.assertTrue(app_server._local_server_is_healthy("127.0.0.1", 8765))

    @unittest.skipUnless(os.name == "nt", "exclusive bind semantics are Windows-specific")
    def test_windows_app_server_rejects_shared_port_binding(self):
        foreign_server = app_server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            app_server.SimpleHTTPRequestHandler,
        )
        foreign_address = foreign_server.server_address
        try:
            with self.assertRaises(OSError):
                app_server.AppHTTPServer(
                    foreign_address,
                    app_server.AppRequestHandler,
                )
        finally:
            foreign_server.server_close()

        app_http_server = app_server.AppHTTPServer(
            ("127.0.0.1", 0),
            app_server.AppRequestHandler,
        )
        app_address = app_http_server.server_address
        try:
            with self.assertRaises(OSError):
                app_server.ThreadingHTTPServer(
                    app_address,
                    app_server.SimpleHTTPRequestHandler,
                )
        finally:
            app_http_server.server_close()

    def test_update_config_normalizes_local_snake_case_overrides(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            resource_dir = tmpdir / "resource"
            base_dir = tmpdir / "base"
            resource_dir.mkdir()
            base_dir.mkdir()
            (resource_dir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "downloadUrl": "https://example.test/old/ClassInEDBMVP-macOS.zip",
                }),
                encoding="utf-8",
            )
            (base_dir / "app_update_config.json").write_text(
                json.dumps({
                    "download_url": "https://example.test/new/ClassInEDBMVP-macOS.zip",
                    "release_notes_url": "https://example.test/releases/new",
                }),
                encoding="utf-8",
            )

            with patch.object(app_server, "RESOURCE_DIR", resource_dir), \
                    patch.object(app_server, "BASE_DIR", base_dir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_ID": "",
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }):
                config = app_server.load_app_update_config()
                status = app_server.build_app_update_status()

            self.assertEqual("https://example.test/new/ClassInEDBMVP-macOS.zip", config["downloadUrl"])
            self.assertNotIn("download_url", config)
            self.assertEqual("https://example.test/releases/new", config["releaseNotesUrl"])
            self.assertEqual("manual_download", status["channelStatus"])
            self.assertEqual("https://example.test/new/ClassInEDBMVP-macOS.zip", status["downloadUrl"])
            self.assertEqual("https://example.test/releases/new", status["releaseNotesUrl"])

    def test_update_status_rejects_conflicting_update_config_aliases(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            resource_dir = tmpdir / "resource"
            resource_dir.mkdir()
            (resource_dir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "downloadUrl": "https://example.test/old/ClassInEDBMVP-macOS.zip",
                    "download_url": "https://example.test/new/ClassInEDBMVP-macOS.zip",
                }),
                encoding="utf-8",
            )

            with patch.object(app_server, "RESOURCE_DIR", resource_dir), \
                    patch.object(app_server, "BASE_DIR", resource_dir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_ID": "",
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_config", status["channelStatus"])
            self.assertEqual(
                "app_update_config.json downloadUrl aliases conflict: "
                "downloadUrl='https://example.test/old/ClassInEDBMVP-macOS.zip', "
                "download_url='https://example.test/new/ClassInEDBMVP-macOS.zip'",
                status["error"],
            )

    def test_sanitize_edb_file_name_normalizes_requested_name(self):
        self.assertEqual("Lesson_1.edb", app_server.sanitize_edb_file_name("Lesson 1"))
        self.assertEqual("고1_샘플.edb", app_server.sanitize_edb_file_name("../고1 샘플.edb"))
        self.assertEqual(
            "fallback_name.edb",
            app_server.sanitize_edb_file_name("", fallback_stem="fallback name"),
        )

    def test_windows_reserved_names_and_unc_file_uris_are_preserved_safely(self):
        self.assertEqual("_CON.edb", app_server.sanitize_edb_file_name("CON.edb"))
        self.assertTrue(app_server.sanitize_upload_file_name("NUL.pdf").startswith("_NUL_"))
        self.assertEqual("_LPT1", app_server.sanitize_output_dir_name("LPT1"))
        self.assertEqual(
            Path("//fileserver/shared/Lesson 1.pdf"),
            app_server.decode_file_reference("file://fileserver/shared/Lesson%201.pdf"),
        )

    def test_upload_cache_component_reserves_bytes_for_content_digest_prefix(self):
        safe_name = app_server.sanitize_upload_file_name(("😀" * 100) + ".pdf")
        target_name = f"{'a' * 64}_{safe_name}"
        self.assertLessEqual(len(safe_name.encode("utf-8")), app_server.UPLOAD_FILE_NAME_MAX_BYTES)
        self.assertLessEqual(len(target_name.encode("utf-8")), 255)
        self.assertTrue(safe_name.endswith(".pdf"))

    def test_path_components_are_utf8_byte_safe_and_preserve_suffixes(self):
        suffix = "20260824_hash123456"
        ascii_name = app_server.sanitize_output_dir_name("A" * 250, suffix=suffix)
        hangul_name = app_server.sanitize_output_dir_name("수학" * 150, suffix=suffix)
        for name in (ascii_name, hangul_name):
            self.assertLessEqual(len(name.encode("utf-8")), app_server.SAFE_PATH_COMPONENT_MAX_BYTES)
            self.assertTrue(name.endswith(f"_{suffix}"))
            self.assertFalse(name.endswith((".", " ")))
        self.assertEqual("_CON", app_server.sanitize_output_dir_name("CON"))

    def test_edb_name_reserves_utf8_space_for_extension_and_part_suffix(self):
        for raw_name in ("A" * 250, "수학" * 150):
            edb_name = app_server.sanitize_edb_file_name(raw_name)
            part_name = app_server._edb_part_file_name(edb_name, 9998, 9999)
            self.assertTrue(edb_name.endswith(".edb"))
            self.assertTrue(part_name.endswith("_part9999.edb"))
            self.assertLessEqual(
                len(part_name.encode("utf-8")),
                app_server.SAFE_PATH_COMPONENT_MAX_BYTES,
            )

    def test_preview_and_generation_names_preserve_stamp_with_long_unicode_stem(self):
        handler = object.__new__(app_server.AppRequestHandler)
        long_source = Path(("긴자료" * 100) + ".pdf")
        stamp = "20260824_123456_1234567890_stampabcdef"
        with patch.object(app_server, "_unique_artifact_stamp", return_value=stamp):
            preview = handler._resolve_preview_output_dir({}, [long_source])
            budget = app_server._managed_path_component_max_bytes(
                app_server.default_output_root() / "previews",
                reserved_descendants=app_server.MANAGED_PREVIEW_RESERVED_DESCENDANTS,
                max_bytes=app_server.MANAGED_OUTPUT_DIR_NAME_MAX_BYTES,
            )
            generation = app_server.sanitize_output_dir_name(
                long_source.stem,
                suffix=stamp,
                max_bytes=budget,
            )
        self.assertEqual(preview.name, generation)
        self.assertTrue(generation.endswith(f"_{stamp}"))
        self.assertLessEqual(
            len(generation.encode("utf-8")),
            app_server.MANAGED_OUTPUT_DIR_NAME_MAX_BYTES,
        )

    @unittest.skipUnless(sys.platform.startswith("win"), "exercises Windows MAX_PATH budgeting, which app_server only enables on win32")
    def test_windows_preview_crop_path_stays_below_legacy_max_path_boundary(self):
        handler = object.__new__(app_server.AppRequestHandler)
        runtime_dir = Path(
            "C:/Users/Administrator/Documents/ClassInEDBMVP/.app_runtime"
        )
        source = Path(
            ("b17d4d83e8de7ab5479d9c53a1dc00d520a8b28a64e86bc67f8f41c20a8cd38d"
             "__2026.08.18_23_고1B_539b127457.pdf")
        )
        stamp = "20260824_143057_1787549457154522500_67513cd0436a"
        with (
            patch.object(app_server, "RUNTIME_DIR", runtime_dir),
            patch.object(app_server, "_unique_artifact_stamp", return_value=stamp),
        ):
            preview = handler._resolve_preview_output_dir({}, [source])

        crop_path = preview / "problem_crops" / "problem_001_df48bf64.png"
        self.assertLess(len(str(crop_path)), 240)
        self.assertLessEqual(
            len(preview.name.encode("utf-8")),
            app_server.MANAGED_OUTPUT_DIR_NAME_MAX_BYTES,
        )

    @unittest.skipUnless(sys.platform.startswith("win"), "exercises Windows MAX_PATH budgeting, which app_server only enables on win32")
    def test_long_onedrive_hangul_root_dynamically_budgets_preview_and_publish(self):
        handler = object.__new__(app_server.AppRequestHandler)
        runtime_dir = Path(
            "C:/Users/"
            + ("LongProfileName" * 3)
            + "/OneDrive - International Academy/문서/ClassInEDBMVP/.app_runtime"
        )
        source = Path(("긴_한글_시험자료" * 80) + ".pdf")
        stamp = "20260824_143057_1787549457154522500_67513cd0436a"
        with (
            patch.object(app_server, "RUNTIME_DIR", runtime_dir),
            patch.object(app_server, "_unique_artifact_stamp", return_value=stamp),
        ):
            preview = handler._resolve_preview_output_dir({}, [source])

        crop_path = preview / app_server.MANAGED_CROP_RELATIVE_PATH
        future_publish_path = (
            preview
            / ".publish-staging"
            / app_server.MANAGED_GENERATION_PLACEHOLDER
            / "classin_handoff.json"
        )
        self.assertLessEqual(
            app_server._windows_path_units(crop_path),
            app_server.WINDOWS_MANAGED_PATH_MAX_UNITS,
        )
        self.assertLessEqual(
            app_server._windows_path_units(future_publish_path),
            app_server.WINDOWS_MANAGED_PATH_MAX_UNITS,
        )
        self.assertLess(
            len(preview.name.encode("utf-8")),
            app_server.MANAGED_OUTPUT_DIR_NAME_MAX_BYTES,
        )

    @unittest.skipUnless(sys.platform.startswith("win"), "exercises Windows MAX_PATH budgeting, which app_server only enables on win32")
    def test_long_onedrive_root_budgets_upload_digest_and_hangul_filename(self):
        upload_dir = Path(
            "C:/Users/"
            + ("LongProfileName" * 3)
            + "/OneDrive - International Academy/Documents/ClassInEDBMVP/.app_runtime/uploads"
        )
        target_name = app_server._managed_upload_target_name(
            ("한글자료" * 100) + ".pdf",
            "a" * 64,
            upload_dir=upload_dir,
        )
        target_path = upload_dir / target_name

        self.assertTrue(target_name.startswith(("a" * 64) + "_"))
        self.assertTrue(target_name.endswith(".pdf"))
        self.assertLessEqual(
            app_server._windows_path_units(target_path),
            app_server.WINDOWS_MANAGED_PATH_MAX_UNITS,
        )

    @unittest.skipUnless(sys.platform.startswith("win"), "exercises Windows MAX_PATH budgeting, which app_server only enables on win32")
    def test_long_runtime_root_budgets_export_and_publish_descendants(self):
        handler = object.__new__(app_server.AppRequestHandler)
        runtime_dir = Path(
            "C:/Users/"
            + ("LongProfileName" * 2)
            + "/OneDrive - International Academy/Shared Documents/문서/"
            "ClassInEDBMVP/.app_runtime"
        )
        source = Path(("수학영역_문제지" * 80) + ".pdf")
        stamps = iter(
            (
                "20260824_143057_1787549457154522500_exportstamp",
                "20260824_143058_1787549457154522501_publishstamp",
            )
        )
        with (
            patch.object(app_server, "RUNTIME_DIR", runtime_dir),
            patch.object(app_server, "_unique_artifact_stamp", side_effect=lambda: next(stamps)),
        ):
            output_dir = handler._resolve_output_dir({}, [source])
            export_staging, _export_final = app_server._managed_export_generation_paths(output_dir)
            edb_name, publish_staging, _publish_final = app_server._managed_publish_artifact_paths(
                output_dir,
                "아주 긴 기말고사 EDB 이름" * 40,
                fallback_stem="classin",
            )

        paths = (
            export_staging / app_server.MANAGED_CROP_RELATIVE_PATH,
            publish_staging / "classin_handoff.json",
            publish_staging / app_server._edb_part_file_name(edb_name, 9998, 9999),
        )
        for path in paths:
            self.assertLessEqual(
                app_server._windows_path_units(path),
                app_server.WINDOWS_MANAGED_PATH_MAX_UNITS,
                str(path),
            )
        self.assertTrue(edb_name.endswith(".edb"))

    def test_export_missing_crop_has_actionable_error_code(self):
        error = FileNotFoundError(
            "C:/runtime/outputs/previews/run/problem_crops/problem_001_df48bf64.png"
        )
        payload = app_server._export_error_payload(error)
        self.assertEqual("recognition_asset_missing", payload["code"])
        self.assertEqual("session_export", payload["operation"])
        self.assertTrue(payload["retryable"])

    def test_image_generation_names_are_byte_safe_and_distinct_for_long_ids(self):
        output_dir = Path("/tmp")
        first = app_server._image_generation_output_path(
            output_dir,
            ("긴문항" * 100) + "-first",
            ("긴모델" * 100) + "_retry",
        )
        second = app_server._image_generation_output_path(
            output_dir,
            ("긴문항" * 100) + "-second",
            ("긴모델" * 100) + "_retry",
        )
        self.assertNotEqual(first.name, second.name)
        for path in (first, second):
            self.assertTrue(path.name.endswith(".png"))
            self.assertLessEqual(len(path.name.encode("utf-8")), 255)

    def test_export_default_output_dir_lives_under_runtime_outputs(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            runtime_dir = tmpdir / "runtime"
            source = tmpdir / "Lesson 1.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            handler = object.__new__(app_server.AppRequestHandler)

            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                default_output = handler._resolve_output_dir({}, [source])
                relative_output = handler._resolve_output_dir({"outputDir": "../Old Session?"}, [source])
                absolute_output = handler._resolve_output_dir({"outputDir": str(tmpdir / "custom out")}, [source])

            self.assertEqual((runtime_dir / "outputs").resolve(), default_output.parent)
            self.assertRegex(default_output.name, r"^Lesson_1_[0-9a-f]{10}$")
            self.assertEqual((runtime_dir / "outputs" / "___Old_Session_").resolve(), relative_output)
            self.assertEqual((tmpdir / "custom out").resolve(), absolute_output)

    def test_default_output_dir_distinguishes_same_stem_sources(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            runtime_dir = tmpdir / "runtime"
            first = tmpdir / "a" / "Lesson.pdf"
            second = tmpdir / "b" / "Lesson.pdf"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            handler = object.__new__(app_server.AppRequestHandler)
            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                first_output = handler._resolve_output_dir({}, [first])
                first_again = handler._resolve_output_dir({}, [first])
                second_output = handler._resolve_output_dir({}, [second])
                first.write_bytes(b"changed")
                changed_output = handler._resolve_output_dir({}, [first])
            self.assertEqual(first_output, first_again)
            self.assertNotEqual(first_output, second_output)
            self.assertNotEqual(first_output, changed_output)

    def test_preview_output_dir_is_unique_and_runtime_isolated(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            runtime_dir = tmpdir / "runtime"
            source = tmpdir / "Lesson 1.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            requested_output = tmpdir / "active-output"
            requested_output.mkdir()
            active_sentinel = requested_output / "active.edb"
            active_sentinel.write_bytes(b"active-session-bytes")
            handler = object.__new__(app_server.AppRequestHandler)

            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                first = handler._resolve_preview_output_dir(
                    {"outputDir": str(requested_output)},
                    [source],
                )
                second = handler._resolve_preview_output_dir(
                    {"outputDir": str(requested_output)},
                    [source],
                )

            preview_root = (runtime_dir / "outputs" / "previews").resolve()
            self.assertNotEqual(first, second)
            self.assertEqual(preview_root, first.parent)
            self.assertEqual(preview_root, second.parent)
            self.assertNotEqual(requested_output.resolve(), first)
            self.assertNotEqual(requested_output.resolve(), second)
            self.assertEqual(b"active-session-bytes", active_sentinel.read_bytes())

    def test_repeated_preview_exports_write_unique_dirs_without_touching_active_output(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            runtime_dir = tmpdir / "runtime"
            active_output = tmpdir / "active-output"
            active_output.mkdir()
            active_sentinel = active_output / "problem_crops" / "problem_001.png"
            active_sentinel.parent.mkdir()
            active_sentinel.write_bytes(b"current-session-image")
            source = tmpdir / "source.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            payload = {
                "files": [str(source)],
                "outputDir": str(active_output),
                "preview": True,
                "exportEdb": False,
            }
            written_output_dirs: list[Path] = []

            class FakeServer:
                allowed_files = set()

                def remember_session(self, session):
                    self.latest_session = session

            def fake_run_problem_export(_source, **kwargs):
                resolved_output = Path(kwargs["output_dir"])
                resolved_output.mkdir(parents=True, exist_ok=True)
                (resolved_output / "preview-marker.txt").write_text("preview", encoding="utf-8")
                written_output_dirs.append(resolved_output)
                return {
                    "ok": True,
                    "ui_session": {
                        "pages": [],
                        "problems": [],
                        "output_dir": str(resolved_output),
                    },
                    "output_dir": str(resolved_output),
                    "ui_session_path": str(resolved_output / "ui_session.json"),
                    "edb_path": None,
                    "summary": {"placements": []},
                }

            responses = []
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = FakeServer()
            handler._read_json_body = lambda: dict(payload)
            handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "run_problem_export", side_effect=fake_run_problem_export),
            ):
                handler._handle_export()
                handler._handle_export()

            self.assertEqual(2, len(written_output_dirs))
            self.assertNotEqual(written_output_dirs[0], written_output_dirs[1])
            preview_root = (runtime_dir / "outputs" / "previews").resolve()
            self.assertTrue(all(path.parent == preview_root for path in written_output_dirs))
            self.assertEqual(b"current-session-image", active_sentinel.read_bytes())
            self.assertTrue(all(body["ok"] and body["preview"] for body, _kwargs in responses))

    def test_unique_artifact_stamp_does_not_collide(self):
        stamps = {app_server._unique_artifact_stamp() for _ in range(100)}
        self.assertEqual(100, len(stamps))

    def test_ensure_runtime_dirs_creates_default_output_root(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            base_dir = tmpdir / "base"
            runtime_dir = tmpdir / "runtime"
            upload_dir = runtime_dir / "uploads"

            with (
                patch.object(app_server, "BASE_DIR", base_dir),
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "UPLOAD_DIR", upload_dir),
            ):
                app_server.ensure_runtime_dirs()

            self.assertTrue(base_dir.is_dir())
            self.assertTrue(upload_dir.is_dir())
            self.assertTrue((runtime_dir / "outputs").is_dir())

    def test_same_origin_guard_rejects_cross_site_browser_posts(self):
        self.assertTrue(app_server._request_is_same_origin({
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
        }))
        self.assertFalse(app_server._request_is_same_origin({
            "Host": "127.0.0.1:8765",
            "Origin": "https://example.test",
        }))

    def test_loopback_host_guard_blocks_dns_rebinding_hosts(self):
        for host in ("127.0.0.1:8765", "localhost:8765", "[::1]:8765", "127.42.1.9:8765"):
            with self.subTest(host=host):
                self.assertTrue(app_server._request_host_is_loopback({"Host": host}))
        for host in ("attacker.example:8765", "127.example:8765", "0.0.0.0:8765", "127.0.0.1@attacker.example"):
            with self.subTest(host=host):
                self.assertFalse(app_server._request_host_is_loopback({"Host": host}))

    def test_browser_write_guard_rejects_cross_site_simple_requests(self):
        self.assertFalse(app_server._browser_write_request_is_trusted({
            "Host": "127.0.0.1:8765",
            "Origin": "https://attacker.example",
            "Content-Type": "text/plain",
        }))
        self.assertFalse(app_server._browser_write_request_is_trusted({
            "Host": "127.0.0.1:8765",
            "Sec-Fetch-Site": "cross-site",
        }))
        self.assertTrue(app_server._browser_write_request_is_trusted({
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
        }))
        self.assertTrue(app_server._browser_write_request_is_trusted({
            "Host": "127.0.0.1:8765",
        }))

    def test_update_status_reports_platform_release_from_feed(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "schemaVersion": 1,
                "appId": "ClassInEDBMVP",
                "channel": "stable",
                "version": "0.1.1",
                "publishedAt": "2026-06-19T00:00:00+00:00",
                "manifestUrl": "https://example.test/releases/0.1.2/manifest.json",
                "manifestSha256": VALID_MANIFEST_SHA256,
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS.dmg",
                        "releaseNotesUrl": "https://example.test/releases/0.1.2",
                        "fileName": "ClassInEDBMVP-macOS.dmg",
                        "artifactType": "dmg",
                        "arch": "arm64",
                        "sizeBytes": 12345,
                        "sha256": VALID_ARTIFACT_SHA256,
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.object(app_server.platform, "machine", return_value="arm64"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertTrue(status["ok"])
            self.assertTrue(status["configured"])
            self.assertTrue(status["updateAvailable"])
            self.assertEqual("update_available", status["channelStatus"])
            self.assertEqual("stable", status["channel"])
            self.assertEqual("https://example.test/releases/0.1.2/manifest.json", status["manifestUrl"])
            self.assertEqual("0.1.2", status["latest"]["version"])
            self.assertEqual("ClassInEDBMVP-macOS.dmg", status["latest"]["fileName"])
            self.assertEqual("dmg", status["latest"]["artifactType"])
            self.assertEqual(VALID_ARTIFACT_SHA256, status["latest"]["sha256"])
            self.assertEqual(12345, status["latest"]["sizeBytes"])
            self.assertEqual("https://example.test/ClassInEDBMVP-macOS.dmg", status["downloadUrl"])

    def test_update_status_blocks_incompatible_architecture_download(self):
        config = {
            "appId": "ClassInEDBMVP",
            "appName": "ClassInEDBMVP",
            "platform": "macos",
            "version": "0.1.0",
            "updateFeedUrl": "https://example.test/update.json",
        }
        feed = {
            "appId": "ClassInEDBMVP",
            "platforms": {
                "macos": {
                    "version": "0.2.0",
                    "downloadUrl": "https://example.test/ClassInEDBMVP-arm64.dmg",
                    "fileName": "ClassInEDBMVP-arm64.dmg",
                    "artifactType": "dmg",
                    "arch": "arm64",
                    "sha256": VALID_ARTIFACT_SHA256,
                }
            },
        }
        with (
            patch.object(app_server, "load_app_update_config", return_value=config),
            patch.object(app_server, "_fetch_update_feed", return_value=feed),
            patch.object(app_server.platform, "machine", return_value="x86_64"),
        ):
            status = app_server.build_app_update_status()

        self.assertFalse(status["updateAvailable"])
        self.assertEqual("unsupported_architecture", status["channelStatus"])
        self.assertEqual("update_architecture_mismatch", status["code"])
        self.assertEqual("", status["downloadUrl"])
        self.assertEqual("", status["latest"]["downloadUrl"])

    def test_update_status_reports_snake_case_artifact_metadata_from_feed(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "app_id": "ClassInEDBMVP",
                "version": "0.1.1",
                "manifest_url": "https://example.test/releases/0.1.2/manifest.json",
                "manifest_sha256": VALID_MANIFEST_SHA256,
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "download_url": "https://example.test/ClassInEDBMVP-macOS.zip",
                        "release_notes_url": "https://example.test/releases/0.1.2",
                        "file_name": "ClassInEDBMVP-macOS.zip",
                        "artifact_type": "zip",
                        "size_bytes": "23456",
                        "sha256": VALID_ARTIFACT_SHA256,
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertTrue(status["updateAvailable"])
            self.assertEqual("update_available", status["channelStatus"])
            self.assertEqual("https://example.test/releases/0.1.2/manifest.json", status["manifestUrl"])
            self.assertEqual(VALID_MANIFEST_SHA256, status["manifestSha256"])
            self.assertEqual("ClassInEDBMVP-macOS.zip", status["latest"]["fileName"])
            self.assertEqual("zip", status["latest"]["artifactType"])
            self.assertEqual(VALID_ARTIFACT_SHA256, status["latest"]["sha256"])
            self.assertEqual(23456, status["latest"]["sizeBytes"])
            self.assertEqual("https://example.test/ClassInEDBMVP-macOS.zip", status["downloadUrl"])

    def test_update_status_rejects_conflicting_download_url_aliases(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.1",
                "downloadUrl": "https://example.test/old/ClassInEDBMVP-macOS.zip",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "download_url": "https://example.test/new/ClassInEDBMVP-macOS.zip",
                        "file_name": "ClassInEDBMVP-macOS.zip",
                        "artifact_type": "zip",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual(
                "update feed downloadUrl aliases conflict: "
                "downloadUrl='https://example.test/old/ClassInEDBMVP-macOS.zip', "
                "download_url='https://example.test/new/ClassInEDBMVP-macOS.zip'",
                status["error"],
            )
            self.assertIsNone(status["latest"])

    def test_update_status_rejects_conflicting_artifact_metadata_aliases(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS.dmg",
                        "fileName": "ClassInEDBMVP-macOS-old.dmg",
                        "file_name": "ClassInEDBMVP-macOS.dmg",
                        "artifactType": "dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual(
                "update feed fileName aliases conflict: "
                "fileName='ClassInEDBMVP-macOS-old.dmg', "
                "file_name='ClassInEDBMVP-macOS.dmg'",
                status["error"],
            )
            self.assertIsNone(status["latest"])

    def test_update_status_rejects_mismatched_feed_app_id(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "appId": "OtherApp",
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS.dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual(
                "update feed appId mismatch: expected ClassInEDBMVP, found OtherApp",
                status["error"],
            )
            self.assertEqual("ClassInEDBMVP", status["appId"])
            self.assertIsNone(status["latest"])

    def test_update_status_rejects_mismatched_feed_app_name(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "appId": "ClassInEDBMVP",
                "appName": "Other App",
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS.dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual(
                "update feed appName mismatch: expected ClassInEDBMVP, found Other App",
                status["error"],
            )
            self.assertEqual("ClassInEDBMVP", status["appName"])
            self.assertIsNone(status["latest"])

    def test_update_status_filters_unsafe_manifest_url_from_feed(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "manifestUrl": "http://example.test/releases/0.1.2/manifest.json",
                "manifestSha256": VALID_MANIFEST_SHA256,
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS.dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertTrue(status["updateAvailable"])
            self.assertNotIn("manifestUrl", status)
            self.assertNotIn("manifestUrl", status["latest"])
            self.assertEqual(VALID_MANIFEST_SHA256, status["manifestSha256"])

    def test_update_status_rejects_available_update_without_usable_download_url(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "http://example.test/ClassInEDBMVP-macOS.dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual("update feed does not include a usable download URL", status["error"])
            self.assertEqual("", status["downloadUrl"])

    def test_update_status_rejects_invalid_integrity_digest_from_feed(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "manifestSha256": "not-a-real-sha",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS.dmg",
                        "sha256": VALID_ARTIFACT_SHA256,
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual("update feed has invalid manifestSha256", status["error"])
            self.assertNotIn("manifestSha256", status)

    def test_update_status_rejects_invalid_artifact_size_from_feed(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS.dmg",
                        "sizeBytes": 0,
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual("update feed has invalid sizeBytes", status["error"])
            self.assertNotIn("sizeBytes", status["latest"])

    def test_update_status_rejects_platform_artifact_type_mismatch(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-Setup.exe",
                        "fileName": "ClassInEDBMVP-Setup.exe",
                        "artifactType": "setup-exe",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual("update feed artifactType for macos must be one of: dmg, zip", status["error"])

    def test_update_status_rejects_platform_file_name_mismatch_without_artifact_type(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-Setup.exe",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual("update feed fileName for macos must use one of: .dmg, .zip", status["error"])

    def test_update_status_rejects_file_name_download_url_mismatch(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS-old.dmg",
                        "fileName": "ClassInEDBMVP-macOS.dmg",
                        "artifactType": "dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual(
                "update feed fileName 'ClassInEDBMVP-macOS.dmg' "
                "does not match download URL file name 'ClassInEDBMVP-macOS-old.dmg'",
                status["error"],
            )

    def test_update_status_rejects_snake_case_file_name_download_url_mismatch(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "download_url": "https://example.test/ClassInEDBMVP-macOS-old.dmg",
                        "file_name": "ClassInEDBMVP-macOS.dmg",
                        "artifact_type": "dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual(
                "update feed fileName 'ClassInEDBMVP-macOS.dmg' "
                "does not match download URL file name 'ClassInEDBMVP-macOS-old.dmg'",
                status["error"],
            )

    def test_update_status_rejects_download_url_without_artifact_extension(self):
        invalid_urls = (
            "https://example.test/download",
            "https://example.test/releases/0.1.2/",
        )
        for download_url in invalid_urls:
            with self.subTest(download_url=download_url), TemporaryDirectory() as raw_tmp:
                app_server.clear_app_update_status_cache()
                tmpdir = Path(raw_tmp)
                (tmpdir / "app_update_config.json").write_text(
                    json.dumps({
                        "appName": "ClassInEDBMVP",
                        "version": "0.1.0",
                        "updateFeedUrl": "https://example.test/classin-edb/update.json",
                    }),
                    encoding="utf-8",
                )
                feed = {
                    "version": "0.1.2",
                    "platforms": {
                        "macos": {
                            "version": "0.1.2",
                            "downloadUrl": download_url,
                        }
                    },
                }
                with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                        patch.object(app_server, "BASE_DIR", tmpdir), \
                        patch.object(app_server.sys, "platform", "darwin"), \
                        patch.dict(os.environ, {
                            "EDB_APP_VERSION": "",
                            "EDB_UPDATE_FEED_URL": "",
                            "EDB_DOWNLOAD_URL": "",
                            "EDB_RELEASE_NOTES_URL": "",
                        }), \
                        patch.object(app_server, "_fetch_update_feed", return_value=feed):
                    status = app_server.build_app_update_status()

            self.assertFalse(status["updateAvailable"])
            self.assertEqual("invalid_feed", status["channelStatus"])
            self.assertEqual(
                "update feed download URL for macos must include artifact extension (.dmg, .zip)",
                status["error"],
            )

    def test_update_status_rejects_typed_download_url_without_artifact_extension(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.2",
                "platforms": {
                    "macos": {
                        "version": "0.1.2",
                        "downloadUrl": "https://example.test/releases/0.1.2/",
                        "artifactType": "dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed):
                status = app_server.build_app_update_status()

        self.assertFalse(status["updateAvailable"])
        self.assertEqual("invalid_feed", status["channelStatus"])
        self.assertEqual(
            "update feed download URL must include dmg artifact extension (.dmg)",
            status["error"],
        )

    def test_update_status_caches_feed_fetches_for_short_ttl(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({
                    "appName": "ClassInEDBMVP",
                    "version": "0.1.0",
                    "updateFeedUrl": "https://example.test/classin-edb/update.json",
                }),
                encoding="utf-8",
            )
            feed = {
                "version": "0.1.1",
                "platforms": {
                    "macos": {
                        "version": "0.1.1",
                        "downloadUrl": "https://example.test/ClassInEDBMVP-macOS.dmg",
                    }
                },
            }
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.object(app_server.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }), \
                    patch.object(app_server, "_fetch_update_feed", return_value=feed) as fetch_feed:
                first = app_server.build_app_update_status()
                second = app_server.build_app_update_status()
                allowed = app_server._allowed_update_urls()

            self.assertTrue(first["updateAvailable"])
            self.assertEqual(first, second)
            self.assertIn("https://example.test/ClassInEDBMVP-macOS.dmg", allowed)
            self.assertEqual(1, fetch_feed.call_count)

    def test_update_status_is_safe_when_channel_is_unconfigured(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            (tmpdir / "app_update_config.json").write_text(
                json.dumps({"version": "0.3.0"}),
                encoding="utf-8",
            )
            with patch.object(app_server, "RESOURCE_DIR", tmpdir), \
                    patch.object(app_server, "BASE_DIR", tmpdir), \
                    patch.dict(os.environ, {
                        "EDB_APP_VERSION": "",
                        "EDB_UPDATE_FEED_URL": "",
                        "EDB_DOWNLOAD_URL": "",
                        "EDB_RELEASE_NOTES_URL": "",
                    }):
                status = app_server.build_app_update_status()

            self.assertTrue(status["ok"])
            self.assertFalse(status["configured"])
            self.assertFalse(status["updateAvailable"])
            self.assertEqual("not_configured", status["channelStatus"])
            self.assertEqual("0.3.0", status["currentVersion"])

    def test_open_url_rejects_unconfigured_url_before_browser_open(self):
        handler = object.__new__(app_server.AppRequestHandler)
        payload = json.dumps({"url": "https://example.test/not-configured"}).encode("utf-8")
        handler.headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "Content-Length": str(len(payload)),
        }
        handler.rfile = io.BytesIO(payload)
        handler.wfile = io.BytesIO()
        statuses = []
        handler.send_response = lambda status: statuses.append(status)
        handler.send_header = lambda _name, _value: None
        handler.end_headers = lambda: None

        with patch.object(app_server, "_allowed_update_urls", return_value=set()), \
                patch.object(app_server.webbrowser, "open", side_effect=AssertionError("browser should not open")):
            handler._handle_open_url()

        self.assertEqual([app_server.HTTPStatus.FORBIDDEN], statuses)
        self.assertIn(b"not in the configured update metadata", handler.wfile.getvalue())

    def test_json_body_rejects_oversized_content_length(self):
        handler = object.__new__(app_server.AppRequestHandler)
        handler.headers = {"Content-Length": str(app_server.MAX_JSON_BODY_BYTES + 1)}
        handler.rfile = io.BytesIO(b"{}")

        with self.assertRaises(app_server.RequestPayloadTooLarge):
            handler._read_json_body()

    def test_export_returns_413_with_recovery_steps_for_oversized_payload(self):
        handler = object.__new__(app_server.AppRequestHandler)
        handler.path = "/api/export"
        handler.headers = {"Content-Length": str(app_server.MAX_JSON_BODY_BYTES + 1)}
        handler.rfile = io.BytesIO(b"")
        responses = []
        handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

        handler.do_POST()

        payload, kwargs = responses[0]
        self.assertEqual(app_server.HTTPStatus.REQUEST_ENTITY_TOO_LARGE, kwargs["status"])
        self.assertEqual("payload_too_large", payload["code"])
        self.assertEqual(app_server.MAX_JSON_BODY_BYTES, payload["maxBytes"])
        self.assertEqual(2, len(payload["recoverySteps"]))

    def test_static_responses_disable_browser_cache(self):
        handler = object.__new__(app_server.AppRequestHandler)
        headers = []
        handler.send_header = lambda name, value: headers.append((name, value))

        with patch.object(app_server.SimpleHTTPRequestHandler, "end_headers", lambda _self: headers.append(("END", ""))):
            handler.end_headers()

        self.assertIn(("Cache-Control", "no-store, max-age=0"), headers)
        self.assertIn(("Pragma", "no-cache"), headers)

    def test_legacy_app_js_requests_serve_current_bundle(self):
        handler = object.__new__(app_server.AppRequestHandler)
        handler.path = "/app.js?v=old-ui"
        served_paths = []

        def fake_static_get(static_handler):
            served_paths.append(static_handler.path)

        with patch.object(app_server.SimpleHTTPRequestHandler, "do_GET", fake_static_get):
            handler.do_GET()

        self.assertEqual(["/app.bundle.js"], served_paths)

    def test_legacy_app_js_head_requests_serve_current_bundle(self):
        handler = object.__new__(app_server.AppRequestHandler)
        handler.path = "/app.js?v=old-ui"
        served_paths = []

        def fake_static_head(static_handler):
            served_paths.append(static_handler.path)

        with patch.object(app_server.SimpleHTTPRequestHandler, "do_HEAD", fake_static_head):
            handler.do_HEAD()

        self.assertEqual(["/app.bundle.js"], served_paths)

    def test_generated_session_script_is_always_empty_bridge(self):
        with TemporaryDirectory() as raw_tmp:
            generated_path = Path(raw_tmp) / "generated_session.js"
            generated_path.write_text(
                'window.EDB_UI_SESSION = {"problems":[{"id":"stale-session"}]};\n',
                encoding="utf-8",
            )
            handler = object.__new__(app_server.AppRequestHandler)
            handler.path = "/generated_session.js"
            statuses = []
            headers = []
            handler.wfile = io.BytesIO()
            handler.send_response = lambda status: statuses.append(status)
            handler.send_header = lambda name, value: headers.append((name, value))
            handler.end_headers = lambda: None

            with patch.object(app_server, "GENERATED_SESSION_JS", generated_path):
                handler.do_GET()

            self.assertEqual([app_server.HTTPStatus.OK], statuses)
            self.assertIn(("Content-Type", "application/javascript; charset=utf-8"), headers)
            self.assertEqual(b"window.EDB_UI_SESSION = null;\n", handler.wfile.getvalue())

    def test_generated_session_placeholder_overwrites_stale_bridge(self):
        with TemporaryDirectory() as raw_tmp:
            generated_path = Path(raw_tmp) / "generated_session.js"
            generated_path.write_text(
                'window.EDB_UI_SESSION = {"problems":[{"id":"stale-session"}]};\n',
                encoding="utf-8",
            )

            with patch.object(app_server, "GENERATED_SESSION_JS", generated_path):
                app_server.write_placeholder_generated_session()

            self.assertEqual("window.EDB_UI_SESSION = null;\n", generated_path.read_text(encoding="utf-8"))

    def test_latest_session_does_not_fallback_to_generated_session_bridge(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            latest_path = tmpdir / "missing_latest.json"
            generated_path = tmpdir / "generated_session.js"
            generated_path.write_text(
                'window.EDB_UI_SESSION = {"problems":[{"id":"old-session"}]};\n',
                encoding="utf-8",
            )

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                self.assertIsNone(app_server.load_latest_session())

    def test_file_download_streams_without_reading_entire_artifact(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            artifact = tmpdir / "large.edb"
            payload = (b"0123456789abcdef" * 70000) + b"tail"
            artifact.write_bytes(payload)
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {
                "allowed_files": {str(artifact.resolve())},
            })()
            statuses = []
            headers = []
            handler.wfile = io.BytesIO()
            handler.send_response = lambda status: statuses.append(status)
            handler.send_header = lambda name, value: headers.append((name, value))
            handler.end_headers = lambda: None
            handler.send_error = lambda status, message=None: statuses.append(status)

            parsed = app_server.urlparse(app_server.path_to_api_url(artifact))
            with patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes should not be used for downloads")):
                handler._handle_file(parsed)

            self.assertEqual([app_server.HTTPStatus.OK], statuses)
            self.assertIn(("Content-Length", str(len(payload))), headers)
            self.assertEqual(payload, handler.wfile.getvalue())

    def test_file_preview_query_downscales_image_without_changing_source(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            artifact = tmpdir / "large-preview.png"
            Image.new("RGB", (2400, 1600), (245, 245, 240)).save(artifact)
            original_size = artifact.stat().st_size
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {
                "allowed_files": {str(artifact.resolve())},
            })()
            statuses = []
            headers = []
            handler.wfile = io.BytesIO()
            handler.send_response = lambda status: statuses.append(status)
            handler.send_header = lambda name, value: headers.append((name, value))
            handler.end_headers = lambda: None
            handler.send_error = lambda status, message=None: statuses.append(status)

            app_server._build_file_preview_payload.cache_clear()
            parsed = app_server.urlparse(
                f"{app_server.path_to_api_url(artifact)}&previewMax=1024"
            )
            handler._handle_file(parsed)

            self.assertEqual([app_server.HTTPStatus.OK], statuses)
            response_headers = dict(headers)
            self.assertEqual("image/jpeg", response_headers["Content-Type"])
            self.assertEqual("1024", response_headers["X-Preview-Max-Dimension"])
            with Image.open(io.BytesIO(handler.wfile.getvalue())) as preview:
                self.assertEqual((1024, 683), preview.size)
            with Image.open(artifact) as source:
                self.assertEqual((2400, 1600), source.size)
            self.assertEqual(original_size, artifact.stat().st_size)
            app_server._build_file_preview_payload.cache_clear()

    def test_file_download_marks_zip_as_attachment(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            artifact = tmpdir / "수업_EDB_파일.zip"
            artifact.write_bytes(b"zip-payload")
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {
                "allowed_files": {str(artifact.resolve())},
            })()
            statuses = []
            headers = []
            handler.wfile = io.BytesIO()
            handler.send_response = lambda status: statuses.append(status)
            handler.send_header = lambda name, value: headers.append((name, value))
            handler.end_headers = lambda: None
            handler.send_error = lambda status, message=None: statuses.append(status)

            handler._handle_file(app_server.urlparse(app_server.path_to_api_url(artifact)))

            self.assertEqual([app_server.HTTPStatus.OK], statuses)
            self.assertEqual("application/zip", dict(headers)["Content-Type"])
            disposition = dict(headers)["Content-Disposition"]
            self.assertIn("attachment", disposition)
            self.assertIn("filename*=UTF-8''", disposition)

    def test_file_download_blocks_reset_and_destructive_cleanup_until_stream_finishes(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            export_dir = runtime_dir / "exports"
            export_dir.mkdir(parents=True)
            artifact = export_dir / "lesson.edb"
            payload = b"download-in-progress"
            artifact.write_bytes(payload)
            os.utime(artifact, (1, 1))
            started = threading.Event()
            release = threading.Event()
            real_copyfileobj = shutil.copyfileobj

            def blocking_copy(source, destination, length=0):
                started.set()
                release.wait(timeout=5)
                return real_copyfileobj(source, destination, length=length)

            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "LATEST_SESSION_JSON", runtime_dir / "latest.json"),
                patch.object(app_server, "SESSION_HISTORY_JSON", runtime_dir / "history.json"),
                patch.object(app_server, "GENERATED_SESSION_JS", runtime_dir / "generated.js"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                server.add_allowed_files({str(artifact.resolve())})
                handler = object.__new__(app_server.AppRequestHandler)
                handler.server = server
                handler.path = app_server.path_to_api_url(artifact)
                handler.wfile = io.BytesIO()
                handler.send_response = lambda _status: None
                handler.send_header = lambda _name, _value: None
                handler.end_headers = lambda: None
                handler.send_error = lambda status, message=None: self.fail(f"unexpected {status}: {message}")
                worker = threading.Thread(target=handler.do_GET)
                try:
                    with patch.object(app_server.shutil, "copyfileobj", side_effect=blocking_copy):
                        worker.start()
                        self.assertTrue(started.wait(timeout=5))
                        with self.assertRaises(app_server.ArtifactCleanupBusy):
                            server.clear_session(
                                expected_revision=0,
                                cleanup_artifacts=True,
                                dry_run=False,
                                min_age_seconds=0,
                            )
                        self.assertTrue(artifact.exists())
                        release.set()
                        worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(payload, handler.wfile.getvalue())
                    cleanup = server.clear_session(
                        expected_revision=0,
                        cleanup_artifacts=True,
                        dry_run=False,
                        min_age_seconds=0,
                    )
                    self.assertEqual(1, cleanup["deletedFileCount"])
                    self.assertFalse(artifact.exists())
                finally:
                    release.set()
                    worker.join(timeout=5)
                    server.server_close()

    def test_problem_image_download_streams_named_png_attachment(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            crop = tmpdir / "crop.png"
            Image.new("RGB", (12, 8), "white").save(crop)
            payload = crop.read_bytes()
            session = {
                "problems": [{
                    "id": "p1",
                    "title": "문항 1",
                    "imagePath": crop.resolve().as_uri(),
                    "boardRenderPath": crop.resolve().as_uri(),
                }],
            }
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {
                "latest_session": session,
            })()
            statuses = []
            headers = []
            handler.wfile = io.BytesIO()
            handler.send_response = lambda status: statuses.append(status)
            handler.send_header = lambda name, value: headers.append((name, value))
            handler.end_headers = lambda: None
            handler.send_error = lambda status, message=None: statuses.append(status)

            parsed = app_server.urlparse("/api/session/problem-image?problemId=p1")
            handler._handle_session_problem_image(parsed)

            self.assertEqual([app_server.HTTPStatus.OK], statuses)
            self.assertIn(("Content-Type", "image/png"), headers)
            self.assertIn(("Content-Length", str(len(payload))), headers)
            disposition = dict(headers)["Content-Disposition"]
            self.assertIn("filename*=UTF-8''01_%EB%AC%B8%ED%95%AD_1.png", disposition)
            self.assertEqual(payload, handler.wfile.getvalue())

    def test_problem_image_post_renders_s3_from_payload_session(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            crop = tmpdir / "crop.png"
            Image.new("RGB", (12, 8), "white").save(crop)
            latest_session = {
                "problems": [{
                    "id": "p1",
                    "title": "최신",
                    "imagePath": crop.resolve().as_uri(),
                    "boardRenderPath": crop.resolve().as_uri(),
                    "step": "s1",
                }],
            }
            payload_session = {
                "board_theme": "charcoal",
                "problems": [{
                    "id": "p1",
                    "title": "선택본",
                    "imagePath": crop.resolve().as_uri(),
                    "boardRenderPath": crop.resolve().as_uri(),
                    "step": "s3",
                    "processingStep": "s3",
                }],
            }
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {
                "latest_session": latest_session,
            })()
            handler._read_json_body = lambda: {"problemId": "p1", "session": payload_session}
            statuses = []
            headers = []
            handler.wfile = io.BytesIO()
            handler.send_response = lambda status: statuses.append(status)
            handler.send_header = lambda name, value: headers.append((name, value))
            handler.end_headers = lambda: None
            handler.send_error = lambda status, message=None: statuses.append(status)

            rendered = Image.new("RGBA", (3, 2), (255, 0, 0, 200))
            with patch.object(app_server, "_build_transparent_reconstruction_image", return_value=rendered):
                handler._handle_session_problem_image_post()

            self.assertEqual([app_server.HTTPStatus.OK], statuses)
            self.assertIn(("Content-Type", "image/png"), headers)
            with Image.open(io.BytesIO(handler.wfile.getvalue())) as downloaded:
                self.assertEqual((3, 2), downloaded.size)
                self.assertEqual((255, 0, 0, 200), downloaded.convert("RGBA").getpixel((0, 0)))

    def test_problem_image_download_returns_404_without_image(self):
        session = {"problems": [{"id": "p1", "title": "문항 1"}]}
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = type("FakeServer", (), {
            "latest_session": session,
        })()
        statuses = []
        handler.send_error = lambda status, message=None: statuses.append((status, message))

        parsed = app_server.urlparse("/api/session/problem-image?problemId=p1")
        handler._handle_session_problem_image(parsed)

        self.assertEqual([(app_server.HTTPStatus.NOT_FOUND, "problem image not found")], statuses)


def _build_session(tmpdir: Path, *, present_page_ids: set[str]) -> dict:
    pages = []
    problems = []
    for page_id in ("page-1", "page-2"):
        if page_id in present_page_ids:
            image_path = tmpdir / f"{page_id}.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            source_uri = image_path.resolve().as_uri()
        else:
            source_uri = (tmpdir / f"missing_{page_id}.png").resolve().as_uri()
        problem_id = f"{page_id}-p1"
        pages.append({
            "id": page_id,
            "sourceImageUri": source_uri,
            "problemIds": [problem_id],
            "riskFlags": ["needs_review"],
        })
        problems.append({
            "id": problem_id,
            "sourcePageId": page_id,
            "bbox": {"left": 0, "top": 0, "width": 100, "height": 100},
            "riskFlags": ["needs_review"],
        })
    return {
        "pages": pages,
        "problems": problems,
        "ai_fallback": {"provider": "gemini"},
    }


def _fake_run_problem_export(source_path, **_kwargs):
    page_id = Path(source_path).stem
    return {
        "ui_session": {
            "pages": [{
                "id": page_id,
                "riskFlags": [],
            }],
            "problems": [{
                "id": f"{page_id}-new",
                "sourcePageId": page_id,
                "bbox": {"left": 0, "top": 0, "width": 80, "height": 80},
                "riskFlags": [],
            }],
            "ai_summary": {"applied": True},
        }
    }


class TestRetryAiResilience(unittest.TestCase):
    def setUp(self):
        self._prev_key = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "test-key"

    def tearDown(self):
        if self._prev_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = self._prev_key

    def test_missing_source_marks_page_failed_and_continues(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            session = _build_session(tmpdir, present_page_ids={"page-2"})

            with patch.object(app_server, "run_problem_export", side_effect=_fake_run_problem_export):
                new_session = app_server._mutate_retry_ai(
                    session,
                    {"pageIds": ["page-1", "page-2"]},
                )

            summaries = new_session.get("ai_retry_summary") or []
            statuses = {row["pageId"]: row["status"] for row in summaries}
            self.assertEqual(statuses, {"page-1": "missing_source", "page-2": "applied"})

            page_1 = next(p for p in new_session["pages"] if p["id"] == "page-1")
            self.assertEqual(page_1["reviewStatus"], "failed")
            self.assertIn("ai_retry_missing_source", page_1["riskFlags"])
            self.assertEqual(page_1["aiRetry"]["status"], "missing_source")
            # page-1's original problem is untouched (no replacement on missing-source)
            page_1_problem_ids = page_1["problemIds"]
            self.assertEqual(page_1_problem_ids, ["page-1-p1"])

            page_2 = next(p for p in new_session["pages"] if p["id"] == "page-2")
            self.assertEqual(page_2["aiRetry"]["status"], "applied")
            self.assertEqual(page_2["aiRetry"]["replacedProblemCount"], 1)
            # page-2's old problem was replaced
            new_problem_ids = {prob["id"] for prob in new_session["problems"]}
            self.assertNotIn("page-2-p1", new_problem_ids)
            self.assertIn("page-1-p1", new_problem_ids)

    def test_retry_ai_clamps_forced_ai_worker_count_from_env(self):
        previous_workers = os.environ.get("EDB_RECOGNITION_WORKERS")
        previous_ai_workers = os.environ.get("EDB_AI_MAX_WORKERS")
        os.environ["EDB_RECOGNITION_WORKERS"] = "8"
        os.environ.pop("EDB_AI_MAX_WORKERS", None)
        try:
            with TemporaryDirectory() as raw_tmp:
                tmpdir = Path(raw_tmp)
                pages = []
                problems = []
                page_ids = []
                for index in range(1, 5):
                    page_id = f"page-{index}"
                    page_ids.append(page_id)
                    image_path = tmpdir / f"{page_id}.png"
                    image_path.write_bytes(b"\x89PNG\r\n\x1a\n")
                    problem_id = f"{page_id}-p1"
                    pages.append({
                        "id": page_id,
                        "sourceImageUri": image_path.resolve().as_uri(),
                        "problemIds": [problem_id],
                        "riskFlags": [],
                    })
                    problems.append({
                        "id": problem_id,
                        "sourcePageId": page_id,
                        "bbox": {"left": 0, "top": 0, "width": 100, "height": 100},
                        "riskFlags": ["needs_review"],
                    })
                session = {
                    "pages": pages,
                    "problems": problems,
                    "ai_fallback": {"provider": "gemini"},
                }

                with patch.object(app_server, "run_problem_export", side_effect=_fake_run_problem_export):
                    new_session = app_server._mutate_retry_ai(session, {"pageIds": page_ids})

                summaries = new_session.get("ai_retry_summary") or []
                self.assertEqual(4, len(summaries))
                self.assertEqual({3}, {summary.get("workerCount") for summary in summaries})
        finally:
            if previous_workers is None:
                os.environ.pop("EDB_RECOGNITION_WORKERS", None)
            else:
                os.environ["EDB_RECOGNITION_WORKERS"] = previous_workers
            if previous_ai_workers is None:
                os.environ.pop("EDB_AI_MAX_WORKERS", None)
            else:
                os.environ["EDB_AI_MAX_WORKERS"] = previous_ai_workers

    def test_missing_key_rejects_before_any_mutation(self):
        os.environ.pop("GEMINI_API_KEY", None)
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            session = _build_session(tmpdir, present_page_ids={"page-1", "page-2"})
            with self.assertRaises(ValueError) as ctx:
                app_server._mutate_retry_ai(session, {"pageIds": ["page-1"]})
            self.assertIn("Gemini API", str(ctx.exception))
            # No ai_retry_summary, no page-level aiRetry should have been written.
            self.assertNotIn("ai_retry_summary", session)
            for page in session["pages"]:
                self.assertNotIn("aiRetry", page)

    def test_partial_retry_preserves_selected_problem_identity_order_and_offsets_bbox(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page-1.png"
            Image.new("RGB", (240, 180), "white").save(page_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["p1", "p2"],
                }],
                "problems": [
                    {
                        "id": "p1",
                        "title": "기존 1번",
                        "problemNumber": "1",
                        "sourcePageId": "page-1",
                        "bbox": {"left": 20, "top": 30, "width": 80, "height": 90},
                        "riskFlags": ["needs_review"],
                    },
                    {
                        "id": "p2",
                        "sourcePageId": "page-1",
                        "bbox": {"left": 120, "top": 30, "width": 80, "height": 90},
                        "riskFlags": [],
                    },
                ],
                "ai_fallback": {"provider": "gemini"},
            }

            def fake_partial_export(source_path, **kwargs):
                self.assertEqual("partial_source.png", Path(source_path).name)
                self.assertEqual("single-problem", kwargs["input_intent"])
                return {
                    "ui_session": {
                        "pages": [{"id": "partial", "riskFlags": []}],
                        "problems": [{
                            "id": "partial-p1",
                            "sourcePageId": "partial",
                            "bbox": {"left": 2, "top": 3, "width": 40, "height": 50},
                            "riskFlags": [],
                        }],
                    }
                }

            with patch.object(app_server, "run_problem_export", side_effect=fake_partial_export):
                new_session = app_server._mutate_retry_ai(
                    session,
                    {
                        "partial": True,
                        "problemIds": ["p1"],
                        "cropBox": {"left": 10, "top": 20, "width": 80, "height": 90},
                    },
                )

            problem_ids = [problem["id"] for problem in new_session["problems"]]
            self.assertEqual(["p1", "p2"], problem_ids)
            replacement = next(problem for problem in new_session["problems"] if problem["id"] == "p1")
            self.assertEqual("기존 1번", replacement["title"])
            self.assertEqual("1", replacement["problemNumber"])
            self.assertEqual("page-1", replacement["sourcePageId"])
            self.assertEqual(12.0, replacement["bbox"]["left"])
            self.assertEqual(23.0, replacement["bbox"]["top"])
            self.assertEqual(40.0, replacement["bbox"]["width"])
            self.assertEqual(50.0, replacement["bbox"]["height"])
            self.assertTrue(replacement["aiRetry"]["partial"])
            self.assertTrue(replacement["aiRetry"]["preservedProblemIdentity"])
            self.assertEqual(["p1", "p2"], new_session["pages"][0]["problemIds"])
            self.assertEqual("applied", new_session["ai_retry_summary"][0]["status"])
            self.assertTrue(new_session["ai_retry_summary"][0]["partial"])
            self.assertTrue(new_session["ai_retry_summary"][0]["preservedProblemIdentity"])

    def test_partial_retry_collapses_multiple_candidates_into_one_preserved_problem(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page-1.png"
            Image.new("RGB", (240, 180), "white").save(page_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["p1", "p2"],
                }],
                "problems": [
                    {
                        "id": "p1",
                        "title": "기존 1번",
                        "problemNumber": "1",
                        "sourcePageId": "page-1",
                        "bbox": {"left": 20, "top": 30, "width": 80, "height": 90},
                        "riskFlags": ["needs_review"],
                    },
                    {
                        "id": "p2",
                        "title": "기존 2번",
                        "problemNumber": "2",
                        "sourcePageId": "page-1",
                        "bbox": {"left": 120, "top": 30, "width": 80, "height": 90},
                        "riskFlags": [],
                    },
                ],
                "ai_fallback": {"provider": "gemini"},
            }

            def fake_partial_export(_source_path, **_kwargs):
                return {
                    "ui_session": {
                        "pages": [{"id": "partial", "riskFlags": []}],
                        "problems": [
                            {
                                "id": "partial-p1",
                                "sourcePageId": "partial",
                                "bbox": {"left": 2, "top": 3, "width": 40, "height": 50},
                                "riskFlags": [],
                            },
                            {
                                "id": "partial-p2",
                                "sourcePageId": "partial",
                                "bbox": {"left": 44, "top": 6, "width": 20, "height": 30},
                                "riskFlags": [],
                            },
                        ],
                    }
                }

            with patch.object(app_server, "run_problem_export", side_effect=fake_partial_export):
                new_session = app_server._mutate_retry_ai(
                    session,
                    {
                        "partial": True,
                        "problemIds": ["p1"],
                        "cropBox": {"left": 10, "top": 20, "width": 80, "height": 90},
                    },
                )

            self.assertEqual(["p1", "p2"], [problem["id"] for problem in new_session["problems"]])
            self.assertEqual(["p1", "p2"], new_session["pages"][0]["problemIds"])
            replacement = new_session["problems"][0]
            self.assertEqual({"left": 12.0, "top": 23.0, "width": 62.0, "height": 50.0}, replacement["bbox"])
            self.assertEqual("기존 1번", replacement["title"])
            self.assertEqual("1", replacement["problemNumber"])
            self.assertEqual(2, replacement["aiRetry"]["detectedProblemCount"])
            self.assertTrue(replacement["aiRetry"]["collapsedMultipleCandidates"])
            self.assertIn("ai_partial_retry_multiple_candidates", replacement["riskFlags"])
            self.assertEqual("check_needed", replacement["reviewStatus"])
            self.assertEqual(1, new_session["ai_retry_summary"][0]["replacedProblemCount"])
            self.assertEqual(2, new_session["ai_retry_summary"][0]["detectedProblemCount"])


class TestSessionExcludeMutation(unittest.TestCase):
    def _session(self) -> dict:
        return {
            "pages": [
                {"id": "page-1", "problemIds": ["p1", "p2"]},
                {"id": "page-2", "problemIds": ["p3"]},
            ],
            "problems": [
                {"id": "p1", "sourcePageId": "page-1", "bbox": {"width": 100, "height": 80}},
                {"id": "p2", "sourcePageId": "page-1", "bbox": {"width": 100, "height": 80}},
                {"id": "p3", "sourcePageId": "page-2", "bbox": {"width": 100, "height": 80}},
            ],
        }

    def test_bulk_exclude_removes_multiple_problems_and_page_links(self):
        session = self._session()

        new_session = app_server._mutate_exclude_many(session, ["p1", "p3"])

        self.assertIs(new_session, session)
        self.assertEqual(["p2"], [problem["id"] for problem in new_session["problems"]])
        self.assertEqual(["p2"], new_session["pages"][0]["problemIds"])
        self.assertEqual([], new_session["pages"][1]["problemIds"])
        self.assertEqual(1, new_session["detected_problem_count"])
        self.assertEqual(1, new_session["detectedProblemCount"])

    def test_session_mutate_exclude_accepts_problem_ids_payload(self):
        session = self._session()

        class FakeServer:
            def __init__(self, latest_session):
                self.latest_session = latest_session
                self.remembered_session = None

            def remember_session(self, new_session):
                self.latest_session = new_session
                self.remembered_session = new_session

        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = FakeServer(session)
        handler._read_json_body = lambda: {"action": "exclude", "problemIds": ["p1", "p3"]}
        responses = []
        handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

        handler._handle_session_mutate()

        self.assertEqual(1, len(responses))
        payload, _kwargs = responses[0]
        self.assertTrue(payload["ok"])
        remembered = handler.server.remembered_session
        self.assertIsNotNone(remembered)
        self.assertEqual(["p2"], [problem["id"] for problem in remembered["problems"]])

    def test_exclude_cleans_snake_case_page_links_and_review_focus(self):
        session = {
            "pages": [
                {"id": "page-1", "problem_ids": ["p1", "p2"]},
            ],
            "problems": [
                {"id": "p1", "sourcePageId": "page-1", "bbox": {"width": 100, "height": 80}},
                {"id": "p2", "sourcePageId": "page-1", "bbox": {"width": 100, "height": 80}},
            ],
            "reviewFocus": {"filter": "all", "problemIds": ["p1"]},
            "review_focus": {"filter": "all", "problem_ids": ["p1"]},
        }

        new_session = app_server._mutate_exclude_many(session, ["p1"])

        self.assertEqual(["p2"], [problem["id"] for problem in new_session["problems"]])
        self.assertEqual(["p2"], new_session["pages"][0]["problemIds"])
        self.assertEqual(["p2"], new_session["pages"][0]["problem_ids"])
        self.assertNotIn("reviewFocus", new_session)
        self.assertNotIn("review_focus", new_session)

    def test_rewrite_session_for_http_normalizes_legacy_problem_links(self):
        session = {
            "pages": [
                {"id": "page-1", "problem_ids": ["p1", "p2"]},
            ],
            "problems": [
                {"id": "p1", "source_page_id": "page-1"},
                {"id": "p2", "sourcePageId": "page-1"},
            ],
        }

        rewritten = app_server.rewrite_session_for_http(session)

        self.assertEqual(["p1", "p2"], rewritten["pages"][0]["problemIds"])
        self.assertEqual(["p1", "p2"], rewritten["pages"][0]["problem_ids"])
        self.assertEqual("page-1", rewritten["problems"][0]["sourcePageId"])


class TestSessionConfirmMutation(unittest.TestCase):
    def test_confirm_clears_problem_and_completed_page_review_state(self):
        session = {
            "pages": [{
                "id": "page-1",
                "problemIds": ["p1", "p2"],
                "riskFlags": ["needs_review"],
                "reviewStatus": "check_needed",
            }],
            "problems": [
                {
                    "id": "p1",
                    "riskFlags": ["parse_failed"],
                    "reviewStatus": "failed",
                    "parseFailed": True,
                },
                {"id": "p2", "riskFlags": [], "reviewStatus": "normal"},
            ],
        }

        updated = app_server._mutate_confirm(session, ["p1"])

        self.assertIs(updated, session)
        self.assertEqual([], updated["problems"][0]["riskFlags"])
        self.assertEqual([], updated["problems"][0]["risk_flags"])
        self.assertEqual("normal", updated["problems"][0]["reviewStatus"])
        self.assertEqual("normal", updated["problems"][0]["review_status"])
        self.assertFalse(updated["problems"][0]["parseFailed"])
        self.assertFalse(updated["problems"][0]["parse_failed"])
        self.assertEqual([], updated["pages"][0]["riskFlags"])
        self.assertEqual("normal", updated["pages"][0]["reviewStatus"])

    def test_confirm_rejects_unknown_problem(self):
        with self.assertRaisesRegex(ValueError, "problem not found: missing"):
            app_server._mutate_confirm({"problems": [{"id": "p1"}]}, ["missing"])

    def test_confirm_page_resolves_empty_page_and_preserves_review_decision(self):
        session = {
            "pages": [{
                "id": "page-3",
                "problemIds": [],
                "riskFlags": [],
                "reviewStatus": "failed",
            }],
            "problems": [],
        }

        updated = app_server._mutate_confirm_pages(session, ["page-3"])

        page = updated["pages"][0]
        self.assertEqual("normal", page["reviewStatus"])
        self.assertEqual("normal", page["review_status"])
        self.assertEqual([], page["riskFlags"])
        self.assertTrue(page["pageReviewConfirmed"])
        self.assertTrue(page["page_review_confirmed"])
        self.assertEqual("no_passage", page["pageReviewDecision"])
        self.assertEqual("no_passage", page["page_review_decision"])

    def test_confirm_page_rejects_page_that_still_has_items(self):
        session = {
            "pages": [{"id": "page-1", "problemIds": ["p1"]}],
            "problems": [{"id": "p1"}],
        }

        with self.assertRaisesRegex(ValueError, "page still has review items: page-1"):
            app_server._mutate_confirm_pages(session, ["page-1"])

    def test_review_summary_counts_unconfirmed_failed_empty_page(self):
        session = {
            "pages": [{"id": "page-3", "problemIds": [], "reviewStatus": "failed"}],
            "problems": [],
        }

        self.assertEqual(1, app_server._session_review_summary(session)["actionableNeedsReviewCount"])

        app_server._mutate_confirm_pages(session, ["page-3"])
        self.assertEqual(0, app_server._session_review_summary(session)["actionableNeedsReviewCount"])


class TestSessionClassifyMutation(unittest.TestCase):
    def test_classify_round_trip_uses_only_separate_shared_passage_role(self):
        session = {
            "pages": [{"id": "page-1", "problemIds": ["p1"]}],
            "problems": [{"id": "p1", "sourcePageId": "page-1", "metadata": {}}],
        }

        updated = app_server._mutate_classify(session, "p1", "shared-passage")
        passage = updated["problems"][0]
        self.assertEqual("passage_fragment", passage["passageRole"])
        self.assertTrue(passage["supplementalItem"])
        self.assertEqual("manual-passage-p1", passage["passageGroupId"])
        self.assertEqual("passage_fragment", passage["metadata"]["passage_role"])

        updated = app_server._mutate_classify(session, "p1", "question")
        question = updated["problems"][0]
        self.assertNotIn("passageRole", question)
        self.assertNotIn("passageGroupId", question)
        self.assertFalse(question["supplementalItem"])
        self.assertEqual("manual", question["classificationSource"])

    def test_classify_rejects_unsupported_value(self):
        with self.assertRaisesRegex(ValueError, "classification must be"):
            app_server._mutate_classify({"problems": [{"id": "p1"}]}, "p1", "table")

    def test_classify_many_updates_each_selected_problem(self):
        session = {
            "pages": [{"id": "page-1", "problemIds": ["p1", "p2", "p3"]}],
            "problems": [
                {"id": "p1", "sourcePageId": "page-1", "metadata": {}},
                {"id": "p2", "sourcePageId": "page-1", "metadata": {}},
                {"id": "p3", "sourcePageId": "page-1", "metadata": {}},
            ],
        }

        updated = app_server._mutate_classify_many(
            session,
            ["p1", "p2", "p1"],
            "shared-passage",
        )
        by_id = {problem["id"]: problem for problem in updated["problems"]}
        self.assertEqual("passage_fragment", by_id["p1"]["passageRole"])
        self.assertEqual("passage_fragment", by_id["p2"]["passageRole"])
        self.assertNotIn("passageRole", by_id["p3"])

    def test_classify_many_rejects_empty_ids(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            app_server._mutate_classify_many({"problems": [{"id": "p1"}]}, [], "question")


class TestSessionCropMutation(unittest.TestCase):
    def test_bbox_crop_wraps_fractional_edges_outward(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source_path = tmpdir / "page.png"
            output_path = tmpdir / "crop.png"
            Image.new("RGB", (80, 60), "white").save(source_path)

            size = app_server._crop_image_by_bbox(
                source_path,
                Box(10.2, 20.2, 20.1, 10.1),
                output_path,
            )

            self.assertEqual((21, 11), size)
            with Image.open(output_path) as crop:
                self.assertEqual((21, 11), crop.size)

    def test_manual_crop_updates_bbox_image_and_can_reset(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source_path = tmpdir / "problem.png"
            Image.new("RGB", (200, 120), "white").save(source_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{"id": "page-1", "problemIds": ["p1"]}],
                "problems": [{
                    "id": "p1",
                    "sourcePageId": "page-1",
                    "imagePath": source_path.resolve().as_uri(),
                    "boardRenderPath": source_path.resolve().as_uri(),
                    "bbox": {"left": 10, "top": 20, "width": 100, "height": 80},
                }],
            }

            cropped_session = app_server._mutate_crop(
                session,
                "p1",
                {"leftRatio": 0.1, "rightRatio": 0.2, "topRatio": 0.25, "bottomRatio": 0.05},
            )

            problem = cropped_session["problems"][0]
            self.assertEqual({"left": 10.0, "top": 20.0, "width": 100.0, "height": 80.0}, problem["cropBaseBbox"])
            self.assertEqual({"leftRatio": 0.1, "rightRatio": 0.2, "topRatio": 0.25, "bottomRatio": 0.05}, problem["manualCrop"])
            self.assertEqual(20.0, problem["bbox"]["left"])
            self.assertEqual(40.0, problem["bbox"]["top"])
            self.assertEqual(70.0, problem["bbox"]["width"])
            self.assertEqual(56.0, problem["bbox"]["height"])
            crop_path = app_server._resolve_session_path(problem["imagePath"])
            self.assertIsNotNone(crop_path)
            self.assertTrue(crop_path.exists())
            with Image.open(crop_path) as crop_image:
                self.assertEqual((140, 84), crop_image.size)

            reset_session = app_server._mutate_crop(cropped_session, "p1", {"leftRatio": 0})
            reset_problem = reset_session["problems"][0]
            self.assertEqual({"left": 10.0, "top": 20.0, "width": 100.0, "height": 80.0}, reset_problem["bbox"])
            self.assertEqual(source_path.resolve().as_uri(), reset_problem["imagePath"])
            self.assertEqual(
                {"leftRatio": 0.0, "rightRatio": 0.0, "topRatio": 0.0, "bottomRatio": 0.0},
                reset_problem["manualCrop"],
            )

    def test_manual_crop_can_expand_from_source_page(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            problem_path = tmpdir / "problem.png"
            Image.new("RGB", (300, 200), "white").save(page_path)
            Image.new("RGB", (100, 80), "white").save(problem_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["p1"],
                }],
                "problems": [{
                    "id": "p1",
                    "sourcePageId": "page-1",
                    "imagePath": problem_path.resolve().as_uri(),
                    "boardRenderPath": problem_path.resolve().as_uri(),
                    "bbox": {"left": 50, "top": 40, "width": 100, "height": 80},
                }],
            }

            expanded_session = app_server._mutate_crop(
                session,
                "p1",
                {"leftRatio": -0.1, "rightRatio": -0.2, "topRatio": -0.25, "bottomRatio": -0.05},
            )

            problem = expanded_session["problems"][0]
            self.assertEqual(40.0, problem["bbox"]["left"])
            self.assertEqual(20.0, problem["bbox"]["top"])
            self.assertEqual(130.0, problem["bbox"]["width"])
            self.assertEqual(104.0, problem["bbox"]["height"])
            self.assertEqual(-0.1, problem["manualCrop"]["leftRatio"])
            self.assertEqual(-0.2, problem["manualCrop"]["rightRatio"])
            crop_path = app_server._resolve_session_path(problem["imagePath"])
            self.assertIsNotNone(crop_path)
            with Image.open(crop_path) as crop_image:
                self.assertEqual((130, 104), crop_image.size)

    def test_manual_crop_accepts_absolute_crop_box(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            problem_path = tmpdir / "problem.png"
            Image.new("RGB", (300, 200), "white").save(page_path)
            Image.new("RGB", (100, 80), "white").save(problem_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["p1"],
                }],
                "problems": [{
                    "id": "p1",
                    "sourcePageId": "page-1",
                    "imagePath": problem_path.resolve().as_uri(),
                    "boardRenderPath": problem_path.resolve().as_uri(),
                    "bbox": {"left": 50, "top": 40, "width": 100, "height": 80},
                }],
            }

            cropped_session = app_server._mutate_crop(
                session,
                "p1",
                {"cropBox": {"left": 35, "top": 30, "width": 150, "height": 120}},
            )

            problem = cropped_session["problems"][0]
            self.assertEqual({"left": 35.0, "top": 30.0, "width": 150.0, "height": 120.0}, problem["bbox"])
            self.assertAlmostEqual(-0.15, problem["manualCrop"]["leftRatio"])
            self.assertAlmostEqual(-0.35, problem["manualCrop"]["rightRatio"])
            self.assertAlmostEqual(-0.125, problem["manualCrop"]["topRatio"])
            self.assertAlmostEqual(-0.375, problem["manualCrop"]["bottomRatio"])
            crop_path = app_server._resolve_session_path(problem["imagePath"])
            self.assertIsNotNone(crop_path)
            with Image.open(crop_path) as crop_image:
                self.assertEqual((150, 120), crop_image.size)

    def test_absolute_crop_preserves_problem_identity_number_order_and_neighbor(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            first_path = tmpdir / "problem-1.png"
            second_path = tmpdir / "problem-2.png"
            Image.new("RGB", (400, 300), "white").save(page_path)
            Image.new("RGB", (120, 90), "white").save(first_path)
            Image.new("RGB", (100, 70), "white").save(second_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["p1", "p2"],
                }],
                "problems": [
                    {
                        "id": "p1",
                        "problemNumber": 12,
                        "sourcePageId": "page-1",
                        "imagePath": first_path.resolve().as_uri(),
                        "boardRenderPath": first_path.resolve().as_uri(),
                        "bbox": {"left": 40, "top": 30, "width": 120, "height": 90},
                        "placementXRatio": 0.25,
                        "placementYRatio": 0.1,
                        "placementScaleRatio": 1.15,
                    },
                    {
                        "id": "p2",
                        "problemNumber": 13,
                        "sourcePageId": "page-1",
                        "imagePath": second_path.resolve().as_uri(),
                        "boardRenderPath": second_path.resolve().as_uri(),
                        "bbox": {"left": 200, "top": 160, "width": 100, "height": 70},
                    },
                ],
            }
            neighbor_before = copy.deepcopy(session["problems"][1])

            result = app_server._mutate_crop(
                session,
                "p1",
                {"cropBox": {"left": 25, "top": 20, "width": 160, "height": 110}},
            )

            self.assertEqual(["p1", "p2"], [problem["id"] for problem in result["problems"]])
            self.assertEqual(["p1", "p2"], result["pages"][0]["problemIds"])
            edited = result["problems"][0]
            self.assertEqual(12, edited["problemNumber"])
            self.assertEqual(0.25, edited["placementXRatio"])
            self.assertEqual(0.1, edited["placementYRatio"])
            self.assertEqual(1.15, edited["placementScaleRatio"])
            self.assertEqual(neighbor_before, result["problems"][1])

    def test_manual_crop_materializes_missing_problem_image_from_source_page(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            Image.new("RGB", (300, 200), "white").save(page_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problem_ids": ["p1"],
                }],
                "problems": [{
                    "id": "p1",
                    "source_page_id": "page-1",
                    "bbox": {"left": 50, "top": 40, "width": 100, "height": 80},
                }],
            }

            cropped_session = app_server._mutate_crop(
                session,
                "p1",
                {"cropBox": {"left": 45, "top": 35, "width": 110, "height": 90}},
            )

            problem = cropped_session["problems"][0]
            self.assertEqual({"left": 50.0, "top": 40.0, "width": 100.0, "height": 80.0}, problem["cropBaseBbox"])
            base_path = app_server._resolve_session_path(problem["cropBaseImagePath"])
            crop_path = app_server._resolve_session_path(problem["imagePath"])
            self.assertIsNotNone(base_path)
            self.assertIsNotNone(crop_path)
            self.assertTrue(base_path.exists())
            self.assertTrue(crop_path.exists())
            with Image.open(base_path) as base_image:
                self.assertEqual((100, 80), base_image.size)
            with Image.open(crop_path) as crop_image:
                self.assertEqual((110, 90), crop_image.size)
            self.assertEqual({"left": 45.0, "top": 35.0, "width": 110.0, "height": 90.0}, problem["bbox"])

    def test_manual_crop_refreshes_board_render_for_processed_steps(self):
        from PIL import Image, ImageDraw

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            problem_path = tmpdir / "problem.png"
            page = Image.new("RGB", (300, 200), "white")
            draw = ImageDraw.Draw(page)
            draw.rectangle((70, 55, 175, 125), outline="black", width=4)
            page.save(page_path)
            page.crop((50, 40, 150, 120)).save(problem_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["p1"],
                }],
                "problems": [{
                    "id": "p1",
                    "sourcePageId": "page-1",
                    "imagePath": problem_path.resolve().as_uri(),
                    "boardRenderPath": problem_path.resolve().as_uri(),
                    "bbox": {"left": 50, "top": 40, "width": 100, "height": 80},
                    "step": "s2",
                }],
            }

            cropped_session = app_server._mutate_crop(
                session,
                "p1",
                {"cropBox": {"left": 45, "top": 35, "width": 145, "height": 110}},
            )

            problem = cropped_session["problems"][0]
            crop_path = app_server._resolve_session_path(problem["imagePath"])
            board_path = app_server._resolve_session_path(problem["boardRenderPath"])
            self.assertIsNotNone(crop_path)
            self.assertIsNotNone(board_path)
            self.assertTrue(crop_path.exists())
            self.assertTrue(board_path.exists())
            self.assertNotEqual(crop_path, board_path)
            self.assertEqual("s2", problem["step"])
            with Image.open(board_path) as board_image:
                self.assertIn("A", board_image.getbands())

    def test_stitch_crop_combines_ordered_regions_across_pages_into_one_passage(self):
        from PIL import Image, ImageDraw

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            first_page_path = tmpdir / "page-1.png"
            second_page_path = tmpdir / "page-2.png"
            original_path = tmpdir / "passage.png"
            first_page = Image.new("RGB", (180, 140), "white")
            second_page = Image.new("RGB", (180, 140), "white")
            ImageDraw.Draw(first_page).rectangle((20, 30, 120, 100), fill="black")
            ImageDraw.Draw(second_page).rectangle((40, 15, 150, 85), fill="black")
            first_page.save(first_page_path)
            second_page.save(second_page_path)
            first_page.crop((20, 30, 121, 101)).save(original_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [
                    {
                        "id": "page-1",
                        "sourceImageUri": first_page_path.resolve().as_uri(),
                        "problemIds": ["p0", "passage", "p1"],
                    },
                    {
                        "id": "page-2",
                        "sourceImageUri": second_page_path.resolve().as_uri(),
                        "problemIds": ["p2"],
                    },
                    {"id": "page-3", "problemIds": ["passage", "p3"]},
                ],
                "problems": [{
                    "id": "passage",
                    "title": "지문 1~3",
                    "problemNumber": 1,
                    "sourcePageId": "page-1",
                    "imagePath": original_path.resolve().as_uri(),
                    "boardRenderPath": original_path.resolve().as_uri(),
                    "bbox": {"left": 20, "top": 30, "width": 101, "height": 71},
                    "passageRole": "passage_fragment",
                    "passageGroupId": "passage-1-3",
                    "riskFlags": ["passage_cross_page_merge_check"],
                }],
            }

            updated = app_server._mutate_stitch_crop(
                session,
                "passage",
                [
                    {
                        "pageId": "page-2",
                        "order": 1,
                        "bbox": {"left": 40, "top": 15, "width": 111, "height": 71},
                    },
                    {
                        "pageId": "page-1",
                        "order": 2,
                        "bbox": {"left": 20, "top": 30, "width": 101, "height": 71},
                    },
                ],
            )

            problem = updated["problems"][0]
            self.assertEqual("passage", problem["id"])
            self.assertEqual(1, problem["problemNumber"])
            self.assertEqual("page-2", problem["sourcePageId"])
            self.assertEqual(
                ["page-2", "page-1"],
                [segment["sourcePageId"] for segment in problem["sourceSegments"]],
            )
            self.assertEqual([1, 2], [segment["fragmentIndex"] for segment in problem["sourceSegments"]])
            self.assertTrue(problem["passageFragmentsMerged"])
            self.assertTrue(problem["manualStitch"])
            self.assertEqual(1, problem["manualStitchDiagnostics"]["join_count"])
            self.assertEqual(1, problem["manualStitchDiagnostics"]["trimmedSourceFragmentCount"])
            self.assertEqual([], problem["riskFlags"])
            self.assertEqual("normal", problem["reviewStatus"])
            self.assertEqual(["p0", "passage", "p1"], updated["pages"][0]["problemIds"])
            self.assertEqual(["p2", "passage"], updated["pages"][1]["problemIds"])
            self.assertEqual(["p3"], updated["pages"][2]["problemIds"])
            stitched_path = app_server._resolve_session_path(problem["imagePath"])
            self.assertIsNotNone(stitched_path)
            self.assertTrue(stitched_path.exists())
            with Image.open(stitched_path) as stitched:
                self.assertGreater(stitched.height, 71)

    def test_stitch_crop_rejects_ambiguous_or_duplicate_regions(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            original_path = tmpdir / "passage.png"
            Image.new("RGB", (180, 140), "white").save(page_path)
            Image.new("RGB", (80, 60), "white").save(original_path)

            def make_session():
                return {
                    "output_dir": str(tmpdir / "out"),
                    "pages": [{
                        "id": "page-1",
                        "sourceImageUri": page_path.resolve().as_uri(),
                        "problemIds": ["passage"],
                    }],
                    "problems": [{
                        "id": "passage",
                        "sourcePageId": "page-1",
                        "imagePath": original_path.resolve().as_uri(),
                        "boardRenderPath": original_path.resolve().as_uri(),
                        "bbox": {"left": 20, "top": 20, "width": 80, "height": 60},
                        "passageRole": "passage_fragment",
                    }],
                }

            with self.assertRaisesRegex(ValueError, "unique order"):
                app_server._mutate_stitch_crop(make_session(), "passage", [
                    {"pageId": "page-1", "order": 1, "bbox": {"left": 10, "top": 10, "width": 70, "height": 50}},
                    {"pageId": "page-1", "order": 1, "bbox": {"left": 90, "top": 10, "width": 70, "height": 50}},
                ])

            with self.assertRaisesRegex(ValueError, "duplicate regions"):
                app_server._mutate_stitch_crop(make_session(), "passage", [
                    {"pageId": "page-1", "order": 1, "bbox": {"left": 10, "top": 10, "width": 70, "height": 50}},
                    {"pageId": "page-1", "order": 2, "bbox": {"left": 10, "top": 10, "width": 70, "height": 50}},
                ])

            with self.assertRaisesRegex(ValueError, "too small"):
                app_server._mutate_stitch_crop(make_session(), "passage", [
                    {"pageId": "page-1", "order": 1, "bbox": {"left": 10, "top": 10, "width": 7, "height": 50}},
                ])

    def test_stitch_crop_clamps_manual_regions_away_from_center_divider(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            original_path = tmpdir / "passage.png"
            Image.new("RGB", (180, 140), "white").save(page_path)
            Image.new("RGB", (80, 60), "white").save(original_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["passage"],
                }],
                "problems": [{
                    "id": "passage",
                    "sourcePageId": "page-1",
                    "imagePath": original_path.resolve().as_uri(),
                    "boardRenderPath": original_path.resolve().as_uri(),
                    "bbox": {"left": 20, "top": 20, "width": 80, "height": 60},
                    "passageRole": "passage_fragment",
                }],
            }
            original_lazy_call = app_server._lazy_call

            def fake_lazy_call(module_name, attr_name, *args, **kwargs):
                if module_name == "segment" and attr_name == "detect_pdf_visual_column_divider_x":
                    return 90.0
                return original_lazy_call(module_name, attr_name, *args, **kwargs)

            with patch.object(app_server, "_lazy_call", side_effect=fake_lazy_call):
                updated = app_server._mutate_stitch_crop(session, "passage", [
                    {
                        "pageId": "page-1",
                        "order": 1,
                        "columnIndex": 1,
                        "bbox": {"left": 20, "top": 10, "width": 85, "height": 70},
                    },
                    {
                        "pageId": "page-1",
                        "order": 2,
                        "columnIndex": 2,
                        "bbox": {"left": 75, "top": 80, "width": 85, "height": 50},
                    },
                ])

            segments = updated["problems"][0]["sourceSegments"]
            self.assertEqual([1, 2], [segment["columnIndex"] for segment in segments])
            self.assertEqual(84.0, segments[0]["bbox"]["left"] + segments[0]["bbox"]["width"])
            self.assertEqual(96.0, segments[1]["bbox"]["left"])

    def test_stitch_crop_corrects_legacy_left_hint_for_right_column_region(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            original_path = tmpdir / "passage.png"
            Image.new("RGB", (180, 160), "white").save(page_path)
            Image.new("RGB", (60, 60), "white").save(original_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["passage"],
                }],
                "problems": [{
                    "id": "passage",
                    "sourcePageId": "page-1",
                    "imagePath": original_path.resolve().as_uri(),
                    "boardRenderPath": original_path.resolve().as_uri(),
                    "bbox": {"left": 100, "top": 20, "width": 60, "height": 60},
                    "passageRole": "passage_fragment",
                }],
            }
            original_lazy_call = app_server._lazy_call

            def fake_lazy_call(module_name, attr_name, *args, **kwargs):
                if module_name == "segment" and attr_name == "detect_pdf_visual_column_divider_x":
                    return 90.0
                return original_lazy_call(module_name, attr_name, *args, **kwargs)

            with patch.object(app_server, "_lazy_call", side_effect=fake_lazy_call):
                updated = app_server._mutate_stitch_crop(session, "passage", [{
                    "pageId": "page-1",
                    "order": 1,
                    "columnIndex": 1,
                    "bbox": {"left": 100, "top": 20, "width": 60, "height": 60},
                }])

            segment = updated["problems"][0]["sourceSegments"][0]
            self.assertEqual(2, segment["columnIndex"])
            self.assertGreaterEqual(segment["bbox"]["left"], 96.0)

    def test_stitch_crop_persists_effective_bounds_after_join_cleanup(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            original_path = tmpdir / "passage.png"
            Image.new("RGB", (180, 220), "white").save(page_path)
            Image.new("RGB", (60, 80), "white").save(original_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["passage"],
                }],
                "problems": [{
                    "id": "passage",
                    "sourcePageId": "page-1",
                    "imagePath": original_path.resolve().as_uri(),
                    "boardRenderPath": original_path.resolve().as_uri(),
                    "bbox": {"left": 20, "top": 20, "width": 60, "height": 80},
                    "passageRole": "passage_fragment",
                }],
            }

            def fake_stitch(_paths, output_path, **kwargs):
                kwargs["source_crop_boxes_output"].extend([
                    (0, 0, 60, 50),
                    (0, 12, 60, 60),
                ])
                kwargs["stitch_diagnostics_output"].update({
                    "join_count": 1,
                    "max_join_blank_band_px": 4,
                })
                Image.new("RGB", (60, 114), "white").save(output_path)
                return 60, 114

            with patch.object(app_server, "_stitch_passage_image_files", side_effect=fake_stitch):
                updated = app_server._mutate_stitch_crop(session, "passage", [
                    {
                        "pageId": "page-1",
                        "order": 1,
                        "bbox": {"left": 20, "top": 20, "width": 60, "height": 80},
                    },
                    {
                        "pageId": "page-1",
                        "order": 2,
                        "bbox": {"left": 100, "top": 100, "width": 60, "height": 80},
                    },
                ])

            problem = updated["problems"][0]
            first, second = problem["sourceSegments"]
            self.assertEqual({"left": 20, "top": 20, "width": 60, "height": 50}, first["bbox"])
            self.assertEqual({"left": 100, "top": 112, "width": 60, "height": 48}, second["bbox"])
            self.assertEqual(2, problem["manualStitchDiagnostics"]["trimmedSourceFragmentCount"])
            self.assertEqual(4, problem["manualStitchDiagnostics"]["max_join_blank_band_px"])

    def test_http_session_recovers_legacy_passage_source_segments_from_pages_json(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            pages_json_path = tmpdir / "pages.json"
            pages_json_path.write_text(json.dumps([
                {
                    "page_id": "page-1",
                    "blocks": [
                        {
                            "block_id": "right-fragment",
                            "reading_order": 2,
                            "bbox": {"left": 100, "top": 10, "width": 80, "height": 90},
                            "metadata": {
                                "segmenter": "pdf-passage-range",
                                "passage_range": {"start": 4, "end": 9},
                                "passage_fragment_index": 2,
                                "column_index": 2,
                            },
                        },
                        {
                            "block_id": "left-fragment",
                            "reading_order": 1,
                            "bbox": {"left": 0, "top": 20, "width": 80, "height": 100},
                            "metadata": {
                                "segmenter": "pdf-passage-range",
                                "passage_range": {"start": 4, "end": 9},
                                "passage_fragment_index": 1,
                                "column_index": 1,
                            },
                        },
                    ],
                },
                {
                    "page_id": "page-2",
                    "blocks": [{
                        "block_id": "child-question-only",
                        "bbox": {"left": 0, "top": 10, "width": 80, "height": 90},
                        "metadata": {"problem_number": 5},
                    }],
                },
                {
                    "page_id": "page-3",
                    "blocks": [{
                        "block_id": "next-page-continuation",
                        "reading_order": 1,
                        "bbox": {"left": 0, "top": 5, "width": 80, "height": 45},
                        "metadata": {
                            "segmenter": "pdf-pre-question-passage-continuation",
                            "passage_range": {"start": 4, "end": 9},
                        },
                    }],
                },
            ], ensure_ascii=False), encoding="utf-8")
            session = {
                "pages_json_path": str(pages_json_path),
                "pages": [
                    {"id": "page-1", "problemIds": ["passage"]},
                    {"id": "page-2", "problemIds": []},
                    {"id": "page-3", "problemIds": []},
                ],
                "problems": [{
                    "id": "passage",
                    "sourcePageId": "page-1",
                    "passageRole": "passage_fragment",
                    "passageRange": {"start": 4, "end": 9},
                    "passageSourcePageIds": ["page-1", "page-2", "page-3"],
                    "bbox": {"left": 0, "top": 10, "width": 180, "height": 110},
                }],
            }

            rewritten = app_server.rewrite_session_for_http(session)

            passage = rewritten["problems"][0]
            self.assertTrue(passage["sourceSegmentsRecovered"])
            self.assertEqual(
                ["left-fragment", "right-fragment", "next-page-continuation"],
                [segment["sourceBlockId"] for segment in passage["sourceSegments"]],
            )
            self.assertEqual(["page-1", "page-1", "page-3"], [segment["sourcePageId"] for segment in passage["sourceSegments"]])
            self.assertEqual([0.0, 100.0, 0.0], [segment["bbox"]["left"] for segment in passage["sourceSegments"]])
            self.assertEqual(["passage"], rewritten["pages"][0]["problemIds"])
            self.assertEqual([], rewritten["pages"][1]["problemIds"])
            self.assertEqual(["passage"], rewritten["pages"][2]["problemIds"])

    def test_bulk_crop_replaces_source_problem_with_multiple_png_entries(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            page_path = tmpdir / "page.png"
            Image.new("RGB", (300, 200), "white").save(page_path)
            session = {
                "output_dir": str(tmpdir / "out"),
                "pages": [{
                    "id": "page-1",
                    "sourceImageUri": page_path.resolve().as_uri(),
                    "problemIds": ["p0", "p1", "p2"],
                }],
                "problems": [
                    {
                        "id": "p0",
                        "sourcePageId": "page-1",
                        "bbox": {"left": 0, "top": 0, "width": 10, "height": 10},
                    },
                    {
                        "id": "p1",
                        "title": "원본",
                        "sourcePageId": "page-1",
                        "sourceFileName": "page.png",
                        "sourceImagePath": page_path.resolve().as_uri(),
                        "bbox": {"left": 0, "top": 0, "width": 300, "height": 200},
                        "actualHeightPages": 1.9,
                        "actual_height_pages": 1.9,
                        "startYPages": 3.6,
                        "start_y_pages": 3.6,
                        "snappedNextStartYPages": 6.0,
                        "snapped_next_start_y_pages": 6.0,
                        "slotSpanCount": 2,
                        "slot_span_count": 2,
                        "riskFlags": ["large_block_dominance"],
                        "recordMode": "image-only",
                    },
                    {
                        "id": "p2",
                        "sourcePageId": "page-1",
                        "bbox": {"left": 10, "top": 10, "width": 20, "height": 20},
                    },
                ],
            }

            updated = app_server._mutate_bulk_crop(
                session,
                "page-1",
                [
                    {"bbox": {"left": 10, "top": 20, "width": 50, "height": 40}, "title": "직접 1"},
                    {"bbox": {"left": 250, "top": 180, "width": 100, "height": 50}},
                ],
                ["p1"],
            )

            problem_ids = [problem["id"] for problem in updated["problems"]]
            self.assertEqual("p0", problem_ids[0])
            self.assertEqual("p2", problem_ids[-1])
            created = updated["problems"][1:3]
            created_ids = [problem["id"] for problem in created]
            self.assertEqual(created_ids, updated["pages"][0]["problemIds"][1:3])
            self.assertEqual(["p0", *created_ids, "p2"], updated["pages"][0]["problemIds"])
            self.assertEqual("직접 1", created[0]["title"])
            self.assertEqual("문항 02", created[1]["title"])
            self.assertEqual("p1", created[0]["replacesProblemId"])
            self.assertEqual("p1", created[0]["replaces_problem_id"])
            self.assertEqual("p1", created[1]["replacesProblemId"])
            self.assertEqual("p1", created[1]["replaces_problem_id"])
            self.assertEqual("image-only", created[0]["recordMode"])
            self.assertEqual(1, created[0]["imageRecordCount"])
            self.assertEqual([], created[0]["riskFlags"])
            self.assertEqual("normal", created[0]["reviewStatus"])
            self.assertEqual(page_path.resolve().as_uri(), created[0]["sourceImagePath"])
            self.assertEqual({"left": 250.0, "top": 180.0, "width": 50.0, "height": 20.0}, created[1]["bbox"])
            self.assertAlmostEqual(
                app_server.estimate_height_pages((50, 40), app_server.LayoutTemplate(name="academy-default")),
                created[0]["actualHeightPages"],
            )
            self.assertAlmostEqual(created[0]["actualHeightPages"], created[0]["actual_height_pages"])
            self.assertNotIn("startYPages", created[0])
            self.assertNotIn("snappedNextStartYPages", created[0])
            self.assertNotIn("slotSpanCount", created[0])

            first_crop = app_server._resolve_session_path(created[0]["imagePath"])
            second_crop = app_server._resolve_session_path(created[1]["imagePath"])
            self.assertIsNotNone(first_crop)
            self.assertIsNotNone(second_crop)
            self.assertTrue(first_crop.exists())
            self.assertTrue(second_crop.exists())
            with Image.open(first_crop) as first_image:
                self.assertEqual((50, 40), first_image.size)
            with Image.open(second_crop) as second_image:
                self.assertEqual((50, 20), second_image.size)

    def test_session_image_export_zip_uses_order_fallback_and_safe_names(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            raw_1 = tmpdir / "raw-1.png"
            board_1 = tmpdir / "board-1.png"
            raw_2 = tmpdir / "raw-2.png"
            Image.new("RGB", (20, 10), "white").save(raw_1)
            Image.new("RGB", (24, 12), "black").save(board_1)
            Image.new("RGB", (18, 8), "blue").save(raw_2)
            session = {
                "session_name": "국어 수업",
                "problems": [
                    {
                        "id": "p1",
                        "title": "문항 01/위험:*",
                        "sourcePageId": "page-1",
                        "bbox": {"left": 1, "top": 2, "width": 3, "height": 4},
                        "imagePath": raw_1.resolve().as_uri(),
                        "boardRenderPath": board_1.resolve().as_uri(),
                    },
                    {
                        "id": "p2",
                        "title": "문항 02",
                        "sourcePageId": "page-1",
                        "bbox": {"left": 5, "top": 6, "width": 7, "height": 8},
                        "imagePath": raw_2.resolve().as_uri(),
                        "boardRenderPath": (tmpdir / "missing-board.png").resolve().as_uri(),
                    },
                ],
            }

            with patch.object(app_server, "RUNTIME_DIR", tmpdir / "runtime"):
                result = app_server._write_session_image_export_zip(session, "both", problem_ids=["p2", "p1"])

            self.assertEqual(2, result["count"])
            self.assertEqual([], result["missing"])
            zip_path = Path(result["zipPath"])
            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())
                self.assertIn("edb_images/001_문항_02.png", names)
                self.assertIn("raw_crops/001_문항_02.png", names)
                self.assertIn("edb_images/002_문항_01_위험.png", names)
                self.assertIn("raw_crops/002_문항_01_위험.png", names)
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

            self.assertEqual("국어 수업", manifest["sessionName"])
            self.assertEqual("both", manifest["mode"])
            self.assertEqual(2, manifest["count"])
            self.assertEqual(["p2", "p1"], [item["problemId"] for item in manifest["items"]])
            self.assertEqual("edb_images/001_문항_02.png", manifest["items"][0]["edbImage"])
            self.assertEqual("raw_crops/001_문항_02.png", manifest["items"][0]["rawCrop"])

    def test_atomic_zip_failure_preserves_existing_destination_and_removes_staging_file(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            destination = tmpdir / "existing.zip"
            original_payload = b"previous-complete-zip"
            destination.write_bytes(original_payload)

            def fail_after_first_member(archive):
                archive.writestr("partial.txt", b"partial")
                raise OSError("disk full")

            with self.assertRaises(OSError):
                app_server._write_zip_atomically(
                    destination,
                    compression=zipfile.ZIP_DEFLATED,
                    populate=fail_after_first_member,
                )

            self.assertEqual(original_payload, destination.read_bytes())
            self.assertEqual([], list(tmpdir.glob(".*.tmp")))

    def test_atomic_zip_fsync_uses_windows_writable_handle_contract(self):
        with TemporaryDirectory() as raw_tmp:
            destination = Path(raw_tmp) / "bundle.zip"
            opened_modes = []
            real_path_open = Path.open

            def tracking_open(path, mode="r", *args, **kwargs):
                if path.name.startswith(".atomic-zip."):
                    opened_modes.append(mode)
                return real_path_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", new=tracking_open):
                app_server._write_zip_atomically(
                    destination,
                    compression=zipfile.ZIP_STORED,
                    populate=lambda archive: archive.writestr("ok.txt", b"ok"),
                )

            self.assertIn("r+b", opened_modes)
            self.assertNotIn("rb", opened_modes)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(b"ok", archive.read("ok.txt"))

    def test_edb_zip_arcname_collision_resolution_rechecks_until_unique(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            first_dir = tmpdir / "first"
            second_dir = tmpdir / "second"
            third_dir = tmpdir / "third"
            first_dir.mkdir()
            second_dir.mkdir()
            third_dir.mkdir()
            first = first_dir / "Lesson.edb"
            second = second_dir / "Lesson_part03.edb"
            third = third_dir / "Lesson.edb"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            third.write_bytes(b"third")

            with patch.object(app_server, "RUNTIME_DIR", tmpdir / "runtime"):
                result = app_server._write_edb_export_zip([first, second, third])

            with zipfile.ZipFile(result["zipPath"]) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), len(set(name.casefold() for name in names)))
                self.assertEqual(
                    ["Lesson.edb", "Lesson_part03.edb", "Lesson_part04.edb"],
                    names,
                )
                self.assertEqual(b"first", archive.read("Lesson.edb"))
                self.assertEqual(b"second", archive.read("Lesson_part03.edb"))
                self.assertEqual(b"third", archive.read("Lesson_part04.edb"))

    def test_export_zip_names_reserve_utf8_bytes_for_generated_suffixes(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            crop = tmpdir / "crop.png"
            Image.new("RGB", (8, 6), "white").save(crop)
            first_edb = tmpdir / "first.edb"
            second_edb = tmpdir / "second.edb"
            first_edb.write_bytes(b"first")
            second_edb.write_bytes(b"second")

            with patch.object(app_server, "RUNTIME_DIR", tmpdir / "runtime"):
                for session_name in ("a" * 400, "긴세션명" * 100):
                    with self.subTest(session_name=session_name[:12]):
                        image_result = app_server._write_session_image_export_zip(
                            {
                                "session_name": session_name,
                                "problems": [{
                                    "id": "p1",
                                    "title": "1",
                                    "imagePath": crop.resolve().as_uri(),
                                    "boardRenderPath": crop.resolve().as_uri(),
                                }],
                            },
                            "raw",
                        )
                        self.assertLessEqual(len(Path(image_result["zipPath"]).name.encode("utf-8")), 255)
                        with zipfile.ZipFile(image_result["zipPath"]) as archive:
                            self.assertIsNone(archive.testzip())

                edb_result = app_server._write_edb_export_zip(
                    [first_edb, second_edb],
                    bundle_name=f"{'한글' * 200}.edb",
                )
                self.assertLessEqual(len(Path(edb_result["zipPath"]).name.encode("utf-8")), 255)
                with zipfile.ZipFile(edb_result["zipPath"]) as archive:
                    self.assertIsNone(archive.testzip())

    def test_session_image_export_zip_renders_s3_edb_images(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            crop = tmpdir / "crop.png"
            Image.new("RGB", (20, 10), "white").save(crop)
            session = {
                "session_name": "수업",
                "board_theme": "charcoal",
                "problems": [{
                    "id": "p1",
                    "title": "문항 1",
                    "sourcePageId": "page-1",
                    "bbox": {"left": 0, "top": 0, "width": 20, "height": 10},
                    "imagePath": crop.resolve().as_uri(),
                    "boardRenderPath": crop.resolve().as_uri(),
                    "step": "s3",
                    "processingStep": "s3",
                }],
            }

            rendered = Image.new("RGBA", (4, 3), (0, 255, 0, 180))
            with (
                patch.object(app_server, "RUNTIME_DIR", tmpdir / "runtime"),
                patch.object(app_server, "_build_transparent_reconstruction_image", return_value=rendered),
            ):
                result = app_server._write_session_image_export_zip(session, "both", problem_ids=["p1"])

            with zipfile.ZipFile(Path(result["zipPath"])) as archive:
                edb_payload = archive.read("edb_images/001_문항_1.png")
                raw_payload = archive.read("raw_crops/001_문항_1.png")
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

            with Image.open(io.BytesIO(edb_payload)) as edb_image:
                self.assertEqual((4, 3), edb_image.size)
                self.assertEqual((0, 255, 0, 180), edb_image.convert("RGBA").getpixel((0, 0)))
            with Image.open(io.BytesIO(raw_payload)) as raw_image:
                self.assertEqual((20, 10), raw_image.size)
            self.assertEqual("s3", manifest["items"][0]["processingStep"])

    def test_session_image_export_zip_renders_s2_edb_images(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            crop = tmpdir / "crop.png"
            Image.new("RGB", (20, 10), "white").save(crop)
            session = {
                "session_name": "수업",
                "board_theme": "charcoal",
                "problems": [{
                    "id": "p1",
                    "title": "문항 1",
                    "sourcePageId": "page-1",
                    "bbox": {"left": 0, "top": 0, "width": 20, "height": 10},
                    "imagePath": crop.resolve().as_uri(),
                    "boardRenderPath": crop.resolve().as_uri(),
                    "step": "s2",
                    "processingStep": "s2",
                }],
            }

            rendered = Image.new("RGBA", (5, 4), (0, 0, 255, 170))
            with (
                patch.object(app_server, "RUNTIME_DIR", tmpdir / "runtime"),
                patch.object(app_server, "_load_board_export_image", return_value=rendered),
            ):
                result = app_server._write_session_image_export_zip(session, "both", problem_ids=["p1"])

            with zipfile.ZipFile(Path(result["zipPath"])) as archive:
                edb_payload = archive.read("edb_images/001_문항_1.png")
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

            with Image.open(io.BytesIO(edb_payload)) as edb_image:
                self.assertEqual((5, 4), edb_image.size)
                self.assertEqual((0, 0, 255, 170), edb_image.convert("RGBA").getpixel((0, 0)))
            self.assertEqual("s2", manifest["items"][0]["processingStep"])

    def test_session_export_images_handler_allows_generated_zip_file(self):
        from PIL import Image

        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            crop = tmpdir / "crop.png"
            Image.new("RGB", (12, 8), "white").save(crop)
            session = {
                "session_name": "수업",
                "problems": [{
                    "id": "p1",
                    "title": "문항 1",
                    "sourcePageId": "page-1",
                    "bbox": {"left": 0, "top": 0, "width": 12, "height": 8},
                    "imagePath": crop.resolve().as_uri(),
                    "boardRenderPath": crop.resolve().as_uri(),
                }],
            }
            fake_server = type("FakeServer", (), {
                "latest_session": session,
                "allowed_files": set(),
            })()
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = fake_server
            handler._read_json_body = lambda: {"mode": "edb"}
            responses = []
            handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

            with patch.object(app_server, "RUNTIME_DIR", tmpdir / "runtime"):
                handler._handle_session_export_images()

            body = responses[0][0]
            self.assertTrue(body["ok"])
            self.assertTrue(body["downloadUrl"].startswith("/api/file?path="))
            self.assertIn(body["zipPath"], fake_server.allowed_files)
            self.assertTrue(Path(body["zipPath"]).exists())

    def test_session_export_edb_handler_bundles_multiple_allowed_files(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            first = tmpdir / "수업_part01.edb"
            second = tmpdir / "수업_part02.edb"
            first.write_bytes(b"first-edb")
            second.write_bytes(b"second-edb")
            fake_server = type("FakeServer", (), {
                "allowed_files": {str(first.resolve()), str(second.resolve())},
            })()
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = fake_server
            handler._read_json_body = lambda: {
                "edbPaths": [str(first), str(second)],
                "fileName": "수업.edb",
            }
            responses = []
            handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

            with patch.object(app_server, "RUNTIME_DIR", tmpdir / "runtime"):
                handler._handle_session_export_edb()

            body = responses[0][0]
            self.assertTrue(body["ok"])
            self.assertTrue(body["bundled"])
            self.assertEqual(2, body["count"])
            self.assertTrue(body["downloadUrl"].startswith("/api/file?path="))
            self.assertIn(body["zipPath"], fake_server.allowed_files)
            with zipfile.ZipFile(body["zipPath"]) as archive:
                self.assertEqual(b"first-edb", archive.read(first.name))
                self.assertEqual(b"second-edb", archive.read(second.name))

    def test_session_export_edb_handler_rejects_unregistered_file(self):
        with TemporaryDirectory() as raw_tmp:
            edb_path = Path(raw_tmp) / "blocked.edb"
            edb_path.write_bytes(b"blocked")
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {"allowed_files": set()})()
            handler._read_json_body = lambda: {"edbPaths": [str(edb_path)]}
            responses = []
            handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

            handler._handle_session_export_edb()

            self.assertFalse(responses[0][0]["ok"])
            self.assertEqual(app_server.HTTPStatus.FORBIDDEN, responses[0][1]["status"])


class TestExportErrorPayload(unittest.TestCase):
    def test_publish_failure_codes_distinguish_deterministic_page_limit(self):
        page_limit = app_server._publish_stage_failure_payload(
            "write",
            ValueError("EDB part page hint 51 exceeds ClassIn limit 50"),
        )
        transient = app_server._publish_stage_failure_payload("write", OSError("disk busy"))
        self.assertEqual("publish_page_limit_exceeded", page_limit["code"])
        self.assertFalse(page_limit["retryable"])
        self.assertEqual("edb_write_failed", transient["code"])
        self.assertTrue(transient["retryable"])

    def test_hangul_conversion_error_includes_recovery_steps(self):
        payload = app_server._export_error_payload(
            ValueError(
                "HWP/HWPX conversion failed. Details: source file could not be loaded "
                "Diagnosis: Input is a valid HWPX ZIP document. "
                "한컴오피스에서 PDF로 내보낸 뒤 다시 업로드하거나 HWPX 지원 변환기를 설치해 주세요."
            )
        )

        self.assertFalse(payload["ok"])
        self.assertEqual("hangul_conversion_failed", payload["errorKind"])
        self.assertIn("한컴오피스", payload["error"])
        self.assertGreaterEqual(len(payload["recoverySteps"]), 2)
        self.assertTrue(any("PDF" in step for step in payload["recoverySteps"]))

    def test_generic_export_error_stays_simple(self):
        payload = app_server._export_error_payload(ValueError("plain failure"))

        self.assertFalse(payload["ok"])
        self.assertEqual("export_failed", payload["errorKind"])
        self.assertEqual("plain failure", payload["error"])
        self.assertNotIn("recoverySteps", payload)


class TestExportSourceResolution(unittest.TestCase):
    def test_export_content_target_accepts_only_supported_values(self):
        self.assertEqual("shared-passages", app_server._extract_content_target({"contentTarget": "shared_passages"}))
        self.assertEqual("questions", app_server._extract_content_target({"content_target": "questions"}))
        self.assertEqual("all", app_server._extract_content_target({"contentTarget": "tables"}))

    def test_files_accepts_source_path_strings_for_automation_clients(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "sample.hwp"
            source.write_bytes(b"fake")
            handler = object.__new__(app_server.AppRequestHandler)

            resolved = handler._resolve_source_paths({"files": [str(source)]})

            self.assertEqual(resolved, [source.resolve()])

    def test_session_reextract_sources_prefer_input_files_and_deduplicate(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "source.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            page = tmpdir / "page.png"
            page.write_bytes(b"png")

            resolved = app_server._session_reextract_source_paths({
                "input_files": [source.resolve().as_uri(), str(source.resolve())],
                "pages": [{"sourceImagePath": page.resolve().as_uri()}],
            })

            self.assertEqual([source.resolve()], resolved)

    def test_session_reextract_sources_fall_back_to_source_pages_in_order(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            missing = tmpdir / "missing.pdf"
            first = tmpdir / "page-1.png"
            second = tmpdir / "page-2.png"
            first.write_bytes(b"one")
            second.write_bytes(b"two")

            resolved = app_server._session_reextract_source_paths({
                "input_files": [missing.resolve().as_uri()],
                "pages": [
                    {"sourceImagePath": first.resolve().as_uri()},
                    {"sourceImageUri": first.resolve().as_uri()},
                    {"sourceImageUri": second.resolve().as_uri()},
                ],
            })

            self.assertEqual([first.resolve(), second.resolve()], resolved)

    def test_session_source_reextract_preview_does_not_replace_current_session(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "source.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            current_session = {
                "session_name": "existing",
                "input_files": [source.resolve().as_uri()],
                "pages": [],
                "problems": [{"id": "question-1"}],
            }
            payload = {
                "reuseSessionSources": True,
                "preview": True,
                "inputIntent": "multi-problem",
                "contentTarget": "shared-passages",
                "exportEdb": False,
            }
            captured: dict[str, object] = {}
            responses = []

            class FakeServer:
                latest_session = copy.deepcopy(current_session)
                allowed_files = set()

                def remember_session(self, _session):
                    raise AssertionError("preview re-extraction must not replace the current session")

            def fake_run_problem_export(run_source, **kwargs):
                captured["source"] = Path(run_source)
                captured.update(kwargs)
                output_dir = Path(kwargs["output_dir"])
                return {
                    "ok": True,
                    "ui_session": {
                        "pages": [],
                        "problems": [{"id": "passage-1", "passageRole": "passage_fragment"}],
                    },
                    "output_dir": str(output_dir),
                    "ui_session_path": str(output_dir / "ui_session.json"),
                    "edb_path": None,
                    "summary": {"placements": []},
                }

            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = FakeServer()
            handler._read_json_body = lambda: dict(payload)
            handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

            with patch.object(app_server, "run_problem_export", side_effect=fake_run_problem_export):
                handler._handle_export()

            self.assertEqual(source.resolve(), captured["source"])
            self.assertEqual("shared-passages", captured["content_target"])
            self.assertEqual(current_session, handler.server.latest_session)
            self.assertTrue(responses[0][0]["ok"])
            self.assertTrue(responses[0][0]["preview"])

    def test_session_source_reextract_rejects_non_preview_export(self):
        current_session = {"input_files": [], "pages": [], "problems": [{"id": "question-1"}]}
        responses = []

        class FakeServer:
            latest_session = copy.deepcopy(current_session)
            allowed_files = set()

        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = FakeServer()
        handler._read_json_body = lambda: {
            "reuseSessionSources": True,
            "preview": False,
            "contentTarget": "shared-passages",
        }
        handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

        with patch.object(
            app_server,
            "run_problem_export",
            side_effect=AssertionError("non-preview session re-extraction must not run"),
        ):
            handler._handle_export()

        self.assertFalse(responses[0][0]["ok"])
        self.assertIn("only allowed for preview", responses[0][0]["error"])
        self.assertEqual(current_session, handler.server.latest_session)

    def test_session_source_reextract_missing_sources_keeps_current_session(self):
        current_session = {
            "session_name": "existing",
            "input_files": ["file:///definitely/missing/source.pdf"],
            "pages": [{"id": "page-1", "sourceImagePath": "file:///definitely/missing/page.png"}],
            "problems": [{"id": "question-1"}],
        }
        responses = []

        class FakeServer:
            latest_session = copy.deepcopy(current_session)
            allowed_files = set()

            def remember_session(self, _session):
                raise AssertionError("failed preview re-extraction must not replace the current session")

        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = FakeServer()
        handler._read_json_body = lambda: {
            "reuseSessionSources": True,
            "preview": True,
            "contentTarget": "shared-passages",
        }
        handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

        with patch.object(
            app_server,
            "run_problem_export",
            side_effect=AssertionError("re-extraction must not run without a preserved source"),
        ):
            handler._handle_export()

        self.assertFalse(responses[0][0]["ok"])
        self.assertIn("no preserved source files", responses[0][0]["error"])
        self.assertEqual(current_session, handler.server.latest_session)

    def test_uploaded_files_reuse_same_content_path_for_cache_stability(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            upload_dir = tmpdir / "uploads"
            upload_dir.mkdir()
            handler = object.__new__(app_server.AppRequestHandler)
            payload = {
                "fileName": "평가원 양식.hwp",
                "fileDataBase64": base64.b64encode(b"same hwp bytes").decode("ascii"),
            }

            with patch.object(app_server, "UPLOAD_DIR", upload_dir):
                first = handler._save_uploaded_file(payload)
                second = handler._save_uploaded_file(payload)

            self.assertEqual(first, second)
            self.assertTrue(first.exists())
            self.assertEqual(b"same hwp bytes", first.read_bytes())
            self.assertIn("평가원 양식", first.name)
            self.assertEqual(1, len(list(upload_dir.glob("*.hwp"))))

    def test_uploaded_files_reuse_digest_path_without_rereading_existing_file(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            upload_dir = tmpdir / "uploads"
            upload_dir.mkdir()
            handler = object.__new__(app_server.AppRequestHandler)
            payload = {
                "fileName": "same.hwp",
                "fileDataBase64": base64.b64encode(b"same hwp bytes").decode("ascii"),
            }

            with patch.object(app_server, "UPLOAD_DIR", upload_dir):
                first = handler._save_uploaded_file(payload)
                with patch.object(Path, "read_bytes", side_effect=AssertionError("should not reread cache hit")):
                    second = handler._save_uploaded_file(payload)

            self.assertEqual(first, second)

    def test_uploaded_file_replaces_corrupt_digest_named_cache_entry_atomically(self):
        with TemporaryDirectory() as raw_tmp:
            upload_dir = Path(raw_tmp) / "uploads"
            upload_dir.mkdir()
            handler = object.__new__(app_server.AppRequestHandler)
            file_bytes = b"expected upload bytes"
            digest = hashlib.sha256(file_bytes).hexdigest()
            safe_name = app_server.sanitize_upload_file_name("same.hwp")
            cached = upload_dir / f"{digest}_{safe_name}"
            cached.write_bytes(b"corrupt upload bytes!")
            payload = {
                "fileName": "same.hwp",
                "fileDataBase64": base64.b64encode(file_bytes).decode("ascii"),
            }

            with patch.object(app_server, "UPLOAD_DIR", upload_dir):
                result = handler._save_uploaded_file(payload)

            self.assertEqual(cached, result)
            self.assertEqual(file_bytes, cached.read_bytes())
            self.assertEqual([], list(upload_dir.glob(".*.tmp")))

    def test_uploaded_files_use_sha256_identity_and_do_not_alias_equal_size_payloads(self):
        with TemporaryDirectory() as raw_tmp:
            upload_dir = Path(raw_tmp) / "uploads"
            upload_dir.mkdir()
            handler = object.__new__(app_server.AppRequestHandler)
            first_bytes = b"PDF payload A"
            second_bytes = b"PDF payload B"
            self.assertEqual(len(first_bytes), len(second_bytes))

            with patch.object(app_server, "UPLOAD_DIR", upload_dir):
                first = handler._save_uploaded_file({
                    "fileName": "same.pdf",
                    "fileDataBase64": base64.b64encode(first_bytes).decode("ascii"),
                })
                second = handler._save_uploaded_file({
                    "fileName": "same.pdf",
                    "fileDataBase64": base64.b64encode(second_bytes).decode("ascii"),
                })

            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith(hashlib.sha256(first_bytes).hexdigest()))
            self.assertTrue(second.name.startswith(hashlib.sha256(second_bytes).hexdigest()))
            self.assertEqual(first_bytes, first.read_bytes())
            self.assertEqual(second_bytes, second.read_bytes())

    def test_uploaded_file_migrates_to_sha256_without_deleting_legacy_sha1_cache(self):
        with TemporaryDirectory() as raw_tmp:
            upload_dir = Path(raw_tmp) / "uploads"
            upload_dir.mkdir()
            handler = object.__new__(app_server.AppRequestHandler)
            file_bytes = b"legacy cached pdf"
            safe_name = app_server.sanitize_upload_file_name("legacy.pdf")
            legacy_path = upload_dir / f"{hashlib.sha1(file_bytes).hexdigest()}_{safe_name}"
            legacy_path.write_bytes(file_bytes)

            with patch.object(app_server, "UPLOAD_DIR", upload_dir):
                migrated = handler._save_uploaded_file({
                    "fileName": "legacy.pdf",
                    "fileDataBase64": base64.b64encode(file_bytes).decode("ascii"),
                })

            self.assertNotEqual(legacy_path, migrated)
            self.assertTrue(migrated.name.startswith(hashlib.sha256(file_bytes).hexdigest()))
            self.assertEqual(file_bytes, migrated.read_bytes())
            self.assertEqual(file_bytes, legacy_path.read_bytes())

    def test_uploaded_file_rejects_malformed_base64(self):
        with TemporaryDirectory() as raw_tmp:
            upload_dir = Path(raw_tmp) / "uploads"
            upload_dir.mkdir()
            handler = object.__new__(app_server.AppRequestHandler)

            with patch.object(app_server, "UPLOAD_DIR", upload_dir):
                with self.assertRaises(ValueError) as ctx:
                    handler._save_uploaded_file({
                        "fileName": "broken.hwp",
                        "fileDataBase64": "not valid base64!!!",
                    })

            self.assertIn("valid base64", str(ctx.exception))

    def test_uploaded_files_reuse_same_content_path_when_filename_changes(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            upload_dir = tmpdir / "uploads"
            upload_dir.mkdir()
            handler = object.__new__(app_server.AppRequestHandler)
            file_data = base64.b64encode(b"same hwp bytes").decode("ascii")

            with patch.object(app_server, "UPLOAD_DIR", upload_dir):
                first = handler._save_uploaded_file(
                    {"fileName": "download-a.hwp", "fileDataBase64": file_data}
                )
                second = handler._save_uploaded_file(
                    {"fileName": "renamed-by-user.hwp", "fileDataBase64": file_data}
                )

            self.assertEqual(first, second)
            self.assertEqual(1, len(list(upload_dir.glob("*.hwp"))))

    def test_export_uses_stable_upload_path_across_repeated_base64_uploads(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            upload_dir = tmpdir / "uploads"
            upload_dir.mkdir()
            output_dir = tmpdir / "out"
            source_paths: list[Path] = []
            payload = {
                "fileName": "same.hwp",
                "fileDataBase64": base64.b64encode(b"same hwp bytes").decode("ascii"),
                "outputDir": str(output_dir),
                "preview": True,
            }

            class FakeServer:
                allowed_files = set()

                def remember_session(self, session):
                    self.latest_session = session

            def fake_run_problem_export(source, **kwargs):
                source_paths.append(Path(source))
                resolved_output = Path(kwargs.get("output_dir") or output_dir)
                return {
                    "ok": True,
                    "ui_session": {"pages": [], "problems": []},
                    "output_dir": str(resolved_output),
                    "ui_session_path": str(resolved_output / "ui_session.json"),
                    "edb_path": None,
                    "summary": {"placements": []},
                }

            responses = []
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = FakeServer()
            handler._read_json_body = lambda: dict(payload)
            handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

            with (
                patch.object(app_server, "UPLOAD_DIR", upload_dir),
                patch.object(app_server, "run_problem_export", side_effect=fake_run_problem_export),
            ):
                handler._handle_export()
                handler._handle_export()

            self.assertEqual(2, len(source_paths))
            self.assertEqual(source_paths[0], source_paths[1])
            self.assertTrue(source_paths[0].exists())
            self.assertEqual(1, len(list(upload_dir.glob("*.hwp"))))
            self.assertEqual(2, len(responses))
            self.assertTrue(all(response[0]["ok"] for response in responses))

    def test_page_as_is_export_forces_source_preserving_preprocessing(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "source.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            output_dir = tmpdir / "out"
            payload = {
                "files": [str(source)],
                "outputDir": str(output_dir),
                "inputIntent": "page-as-is",
                "contentTarget": "shared-passages",
                "preview": True,
                "exportEdb": False,
                "detectPerspective": True,
                "skipCrop": False,
                "skipDeskew": False,
                "maxDimension": 1200,
                "pdfDpi": 144,
            }
            captured_kwargs = {}

            class FakeServer:
                allowed_files = set()

                def remember_session(self, session):
                    self.latest_session = session

            def fake_run_problem_export(_source, **kwargs):
                captured_kwargs.update(kwargs)
                resolved_output = Path(kwargs.get("output_dir") or output_dir)
                return {
                    "ok": True,
                    "ui_session": {"pages": [], "problems": []},
                    "output_dir": str(resolved_output),
                    "ui_session_path": str(resolved_output / "ui_session.json"),
                    "edb_path": None,
                    "summary": {"placements": []},
                }

            responses = []
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = FakeServer()
            handler._read_json_body = lambda: dict(payload)
            handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

            with patch.object(app_server, "run_problem_export", side_effect=fake_run_problem_export):
                handler._handle_export()

            self.assertEqual("page-as-is", captured_kwargs["input_intent"])
            self.assertEqual("shared-passages", captured_kwargs["content_target"])
            self.assertFalse(captured_kwargs["detect_perspective"])
            self.assertTrue(captured_kwargs["skip_crop"])
            self.assertTrue(captured_kwargs["skip_deskew"])
            self.assertEqual(200, captured_kwargs["pdf_dpi"])
            self.assertIsNone(captured_kwargs["max_dimension"])
            self.assertEqual("off", captured_kwargs["page_tile_mode"])
            self.assertTrue(responses[0][0]["ok"])

    def test_recognition_export_defaults_to_source_first_safety_cap(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "source.png"
            source.write_bytes(b"png")
            captured_kwargs = {}

            class FakeServer:
                allowed_files = set()

                def remember_session(self, session):
                    self.latest_session = session

            def fake_run_problem_export(_source, **kwargs):
                captured_kwargs.update(kwargs)
                output_dir = Path(kwargs["output_dir"])
                return {
                    "ok": True,
                    "ui_session": {"pages": [], "problems": []},
                    "output_dir": str(output_dir),
                    "ui_session_path": str(output_dir / "ui_session.json"),
                    "edb_path": None,
                    "summary": {"placements": []},
                }

            responses = []
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = FakeServer()
            handler._read_json_body = lambda: {
                "files": [str(source)],
                "outputDir": str(tmpdir / "out"),
                "inputIntent": "multi-problem",
                "preview": True,
                "exportEdb": False,
            }
            handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

            with patch.object(app_server, "run_problem_export", side_effect=fake_run_problem_export):
                handler._handle_export()

            self.assertEqual(
                app_server.DEFAULT_RECOGNITION_MAX_DIMENSION,
                captured_kwargs["max_dimension"],
            )
            self.assertTrue(responses[0][0]["ok"])

    def test_export_passes_sanitized_requested_edb_name(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "sample.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmpdir / "out"
            captured_kwargs: dict[str, object] = {}
            payload = {
                "files": [str(source)],
                "outputDir": str(output_dir),
                "edbName": "../Renamed Lesson?.edb",
                "preview": True,
                "exportEdb": False,
            }

            class FakeServer:
                allowed_files = set()

                def remember_session(self, session):
                    self.latest_session = session

            def fake_run_problem_export(_source, **kwargs):
                captured_kwargs.update(kwargs)
                resolved_output = Path(kwargs.get("output_dir") or output_dir)
                return {
                    "ok": True,
                    "ui_session": {"pages": [], "problems": []},
                    "output_dir": str(resolved_output),
                    "ui_session_path": str(resolved_output / "ui_session.json"),
                    "edb_path": None,
                    "summary": {"placements": []},
                }

            responses = []
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = FakeServer()
            handler._read_json_body = lambda: dict(payload)
            handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

            with patch.object(app_server, "run_problem_export", side_effect=fake_run_problem_export):
                handler._handle_export()

            self.assertEqual("Renamed_Lesson.edb", captured_kwargs["edb_name"])
            self.assertEqual(1, len(responses))
            self.assertTrue(responses[0][0]["ok"])

    def test_export_response_exposes_classin_preflight_from_session(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "sample.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmpdir / "out"
            preflight = {
                "passed": False,
                "status": "needs_attention",
                "issueCount": 1,
                "issues": [{"type": "board_placement_overlap", "problemId": "p1"}],
            }
            payload = {
                "files": [str(source)],
                "outputDir": str(output_dir),
                "preview": True,
                "exportEdb": False,
            }

            class FakeServer:
                allowed_files = set()

                def remember_session(self, session):
                    self.latest_session = session

            def fake_run_problem_export(_source, **kwargs):
                resolved_output = Path(kwargs.get("output_dir") or output_dir)
                return {
                    "ok": True,
                    "ui_session": {
                        "pages": [],
                        "problems": [],
                        "classinPreflight": preflight,
                        "classin_preflight": preflight,
                    },
                    "output_dir": str(resolved_output),
                    "ui_session_path": str(resolved_output / "ui_session.json"),
                    "edb_path": None,
                    "summary": {"placements": []},
                }

            responses = []
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = FakeServer()
            handler._read_json_body = lambda: dict(payload)
            handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

            with patch.object(app_server, "run_problem_export", side_effect=fake_run_problem_export):
                handler._handle_export()

            body = responses[0][0]
            self.assertEqual(preflight, body["classinPreflight"])
            self.assertEqual(preflight, body["classin_preflight"])
            self.assertEqual("needs_attention", body["classinPreflightStatus"])
            self.assertEqual(1, body["classinPreflightIssueCount"])
            self.assertFalse(body["classinPreflightPassed"])

    def test_export_response_synthesizes_single_edb_part_metadata(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            source = tmpdir / "sample.hwp"
            source.write_bytes(b"hwp")
            output_dir = tmpdir / "out"
            payload = {
                "files": [str(source)],
                "outputDir": str(output_dir),
                "preview": True,
            }

            class FakeServer:
                allowed_files = set()

                def remember_session(self, session):
                    self.latest_session = session

            def fake_run_problem_export(_source, **kwargs):
                resolved_output = Path(kwargs.get("output_dir") or output_dir)
                resolved_output.mkdir(parents=True, exist_ok=True)
                edb_path = resolved_output / "lesson.edb"
                edb_path.write_bytes(b"edb")
                return {
                    "ok": True,
                    "ui_session": {"pages": [], "problems": [{"id": "p1"}]},
                    "output_dir": str(resolved_output),
                    "ui_session_path": str(resolved_output / "ui_session.json"),
                    "edb_path": edb_path,
                    "summary": {"placements": [{"problem_id": "p1"}]},
                }

            responses = []
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = FakeServer()
            handler._read_json_body = lambda: dict(payload)
            handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

            with (
                patch.object(app_server, "run_problem_export", side_effect=fake_run_problem_export),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": 1,
                    "recordCountActual": 1,
                }),
            ):
                handler._handle_export()

            body = responses[0][0]
            self.assertTrue(body["ok"], body)
            self.assertFalse(body["edbSplit"])
            self.assertEqual(1, body["edbPartCount"])
            self.assertEqual("lesson.edb", body["edbParts"][0]["edbFileName"])
            self.assertFalse(body["session"]["edbSplit"])
            self.assertEqual(1, body["session"]["edbPartCount"])

    def test_validate_edb_parts_rejects_page_hint_over_classin_limit(self):
        with TemporaryDirectory() as raw_tmp:
            edb_path = Path(raw_tmp) / "too-long.edb"
            edb_path.write_bytes(b"edb")

            with patch.object(app_server, "validate_edb_file", return_value={
                "outerSize": 10,
                "innerSize": 8,
                "pageCountHint": 51,
                "recordCountHint": 1,
                "recordCountActual": 1,
            }):
                with self.assertRaises(ValueError) as ctx:
                    app_server._validate_edb_parts([
                        {"edbPath": str(edb_path), "edbFileName": edb_path.name, "recordCount": 1}
                    ])

            self.assertIn("exceeds ClassIn limit 50", str(ctx.exception))


class TestSessionPublishPreflightGuard(unittest.TestCase):
    def _publish(self, session: dict, payload: dict | None = None):
        class FakeServer:
            def __init__(self, latest_session):
                self.latest_session = latest_session
                self.remembered_session = None

            def remember_session(self, new_session):
                self.latest_session = new_session
                self.remembered_session = new_session

        responses = []
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = FakeServer(session)
        handler._read_json_body = lambda: dict(payload or {})
        handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))
        return handler, responses

    def test_session_publish_rejects_raw_duplicate_ids_before_writer(self):
        with TemporaryDirectory() as raw_tmp:
            session = {
                "output_dir": raw_tmp,
                "pages": [],
                "problems": [
                    {"id": "dup", "title": "1", "riskFlags": [], "bbox": {}},
                    {"id": "dup", "title": "2", "riskFlags": [], "bbox": {}},
                ],
            }
            handler, responses = self._publish(session)
            with patch.object(app_server, "write_classin_limited_edb_files") as writer:
                handler._handle_session_publish()
            writer.assert_not_called()
            body, kwargs = responses[0]
            self.assertEqual("publish_preflight_blocked", body["code"])
            self.assertFalse(body["retryable"])
            self.assertEqual("duplicate_problem_id", body["classinPreflight"]["issues"][0]["type"])
            self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs["status"])

    def test_session_publish_rejects_raw_missing_id_before_writer(self):
        session = {
            "pages": [],
            "problems": [{"title": "1", "riskFlags": [], "bbox": {}}],
        }
        handler, responses = self._publish(session)
        with patch.object(app_server, "write_classin_limited_edb_files") as writer:
            handler._handle_session_publish()
        writer.assert_not_called()
        body, _kwargs = responses[0]
        self.assertEqual("publish_preflight_blocked", body["code"])
        self.assertEqual("missing_problem_id", body["classinPreflight"]["issues"][0]["type"])

    def test_session_publish_rejects_malformed_problem_entry_before_writer(self):
        session = {
            "pages": [],
            "problems": [
                {"id": "p1", "title": "1", "riskFlags": [], "bbox": {}},
                None,
            ],
        }
        handler, responses = self._publish(session)
        with patch.object(app_server, "write_classin_limited_edb_files") as writer:
            handler._handle_session_publish()
        writer.assert_not_called()
        body, _kwargs = responses[0]
        issue = body["classinPreflight"]["issues"][0]
        self.assertEqual("publish_preflight_blocked", body["code"])
        self.assertEqual("missing_problem_id", issue["type"])
        self.assertTrue(issue["malformedEntry"])
        self.assertEqual(1, issue["entryIndex"])

    def test_local_classin_split_writer_caps_part_page_hint_after_render_expands_template(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            entries = [SimpleNamespace(problem_id=f"p{i}") for i in range(29)]
            template = app_server.LayoutTemplate(name="academy-default", board_page_count=58)
            captured: dict[str, object] = {}

            def fake_build_records(received_entries, render_template, **kwargs):
                captured.setdefault("render_page_counts", []).append(render_template.board_page_count)
                captured["reserve_image_layout_height"] = kwargs.get("reserve_image_layout_height")
                captured["expand_board_capacity"] = kwargs.get("expand_board_capacity")
                render_template.board_page_count = 58
                return (
                    [b"record"] * len(received_entries),
                    [
                        {
                            "problem_id": entry.problem_id,
                            "record_bottom_y_pages": 49.0,
                            "actual_bottom_y_pages": 49.0,
                            "snapped_next_start_y_pages": 49.0,
                        }
                        for entry in received_entries
                    ],
                    4,
                )

            def fake_build_edb(records, **kwargs):
                captured.setdefault("page_count_hints", []).append(kwargs.get("page_count_hint"))
                return b"edb"

            with (
                patch.object(app_server, "split_problem_entries_for_classin_page_limit", return_value=[entries]),
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "build_edb", side_effect=fake_build_edb),
                patch.object(app_server, "write_edb", side_effect=lambda path, data: Path(path).write_bytes(data)),
            ):
                parts = app_server.write_classin_limited_edb_files(
                    entries,
                    template,
                    root,
                    "math_current_code_check_20260708.edb",
                    record_mode="image-only",
                    text_confidence_threshold=0.78,
                    dark_board=True,
                    board_theme=app_server.DEFAULT_BOARD_THEME,
                    crop_format=app_server.CROP_FORMAT_V1,
                )

            self.assertEqual([50], captured["render_page_counts"])
            self.assertTrue(captured["reserve_image_layout_height"])
            self.assertFalse(captured["expand_board_capacity"])
            self.assertEqual([50], captured["page_count_hints"])
            self.assertEqual(50, parts[0]["pageCountHint"])
            self.assertEqual(50, parts[0]["page_count_hint"])

    def test_local_classin_split_writer_rejects_mismatched_record_page_scale(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            entries = [SimpleNamespace(problem_id="p0")]
            template = app_server.LayoutTemplate(name="academy-default", board_page_count=63)

            with (
                patch.object(app_server, "split_problem_entries_for_classin_page_limit", return_value=[entries]),
                patch.object(
                    app_server,
                    "build_records",
                    return_value=(
                        [b"record"],
                        [{
                            "problem_id": "p0",
                            "record_bottom_y_pages": 2.0,
                            "record_page_count_hint": 63,
                        }],
                        4,
                    ),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "different page scale"):
                    app_server.write_classin_limited_edb_files(
                        entries,
                        template,
                        root,
                        "bad-scale.edb",
                        record_mode="image-only",
                        text_confidence_threshold=0.78,
                        dark_board=True,
                        board_theme=app_server.DEFAULT_BOARD_THEME,
                        crop_format=app_server.CROP_FORMAT_V1,
                    )

    def test_session_publish_uses_requested_name_and_splits_over_fifty_pages(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = {
                "session_name": "fallback lesson",
                "output_dir": str(root),
                "input_files": [],
                "pages": [],
                "problems": [
                    {
                        "id": f"p{i}",
                        "title": f"{i + 1}.",
                        "bbox": {},
                        "riskFlags": [],
                    }
                    for i in range(26)
                ],
            }
            handler, responses = self._publish(session, {"edbName": "../Renamed Lesson?.edb"})
            captured: dict[str, object] = {}
            entries = [object() for _ in range(26)]

            def fake_build_records(received_entries, template, **_kwargs):
                captured["entry_count"] = len(received_entries)
                captured["board_page_count"] = template.board_page_count
                return ([b"record"] * len(received_entries), [], 3)

            def fake_write_classin_limited_edb_files(received_entries, template, output_dir, edb_name, **_kwargs):
                captured["split_entry_count"] = len(received_entries)
                captured["split_board_page_count"] = template.board_page_count
                captured["split_edb_name"] = edb_name
                paths = [
                    Path(output_dir) / "Renamed_Lesson_part01.edb",
                    Path(output_dir) / "Renamed_Lesson_part02.edb",
                ]
                for path in paths:
                    path.write_bytes(b"edb")
                return [
                    {
                        "partIndex": 1,
                        "partCount": 2,
                        "edbPath": str(paths[0]),
                        "edbFileName": paths[0].name,
                        "recordCount": 13,
                        "pageCountHint": 50,
                    },
                    {
                        "partIndex": 2,
                        "partCount": 2,
                        "edbPath": str(paths[1]),
                        "edbFileName": paths[1].name,
                        "recordCount": 13,
                        "pageCountHint": 50,
                    },
                ]

            def fake_validate(path, *, expected_min_records=1):
                captured.setdefault("validated_paths", []).append(Path(path).name)
                captured.setdefault("expected_min_records", []).append(expected_min_records)
                return {
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": expected_min_records,
                    "recordCountActual": expected_min_records,
                }

            def fake_build_ui_session(**_kwargs):
                return {
                    "session_name": "published",
                    "output_dir": str(root),
                    "problems": [
                        {"id": f"p{i}", "title": f"{i + 1}.", "riskFlags": [], "bbox": {}}
                        for i in range(26)
                    ],
                    "pages": [],
                }

            def fake_write_handoff(output_dir, **_kwargs):
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps({
                        "status": "ready_for_classin_review",
                        "readyForClassIn": True,
                        "classinPreflight": {
                            "status": "passed",
                            "passed": True,
                            "issueCount": 0,
                            "issues": [],
                        }
                    }),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "_problems_to_entries", return_value=entries),
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "write_classin_limited_edb_files", side_effect=fake_write_classin_limited_edb_files),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "validate_edb_file", side_effect=fake_validate),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            self.assertEqual(1, len(responses))
            body, _kwargs = responses[0]
            self.assertTrue(body["ok"], body)
            self.assertEqual(26, captured["entry_count"])
            self.assertEqual(52, captured["board_page_count"])
            self.assertEqual(26, captured["split_entry_count"])
            self.assertEqual(52, captured["split_board_page_count"])
            self.assertEqual("Renamed_Lesson.edb", captured["split_edb_name"])
            self.assertEqual(["Renamed_Lesson_part01.edb", "Renamed_Lesson_part02.edb"], captured["validated_paths"])
            self.assertEqual([13, 13], captured["expected_min_records"])
            self.assertEqual("Renamed_Lesson_part01.edb", body["publishSummary"]["edbFileName"])
            self.assertTrue(body["publishSummary"]["edbFileExists"])
            self.assertTrue(body["publishSummary"]["outputDirExists"])
            self.assertTrue(body["publishSummary"]["readyForClassIn"])
            self.assertTrue(body["publishSummary"]["canDownload"])
            published_path = app_server.decode_file_reference(
                body["publishSummary"]["edbFileUri"]
            )
            self.assertIsNotNone(published_path)
            self.assertIn("published", published_path.parts)
            self.assertNotIn(".publish-staging", published_path.parts)
            self.assertEqual(50, body["publishSummary"]["pageCountHint"])
            self.assertTrue(body["publishSummary"]["edbSplit"])
            self.assertEqual(2, body["publishSummary"]["edbPartCount"])
            self.assertEqual(["Renamed_Lesson_part01.edb", "Renamed_Lesson_part02.edb"], [
                part["edbFileName"] for part in body["publishSummary"]["edbParts"]
            ])
            self.assertEqual(
                "Renamed_Lesson_part01.edb",
                handler.server.remembered_session["publishSummary"]["edbFileName"],
            )

    def test_session_publish_blocks_source_bbox_overlap_before_build(self):
        with TemporaryDirectory() as raw_tmp:
            session = {
                "session_name": "source-overlap",
                "output_dir": raw_tmp,
                "pages": [{"id": "page-1", "problemIds": ["p21", "p22"]}],
                "problems": [
                    {
                        "id": "p21",
                        "title": "21.",
                        "problemNumber": 21,
                        "sourcePageId": "page-1",
                        "bbox": {"left": 40, "top": 100, "width": 520, "height": 320},
                    },
                    {
                        "id": "p22",
                        "title": "22.",
                        "problemNumber": 22,
                        "sourcePageId": "page-1",
                        "bbox": {"left": 60, "top": 125, "width": 500, "height": 300},
                    },
                ],
            }
            handler, responses = self._publish(session)

            with patch.object(app_server, "_problems_to_entries", side_effect=AssertionError("build should be blocked")) as entries:
                handler._handle_session_publish()

            self.assertEqual(1, len(responses))
            body, kwargs = responses[0]
            self.assertFalse(body["ok"])
            self.assertEqual("publish_preflight_blocked", body["errorKind"])
            self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs.get("status"))
            issue_types = {issue["type"] for issue in body["classinPreflight"]["issues"]}
            self.assertIn("source_problem_bbox_overlap", issue_types)
            self.assertEqual(["p21", "p22"], body["blockingProblemIds"])
            self.assertEqual(["p21", "p22"], body["blocking_problem_ids"])
            entries.assert_not_called()

    def test_session_publish_blocks_passage_group_source_reuse_before_build(self):
        with TemporaryDirectory() as raw_tmp:
            session = {
                "session_name": "passage-source-reuse",
                "output_dir": raw_tmp,
                "pages": [{"id": "page-4", "problemIds": ["p22", "p23"]}],
                "problems": [
                    {
                        "id": "p22",
                        "title": "22.",
                        "problemNumber": 22,
                        "sourcePageId": "page-4",
                        "bbox": {"left": 42, "top": 120, "width": 520, "height": 430},
                        "passageGroupId": "hwp-continuation-passage-22-26",
                        "passageRole": "child_question",
                    },
                    {
                        "id": "p23",
                        "title": "23.",
                        "problemNumber": 23,
                        "sourcePageId": "page-4",
                        "bbox": {"left": 48, "top": 132, "width": 510, "height": 410},
                        "passageGroupId": "hwp-continuation-passage-22-26",
                        "passageRole": "child_question",
                    },
                ],
            }
            handler, responses = self._publish(session)

            with patch.object(app_server, "_problems_to_entries", side_effect=AssertionError("build should be blocked")) as entries:
                handler._handle_session_publish()

            self.assertEqual(1, len(responses))
            body, kwargs = responses[0]
            self.assertFalse(body["ok"])
            self.assertEqual("publish_preflight_blocked", body["errorKind"])
            self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs.get("status"))
            issue_types = {issue["type"] for issue in body["classinPreflight"]["issues"]}
            self.assertIn("passage_group_source_reuse", issue_types)
            self.assertEqual(["p22", "p23"], body["blockingProblemIds"])
            self.assertEqual(["p22", "p23"], body["blocking_problem_ids"])
            entries.assert_not_called()

    def test_session_publish_allows_duplicate_problem_numbers_in_page_order(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = {
                "session_name": "duplicate-number",
                "output_dir": raw_tmp,
                "pages": [
                    {"id": "page-1", "problemIds": ["p7-a"]},
                    {"id": "page-2", "problemIds": ["p7-b"]},
                ],
                "problems": [
                    {
                        "id": "p7-a",
                        "title": "7.",
                        "problemNumber": 7,
                        "sourcePageId": "page-1",
                        "bbox": {"left": 10, "top": 10, "width": 120, "height": 100},
                    },
                    {
                        "id": "p7-b",
                        "title": "7.",
                        "problemNumber": 7,
                        "sourcePageId": "page-2",
                        "bbox": {"left": 10, "top": 140, "width": 120, "height": 100},
                    },
                ],
            }
            handler, responses = self._publish(session)
            captured: dict[str, object] = {}
            entry_objects = [object(), object()]

            def fake_problems_to_entries(problems, **_kwargs):
                captured["problem_order"] = [problem["id"] for problem in problems]
                return entry_objects

            def fake_build_records(received_entries, _template, **_kwargs):
                captured["entry_count"] = len(received_entries)
                return (
                    [{"record": "p7-a"}, {"record": "p7-b"}],
                    [
                        {
                            "problem_id": "p7-a",
                            "title": "7.",
                            "problem_number": 7,
                            "subject": "math",
                            "source_page_id": "page-1",
                            "crop_path": str(root / "p7-a.png"),
                            "board_render_path": str(root / "p7-a.png"),
                            "source_path": "page-1",
                            "bbox": {"left": 10, "top": 10, "width": 120, "height": 100},
                            "actual_content_height_pages": 0.8,
                            "overflow_allowed": False,
                            "start_y_pages": 0.0,
                            "snapped_next_start_y_pages": 1.2,
                            "overflow_amount_pages": 0.0,
                            "overflow_violation": False,
                            "slot_span_count": 1,
                            "placement_x_ratio": 0.0,
                            "placement_y_ratio": 0.0,
                            "placement_scale_ratio": 1.0,
                            "risk_flags": [],
                        },
                        {
                            "problem_id": "p7-b",
                            "title": "7.",
                            "problem_number": 7,
                            "subject": "math",
                            "source_page_id": "page-2",
                            "crop_path": str(root / "p7-b.png"),
                            "board_render_path": str(root / "p7-b.png"),
                            "source_path": "page-2",
                            "bbox": {"left": 10, "top": 140, "width": 120, "height": 100},
                            "actual_content_height_pages": 0.8,
                            "overflow_allowed": False,
                            "start_y_pages": 1.2,
                            "snapped_next_start_y_pages": 2.4,
                            "overflow_amount_pages": 0.0,
                            "overflow_violation": False,
                            "slot_span_count": 1,
                            "placement_x_ratio": 0.0,
                            "placement_y_ratio": 0.0,
                            "placement_scale_ratio": 1.0,
                            "risk_flags": [],
                        },
                    ],
                    0,
                )

            def fake_write_classin_limited_edb_files(_entries, _template, output_dir, edb_name, **_kwargs):
                path = Path(output_dir) / edb_name
                path.write_bytes(b"edb")
                return [
                    {
                        "partIndex": 1,
                        "partCount": 1,
                        "edbPath": str(path),
                        "edbFileName": path.name,
                        "recordCount": 2,
                        "pageCountHint": 4,
                    }
                ]

            def fake_build_ui_session(**_kwargs):
                return {
                    "session_name": "published",
                    "output_dir": str(root),
                    "problems": [
                        {"id": "p7-a", "title": "7.", "problemNumber": 7, "riskFlags": [], "bbox": {}},
                        {"id": "p7-b", "title": "7.", "problemNumber": 7, "riskFlags": [], "bbox": {}},
                    ],
                    "pages": [],
                }

            def fake_write_handoff(output_dir, **_kwargs):
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps({
                        "status": "ready_for_classin_review",
                        "readyForClassIn": True,
                        "classinPreflight": {"status": "passed", "passed": True, "issueCount": 0, "issues": []},
                    }),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "_problems_to_entries", side_effect=fake_problems_to_entries) as entries,
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "write_classin_limited_edb_files", side_effect=fake_write_classin_limited_edb_files),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 4,
                    "recordCountHint": 2,
                    "recordCountActual": 2,
                }),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            body, kwargs = responses[0]
            self.assertTrue(body["ok"], body)
            self.assertNotEqual(app_server.HTTPStatus.CONFLICT, kwargs.get("status"))
            self.assertEqual(["p7-a", "p7-b"], captured["problem_order"])
            self.assertEqual(2, captured["entry_count"])
            entries.assert_called_once()

    def test_session_publish_reflows_board_placement_overlap_before_build(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = {
                "session_name": "placement-overlap",
                "output_dir": raw_tmp,
                "pages": [
                    {"id": "page-1", "problemIds": ["p13"]},
                    {"id": "page-2", "problemIds": ["p14"]},
                ],
                "problems": [
                    {
                        "id": "p13",
                        "title": "13.",
                        "problemNumber": 13,
                        "sourcePageId": "page-1",
                        "bbox": {"left": 10, "top": 10, "width": 120, "height": 100},
                        "startYPages": 0.0,
                        "actualHeightPages": 1.1,
                    },
                    {
                        "id": "p14",
                        "title": "14.",
                        "problemNumber": 14,
                        "sourcePageId": "page-2",
                        "bbox": {"left": 10, "top": 140, "width": 120, "height": 100},
                        "startYPages": 1.2,
                        "actualHeightPages": 0.8,
                    },
                ],
            }
            handler, responses = self._publish(
                session,
                {"placements": {"p13": {"placementScaleRatio": 1.4}}},
            )

            def fake_problems_to_entries(problems, **_kwargs):
                self.assertEqual(["p13", "p14"], [problem["id"] for problem in problems])
                self.assertEqual(1.4, problems[0]["placementScaleRatio"])
                return [
                    app_server.ProblemEntry(
                        problem_id=problem["id"],
                        title=problem["title"],
                        problem_number=problem["problemNumber"],
                        subject=app_server.resolve_subject("math"),
                        source_page_id=problem["sourcePageId"],
                        source_path=problem["sourcePageId"],
                        prepared_page=None,
                        bounds=Box(left=0, top=0, width=100, height=100),
                        crop_path=root / f"{problem['id']}.png",
                        board_render_path=root / f"{problem['id']}.png",
                        blocks=[],
                        actual_height_pages=problem["actualHeightPages"],
                        overflow_allowed=True,
                        reading_heavy=False,
                        risk_flags=[],
                        placement_scale_ratio=problem.get("placementScaleRatio"),
                    )
                    for problem in problems
                ]

            def fake_build_records(entries, _template, **_kwargs):
                self.assertEqual(["p13", "p14"], [entry.problem_id for entry in entries])
                return (
                    [{"record": "p13"}, {"record": "p14"}],
                    [
                        {
                            "problem_id": "p13",
                            "title": "13.",
                            "record_index": 0,
                            "crop_path": str(root / "p13.png"),
                            "board_render_path": str(root / "p13.png"),
                            "start_y_pages": 0.0,
                            "snapped_next_start_y_pages": 2.4,
                            "actual_height_pages": 1.1,
                            "placement_scale_ratio": 1.4,
                        },
                        {
                            "problem_id": "p14",
                            "title": "14.",
                            "record_index": 1,
                            "crop_path": str(root / "p14.png"),
                            "board_render_path": str(root / "p14.png"),
                            "start_y_pages": 2.4,
                            "snapped_next_start_y_pages": 3.6,
                            "actual_height_pages": 0.8,
                            "placement_scale_ratio": 1.0,
                        },
                    ],
                    0,
                )

            def fake_build_ui_session(**kwargs):
                placements = kwargs["placements"]
                return {
                    "session_name": "published",
                    "output_dir": str(root),
                    "problems": [
                        {
                            "id": placement["problem_id"],
                            "title": placement["title"],
                            "problemNumber": 13 + index,
                            "sourcePageId": f"page-{index + 1}",
                            "imagePath": (root / f"p{13 + index}.png").resolve().as_uri(),
                            "bbox": {},
                            "riskFlags": [],
                            "startYPages": placement["start_y_pages"],
                            "snappedNextStartYPages": placement["snapped_next_start_y_pages"],
                            "actualHeightPages": placement["actual_height_pages"],
                            "placementScaleRatio": placement["placement_scale_ratio"],
                        }
                        for index, placement in enumerate(placements)
                    ],
                    "pages": [],
                }

            def fake_write_handoff(output_dir, *, ui_session, **_kwargs):
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps({
                        "status": "ready_for_classin_review",
                        "readyForClassIn": True,
                        "classinPreflight": {"status": "passed", "passed": True, "issueCount": 0, "issues": []},
                    }),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "_problems_to_entries", side_effect=fake_problems_to_entries) as entries,
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "build_edb", return_value=b"edb"),
                patch.object(app_server, "write_edb", side_effect=lambda path, data: Path(path).write_bytes(data)),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": 2,
                    "recordCountActual": 2,
                }),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            body, kwargs = responses[0]
            self.assertTrue(body["ok"], body)
            self.assertNotEqual(app_server.HTTPStatus.CONFLICT, kwargs.get("status"))
            entries.assert_called_once()
            remembered = handler.server.remembered_session
            self.assertEqual(0.0, remembered["problems"][0]["startYPages"])
            self.assertEqual(2.4, remembered["problems"][1]["startYPages"])
            self.assertEqual(1.4, remembered["problems"][0]["placementScaleRatio"])

    def test_session_publish_preserves_page_as_is_layout_metadata(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            original_page = root / "page-1-original.png"
            published_page = root / "p-page.png"
            Image.new("RGB", (1000, 1400), "white").save(original_page)
            Image.new("RGB", (1600, 2240), "white").save(published_page)
            session = {
                "session_name": "page-as-is-publish",
                "inputIntent": "page-as-is",
                "input_intent": "page-as-is",
                "output_dir": raw_tmp,
                "pages": [{"id": "page-1", "problemIds": ["p-page"]}],
                "problems": [
                    {
                        "id": "p-page",
                        "title": "페이지 1",
                        "originalImagePath": original_page.resolve().as_uri(),
                        "sourcePageId": "page-1",
                        "bbox": {"left": 0, "top": 0, "width": 1000, "height": 1400},
                        "riskFlags": [],
                        "inputIntent": "page-as-is",
                        "input_intent": "page-as-is",
                        "placementMode": "continuous-page-as-is",
                        "placement_mode": "continuous-page-as-is",
                        "forceFullPageBounds": True,
                        "force_full_page_bounds": True,
                        "placementScaleRatio": 1.6,
                    }
                ],
            }
            handler, responses = self._publish(session)

            def fake_problems_to_entries(problems, **_kwargs):
                self.assertEqual("page-as-is", problems[0]["inputIntent"])
                self.assertTrue(problems[0]["forceFullPageBounds"])
                return [
                    app_server.ProblemEntry(
                        problem_id="p-page",
                        title="페이지 1",
                        problem_number=None,
                        subject=app_server.resolve_subject("unknown"),
                        source_page_id="page-1",
                        source_path="page-1",
                        prepared_page=None,
                        bounds=Box(left=0, top=0, width=1000, height=1400),
                        crop_path=root / "p-page.png",
                        board_render_path=root / "p-page.png",
                        blocks=[],
                        actual_height_pages=1.2,
                        overflow_allowed=False,
                        reading_heavy=False,
                        risk_flags=[],
                        placement_scale_ratio=1.6,
                        input_intent="page-as-is",
                        force_full_page_bounds=True,
                    )
                ]

            def fake_build_records(entries, _template, **_kwargs):
                self.assertEqual("page-as-is", entries[0].input_intent)
                self.assertTrue(entries[0].force_full_page_bounds)
                return (
                    [{"record": "p-page"}],
                    [
                        {
                            "problem_id": "p-page",
                            "title": "페이지 1",
                            "record_index": 0,
                            "crop_path": str(root / "p-page.png"),
                            "board_render_path": str(root / "p-page.png"),
                            "start_y_pages": 0.0,
                            "snapped_next_start_y_pages": 1.9,
                            "actual_height_pages": 1.2,
                            "placement_scale_ratio": 1.6,
                        }
                    ],
                    0,
                )

            def fake_build_ui_session(**_kwargs):
                return {
                    "session_name": "published",
                    "inputIntent": "auto",
                    "input_intent": "auto",
                    "output_dir": str(root),
                    "problems": [
                        {
                            "id": "p-page",
                            "title": "페이지 1",
                            "sourcePageId": "page-1",
                            "imagePath": (root / "p-page.png").resolve().as_uri(),
                            "originalImagePath": published_page.resolve().as_uri(),
                            "bbox": {},
                            "riskFlags": [],
                            "inputIntent": "auto",
                            "input_intent": "auto",
                            "placementMode": "one-problem-per-page",
                            "placement_mode": "one-problem-per-page",
                            "forceFullPageBounds": False,
                            "force_full_page_bounds": False,
                            "placementScaleRatio": 1.6,
                        }
                    ],
                    "pages": [],
                }

            def fake_write_classin_limited_edb_files(_entries, _template, output_dir, edb_name, **_kwargs):
                path = Path(output_dir) / edb_name
                path.write_bytes(b"edb")
                return [
                    {
                        "partIndex": 1,
                        "partCount": 1,
                        "edbPath": str(path),
                        "edbFileName": path.name,
                        "recordCount": 1,
                        "pageCountHint": 50,
                    }
                ]

            def fake_write_handoff(output_dir, *, ui_session, **_kwargs):
                problem = ui_session["problems"][0]
                self.assertEqual("page-as-is", problem["inputIntent"])
                self.assertEqual("continuous-page-as-is", problem["placementMode"])
                self.assertTrue(problem["forceFullPageBounds"])
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps({
                        "classinPreflight": {
                            "status": "passed",
                            "passed": True,
                            "issueCount": 0,
                            "issues": [],
                        }
                    }),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "_problems_to_entries", side_effect=fake_problems_to_entries),
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "write_classin_limited_edb_files", side_effect=fake_write_classin_limited_edb_files),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": 1,
                    "recordCountActual": 1,
                }),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            body, _kwargs = responses[0]
            self.assertTrue(body["ok"], body)
            remembered = handler.server.remembered_session
            self.assertEqual("page-as-is", remembered["inputIntent"])
            self.assertEqual("page-as-is", remembered["input_intent"])
            remembered_problem = remembered["problems"][0]
            self.assertEqual("page-as-is", remembered_problem["inputIntent"])
            self.assertEqual("page-as-is", remembered_problem["input_intent"])
            self.assertEqual("continuous-page-as-is", remembered_problem["placementMode"])
            self.assertEqual("continuous-page-as-is", remembered_problem["placement_mode"])
            self.assertEqual(original_page.resolve().as_uri(), remembered_problem["originalImagePath"])
            self.assertEqual(original_page.resolve().as_uri(), remembered_problem["original_image_path"])
            self.assertTrue(remembered_problem["forceFullPageBounds"])
            self.assertTrue(remembered_problem["force_full_page_bounds"])

    def test_session_publish_blocks_unresolved_passage_review_queue_before_build(self):
        with TemporaryDirectory() as raw_tmp:
            session = {
                "session_name": "passage-review-queue",
                "output_dir": raw_tmp,
                "pages": [
                    {"id": "page-5", "problemIds": ["p31"]},
                    {"id": "page-6", "problemIds": ["p32"]},
                ],
                "passageReviewItems": [
                    {
                        "groupId": "hwp-text-passage-31-32",
                        "numberLabel": "31-32",
                        "problemIds": ["p31", "p32"],
                        "fragmentProblemIds": ["page-5-continuation"],
                        "sourcePageIds": ["page-5", "page-6"],
                        "problemCount": 2,
                        "fragmentProblemCount": 1,
                        "continuesAcrossPages": True,
                        "reviewReasonCodes": ["cross_page_passage_group", "passage_fragment"],
                        "riskFlags": ["passage_cross_page_merge_check"],
                        "message": "31-32 지문 묶음은 2개 페이지와 2개 하위 문항, 지문 본문 1개를 확인해야 합니다.",
                    }
                ],
                "passageReviewItemCount": 1,
                "crossPagePassageReviewItemCount": 1,
                "problems": [
                    {
                        "id": "p31",
                        "title": "31.",
                        "problemNumber": 31,
                        "sourcePageId": "page-5",
                        "bbox": {"left": 10, "top": 10, "width": 120, "height": 100},
                        "reviewStatus": "check_needed",
                        "riskFlags": [],
                    },
                    {
                        "id": "p32",
                        "title": "32.",
                        "problemNumber": 32,
                        "sourcePageId": "page-6",
                        "bbox": {"left": 20, "top": 20, "width": 130, "height": 100},
                        "reviewStatus": "normal",
                        "riskFlags": [],
                    },
                ],
            }
            handler, responses = self._publish(session)

            with patch.object(app_server, "_problems_to_entries", side_effect=AssertionError("build should be blocked")) as entries:
                handler._handle_session_publish()

            body, kwargs = responses[0]
            self.assertFalse(body["ok"])
            self.assertEqual("publish_preflight_blocked", body["errorKind"])
            self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs.get("status"))
            self.assertIn("제작 전 확인", body["error"])
            self.assertNotIn("겹침/중복", body["error"])
            issues = body["classinPreflight"]["issues"]
            issue_types = {issue["type"] for issue in issues}
            self.assertIn("passage_review_queue_remaining", issue_types)
            queue_issue = next(issue for issue in issues if issue["type"] == "passage_review_queue_remaining")
            self.assertEqual(["p31", "p32", "page-5-continuation"], queue_issue["problemIds"])
            self.assertEqual(["page-5-continuation"], queue_issue["fragmentProblemIds"])
            self.assertEqual(["cross_page_passage_group", "passage_fragment"], queue_issue["reviewReasonCodes"])
            self.assertEqual(["passage_cross_page_merge_check"], queue_issue["riskFlags"])
            self.assertEqual(2, queue_issue["problemCount"])
            self.assertEqual(1, queue_issue["fragmentProblemCount"])
            self.assertIn("지문 본문 1개", queue_issue["message"])
            entries.assert_not_called()

    def test_session_publish_excludes_supplemental_passage_fragments_from_edb_entries(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            fragment_crop = root / "page-2-continuation.png"
            p22_crop = root / "p22.png"
            p23_crop = root / "p23.png"
            for crop_path in (fragment_crop, p22_crop, p23_crop):
                crop_path.write_bytes(b"fake image")

            session = {
                "session_name": "passage-fragment-publish",
                "output_dir": str(root),
                "input_files": [str(root / "source.hwp")],
                "pages": [
                    {"id": "page-2", "problemIds": ["page-2-continuation", "p22"]},
                    {"id": "page-3", "problemIds": ["p23"]},
                ],
                "problems": [
                    {
                        "id": "page-2-continuation",
                        "title": "지문 계속",
                        "sourcePageId": "page-2",
                        "subject": "korean",
                        "imagePath": fragment_crop.resolve().as_uri(),
                        "bbox": {"left": 10, "top": 20, "width": 520, "height": 420},
                        "riskFlags": ["marker_document_continuation", "passage_cross_page_merge_check"],
                        "passageGroupId": "hwp-continuation-passage-22-23",
                        "passageRole": "passage_fragment",
                        "passageRange": {"start": 22, "end": 23},
                        "passageChildProblemNumbers": [22, 23],
                        "passageSourcePageIds": ["page-2", "page-3"],
                        "passageContinuesAcrossPages": True,
                    },
                    {
                        "id": "p22",
                        "title": "22.",
                        "problemNumber": 22,
                        "sourcePageId": "page-2",
                        "subject": "korean",
                        "imagePath": p22_crop.resolve().as_uri(),
                        "bbox": {"left": 30, "top": 60, "width": 500, "height": 360},
                        "passageGroupId": "hwp-continuation-passage-22-23",
                        "passageRole": "child_question",
                        "passageRange": {"start": 22, "end": 23},
                        "passageChildProblemNumbers": [22, 23],
                        "passageSourcePageIds": ["page-2", "page-3"],
                        "passageContinuesAcrossPages": True,
                    },
                    {
                        "id": "p23",
                        "title": "23.",
                        "problemNumber": 23,
                        "sourcePageId": "page-3",
                        "subject": "korean",
                        "imagePath": p23_crop.resolve().as_uri(),
                        "bbox": {"left": 30, "top": 430, "width": 500, "height": 220},
                        "passageGroupId": "hwp-continuation-passage-22-23",
                        "passageRole": "child_question",
                        "passageRange": {"start": 22, "end": 23},
                        "passageChildProblemNumbers": [22, 23],
                        "passageSourcePageIds": ["page-2", "page-3"],
                        "passageContinuesAcrossPages": True,
                    },
                ],
            }
            handler, responses = self._publish(session)

            def fake_build_records(entries, template, **_kwargs):
                self.assertEqual(["p22", "p23"], [entry.problem_id for entry in entries])
                return (
                    [{"record": "p22"}, {"record": "p23"}],
                    [
                        {
                            "problem_id": "p22",
                            "title": "22.",
                            "record_index": 0,
                            "crop_path": str(p22_crop),
                            "board_render_path": str(p22_crop),
                            "start_y_pages": 0.0,
                            "actual_height_pages": 1.0,
                        },
                        {
                            "problem_id": "p23",
                            "title": "23.",
                            "record_index": 1,
                            "crop_path": str(p23_crop),
                            "board_render_path": str(p23_crop),
                            "start_y_pages": 2.0,
                            "actual_height_pages": 1.0,
                        },
                    ],
                    0,
                )

            def fake_build_ui_session(**_kwargs):
                return {
                    "session_name": "published",
                    "output_dir": str(root),
                    "problems": [
                        {
                            "id": "p22",
                            "title": "22.",
                            "problemNumber": 22,
                            "sourcePageId": "page-2",
                            "imagePath": p22_crop.resolve().as_uri(),
                            "bbox": {},
                            "riskFlags": [],
                        },
                        {
                            "id": "p23",
                            "title": "23.",
                            "problemNumber": 23,
                            "sourcePageId": "page-3",
                            "imagePath": p23_crop.resolve().as_uri(),
                            "bbox": {},
                            "riskFlags": [],
                        },
                    ],
                    "pages": [],
                }

            def fake_write_handoff(output_dir, *, ui_session, **_kwargs):
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps(
                        {
                            "status": "ready_for_classin_review",
                            "readyForClassIn": True,
                            "classinPreflight": {"status": "passed", "passed": True, "issueCount": 0, "issues": []},
                            "passageGroups": [
                                {
                                    "id": "hwp-continuation-passage-22-23",
                                    "problemCount": 2,
                                    "continuesAcrossPages": True,
                                    "sourcePageIds": ["page-2", "page-3"],
                                }
                            ],
                            "passageGroupCount": 1,
                            "passageProblemCount": 2,
                            "crossPagePassageGroupCount": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "build_edb", return_value=b"edb"),
                patch.object(app_server, "write_edb", side_effect=lambda path, data: Path(path).write_bytes(data)),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": 2,
                    "recordCountActual": 2,
                }),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            self.assertEqual(1, len(responses))
            body, kwargs = responses[0]
            self.assertTrue(body["ok"], body)
            self.assertNotEqual(app_server.HTTPStatus.CONFLICT, kwargs.get("status"))
            summary = body["publishSummary"]
            self.assertEqual(2, summary["recordCount"])
            self.assertEqual(2, summary["coreProblemCount"])
            self.assertEqual(1, summary["supplementalItemCount"])
            self.assertEqual("2문항 + 자료 1", summary["recordCountLabel"])
            self.assertEqual(["p22", "p23"], [problem["id"] for problem in handler.server.remembered_session["problems"]])

    def test_session_publish_allows_official_alternate_section_duplicate_numbers(self):
        problems = []
        for section_index, source_prefix in enumerate(("speech-writing", "language-media")):
            for number in range(35, 41):
                problems.append({
                    "id": f"{source_prefix}-{number}",
                    "title": f"{number}.",
                    "problemNumber": number,
                    "sourcePageId": f"{source_prefix}-{number}",
                    "bbox": {
                        "left": 10,
                        "top": 20 + section_index * 220,
                        "width": 120,
                        "height": 100,
                    },
                })

        preflight, duplicate_groups = app_server._session_publish_blocking_preflight(problems)

        self.assertTrue(preflight["passed"])
        self.assertEqual("passed", preflight["status"])
        self.assertEqual(0, preflight["issueCount"])
        self.assertEqual([], duplicate_groups)

    def test_session_publish_preserves_passage_groups_in_publish_summary(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            crop_path = root / "p13.png"
            crop_path.write_bytes(b"fake image")
            session = {
                "session_name": "passage-publish",
                "output_dir": str(root),
                "input_files": [str(root / "source.hwp")],
                "pages": [{"id": "page-1", "problemIds": ["p13"]}],
                "problems": [
                    {
                        "id": "p13",
                        "title": "13.",
                        "problemNumber": 13,
                        "sourcePageId": "page-1",
                        "imagePath": crop_path.resolve().as_uri(),
                        "bbox": {"left": 10, "top": 10, "width": 120, "height": 100},
                        "passageGroupId": "page-1-passage-13-16",
                        "passageRange": {"start": 13, "end": 16},
                        "passageSourcePageIds": ["page-1", "page-2"],
                        "passageContinuesAcrossPages": True,
                    }
                ],
            }
            handler, responses = self._publish(session)

            def fake_build_records(entries, template, **_kwargs):
                return (
                    [{"record": "p13"}],
                    [
                        {
                            "problem_id": "p13",
                            "title": "13.",
                            "record_index": 0,
                            "crop_path": str(crop_path),
                            "board_render_path": str(crop_path),
                            "start_y_pages": 0.0,
                            "actual_height_pages": 1.0,
                        }
                    ],
                    0,
                )

            def fake_build_ui_session(**_kwargs):
                return {
                    "session_name": "published",
                    "output_dir": str(root),
                    "problems": [
                        {
                            "id": "p13",
                            "title": "13.",
                            "problemNumber": 13,
                            "sourcePageId": "page-1",
                            "imagePath": crop_path.resolve().as_uri(),
                            "bbox": {},
                            "riskFlags": [],
                        }
                    ],
                    "pages": [],
                }

            def fake_write_handoff(output_dir, *, ui_session, **_kwargs):
                grouped = [
                    problem for problem in ui_session.get("problems", [])
                    if problem.get("passageGroupId") == "page-1-passage-13-16"
                ]
                passage_groups = (
                    [
                        {
                            "id": "page-1-passage-13-16",
                            "problemCount": len(grouped),
                            "continuesAcrossPages": True,
                            "sourcePageIds": ["page-1", "page-2"],
                        }
                    ]
                    if grouped
                    else []
                )
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps(
                        {
                            "status": "ready_for_classin_review",
                            "readyForClassIn": True,
                            "classinPreflight": {"status": "passed", "passed": True, "issueCount": 0, "issues": []},
                            "passageGroups": passage_groups,
                            "passageGroupCount": len(passage_groups),
                            "passageProblemCount": len(grouped),
                            "crossPagePassageGroupCount": len(passage_groups),
                        }
                    ),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "build_edb", return_value=b"edb"),
                patch.object(app_server, "write_edb", side_effect=lambda path, data: Path(path).write_bytes(data)),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": 1,
                    "recordCountActual": 1,
                }),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            self.assertEqual(1, len(responses))
            body, _kwargs = responses[0]
            self.assertTrue(body["ok"])
            summary = body["publishSummary"]
            self.assertEqual(1, summary["passageGroupCount"])
            self.assertEqual(1, summary["passageProblemCount"])
            self.assertEqual(1, summary["crossPagePassageGroupCount"])
            self.assertEqual("page-1-passage-13-16", summary["passageGroups"][0]["id"])
            remembered_problem = handler.server.remembered_session["problems"][0]
            self.assertEqual("page-1-passage-13-16", remembered_problem["passageGroupId"])
            self.assertTrue(remembered_problem["passageContinuesAcrossPages"])

    def test_session_publish_preserves_passage_group_source_reuse_in_publish_summary(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            crop_path = root / "p31.png"
            crop_path.write_bytes(b"fake image")
            source_reuse_groups = [
                {
                    "passageGroupId": "hwp-text-passage-31-34",
                    "sourcePageId": "page-004",
                    "problemIds": ["p31", "p32"],
                    "overlapAreaRatio": 0.92,
                }
            ]
            session = {
                "session_name": "passage-reuse-publish",
                "output_dir": str(root),
                "input_files": [str(root / "source.hwp")],
                "pages": [{"id": "page-004", "problemIds": ["p31", "p32"]}],
                "problems": [
                    {
                        "id": "p31",
                        "title": "31.",
                        "problemNumber": 31,
                        "sourcePageId": "page-004",
                        "imagePath": crop_path.resolve().as_uri(),
                        "bbox": {"left": 10, "top": 10, "width": 120, "height": 100},
                    },
                    {
                        "id": "p32",
                        "title": "32.",
                        "problemNumber": 32,
                        "sourcePageId": "page-004",
                        "imagePath": crop_path.resolve().as_uri(),
                        "bbox": {"left": 200, "top": 10, "width": 120, "height": 100},
                    },
                ],
            }
            handler, responses = self._publish(session)

            def fake_build_records(entries, template, **_kwargs):
                return (
                    [{"record": "p31"}, {"record": "p32"}],
                    [
                        {
                            "problem_id": "p31",
                            "title": "31.",
                            "record_index": 0,
                            "crop_path": str(crop_path),
                            "board_render_path": str(crop_path),
                            "start_y_pages": 0.0,
                            "actual_height_pages": 1.0,
                        },
                        {
                            "problem_id": "p32",
                            "title": "32.",
                            "record_index": 1,
                            "crop_path": str(crop_path),
                            "board_render_path": str(crop_path),
                            "start_y_pages": 1.0,
                            "actual_height_pages": 1.0,
                        },
                    ],
                    0,
                )

            def fake_build_ui_session(**_kwargs):
                return {
                    "session_name": "published",
                    "output_dir": str(root),
                    "problems": [
                        {"id": "p31", "title": "31.", "riskFlags": []},
                        {"id": "p32", "title": "32.", "riskFlags": []},
                    ],
                    "pages": [],
                }

            def fake_write_handoff(output_dir, **_kwargs):
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps(
                        {
                            "status": "ready_for_classin_review",
                            "readyForClassIn": True,
                            "classinPreflight": {"status": "passed", "passed": True, "issueCount": 0, "issues": []},
                            "passageGroupSourceReuseGroups": source_reuse_groups,
                            "passageGroupSourceReuseGroupCount": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "build_edb", return_value=b"edb"),
                patch.object(app_server, "write_edb", side_effect=lambda path, data: Path(path).write_bytes(data)),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": 2,
                    "recordCountActual": 2,
                }),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            self.assertEqual(1, len(responses))
            body, _kwargs = responses[0]
            self.assertTrue(body["ok"])
            summary = body["publishSummary"]
            self.assertEqual(source_reuse_groups, summary["passageGroupSourceReuseGroups"])
            self.assertEqual(source_reuse_groups, summary["passage_group_source_reuse_groups"])
            self.assertEqual(1, summary["passageGroupSourceReuseGroupCount"])
            self.assertEqual(1, summary["passage_group_source_reuse_group_count"])
            self.assertEqual(source_reuse_groups, handler.server.remembered_session["publishSummary"]["passageGroupSourceReuseGroups"])

    def test_session_publish_preserves_source_problem_overlap_in_publish_summary(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            crop_path = root / "p31.png"
            crop_path.write_bytes(b"fake image")
            source_overlap_groups = [
                {
                    "sourcePageId": "page-004",
                    "problemIds": ["p31", "p32"],
                    "overlapAreaRatio": 0.88,
                }
            ]
            session = {
                "session_name": "source-overlap-publish",
                "output_dir": str(root),
                "input_files": [str(root / "source.hwp")],
                "pages": [{"id": "page-004", "problemIds": ["p31", "p32"]}],
                "problems": [
                    {
                        "id": "p31",
                        "title": "31.",
                        "problemNumber": 31,
                        "sourcePageId": "page-004",
                        "imagePath": crop_path.resolve().as_uri(),
                        "bbox": {"left": 10, "top": 10, "width": 120, "height": 100},
                    },
                    {
                        "id": "p32",
                        "title": "32.",
                        "problemNumber": 32,
                        "sourcePageId": "page-004",
                        "imagePath": crop_path.resolve().as_uri(),
                        "bbox": {"left": 200, "top": 10, "width": 120, "height": 100},
                    },
                ],
            }
            handler, responses = self._publish(session)

            def fake_build_records(entries, template, **_kwargs):
                return (
                    [{"record": "p31"}, {"record": "p32"}],
                    [
                        {
                            "problem_id": "p31",
                            "title": "31.",
                            "record_index": 0,
                            "crop_path": str(crop_path),
                            "board_render_path": str(crop_path),
                            "start_y_pages": 0.0,
                            "actual_height_pages": 1.0,
                        },
                        {
                            "problem_id": "p32",
                            "title": "32.",
                            "record_index": 1,
                            "crop_path": str(crop_path),
                            "board_render_path": str(crop_path),
                            "start_y_pages": 1.0,
                            "actual_height_pages": 1.0,
                        },
                    ],
                    0,
                )

            def fake_build_ui_session(**_kwargs):
                return {
                    "session_name": "published",
                    "output_dir": str(root),
                    "problems": [
                        {"id": "p31", "title": "31.", "riskFlags": []},
                        {"id": "p32", "title": "32.", "riskFlags": []},
                    ],
                    "pages": [],
                }

            def fake_write_handoff(output_dir, **_kwargs):
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps(
                        {
                            "status": "ready_for_classin_review",
                            "readyForClassIn": True,
                            "classinPreflight": {"status": "passed", "passed": True, "issueCount": 0, "issues": []},
                            "sourceProblemOverlapGroups": source_overlap_groups,
                            "sourceProblemOverlapGroupCount": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "build_edb", return_value=b"edb"),
                patch.object(app_server, "write_edb", side_effect=lambda path, data: Path(path).write_bytes(data)),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": 2,
                    "recordCountActual": 2,
                }),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            self.assertEqual(1, len(responses))
            body, _kwargs = responses[0]
            self.assertTrue(body["ok"])
            summary = body["publishSummary"]
            self.assertEqual(source_overlap_groups, summary["sourceProblemOverlapGroups"])
            self.assertEqual(source_overlap_groups, summary["source_problem_overlap_groups"])
            self.assertEqual(1, summary["sourceProblemOverlapGroupCount"])
            self.assertEqual(1, summary["source_problem_overlap_group_count"])
            self.assertEqual("문항 영역 겹침 1건", summary["sourceProblemOverlapLabel"])
            self.assertEqual("문항 영역 겹침 1건", summary["source_problem_overlap_label"])
            self.assertEqual("page-004 88%", summary["sourceProblemOverlapDetailLabel"])
            self.assertEqual("page-004 88%", summary["source_problem_overlap_detail_label"])
            self.assertEqual(source_overlap_groups, handler.server.remembered_session["publishSummary"]["sourceProblemOverlapGroups"])

    def test_session_publish_summary_prefers_resolved_session_passage_review_queue(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            crop_path = root / "p31.png"
            crop_path.write_bytes(b"fake image")
            session = {
                "session_name": "resolved-passage-publish",
                "output_dir": str(root),
                "input_files": [str(root / "source.hwp")],
                "pages": [{"id": "page-5", "problemIds": ["p31"]}],
                "passageReviewItems": [
                    {
                        "groupId": "hwp-text-passage-31-32",
                        "numberLabel": "31-32",
                        "problemIds": ["p31"],
                        "continuesAcrossPages": True,
                    }
                ],
                "passageReviewItemCount": 1,
                "crossPagePassageReviewItemCount": 1,
                "problems": [
                    {
                        "id": "p31",
                        "title": "31.",
                        "problemNumber": 31,
                        "sourcePageId": "page-5",
                        "imagePath": crop_path.resolve().as_uri(),
                        "bbox": {"left": 10, "top": 10, "width": 120, "height": 100},
                        "reviewStatus": "normal",
                        "riskFlags": [],
                    }
                ],
            }
            handler, responses = self._publish(session)

            def fake_build_records(entries, template, **_kwargs):
                return (
                    [{"record": "p31"}],
                    [
                        {
                            "problem_id": "p31",
                            "title": "31.",
                            "record_index": 0,
                            "crop_path": str(crop_path),
                            "board_render_path": str(crop_path),
                            "start_y_pages": 0.0,
                            "actual_height_pages": 1.0,
                        }
                    ],
                    0,
                )

            def fake_build_ui_session(**_kwargs):
                return {
                    "session_name": "published",
                    "output_dir": str(root),
                    "problems": [
                        {
                            "id": "p31",
                            "title": "31.",
                            "problemNumber": 31,
                            "sourcePageId": "page-5",
                            "imagePath": crop_path.resolve().as_uri(),
                            "bbox": {},
                            "riskFlags": [],
                        }
                    ],
                    "pages": [],
                }

            def fake_write_handoff(output_dir, **_kwargs):
                handoff_path = Path(output_dir) / "classin_handoff.json"
                handoff_md_path = Path(output_dir) / "classin_handoff.md"
                handoff_path.write_text(
                    json.dumps(
                        {
                            "status": "ready_for_classin_review",
                            "readyForClassIn": True,
                            "classinPreflight": {"status": "passed", "passed": True, "issueCount": 0, "issues": []},
                            "passageReviewItems": [
                                {
                                    "groupId": "hwp-text-passage-31-32",
                                    "numberLabel": "31-32",
                                    "problemIds": ["p31"],
                                    "continuesAcrossPages": True,
                                }
                            ],
                            "passageReviewItemCount": 1,
                            "crossPagePassageReviewItemCount": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                handoff_md_path.write_text("# handoff", encoding="utf-8")
                return handoff_path, handoff_md_path

            with (
                patch.object(app_server, "build_records", side_effect=fake_build_records),
                patch.object(app_server, "build_ui_session", side_effect=fake_build_ui_session),
                patch.object(app_server, "build_edb", return_value=b"edb"),
                patch.object(app_server, "write_edb", side_effect=lambda path, data: Path(path).write_bytes(data)),
                patch.object(app_server, "validate_edb_file", return_value={
                    "outerSize": 10,
                    "innerSize": 8,
                    "pageCountHint": 50,
                    "recordCountHint": 1,
                    "recordCountActual": 1,
                }),
                patch.object(app_server, "write_classin_handoff_manifest", side_effect=fake_write_handoff),
            ):
                handler._handle_session_publish()

            self.assertEqual(1, len(responses))
            body, _kwargs = responses[0]
            self.assertTrue(body["ok"], body)
            summary = body["publishSummary"]
            self.assertEqual([], summary["passageReviewItems"])
            self.assertEqual(0, summary["passageReviewItemCount"])
            self.assertEqual(0, summary["crossPagePassageReviewItemCount"])
            remembered = handler.server.remembered_session
            self.assertEqual([], remembered["passageReviewItems"])
            self.assertEqual(0, remembered["passageReviewItemCount"])

    def test_session_publish_summary_exposes_passage_review_items(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            edb_path = root / "lesson.edb"
            edb_path.write_bytes(b"placeholder")
            review_items = [
                {
                    "groupId": "hwp-text-passage-31-34",
                    "numberLabel": "31-34",
                    "problemIds": ["p31", "p32"],
                    "sourcePageIds": ["page-5", "page-6"],
                    "problemCount": 2,
                    "continuesAcrossPages": True,
                    "reviewReasonCodes": [
                        "cross_page_passage_group",
                        "passage_missing_child_questions",
                        "cross_page_passage_group",
                    ],
                }
            ]

            summary = app_server._session_publish_summary(
                edb_path=edb_path,
                output_dir=root,
                edb_validation={
                    "outerSize": 1234,
                    "innerSize": 987,
                    "pageCountHint": 50,
                    "recordCountHint": 2,
                    "recordCountActual": 2,
                },
                record_count=2,
                passage_review_items=review_items,
                published_at="2026-06-13T12:00:00+09:00",
            )

            self.assertEqual(review_items, summary["passageReviewItems"])
            self.assertEqual(review_items, summary["passage_review_items"])
            self.assertEqual(1, summary["passageReviewItemCount"])
            self.assertEqual(1, summary["passage_review_item_count"])
            self.assertEqual(1, summary["crossPagePassageReviewItemCount"])
            self.assertEqual(1, summary["cross_page_passage_review_item_count"])
            self.assertEqual("페이지 이어짐, 문항 누락", summary["passageReviewReasonLabel"])
            self.assertEqual("페이지 이어짐, 문항 누락", summary["passage_review_reason_label"])


class TestRuntimeDiagnostics(unittest.TestCase):
    def test_startup_retention_cleanup_is_best_effort(self):
        class FailingServer:
            def cleanup_artifacts(self, **_kwargs):
                raise OSError("locked")

        self.assertIsNone(app_server._run_startup_artifact_cleanup(FailingServer()))

    def test_startup_retention_cleanup_defaults_to_non_destructive_dry_run(self):
        calls = []

        class Server:
            def cleanup_artifacts(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "dryRun": kwargs["dry_run"]}

        with patch.dict(os.environ, {"EDB_AUTO_ARTIFACT_CLEANUP": ""}):
            result = app_server._run_startup_artifact_cleanup(Server())
        self.assertTrue(result["dryRun"])
        self.assertTrue(calls[0]["dry_run"])

    def test_startup_retention_cleanup_deletes_only_with_explicit_opt_in(self):
        calls = []

        class Server:
            def cleanup_artifacts(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "dryRun": kwargs["dry_run"]}

        with patch.dict(os.environ, {"EDB_AUTO_ARTIFACT_CLEANUP": "1"}):
            result = app_server._run_startup_artifact_cleanup(Server())
        self.assertFalse(result["dryRun"])
        self.assertFalse(calls[0]["dry_run"])

    def test_startup_retention_cleanup_does_not_treat_generic_true_as_delete_opt_in(self):
        calls = []

        class Server:
            def cleanup_artifacts(self, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "dryRun": kwargs["dry_run"]}

        with patch.dict(os.environ, {"EDB_AUTO_ARTIFACT_CLEANUP": "true"}):
            result = app_server._run_startup_artifact_cleanup(Server())
        self.assertTrue(result["dryRun"])
        self.assertTrue(calls[0]["dry_run"])

    def test_runtime_diagnostics_reports_hangul_converter_readiness(self):
        with (
            patch.object(app_server.preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]]),
            patch.object(app_server.preprocess, "_iter_hwp_hwpx_converter_commands", return_value=[["/usr/local/bin/hwpilot"]]),
            patch.object(app_server.preprocess, "_iter_pyhwp_html_converter_commands", return_value=[["/venv/bin/hwp5html"]]),
            patch.object(app_server.preprocess, "_iter_hwp_text_converter_commands", return_value=[["/venv/bin/hwp5txt"], ["/venv/bin/python", "-c", "import unhwp; unhwp.extract_text('x')"]]),
            patch.object(app_server.preprocess, "_iter_chrome_pdf_commands", return_value=[["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]]),
            patch.object(app_server.preprocess, "_iter_rhwp_core_renderer_commands", return_value=[]),
        ):
            diagnostics = app_server.describe_runtime_diagnostics()

        self.assertTrue(diagnostics["ok"])
        hangul = diagnostics["hangul"]
        self.assertTrue(hangul["pdfReady"])
        self.assertTrue(hangul["hwpReady"])
        self.assertTrue(hangul["hwpxReady"])
        self.assertEqual("ready", hangul["status"])
        self.assertEqual("soffice", hangul["pdfConverters"][0]["name"])
        self.assertEqual("hwpilot", hangul["hwpToHwpxConverters"][0]["name"])
        self.assertEqual("hwp5txt", hangul["textExtractors"][0]["name"])
        self.assertEqual("unhwp", hangul["textExtractors"][1]["name"])
        self.assertEqual("hwp5html", hangul["htmlConverters"][0]["name"])
        self.assertEqual("Google Chrome", hangul["chromePdfConverters"][0]["name"])
        self.assertEqual("준비됨", hangul["label"])
        self.assertEqual(
            {
                "pdfConverters": 1,
                "hwpToHwpxConverters": 1,
                "htmlConverters": 1,
                "textExtractors": 2,
                "chromePdfConverters": 1,
                "hwpRenderers": 0,
            },
            hangul["toolCounts"],
        )
        self.assertIn("PDF 1", hangul["summary"])
        self.assertIn("텍스트 2", hangul["summary"])

    def test_runtime_diagnostics_treats_rhwp_core_renderer_as_hangul_ready(self):
        with (
            patch.object(app_server.preprocess, "_iter_hwp_pdf_converter_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_hwp_hwpx_converter_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_pyhwp_html_converter_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_hwp_text_converter_commands", return_value=[["/app/.bin/kordoc"]]),
            patch.object(app_server.preprocess, "_iter_chrome_pdf_commands", return_value=[]),
            patch.object(
                app_server.preprocess,
                "_iter_rhwp_core_renderer_commands",
                return_value=[["/usr/bin/node", "/app/scripts/render_hwp_with_rhwp_core.mjs", "--node-modules", "/app/node_modules"]],
            ),
        ):
            diagnostics = app_server.describe_runtime_diagnostics()

        hangul = diagnostics["hangul"]
        self.assertFalse(hangul["pdfReady"])
        self.assertTrue(hangul["hwpReady"])
        self.assertTrue(hangul["hwpxReady"])
        self.assertTrue(hangul["hwpRendererReady"])
        self.assertEqual("ready", hangul["status"])
        self.assertEqual("rhwp-core", hangul["hwpRenderers"][0]["name"])
        self.assertIn("렌더 1", hangul["summary"])
        self.assertFalse(any("PDF converter was not found" in warning for warning in hangul["warnings"]))

    def test_runtime_diagnostics_reports_actionable_hangul_warning_when_missing_pdf_converter(self):
        with (
            patch.object(app_server.preprocess, "_iter_hwp_pdf_converter_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_hwp_hwpx_converter_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_pyhwp_html_converter_commands", return_value=[["/venv/bin/hwp5html"]]),
            patch.object(app_server.preprocess, "_iter_pyhwp_text_converter_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_chrome_pdf_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_rhwp_core_renderer_commands", return_value=[]),
        ):
            diagnostics = app_server.describe_runtime_diagnostics()

        hangul = diagnostics["hangul"]
        self.assertFalse(hangul["pdfReady"])
        self.assertFalse(hangul["hwpReady"])
        self.assertFalse(hangul["hwpxReady"])
        self.assertEqual("blocked", hangul["status"])
        self.assertEqual("확인 필요", hangul["label"])
        self.assertIn("주의", hangul["summary"])
        self.assertTrue(any("rhwp" in warning for warning in hangul["warnings"]))
        self.assertTrue(any("PDF" in step for step in hangul["recommendedActions"]))
        self.assertTrue(any("Chrome" in warning or "LibreOffice" in warning for warning in hangul["warnings"]))

    def test_runtime_diagnostics_labels_node_wrapped_hwpilot_bridge(self):
        with (
            patch.object(app_server.preprocess, "_iter_hwp_pdf_converter_commands", return_value=[["/usr/bin/soffice", "--headless"]]),
            patch.object(
                app_server.preprocess,
                "_iter_hwp_hwpx_converter_commands",
                return_value=[["/usr/local/bin/node", "/tmp/hwpilot-src/dist/src/cli/main.js"]],
            ),
            patch.object(app_server.preprocess, "_iter_pyhwp_html_converter_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_hwp_text_converter_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_chrome_pdf_commands", return_value=[]),
            patch.object(app_server.preprocess, "_iter_rhwp_core_renderer_commands", return_value=[]),
        ):
            diagnostics = app_server.describe_runtime_diagnostics()

        hangul = diagnostics["hangul"]
        self.assertEqual("hwpilot", hangul["hwpToHwpxConverters"][0]["name"])


class TestSessionHistory(unittest.TestCase):
    def test_session_file_paths_and_http_rewrite_include_classin_handoff_files(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            handoff_json = tmpdir / "classin_handoff.json"
            handoff_md = tmpdir / "classin_handoff.md"
            publish_handoff_json = tmpdir / "publish_classin_handoff.json"
            publish_handoff_md = tmpdir / "publish_classin_handoff.md"
            handoff_json.write_text("{}", encoding="utf-8")
            handoff_md.write_text("# check", encoding="utf-8")
            publish_handoff_json.write_text("{}", encoding="utf-8")
            publish_handoff_md.write_text("# publish check", encoding="utf-8")
            session = {
                "classin_handoff_path": str(handoff_json),
                "classinHandoffMarkdownPath": str(handoff_md),
                "publishSummary": {
                    "classinHandoffPath": str(publish_handoff_json),
                    "classinHandoffMarkdownUri": publish_handoff_md.resolve().as_uri(),
                },
            }

            paths = app_server.collect_session_file_paths(session)
            rewritten = app_server.rewrite_session_for_http(session)

        self.assertIn(str(handoff_json.resolve()), paths)
        self.assertIn(str(handoff_md.resolve()), paths)
        self.assertIn(str(publish_handoff_json.resolve()), paths)
        self.assertIn(str(publish_handoff_md.resolve()), paths)
        self.assertIn("/api/file?path=", rewritten["classin_handoff_uri"])
        self.assertIn("/api/file?path=", rewritten["classin_handoff_markdown_uri"])

    def test_session_history_endpoint_allows_handoff_files_from_recent_work(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            handoff_json = tmpdir / "classin_handoff.json"
            handoff_md = tmpdir / "classin_handoff.md"
            handoff_json.write_text("{}", encoding="utf-8")
            handoff_md.write_text("# check", encoding="utf-8")
            history = [{
                "id": "recent",
                "publishSummary": {
                    "classinHandoffPath": str(handoff_json),
                    "classinHandoffMarkdownPath": str(handoff_md),
                },
            }]
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {"allowed_files": set()})()
            responses = []
            handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

            with patch.object(app_server, "load_session_history", return_value=history):
                handler._handle_session_history()

            self.assertIn(str(handoff_json.resolve()), handler.server.allowed_files)
            self.assertIn(str(handoff_md.resolve()), handler.server.allowed_files)
            self.assertEqual(1, len(responses))
            self.assertTrue(responses[0][0]["ok"])

    def test_session_history_deduplicates_by_output_dir_and_keeps_latest_snapshot(self):
        older = {
            "session_name": "국어 6월",
            "generated_at": "2026-06-13T12:00:00+09:00",
            "output_dir": "/tmp/session-a",
            "problems": [{"id": "old"}],
            "core_problem_count": 1,
            "supplemental_item_count": 0,
        }
        newer = {
            "session_name": "국어 6월",
            "generated_at": "2026-06-13T12:10:00+09:00",
            "output_dir": "/tmp/session-a",
            "problems": [{"id": "new-1"}, {"id": "new-2"}],
            "core_problem_count": 2,
            "supplemental_item_count": 0,
        }

        first = app_server._session_history_with_session([], older, updated_at="2026-06-13T12:00:00+09:00")
        history = app_server._session_history_with_session(first, newer, updated_at="2026-06-13T12:10:00+09:00")

        self.assertEqual(1, len(history))
        self.assertEqual("국어 6월", history[0]["sessionName"])
        self.assertEqual("/tmp/session-a", history[0]["outputDir"])
        self.assertEqual(2, history[0]["coreProblemCount"])
        self.assertEqual(["new-1", "new-2"], [problem["id"] for problem in history[0]["session"]["problems"]])

    def test_session_history_exposes_review_mode_without_full_session_payload(self):
        session = {
            "session_name": "페이지 원본",
            "generated_at": "2026-08-18T14:00:00+09:00",
            "output_dir": "/tmp/page-as-is-session",
            "input_intent": "page-as-is",
            "content_target": "all",
            "pages": [{"id": "page-001"}, {"id": "page-002"}],
            "problems": [{"id": "p1"}, {"id": "p2"}],
        }

        history = app_server._session_history_with_session([], session)
        public = app_server._public_session_history(history)

        self.assertNotIn("session", public[0])
        self.assertEqual("page-as-is", public[0]["inputIntent"])
        self.assertEqual("all", public[0]["contentTarget"])
        self.assertEqual(2, public[0]["pageCount"])

    def test_public_session_history_omits_full_session_payload(self):
        session = {
            "session_name": "영어 양식",
            "generated_at": "2026-06-13T12:00:00+09:00",
            "output_dir": "/tmp/session-b",
            "problems": [{"id": "p1"}],
            "core_problem_count": 1,
            "supplemental_item_count": 0,
        }
        history = app_server._session_history_with_session([], session, updated_at="2026-06-13T12:00:00+09:00")

        public = app_server._public_session_history(history)

        self.assertEqual(1, len(public))
        self.assertNotIn("session", public[0])
        self.assertEqual(history[0]["id"], public[0]["id"])
        self.assertEqual("영어 양식", public[0]["sessionName"])

    def test_public_session_history_marks_missing_publish_artifacts(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = {
                "session_name": "삭제된 제작본",
                "generated_at": "2026-06-13T12:00:00+09:00",
                "output_dir": str(root),
                "problems": [{"id": "p1"}],
                "publishSummary": {
                    "edbFileName": "missing.edb",
                    "edbPath": str(root / "missing.edb"),
                    "edbFileUri": "/api/file?path=missing",
                    "outputDir": str(root / "missing-output"),
                    "classinHandoffPath": str(root / "classin_handoff.json"),
                    "classinHandoffMarkdownPath": str(root / "classin_handoff.md"),
                },
            }
            history = app_server._session_history_with_session(
                [],
                session,
                updated_at="2026-06-13T12:00:00+09:00",
            )

            public = app_server._public_session_history(history)

            summary = public[0]["publishSummary"]
            self.assertFalse(summary["edbFileExists"])
            self.assertFalse(summary["outputDirExists"])
            self.assertFalse(summary["edb_file_exists"])
            self.assertFalse(summary["output_dir_exists"])
            self.assertIn("/api/file?path=", summary["classinHandoffUri"])
            self.assertIn("/api/file?path=", summary["classinHandoffMarkdownUri"])
            self.assertEqual(summary["classinHandoffUri"], summary["classin_handoff_uri"])
            self.assertEqual(summary["classinHandoffMarkdownUri"], summary["classin_handoff_markdown_uri"])

    def test_public_session_history_exposes_classin_handoff_readiness(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            handoff_path = root / "classin_handoff.json"
            handoff_path.write_text(
                json.dumps(
                    {
                        "status": "needs_attention_before_classin",
                        "readyForClassIn": False,
                    }
                ),
                encoding="utf-8",
            )
            session = {
                "session_name": "주의 필요 제작본",
                "generated_at": "2026-06-13T12:00:00+09:00",
                "output_dir": str(root),
                "problems": [{"id": "p1"}],
                "publishSummary": {
                    "edbFileName": "lesson.edb",
                    "edbPath": str(root / "lesson.edb"),
                    "outputDir": str(root),
                    "classinHandoffPath": str(handoff_path),
                },
            }
            history = app_server._session_history_with_session(
                [],
                session,
                updated_at="2026-06-13T12:00:00+09:00",
            )

            public = app_server._public_session_history(history)

            summary = public[0]["publishSummary"]
            self.assertEqual("needs_attention_before_classin", summary["classinHandoffStatus"])
            self.assertFalse(summary["readyForClassIn"])
            self.assertEqual("needs_attention_before_classin", summary["classin_handoff_status"])
            self.assertFalse(summary["ready_for_classin"])

    def test_public_session_history_backfills_passage_review_reason_label(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            session = {
                "session_name": "긴 지문 제작본",
                "generated_at": "2026-06-13T12:00:00+09:00",
                "output_dir": str(root),
                "problems": [{"id": "p31"}, {"id": "p32"}],
                "publishSummary": {
                    "edbFileName": "lesson.edb",
                    "edbPath": str(root / "lesson.edb"),
                    "outputDir": str(root),
                    "passageReviewItems": [
                        {
                            "numberLabel": "31-32",
                            "problemIds": ["p31", "p32"],
                            "reviewReasonCodes": [
                                "cross_page_passage_group",
                                "passage_missing_child_questions",
                            ],
                        }
                    ],
                },
            }
            history = app_server._session_history_with_session(
                [],
                session,
                updated_at="2026-06-13T12:00:00+09:00",
            )

            public = app_server._public_session_history(history)

            summary = public[0]["publishSummary"]
            self.assertEqual("페이지 이어짐, 문항 누락", summary["passageReviewReasonLabel"])
            self.assertEqual("페이지 이어짐, 문항 누락", summary["passage_review_reason_label"])

    def test_public_session_history_backfills_source_problem_overlap_label(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            source_overlap_groups = [
                {
                    "sourcePageId": "page-004",
                    "problemIds": ["p31", "p32"],
                    "overlapAreaRatio": 0.88,
                }
            ]
            session = {
                "session_name": "문항 영역 겹침 제작본",
                "generated_at": "2026-06-13T12:00:00+09:00",
                "output_dir": str(root),
                "problems": [{"id": "p31"}, {"id": "p32"}],
                "publishSummary": {
                    "edbFileName": "lesson.edb",
                    "edbPath": str(root / "lesson.edb"),
                    "outputDir": str(root),
                    "sourceProblemOverlapGroups": source_overlap_groups,
                },
            }
            history = app_server._session_history_with_session(
                [],
                session,
                updated_at="2026-06-13T12:00:00+09:00",
            )

            public = app_server._public_session_history(history)

            summary = public[0]["publishSummary"]
            self.assertEqual(source_overlap_groups, summary["sourceProblemOverlapGroups"])
            self.assertEqual(source_overlap_groups, summary["source_problem_overlap_groups"])
            self.assertEqual(1, summary["sourceProblemOverlapGroupCount"])
            self.assertEqual(1, summary["source_problem_overlap_group_count"])
            self.assertEqual("문항 영역 겹침 1건", summary["sourceProblemOverlapLabel"])
            self.assertEqual("문항 영역 겹침 1건", summary["source_problem_overlap_label"])
            self.assertEqual("page-004 88%", summary["sourceProblemOverlapDetailLabel"])
            self.assertEqual("page-004 88%", summary["source_problem_overlap_detail_label"])


class TestSystemOpenTargets(unittest.TestCase):
    def test_session_json_writes_are_atomic_when_replace_fails(self):
        with TemporaryDirectory() as raw_tmp:
            target = Path(raw_tmp) / "latest.json"
            history_target = Path(raw_tmp) / "history.json"
            target.write_text('{"version": "old"}', encoding="utf-8")
            history_target.write_text('[{"id": "old"}]', encoding="utf-8")

            with patch.object(app_server.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    app_server.save_latest_session({"version": "new"}, target)
                with self.assertRaises(OSError):
                    app_server.save_session_history([{"id": "new"}], history_target)

            self.assertEqual('{"version": "old"}', target.read_text(encoding="utf-8"))
            self.assertEqual('[{"id": "old"}]', history_target.read_text(encoding="utf-8"))
            self.assertEqual([], list(target.parent.glob(".latest.json.*.tmp")))
            self.assertEqual([], list(target.parent.glob(".history.json.*.tmp")))

    def test_concurrent_history_updates_do_not_lose_distinct_sessions(self):
        with TemporaryDirectory() as raw_tmp:
            history_path = Path(raw_tmp) / "history.json"
            barrier = threading.Barrier(8)

            def remember(index):
                barrier.wait()
                app_server.remember_session_history(
                    {
                        "session_name": f"session-{index}",
                        "output_dir": f"/tmp/session-{index}",
                        "problems": [{"id": f"p{index}"}],
                    },
                    path=history_path,
                    limit=20,
                )

            threads = [threading.Thread(target=remember, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            history = app_server.load_session_history(history_path)
            self.assertEqual(8, len(history))
            self.assertEqual(
                {f"/tmp/session-{index}" for index in range(8)},
                {entry["outputDir"] for entry in history},
            )

    def test_compare_and_remember_rejects_stale_session_snapshot(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    self.assertRegex(server.session_epoch(), r"^\d{20}-[0-9a-f]{32}$")
                    base = {"session_name": "lesson", "problems": [{"id": "p1"}]}
                    server.remember_session(base)
                    client_revision = server.session_revision()
                    first_base = server.session_snapshot()
                    stale_base = server.session_snapshot()
                    self.assertIsNotNone(first_base)
                    self.assertIsNotNone(stale_base)

                    first_update = dict(first_base)
                    first_update["firstMutation"] = True
                    stale_update = dict(stale_base)
                    stale_update["staleMutation"] = True

                    first_committed_revision = server.remember_session_if_current(
                        first_base,
                        first_update,
                        expected_revision=client_revision,
                    )
                    self.assertEqual(client_revision + 1, first_committed_revision)
                    # Even if a stale restore request starts after the first
                    # commit and therefore snapshots the new server state, the
                    # revision captured by its client is still rejected.
                    second_request_base = server.session_snapshot()
                    self.assertFalse(
                        server.remember_session_if_current(
                            second_request_base,
                            stale_update,
                            expected_revision=client_revision,
                        )
                    )
                    current = server.session_snapshot()
                    self.assertTrue(current["firstMutation"])
                    self.assertNotIn("staleMutation", current)
                    self.assertEqual(current, app_server.load_latest_session())
                finally:
                    server.server_close()

    def test_history_write_failure_does_not_split_committed_latest_from_memory(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    updated = {"session_name": "committed", "problems": [{"id": "p1"}]}
                    with patch.object(
                        app_server,
                        "save_session_history",
                        side_effect=OSError("disk full"),
                    ):
                        revision = server.remember_session_if_current(
                            None,
                            updated,
                            expected_revision=0,
                        )

                    self.assertEqual(1, revision)
                    self.assertEqual(updated, server.session_snapshot())
                    self.assertEqual(updated, app_server.load_latest_session())
                    self.assertFalse(history_path.exists())
                finally:
                    server.server_close()

                restarted = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    restarted_snapshot = restarted.session_snapshot()
                    self.assertEqual("committed", restarted_snapshot["session_name"])
                    self.assertEqual(["p1"], [item["id"] for item in restarted_snapshot["problems"]])
                    self.assertEqual(1, restarted.session_revision())
                finally:
                    restarted.server_close()

    def test_mutation_response_pairs_committed_session_with_its_revision(self):
        base = {
            "pages": [{"id": "page-1", "problemIds": ["p1"]}],
            "problems": [{
                "id": "p1",
                "riskFlags": ["needs_review"],
                "reviewStatus": "check_needed",
            }],
        }

        class RacingServer:
            def __init__(self):
                self.latest_session = app_server._clone_jsonish(base)
                self.allowed_files = set()

            def session_snapshot_with_revision(self):
                return app_server._clone_jsonish(self.latest_session), 5

            def remember_session_if_current(self, expected, updated, *, expected_revision=None):
                self.latest_session = app_server._clone_jsonish(updated)
                return 6

            def session_revision(self):
                # A later request may already have committed before the first
                # response is serialized. This value must never be mixed with
                # the session returned by the first commit.
                return 7

            def session_epoch(self):
                return "epoch-current"

        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = RacingServer()
        handler._read_json_body = lambda: {
            "action": "confirm",
            "problemIds": ["p1"],
            "expectedSessionRevision": 5,
            "expectedSessionEpoch": "epoch-current",
        }
        responses = []
        handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

        handler._handle_session_mutate()

        payload, _kwargs = responses[0]
        self.assertEqual(6, payload["sessionRevision"])
        self.assertEqual("normal", payload["session"]["problems"][0]["reviewStatus"])

    def test_mutation_rejects_revision_from_previous_server_epoch(self):
        base = {"problems": [{"id": "p1", "reviewStatus": "check_needed"}]}

        class RestartedServer:
            def __init__(self):
                self.latest_session = app_server._clone_jsonish(base)
                self.allowed_files = set()
                self.remembered = False

            def session_snapshot_with_revision(self):
                return app_server._clone_jsonish(self.latest_session), 1

            def session_epoch(self):
                return "epoch-new"

            def remember_session_if_current(self, expected, updated, *, expected_revision=None):
                self.remembered = True
                return 2

        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = RestartedServer()
        handler._read_json_body = lambda: {
            "action": "confirm",
            "problemIds": ["p1"],
            "expectedSessionRevision": 1,
            "expectedSessionEpoch": "epoch-old",
        }
        responses = []
        handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

        handler._handle_session_mutate()

        payload, kwargs = responses[0]
        self.assertEqual("session_conflict", payload["code"])
        self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs["status"])
        self.assertFalse(handler.server.remembered)

    def test_runtime_artifact_cleanup_protects_active_and_history_files(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            upload_dir = runtime_dir / "uploads"
            output_dir = runtime_dir / "outputs" / "active"
            export_dir = runtime_dir / "exports"
            upload_dir.mkdir()
            output_dir.mkdir(parents=True)
            export_dir.mkdir()
            protected_upload = upload_dir / "source.pdf"
            protected_output = output_dir / "board.edb"
            protected_history = export_dir / "history.zip"
            orphan = export_dir / "orphan.zip"
            for path in (protected_upload, protected_output, protected_history, orphan):
                path.write_bytes(b"artifact")
                os.utime(path, (1, 1))
            active_session = {
                "input_files": [str(protected_upload)],
                "output_dir": str(output_dir),
                "problems": [],
            }
            history = [{"session": {"problems": [], "edb_path": str(protected_history)}}]

            preview = app_server.cleanup_runtime_artifacts(
                runtime_dir=runtime_dir,
                active_session=active_session,
                history=history,
                dry_run=True,
                min_age_seconds=0,
                now=100,
            )

            self.assertEqual([str(orphan.resolve())], preview["selectedPaths"])
            self.assertEqual(0, preview["deletedFileCount"])
            self.assertTrue(orphan.exists())

            applied = app_server.cleanup_runtime_artifacts(
                runtime_dir=runtime_dir,
                active_session=active_session,
                history=history,
                dry_run=False,
                min_age_seconds=0,
                now=100,
            )

            self.assertEqual(1, applied["deletedFileCount"])
            self.assertFalse(orphan.exists())
            self.assertTrue(protected_upload.exists())
            self.assertTrue(protected_output.exists())
            self.assertTrue(protected_history.exists())

    def test_runtime_artifact_capacity_only_selects_bytes_above_limit(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            export_dir = runtime_dir / "exports"
            export_dir.mkdir()
            oldest = export_dir / "oldest.zip"
            newer = export_dir / "newer.zip"
            oldest.write_bytes(b"a" * 6)
            newer.write_bytes(b"b" * 6)
            os.utime(oldest, (1, 1))
            os.utime(newer, (2, 2))

            result = app_server.cleanup_runtime_artifacts(
                runtime_dir=runtime_dir,
                active_session={},
                history=[],
                dry_run=True,
                min_age_seconds=0,
                max_bytes=8,
                now=100,
            )

            self.assertEqual([str(oldest.resolve())], result["selectedPaths"])
            self.assertEqual(6, result["selectedBytes"])

    def test_runtime_artifact_cleanup_pauses_while_job_is_writing(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            output_dir = runtime_dir / "outputs" / "active-job"
            output_dir.mkdir(parents=True)
            in_progress = output_dir / "render.png"
            in_progress.write_bytes(b"partial-render")
            os.utime(in_progress, (1, 1))
            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "LATEST_SESSION_JSON", runtime_dir / "latest_session.json"),
                patch.object(app_server, "SESSION_HISTORY_JSON", runtime_dir / "session_history.json"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    server.begin_artifact_job()
                    with self.assertRaises(app_server.ArtifactCleanupBusy):
                        server.cleanup_artifacts(dry_run=False, min_age_seconds=0)
                    with self.assertRaises(app_server.ArtifactCleanupBusy):
                        server.clear_session(
                            cleanup_artifacts=True,
                            dry_run=False,
                            min_age_seconds=0,
                        )
                    self.assertTrue(in_progress.exists())
                    server.end_artifact_job()

                    result = server.cleanup_artifacts(dry_run=False, min_age_seconds=0)
                    self.assertEqual(1, result["deletedFileCount"])
                    self.assertFalse(in_progress.exists())
                finally:
                    server.server_close()

    def test_artifact_jobs_serialize_before_stale_revision_can_write(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            artifact_path = runtime_dir / "outputs" / "shared" / "lesson.edb"
            artifact_path.parent.mkdir(parents=True)
            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "LATEST_SESSION_JSON", runtime_dir / "latest_session.json"),
                patch.object(app_server, "SESSION_HISTORY_JSON", runtime_dir / "session_history.json"),
                patch.object(app_server, "GENERATED_SESSION_JS", runtime_dir / "generated_session.js"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                server.latest_session = {"problems": [], "artifactMarker": "base"}
                server._session_revision = 1
                expected_revision = server.session_revision()
                first_started = threading.Event()
                release_first = threading.Event()
                results: list[tuple[str, bool]] = []
                writes: list[str] = []

                def publish(marker: str, pause: bool = False) -> None:
                    server.begin_artifact_job()
                    try:
                        base, revision = server.session_snapshot_with_revision()
                        if revision != expected_revision:
                            results.append((marker, False))
                            return
                        if pause:
                            first_started.set()
                            release_first.wait(timeout=5)
                        artifact_path.write_bytes(marker.encode("utf-8"))
                        writes.append(marker)
                        updated = dict(base or {})
                        updated["artifactMarker"] = marker
                        committed = server.remember_session_if_current(
                            base,
                            updated,
                            expected_revision=revision,
                        )
                        results.append((marker, committed is not None))
                    finally:
                        server.end_artifact_job()

                first = threading.Thread(target=publish, args=("winner", True))
                second = threading.Thread(target=publish, args=("stale",))
                try:
                    first.start()
                    self.assertTrue(first_started.wait(timeout=5))
                    second.start()
                    self.assertTrue(second.is_alive())
                    release_first.set()
                    first.join(timeout=5)
                    second.join(timeout=5)

                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                    self.assertEqual(["winner"], writes)
                    self.assertEqual(b"winner", artifact_path.read_bytes())
                    self.assertEqual("winner", server.session_snapshot()["artifactMarker"])
                    self.assertCountEqual([("winner", True), ("stale", False)], results)
                finally:
                    release_first.set()
                    first.join(timeout=5)
                    second.join(timeout=5)
                    server.server_close()

    def test_session_clear_revision_check_is_atomic_with_delete(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    server.remember_session({"problems": [{"id": "p1"}]})
                    stale_revision = server.session_revision()
                    server.remember_session({"problems": [{"id": "p1"}, {"id": "p2"}]})

                    with self.assertRaises(app_server.SessionRevisionConflict):
                        server.clear_session(expected_revision=stale_revision)

                    snapshot, current_revision = server.session_snapshot_with_revision()
                    self.assertEqual(["p1", "p2"], [problem["id"] for problem in snapshot["problems"]])
                    self.assertGreater(current_revision, stale_revision)
                    self.assertTrue(latest_path.exists())
                finally:
                    server.server_close()

    def test_session_clear_rolls_back_all_state_files_when_generated_reset_fails(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            latest_payload = '{"problems": [{"id": "p1"}]}'
            history_payload = "[]"
            generated_payload = "window.EDB_UI_SESSION = { problems: [] };\n"
            latest_path.write_text(latest_payload, encoding="utf-8")
            history_path.write_text(history_payload, encoding="utf-8")
            generated_path.write_text(generated_payload, encoding="utf-8")
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                revision = server.session_revision()
                try:
                    with patch.object(app_server, "_atomic_write_text", side_effect=OSError("disk full")):
                        with self.assertRaises(OSError):
                            server.clear_session(expected_revision=revision)
                    self.assertEqual(latest_payload, latest_path.read_text(encoding="utf-8"))
                    self.assertEqual(history_payload, history_path.read_text(encoding="utf-8"))
                    self.assertEqual(generated_payload, generated_path.read_text(encoding="utf-8"))
                    self.assertEqual(revision, server.session_revision())
                    self.assertEqual("p1", server.session_snapshot()["problems"][0]["id"])
                finally:
                    server.server_close()

    def test_session_clear_rolls_back_when_second_state_file_move_fails(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            latest_path.write_text('{"problems": [{"id": "p1"}]}', encoding="utf-8")
            history_path.write_text("[]", encoding="utf-8")
            generated_path.write_text("window.EDB_UI_SESSION = {};\n", encoding="utf-8")
            real_replace = os.replace
            failed = False

            def fail_history_once(source, target):
                nonlocal failed
                if Path(source) == history_path and not failed:
                    failed = True
                    raise OSError("history locked")
                return real_replace(source, target)

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
                patch.object(app_server.os, "replace", side_effect=fail_history_once),
            ):
                with self.assertRaises(OSError):
                    app_server._atomically_clear_persisted_session_state()

            self.assertTrue(latest_path.exists())
            self.assertTrue(history_path.exists())
            self.assertEqual("window.EDB_UI_SESSION = {};\n", generated_path.read_text(encoding="utf-8"))

    def test_session_clear_rolls_back_when_reset_commit_marker_write_fails(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            latest_payload = '{"problems": [{"id": "p1"}]}'
            history_payload = "[]"
            generated_payload = "window.EDB_UI_SESSION = {};\n"
            latest_path.write_text(latest_payload, encoding="utf-8")
            history_path.write_text(history_payload, encoding="utf-8")
            generated_path.write_text(generated_payload, encoding="utf-8")
            marker = runtime_dir / ".session_reset_transaction.json"
            real_atomic_write = app_server._atomic_write_text

            def fail_committed_marker(path, payload, **kwargs):
                if Path(path) == marker and '"phase": "committed"' in payload:
                    raise OSError("marker disk full")
                return real_atomic_write(path, payload, **kwargs)

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
                patch.object(app_server, "_atomic_write_text", side_effect=fail_committed_marker),
            ):
                with self.assertRaises(OSError):
                    app_server._atomically_clear_persisted_session_state()

            self.assertEqual(latest_payload, latest_path.read_text(encoding="utf-8"))
            self.assertEqual(history_payload, history_path.read_text(encoding="utf-8"))
            self.assertEqual(generated_payload, generated_path.read_text(encoding="utf-8"))
            self.assertFalse(marker.exists())
            self.assertEqual([], list(runtime_dir.glob(".*.reset")))

    def test_reset_recovery_never_overwrites_new_latest_after_rollback_failure(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            old_payload = '{"marker": "old", "problems": []}'
            new_payload = '{"marker": "new", "problems": []}'
            latest_path.write_text(old_payload, encoding="utf-8")
            history_path.write_text("[]", encoding="utf-8")
            generated_path.write_text("window.EDB_UI_SESSION = {};\n", encoding="utf-8")
            real_replace = os.replace

            def fail_move_and_rollback(source, target):
                source_path = Path(source)
                target_path = Path(target)
                if source_path == history_path:
                    raise OSError("history move failed")
                if (
                    source_path.name.startswith(f".{latest_path.name}.")
                    and source_path.name.endswith(".reset")
                    and target_path == latest_path
                ):
                    raise OSError("latest rollback failed")
                return real_replace(source, target)

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
                patch.object(app_server.os, "replace", side_effect=fail_move_and_rollback),
            ):
                with self.assertRaises(OSError):
                    app_server._atomically_clear_persisted_session_state()

            marker = runtime_dir / ".session_reset_transaction.json"
            self.assertTrue(marker.exists())
            self.assertFalse(latest_path.exists())
            latest_path.write_text(new_payload, encoding="utf-8")

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
                patch.object(app_server, "_refresh_session_problem_counts"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    self.assertEqual("new", server.session_snapshot()["marker"])
                finally:
                    server.server_close()

            self.assertEqual(new_payload, latest_path.read_text(encoding="utf-8"))
            self.assertFalse(marker.exists())
            quarantined = list(
                (runtime_dir / ".session-reset-recovery-conflicts").rglob(
                    f".{latest_path.name}.*.reset"
                )
            )
            self.assertEqual(1, len(quarantined))
            self.assertEqual(old_payload, quarantined[0].read_text(encoding="utf-8"))

    def test_new_latest_write_recovers_stale_reset_before_commit(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            old_payload = {"marker": "old", "problems": []}
            new_payload = {"marker": "new", "problems": []}
            latest_path.write_text(json.dumps(old_payload), encoding="utf-8")
            stamp = "stale-before-new-write"
            tombstone = latest_path.with_name(f".{latest_path.name}.{stamp}.reset")
            os.replace(latest_path, tombstone)
            marker = runtime_dir / ".session_reset_transaction.json"
            marker.write_text(json.dumps({
                "version": 1,
                "stamp": stamp,
                "phase": "preparing",
                "generatedExistedBefore": False,
                "entries": [{"target": str(latest_path), "tombstone": str(tombstone)}],
            }), encoding="utf-8")

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                app_server.save_latest_session(new_payload)

            self.assertEqual(new_payload, json.loads(latest_path.read_text(encoding="utf-8")))
            self.assertFalse(marker.exists())
            self.assertFalse(tombstone.exists())

    def test_persist_latest_recovers_old_history_before_merging_new_session(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            old_latest = {"marker": "old", "problems": []}
            old_history = [{
                "id": "old-entry",
                "sessionName": "Old",
                "session": old_latest,
            }]
            latest_path.write_text(json.dumps(old_latest), encoding="utf-8")
            history_path.write_text(json.dumps(old_history), encoding="utf-8")
            generated_path.write_text("window.EDB_UI_SESSION = {};\n", encoding="utf-8")
            stamp = "persist-history-recovery"
            entries = []
            for target in (latest_path, history_path, generated_path):
                tombstone = target.with_name(f".{target.name}.{stamp}.reset")
                os.replace(target, tombstone)
                entries.append({"target": str(target), "tombstone": str(tombstone)})
            marker = runtime_dir / ".session_reset_transaction.json"
            marker.write_text(json.dumps({
                "version": 1,
                "stamp": stamp,
                "phase": "preparing",
                "generatedExistedBefore": True,
                "entries": entries,
            }), encoding="utf-8")
            new_session = {"marker": "new", "session_name": "New", "problems": []}

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                merged, history_error = app_server.persist_latest_session_with_history(new_session)

            self.assertIsNone(history_error)
            self.assertEqual("new", json.loads(latest_path.read_text(encoding="utf-8"))["marker"])
            stored_history = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["id"] for entry in merged], [entry["id"] for entry in stored_history])
            self.assertEqual("new", stored_history[0]["session"]["marker"])
            self.assertIn("old-entry", [entry["id"] for entry in stored_history])
            self.assertFalse(marker.exists())

    def test_remember_history_recovers_old_history_before_merging_new_session(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            old_latest = {"marker": "old", "problems": []}
            old_history = [{
                "id": "old-entry",
                "sessionName": "Old",
                "session": old_latest,
            }]
            latest_path.write_text(json.dumps(old_latest), encoding="utf-8")
            history_path.write_text(json.dumps(old_history), encoding="utf-8")
            stamp = "remember-history-recovery"
            entries = []
            for target in (latest_path, history_path):
                tombstone = target.with_name(f".{target.name}.{stamp}.reset")
                os.replace(target, tombstone)
                entries.append({"target": str(target), "tombstone": str(tombstone)})
            marker = runtime_dir / ".session_reset_transaction.json"
            marker.write_text(json.dumps({
                "version": 1,
                "stamp": stamp,
                "phase": "preparing",
                "generatedExistedBefore": False,
                "entries": entries,
            }), encoding="utf-8")
            new_session = {"marker": "new", "session_name": "New", "problems": []}

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                merged = app_server.remember_session_history(new_session)

            self.assertEqual("old", json.loads(latest_path.read_text(encoding="utf-8"))["marker"])
            stored_history = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["id"] for entry in merged], [entry["id"] for entry in stored_history])
            self.assertEqual("new", stored_history[0]["session"]["marker"])
            self.assertIn("old-entry", [entry["id"] for entry in stored_history])
            self.assertFalse(marker.exists())

    def test_register_current_session_recovers_history_tombstone_before_merge(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            current_session = {"marker": "current", "session_name": "Current", "problems": []}
            old_history = [{
                "id": "old-entry",
                "sessionName": "Old",
                "session": {"marker": "old", "problems": []},
            }]
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    server.latest_session = current_session
                    latest_path.write_text(json.dumps(current_session), encoding="utf-8")
                    history_path.write_text(json.dumps(old_history), encoding="utf-8")
                    stamp = "register-history-recovery"
                    tombstone = history_path.with_name(f".{history_path.name}.{stamp}.reset")
                    os.replace(history_path, tombstone)
                    marker = runtime_dir / ".session_reset_transaction.json"
                    marker.write_text(json.dumps({
                        "version": 1,
                        "stamp": stamp,
                        "phase": "preparing",
                        "generatedExistedBefore": False,
                        "entries": [{"target": str(history_path), "tombstone": str(tombstone)}],
                    }), encoding="utf-8")

                    self.assertTrue(server.register_current_session(current_session))
                finally:
                    server.server_close()

            stored_history = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual("current", stored_history[0]["session"]["marker"])
            self.assertIn("old-entry", [entry["id"] for entry in stored_history])
            self.assertFalse(marker.exists())

    def test_destructive_cleanup_recovers_history_before_selecting_old_artifact(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            export_dir = runtime_dir / "exports"
            export_dir.mkdir(parents=True)
            protected_artifact = export_dir / "old-history-only.edb"
            protected_artifact.write_bytes(b"must survive")
            os.utime(protected_artifact, (1, 1))
            old_history = [{
                "id": "old-entry",
                "sessionName": "Old",
                "session": {"edb_path": str(protected_artifact), "problems": []},
            }]
            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    history_path.write_text(json.dumps(old_history), encoding="utf-8")
                    stamp = "cleanup-history-recovery"
                    tombstone = history_path.with_name(f".{history_path.name}.{stamp}.reset")
                    os.replace(history_path, tombstone)
                    marker = runtime_dir / ".session_reset_transaction.json"
                    marker.write_text(json.dumps({
                        "version": 1,
                        "stamp": stamp,
                        "phase": "preparing",
                        "generatedExistedBefore": False,
                        "entries": [{"target": str(history_path), "tombstone": str(tombstone)}],
                    }), encoding="utf-8")

                    result = server.cleanup_artifacts(
                        dry_run=False,
                        min_age_seconds=0,
                    )
                finally:
                    server.server_close()

            self.assertTrue(protected_artifact.exists())
            self.assertEqual(0, result["deletedFileCount"])
            self.assertEqual(1, result["protectedFileCount"])
            self.assertFalse(marker.exists())

    def test_server_startup_restores_session_reset_interrupted_before_commit(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            latest_payload = '{"problems": [{"id": "p1"}]}'
            history_payload = "[]"
            generated_payload = "window.EDB_UI_SESSION = {};\n"
            latest_path.write_text(latest_payload, encoding="utf-8")
            history_path.write_text(history_payload, encoding="utf-8")
            generated_path.write_text(generated_payload, encoding="utf-8")
            stamp = "interrupted"
            entries = []
            for target in (latest_path, history_path, generated_path):
                tombstone = target.with_name(f".{target.name}.{stamp}.reset")
                os.replace(target, tombstone)
                entries.append({"target": str(target), "tombstone": str(tombstone)})
            marker = runtime_dir / ".session_reset_transaction.json"
            marker.write_text(json.dumps({
                "version": 1,
                "stamp": stamp,
                "phase": "preparing",
                "generatedExistedBefore": True,
                "entries": entries,
            }), encoding="utf-8")

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
                patch.object(app_server, "_refresh_session_problem_counts"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    self.assertEqual("p1", server.session_snapshot()["problems"][0]["id"])
                finally:
                    server.server_close()

            self.assertEqual(latest_payload, latest_path.read_text(encoding="utf-8"))
            self.assertEqual(history_payload, history_path.read_text(encoding="utf-8"))
            self.assertEqual(generated_payload, generated_path.read_text(encoding="utf-8"))
            self.assertFalse(marker.exists())

    def test_server_startup_finishes_session_reset_interrupted_after_commit(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            latest_path.write_text('{"problems": [{"id": "p1"}]}', encoding="utf-8")
            history_path.write_text("[]", encoding="utf-8")
            generated_path.write_text("window.EDB_UI_SESSION = {};\n", encoding="utf-8")
            stamp = "committed"
            entries = []
            for target in (latest_path, history_path, generated_path):
                tombstone = target.with_name(f".{target.name}.{stamp}.reset")
                os.replace(target, tombstone)
                entries.append({"target": str(target), "tombstone": str(tombstone)})
            generated_path.write_text(app_server.EMPTY_GENERATED_SESSION_JS, encoding="utf-8")
            marker = runtime_dir / ".session_reset_transaction.json"
            marker.write_text(json.dumps({
                "version": 1,
                "stamp": stamp,
                "phase": "committed",
                "generatedExistedBefore": True,
                "entries": entries,
            }), encoding="utf-8")

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    self.assertIsNone(server.session_snapshot())
                finally:
                    server.server_close()

            self.assertFalse(latest_path.exists())
            self.assertFalse(history_path.exists())
            self.assertEqual(app_server.EMPTY_GENERATED_SESSION_JS, generated_path.read_text(encoding="utf-8"))
            self.assertFalse(marker.exists())
            self.assertEqual([], list(runtime_dir.glob(".*.reset")))

    def test_session_clear_reports_optional_cleanup_failure_after_successful_reset(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", runtime_dir / "latest.json"),
                patch.object(app_server, "SESSION_HISTORY_JSON", runtime_dir / "history.json"),
                patch.object(app_server, "GENERATED_SESSION_JS", runtime_dir / "generated.js"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                server.latest_session = {"problems": [{"id": "p1"}]}
                server._session_revision = 1
                try:
                    with patch.object(
                        app_server,
                        "cleanup_runtime_artifacts",
                        side_effect=OSError("cleanup locked"),
                    ), patch.object(app_server, "_log_operation_exception"):
                        cleanup, revision = server.clear_session_with_revision(
                            expected_revision=1,
                            cleanup_artifacts=True,
                            dry_run=False,
                        )
                    self.assertFalse(cleanup["ok"])
                    self.assertEqual("artifact_cleanup_failed", cleanup["code"])
                    self.assertIsNone(server.session_snapshot())
                    self.assertEqual(2, revision)
                finally:
                    server.server_close()

    def test_staged_publish_cas_conflict_never_exposes_final_generation(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest_session.json"
            history_path = runtime_dir / "session_history.json"
            generated_path = runtime_dir / "generated_session.js"
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    server.remember_session({"problems": [{"id": "p1"}]})
                    expected, revision = server.session_snapshot_with_revision()
                    server.remember_session({"problems": [{"id": "p1"}], "marker": "newer"})
                    staging_dir = runtime_dir / ".publish-staging" / "generation"
                    final_dir = runtime_dir / "published" / "generation"
                    staging_dir.mkdir(parents=True)
                    (staging_dir / "lesson.edb").write_bytes(b"edb")

                    committed = server.adopt_staged_publish_if_current(
                        expected,
                        {"problems": [{"id": "p1"}], "marker": "stale"},
                        staging_dir=staging_dir,
                        final_dir=final_dir,
                        expected_revision=revision,
                    )

                    self.assertIsNone(committed)
                    self.assertTrue((staging_dir / "lesson.edb").exists())
                    self.assertFalse(final_dir.exists())
                    self.assertEqual("newer", server.session_snapshot()["marker"])
                finally:
                    server.server_close()

    def test_staged_publish_persist_failure_rolls_generation_back(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", runtime_dir / "latest.json"),
                patch.object(app_server, "SESSION_HISTORY_JSON", runtime_dir / "history.json"),
                patch.object(app_server, "GENERATED_SESSION_JS", runtime_dir / "generated.js"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                try:
                    server.latest_session = {"problems": [{"id": "p1"}]}
                    server._session_revision = 1
                    expected, revision = server.session_snapshot_with_revision()
                    staging_dir = runtime_dir / ".publish-staging" / "generation"
                    final_dir = runtime_dir / "published" / "generation"
                    staging_dir.mkdir(parents=True)
                    (staging_dir / "lesson.edb").write_bytes(b"edb")
                    with patch.object(
                        app_server,
                        "persist_latest_session_with_history",
                        side_effect=OSError("disk full"),
                    ):
                        with self.assertRaises(OSError):
                            server.adopt_staged_publish_if_current(
                                expected,
                                {"problems": [{"id": "p1"}], "marker": "publish"},
                                staging_dir=staging_dir,
                                final_dir=final_dir,
                                expected_revision=revision,
                            )
                    self.assertTrue((staging_dir / "lesson.edb").exists())
                    self.assertFalse(final_dir.exists())
                    self.assertNotIn("marker", server.session_snapshot())
                    self.assertEqual(revision, server.session_revision())
                finally:
                    server.server_close()

    def test_export_edb_artifact_job_blocks_reset_until_allowed_files_update_finishes(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", runtime_dir / "latest.json"),
                patch.object(app_server, "SESSION_HISTORY_JSON", runtime_dir / "history.json"),
                patch.object(app_server, "GENERATED_SESSION_JS", runtime_dir / "generated.js"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                handler = object.__new__(app_server.AppRequestHandler)
                handler.server = server
                started = threading.Event()
                release = threading.Event()

                def export_job():
                    started.set()
                    release.wait(timeout=5)
                    server.add_allowed_files({"/tmp/export.zip"})

                worker = threading.Thread(target=lambda: handler._run_artifact_job(export_job))
                try:
                    worker.start()
                    self.assertTrue(started.wait(timeout=5))
                    with self.assertRaises(app_server.ArtifactCleanupBusy):
                        server.clear_session(expected_revision=0)
                    release.set()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
                    self.assertIn("/tmp/export.zip", server.allowed_files)
                    server.clear_session(expected_revision=0)
                    self.assertEqual(set(), server.allowed_files)
                finally:
                    release.set()
                    worker.join(timeout=5)
                    server.server_close()

    def test_restore_route_blocks_reset_and_cleanup_until_commit_finishes(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            with (
                patch.object(app_server, "LATEST_SESSION_JSON", runtime_dir / "latest.json"),
                patch.object(app_server, "SESSION_HISTORY_JSON", runtime_dir / "history.json"),
                patch.object(app_server, "GENERATED_SESSION_JS", runtime_dir / "generated.js"),
            ):
                server = app_server.AppHTTPServer(("127.0.0.1", 0), app_server.AppRequestHandler)
                handler = object.__new__(app_server.AppRequestHandler)
                handler.server = server
                handler.path = "/api/session/restore"
                started = threading.Event()
                release = threading.Event()

                def blocking_restore():
                    started.set()
                    release.wait(timeout=5)

                handler._handle_session_restore = blocking_restore
                worker = threading.Thread(target=handler._dispatch_post)
                try:
                    worker.start()
                    self.assertTrue(started.wait(timeout=5))
                    with self.assertRaises(app_server.ArtifactCleanupBusy):
                        server.clear_session(
                            expected_revision=0,
                            cleanup_artifacts=True,
                            dry_run=False,
                            min_age_seconds=0,
                        )
                    release.set()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
                    server.clear_session(expected_revision=0)
                finally:
                    release.set()
                    worker.join(timeout=5)
                    server.server_close()

    def test_resolve_open_file_target_accepts_runtime_edb(self):
        app_server.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=app_server.RUNTIME_DIR) as raw_tmp:
            edb_path = Path(raw_tmp) / "lesson.edb"
            edb_path.write_bytes(b"edb")

            target = app_server._resolve_open_target(str(edb_path), kind="file")

            self.assertEqual(edb_path.resolve(), target)

    def test_resolve_open_file_target_rejects_outside_allowed_roots(self):
        with TemporaryDirectory() as raw_tmp:
            edb_path = Path(raw_tmp) / "outside.edb"
            edb_path.write_bytes(b"edb")

            with self.assertRaises(ValueError) as ctx:
                app_server._resolve_open_target(str(edb_path), kind="file")

            self.assertIn("outside allowed roots", str(ctx.exception))

    def test_resolve_open_file_target_rejects_folder_for_file_open(self):
        app_server.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=app_server.RUNTIME_DIR) as raw_tmp:
            with self.assertRaises(FileNotFoundError) as ctx:
                app_server._resolve_open_target(raw_tmp, kind="file")

            self.assertIn("file not found", str(ctx.exception))

    def test_remember_session_history_writes_history_file(self):
        with TemporaryDirectory() as raw_tmp:
            history_path = Path(raw_tmp) / "history.json"
            session = {
                "session_name": "과탐 양식",
                "generated_at": "2026-06-13T12:00:00+09:00",
                "output_dir": "/tmp/session-c",
                "problems": [{"id": "p1"}],
            }

            history = app_server.remember_session_history(
                session,
                path=history_path,
                updated_at="2026-06-13T12:00:00+09:00",
            )

            persisted = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(history, persisted)
            self.assertEqual("과탐 양식", persisted[0]["sessionName"])
            self.assertEqual(["p1"], [problem["id"] for problem in persisted[0]["session"]["problems"]])

    def test_session_history_helpers_use_current_default_path(self):
        with TemporaryDirectory() as raw_tmp:
            history_path = Path(raw_tmp) / "patched-history.json"
            session = {
                "session_name": "임시 런타임",
                "generated_at": "2026-06-13T12:00:00+09:00",
                "output_dir": "/tmp/session-patched",
                "problems": [{"id": "p1"}],
            }

            with patch.object(app_server, "SESSION_HISTORY_JSON", history_path):
                history = app_server.remember_session_history(
                    session,
                    updated_at="2026-06-13T12:00:00+09:00",
                )
                loaded = app_server.load_session_history()

            self.assertTrue(history_path.exists())
            self.assertEqual(history, loaded)
            self.assertEqual("임시 런타임", loaded[0]["sessionName"])

    def test_latest_session_registers_history_entry(self):
        with TemporaryDirectory() as raw_tmp:
            latest_path = Path(raw_tmp) / "latest.json"
            session = {
                "session_name": "최근 작업",
                "generated_at": "2026-06-13T12:00:00+09:00",
                "output_dir": "/tmp/session-d",
                "problems": [{"id": "p1"}],
            }
            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {
                "latest_session": session,
                "allowed_files": set(),
            })()
            responses = []
            handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "remember_session_history") as mock_history,
            ):
                handler._handle_latest_session()

            mock_history.assert_called_once_with(session)
            self.assertTrue(responses[0][0]["ok"])

    def test_latest_session_empty_state_returns_ok_null(self):
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = type("FakeServer", (), {
            "latest_session": None,
            "allowed_files": set(),
        })()
        responses = []
        handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

        with (
            patch.object(app_server, "load_latest_session", return_value=None),
            patch.object(app_server, "remember_session_history") as mock_history,
        ):
            handler._handle_latest_session()

        mock_history.assert_not_called()
        self.assertEqual([({"ok": True, "session": None}, {})], responses)

    def test_session_clear_removes_latest_session_and_history_files(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            latest_path = tmpdir / "latest.json"
            history_path = tmpdir / "history.json"
            generated_path = tmpdir / "generated_session.js"
            latest_path.write_text('{"problems": [{"id": "p1"}]}', encoding="utf-8")
            history_path.write_text('[{"id": "old"}]', encoding="utf-8")
            generated_path.write_text("window.EDB_UI_SESSION = { problems: [] };\n", encoding="utf-8")

            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {
                "latest_session": {"problems": [{"id": "p1"}]},
                "allowed_files": {"some-file"},
            })()
            responses = []
            handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

            with (
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                handler._handle_session_clear()

            self.assertFalse(latest_path.exists())
            self.assertFalse(history_path.exists())
            self.assertEqual("window.EDB_UI_SESSION = null;\n", generated_path.read_text(encoding="utf-8"))
            self.assertIsNone(handler.server.latest_session)
            self.assertEqual(set(), handler.server.allowed_files)
            self.assertEqual(
                {
                    "ok": True,
                    "history": [],
                    "session": None,
                    "artifactCleanupRequested": False,
                    "artifactCleanupDryRun": None,
                    "artifactCleanupPerformed": False,
                    "artifactCleanupSucceeded": None,
                },
                responses[0][0],
            )

    def test_session_clear_can_explicitly_delete_old_artifacts(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp)
            latest_path = runtime_dir / "latest.json"
            history_path = runtime_dir / "history.json"
            generated_path = runtime_dir / "generated_session.js"
            export_dir = runtime_dir / "exports"
            export_dir.mkdir()
            artifact = export_dir / "old.zip"
            artifact.write_bytes(b"old")
            os.utime(artifact, (1, 1))
            latest_path.write_text('{"problems": []}', encoding="utf-8")
            history_path.write_text("[]", encoding="utf-8")

            handler = object.__new__(app_server.AppRequestHandler)
            handler.server = type("FakeServer", (), {
                "latest_session": {"problems": []},
                "allowed_files": set(),
            })()
            responses = []
            handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "LATEST_SESSION_JSON", latest_path),
                patch.object(app_server, "SESSION_HISTORY_JSON", history_path),
                patch.object(app_server, "GENERATED_SESSION_JS", generated_path),
            ):
                handler._handle_session_clear(SimpleNamespace(
                    query="cleanupArtifacts=true&dryRun=false&minAgeDays=0"
                ))

            self.assertFalse(artifact.exists())
            cleanup = responses[0][0]["artifactCleanup"]
            self.assertFalse(cleanup["dryRun"])
            self.assertEqual(1, cleanup["deletedFileCount"])
            self.assertTrue(responses[0][0]["artifactCleanupRequested"])
            self.assertTrue(responses[0][0]["artifactCleanupPerformed"])
            self.assertTrue(responses[0][0]["artifactCleanupSucceeded"])

    def test_runtime_artifact_cleanup_rejects_cross_origin_requests(self):
        handler = object.__new__(app_server.AppRequestHandler)
        handler.headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "https://example.test",
        }
        responses = []
        handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

        handler._handle_runtime_artifact_cleanup()

        payload, kwargs = responses[0]
        self.assertEqual("cross-origin request rejected", payload["error"])
        self.assertEqual(app_server.HTTPStatus.FORBIDDEN, kwargs["status"])

    def test_shutdown_endpoint_sends_ok_and_stops_server(self):
        shutdown_called = threading.Event()
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = type("FakeServer", (), {
            "shutdown": lambda _self: shutdown_called.set(),
        })()
        handler.headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
        }
        responses = []
        handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

        handler._handle_shutdown()

        self.assertEqual([({"ok": True}, {})], responses)
        self.assertTrue(shutdown_called.wait(1.0))


class TestClassInManualReview(unittest.TestCase):
    def test_apply_classin_review_result_marks_current_publish_summary_passed(self):
        session = {
            "publish_summary": {
                "edbFileName": "lesson.edb",
                "edbPath": "/tmp/lesson.edb",
            },
            "publishSummary": {
                "edbFileName": "lesson.edb",
                "edbPath": "/tmp/lesson.edb",
            },
            "publish_history": [
                {"edbFileName": "lesson.edb", "edbPath": "/tmp/lesson.edb"},
                {"edbFileName": "old.edb", "edbPath": "/tmp/old.edb"},
            ],
        }

        review = app_server._apply_classin_review_result(
            session,
            {"status": "passed", "notes": "ClassIn에서 정상 확인"},
            reviewed_at="2026-06-14T00:30:00+09:00",
        )

        self.assertEqual("passed", review["status"])
        self.assertEqual("ClassIn 확인 완료", review["statusLabel"])
        self.assertTrue(review["classinOpened"])
        self.assertTrue(review["recordCountOk"])
        self.assertTrue(review["orderOk"])
        self.assertTrue(review["readabilityOk"])
        self.assertEqual("ClassIn에서 정상 확인", review["notes"])
        self.assertEqual("passed", session["classinReview"]["status"])
        self.assertEqual("passed", session["publishSummary"]["classinReviewStatus"])
        self.assertTrue(session["publishSummary"]["classinReviewPassed"])
        self.assertEqual("ClassIn 확인 완료", session["publish_summary"]["classin_review_status_label"])
        self.assertEqual("passed", session["publish_history"][0]["classinReviewStatus"])
        self.assertNotIn("classinReviewStatus", session["publish_history"][1])

    def test_classin_review_handler_returns_review_alias(self):
        session = {
            "session_name": "수업",
            "publishSummary": {
                "edbFileName": "lesson.edb",
                "edbPath": "/tmp/lesson.edb",
            },
        }
        remembered = []
        fake_server = type("FakeServer", (), {
            "latest_session": session,
            "allowed_files": set(),
            "remember_session": lambda self, next_session: remembered.append(next_session),
        })()
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = fake_server
        handler._read_json_body = lambda: {"status": "passed"}
        responses = []
        handler._send_json = lambda payload, **kwargs: responses.append((payload, kwargs))

        with patch.object(app_server, "load_session_history", return_value=[]):
            handler._handle_session_classin_review()

        self.assertEqual(1, len(remembered))
        body = responses[0][0]
        self.assertTrue(body["ok"])
        self.assertEqual("passed", body["review"]["status"])
        self.assertEqual(body["classinReview"], body["review"])
        self.assertEqual("passed", body["session"]["publishSummary"]["classinReviewStatus"])


class TestReviewSummary(unittest.TestCase):
    def test_session_review_summary_collects_hwp_text_qa(self):
        session = {
            "warning_messages": ["감지된 문항 수 확인 필요"],
            "problems": [
                {"id": "p1", "riskFlags": []},
                {"id": "p2", "riskFlags": []},
                {"id": "doc-1", "riskFlags": ["marker_document_continuation"]},
            ],
            "pages": [
                {
                    "id": "page-1",
                    "metadata": {
                        "hwp_conversion_quality": {
                            "hwp_text_extractor": "hwp5txt",
                            "hwp_text_numbered_problem_count": 45,
                            "hwp_text_stem_problem_count": 0,
                        }
                    },
                },
                {
                    "id": "page-2",
                    "metadata": {
                        "hwp_conversion_quality": {
                            "hwp_text_extractor": "rhwp",
                            "hwp_text_numbered_problem_count": 45,
                            "hwp_text_stem_problem_count": 3,
                        }
                    },
                },
                {
                    "id": "page-3",
                    "metadata": {
                        "hwp_conversion_quality": {
                            "hwp_text_extractor": "rhwp",
                            "hwp_text_numbered_problem_count": 45,
                            "hwp_text_stem_problem_count": 3,
                        }
                    },
                },
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual(3, summary["detectedProblemCount"])
        self.assertEqual(2, summary["coreProblemCount"])
        self.assertEqual(1, summary["supplementalItemCount"])
        self.assertEqual(2, summary["warningCount"])
        self.assertEqual({"hwp5txt": 1, "rhwp": 2}, summary["hwpTextExtractors"])
        self.assertEqual(45, summary["hwpTextProblemSignalCount"])
        self.assertEqual("mismatch", summary["hwpTextProblemCountStatus"])

    def test_session_review_summary_collects_review_status_and_risk_flags(self):
        session = {
            "problems": [
                {
                    "id": "p1",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": [],
                },
                {
                    "id": "p2",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": ["fallback_grouping", "ocr_disabled"],
                },
                {
                    "id": "p3",
                    "bbox": {"width": 0, "height": 0},
                    "riskFlags": ["ai_retry_missing_source"],
                },
                {
                    "id": "doc-1",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": ["marker_document_continuation", "fallback_grouping"],
                },
            ],
            "pages": [
                {
                    "id": "page-1",
                    "riskFlags": ["large_block_dominance", "ocr_disabled"],
                    "problemIds": ["p1", "p2"],
                }
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual(
            {"all": 4, "normal": 1, "check_needed": 2, "failed": 1},
            summary["reviewStatusCounts"],
        )
        self.assertEqual(3, summary["needsReviewCount"])
        self.assertEqual(
            {
                "ai_retry_missing_source": 1,
                "fallback_grouping": 2,
                "large_block_dominance": 1,
                "marker_document_continuation": 1,
                "ocr_disabled": 2,
            },
            summary["riskFlagCounts"],
        )
        self.assertEqual(
            [
                {"flag": "fallback_grouping", "count": 2},
                {"flag": "ocr_disabled", "count": 2},
                {"flag": "ai_retry_missing_source", "count": 1},
            ],
            summary["topRiskFlags"],
        )
        self.assertEqual(
            {
                "ai_retry_missing_source": 1,
                "fallback_grouping": 2,
                "large_block_dominance": 1,
            },
            summary["actionableRiskFlagCounts"],
        )
        self.assertEqual(
            [
                {"flag": "fallback_grouping", "count": 2},
                {"flag": "ai_retry_missing_source", "count": 1},
                {"flag": "large_block_dominance", "count": 1},
            ],
            summary["topActionableRiskFlags"],
        )
        self.assertEqual({"all": 1, "normal": 0, "check_needed": 1, "failed": 0}, summary["supplementalReviewStatusCounts"])
        self.assertEqual({"all": 3, "normal": 1, "check_needed": 1, "failed": 1}, summary["coreReviewStatusCounts"])

    def test_session_review_summary_demotes_fallback_grouping_when_hwp_counts_match(self):
        session = {
            "problems": [
                {
                    "id": "p1",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": [
                        "fallback_grouping",
                        "large_block_dominance",
                        "ocr_disabled",
                        "problem_per_block",
                    ],
                },
                {
                    "id": "p2",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": [
                        "fallback_grouping",
                        "large_block_dominance",
                        "ocr_disabled",
                        "problem_per_block",
                    ],
                },
            ],
            "pages": [
                {
                    "id": "page-1",
                    "problemIds": ["p1", "p2"],
                    "riskFlags": ["sparse_segmentation", "no_problem_markers"],
                    "metadata": {
                        "hwp_conversion_quality": {
                            "hwp_text_extractor": "rhwp-markdown",
                            "hwp_text_numbered_problem_count": 2,
                            "hwp_layout_extractor": "rhwp-render-tree",
                            "hwp_layout_problem_marker_count": 2,
                        }
                    },
                }
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual(
            {
                "fallback_grouping": 2,
                "large_block_dominance": 2,
                "no_problem_markers": 1,
                "ocr_disabled": 2,
                "problem_per_block": 2,
                "sparse_segmentation": 1,
            },
            summary["riskFlagCounts"],
        )
        self.assertTrue(summary["hwpTextProblemCountMatches"])
        self.assertTrue(summary["hwpLayoutProblemCountMatches"])
        self.assertEqual(2, summary["needsReviewCount"])
        self.assertEqual(0, summary["actionableNeedsReviewCount"])
        self.assertEqual({}, summary["actionableRiskFlagCounts"])
        self.assertEqual([], summary["topActionableRiskFlags"])

    def test_session_review_summary_keeps_sparse_marker_risks_actionable_without_hwp_count_match(self):
        session = {
            "problems": [
                {
                    "id": "p1",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": [],
                },
            ],
            "pages": [
                {
                    "id": "page-1",
                    "problemIds": ["p1"],
                    "riskFlags": ["sparse_segmentation", "no_problem_markers"],
                }
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertFalse(summary["hwpTextProblemCountMatches"])
        self.assertFalse(summary["hwpLayoutProblemCountMatches"])
        self.assertEqual(
            {"no_problem_markers": 1, "sparse_segmentation": 1},
            summary["actionableRiskFlagCounts"],
        )
        self.assertEqual(1, summary["actionableNeedsReviewCount"])

    def test_session_review_summary_counts_hwp_segmentation_risks(self):
        session = {
            "problems": [
                {
                    "id": "p1",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": [],
                },
                {
                    "id": "p2",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": [],
                },
            ],
            "pages": [
                {
                    "id": "page-1",
                    "problemIds": ["p1", "p2"],
                    "riskFlags": ["hwp_problem_count_mismatch", "hwp_oversegmentation"],
                },
                {
                    "id": "page-2",
                    "riskFlags": ["hwp_problem_count_mismatch"],
                },
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual(2, summary["hwpProblemCountMismatchCount"])
        self.assertEqual(1, summary["hwpOversegmentationCount"])
        self.assertEqual(3, summary["actionableNeedsReviewCount"])
        self.assertEqual(
            {
                "hwp_problem_count_mismatch": 2,
                "hwp_oversegmentation": 1,
            },
            summary["actionableRiskFlagCounts"],
        )

    def test_session_review_summary_keeps_cross_page_passage_checks_actionable(self):
        session = {
            "problems": [
                {
                    "id": "p15",
                    "bbox": {"width": 120, "height": 80},
                    "riskFlags": ["passage_cross_page_merge_check"],
                    "passageGroupId": "page-1-passage-13-16",
                    "passageContinuesAcrossPages": True,
                },
            ],
            "pages": [],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual(
            {"passage_cross_page_merge_check": 1},
            summary["actionableRiskFlagCounts"],
        )
        self.assertEqual(1, summary["actionableNeedsReviewCount"])

    def test_refresh_session_counts_removes_resolved_passage_review_queue(self):
        session = {
            "passageReviewItems": [
                {
                    "groupId": "hwp-text-passage-31-32",
                    "numberLabel": "31-32",
                    "problemIds": ["p31", "p32"],
                    "sourcePageIds": ["page-5", "page-6"],
                    "problemCount": 2,
                    "continuesAcrossPages": True,
                }
            ],
            "passage_review_items": [
                {
                    "group_id": "hwp-text-passage-31-32",
                    "number_label": "31-32",
                    "problem_ids": ["p31", "p32"],
                    "source_page_ids": ["page-5", "page-6"],
                    "problem_count": 2,
                    "continues_across_pages": True,
                }
            ],
            "passageReviewItemCount": 1,
            "passage_review_item_count": 1,
            "crossPagePassageReviewItemCount": 1,
            "cross_page_passage_review_item_count": 1,
            "problems": [
                {
                    "id": "p31",
                    "bbox": {"width": 120, "height": 80},
                    "reviewStatus": "normal",
                    "riskFlags": [],
                },
                {
                    "id": "p32",
                    "bbox": {"width": 120, "height": 80},
                    "reviewStatus": "normal",
                    "riskFlags": [],
                },
            ],
            "pages": [
                {
                    "id": "page-5",
                    "problemIds": ["p31"],
                    "riskFlags": [],
                },
                {
                    "id": "page-6",
                    "problemIds": ["p32"],
                    "riskFlags": [],
                },
            ],
        }

        app_server._refresh_session_problem_counts(session)

        self.assertEqual([], session["passageReviewItems"])
        self.assertEqual([], session["passage_review_items"])
        self.assertEqual(0, session["passageReviewItemCount"])
        self.assertEqual(0, session["passage_review_item_count"])
        self.assertEqual(0, session["crossPagePassageReviewItemCount"])
        self.assertEqual(0, session["cross_page_passage_review_item_count"])

    def test_refresh_session_counts_keeps_check_needed_passage_review_queue_without_flags(self):
        session = {
            "passageReviewItems": [
                {
                    "groupId": "hwp-text-passage-31-32",
                    "numberLabel": "31-32",
                    "problemIds": ["p31", "p32"],
                    "sourcePageIds": ["page-5", "page-6"],
                    "problemCount": 2,
                    "continuesAcrossPages": True,
                }
            ],
            "passageReviewItemCount": 1,
            "crossPagePassageReviewItemCount": 1,
            "problems": [
                {
                    "id": "p31",
                    "bbox": {"width": 120, "height": 80},
                    "reviewStatus": "check_needed",
                    "riskFlags": [],
                },
                {
                    "id": "p32",
                    "bbox": {"width": 120, "height": 80},
                    "reviewStatus": "normal",
                    "riskFlags": [],
                },
            ],
            "pages": [
                {
                    "id": "page-5",
                    "problemIds": ["p31"],
                    "riskFlags": [],
                },
                {
                    "id": "page-6",
                    "problemIds": ["p32"],
                    "riskFlags": [],
                },
            ],
        }

        app_server._refresh_session_problem_counts(session)

        self.assertEqual(1, len(session["passageReviewItems"]))
        self.assertEqual("31-32", session["passageReviewItems"][0]["numberLabel"])
        self.assertEqual(1, session["passageReviewItemCount"])
        self.assertEqual(1, session["crossPagePassageReviewItemCount"])

    def test_refresh_session_counts_removes_count_only_passage_review_queue(self):
        session = {
            "passageReviewItemCount": 1,
            "passage_review_item_count": 1,
            "crossPagePassageReviewItemCount": 1,
            "cross_page_passage_review_item_count": 1,
            "problems": [
                {
                    "id": "p31",
                    "bbox": {"width": 120, "height": 80},
                    "reviewStatus": "normal",
                    "riskFlags": [],
                },
            ],
            "pages": [
                {
                    "id": "page-5",
                    "problemIds": ["p31"],
                    "riskFlags": [],
                },
            ],
        }

        app_server._refresh_session_problem_counts(session)

        self.assertEqual([], session["passageReviewItems"])
        self.assertEqual([], session["passage_review_items"])
        self.assertEqual(0, session["passageReviewItemCount"])
        self.assertEqual(0, session["passage_review_item_count"])
        self.assertEqual(0, session["crossPagePassageReviewItemCount"])
        self.assertEqual(0, session["cross_page_passage_review_item_count"])

    def test_session_review_summary_reads_hwp_text_qa_from_pages_json_path(self):
        with TemporaryDirectory() as raw_tmp:
            tmpdir = Path(raw_tmp)
            pages_json_path = tmpdir / "pages.json"
            pages_json_path.write_text(
                json.dumps(
                    [
                        {
                            "page_id": "page-1",
                            "metadata": {
                                "hwp_conversion_quality": {
                                    "hwp_text_extractor": "rhwp",
                                    "hwp_text_numbered_problem_count": 45,
                                    "hwp_text_stem_problem_count": 0,
                                }
                            },
                        },
                        {
                            "page_id": "page-2",
                            "metadata": {
                                "hwp_conversion_quality": {
                                    "hwp_text_extractor": "rhwp",
                                    "hwp_text_numbered_problem_count": 45,
                                    "hwp_text_stem_problem_count": 0,
                                }
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )
            session = {
                "pages_json_path": str(pages_json_path),
                "problems": [{"id": "p1"}, {"id": "doc-1", "riskFlags": ["marker_document_continuation"]}],
                "pages": [{"id": "page-1"}, {"id": "page-2"}],
            }

            summary = app_server._session_review_summary(session)

        self.assertEqual({"rhwp": 2}, summary["hwpTextExtractors"])
        self.assertEqual(45, summary["hwpTextProblemSignalCount"])

    def test_session_review_summary_flags_hwp_text_count_mismatch(self):
        session = {
            "problems": [
                {"id": "p1"},
                {"id": "p2"},
                {"id": "p3"},
                {"id": "doc-1", "riskFlags": ["marker_document_continuation"]},
            ],
            "pages": [
                {
                    "id": "page-1",
                    "metadata": {
                        "hwp_conversion_quality": {
                            "hwp_text_extractor": "rhwp",
                            "hwp_text_numbered_problem_count": 5,
                            "hwp_text_stem_problem_count": 0,
                        }
                    },
                },
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual("mismatch", summary["hwpTextProblemCountStatus"])
        self.assertFalse(summary["hwpTextProblemCountMatches"])
        self.assertEqual(-2, summary["hwpTextProblemDelta"])
        self.assertEqual(1, summary["warningCount"])
        self.assertIn("누락 문항", summary["hwpTextProblemCountMessage"])
        self.assertIn(summary["hwpTextProblemCountMessage"], summary["warningMessages"])

    def test_session_review_summary_collects_hwp_layout_qa(self):
        session = {
            "problems": [{"id": "p1"}, {"id": "p2"}],
            "pages": [
                {
                    "id": "page-1",
                    "metadata": {
                        "hwp_conversion_quality": {
                            "hwp_layout_extractor": "rhwp-render-tree",
                            "hwp_layout_page_count": 2,
                            "hwp_layout_problem_marker_count": 2,
                            "hwp_layout_text_line_count": 12,
                        }
                    },
                },
                {
                    "id": "page-2",
                    "metadata": {
                        "hwp_conversion_quality": {
                            "hwp_layout_extractor": "rhwp-render-tree",
                            "hwp_layout_page_count": 2,
                            "hwp_layout_problem_marker_count": 2,
                            "hwp_layout_text_line_count": 12,
                        }
                    },
                },
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual({"rhwp-render-tree": 2}, summary.get("hwpLayoutExtractors"))
        self.assertEqual(2, summary.get("hwpLayoutProblemSignalCount"))
        self.assertEqual(2, summary.get("hwpLayoutPageCount"))
        self.assertEqual(12, summary.get("hwpLayoutTextLineCount"))
        self.assertEqual("match", summary.get("hwpLayoutProblemCountStatus"))
        self.assertTrue(summary.get("hwpLayoutProblemCountMatches"))

    def test_session_review_summary_adjusts_hwp_layout_count_by_duplicate_marker_skips(self):
        session = {
            "problems": [{"id": f"p{index}"} for index in range(1, 21)],
            "pages": [
                {
                    "id": "page-1",
                    "metadata": {
                        "duplicate_problem_numbers_skipped": list(range(21, 32)),
                        "hwp_conversion_quality": {
                            "hwp_text_extractor": "rhwp-markdown",
                            "hwp_text_numbered_problem_count": 20,
                            "hwp_layout_extractor": "rhwp-render-tree",
                            "hwp_layout_problem_marker_count": 31,
                        },
                    },
                }
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual(20, summary.get("hwpLayoutProblemSignalCount"))
        self.assertEqual("match", summary.get("hwpLayoutProblemCountStatus"))
        self.assertTrue(summary.get("hwpLayoutProblemCountMatches"))
        self.assertEqual(0, summary.get("hwpLayoutProblemDelta"))

    def test_session_review_summary_collects_hwp_cache_hits(self):
        session = {
            "problems": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}],
            "pages": [
                {
                    "id": "page-1",
                    "metadata": {
                        "hwp_renderer_cache_hit": True,
                        "hwp_normalized_cache_hit": True,
                    },
                },
                {
                    "id": "page-2",
                    "metadata": {
                        "hwp_renderer_cache_hit": True,
                    },
                },
                {
                    "id": "page-3",
                    "metadata": {},
                },
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual(2, summary["hwpCacheHitPageCount"])
        self.assertEqual(2, summary["hwpRendererCacheHitCount"])
        self.assertEqual(1, summary["hwpNormalizedCacheHitCount"])

    def test_session_review_summary_treats_layout_mismatch_as_advisory_when_text_matches(self):
        session = {
            "problems": [{"id": "p1"}, {"id": "p2"}],
            "pages": [
                {
                    "id": "page-1",
                    "metadata": {
                        "hwp_conversion_quality": {
                            "hwp_text_extractor": "rhwp-markdown",
                            "hwp_text_numbered_problem_count": 2,
                            "hwp_layout_extractor": "rhwp-render-tree",
                            "hwp_layout_problem_marker_count": 3,
                        }
                    },
                },
            ],
        }

        summary = app_server._session_review_summary(session)

        self.assertEqual("match", summary.get("hwpTextProblemCountStatus"))
        self.assertEqual("mismatch", summary.get("hwpLayoutProblemCountStatus"))
        self.assertEqual(0, summary.get("warningCount"))
        self.assertEqual([], summary.get("warningMessages"))


if __name__ == "__main__":
    unittest.main()
