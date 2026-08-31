from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parent
LAUNCHER = PROJECT_ROOT / "run_local_app.ps1"


class TestWindowsLauncher(unittest.TestCase):
    def test_launcher_isolates_dependency_install_and_checks_python(self) -> None:
        source = LAUNCHER.read_text(encoding="utf-8")

        self.assertIn('Get-Command "py.exe"', source)
        self.assertIn('@("-m", "venv", $VenvDir)', source)
        self.assertIn("sys.version_info >= (3, 11)", source)
        self.assertIn('-Label "Dependency installation"', source)
        self.assertIn('$env:PYTHONUTF8 = "1"', source)

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher integration is Windows-only")
    def test_launcher_stops_when_python_version_check_fails(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            self.skipTest("PowerShell executable not found")

        with TemporaryDirectory() as raw_tmp:
            fake_python = Path(raw_tmp) / "old-python.cmd"
            fake_python.write_text(
                "@echo off\r\n"
                'if "%1"=="-c" exit /b 5\r\n'
                "exit /b 0\r\n",
                encoding="ascii",
            )
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(LAUNCHER),
                    "-NoBrowser",
                    "-PythonExe",
                    str(fake_python),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        output = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode, output)
        self.assertIn("Python 3.11+ version check failed with exit code 5", output)
        self.assertNotIn("Launching local MVP app", output)

    @unittest.skipUnless(os.name == "nt", "PowerShell launcher integration is Windows-only")
    def test_launcher_preserves_app_failure_exit_code(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            self.skipTest("PowerShell executable not found")

        with TemporaryDirectory() as raw_tmp:
            fake_python = Path(raw_tmp) / "fake-python.cmd"
            fake_python.write_text(
                "@echo off\r\n"
                'if "%1"=="-c" exit /b 0\r\n'
                "exit /b 7\r\n",
                encoding="ascii",
            )
            completed = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(LAUNCHER),
                    "-NoBrowser",
                    "-PythonExe",
                    str(fake_python),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )

        self.assertEqual(7, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("Local app exited with code 7", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
