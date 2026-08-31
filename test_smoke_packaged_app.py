from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.smoke_packaged_app import _ProcessTreeGuard


class TestPackagedSmokeProcessTree(unittest.TestCase):
    def test_unicode_space_cwd_and_clean_process_exit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="edb-smoke-test-") as raw_temp:
            launch_dir = Path(raw_temp) / "한글 경로 with spaces" / "실행 위치"
            launch_dir.mkdir(parents=True)
            log_path = launch_dir / "프로세스 log.bin"
            with log_path.open("wb") as log_handle:
                tree = _ProcessTreeGuard.launch(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "Path('실행됨.txt').write_text('ok', encoding='utf-8')"
                        ),
                    ],
                    cwd=launch_dir,
                    env=dict(os.environ),
                    stdout=log_handle,
                )
                try:
                    self.assertEqual(0, tree.process.wait(timeout=10))
                    self.assertTrue(tree.wait_for_empty(2))
                finally:
                    tree.terminate(timeout=2)
                    tree.close()

            self.assertEqual("ok", (launch_dir / "실행됨.txt").read_text(encoding="utf-8"))

    def test_lingering_descendant_is_detected_and_terminated(self) -> None:
        child_code = "import time; time.sleep(30)"
        parent_code = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "time.sleep(0.3)"
        )
        with tempfile.TemporaryDirectory(prefix="edb-process-tree-") as raw_temp:
            launch_dir = Path(raw_temp)
            log_path = launch_dir / "tree.log"
            with log_path.open("wb") as log_handle:
                tree = _ProcessTreeGuard.launch(
                    [sys.executable, "-c", parent_code],
                    cwd=launch_dir,
                    env=dict(os.environ),
                    stdout=log_handle,
                )
                try:
                    self.assertEqual(0, tree.process.wait(timeout=10))
                    self.assertFalse(
                        tree.wait_for_empty(0.2),
                        "the guard must not mistake parent exit for whole-tree exit",
                    )
                    tree.terminate(timeout=3)
                    self.assertTrue(tree.wait_for_empty(2))
                finally:
                    tree.terminate(timeout=3)
                    tree.close()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object behavior is Windows-specific")
    def test_windows_job_close_terminates_assigned_process(self) -> None:
        with tempfile.TemporaryDirectory(prefix="edb-job-test-") as raw_temp:
            launch_dir = Path(raw_temp)
            with (launch_dir / "job.log").open("wb") as log_handle:
                tree = _ProcessTreeGuard.launch(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=launch_dir,
                    env=dict(os.environ),
                    stdout=log_handle,
                )
                job_closed = False
                try:
                    self.assertIsNotNone(tree._windows_job)
                    self.assertGreaterEqual(tree._windows_job.active_process_count(), 1)
                    tree.close()
                    job_closed = True
                    tree.process.wait(timeout=3)
                finally:
                    if not job_closed:
                        tree.terminate(timeout=3)
                        tree.close()
            self.assertIsNotNone(tree.process.poll())


if __name__ == "__main__":
    unittest.main()
