#!/usr/bin/env python3
"""Optional local Upscayl Lite backend used transparently by stage 3.

The backend is deliberately fail-open: an unavailable binary, incompatible
GPU, timeout, or malformed output returns the original image so stage 3 can
continue with its existing Lanczos/sharpen pipeline.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from PIL import Image


DEFAULT_UPSCAYL_MODEL = "upscayl-lite-4x"
DEFAULT_TARGET_WIDTH_PX = 1600
DEFAULT_MAX_SOURCE_WIDTH_PX = 900
DEFAULT_MAX_OUTPUT_PIXELS = 16_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_FAILURE_BACKOFF_SECONDS = 30.0
MAX_FAILURE_BACKOFF_SECONDS = 300.0
DEFAULT_NEGATIVE_DISCOVERY_TTL_SECONDS = 30.0

_UPSCAYL_RUN_LOCK = threading.Semaphore(1)
_UPSCAYL_DISCOVERY_LOCK = threading.RLock()
_UPSCAYL_FAILURE_LOCK = threading.RLock()


@dataclass(slots=True)
class _UpscaylFailureState:
    reason: str
    failure_count: int
    retry_at: float


_UPSCAYL_FAILURES: dict[tuple[str, str, str], _UpscaylFailureState] = {}
_UPSCAYL_NEGATIVE_DISCOVERY_AT: dict[tuple[str, ...], float] = {}


@dataclass(frozen=True, slots=True)
class UpscaylInstallation:
    binary_path: Path
    models_dir: Path
    model: str = DEFAULT_UPSCAYL_MODEL


@dataclass(slots=True)
class UpscaylAutoResult:
    image: Image.Image
    status: str
    reason: str
    source_width: int
    output_width: int
    latency_ms: int = 0
    binary_path: Path | None = None
    model: str = DEFAULT_UPSCAYL_MODEL
    cooldown_remaining_ms: int = 0

    @property
    def applied(self) -> bool:
        return self.status == "applied"

    def to_metadata(self) -> dict[str, Any]:
        fallback_message = _fallback_message(self.reason) if not self.applied else None
        return {
            "status": self.status,
            "reason": self.reason,
            "reason_code": _reason_code(self.reason),
            "source_width": self.source_width,
            "output_width": self.output_width,
            "latency_ms": self.latency_ms,
            "processing_time_ms": self.latency_ms,
            "binary_path": str(self.binary_path) if self.binary_path else None,
            "model": self.model,
            "fallback_applied": not self.applied,
            "fallback_message": fallback_message,
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
        }


def _env_enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _reason_code(reason: str) -> str:
    normalized = (reason or "unknown").split(":", 1)[0]
    if normalized == "temporary_backoff":
        nested = (reason or "").split(":", 2)
        return nested[1] if len(nested) > 1 and nested[1] else normalized
    return normalized


def _fallback_message(reason: str) -> str:
    messages = {
        "disabled": "Neural upscaling is disabled; the original enhancement path was used.",
        "invalid_source_size": "The source image size was invalid, so the original image was kept.",
        "source_already_large": "The source is already large enough; neural upscaling was skipped.",
        "target_already_met": "The requested output width is already met.",
        "output_pixel_limit": "Neural upscaling was skipped to stay within the safe pixel limit.",
        "installation_not_found": "Upscayl is not installed or its model files could not be found.",
        "timeout": "Upscayl timed out; the original enhancement path was used.",
        "process_failed": "Upscayl could not process this image; the original enhancement path was used.",
        "invalid_output_size": "Upscayl returned an unsafe output size, so the original image was kept.",
        "runtime_error": "Upscayl was unavailable at runtime; the original enhancement path was used.",
    }
    code = _reason_code(reason)
    if (reason or "").startswith("temporary_backoff:"):
        base = messages.get(code, "Upscayl is temporarily unavailable.")
        return f"{base} Repeated attempts are paused briefly."
    return messages.get(code, "The original enhancement path was used.")


def _platform_resource_name() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


def _binary_filename() -> str:
    return "upscayl-bin.exe" if sys.platform.startswith("win") else "upscayl-bin"


def _configured_path(value: str | Path) -> Path:
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    text = re.sub(
        r"%([^%]+)%",
        lambda match: os.environ.get(match.group(1), match.group(0)),
        text,
    )
    return Path(os.path.expandvars(os.path.expanduser(text)))


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _subprocess_platform_kwargs() -> dict[str, int]:
    if not sys.platform.startswith("win"):
        return {}
    creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"creationflags": creation_flags} if creation_flags else {}


def _runtime_roots() -> list[Path]:
    roots = [Path(__file__).resolve().parent]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(str(frozen_root)))
    executable = Path(sys.executable).resolve()
    roots.extend([executable.parent, executable.parent.parent])
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = _path_identity(root)
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def _models_near_binary(binary: Path) -> Iterable[Path]:
    parents = list(binary.parents)
    candidates = [
        binary.parent / "models",
        binary.parent.parent / "models" if len(parents) >= 2 else binary.parent / "models",
        binary.parent.parent.parent / "models" if len(parents) >= 3 else binary.parent / "models",
    ]
    yield from candidates


def _candidate_installations(
    env_binary: str,
    env_models: str,
    path_env: str,
    local_app_data: str = "",
    program_files: str = "",
    program_files_x86: str = "",
) -> Iterable[tuple[Path, Path]]:
    binary_name = _binary_filename()
    platform_name = _platform_resource_name()

    if env_binary:
        binary = _configured_path(env_binary)
        if env_models:
            yield binary, _configured_path(env_models)
        else:
            for models in _models_near_binary(binary):
                yield binary, models

    for root in _runtime_roots():
        for resource_root in (
            root / "resources" / "upscayl",
            root / "upscayl",
            root / "resources",
        ):
            yield (
                resource_root / platform_name / "bin" / binary_name,
                resource_root / "models",
            )

    path_binary = shutil.which(binary_name, path=path_env or None)
    if path_binary:
        binary = Path(path_binary)
        if env_models:
            yield binary, _configured_path(env_models)
        for models in _models_near_binary(binary):
            yield binary, models

    if sys.platform == "darwin":
        for applications in (Path("/Applications"), Path.home() / "Applications"):
            resource_root = applications / "Upscayl.app" / "Contents" / "Resources" / "resources"
            yield resource_root / "mac" / "bin" / binary_name, resource_root / "models"
    elif sys.platform.startswith("win"):
        for env_name, base in (
            ("LOCALAPPDATA", local_app_data),
            ("PROGRAMFILES", program_files),
            ("PROGRAMFILES(X86)", program_files_x86),
        ):
            if not base:
                continue
            base_path = _configured_path(base)
            for app_name in ("Upscayl", "upscayl"):
                roots = [base_path / app_name / "resources" / "resources"]
                if env_name == "LOCALAPPDATA":
                    roots.append(base_path / "Programs" / app_name / "resources" / "resources")
                for resource_root in roots:
                    yield resource_root / "win" / "bin" / binary_name, resource_root / "models"
    else:
        for app_root in (Path("/opt/Upscayl"), Path("/opt/upscayl"), Path.home() / ".local" / "opt" / "upscayl"):
            resource_root = app_root / "resources" / "resources"
            yield resource_root / "linux" / "bin" / binary_name, resource_root / "models"


def _valid_installation(binary: Path, models_dir: Path, model: str) -> UpscaylInstallation | None:
    try:
        binary = _configured_path(binary).resolve()
        models_dir = _configured_path(models_dir).resolve()
    except OSError:
        return None
    if not binary.is_file() or not models_dir.is_dir():
        return None
    if not (models_dir / f"{model}.bin").is_file():
        return None
    if not (models_dir / f"{model}.param").is_file():
        return None
    return UpscaylInstallation(binary_path=binary, models_dir=models_dir, model=model)


@lru_cache(maxsize=16)
def _discover_cached(
    env_binary: str,
    env_models: str,
    path_env: str,
    local_app_data: str,
    program_files: str,
    program_files_x86: str,
    model: str,
) -> UpscaylInstallation | None:
    seen: set[tuple[str, str]] = set()
    for binary, models_dir in _candidate_installations(
        env_binary,
        env_models,
        path_env,
        local_app_data,
        program_files,
        program_files_x86,
    ):
        key = (_path_identity(binary), _path_identity(models_dir))
        if key in seen:
            continue
        seen.add(key)
        installation = _valid_installation(binary, models_dir, model)
        if installation is not None:
            return installation
    return None


def clear_upscayl_discovery_cache() -> None:
    with _UPSCAYL_DISCOVERY_LOCK:
        _discover_cached.cache_clear()
        _UPSCAYL_NEGATIVE_DISCOVERY_AT.clear()


def clear_upscayl_failure_backoff() -> None:
    with _UPSCAYL_FAILURE_LOCK:
        _UPSCAYL_FAILURES.clear()


def clear_upscayl_runtime_cache() -> None:
    """Refresh installation discovery and forget temporary runtime failures."""

    clear_upscayl_discovery_cache()
    clear_upscayl_failure_backoff()


def discover_upscayl_installation(
    *,
    model: str = DEFAULT_UPSCAYL_MODEL,
    refresh: bool = False,
) -> UpscaylInstallation | None:
    """Find Upscayl once per environment signature.

    ``refresh=True`` supports installers or model downloads that add packaged
    resources while the desktop process is already running. Changes to the
    explicit binary/model/PATH environment values naturally use a new key.
    """

    with _UPSCAYL_DISCOVERY_LOCK:
        discovery_key = (
            os.environ.get("UPSCAYL_BIN", "").strip(),
            os.environ.get("UPSCAYL_MODELS_DIR", "").strip(),
            os.environ.get("PATH", ""),
            os.environ.get("LOCALAPPDATA", "").strip(),
            os.environ.get("PROGRAMFILES", "").strip(),
            os.environ.get("PROGRAMFILES(X86)", "").strip(),
            model,
        )
        if refresh:
            _discover_cached.cache_clear()
            _UPSCAYL_NEGATIVE_DISCOVERY_AT.clear()
        negative_checked_at = _UPSCAYL_NEGATIVE_DISCOVERY_AT.get(discovery_key)
        if negative_checked_at is not None:
            negative_ttl = _env_float(
                "EDB_UPSCAYL_NEGATIVE_DISCOVERY_TTL_SECONDS",
                DEFAULT_NEGATIVE_DISCOVERY_TTL_SECONDS,
                minimum=0.0,
                maximum=300.0,
            )
            if time.monotonic() - negative_checked_at >= negative_ttl:
                # functools.lru_cache has no per-key eviction. Discovery has a
                # tiny key space, so clearing it is cheaper than hiding a newly
                # installed binary for the lifetime of the desktop process.
                _discover_cached.cache_clear()
                _UPSCAYL_NEGATIVE_DISCOVERY_AT.pop(discovery_key, None)
        installation = _discover_cached(*discovery_key)
        if installation is None:
            _UPSCAYL_NEGATIVE_DISCOVERY_AT.setdefault(discovery_key, time.monotonic())
        else:
            _UPSCAYL_NEGATIVE_DISCOVERY_AT.pop(discovery_key, None)
        return installation


def _installation_key(installation: UpscaylInstallation) -> tuple[str, str, str]:
    return (
        str(installation.binary_path),
        str(installation.models_dir),
        installation.model,
    )


def _failure_backoff_status(
    installation: UpscaylInstallation,
) -> tuple[_UpscaylFailureState | None, int]:
    key = _installation_key(installation)
    now = time.monotonic()
    with _UPSCAYL_FAILURE_LOCK:
        state = _UPSCAYL_FAILURES.get(key)
        if state is None or state.retry_at <= now:
            return state, 0
        remaining_ms = max(1, int(round((state.retry_at - now) * 1000.0)))
        return state, remaining_ms


def _record_failure(installation: UpscaylInstallation, reason: str) -> int:
    key = _installation_key(installation)
    base_seconds = _env_float(
        "EDB_UPSCAYL_FAILURE_BACKOFF_SECONDS",
        DEFAULT_FAILURE_BACKOFF_SECONDS,
        minimum=0.0,
        maximum=MAX_FAILURE_BACKOFF_SECONDS,
    )
    now = time.monotonic()
    with _UPSCAYL_FAILURE_LOCK:
        previous = _UPSCAYL_FAILURES.get(key)
        failure_count = (previous.failure_count + 1) if previous else 1
        delay_seconds = min(
            MAX_FAILURE_BACKOFF_SECONDS,
            base_seconds * (2 ** min(failure_count - 1, 4)),
        )
        _UPSCAYL_FAILURES[key] = _UpscaylFailureState(
            reason=_reason_code(reason),
            failure_count=failure_count,
            retry_at=now + delay_seconds,
        )
    return int(round(delay_seconds * 1000.0))


def _record_success(installation: UpscaylInstallation) -> None:
    with _UPSCAYL_FAILURE_LOCK:
        _UPSCAYL_FAILURES.pop(_installation_key(installation), None)


def auto_upscale_eligible(
    image: Image.Image,
    *,
    target_width: int = DEFAULT_TARGET_WIDTH_PX,
    max_source_width: int = DEFAULT_MAX_SOURCE_WIDTH_PX,
    max_output_pixels: int = DEFAULT_MAX_OUTPUT_PIXELS,
) -> tuple[bool, str]:
    if not _env_enabled("EDB_AUTO_UPSCAYL", True):
        return False, "disabled"
    width, height = image.size
    if width <= 0 or height <= 0:
        return False, "invalid_source_size"
    if width >= max_source_width:
        return False, "source_already_large"
    if width >= target_width:
        return False, "target_already_met"
    target_height = max(1, round(height * target_width / width))
    if target_width * target_height > max_output_pixels:
        return False, "output_pixel_limit"
    return True, "low_resolution_source"


def _unchanged_result(
    image: Image.Image,
    *,
    status: str,
    reason: str,
    started_at: float | None = None,
    installation: UpscaylInstallation | None = None,
    cooldown_remaining_ms: int = 0,
) -> UpscaylAutoResult:
    latency_ms = 0
    if started_at is not None:
        latency_ms = int(round(max(0.0, time.perf_counter() - started_at) * 1000.0))
    return UpscaylAutoResult(
        image=image,
        status=status,
        reason=reason,
        source_width=image.width,
        output_width=image.width,
        latency_ms=latency_ms,
        binary_path=installation.binary_path if installation else None,
        model=installation.model if installation else DEFAULT_UPSCAYL_MODEL,
        cooldown_remaining_ms=cooldown_remaining_ms,
    )


def auto_upscale_image(
    image: Image.Image,
    *,
    installation: UpscaylInstallation | None = None,
    target_width: int = DEFAULT_TARGET_WIDTH_PX,
    max_source_width: int = DEFAULT_MAX_SOURCE_WIDTH_PX,
    max_output_pixels: int = DEFAULT_MAX_OUTPUT_PIXELS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> UpscaylAutoResult:
    """Try Upscayl Lite for an undersized image and otherwise return it unchanged."""

    started = time.perf_counter()
    eligible, reason = auto_upscale_eligible(
        image,
        target_width=target_width,
        max_source_width=max_source_width,
        max_output_pixels=max_output_pixels,
    )
    if not eligible:
        return _unchanged_result(image, status="skipped", reason=reason, started_at=started)

    resolved = installation or discover_upscayl_installation()
    if resolved is None:
        return _unchanged_result(
            image,
            status="unavailable",
            reason="installation_not_found",
            started_at=started,
        )

    # Avoid PNG encoding and temporary-directory I/O while a known-bad GPU or
    # binary is cooling down. The same check is repeated under the run lock to
    # close the race between parallel callers.
    failure_state, remaining_ms = _failure_backoff_status(resolved)
    if failure_state is not None and remaining_ms > 0:
        return _unchanged_result(
            image,
            status="backoff",
            reason=f"temporary_backoff:{failure_state.reason}",
            started_at=started,
            installation=resolved,
            cooldown_remaining_ms=remaining_ms,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="edb-upscayl-") as raw_tmp:
            work_dir = Path(raw_tmp)
            source_path = work_dir / "source.png"
            output_path = work_dir / "output.png"
            source_mode = "RGBA" if "A" in image.getbands() else "RGB"
            image.convert(source_mode).save(source_path, format="PNG")
            command = [
                str(resolved.binary_path),
                "-i",
                str(source_path),
                "-o",
                str(output_path),
                "-m",
                str(resolved.models_dir),
                "-n",
                resolved.model,
                "-w",
                str(target_width),
                "-f",
                "png",
            ]
            with _UPSCAYL_RUN_LOCK:
                failure_state, remaining_ms = _failure_backoff_status(resolved)
                if failure_state is not None and remaining_ms > 0:
                    return _unchanged_result(
                        image,
                        status="backoff",
                        reason=f"temporary_backoff:{failure_state.reason}",
                        started_at=started,
                        installation=resolved,
                        cooldown_remaining_ms=remaining_ms,
                    )
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=max(1.0, float(timeout_seconds)),
                        check=False,
                        **_subprocess_platform_kwargs(),
                    )
                except subprocess.TimeoutExpired:
                    cooldown_ms = _record_failure(resolved, "timeout")
                    return _unchanged_result(
                        image,
                        status="failed",
                        reason="timeout",
                        started_at=started,
                        installation=resolved,
                        cooldown_remaining_ms=cooldown_ms,
                    )
                if completed.returncode != 0 or not output_path.is_file():
                    detail = (completed.stderr or completed.stdout or "unknown Upscayl failure").strip()
                    reason = f"process_failed:{detail[-300:]}" if detail else "process_failed"
                    cooldown_ms = _record_failure(resolved, reason)
                    return _unchanged_result(
                        image,
                        status="failed",
                        reason=reason,
                        started_at=started,
                        installation=resolved,
                        cooldown_remaining_ms=cooldown_ms,
                    )
                with Image.open(output_path) as loaded:
                    output_mode = (
                        "RGBA"
                        if "A" in loaded.getbands() or "A" in image.getbands()
                        else "RGB"
                    )
                    output = loaded.convert(output_mode).copy()
                if output.width < image.width or output.width * output.height > max_output_pixels:
                    cooldown_ms = _record_failure(resolved, "invalid_output_size")
                    return _unchanged_result(
                        image,
                        status="failed",
                        reason="invalid_output_size",
                        started_at=started,
                        installation=resolved,
                        cooldown_remaining_ms=cooldown_ms,
                    )
                _record_success(resolved)
    except (OSError, ValueError) as exc:
        reason = f"runtime_error:{type(exc).__name__}"
        cooldown_ms = _record_failure(resolved, reason)
        return _unchanged_result(
            image,
            status="failed",
            reason=reason,
            started_at=started,
            installation=resolved,
            cooldown_remaining_ms=cooldown_ms,
        )

    latency_ms = int(round((time.perf_counter() - started) * 1000.0))
    return UpscaylAutoResult(
        image=output,
        status="applied",
        reason="low_resolution_source",
        source_width=image.width,
        output_width=output.width,
        latency_ms=latency_ms,
        binary_path=resolved.binary_path,
        model=resolved.model,
    )
