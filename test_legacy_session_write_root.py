import errno
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

import app_server
from image_reconstruction_backend import ImageReconstructionResult


class TestLegacySessionWriteRoot(unittest.TestCase):
    @staticmethod
    def _editable_session(root: Path, output_dir: Path) -> dict:
        source_path = root / "page.png"
        Image.new("RGB", (160, 200), "white").save(source_path)
        return {
            "output_dir": str(output_dir),
            "outputDir": str(output_dir),
            "pages": [{
                "id": "page-1",
                "problemIds": ["p1"],
                "sourceImagePath": source_path.resolve().as_uri(),
            }],
            "problems": [{
                "id": "p1",
                "title": "1.",
                "sourcePageId": "page-1",
                "sourceImagePath": source_path.resolve().as_uri(),
                "imagePath": source_path.resolve().as_uri(),
                "boardRenderPath": source_path.resolve().as_uri(),
                "bbox": {"left": 10, "top": 10, "width": 120, "height": 160},
                "riskFlags": [],
            }],
        }

    def test_managed_long_root_uses_stable_recovery_without_moving_source(self):
        with TemporaryDirectory() as raw_tmp:
            runtime_dir = Path(raw_tmp) / ".app_runtime"
            legacy_root = runtime_dir / "outputs" / ("legacy_" + "가" * 180)
            session = {
                "output_dir": str(legacy_root),
                "generated_at": "2026-08-24T12:00:00+09:00",
                "session_name": "긴 구버전 작업",
                "problems": [],
            }
            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                first, first_recovered = app_server._select_session_write_root(
                    session,
                    operation="session_publish",
                    reserved_descendants=app_server.MANAGED_OUTPUT_RESERVED_DESCENDANTS,
                )
                second, second_recovered = app_server._select_session_write_root(
                    session,
                    operation="session_publish",
                    reserved_descendants=app_server.MANAGED_OUTPUT_RESERVED_DESCENDANTS,
                )

            self.assertTrue(first_recovered)
            self.assertTrue(second_recovered)
            self.assertEqual(first, second)
            self.assertEqual("recovered", first.parent.name)
            self.assertTrue(first.name.startswith("session_"))
            self.assertFalse(first.exists())
            self.assertFalse(legacy_root.exists())

    def test_custom_safe_file_uri_is_preserved(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime_dir = root / "runtime"
            custom = root / "custom-output"
            session = {"outputDir": custom.resolve().as_uri(), "problems": []}

            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                selected, recovered = app_server._select_session_write_root(
                    session,
                    operation="session_publish",
                    reserved_descendants=app_server.MANAGED_OUTPUT_RESERVED_DESCENDANTS,
                )

            self.assertEqual(custom.resolve(), selected)
            self.assertFalse(recovered)

    def test_custom_long_root_raises_typed_error_instead_of_redirecting(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime_dir = root / "runtime"
            custom = root / ("custom_" + "x" * 230)
            session = {"outputDir": str(custom), "problems": []}

            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                with self.assertRaises(app_server.SessionWritePathTooLong) as raised:
                    app_server._select_session_write_root(
                        session,
                        operation="session_publish",
                        reserved_descendants=app_server.MANAGED_OUTPUT_RESERVED_DESCENDANTS,
                    )

            self.assertEqual("session_publish", raised.exception.operation)
            self.assertEqual(custom.resolve(), raised.exception.path.resolve())
            self.assertFalse((runtime_dir / "outputs" / "recovered").exists())

    def test_split_reads_old_source_and_writes_only_to_recovered_root(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime_dir = root / ".app_runtime"
            legacy_root = runtime_dir / "outputs" / ("legacy_" + "가" * 180)
            session = self._editable_session(root, legacy_root)
            original_source = session["pages"][0]["sourceImagePath"]

            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                updated = app_server._mutate_split(session, "p1", 0.5)

            recovered_root = Path(updated["output_dir"])
            self.assertEqual(recovered_root, Path(updated["outputDir"]))
            self.assertEqual("recovered", recovered_root.parent.name)
            self.assertEqual(original_source, updated["pages"][0]["sourceImagePath"])
            self.assertFalse(legacy_root.exists())
            self.assertEqual(2, len(updated["problems"]))
            for problem in updated["problems"]:
                crop_path = app_server.decode_file_reference(problem["imagePath"])
                self.assertIsNotNone(crop_path)
                self.assertTrue(crop_path.is_file())
                self.assertTrue(app_server._path_is_within(crop_path, recovered_root))

    def test_split_rejects_custom_long_root_without_writing(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime_dir = root / "runtime"
            custom = root / ("custom_" + "x" * 230)
            session = self._editable_session(root, custom)

            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                with self.assertRaises(app_server.SessionWritePathTooLong) as raised:
                    app_server._mutate_split(session, "p1", 0.5)

            self.assertEqual("session_mutate", raised.exception.operation)
            self.assertEqual(str(custom), session["output_dir"])
            self.assertFalse(custom.exists())

    def test_manual_crop_writes_to_recovered_root(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime_dir = root / ".app_runtime"
            legacy_root = runtime_dir / "outputs" / ("legacy_" + "다" * 180)
            session = self._editable_session(root, legacy_root)
            original_source = session["pages"][0]["sourceImagePath"]

            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                updated = app_server._mutate_crop(
                    session,
                    "p1",
                    {"topRatio": 0.1, "bottomRatio": 0.1},
                )

            recovered_root = Path(updated["output_dir"])
            crop_path = app_server.decode_file_reference(updated["problems"][0]["imagePath"])
            self.assertEqual("recovered", recovered_root.parent.name)
            self.assertEqual(original_source, updated["pages"][0]["sourceImagePath"])
            self.assertTrue(crop_path.is_file())
            self.assertTrue(app_server._path_is_within(crop_path, recovered_root))
            self.assertFalse(legacy_root.exists())

    def test_merge_writes_to_recovered_root(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime_dir = root / ".app_runtime"
            legacy_root = runtime_dir / "outputs" / ("legacy_" + "라" * 180)
            session = self._editable_session(root, legacy_root)
            second = dict(session["problems"][0])
            second.update({
                "id": "p2",
                "title": "2.",
                "bbox": {"left": 10, "top": 100, "width": 120, "height": 80},
            })
            session["problems"].append(second)
            session["pages"][0]["problemIds"] = ["p1", "p2"]

            with patch.object(app_server, "RUNTIME_DIR", runtime_dir):
                updated = app_server._mutate_merge(session, ["p1", "p2"])

            recovered_root = Path(updated["output_dir"])
            crop_path = app_server.decode_file_reference(updated["problems"][0]["imagePath"])
            self.assertEqual("recovered", recovered_root.parent.name)
            self.assertEqual(1, len(updated["problems"]))
            self.assertTrue(crop_path.is_file())
            self.assertTrue(app_server._path_is_within(crop_path, recovered_root))
            self.assertFalse(legacy_root.exists())

    def test_image_enhancement_adopts_recovered_root_only_after_applied_output(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime_dir = root / ".app_runtime"
            legacy_root = runtime_dir / "outputs" / ("legacy_" + "나" * 180)
            session = self._editable_session(root, legacy_root)
            original_source = session["problems"][0]["imagePath"]

            def fake_upscale(source_path, output_path, **_kwargs):
                output_path = Path(output_path)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (320, 400), (0, 0, 0, 0)).save(output_path)
                return ImageReconstructionResult(
                    output_path=output_path,
                    provider="local",
                    model="content-safe-lanczos",
                    prompt="",
                    source_path=Path(source_path),
                    latency_ms=1,
                    postprocess={
                        "status": "applied",
                        "content_preservation": {
                            "status": "pass",
                            "review_required": False,
                            "reasons": [],
                        },
                    },
                )

            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "_global_ai_enabled", return_value=False),
                patch.object(app_server, "build_content_safe_upscale", side_effect=fake_upscale),
            ):
                updated = app_server._mutate_enhance_image(
                    session,
                    {"problemIds": ["p1"], "mode": "preserve"},
                )

            recovered_root = Path(updated["output_dir"])
            enhanced_path = app_server.decode_file_reference(updated["problems"][0]["imagePath"])
            self.assertEqual("recovered", recovered_root.parent.name)
            self.assertEqual(recovered_root, Path(updated["outputDir"]))
            self.assertEqual(original_source, updated["problems"][0]["originalImagePath"])
            self.assertTrue(enhanced_path.is_file())
            self.assertTrue(app_server._path_is_within(enhanced_path, recovered_root))
            self.assertFalse(legacy_root.exists())

    def test_image_enhancement_rejects_custom_long_root_without_writing(self):
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            runtime_dir = root / "runtime"
            custom = root / ("custom_" + "y" * 230)
            session = self._editable_session(root, custom)

            with (
                patch.object(app_server, "RUNTIME_DIR", runtime_dir),
                patch.object(app_server, "_global_ai_enabled", return_value=False),
            ):
                with self.assertRaises(app_server.SessionWritePathTooLong) as raised:
                    app_server._mutate_enhance_image(
                        session,
                        {"problemIds": ["p1"], "mode": "preserve"},
                    )

            self.assertEqual("session_enhance_image", raised.exception.operation)
            self.assertEqual(str(custom), session["output_dir"])
            self.assertFalse(custom.exists())

    def test_non_windows_does_not_apply_239_unit_budget(self):
        parent = Path("/tmp") / ("x" * 400)
        with patch.object(app_server.sys, "platform", "linux"):
            budget = app_server._managed_path_component_max_bytes(
                parent,
                reserved_descendants=app_server.MANAGED_OUTPUT_RESERVED_DESCENDANTS,
                max_bytes=96,
            )
            self.assertEqual(96, budget)
            self.assertTrue(
                app_server._session_write_paths_fit(
                    parent,
                    app_server.MANAGED_OUTPUT_RESERVED_DESCENDANTS,
                )
            )

    def test_retry_generation_uses_compact_budgeted_path(self):
        with TemporaryDirectory() as raw_tmp:
            output_dir = Path(raw_tmp) / ("managed_" + "x" * 90)
            retry_dir = app_server._managed_retry_dir(
                output_dir,
                "page_" + "긴이름" * 80,
                "20260824_120000_very_long_retry_stamp",
            )
            crop_path = retry_dir / app_server.MANAGED_CROP_RELATIVE_PATH

            self.assertLessEqual(
                app_server._windows_path_units(crop_path),
                app_server.WINDOWS_MANAGED_PATH_MAX_UNITS,
            )
            self.assertLessEqual(
                len(retry_dir.name.encode("utf-8")),
                app_server.MANAGED_GENERATION_DIR_NAME_MAX_BYTES,
            )

    def test_publish_prepare_file_not_found_is_not_misreported_as_missing_asset(self):
        failure = app_server._publish_stage_failure_payload(
            "prepare",
            FileNotFoundError(3, "The system cannot find the path specified"),
        )
        self.assertEqual("publish_output_unavailable", failure["code"])
        self.assertNotEqual("publish_asset_missing", failure["code"])

    def test_publish_prepare_enametoolong_has_dedicated_code(self):
        failure = app_server._publish_stage_failure_payload(
            "prepare",
            OSError(errno.ENAMETOOLONG, "too long"),
        )
        self.assertEqual("publish_path_too_long", failure["code"])
        self.assertFalse(failure["retryable"])

    def test_retry_adopts_recovered_aliases_only_after_applied_result(self):
        original = Path("C:/legacy/long-output")
        recovered = Path("C:/short/recovered/session_123")
        session = {"output_dir": str(original), "outputDir": str(original)}

        changed = app_server._adopt_recovered_output_dir_after_success(
            session,
            recovered,
            recovered=True,
            summaries=[{"status": "missing_source"}, {"status": "failed"}],
        )
        self.assertFalse(changed)
        self.assertEqual(str(original), session["output_dir"])

        changed = app_server._adopt_recovered_output_dir_after_success(
            session,
            recovered,
            recovered=True,
            summaries=[{"status": "applied"}],
        )
        self.assertTrue(changed)
        self.assertEqual(str(recovered), session["output_dir"])
        self.assertEqual(str(recovered), session["outputDir"])

    def test_publish_handler_returns_typed_custom_path_failure(self):
        class FakeServer:
            def __init__(self, session):
                self.latest_session = session

        session = {
            "problems": [{"id": "p1", "title": "1", "riskFlags": [], "bbox": {}}],
            "pages": [],
        }
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = FakeServer(session)
        handler._read_json_body = lambda: {}
        responses = []
        handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

        error = app_server.SessionWritePathTooLong("session_publish", Path("C:/very-long"))
        with (
            patch.object(app_server, "_problems_to_entries", return_value=[object()]),
            patch.object(app_server, "_select_session_write_root", side_effect=error),
        ):
            handler._handle_session_publish()

        body, kwargs = responses[0]
        self.assertEqual("publish_path_too_long", body["code"])
        self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs["status"])

    def test_retry_handler_returns_typed_custom_path_failure(self):
        class FakeServer:
            def __init__(self, session):
                self.latest_session = session

        session = {"problems": [], "pages": []}
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = FakeServer(session)
        handler._read_json_body = lambda: {}
        responses = []
        handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

        error = app_server.SessionWritePathTooLong("session_retry_ai", Path("C:/very-long"))
        with patch.object(app_server, "_mutate_retry_ai", side_effect=error):
            handler._handle_session_retry_ai()

        body, kwargs = responses[0]
        self.assertEqual("recognition_retry_path_too_long", body["code"])
        self.assertEqual("session_retry_ai", body["operation"])
        self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs["status"])

    def test_mutation_handler_returns_typed_custom_path_failure(self):
        class FakeServer:
            def __init__(self, session):
                self.latest_session = session

        session = {"problems": [{"id": "p1"}], "pages": []}
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = FakeServer(session)
        handler._read_json_body = lambda: {"action": "crop", "problemId": "p1"}
        responses = []
        handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

        error = app_server.SessionWritePathTooLong("session_mutate", Path("C:/very-long"))
        with patch.object(app_server, "_mutate_crop", side_effect=error):
            handler._handle_session_mutate()

        body, kwargs = responses[0]
        self.assertEqual("session_mutation_path_too_long", body["code"])
        self.assertEqual("session_mutate", body["operation"])
        self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs["status"])

    def test_enhancement_handler_returns_typed_custom_path_failure(self):
        class FakeServer:
            def __init__(self, session):
                self.latest_session = session

        session = {"problems": [{"id": "p1"}], "pages": []}
        handler = object.__new__(app_server.AppRequestHandler)
        handler.server = FakeServer(session)
        handler._read_json_body = lambda: {"problemIds": ["p1"], "mode": "preserve"}
        responses = []
        handler._send_json = lambda body, **kwargs: responses.append((body, kwargs))

        error = app_server.SessionWritePathTooLong("session_enhance_image", Path("C:/very-long"))
        with patch.object(app_server, "_mutate_enhance_image", side_effect=error):
            handler._handle_session_enhance_image()

        body, kwargs = responses[0]
        self.assertEqual("image_enhancement_path_too_long", body["code"])
        self.assertEqual("session_enhance_image", body["operation"])
        self.assertEqual(app_server.HTTPStatus.CONFLICT, kwargs["status"])


if __name__ == "__main__":
    unittest.main()
