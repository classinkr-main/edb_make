#!/usr/bin/env python3
"""Launch a packaged executable and verify real HTTP startup/diagnostics/shutdown."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


if os.name == "nt":
    from ctypes import wintypes

    class _WindowsIoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _WindowsJobBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]


    class _WindowsJobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _WindowsJobBasicLimitInformation),
            ("IoInfo", _WindowsIoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


    class _WindowsJobBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]


class _WindowsJob:
    """Own a Windows job so every packaged-app descendant is observable and killable."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Windows jobs are only available on Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _WindowsJobExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def _configure_api(self) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_process_count(self) -> int:
        accounting = _WindowsJobBasicAccountingInformation()
        returned_length = wintypes.DWORD()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            _WINDOWS_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            ctypes.byref(returned_length),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(accounting.ActiveProcesses)

    def terminate(self) -> None:
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class _ProcessTreeGuard:
    """Launch and account for a process tree without third-party dependencies."""

    def __init__(self, process: subprocess.Popen[bytes], windows_job: _WindowsJob | None) -> None:
        self.process = process
        self._windows_job = windows_job

    @classmethod
    def launch(
        cls,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout: Any,
    ) -> _ProcessTreeGuard:
        windows_job = _WindowsJob() if os.name == "nt" else None
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                start_new_session=(os.name != "nt"),
            )
            if windows_job is not None:
                try:
                    windows_job.assign(process)
                except Exception:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            return cls(process, windows_job)
        except Exception:
            if windows_job is not None:
                windows_job.close()
            raise

    def _tree_is_empty(self) -> bool:
        if self._windows_job is not None:
            return self._windows_job.active_process_count() == 0
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def wait_for_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._tree_is_empty():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def terminate(self, *, timeout: float = 5.0) -> None:
        if self._tree_is_empty():
            return
        if self._windows_job is not None:
            self._windows_job.terminate()
        else:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        if self.wait_for_empty(timeout):
            if self._windows_job is not None:
                # Job accounting reaches zero just before every inherited file
                # handle is consistently observable as closed by the filesystem.
                time.sleep(0.2)
            return
        if self._windows_job is not None:
            self._windows_job.terminate()
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        if not self.wait_for_empty(timeout):
            raise RuntimeError("could not terminate the packaged app process tree")
        if self._windows_job is not None:
            time.sleep(0.2)

    def close(self) -> None:
        if self._windows_job is not None:
            self._windows_job.close()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    url: str,
    *,
    method: str = "GET",
    timeout: float = 3.0,
    request_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = (
        json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        if request_payload is not None
        else b""
        if method == "POST"
        else None
    )
    headers = {"Accept": "application/json", "Origin": url.split("/api/", 1)[0]}
    if request_payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"{url} returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-4_000:]
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{url} did not return a JSON object")
    return payload


def _bytes_request(url: str, *, timeout: float = 3.0) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "text/html,application/javascript"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return response.read()


def _validate_ui_assets(board_html: str, bundle: str) -> str:
    references = re.findall(
        r'app\.bundle\.js\?v=frontend-bundle-([0-9a-f]{64})',
        board_html,
    )
    if len(references) != 1:
        raise RuntimeError("packaged board.html must reference exactly one cache-busted app.bundle.js")
    digest = references[0]
    source_digests = re.findall(r"Source SHA256:\s*([0-9a-f]{64})", bundle)
    if source_digests != [digest]:
        raise RuntimeError("packaged board.html cache bust does not match the served bundle source digest")
    if "Generated by scripts/build_frontend_bundle.mjs" not in bundle or "/* app.jsx */" not in bundle:
        raise RuntimeError("served app.bundle.js is missing generated bundle content markers")
    return digest


def _validate_update_metadata(payload: dict[str, Any], *, expected_app_id: str = "ClassInEDBMVP") -> None:
    if payload.get("ok") is not True:
        raise RuntimeError(f"update metadata endpoint did not report ok=true: {payload}")
    if payload.get("appId") != expected_app_id:
        raise RuntimeError(
            f"unexpected update metadata appId: expected {expected_app_id!r}, "
            f"found {payload.get('appId')!r}"
        )
    for field in ("currentVersion", "platform", "arch", "channelStatus"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise RuntimeError(f"update metadata is missing non-empty {field}")
    for field in ("configured", "updateAvailable"):
        if not isinstance(payload.get(field), bool):
            raise RuntimeError(f"update metadata {field} must be boolean")


def _smoke_edb_export(base_url: str, source: Path, output_dir: Path) -> dict[str, Any]:
    resolved_source = source.expanduser().resolve()
    if not resolved_source.is_file():
        raise FileNotFoundError(f"EDB smoke source does not exist: {resolved_source}")
    resolved_output = output_dir.resolve()
    session_state = _json_request(f"{base_url}/api/session/latest")
    session_revision = session_state.get("sessionRevision")
    session_epoch = str(session_state.get("sessionEpoch") or "").strip()
    if type(session_revision) is not int or not session_epoch:
        raise RuntimeError(
            f"packaged EDB smoke could not read session concurrency metadata: {session_state}"
        )
    payload = _json_request(
        f"{base_url}/api/export",
        method="POST",
        timeout=180.0,
        request_payload={
            "files": [str(resolved_source)],
            "outputDir": str(resolved_output),
            "inputIntent": "page-as-is",
            "contentTarget": "full-page",
            "ocr": "off",
            "preview": False,
            "exportEdb": True,
            "recordMode": "image-only",
            "edbName": "Windows_Packaged_Smoke.edb",
            "expectedSessionRevision": session_revision,
            "expectedSessionEpoch": session_epoch,
        },
    )
    if payload.get("ok") is not True:
        raise RuntimeError(f"packaged EDB smoke export did not report ok=true: {payload}")
    edb_path = Path(str(payload.get("edbPath") or "")).expanduser().resolve()
    if not edb_path.is_file() or edb_path.suffix.lower() != ".edb":
        raise RuntimeError(f"packaged EDB smoke export did not create an EDB file: {edb_path}")
    if not edb_path.is_relative_to(resolved_output):
        raise RuntimeError(
            f"packaged EDB smoke export escaped the requested output directory: {edb_path}"
        )
    validation = payload.get("edbValidation")
    if not isinstance(validation, dict):
        raise RuntimeError("packaged EDB smoke export omitted server-side validation metadata")
    record_count = int(validation.get("recordCountActual") or 0)
    if record_count < 1:
        raise RuntimeError(f"packaged EDB smoke export contains no validated records: {validation}")
    edb_parts = payload.get("edbParts")
    if not isinstance(edb_parts, list) or not edb_parts:
        raise RuntimeError("packaged EDB smoke export omitted EDB part metadata")
    return {
        "fileName": edb_path.name,
        "bytes": edb_path.stat().st_size,
        "sha256": hashlib.sha256(edb_path.read_bytes()).hexdigest(),
        "recordCount": record_count,
        "partCount": len(edb_parts),
        "pageCountHint": int(validation.get("pageCountHint") or 0),
    }


def smoke_packaged_executable(
    executable: Path,
    *,
    startup_timeout: float = 45.0,
    shutdown_timeout: float = 15.0,
    expected_app_id: str = "ClassInEDBMVP",
    edb_source: Path | None = None,
) -> dict[str, Any]:
    resolved_executable = executable.expanduser().resolve()
    if not resolved_executable.is_file():
        raise FileNotFoundError(f"packaged executable does not exist: {resolved_executable}")
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    command = [
        str(resolved_executable),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-open-browser",
    ]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="edb-packaged-smoke-") as raw_temp:
        temp_root = Path(raw_temp)
        unicode_workspace = temp_root / "한글 경로 with spaces"
        launch_directory = unicode_workspace / "실행 위치"
        app_home = unicode_workspace / "사용자 데이터 ClassIn EDB"
        launch_directory.mkdir(parents=True)
        log_path = unicode_workspace / "packaged app.log"
        environment = dict(os.environ)
        environment["EDB_APP_HOME"] = str(app_home)
        environment.pop("GEMINI_API_KEY", None)
        environment.pop("GOOGLE_API_KEY", None)
        environment.pop("OPENAI_API_KEY", None)
        with log_path.open("wb") as log_handle:
            process_tree = _ProcessTreeGuard.launch(
                command,
                cwd=launch_directory,
                env=environment,
                stdout=log_handle,
            )
            process = process_tree.process
            try:
                health: dict[str, Any] | None = None
                deadline = time.monotonic() + startup_timeout
                while time.monotonic() < deadline:
                    exit_code = process.poll()
                    if exit_code is not None:
                        raise RuntimeError(
                            f"packaged app exited before health check with code {exit_code}"
                        )
                    try:
                        health = _json_request(f"{base_url}/api/health")
                    except (OSError, ValueError, urllib.error.URLError, RuntimeError):
                        time.sleep(0.2)
                        continue
                    break
                if health is None:
                    raise RuntimeError(f"packaged app did not become healthy within {startup_timeout:.1f}s")
                if health.get("ok") is not True or health.get("app") != "ClassIn EDB MVP Local App":
                    raise RuntimeError(f"unexpected packaged health payload: {health}")

                diagnostics = _json_request(f"{base_url}/api/runtime-diagnostics")
                if diagnostics.get("ok") is not True:
                    raise RuntimeError(f"runtime diagnostics did not report ok=true: {diagnostics}")
                ui_bytes = _bytes_request(f"{base_url}/")
                if b"<!doctype html" not in ui_bytes[:512].lower():
                    raise RuntimeError("packaged UI root did not return the expected HTML shell")
                board_bytes = _bytes_request(f"{base_url}/board.html")
                try:
                    board_html = board_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RuntimeError("packaged board.html is not valid UTF-8") from exc
                bundle_match = re.search(
                    r'app\.bundle\.js\?v=frontend-bundle-[0-9a-f]{64}',
                    board_html,
                )
                if bundle_match is None:
                    raise RuntimeError("packaged board.html does not contain a valid bundle reference")
                bundle_bytes = _bytes_request(f"{base_url}/{bundle_match.group(0)}")
                try:
                    bundle = bundle_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RuntimeError("served app.bundle.js is not valid UTF-8") from exc
                frontend_digest = _validate_ui_assets(board_html, bundle)

                update_metadata = _json_request(f"{base_url}/api/app/update", timeout=8.0)
                _validate_update_metadata(update_metadata, expected_app_id=expected_app_id)

                expected_runtime_dirs = (
                    app_home / ".app_runtime",
                    app_home / ".app_runtime" / "uploads",
                    app_home / ".app_runtime" / "outputs",
                )
                missing_runtime_dirs = [str(path) for path in expected_runtime_dirs if not path.is_dir()]
                if missing_runtime_dirs:
                    raise RuntimeError(
                        "packaged app did not initialize its isolated Unicode app home: "
                        + ", ".join(missing_runtime_dirs)
                    )

                edb_export = None
                if edb_source is not None:
                    edb_export = _smoke_edb_export(
                        base_url,
                        edb_source,
                        unicode_workspace / "실제 EDB 출력",
                    )

                shutdown = _json_request(f"{base_url}/api/system/shutdown", method="POST")
                if shutdown.get("ok") is not True:
                    raise RuntimeError(f"shutdown endpoint did not report ok=true: {shutdown}")
                shutdown_deadline = time.monotonic() + shutdown_timeout
                try:
                    exit_code = process.wait(timeout=shutdown_timeout)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        f"packaged app did not stop within {shutdown_timeout:.1f}s"
                    ) from exc
                if exit_code != 0:
                    raise RuntimeError(f"packaged app stopped with non-zero code {exit_code}")
                remaining_shutdown_time = max(0.0, shutdown_deadline - time.monotonic())
                if not process_tree.wait_for_empty(remaining_shutdown_time):
                    raise RuntimeError("packaged app left child processes running after shutdown")
                result = {
                    "executable": str(resolved_executable),
                    "startupSeconds": round(time.monotonic() - started, 3),
                    "health": health,
                    "diagnosticsStatus": "ok",
                    "frontendDigest": frontend_digest,
                    "updateChannelStatus": update_metadata["channelStatus"],
                    "frontendAssets": "ok",
                    "isolatedUnicodeAppHome": True,
                    "cleanShutdown": True,
                    "cleanProcessTree": True,
                }
                if edb_export is not None:
                    result["edbExport"] = edb_export
                return result
            except Exception as exc:
                cleanup_error: Exception | None = None
                try:
                    process_tree.terminate(timeout=5)
                except Exception as terminate_exc:
                    cleanup_error = terminate_exc
                log_handle.flush()
                log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                cleanup_detail = (
                    f"\n--- process-tree cleanup error ---\n{cleanup_error}"
                    if cleanup_error is not None
                    else ""
                )
                raise RuntimeError(
                    f"{exc}{cleanup_detail}\n--- packaged app log tail ---\n{log_tail}"
                ) from exc
            finally:
                process_tree.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a packaged ClassInEDBMVP executable.")
    parser.add_argument("executable", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    parser.add_argument("--shutdown-timeout", type=float, default=15.0)
    parser.add_argument("--expected-app-id", default="ClassInEDBMVP")
    parser.add_argument(
        "--smoke-edb-source",
        type=Path,
        help="Create and validate an image-only EDB from this source before shutdown.",
    )
    args = parser.parse_args(argv)
    try:
        result = smoke_packaged_executable(
            args.executable,
            startup_timeout=args.startup_timeout,
            shutdown_timeout=args.shutdown_timeout,
            expected_app_id=args.expected_app_id,
            edb_source=args.smoke_edb_source,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[packaged-smoke] ERROR: {exc}", file=sys.stderr)
        return 1
    print("[packaged-smoke] OK: " + json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
