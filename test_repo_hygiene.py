from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.clean_local_artifacts import (
    CleanupCandidate,
    CleanupSafetyError,
    collect_cleanup_candidates,
    main as cleanup_main,
    remove_candidate,
)


PROJECT_ROOT = Path(__file__).resolve().parent


def _init_cleanup_repo(root: Path) -> None:
    (root / ".gitignore").write_text(
        "/dist*\n/build/\n/tmp_validation_*/\n/generated_edb_pair*/\n/.app_runtime/\n"
        "ui_prototype/generated_session.js\nui_prototype/prototype_data.js\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["git", "init", "-q"], cwd=root, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


class TestGeneratedArtifactIgnores(unittest.TestCase):
    def test_repository_does_not_track_ignored_files(self) -> None:
        result = subprocess.run(
            ["git", "ls-files", "-ci", "--exclude-standard"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        existing_ignored_tracked_paths = [
            relative_path
            for relative_path in result.stdout.splitlines()
            if (PROJECT_ROOT / relative_path).exists()
            or (PROJECT_ROOT / relative_path).is_symlink()
        ]
        self.assertEqual([], existing_ignored_tracked_paths)

    def test_legacy_standalone_openai_image_backend_is_removed(self) -> None:
        self.assertFalse(
            (PROJECT_ROOT / "openai_image_backend.py").exists(),
            "OpenAI image reconstruction lives in image_reconstruction_backend.py",
        )

    def test_local_run_artifacts_are_ignored(self) -> None:
        samples = [
            ".DS_Store",
            ".claude/settings.local.json",
            "pipeline_output_worker99_img1/pages.json",
            "output/edb/private-session/ui_session.json",
            "ffffffffffffffffffffffffffffffffffffffff_img_sample_deadbeef/pages.json",
            f"{'a' * 40}_legacy_session_deadbeef00/pages.json",
            "generated_edb_pair_future_20990101/page_as_is/classin_handoff.json",
            "dist_browser_home/.app_runtime/latest_session.json",
            "tmp_validation_future/ClassInEDBMVP/ui_prototype/app.bundle.js",
        ]
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            input=("\n".join(samples) + "\n").encode("utf-8"),
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(set(samples), set(result.stdout.decode("utf-8").splitlines()))

    def test_local_cleanup_defaults_target_stale_package_outputs_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for name in (
                "dist",
                "dist_sizecheck",
                "build",
                "tmp_validation_future",
                "generated_edb_pair_future_20990101",
                ".app_runtime",
                "ui_prototype",
            ):
                (root / name).mkdir()

            candidates = collect_cleanup_candidates(root)
            names = {candidate.path.name for candidate in candidates}

        self.assertEqual({"build", "dist", "dist_sizecheck", "tmp_validation_future"}, names)

    def test_local_cleanup_defaults_target_legacy_ui_bridge_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            _init_cleanup_repo(root)
            runtime = root / ".app_runtime"
            runtime.mkdir()
            (runtime / "generated_session.js").write_text("window.EDB_UI_SESSION = stale;\n", encoding="utf-8")
            (runtime / "latest_session.json").write_text('{"problems":[]}\n', encoding="utf-8")
            ui_root = root / "ui_prototype"
            ui_root.mkdir()
            (ui_root / "generated_session.js").write_text("window.EDB_UI_SESSION = stale;\n", encoding="utf-8")
            (ui_root / "prototype_data.js").write_text("window.PROTOTYPE_DATA = stale;\n", encoding="utf-8")

            candidates = collect_cleanup_candidates(root)
            relative_paths = {candidate.path.relative_to(root).as_posix() for candidate in candidates}
            categories = {candidate.category for candidate in candidates}
            for candidate in candidates:
                remove_candidate(root, candidate)

            self.assertEqual(
                {
                    "ui_prototype/generated_session.js",
                    "ui_prototype/prototype_data.js",
                },
                relative_paths,
            )
            self.assertEqual({"legacy-ui"}, categories)
            self.assertTrue((runtime / "latest_session.json").exists())
            self.assertTrue(runtime.exists())
            self.assertTrue(ui_root.exists())
            self.assertTrue((runtime / "generated_session.js").exists())
            self.assertFalse((ui_root / "generated_session.js").exists())
            self.assertFalse((ui_root / "prototype_data.js").exists())

    def test_local_cleanup_can_opt_into_generated_exports(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            for name in ("dist", "generated_edb_pair_future_20990101", ".app_runtime"):
                (root / name).mkdir()

            candidates = collect_cleanup_candidates(root, include_edb_exports=True)
            names = {candidate.path.name for candidate in candidates}

        self.assertEqual({"dist", "generated_edb_pair_future_20990101"}, names)

    def test_local_cleanup_refuses_runtime_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            runtime = root / ".app_runtime"
            runtime.mkdir()
            (runtime / "generated_session.js").write_text("window.EDB_UI_SESSION = stale;\n", encoding="utf-8")

            with self.assertRaises(CleanupSafetyError):
                collect_cleanup_candidates(root, include_runtime=True)

            self.assertTrue(runtime.exists())

    def test_local_cleanup_removes_only_root_child_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp)
            _init_cleanup_repo(root)
            dist_dir = root / "dist_sizecheck"
            nested_file = dist_dir / "ClassInEDBMVP.app" / "Contents" / "Info.plist"
            nested_file.parent.mkdir(parents=True)
            nested_file.write_text("old app", encoding="utf-8")
            keep_dir = root / "generated_edb_pair_future_20990101"
            keep_dir.mkdir()

            [candidate] = collect_cleanup_candidates(root)
            remove_candidate(root, candidate)

            self.assertFalse(dist_dir.exists())
            self.assertTrue(keep_dir.exists())

    def test_local_cleanup_rejects_arbitrary_nested_file_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            _init_cleanup_repo(root)
            nested_file = root / "dist_sizecheck" / "ClassInEDBMVP.app" / "Contents" / "Info.plist"
            nested_file.parent.mkdir(parents=True)
            nested_file.write_text("old app", encoding="utf-8")

            with self.assertRaises(ValueError):
                remove_candidate(root, CleanupCandidate(path=nested_file, category="packaging"))

            self.assertTrue(nested_file.exists())

    def test_local_cleanup_rejects_non_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            dist_dir = root / "dist"
            dist_dir.mkdir()

            with self.assertRaises(CleanupSafetyError):
                remove_candidate(root, CleanupCandidate(path=dist_dir, category="packaging"))

            self.assertTrue(dist_dir.exists())

    def test_local_cleanup_rejects_tracked_content_inside_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            _init_cleanup_repo(root)
            tracked_file = root / "dist" / "keep.txt"
            tracked_file.parent.mkdir()
            tracked_file.write_text("keep", encoding="utf-8")
            add_result = subprocess.run(
                ["git", "add", "-f", "--", "dist/keep.txt"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, add_result.returncode, add_result.stderr)

            with self.assertRaises(CleanupSafetyError):
                remove_candidate(
                    root,
                    CleanupCandidate(path=tracked_file.parent, category="packaging"),
                )

            self.assertTrue(tracked_file.exists())

    def test_local_cleanup_rejects_nonignored_untracked_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            _init_cleanup_repo(root)
            source_dir = root / "build_notes"
            source_dir.mkdir()
            (root / ".gitignore").write_text("/dist*/\n", encoding="utf-8")

            with self.assertRaises(CleanupSafetyError):
                remove_candidate(
                    root,
                    CleanupCandidate(path=source_dir, category="packaging"),
                )

            self.assertTrue(source_dir.exists())

    def test_local_cleanup_rejects_forged_category_or_protected_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            _init_cleanup_repo(root)
            source_file = root / "notes.txt"
            source_file.write_text("keep", encoding="utf-8")
            venv_dir = root / ".venv"
            venv_dir.mkdir()
            runtime_dir = root / ".app_runtime"
            runtime_dir.mkdir()

            with self.assertRaises(CleanupSafetyError):
                remove_candidate(
                    root,
                    CleanupCandidate(path=source_file, category="packaging"),
                )
            with self.assertRaises(CleanupSafetyError):
                remove_candidate(
                    root,
                    CleanupCandidate(path=venv_dir, category="packaging"),
                )
            with self.assertRaises(CleanupSafetyError):
                remove_candidate(
                    root,
                    CleanupCandidate(path=runtime_dir, category="runtime"),
                )

            self.assertTrue(source_file.exists())
            self.assertTrue(venv_dir.exists())
            self.assertTrue(runtime_dir.exists())

    def test_local_cleanup_is_dry_run_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = Path(raw_tmp).resolve()
            _init_cleanup_repo(root)
            dist_dir = root / "dist"
            dist_dir.mkdir()

            self.assertEqual(0, cleanup_main(["--root", str(root)]))
            self.assertTrue(dist_dir.exists())


if __name__ == "__main__":
    unittest.main()
