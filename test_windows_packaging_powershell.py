from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parent
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
WINDOWS_POWERSHELL = shutil.which("powershell")


def _ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(POWERSHELL, "PowerShell is not available")
class TestWindowsPackagingPowerShell(unittest.TestCase):
    def _run(self, command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )

    def test_explicit_missing_signtool_path_fails_closed(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            missing = Path(raw_tmp) / "missing-signtool.exe"
            completed = self._run(
                "$ErrorActionPreference = 'Stop'; "
                f". {_ps_quote(PROJECT_ROOT / 'scripts' / 'Sign-WindowsArtifact.ps1')}; "
                f"Find-EDBSignTool -Requested {_ps_quote(missing)}"
            )

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "Requested signtool.exe was not found",
            completed.stdout + completed.stderr,
        )

    def test_explicit_signtool_path_is_resolved_literally(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tool_dir = Path(raw_tmp) / "sdk[release]"
            tool_dir.mkdir()
            tool = tool_dir / "signtool.exe"
            tool.write_bytes(b"test tool placeholder")
            completed = self._run(
                "$ErrorActionPreference = 'Stop'; "
                f". {_ps_quote(PROJECT_ROOT / 'scripts' / 'Sign-WindowsArtifact.ps1')}; "
                f"Find-EDBSignTool -Requested {_ps_quote(tool)}"
            )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(str(tool.resolve()).casefold(), completed.stdout.strip().casefold())

    def test_explicit_inno_compiler_directory_fails_closed(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            directory = Path(raw_tmp) / "ISCC.exe"
            directory.mkdir()
            script_path = PROJECT_ROOT / "package_windows_installer.ps1"
            command = f"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {_ps_quote(script_path)},
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {{ throw ($parseErrors | Out-String) }}
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Find-InnoSetupCompiler'
}}, $true)
if (-not $functionAst) {{ throw 'Find-InnoSetupCompiler was not found' }}
Invoke-Expression $functionAst.Extent.Text
Find-InnoSetupCompiler -Requested {_ps_quote(directory)}
"""
            completed = self._run(command)

        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "Requested Inno Setup compiler was not found",
            completed.stdout + completed.stderr,
        )

    def test_explicit_inno_compiler_file_with_wildcard_chars_is_valid(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            tool_dir = Path(raw_tmp) / "inno[release]"
            tool_dir.mkdir()
            compiler = tool_dir / "ISCC.exe"
            compiler.write_bytes(b"test compiler placeholder")
            script_path = PROJECT_ROOT / "package_windows_installer.ps1"
            command = f"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {_ps_quote(script_path)},
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {{ throw ($parseErrors | Out-String) }}
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Find-InnoSetupCompiler'
}}, $true)
Invoke-Expression $functionAst.Extent.Text
Find-InnoSetupCompiler -Requested {_ps_quote(compiler)}
"""
            completed = self._run(command)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            str(compiler.resolve()).casefold(),
            completed.stdout.strip().casefold(),
        )

    def test_packaged_metadata_path_is_resolved_literally(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            package_root = Path(raw_tmp) / "dist[release]" / "ClassInEDBMVP"
            package_root.mkdir(parents=True)
            (package_root / "app_update_config.json").write_text(
                '{"version":"1.2.3"}',
                encoding="utf-8",
            )
            script_path = PROJECT_ROOT / "package_windows_installer.ps1"
            command = f"""
$ErrorActionPreference = 'Stop'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    {_ps_quote(script_path)},
    [ref]$tokens,
    [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) {{ throw ($parseErrors | Out-String) }}
$functionAst = $ast.Find({{
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Read-PackagedUpdateConfig'
}}, $true)
Invoke-Expression $functionAst.Extent.Text
(Read-PackagedUpdateConfig -PackageRoot {_ps_quote(package_root)}).version
"""
            completed = self._run(command)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("1.2.3", completed.stdout.strip())

    @unittest.skipUnless(WINDOWS_POWERSHELL, "Windows PowerShell 5.1 is not available")
    def test_scripts_parse_with_windows_powershell_51(self) -> None:
        scripts = [
            PROJECT_ROOT / "package_windows_installer.ps1",
            PROJECT_ROOT / "scripts" / "Sign-WindowsArtifact.ps1",
        ]
        for script in scripts:
            command = (
                "$tokens = $null; $errors = $null; "
                "[System.Management.Automation.Language.Parser]::ParseFile("
                f"{_ps_quote(script)}, [ref]$tokens, [ref]$errors) | Out-Null; "
                "if ($errors.Count -ne 0) { $errors | Out-String | Write-Error; exit 1 }"
            )
            completed = subprocess.run(
                [
                    str(WINDOWS_POWERSHELL),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)


class TestWindowsInstallerUpgradePolicy(unittest.TestCase):
    def test_upgrade_replaces_full_internal_payload_and_preserves_external_state(self) -> None:
        installer = (
            PROJECT_ROOT / "installer" / "windows" / "ClassInEDBMVP.iss"
        ).read_text(encoding="utf-8")
        payload_delete = 'Type: filesandordirs; Name: "{app}\\_internal"'

        self.assertIn("CloseApplications=yes", installer)
        self.assertIn(payload_delete, installer)
        self.assertLess(
            installer.index(payload_delete),
            installer.index('Name: "{app}\\_internal\\.app_runtime"'),
        )
        install_delete = installer.split("[InstallDelete]", 1)[1].split("[Icons]", 1)[0]
        self.assertNotIn("{userdocs}", install_delete)
        self.assertNotIn("{localappdata}", install_delete)

        documentation = (PROJECT_ROOT / "PACKAGING_MVP.md").read_text(encoding="utf-8")
        self.assertIn("replacing the complete PyInstaller `_internal` payload", documentation)
        self.assertIn("remain untouched", documentation)
        self.assertIn("Windows Documents known folder", documentation)

    def test_installer_artifact_cleanup_uses_literal_path(self) -> None:
        source = (PROJECT_ROOT / "package_windows_installer.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "Test-Path -LiteralPath $InstallerPath -PathType Leaf",
            source,
        )
        self.assertIn("Remove-Item -Force -LiteralPath $InstallerPath", source)


if __name__ == "__main__":
    unittest.main()
