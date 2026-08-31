from __future__ import annotations

import plistlib
import hashlib
import json
import shutil
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from scripts.verify_frontend_package import (
    REQUIRED_RUNTIME_SOURCE_FILES,
    REQUIRED_UI_FILES,
    collect_deterministic_bundle_errors,
    collect_errors,
    frontend_bundle_source_digest,
)
from scripts.verify_packaged_app import collect_package_errors
from scripts.verify_packaged_app import REQUIRED_SOURCE_PACKAGE_FILES
from scripts.smoke_packaged_app import _validate_ui_assets, _validate_update_metadata


PROJECT_ROOT = Path(__file__).resolve().parent


class TestPackagingFrontendManifest(unittest.TestCase):
    def _write_release_metadata(self, resource_root: Path, version: str = "test") -> None:
        metadata_root = resource_root / "release_metadata"
        metadata_root.mkdir(parents=True)
        component = {
            "name": "react",
            "normalizedName": "react",
            "version": "18.2.0",
            "licenseExpression": "MIT",
            "disposition": "notice",
            "licenseFiles": [{"path": "THIRD_PARTY_NOTICES.md"}],
        }
        payloads = {
            "dependency-inventory.json": {
                "schemaVersion": 1,
                "appVersion": version,
                "environment": "test",
                "components": [component],
            },
            "sbom.spdx.json": {
                "spdxVersion": "SPDX-2.3",
                "dataLicense": "CC0-1.0",
                "SPDXID": "SPDXRef-DOCUMENT",
                "packages": [{"SPDXID": "SPDXRef-Package-react", "name": "react"}],
            },
            "release-provenance.json": {
                "schemaVersion": 1,
                "appVersion": version,
                "gitCommit": "unknown",
                "dependencyFingerprintSha256": "0" * 64,
                "toolFingerprintSha256": "1" * 64,
                "toolInventory": {
                    "python": "test",
                    "pythonImplementation": "test",
                    "pip": "test",
                    "pyinstaller": "test",
                    "platform": "test",
                },
            },
        }
        for file_name, payload in payloads.items():
            (metadata_root / file_name).write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
        (metadata_root / "THIRD_PARTY_NOTICES.md").write_text("MIT notice\n", encoding="utf-8")
        manifest_files = []
        for path in sorted(metadata_root.iterdir()):
            if not path.is_file():
                continue
            content = path.read_bytes()
            manifest_files.append(
                {
                    "path": path.name,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "sizeBytes": len(content),
                }
            )
        (metadata_root / "metadata-manifest.json").write_text(
            json.dumps(
                {"schemaVersion": 1, "appVersion": version, "files": manifest_files},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_frontend_project(self, project_root: Path) -> None:
        ui_root = project_root / "ui_prototype"
        vendor_root = ui_root / "vendor"
        scripts_root = project_root / "scripts"
        build_vendor_root = scripts_root / "vendor"
        assets_root = project_root / "assets"
        vendor_root.mkdir(parents=True)
        build_vendor_root.mkdir(parents=True)
        assets_root.mkdir(parents=True)
        (ui_root / "art.jsx").write_text("const Art = () => null;\n", encoding="utf-8")
        (ui_root / "tweaks-panel.jsx").write_text("const Tweaks = () => null;\n", encoding="utf-8")
        (ui_root / "app.jsx").write_text("const App = () => null;\n", encoding="utf-8")
        (scripts_root / "build_frontend_bundle.mjs").write_text("console.log('build');\n", encoding="utf-8")
        (scripts_root / "render_hwp_with_rhwp_core.mjs").write_text("console.log('render');\n", encoding="utf-8")
        (build_vendor_root / "babel.min.js").write_text("// babel build tool\n", encoding="utf-8")
        (assets_root / "app_icon.png").write_bytes(b"png")
        (ui_root / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        (ui_root / "favicon.png").write_bytes(b"png")
        (ui_root / "reorder.js").write_text("// reorder\n", encoding="utf-8")
        (ui_root / "review_filters.js").write_text("// filters\n", encoding="utf-8")
        (ui_root / "publish_summary.js").write_text("// summary\n", encoding="utf-8")
        (ui_root / "publish_guard.js").write_text("// guard\n", encoding="utf-8")
        (vendor_root / "react.production.min.js").write_text("// react\n", encoding="utf-8")
        (vendor_root / "react-dom.production.min.js").write_text("// react-dom\n", encoding="utf-8")
        digest = frontend_bundle_source_digest(project_root)
        self.assertIsNotNone(digest)
        (ui_root / "app.bundle.js").write_text(
            "/*\n"
            " * Generated by scripts/build_frontend_bundle.mjs.\n"
            f" * Source SHA256: {digest}\n"
            " */\n"
            "/* app.jsx */\n",
            encoding="utf-8",
        )
        (ui_root / "board.html").write_text(
            f'<!doctype html><script src="app.bundle.js?v=frontend-bundle-{digest}"></script>\n',
            encoding="utf-8",
        )
        manifest_text = "\n".join((*REQUIRED_UI_FILES, *REQUIRED_RUNTIME_SOURCE_FILES)) + "\n"
        for manifest_name in ("ClassInEDBMVP.spec", "package_macos_app.sh", "package_mvp.ps1"):
            (project_root / manifest_name).write_text(manifest_text, encoding="utf-8")

    def _write_packaged_runtime(self, package_root: Path, resource_rel: str = "_internal") -> Path:
        resource_root = package_root / resource_rel
        ui_root = resource_root / "ui_prototype"
        vendor_root = ui_root / "vendor"
        scripts_root = resource_root / "scripts"
        assets_root = resource_root / "assets"
        vendor_root.mkdir(parents=True)
        scripts_root.mkdir(parents=True)
        assets_root.mkdir(parents=True)
        (resource_root / "app_update_config.json").write_text(
            '{"appId":"ClassInEDBMVP","appName":"ClassInEDBMVP","version":"test"}\n',
            encoding="utf-8",
        )
        self._write_release_metadata(resource_root)
        (scripts_root / "render_hwp_with_rhwp_core.mjs").write_text("console.log('ok');\n", encoding="utf-8")
        (assets_root / "app_icon.png").write_bytes(b"png")
        (ui_root / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
        (ui_root / "favicon.png").write_bytes(b"png")
        digest = "0" * 64
        (ui_root / "board.html").write_text(
            f'<!doctype html><script src="app.bundle.js?v=frontend-bundle-{digest}"></script>\n',
            encoding="utf-8",
        )
        (ui_root / "reorder.js").write_text("// reorder\n", encoding="utf-8")
        (ui_root / "review_filters.js").write_text("// filters\n", encoding="utf-8")
        (ui_root / "publish_summary.js").write_text("// summary\n", encoding="utf-8")
        (ui_root / "publish_guard.js").write_text("// guard\n", encoding="utf-8")
        (ui_root / "app.bundle.js").write_text(
            "/*\n"
            " * Generated by scripts/build_frontend_bundle.mjs.\n"
            f" * Source SHA256: {digest}\n"
            " */\n"
            "/* app.jsx */\n",
            encoding="utf-8",
        )
        (vendor_root / "react.production.min.js").write_text("// react\n", encoding="utf-8")
        (vendor_root / "react-dom.production.min.js").write_text("// react-dom\n", encoding="utf-8")
        if resource_rel == "_internal":
            (package_root / f"{package_root.name}.exe").write_bytes(b"launcher")
            (resource_root / "python312.dll").write_bytes(b"python runtime")
        return resource_root

    def _write_macos_info_plist(
        self,
        package_root: Path,
        version: str = "test",
        bundle_id: str = "local.classin.edbmvp",
    ) -> None:
        contents_root = package_root / "Contents"
        contents_root.mkdir(parents=True, exist_ok=True)
        (contents_root / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": bundle_id,
                    "CFBundleName": "ClassInEDBMVP",
                    "CFBundleShortVersionString": version,
                    "CFBundleVersion": version,
                },
                sort_keys=True,
            )
        )

    def test_frontend_package_manifest_is_current(self) -> None:
        self.assertEqual([], collect_errors(PROJECT_ROOT))

    def test_frontend_package_rejects_stale_bundle_source_digest(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            self._write_frontend_project(project_root)
            self.assertEqual([], collect_errors(project_root))

            (project_root / "ui_prototype" / "app.jsx").write_text("const App = () => 'changed';\n", encoding="utf-8")
            errors = collect_errors(project_root)

        self.assertTrue(any("source digest is stale" in error for error in errors))

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for deterministic bundle checking")
    def test_deterministic_bundle_check_rejects_tracked_bundle_body_tampering(self) -> None:
        required_files = (
            "ui_prototype/art.jsx",
            "ui_prototype/tweaks-panel.jsx",
            "ui_prototype/app.jsx",
            "ui_prototype/app.bundle.js",
            "ui_prototype/board.html",
            "scripts/build_frontend_bundle.mjs",
            "scripts/vendor/babel.min.js",
        )
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            for rel_path in required_files:
                target = project_root / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROJECT_ROOT / rel_path, target)

            self.assertEqual([], collect_deterministic_bundle_errors(project_root))
            bundle_path = project_root / "ui_prototype" / "app.bundle.js"
            bundle_path.write_bytes(bundle_path.read_bytes() + b"\n// preserved-header tamper\n")

            errors = collect_deterministic_bundle_errors(project_root)

        self.assertTrue(any("deterministic frontend bundle check failed" in error for error in errors))

    def test_frontend_package_rejects_bundle_built_with_old_bundler(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            self._write_frontend_project(project_root)
            self.assertEqual([], collect_errors(project_root))

            (project_root / "scripts" / "build_frontend_bundle.mjs").write_text(
                "console.log('changed build script');\n",
                encoding="utf-8",
            )
            errors = collect_errors(project_root)

        self.assertTrue(any("source digest is stale" in error for error in errors))

    def test_frontend_package_rejects_bundle_built_with_old_babel_transformer(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            self._write_frontend_project(project_root)
            self.assertEqual([], collect_errors(project_root))

            (project_root / "scripts" / "vendor" / "babel.min.js").write_text(
                "// changed babel build tool\n",
                encoding="utf-8",
            )
            errors = collect_errors(project_root)

        self.assertTrue(any("source digest is stale" in error for error in errors))

    def test_frontend_package_rejects_bundle_without_source_digest(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            self._write_frontend_project(project_root)
            (project_root / "ui_prototype" / "app.bundle.js").write_text(
                "/* Generated by scripts/build_frontend_bundle.mjs */\n/* app.jsx */\n",
                encoding="utf-8",
            )

            errors = collect_errors(project_root)

        self.assertTrue(any("missing the source digest" in error for error in errors))

    def test_frontend_package_rejects_stale_board_cache_bust(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            self._write_frontend_project(project_root)
            self.assertEqual([], collect_errors(project_root))

            (project_root / "ui_prototype" / "board.html").write_text(
                f'<!doctype html><script src="app.bundle.js?v=frontend-bundle-{"1" * 64}"></script>\n',
                encoding="utf-8",
            )
            errors = collect_errors(project_root)

        self.assertTrue(any("cache bust is stale" in error for error in errors))

    def test_frontend_package_rejects_browser_side_babel_in_ui_assets(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            self._write_frontend_project(project_root)
            self.assertEqual([], collect_errors(project_root))

            (project_root / "ui_prototype" / "vendor" / "babel.min.js").write_text(
                "// old browser-side babel\n",
                encoding="utf-8",
            )
            errors = collect_errors(project_root)

        self.assertTrue(any("ui_prototype/vendor/babel.min.js" in error for error in errors))

    def test_frontend_package_rejects_missing_runtime_source_asset(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            self._write_frontend_project(project_root)
            self.assertEqual([], collect_errors(project_root))

            (project_root / "scripts" / "render_hwp_with_rhwp_core.mjs").unlink()
            errors = collect_errors(project_root)

        self.assertTrue(any("missing required runtime asset" in error for error in errors))

    def test_frontend_package_rejects_manifest_missing_runtime_source_asset(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project_root = Path(raw_tmp)
            self._write_frontend_project(project_root)
            self.assertEqual([], collect_errors(project_root))

            manifest = "\n".join(REQUIRED_UI_FILES) + "\n"
            (project_root / "ClassInEDBMVP.spec").write_text(manifest, encoding="utf-8")
            errors = collect_errors(project_root)

        self.assertTrue(any("ClassInEDBMVP.spec does not include runtime asset" in error for error in errors))

    def test_packaged_app_layout_accepts_current_runtime_assets(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            self._write_packaged_runtime(package_root)

            self.assertEqual([], collect_package_errors(package_root))

    def test_packaged_app_layout_rejects_missing_release_metadata(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            for path in sorted((resource_root / "release_metadata").rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            (resource_root / "release_metadata").rmdir()

            errors = collect_package_errors(package_root)

        self.assertTrue(any("release metadata" in error for error in errors))

    def test_packaged_app_rejects_expected_commit_mismatch(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            self._write_packaged_runtime(package_root)

            errors = collect_package_errors(
                package_root,
                expected_git_commit="a" * 40,
            )

        self.assertTrue(any("git commit mismatch" in error for error in errors))

    def test_packaged_app_layout_accepts_custom_install_directory(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "custom-install-location"
            self._write_packaged_runtime(package_root)
            (package_root / "custom-install-location.exe").rename(package_root / "ClassInEDBMVP.exe")
            (package_root / "unins000.exe").write_bytes(b"uninstaller")

            errors = collect_package_errors(
                package_root,
                expected_app_name="ClassInEDBMVP",
            )

        self.assertEqual([], errors)

    def test_packaged_app_layout_rejects_incomplete_windows_onedir_runtime(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (package_root / "ClassInEDBMVP.exe").unlink()
            (resource_root / "python312.dll").unlink()

            errors = collect_package_errors(package_root)

        self.assertTrue(any("missing Windows packaged launcher" in error for error in errors))
        self.assertTrue(any("missing Windows packaged Python runtime DLL" in error for error in errors))

    def test_packaged_app_layout_rejects_nested_distribution_artifacts(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            self._write_packaged_runtime(package_root)
            (package_root / "ClassInEDBMVP.zip").write_bytes(b"stale archive")

            errors = collect_package_errors(package_root)

        self.assertTrue(any("forbidden nested distribution artifact" in error for error in errors))
        self.assertTrue(any("ClassInEDBMVP.zip" in error for error in errors))

    def test_packaged_app_layout_accepts_app_name_alias(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "app_update_config.json").write_text(
                '{"appId":"ClassInEDBMVP","app_name":"ClassInEDBMVP","version":"test"}\n',
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root, expected_app_name="ClassInEDBMVP")

        self.assertEqual([], errors)

    def test_packaged_app_layout_rejects_missing_update_app_id(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "app_update_config.json").write_text(
                '{"appName":"ClassInEDBMVP","version":"test"}\n',
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("missing appId" in error for error in errors))

    def test_packaged_app_layout_rejects_invalid_update_metadata_urls(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "app_update_config.json").write_text(
                "{"
                '"appId":"ClassInEDBMVP",'
                '"appName":"ClassInEDBMVP",'
                '"version":"test",'
                '"updateFeedUrl":"http://example.test/update.json",'
                '"downloadUrl":"not-a-url",'
                '"releaseNotesUrl":"ftp://example.test/releases/test"'
                "}\n",
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("updateFeedUrl must use https or loopback http" in error for error in errors))
        self.assertTrue(any("downloadUrl must be an absolute URL" in error for error in errors))
        self.assertTrue(any("releaseNotesUrl must use https or loopback http" in error for error in errors))

    def test_packaged_app_layout_allows_loopback_update_metadata_url(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "app_update_config.json").write_text(
                "{"
                '"appId":"ClassInEDBMVP",'
                '"appName":"ClassInEDBMVP",'
                '"version":"test",'
                '"updateFeedUrl":"http://127.0.0.1:8765/update.json",'
                '"downloadUrl":"http://localhost:8765/ClassInEDBMVP-macOS.zip",'
                '"releaseNotesUrl":"https://example.test/releases/test"'
                "}\n",
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertEqual([], errors)

    def test_packaged_app_layout_rejects_unexpected_update_metadata_urls(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "app_update_config.json").write_text(
                "{"
                '"appId":"ClassInEDBMVP",'
                '"appName":"ClassInEDBMVP",'
                '"version":"test",'
                '"updateFeedUrl":"https://example.test/update.json",'
                '"downloadUrl":"https://example.test/ClassInEDBMVP-macOS.zip",'
                '"releaseNotesUrl":"https://example.test/releases/test"'
                "}\n",
                encoding="utf-8",
            )

            errors = collect_package_errors(
                package_root,
                expected_update_feed_url="https://example.test/other-update.json",
                expected_download_url="https://example.test/other.zip",
                expected_release_notes_url="https://example.test/releases/other",
            )

        self.assertTrue(any("updateFeedUrl mismatch" in error for error in errors))
        self.assertTrue(any("downloadUrl mismatch" in error for error in errors))
        self.assertTrue(any("releaseNotesUrl mismatch" in error for error in errors))

    def test_packaged_app_layout_rejects_conflicting_update_metadata_aliases(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "app_update_config.json").write_text(
                "{"
                '"appId":"ClassInEDBMVP",'
                '"app_id":"OtherApp",'
                '"appName":"ClassInEDBMVP",'
                '"app_name":"Old ClassIn App",'
                '"version":"test",'
                '"updateFeedUrl":"https://example.test/update.json",'
                '"update_feed_url":"https://example.test/other-update.json",'
                '"downloadUrl":"https://example.test/ClassInEDBMVP-macOS.zip",'
                '"download_url":"https://example.test/other.zip",'
                '"releaseNotesUrl":"https://example.test/releases/test",'
                '"release_notes_url":"https://example.test/releases/other"'
                "}\n",
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("appId aliases conflict" in error for error in errors))
        self.assertTrue(any("appName aliases conflict" in error for error in errors))
        self.assertTrue(any("updateFeedUrl aliases conflict" in error for error in errors))
        self.assertTrue(any("downloadUrl aliases conflict" in error for error in errors))
        self.assertTrue(any("releaseNotesUrl aliases conflict" in error for error in errors))

    def test_packaged_app_layout_rejects_conflicting_duplicate_update_configs(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            self._write_packaged_runtime(package_root)
            (package_root / "app_update_config.json").write_text(
                '{"appId":"ClassInEDBMVP","appName":"ClassInEDBMVP","version":"old"}\n',
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("multiple packaged app_update_config.json files disagree" in error for error in errors))

    def test_packaged_app_layout_rejects_legacy_browser_runtime(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            ui_root = resource_root / "ui_prototype"
            (ui_root / "vendor" / "babel.min.js").write_text("// babel\n", encoding="utf-8")
            (ui_root / "board.html").write_text(
                '<!doctype html><script src="app.js?v=old-ui"></script>\n',
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("babel.min.js" in error for error in errors))
        self.assertTrue(any("app.js?v=" in error for error in errors))

    def test_packaged_app_layout_rejects_bundle_without_source_digest(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "ui_prototype" / "app.bundle.js").write_text(
                "/* Generated by scripts/build_frontend_bundle.mjs */\n/* app.jsx */\n",
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("source digest" in error for error in errors))

    def test_packaged_app_layout_rejects_board_cache_bust_mismatch(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "ui_prototype" / "board.html").write_text(
                f'<!doctype html><script src="app.bundle.js?v=frontend-bundle-{"1" * 64}"></script>\n',
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("cache bust does not match" in error for error in errors))

    def test_packaged_app_layout_rejects_unexpected_update_metadata(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            self._write_packaged_runtime(package_root)

            errors = collect_package_errors(
                package_root,
                expected_app_id="OtherApp",
                expected_app_name="ClassInEDBMVP",
                expected_version="0.2.0",
            )

        self.assertTrue(any("appId mismatch" in error for error in errors))
        self.assertTrue(any("version mismatch" in error for error in errors))

    def test_packaged_app_layout_rejects_macos_info_plist_version_mismatch(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP.app"
            self._write_packaged_runtime(package_root, "Contents/Resources")
            self._write_macos_info_plist(package_root, version="0.9.0")

            errors = collect_package_errors(package_root)

        self.assertTrue(any("CFBundleShortVersionString mismatch" in error for error in errors))
        self.assertTrue(any("CFBundleVersion mismatch" in error for error in errors))

    def test_packaged_app_layout_rejects_macos_bundle_identifier_mismatch(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP.app"
            self._write_packaged_runtime(package_root, "Contents/Resources")
            self._write_macos_info_plist(package_root, bundle_id="com.example.old-edb")

            errors = collect_package_errors(
                package_root,
                expected_bundle_id="local.classin.edbmvp",
            )

        self.assertTrue(any("CFBundleIdentifier mismatch" in error for error in errors))

    def test_packaged_app_layout_rejects_missing_macos_info_plist(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP.app"
            self._write_packaged_runtime(package_root, "Contents/Resources")

            errors = collect_package_errors(package_root)

        self.assertTrue(any("missing macOS app bundle Info.plist" in error for error in errors))

    def test_packaged_app_layout_rejects_runtime_session_artifacts(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (package_root / ".app_runtime" / "outputs").mkdir(parents=True)
            (resource_root / "latest_session.json").write_text('{"problems":[]}\n', encoding="utf-8")
            (resource_root / "uploads").mkdir()

            errors = collect_package_errors(package_root)

        self.assertTrue(any(".app_runtime" in error for error in errors))
        self.assertTrue(any("latest_session.json" in error for error in errors))
        self.assertTrue(any("uploads" in error for error in errors))

    def test_packaged_app_layout_rejects_secret_files(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / ".env").write_text("OPENAI_API_KEY=placeholder\n", encoding="utf-8")
            (resource_root / "user_settings.json").write_text(
                '{"openai_api_key":"placeholder"}\n',
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("forbidden packaged secret file exists" in error for error in errors))
        self.assertTrue(any(".env" in error for error in errors))
        self.assertTrue(any("user_settings.json" in error for error in errors))

    def test_packaged_app_layout_rejects_secret_values(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            fake_openai_key = "sk-" + ("a" * 40)
            bundle_path = resource_root / "ui_prototype" / "app.bundle.js"
            bundle_path.write_text(
                bundle_path.read_text(encoding="utf-8") + f"const leaked = '{fake_openai_key}';\n",
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("forbidden packaged secret value" in error for error in errors))
        self.assertTrue(any("OpenAI API key" in error for error in errors))

    def test_source_package_layout_requires_runtime_python_files(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "source-package"
            self._write_packaged_runtime(package_root, "")
            for rel_path in REQUIRED_SOURCE_PACKAGE_FILES:
                target = package_root / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# runtime\n", encoding="utf-8")
            (package_root / "pipeline_cache.py").unlink()

            errors = collect_package_errors(package_root)

        self.assertTrue(any("missing source-package runtime file: pipeline_cache.py" in error for error in errors))

    def test_source_package_layout_requires_bug_reporting_module(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "source-package"
            self._write_packaged_runtime(package_root, "")
            for rel_path in REQUIRED_SOURCE_PACKAGE_FILES:
                target = package_root / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# runtime\n", encoding="utf-8")
            (package_root / "bug_reporting.py").unlink()

            errors = collect_package_errors(package_root)

        self.assertTrue(any("missing source-package runtime file: bug_reporting.py" in error for error in errors))

    def test_source_package_can_smoke_import_real_entrypoint(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "source-package"
            self._write_packaged_runtime(package_root, "")
            for rel_path in REQUIRED_SOURCE_PACKAGE_FILES:
                target = package_root / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# runtime\n", encoding="utf-8")
            (package_root / "app_server.py").write_text(
                "import bug_reporting\n\ndef main():\n    return 0\n",
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root, smoke_source_import=True)

        self.assertEqual([], errors)

    def test_packaged_app_rejects_frontend_built_from_other_source(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            temp_root = Path(raw_tmp)
            project_root = temp_root / "project"
            project_root.mkdir()
            self._write_frontend_project(project_root)
            package_root = project_root / "dist" / "ClassInEDBMVP"
            self._write_packaged_runtime(package_root)

            errors = collect_package_errors(package_root, source_root=project_root)

        self.assertTrue(any("does not match current source" in error for error in errors))

    def test_packaged_app_rejects_bundle_byte_tampering_with_valid_header(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            temp_root = Path(raw_tmp)
            project_root = temp_root / "project"
            project_root.mkdir()
            self._write_frontend_project(project_root)
            package_root = project_root / "dist" / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            source_bundle = project_root / "ui_prototype" / "app.bundle.js"
            packaged_bundle = resource_root / "ui_prototype" / "app.bundle.js"
            digest = frontend_bundle_source_digest(project_root)
            (resource_root / "ui_prototype" / "board.html").write_text(
                f'<!doctype html><script src="app.bundle.js?v=frontend-bundle-{digest}"></script>\n',
                encoding="utf-8",
            )
            packaged_bundle.write_bytes(source_bundle.read_bytes())
            self.assertEqual(
                [],
                collect_package_errors(package_root, source_root=project_root),
            )
            packaged_bundle.write_bytes(source_bundle.read_bytes() + b"\n// tampered after header\n")

            errors = collect_package_errors(package_root, source_root=project_root)

        self.assertTrue(any("bytes do not match current source bundle" in error for error in errors))

    def test_packaged_app_layout_rejects_build_time_frontend_tools(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "scripts" / "vendor").mkdir()
            (resource_root / "scripts" / "build_frontend_bundle.mjs").write_text(
                "console.log('build');\n",
                encoding="utf-8",
            )
            (resource_root / "scripts" / "vendor" / "babel.min.js").write_text(
                "// build-time babel\n",
                encoding="utf-8",
            )
            (resource_root / "scripts" / "verify_frontend_package.py").write_text(
                "print('dev verifier')\n",
                encoding="utf-8",
            )

            errors = collect_package_errors(package_root)

        self.assertTrue(any("scripts/build_frontend_bundle.mjs" in error for error in errors))
        self.assertTrue(any("scripts/vendor" in error for error in errors))
        self.assertTrue(any("scripts/verify_frontend_package.py" in error for error in errors))

    def test_packaged_app_layout_rejects_source_asset_files(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "assets" / "app_icon.svg").write_text("<svg />\n", encoding="utf-8")
            (resource_root / "assets" / "brand_mark.svg").write_text("<svg />\n", encoding="utf-8")

            errors = collect_package_errors(package_root)

        self.assertTrue(any("assets/app_icon.svg" in error for error in errors))
        self.assertTrue(any("assets/brand_mark.svg" in error for error in errors))

    def test_packaged_app_layout_rejects_unlicensed_upscayl_bundle(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP"
            resource_root = self._write_packaged_runtime(package_root)
            (resource_root / "resources" / "upscayl").mkdir(parents=True)

            errors = collect_package_errors(package_root)

        self.assertTrue(any("Upscayl runtime is missing compliance file" in error for error in errors))

    def test_packaged_app_layout_dedupes_macos_resource_symlinks(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "ClassInEDBMVP.app"
            frameworks_root = package_root / "Contents" / "Frameworks"
            self._write_packaged_runtime(package_root, "Contents/Resources")
            self._write_macos_info_plist(package_root)
            frameworks_root.mkdir(parents=True)
            try:
                (frameworks_root / "ui_prototype").symlink_to("../Resources/ui_prototype", target_is_directory=True)
                (frameworks_root / "scripts").symlink_to("../Resources/scripts", target_is_directory=True)
                (frameworks_root / "app_update_config.json").symlink_to("../Resources/app_update_config.json")
            except OSError as exc:
                raise unittest.SkipTest(f"symlink creation is not available: {exc}") from exc

            self.assertEqual([], collect_package_errors(package_root))

    def test_packaging_scripts_run_frontend_package_verifier(self) -> None:
        for rel_path in ("ClassInEDBMVP.spec", "package_macos_app.sh", "package_mvp.ps1"):
            with self.subTest(rel_path=rel_path):
                source = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
                if rel_path.endswith(".spec"):
                    self.assertIn("collect_errors", source)
                    self.assertIn("verify_frontend_package()", source)
                else:
                    self.assertIn("scripts/verify_frontend_package.py".replace("/", "\\" if rel_path.endswith(".ps1") else "/"), source)

    def test_packaging_scripts_verify_frontend_after_optional_bundle_build(self) -> None:
        shell_source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertLess(
            shell_source.index("scripts/build_frontend_bundle.mjs"),
            shell_source.index("scripts/verify_frontend_package.py"),
        )

        ps_source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertLess(
            ps_source.index("scripts\\build_frontend_bundle.mjs"),
            ps_source.index("scripts\\verify_frontend_package.py"),
        )

        spec_source = (PROJECT_ROOT / "ClassInEDBMVP.spec").read_text(encoding="utf-8")
        self.assertLess(spec_source.index("verify_frontend_package()"), spec_source.index("a = Analysis("))

    def test_pyinstaller_spec_uses_project_root_for_direct_builds(self) -> None:
        source = (PROJECT_ROOT / "ClassInEDBMVP.spec").read_text(encoding="utf-8")
        self.assertIn("SPECPATH", source)
        self.assertIn("PROJECT_ROOT", source)
        self.assertLess(source.index("sys.path.insert(0, str(PROJECT_ROOT))"), source.index("from scripts.verify_frontend_package import collect_errors"))
        self.assertIn("from scripts.build_app_update_config import build_config, write_config", source)
        self.assertIn("collect_errors(PROJECT_ROOT)", source)
        self.assertIn('globals().get("workpath", project_path("build/ClassInEDBMVP"))', source)
        self.assertIn('pyinstaller_work_path() / "app_update_config.json"', source)
        self.assertIn('build_config(project_path("app_update_config.json"))', source)
        self.assertIn("write_config(target, config)", source)
        self.assertIn('[str(project_path("app_server.py"))]', source)
        self.assertIn("pathex=[str(PROJECT_ROOT)]", source)
        self.assertIn('"CFBundleVersion": bundle_version', source)
        self.assertNotIn("collect_errors(Path.cwd())", source)
        self.assertNotIn("pathex=[]", source)
        self.assertNotIn('project_path("build/app_update_config.json")', source)
        self.assertNotIn("EDB_APP_VERSION", source)

    def test_frontend_bundle_builder_updates_board_cache_bust(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "build_frontend_bundle.mjs").read_text(encoding="utf-8")
        self.assertIn("board.html", source)
        self.assertIn("frontend-bundle-${sourceDigest}", source)
        self.assertIn('args.delete("--check")', source)

    def test_packaging_requires_node_for_deterministic_frontend_verification(self) -> None:
        macos = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        windows = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        spec = (PROJECT_ROOT / "ClassInEDBMVP.spec").read_text(encoding="utf-8")
        self.assertIn("build or deterministically verify", macos)
        self.assertIn("build or deterministically verify", windows)
        self.assertIn("collect_deterministic_bundle_errors", spec)

    def test_packaging_scripts_run_built_artifact_verifier(self) -> None:
        for rel_path in ("package_macos_app.sh", "package_mvp.ps1", "package_windows_installer.ps1"):
            with self.subTest(rel_path=rel_path):
                source = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
                self.assertIn(
                    "scripts/verify_packaged_app.py".replace("/", "\\" if rel_path.endswith(".ps1") else "/"),
                    source,
                )

    def test_windows_source_fallback_smokes_import_and_compares_source(self) -> None:
        source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn('"bug_reporting.py"', source)
        self.assertIn('"--source-root"', source)
        self.assertIn('"--smoke-source-import"', source)
        self.assertIn("-SmokeSourceImport", source)

    def test_windows_onefile_runs_isolated_health_smoke(self) -> None:
        source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        smoke = source.split("function Invoke-EDBOneFileHealthSmoke", 1)[1].split(
            "if (-not $SkipFrontendBuild)", 1
        )[0]
        smoke_script = (PROJECT_ROOT / "scripts" / "smoke_packaged_app.py").read_text(encoding="utf-8")
        onefile_branch = source.split(
            'Assert-EDBNonEmptyFile -Path $PackageRoot -Label "PyInstaller one-file executable"',
            1,
        )[1].split("} else {", 1)[0]

        self.assertIn("scripts\\smoke_packaged_app.py", smoke)
        self.assertIn('"EDB_APP_HOME"', smoke_script)
        self.assertIn('"--no-open-browser"', smoke_script)
        for endpoint in ("/api/health", "/board.html", "/api/app/update"):
            self.assertIn(endpoint, smoke_script)
        self.assertIn(r"app\.bundle\.js", smoke_script)
        self.assertIn("Invoke-EDBOneFileHealthSmoke -ExecutablePath $PackageRoot", onefile_branch)

    def test_packaged_runtime_smoke_validates_ui_and_update_contracts(self) -> None:
        digest = "a" * 64
        board = f'<script src="app.bundle.js?v=frontend-bundle-{digest}"></script>'
        bundle = (
            "/* Generated by scripts/build_frontend_bundle.mjs.\n"
            f" * Source SHA256: {digest}\n */\n/* app.jsx */\n"
        )
        self.assertEqual(digest, _validate_ui_assets(board, bundle))
        with self.assertRaisesRegex(RuntimeError, "cache bust"):
            _validate_ui_assets(board, bundle.replace(digest, "b" * 64))

        update = {
            "ok": True,
            "appId": "ClassInEDBMVP",
            "currentVersion": "1.2.3",
            "platform": "windows",
            "arch": "x64",
            "channelStatus": "up_to_date",
            "configured": True,
            "updateAvailable": False,
        }
        _validate_update_metadata(update)
        _validate_update_metadata(
            {**update, "appId": "CustomEDB"},
            expected_app_id="CustomEDB",
        )
        with self.assertRaisesRegex(RuntimeError, "configured must be boolean"):
            _validate_update_metadata({**update, "configured": "true"})

    def test_packaging_wrappers_pass_explicit_source_root_to_verifier(self) -> None:
        macos = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        windows = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn('--source-root "$PROJECT_ROOT"', macos)
        self.assertIn('"--source-root"', windows)
        self.assertIn("$ProjectRoot", windows)

    def test_packaging_manifests_include_optional_upscayl_runtime(self) -> None:
        expected_resource = "resources/upscayl"
        for rel_path in ("ClassInEDBMVP.spec", "package_macos_app.sh", "package_mvp.ps1"):
            with self.subTest(rel_path=rel_path):
                source = (PROJECT_ROOT / rel_path).read_text(encoding="utf-8")
                normalized = source.replace("\\", "/")
                self.assertIn("upscayl_backend", source)
                self.assertIn(expected_resource, normalized)

    def test_packaging_scripts_print_final_artifact_hashes(self) -> None:
        shell_source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn("print_file_artifact_summary()", shell_source)
        self.assertIn("shasum -a 256", shell_source)
        self.assertIn('print_file_artifact_summary "$ZIP_PATH" "Zip archive"', shell_source)
        self.assertIn('print_file_artifact_summary "$DMG_PATH" "DMG installer"', shell_source)

        ps_source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn("function Write-EDBArtifactSummary", ps_source)
        self.assertIn("Get-FileHash -Algorithm SHA256", ps_source)
        self.assertIn('Write-Host "${Label}: $Path"', ps_source)
        self.assertIn('Write-EDBArtifactSummary -Path $ZipPath -Label "Zip archive"', ps_source)
        self.assertIn('Write-EDBArtifactSummary -Path $PackageRoot -Label "PyInstaller one-file executable"', ps_source)

        installer_source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("function Write-EDBArtifactSummary", installer_source)
        self.assertIn("Get-FileHash -Algorithm SHA256", installer_source)
        self.assertIn('Write-Host "${Label}: $Path"', installer_source)
        self.assertIn('Write-EDBArtifactSummary -Path $InstallerPath -Label "Windows installer"', installer_source)

    def test_windows_installer_metadata_is_parameterized(self) -> None:
        script_source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn('[string]$AppDisplayName = "ClassIn EDB"', script_source)
        self.assertIn('[string]$AppPublisher = "ClassIn EDB"', script_source)
        self.assertIn('"/DAppDisplayName=$AppDisplayName"', script_source)
        self.assertIn('"/DAppPublisher=$AppPublisher"', script_source)

        installer_source = (PROJECT_ROOT / "installer" / "windows" / "ClassInEDBMVP.iss").read_text(encoding="utf-8")
        self.assertIn("#define AppPublisher", installer_source)
        self.assertIn("AppPublisher={#AppPublisher}", installer_source)
        self.assertIn("UninstallDisplayName={#AppDisplayName}", installer_source)

    def test_windows_installer_optimizes_compression_and_cleans_upgrade_artifacts(self) -> None:
        source = (PROJECT_ROOT / "installer" / "windows" / "ClassInEDBMVP.iss").read_text(encoding="utf-8")
        self.assertIn("Compression=lzma2/ultra64", source)
        self.assertIn("SolidCompression=yes", source)
        self.assertIn("CloseApplications=yes", source)
        self.assertIn("RestartApplications=no", source)
        self.assertIn("[InstallDelete]", source)
        for stale_path in (
            r"{app}\.app_runtime",
            r"{app}\ui_prototype\app.js",
            r"{app}\scripts\vendor",
            r"{app}\assets\app_icon.svg",
            r"{app}\_internal\.app_runtime",
            r"{app}\_internal\ui_prototype\app.js",
            r"{app}\_internal\scripts\vendor",
            r"{app}\_internal\assets\app_icon.svg",
        ):
            with self.subTest(stale_path=stale_path):
                self.assertIn(stale_path, source)

    def test_windows_source_package_fallback_copies_only_runtime_scripts(self) -> None:
        source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        for rel_path in (*REQUIRED_SOURCE_PACKAGE_FILES, "scripts\\render_hwp_with_rhwp_core.mjs"):
            windows_rel_path = rel_path.replace("/", "\\")
            with self.subTest(rel_path=windows_rel_path):
                self.assertIn(f'"{windows_rel_path}"', source)
        self.assertNotRegex(source, r'(?m)^\s+"scripts",\s*$')

    def test_windows_source_package_fallback_copies_only_runtime_assets(self) -> None:
        source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn('"assets\\app_icon.png"', source)
        self.assertNotRegex(source, r'(?m)^\s+"assets",\s*$')

    def test_windows_source_package_fallback_uses_generated_update_config(self) -> None:
        source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(source, r'(?m)^\s+"app_update_config\.json",\s*$')
        self.assertIn('Copy-Item -Force $BuildUpdateConfig (Join-Path $PackageRoot "app_update_config.json")', source)

    def test_packaging_scripts_verify_expected_update_metadata(self) -> None:
        shell_source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn('--expected-app-id "$EFFECTIVE_APP_ID"', shell_source)
        self.assertIn('--expected-app-name "$APP_NAME"', shell_source)
        self.assertIn('--expected-version "$EFFECTIVE_APP_VERSION"', shell_source)
        self.assertIn('--expected-update-feed-url "$EFFECTIVE_UPDATE_FEED_URL"', shell_source)
        self.assertIn('--expected-download-url "$EFFECTIVE_DOWNLOAD_URL"', shell_source)
        self.assertIn('--expected-release-notes-url "$EFFECTIVE_RELEASE_NOTES_URL"', shell_source)
        self.assertIn('--expected-bundle-id "$BUNDLE_ID"', shell_source)
        self.assertIn('EDB_PACKAGE_APP_ID="$APP_ID"', shell_source)

        ps_source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn('"--expected-app-id"', ps_source)
        self.assertIn("$EffectiveAppId", ps_source)
        self.assertIn('"--expected-app-name"', ps_source)
        self.assertIn("$EffectiveAppName", ps_source)
        self.assertIn('"--expected-version"', ps_source)
        self.assertIn("$EffectiveAppVersion", ps_source)
        self.assertIn("--expected-update-feed-url", ps_source)
        self.assertIn("$EffectiveUpdateFeedUrl", ps_source)
        self.assertIn("--expected-download-url", ps_source)
        self.assertIn("$EffectiveDownloadUrl", ps_source)
        self.assertIn("--expected-release-notes-url", ps_source)
        self.assertIn("$EffectiveReleaseNotesUrl", ps_source)
        self.assertIn("EDB_PACKAGE_APP_ID = $AppId", ps_source)

        builder_source = (PROJECT_ROOT / "scripts" / "build_app_update_config.py").read_text(encoding="utf-8")
        self.assertIn('"appId": "ClassInEDBMVP"', builder_source)

        installer_source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn('"--expected-app-id"', installer_source)
        self.assertIn('"--expected-app-name"', installer_source)
        self.assertIn('"--expected-version"', installer_source)
        self.assertIn('"--expected-update-feed-url"', installer_source)
        self.assertIn('"--expected-download-url"', installer_source)
        self.assertIn('"--expected-release-notes-url"', installer_source)
        self.assertIn("$EffectiveInstallerAppId", installer_source)

    def test_packaging_scripts_normalize_update_config_aliases(self) -> None:
        spec_source = (PROJECT_ROOT / "ClassInEDBMVP.spec").read_text(encoding="utf-8")
        self.assertIn("from scripts.build_app_update_config import build_config, write_config", spec_source)
        self.assertIn('build_config(project_path("app_update_config.json"))', spec_source)
        self.assertNotIn("EDB_UPDATE_FEED_URL", spec_source)

        shell_source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn('"$PROJECT_ROOT/scripts/build_app_update_config.py"', shell_source)
        self.assertIn('EDB_PACKAGE_UPDATE_FEED_URL="$UPDATE_FEED_URL"', shell_source)
        self.assertNotIn("APP_UPDATE_CONFIG_ALIASES", shell_source)

        ps_source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn("function Get-EDBJsonStringProperty", ps_source)
        self.assertIn('$UpdateConfigScript = Join-Path $ProjectRoot "scripts\\build_app_update_config.py"', ps_source)
        self.assertIn("& $PythonExe $UpdateConfigScript $ProjectUpdateConfig $BuildUpdateConfig", ps_source)
        self.assertIn('Assert-EDBNativeCommandSucceeded "app_update_config generation"', ps_source)
        self.assertIn("EDB_PACKAGE_UPDATE_FEED_URL = $UpdateFeedUrl", ps_source)
        self.assertNotIn("function Set-EDBUpdateConfigAliasValue", ps_source)

        installer_source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn('[string[]]$Names', installer_source)
        self.assertIn('@("updateFeedUrl", "update_feed_url")', installer_source)
        self.assertIn('@("downloadUrl", "download_url")', installer_source)
        self.assertIn('@("releaseNotesUrl", "release_notes_url")', installer_source)

    def test_macos_packaging_rechecks_app_after_signing_and_stapling(self) -> None:
        source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn("verify_packaged_app_root()", source)
        self.assertIn('verify_packaged_app_root "$PACKAGED_APP_ROOT"', source)
        signed_verify_index = source.index('verify_packaged_app_root "$APP_PATH"', source.index('codesign --force --deep --sign -'))
        self.assertLess(source.index('codesign --force --deep --sign -'), signed_verify_index)
        stapler_index = source.index('xcrun stapler staple "$APP_PATH"')
        stapled_verify_index = source.index('verify_packaged_app_root "$APP_PATH"', stapler_index)
        self.assertLess(stapler_index, stapled_verify_index)
        self.assertLess(signed_verify_index, source.index('if [[ "$ZIP" == "1"'))

    def test_packaging_scripts_remove_previous_same_name_outputs(self) -> None:
        shell_source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn('WORK_DIR="$RESOLVED_OUTPUT_DIR/_pyinstaller_build"', shell_source)
        self.assertIn('APP_DIR_PATH="$RESOLVED_OUTPUT_DIR/$APP_NAME"', shell_source)
        self.assertIn('rm -rf "$WORK_DIR" "$APP_PATH" "$APP_DIR_PATH" "$ZIP_PATH" "$DMG_PATH" "$APP_NOTARY_ZIP"', shell_source)
        self.assertIn('find "$RESOLVED_OUTPUT_DIR" -maxdepth 1 -type d -name "$APP_NAME.dmg.*"', shell_source)
        self.assertLess(shell_source.index('rm -rf "$WORK_DIR"'), shell_source.index('"$PYTHON_EXE" -m PyInstaller'))
        verifier_index = shell_source.index("scripts/verify_packaged_app.py")
        collect_cleanup_index = shell_source.index('rm -rf "$APP_DIR_PATH"', verifier_index)
        self.assertLess(verifier_index, collect_cleanup_index)
        work_cleanup_index = shell_source.index('rm -rf "$WORK_DIR"', collect_cleanup_index)
        self.assertLess(collect_cleanup_index, work_cleanup_index)
        self.assertLess(work_cleanup_index, shell_source.index('if [[ "$ZIP" == "1"'))

        ps_source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn("function Remove-EDBPathIfExists", ps_source)
        self.assertIn("$PackageDirPath = Join-Path $ResolvedOutputDir $AppName", ps_source)
        self.assertIn('$SourcePackagePath = Join-Path $ResolvedOutputDir "source-package"', ps_source)
        self.assertIn('$WorkPath = Join-Path $ResolvedOutputDir "_pyinstaller_build"', ps_source)
        cleanup_index = ps_source.index("foreach ($StalePath in @($WorkPath, $PackageDirPath, $PackageExePath, $SourcePackagePath, $ZipPath, $PortableReadmePath))")
        self.assertLess(cleanup_index, ps_source.index("$HasPyInstaller = $true"))
        self.assertIn("Remove-EDBPathIfExists $StalePath", ps_source)
        pyinstaller_index = ps_source.index("if ($HasPyInstaller)")
        package_root_index = ps_source.index("$PackageRoot = if ($OneFile) { $PackageExePath } else { $PackageDirPath }", pyinstaller_index)
        self.assertLess(
            package_root_index,
            ps_source.index("& $PythonExe -m PyInstaller", pyinstaller_index),
        )
        fallback_index = ps_source.index("$PackageRoot = $SourcePackagePath")
        self.assertLess(
            fallback_index,
            ps_source.index("New-Item -ItemType Directory -Force -Path $PackageRoot", fallback_index),
        )

    def test_packaging_scripts_isolate_pyinstaller_workpath(self) -> None:
        shell_source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn('--workpath "$WORK_DIR"', shell_source)
        self.assertLess(shell_source.index('--workpath "$WORK_DIR"'), shell_source.index('--name "$APP_NAME"'))
        self.assertIn('rm -rf "$WORK_DIR"', shell_source)

        ps_source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn('"--workpath", $WorkPath', ps_source)
        self.assertLess(ps_source.index('"--workpath", $WorkPath'), ps_source.index('"--name", $AppName'))
        self.assertLess(
            ps_source.index("& $PythonExe -m PyInstaller @PyInstallerArgs"),
            ps_source.index("Remove-EDBPathIfExists $WorkPath", ps_source.index("& $PythonExe -m PyInstaller @PyInstallerArgs")),
        )

    def test_windows_installer_derives_version_from_packaged_update_metadata(self) -> None:
        source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("Read-PackagedUpdateConfig", source)
        self.assertIn('$EffectiveInstallerVersion = if ($Version) { $Version } else { $PackagedVersion }', source)
        self.assertIn('"--expected-version"', source)
        self.assertIn("$EffectiveInstallerVersion", source)
        self.assertIn('"/DAppVersion=$EffectiveInstallerVersion"', source)
        self.assertNotIn('"/DAppVersion=$Version"', source)

    def test_windows_installer_finds_per_user_inno_setup(self) -> None:
        source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn('$env:LOCALAPPDATA', source)
        self.assertIn('"Programs\\Inno Setup 6\\ISCC.exe"', source)

    def test_windows_installer_verifies_existing_app_before_installer_build(self) -> None:
        source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        verifier_index = source.index("scripts\\verify_packaged_app.py")
        self.assertLess(source.index("PyInstaller app output was not found"), verifier_index)
        self.assertLess(verifier_index, source.index("if ($Sign -and $SkipAppBuild)"))
        self.assertLess(verifier_index, source.index("$Iscc = Find-InnoSetupCompiler"))

    def test_windows_installer_removes_stale_setup_before_compiling(self) -> None:
        source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn('$InstallerPath = Join-Path $ResolvedOutputDir "$AppName-Setup.exe"', source)
        remove_index = source.index("Remove-Item -Force -LiteralPath $InstallerPath")
        self.assertLess(remove_index, source.index("$Iscc = Find-InnoSetupCompiler"))
        self.assertLess(remove_index, source.index("& $Iscc @IsccArgs $InstallerScript"))

    def test_packaging_scripts_verify_distribution_archives(self) -> None:
        shell_source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn("require_nonempty_file()", shell_source)
        self.assertIn("require_zip_entry()", shell_source)
        self.assertIn("verify_dmg_contains_app()", shell_source)
        self.assertIn('awk -v expected="$entry"', shell_source)
        self.assertNotIn("grep -Fxq", shell_source)
        self.assertIn('require_nonempty_file "$ZIP_PATH" "Zip archive"', shell_source)
        self.assertIn('require_zip_entry "$ZIP_PATH" "$APP_NAME.app/Contents/Info.plist"', shell_source)
        self.assertIn('require_nonempty_file "$DMG_PATH" "DMG installer"', shell_source)
        self.assertIn('verify_dmg_contains_app "$DMG_PATH" "$APP_NAME"', shell_source)
        self.assertIn('find "$RESOLVED_OUTPUT_DIR" -maxdepth 1 -type d -name "$APP_NAME.mount.*"', shell_source)
        self.assertIn('require_nonempty_file "$APP_NOTARY_ZIP" "Notary upload archive"', shell_source)
        self.assertLess(
            shell_source.index('require_nonempty_file "$DMG_PATH" "DMG installer"'),
            shell_source.index('hdiutil verify "$DMG_PATH"'),
        )
        self.assertLess(
            shell_source.index('hdiutil verify "$DMG_PATH"'),
            shell_source.index('verify_dmg_contains_app "$DMG_PATH" "$APP_NAME"'),
        )

        ps_source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn("function Assert-EDBNonEmptyFile", ps_source)
        self.assertIn("function Assert-EDBZipContainsEntry", ps_source)
        self.assertIn('$ZipPath = Join-Path $ResolvedOutputDir "$AppName-Portable.zip"', ps_source)
        self.assertIn('$PortableReadmeName = "EXTRACT_BEFORE_RUNNING.txt"', ps_source)
        self.assertIn('Assert-EDBNonEmptyFile -Path $PackageRoot -Label "PyInstaller one-file executable"', ps_source)
        self.assertIn('Assert-EDBNonEmptyFile -Path $ZipPath -Label "Zip archive"', ps_source)
        self.assertIn('Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName $PortableReadmeName', ps_source)
        self.assertIn('Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "$AppName.exe"', ps_source)
        self.assertIn('Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "$AppName/$AppName.exe"', ps_source)
        self.assertIn('Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "source-package/app_update_config.json"', ps_source)
        self.assertLess(
            ps_source.index("Compress-Archive -Path @($PackageRoot, $PortableReadmePath) -DestinationPath $ZipPath"),
            ps_source.index('Assert-EDBNonEmptyFile -Path $ZipPath -Label "Zip archive"'),
        )
        self.assertLess(
            ps_source.index("$BuiltWithPyInstaller = $false"),
            ps_source.index("if ($HasPyInstaller)"),
        )
        self.assertLess(
            ps_source.index("$BuiltWithPyInstaller = $true"),
            ps_source.index("& $PythonExe -m PyInstaller @PyInstallerArgs"),
        )
        self.assertLess(
            ps_source.index('if ($BuiltWithPyInstaller -and $OneFile)'),
            ps_source.index('Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "$AppName.exe"'),
        )
        self.assertLess(
            ps_source.index('} elseif ($BuiltWithPyInstaller)'),
            ps_source.index('Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "$AppName/$AppName.exe"'),
        )
        self.assertLess(
            ps_source.index('} elseif (-not $BuiltWithPyInstaller)'),
            ps_source.index('Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "source-package/app_update_config.json"'),
        )

    def test_windows_installer_verifies_setup_output_before_signing(self) -> None:
        source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("function Assert-EDBNonEmptyFile", source)
        verify_index = source.index('Assert-EDBNonEmptyFile -Path $InstallerPath -Label "Windows installer"')
        self.assertLess(source.index("& $Iscc @IsccArgs $InstallerScript"), verify_index)
        self.assertLess(verify_index, source.index("if ($Sign) {", verify_index))

    def test_windows_packaging_native_command_failures_stop_builds(self) -> None:
        ps_source = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        self.assertIn("function Assert-EDBNativeCommandSucceeded", ps_source)
        for command, label in (
            (
                '& $PythonExe -m pip install --disable-pip-version-check --require-hashes --no-build-isolation -r (Join-Path $ProjectRoot "requirements-release.lock")',
                "Locked release dependency installation",
            ),
            ("& $PythonExe $UpdateConfigScript $ProjectUpdateConfig $BuildUpdateConfig", "app_update_config generation"),
            ("& $PythonExe @VerifierArgs", "Packaged app verification"),
            ('& $PythonExe (Join-Path $ProjectRoot "scripts\\verify_frontend_package.py") --root $ProjectRoot', "Frontend package verification"),
            ("& $PythonExe -m PyInstaller @PyInstallerArgs", "PyInstaller packaging"),
        ):
            with self.subTest(script="package_mvp.ps1", label=label):
                command_index = ps_source.index(command)
                assertion_index = ps_source.index(f'Assert-EDBNativeCommandSucceeded "{label}"', command_index)
                self.assertLess(command_index, assertion_index)

        self.assertIn('Assert-EDBNativeCommandSucceeded "Frontend bundle build"', ps_source)

        installer_source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("function Assert-EDBNativeCommandSucceeded", installer_source)
        for command, label in (
            ("& $PythonExe @VerifierArgs", "Packaged app verification"),
            ("& $Iscc @IsccArgs $InstallerScript", "Inno Setup compilation"),
        ):
            with self.subTest(script="package_windows_installer.ps1", label=label):
                command_index = installer_source.index(command)
                assertion_index = installer_source.index(f'Assert-EDBNativeCommandSucceeded "{label}"', command_index)
                self.assertLess(command_index, assertion_index)

    def test_windows_signing_fails_when_no_signable_artifacts_exist(self) -> None:
        source = (PROJECT_ROOT / "scripts" / "Sign-WindowsArtifact.ps1").read_text(encoding="utf-8")
        self.assertIn('Where-Object { $_.Extension -in @(".exe", ".dll", ".pyd") }', source)
        self.assertIn("No signable Windows artifacts were found under", source)
        guard_index = source.index("No signable Windows artifacts were found under")
        signing_index = source.index("foreach ($Target in $Targets)")
        self.assertLess(guard_index, signing_index)

    def test_ci_installer_workflow_uses_packaging_wrappers(self) -> None:
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "build-installers.yml").read_text(encoding="utf-8")
        self.assertIn("actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020 # v4", workflow)
        self.assertIn("./package_macos_app.sh", workflow)
        self.assertIn(".\\package_windows_installer.ps1", workflow)
        self.assertNotIn("-SkipFrontendBuild", workflow)
        self.assertNotIn("-SkipAppBuild", workflow)
        self.assertLess(workflow.index("./package_macos_app.sh"), workflow.index("dist/ClassInEDBMVP-macOS.dmg"))
        self.assertLess(workflow.index(".\\package_windows_installer.ps1"), workflow.index("dist/ClassInEDBMVP-Setup.exe"))
        self.assertLess(workflow.index("- macos"), workflow.index("python scripts/build_update_feed.py"))
        self.assertLess(workflow.index("- windows"), workflow.index("python scripts/build_update_feed.py"))


if __name__ == "__main__":
    unittest.main()
