from __future__ import annotations

import unittest
import json
import shutil
import subprocess
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.build_release_evidence import (
    collect_release_evidence_errors,
    create_release_evidence,
)
from scripts.build_release_metadata import (
    build_release_metadata,
    collect_locked_environment_errors,
    collect_release_metadata_errors,
    collect_release_policy_errors,
)
from scripts.validate_release_inputs import collect_release_input_errors
from scripts.verify_release_licenses import (
    UPSCAYL_REQUIRED_COMPLIANCE_FILES,
    collect_release_license_errors,
)


PROJECT_ROOT = Path(__file__).resolve().parent


class TestReleaseInputValidation(unittest.TestCase):
    def test_internal_release_allows_no_download_urls(self) -> None:
        self.assertEqual(
            [],
            collect_release_input_errors(version="1.3.0", release_mode="internal-test"),
        )

    def test_public_release_requires_safe_complete_urls(self) -> None:
        self.assertEqual(
            [],
            collect_release_input_errors(
                version="1.3.0",
                release_mode="public",
                update_feed_url="https://downloads.example.test/update.json",
                release_notes_url="https://downloads.example.test/releases/1.3.0",
                manifest_url="https://downloads.example.test/manifest.json",
                macos_download_url="https://downloads.example.test/ClassInEDBMVP-macOS.dmg",
                windows_download_url="https://downloads.example.test/ClassInEDBMVP-Setup.exe",
                license_compliance_approved=True,
            ),
        )

    def test_release_rejects_cross_platform_and_version_mismatches(self) -> None:
        errors = collect_release_input_errors(
            version="1.3-beta",
            release_mode="internal-test",
            macos_download_url="https://downloads.example.test/ClassInEDBMVP-macOS.zip",
        )
        self.assertTrue(any("three numeric components" in error for error in errors))
        self.assertTrue(any("supplied together" in error for error in errors))
        self.assertTrue(any("does not match dmg" in error for error in errors))

    def test_public_release_rejects_missing_feed_and_artifacts(self) -> None:
        errors = collect_release_input_errors(version="1.3.0", release_mode="public")
        self.assertTrue(any("requires an update feed URL" in error for error in errors))
        self.assertTrue(any("requires a release notes URL" in error for error in errors))
        self.assertTrue(any("requires a release manifest URL" in error for error in errors))
        self.assertTrue(any("requires both platform download URLs" in error for error in errors))
        self.assertTrue(any("license compliance approval" in error for error in errors))


class TestReleaseLicensePolicy(unittest.TestCase):
    def test_release_lock_policy_and_current_environment_are_closed(self) -> None:
        self.assertEqual([], collect_release_policy_errors(PROJECT_ROOT))
        self.assertEqual([], collect_locked_environment_errors(PROJECT_ROOT))

    def test_packaging_sources_reject_unlocked_installed_distributions(self) -> None:
        macos = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        windows = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        spec = (PROJECT_ROOT / "ClassInEDBMVP.spec").read_text(encoding="utf-8")
        self.assertIn("--reject-unlocked-environment", macos)
        self.assertIn("--strict-environment", macos)
        self.assertIn("--reject-unlocked-environment", windows)
        self.assertIn("--strict-environment", windows)
        self.assertIn("reject_unlocked_environment=True", spec)
        self.assertIn("strict_environment=True", spec)

    def test_release_metadata_is_deterministic_and_tamper_evident(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            output = Path(raw_tmp) / "release-metadata"
            result = build_release_metadata(PROJECT_ROOT, output, version="1.3.0")
            self.assertGreaterEqual(result["componentCount"], 20)
            self.assertEqual([], collect_release_metadata_errors(output, expected_version="1.3.0"))

            notice_path = output / "THIRD_PARTY_NOTICES.md"
            notice_path.write_text("tampered\n", encoding="utf-8")
            errors = collect_release_metadata_errors(output, expected_version="1.3.0")

        self.assertTrue(any("SHA-256 mismatch" in error for error in errors))

    def test_release_evidence_requires_exact_platform_artifacts(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            metadata = root / "release-metadata"
            commit = "a" * 40
            build_release_metadata(
                PROJECT_ROOT,
                metadata,
                version="1.3.0",
                git_commit=commit,
            )
            dmg = root / "ClassInEDBMVP-macOS.dmg"
            archive = root / "ClassInEDBMVP-macOS.zip"
            dmg.write_bytes(b"dmg")
            archive.write_bytes(b"zip")
            evidence = create_release_evidence(
                platform_name="macos",
                version="1.3.0",
                git_commit=commit,
                metadata_root=metadata,
                artifacts=[dmg, archive],
            )
            evidence_path = root / "evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            self.assertEqual(
                [],
                collect_release_evidence_errors(
                    evidence_path,
                    root,
                    expected_version="1.3.0",
                    expected_git_commit=commit,
                ),
            )
            dmg.write_bytes(b"changed")
            errors = collect_release_evidence_errors(evidence_path, root)

        self.assertTrue(any("mismatch" in error for error in errors))

    def test_external_upscayl_discovery_needs_no_bundled_license_files(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            self.assertEqual(
                [],
                collect_release_license_errors(Path(raw_tmp), bundle_upscayl=False),
            )

    def test_upscayl_bundle_requires_compliance_files(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            (root / "resources" / "upscayl").mkdir(parents=True)
            errors = collect_release_license_errors(root, bundle_upscayl=True)

        for file_name in UPSCAYL_REQUIRED_COMPLIANCE_FILES:
            self.assertTrue(any(file_name in error for error in errors))

    def test_upscayl_bundle_accepts_nonempty_compliance_files(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            upscayl_root = root / "resources" / "upscayl"
            upscayl_root.mkdir(parents=True)
            for file_name in UPSCAYL_REQUIRED_COMPLIANCE_FILES:
                (upscayl_root / file_name).write_text("reviewed\n", encoding="utf-8")
            (upscayl_root / "win" / "bin").mkdir(parents=True)
            (upscayl_root / "win" / "bin" / "upscayl-bin.exe").write_bytes(b"binary")
            (upscayl_root / "models").mkdir()
            (upscayl_root / "models" / "upscayl-lite-4x.bin").write_bytes(b"model")
            (upscayl_root / "models" / "upscayl-lite-4x.param").write_text("model\n", encoding="utf-8")

            self.assertEqual(
                [],
                collect_release_license_errors(root, bundle_upscayl=True, platform_name="win"),
            )


class TestPackagingSourceHardening(unittest.TestCase):
    def test_packaging_output_cleanup_has_protected_path_guards(self) -> None:
        macos = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        windows_app = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")
        windows_installer = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(encoding="utf-8")

        self.assertIn("Refusing unsafe packaging output directory", macos)
        for source in (windows_app, windows_installer):
            self.assertIn("Assert-EDBSafeOutputDirectory", source)
            self.assertIn("Refusing unsafe packaging output directory", source)
            self.assertIn("outside the exact dist allowlist", source)
            self.assertIn(".edb-packaging-output", source)
        self.assertIn("outside the exact dist allowlist", macos)
        self.assertIn(".edb-packaging-output", macos)

    def test_macos_clean_rejects_git_directory_without_touching_it(self) -> None:
        zsh = shutil.which("zsh")
        if zsh is None:
            self.skipTest("zsh is not available")
        with TemporaryDirectory() as raw_tmp:
            project = Path(raw_tmp) / "project"
            project.mkdir()
            script = project / "package_macos_app.sh"
            shutil.copyfile(PROJECT_ROOT / "package_macos_app.sh", script)
            git_dir = project / ".git"
            git_dir.mkdir()
            sentinel = git_dir / "DO_NOT_DELETE"
            sentinel.write_text("intact\n", encoding="utf-8")

            completed = subprocess.run(
                [zsh, str(script), "--output-dir", ".git", "--clean", "--skip-frontend-build"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("Refusing unsafe packaging output directory", completed.stderr)
            self.assertEqual("intact\n", sentinel.read_text(encoding="utf-8"))

    def test_macos_nonclean_rejects_unmarked_external_output_without_touching_it(self) -> None:
        zsh = shutil.which("zsh")
        if zsh is None:
            self.skipTest("zsh is not available")
        with TemporaryDirectory() as raw_tmp:
            temp_root = Path(raw_tmp)
            project = temp_root / "project"
            project.mkdir()
            script = project / "package_macos_app.sh"
            shutil.copyfile(PROJECT_ROOT / "package_macos_app.sh", script)
            external_output = temp_root / "existing-output"
            external_output.mkdir()
            sentinel = external_output / "DO_NOT_DELETE"
            sentinel.write_text("intact\n", encoding="utf-8")

            completed = subprocess.run(
                [zsh, str(script), "--output-dir", str(external_output), "--skip-frontend-build"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("unmarked external packaging output", completed.stderr)
            self.assertEqual("intact\n", sentinel.read_text(encoding="utf-8"))

    def test_release_metadata_rejects_git_output_without_touching_it(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            project = Path(raw_tmp) / "project"
            git_dir = project / ".git"
            git_dir.mkdir(parents=True)
            sentinel = git_dir / "DO_NOT_DELETE"
            sentinel.write_text("intact\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "inside .git"):
                build_release_metadata(project, git_dir, version="1.3.0")

            self.assertEqual("intact\n", sentinel.read_text(encoding="utf-8"))

    def test_upscayl_bundle_is_explicit_and_license_gated(self) -> None:
        spec = (PROJECT_ROOT / "ClassInEDBMVP.spec").read_text(encoding="utf-8")
        macos = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        windows = (PROJECT_ROOT / "package_mvp.ps1").read_text(encoding="utf-8")

        self.assertIn("EDB_BUNDLE_UPSCAYL", spec)
        self.assertIn("collect_release_license_errors", spec)
        self.assertIn("--bundle-upscayl", macos)
        self.assertIn("verify_release_licenses.py", macos)
        self.assertIn("[switch]$BundleUpscayl", windows)
        self.assertIn("verify_release_licenses.py", windows)

    def test_macos_release_metadata_add_data_uses_unambiguous_zsh_expansion(self) -> None:
        source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn('"${RELEASE_METADATA_DIR}:release_metadata"', source)
        self.assertNotIn('"$RELEASE_METADATA_DIR:release_metadata"', source)

    def test_macos_notarization_validates_signature_runtime_and_tickets(self) -> None:
        source = (PROJECT_ROOT / "package_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn("Authority=Developer ID Application:", source)
        self.assertIn("hardened runtime flag", source)
        self.assertIn('xcrun stapler validate "$APP_PATH"', source)
        self.assertIn('xcrun stapler validate "$DMG_PATH"', source)

    def test_workflow_distinguishes_internal_and_production_releases(self) -> None:
        source = (PROJECT_ROOT / ".github" / "workflows" / "build-installers.yml").read_text(encoding="utf-8")
        self.assertIn("production_release:", source)
        self.assertIn("license_compliance_approved:", source)
        self.assertIn("type: boolean", source)
        self.assertIn("default: true", source)
        self.assertIn("Production release requires MACOS_CERTIFICATE_P12_BASE64", source)
        self.assertIn("Production release requires all Apple notary API key secrets", source)
        self.assertIn("Production release requires WINDOWS_CERTIFICATE_PFX_BASE64", source)
        self.assertIn("scripts/validate_release_inputs.py", source)
        self.assertIn("requirements-release.lock", source)
        self.assertIn("--require-locked-environment", source)

    def test_workflow_smokes_real_packages_and_verifies_exact_evidence(self) -> None:
        source = (PROJECT_ROOT / ".github" / "workflows" / "build-installers.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/smoke_packaged_app.py dist/ClassInEDBMVP.app/Contents/MacOS/ClassInEDBMVP", source)
        self.assertIn("scripts\\smoke_packaged_app.py $portableExe", source)
        self.assertIn("scripts\\smoke_packaged_app.py $installedExe", source)
        self.assertIn("EDB 한글 package smoke", source)
        self.assertIn("포터블 앱 with spaces", source)
        self.assertIn("설치 경로 with spaces", source)
        self.assertIn("$startInfo.ArgumentList.Add($argument)", source)
        self.assertIn("function Assert-NativeCommandSucceeded", source)
        self.assertIn('Assert-NativeCommandSucceeded "Relocated portable launch smoke"', source)
        self.assertIn('Assert-NativeCommandSucceeded "Installed package launch smoke"', source)
        self.assertIn("Refusing unsafe Windows smoke root", source)
        self.assertIn("scripts\\verify_packaged_app.py $installDir", source)
        self.assertIn('Get-ChildItem -LiteralPath $installDir -Filter "unins*.exe"', source)
        self.assertIn('Windows uninstaller left the installed executable behind', source)
        self.assertNotIn("Start-Process -FilePath \"dist\\ClassInEDBMVP-Setup.exe\"", source)
        self.assertIn("scripts/build_release_evidence.py create", source)
        self.assertIn("scripts/build_release_evidence.py verify", source)
        self.assertIn("spctl --assess --type execute", source)
        self.assertIn("Get-AuthenticodeSignature", source)

    def test_workflow_supply_chain_inputs_are_immutable(self) -> None:
        source = (PROJECT_ROOT / ".github" / "workflows" / "build-installers.yml").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"uses:\s+actions/[^@]+@v[0-9]")
        self.assertNotIn("pip install --upgrade pip", source)
        self.assertNotIn("-r requirements-dev.txt", source)
        self.assertIn("--require-hashes -r requirements-release-bootstrap.lock", source)
        self.assertIn("--require-hashes --no-build-isolation -r requirements-release.lock", source)
        self.assertIn("--require-hashes -r requirements-ci.lock", source)
        self.assertIn('$innoVersion = "6.7.1"', source)
        self.assertIn("a0dad33db33099d9cd2b89ac2d08b5d70c589b15118ced3b95f469f044f99950", source)

    def test_workflow_jobs_have_no_duplicate_top_level_keys(self) -> None:
        source = (PROJECT_ROOT / ".github" / "workflows" / "build-installers.yml").read_text(encoding="utf-8")
        jobs_source = source.split("\njobs:\n", 1)[1]
        job_matches = list(re.finditer(r"(?m)^  ([a-z0-9-]+):\s*$", jobs_source))
        self.assertGreaterEqual(len(job_matches), 5)
        for index, match in enumerate(job_matches):
            end = job_matches[index + 1].start() if index + 1 < len(job_matches) else len(jobs_source)
            block = jobs_source[match.end():end]
            keys = re.findall(r"(?m)^    ([A-Za-z][A-Za-z0-9_-]*):", block)
            with self.subTest(job=match.group(1)):
                self.assertEqual(len(keys), len(set(keys)), f"duplicate job keys: {keys}")

    def test_production_installers_require_private_corpus_gate(self) -> None:
        source = (PROJECT_ROOT / ".github" / "workflows" / "build-installers.yml").read_text(encoding="utf-8")
        self.assertIn("private-quality-gate:", source)
        self.assertIn("runs-on: [self-hosted, edb-quality-corpus]", source)
        self.assertIn("EDB_QUALITY_CORPUS_MANIFEST", source)
        self.assertIn("EDB_QUALITY_CORPUS_ROOT", source)
        self.assertIn("scripts/run_quality_corpus.py", source)
        self.assertIn("Install private quality pipeline dependencies in clean venv", source)
        self.assertIn('python -m venv "$venv_root/venv"', source)
        self.assertIn("--reject-unlocked-environment", source)
        self.assertIn("Refusing optional OCR backend distributions", source)
        self.assertIn("EDB_QUALITY_OCR_PROFILE: gemini-3.5-flash", source)
        self.assertIn("EDB_GEMINI_OCR_THINKING_LEVEL: low", source)
        self.assertIn("secrets.EDB_QUALITY_GEMINI_API_KEY", source)
        self.assertIn('--ocr "$EDB_QUALITY_OCR_PROFILE"', source)
        self.assertIn('--python "$QUALITY_PYTHON"', source)
        self.assertIn('"edb-quality-venv."', source)
        self.assertIn("Run synthetic harness smoke (not production evidence)", source)
        installer_gate = "needs: [quality-gate, private-quality-gate]"
        self.assertEqual(2, source.count(installer_gate))
        self.assertEqual(2, source.count("!inputs.production_release || needs.private-quality-gate.result == 'success'"))

    def test_workflow_skips_feed_without_complete_urls_and_records_real_arch(self) -> None:
        source = (PROJECT_ROOT / ".github" / "workflows" / "build-installers.yml").read_text(encoding="utf-8")
        self.assertIn("if: inputs.macos_download_url != '' && inputs.windows_download_url != ''", source)
        self.assertIn('arch=$(uname -m)', source)
        self.assertIn("MACOS_ARTIFACT_ARCH: ${{ needs.macos.outputs.artifact_arch }}", source)
        self.assertIn("WINDOWS_ARTIFACT_ARCH: ${{ needs.windows.outputs.artifact_arch }}", source)
        self.assertNotIn('--version "${{ inputs.version }}"', source)


if __name__ == "__main__":
    unittest.main()
