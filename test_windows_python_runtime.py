from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

import bug_reporting
import preprocess
import upscayl_backend
import user_settings


def _fake_upscayl_installation(root: Path) -> upscayl_backend.UpscaylInstallation:
    binary = root / "upscayl-bin.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"fake")
    models = root / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "upscayl-lite-4x.bin").write_bytes(b"model")
    (models / "upscayl-lite-4x.param").write_text("model", encoding="utf-8")
    return upscayl_backend.UpscaylInstallation(binary, models)


def test_settings_load_utf8_bom_from_hangul_path() -> None:
    with TemporaryDirectory() as raw_tmp:
        runtime_dir = Path(raw_tmp) / "한글 사용자" / "설정"
        runtime_dir.mkdir(parents=True)
        user_settings.settings_path(runtime_dir).write_text(
            json.dumps({"ai_enabled": False, "label": "국어"}, ensure_ascii=False),
            encoding="utf-8-sig",
        )

        loaded = user_settings.load_user_settings(runtime_dir)

    assert loaded == {"ai_enabled": False, "label": "국어"}


def test_settings_invalid_windows_encoded_file_fails_closed() -> None:
    with TemporaryDirectory() as raw_tmp:
        runtime_dir = Path(raw_tmp)
        user_settings.settings_path(runtime_dir).write_bytes(b"{\x81invalid-json")

        loaded = user_settings.load_user_settings(runtime_dir)

    assert loaded == {}


def test_settings_atomic_replace_failure_preserves_previous_file() -> None:
    with TemporaryDirectory() as raw_tmp:
        runtime_dir = Path(raw_tmp)
        user_settings.save_user_settings(runtime_dir, {"gemini_api_key": "original"})

        with patch.object(user_settings.os, "replace", side_effect=PermissionError("locked")):
            try:
                user_settings.save_user_settings(runtime_dir, {"gemini_api_key": "replacement"})
            except PermissionError:
                pass
            else:
                raise AssertionError("replace failure should propagate")

        assert user_settings.load_user_settings(runtime_dir) == {"gemini_api_key": "original"}
        assert list(runtime_dir.glob(".user_settings.json.*.tmp")) == []

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "original-env"}, clear=False),
            patch.object(user_settings.os, "replace", side_effect=PermissionError("locked")),
        ):
            try:
                user_settings.update_api_keys(runtime_dir, gemini_api_key="new-env")
            except PermissionError:
                pass
            else:
                raise AssertionError("replace failure should propagate")
            assert os.environ["GEMINI_API_KEY"] == "original-env"


def test_concurrent_settings_updates_do_not_drop_provider_key() -> None:
    with TemporaryDirectory() as raw_tmp:
        runtime_dir = Path(raw_tmp)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(user_settings.update_api_keys, runtime_dir, gemini_api_key="gemini-key"),
                    executor.submit(user_settings.update_api_keys, runtime_dir, openai_api_key="openai-key"),
                ]
                for future in futures:
                    future.result()

        stored = user_settings.load_user_settings(runtime_dir)

    assert stored["gemini_api_key"] == "gemini-key"
    assert stored["openai_api_key"] == "openai-key"


def test_preprocess_discovers_standard_windows_office_and_browser_installs() -> None:
    with TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp) / "프로그램 파일"
        soffice = root / "LibreOffice" / "program" / "soffice.exe"
        chrome = root / "Google" / "Chrome" / "Application" / "chrome.exe"
        soffice.parent.mkdir(parents=True)
        chrome.parent.mkdir(parents=True)
        soffice.write_bytes(b"exe")
        chrome.write_bytes(b"exe")

        with (
            patch.object(preprocess.sys, "platform", "win32"),
            patch.dict(
                os.environ,
                {"PROGRAMFILES": str(root), "PROGRAMFILES(X86)": "", "LOCALAPPDATA": ""},
                clear=False,
            ),
            patch.object(preprocess.shutil, "which", return_value=None),
        ):
            converters = preprocess._iter_hwp_pdf_converter_commands()
            browsers = preprocess._iter_chrome_pdf_commands()

    assert [str(soffice), "--headless"] in converters
    assert [str(chrome)] in browsers


def test_preprocess_expands_quoted_windows_executable_env_path() -> None:
    with TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp) / "한글 도구"
        executable = root / "rhwp.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"exe")
        configured = '"%EDB_TEST_TOOL_ROOT%\\rhwp.exe"'

        with (
            patch.dict(
                os.environ,
                {"EDB_RHWP": configured, "EDB_TEST_TOOL_ROOT": str(root)},
                clear=False,
            ),
            patch.object(preprocess.shutil, "which", return_value=None),
        ):
            commands = preprocess._iter_rhwp_converter_commands()

    assert [str(executable)] in commands


def test_airun_windows_does_not_stage_unexecutable_batch_shim() -> None:
    with TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        soffice = root / "LibreOffice" / "program" / "soffice.exe"
        soffice.parent.mkdir(parents=True)
        soffice.write_bytes(b"exe")
        target = root / "출력"

        def fake_which(name: str) -> str | None:
            return str(soffice) if name == "soffice" else None

        with (
            patch.object(preprocess.sys, "platform", "win32"),
            patch.object(preprocess.shutil, "which", side_effect=fake_which),
        ):
            env = preprocess._airun_hwp_env(target)

        assert env is None
        shim = target / "_airun_bin" / "libreoffice.cmd"
        assert not shim.exists()


def test_airun_windows_conversion_does_not_inject_batch_shim_env() -> None:
    with TemporaryDirectory() as raw_tmp:
        root = Path(raw_tmp)
        source = root / "시험 자료.hwpx"
        source.write_bytes(b"hwpx")
        target = root / "output"
        target.mkdir()

        def fake_run(command, **kwargs):
            assert "airun-hwp" in Path(command[0]).name
            assert "env" not in kwargs
            return subprocess.CompletedProcess(command, 1, "", "conversion failed")

        with (
            patch.object(preprocess.sys, "platform", "win32"),
            patch.object(preprocess.subprocess, "run", side_effect=fake_run),
        ):
            converted, errors = preprocess._run_hwp_pdf_converter_commands(
                source,
                target,
                [["airun-hwp.exe"]],
                timeout_seconds=5,
            )

    assert converted is None
    assert any("conversion failed" in error for error in errors)


def test_upscayl_discovers_windows_per_user_program_install() -> None:
    with TemporaryDirectory() as raw_tmp:
        local = Path(raw_tmp) / "한글 사용자" / "AppData" / "Local"
        resources = local / "Programs" / "Upscayl" / "resources" / "resources"
        expected = _fake_upscayl_installation(resources / "win" / "bin")
        # Upscayl keeps models beside the platform folders, not beside bin.
        expected.models_dir.rename(resources / "models")
        expected = upscayl_backend.UpscaylInstallation(expected.binary_path, resources / "models")

        with (
            patch.object(upscayl_backend.sys, "platform", "win32"),
            patch.dict(
                os.environ,
                {
                    "LOCALAPPDATA": str(local),
                    "PROGRAMFILES": "",
                    "PROGRAMFILES(X86)": "",
                    "PATH": "",
                    "UPSCAYL_BIN": "",
                    "UPSCAYL_MODELS_DIR": "",
                },
                clear=False,
            ),
            patch.object(upscayl_backend.shutil, "which", return_value=None),
        ):
            discovered = upscayl_backend.discover_upscayl_installation(refresh=True)

    assert discovered == upscayl_backend.UpscaylInstallation(
        expected.binary_path.resolve(), expected.models_dir.resolve()
    )


def test_upscayl_windows_process_is_hidden_and_utf8_tolerant() -> None:
    with TemporaryDirectory() as raw_tmp:
        installation = _fake_upscayl_installation(Path(raw_tmp))
        image = Image.new("RGB", (640, 360), "white")

        def fake_run(command, **kwargs):
            output = Path(command[command.index("-o") + 1])
            Image.new("RGB", (1600, 900), "white").save(output)
            assert kwargs["encoding"] == "utf-8"
            assert kwargs["errors"] == "replace"
            assert kwargs["creationflags"] == 0x08000000
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.object(upscayl_backend.sys, "platform", "win32"),
            patch.object(upscayl_backend.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True),
            patch.object(upscayl_backend.subprocess, "run", side_effect=fake_run),
        ):
            result = upscayl_backend.auto_upscale_image(image, installation=installation)

    assert result.applied


def test_bug_report_redacts_spaced_hangul_windows_and_unc_paths() -> None:
    text = (
        'failed "C:\\Users\\홍길동\\OneDrive - 학교\\시험 자료\\중간 고사.hwp" and '
        "\\\\school-server\\교무실 공유\\시험지\\정답.pdf"
    )

    redacted = bug_reporting.redact_sensitive_text(text)

    assert "홍길동" not in redacted
    assert "학교" not in redacted
    assert "교무실" not in redacted
    assert "중간 고사.hwp" not in redacted
    assert redacted.count("[local-path]") == 2


def test_bug_report_redacts_unquoted_spaced_windows_directory_path() -> None:
    text = (
        "cache failure at C:\\Users\\홍길동\\OneDrive - 학교\\비밀 폴더 "
        "while saving settings"
    )

    redacted = bug_reporting.redact_sensitive_text(text)

    assert "홍길동" not in redacted
    assert "학교" not in redacted
    assert "비밀 폴더" not in redacted
    assert "[local-path]" in redacted


def test_bug_report_reads_utf16_windows_log_and_redacts_path() -> None:
    with TemporaryDirectory() as raw_tmp:
        log_file = Path(raw_tmp) / "windows.log"
        log_file.write_text(
            "한글 오류: C:\\Users\\홍길동\\문서\\시험 문제.hwp 처리 실패",
            encoding="utf-16",
        )

        tail = bug_reporting._read_log_tail(log_file)

    assert "한글 오류" in tail
    assert "홍길동" not in tail
    assert "[local-path]" in tail
