#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import concurrent.futures
import errno
import gzip
import hashlib
import importlib
import ipaddress
import json
import math
import mimetypes
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from datetime import datetime
from functools import lru_cache, partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, url2pathname, urlopen

from bug_reporting import (
    DEFAULT_BUG_REPORT_URL,
    BugReportDeliveryError,
    BugReportValidationError,
    build_bug_report,
    deliver_bug_report,
)
from layout_template_schema import LayoutTemplate
from structured_schema import Box, Subject
from user_settings import (
    ai_enabled_from_settings,
    apply_to_env as apply_user_settings_to_env,
    load_user_settings,
    summarize_for_response as summarize_user_settings,
    update_api_keys,
)


CROP_FORMAT_V1 = "v1"
CROP_FORMAT_V2 = "v2"
DEFAULT_BOARD_THEME = "charcoal"
ONE_PROBLEM_SLOT_HEIGHT_PAGES = 1.2
CLASSIN_MAX_BOARD_PAGE_COUNT = 50
REGULAR_PLACEMENT_SCALE_MAX = 1.6
DEFAULT_IMAGE_RECONSTRUCTION_PROVIDER = "gemini"
ACTIVE_IMAGE_ENHANCE_SIZE = "1k"
HIGH_RES_IMAGE_SIZE_ALIASES = {"2K", "2048", "2048PX", "4K", "4096", "4096PX"}
PASSAGE_REVIEW_REASON_LABELS = {
    "cross_page_passage_group": "페이지 이어짐",
    "hwp_text_fallback_problem": "HWP 텍스트 fallback",
    "marker_document_continuation": "문서 이어짐 표시",
    "passage_cross_page_merge_check": "병합 확인",
    "passage_fragment": "지문 본문",
    "passage_group_source_reuse": "지문 겹침",
    "passage_missing_child_questions": "문항 누락",
    "passage_quality_review": "지문 품질 확인",
    "source_problem_bbox_overlap": "문항 영역 겹침",
}
MANUAL_PASSAGE_CENTER_DIVIDER_EXCLUSION_PX = 6.0
MANUAL_PASSAGE_CENTER_DIVIDER_MIN_RATIO = 0.30
MANUAL_PASSAGE_CENTER_DIVIDER_MAX_RATIO = 0.70
PUBLISH_RECOVERY_STEPS = [
    "같은 작업에서 다시 제작해 주세요. 편집 내용은 최근 작업에 보관됩니다.",
    "계속 실패하면 PNG 묶음으로 먼저 보관한 뒤 오류 정보를 복사해 신고해 주세요.",
]


def _log_operation_exception(operation: str, exc: BaseException) -> None:
    print(
        f"[operation-error] {operation}: {type(exc).__name__}: {exc}",
        file=sys.stderr,
        flush=True,
    )
    traceback.print_exc(file=sys.stderr)


def _publish_failure_payload(
    *,
    code: str,
    message: str,
    exc: BaseException,
    retryable: bool = True,
    recovery_steps: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"{message}: {exc}",
        "code": code,
        "operation": "session_publish",
        "retryable": retryable,
        "recoverySteps": list(recovery_steps or PUBLISH_RECOVERY_STEPS),
    }


def _publish_stage_failure_payload(stage: str, exc: BaseException) -> dict[str, Any]:
    text = str(exc).lower()
    if isinstance(exc, FileNotFoundError):
        return _publish_failure_payload(
            code="publish_asset_missing",
            message="EDB 제작에 필요한 원본 파일을 찾지 못했습니다",
            exc=exc,
            retryable=False,
            recovery_steps=[
                "최근 작업에서 원본 PDF 또는 이미지가 열리는지 확인해 주세요.",
                "파일이 이동되었다면 원본 PDF를 다시 등록하고 제작해 주세요.",
            ],
        )
    if isinstance(exc, ValueError) and ("page limit" in text or "50" in text and "page" in text):
        return _publish_failure_payload(
            code="publish_page_limit_exceeded",
            message="ClassIn 보드 페이지 제한을 초과했습니다",
            exc=exc,
            retryable=False,
            recovery_steps=[
                "문항을 나누거나 일부 문항을 제외한 뒤 다시 제작해 주세요.",
                "계속 필요하면 여러 EDB로 나누어 제작해 주세요.",
            ],
        )
    definitions = {
        "prepare": ("publish_output_unavailable", "제작 파일을 저장할 폴더를 준비하지 못했습니다"),
        "build": ("publish_build_failed", "EDB 제작 데이터 생성에 실패했습니다"),
        "write": ("edb_write_failed", "EDB 파일 저장 또는 검증에 실패했습니다"),
        "handoff": ("publish_handoff_failed", "ClassIn 전달 파일 생성에 실패했습니다"),
        "commit": ("publish_session_commit_failed", "완성 파일을 현재 작업에 반영하지 못했습니다"),
    }
    code, message = definitions.get(stage, ("publish_failed", "EDB 제작에 실패했습니다"))
    return _publish_failure_payload(code=code, message=message, exc=exc)


@lru_cache(maxsize=None)
def _lazy_module(module_name: str) -> Any:
    return importlib.import_module(module_name)


def _lazy_call(module_name: str, attr_name: str, *args: Any, **kwargs: Any) -> Any:
    return getattr(_lazy_module(module_name), attr_name)(*args, **kwargs)


def _lazy_attr(module_name: str, attr_name: str) -> Any:
    return getattr(_lazy_module(module_name), attr_name)


class _LazyModuleProxy:
    def __init__(self, module_name: str) -> None:
        object.__setattr__(self, "_module_name", module_name)

    def _module(self) -> Any:
        return _lazy_module(str(object.__getattribute__(self, "_module_name")))

    def __getattr__(self, attr_name: str) -> Any:
        return getattr(self._module(), attr_name)

    def __setattr__(self, attr_name: str, value: Any) -> None:
        setattr(self._module(), attr_name, value)

    def __delattr__(self, attr_name: str) -> None:
        delattr(self._module(), attr_name)


preprocess = _LazyModuleProxy("preprocess")


def _preprocess_module() -> Any:
    return preprocess._module()



def run_export(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_mvp_export", "run_export", *args, **kwargs)


def ProblemEntry(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "ProblemEntry", *args, **kwargs)


def _classin_board_placement_overlap_issues(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_classin_board_placement_overlap_issues", *args, **kwargs)


def _classin_passage_group_source_reuse_issues(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_classin_passage_group_source_reuse_issues", *args, **kwargs)


def _classin_page_chrome_artifact_issues(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_classin_page_chrome_artifact_issues", *args, **kwargs)


def _classin_source_bbox_overlap_issues(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_classin_source_bbox_overlap_issues", *args, **kwargs)


def _build_transparent_reconstruction_image(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_build_transparent_reconstruction_image", *args, **kwargs)


def _problem_prefers_text_preservation(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_problem_prefers_text_preservation", *args, **kwargs)


def _load_board_export_image(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_load_board_export_image", *args, **kwargs)


def _normalize_processing_step(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_normalize_processing_step", *args, **kwargs)


def _session_problem_count_payload(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_session_problem_count_payload", *args, **kwargs)


def build_records(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "build_records", *args, **kwargs)


def split_problem_entries_for_classin_page_limit(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "split_problem_entries_for_classin_page_limit", *args, **kwargs)


def _validate_record_page_count_hints(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_validate_record_page_count_hints", *args, **kwargs)


def _validate_sequential_record_placements(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_validate_sequential_record_placements", *args, **kwargs)


def write_classin_limited_edb_files(*args: Any, **kwargs: Any) -> Any:
    return _write_classin_limited_edb_files_local(*args, **kwargs)


def build_ui_session(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "build_ui_session", *args, **kwargs)


def estimate_height_pages(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "estimate_height_pages", *args, **kwargs)


def estimate_page_as_is_height_pages(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "estimate_page_as_is_height_pages", *args, **kwargs)


def recrop_problem(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "recrop_problem", *args, **kwargs)


def _stitch_passage_image_files(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "_stitch_passage_image_files", *args, **kwargs)


def resolve_subject(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "resolve_subject", *args, **kwargs)


def run_problem_export(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "run_problem_export", *args, **kwargs)


def write_classin_handoff_manifest(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_problem_board_edb", "write_classin_handoff_manifest", *args, **kwargs)


def resolve_recognition_worker_count(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("build_structured_page_json", "resolve_recognition_worker_count", *args, **kwargs)


def build_edb(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("edb_builder", "build_edb", *args, **kwargs)


def version_string_for_crop_format(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("edb_builder", "version_string_for_crop_format", *args, **kwargs)


def write_edb(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("edb_builder", "write_edb", *args, **kwargs)


def build_ai_fallback_config(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("page_repair", "build_ai_fallback_config", *args, **kwargs)


def _default_reconstruction_prompt() -> str:
    return str(_lazy_attr("image_reconstruction_backend", "DEFAULT_RECONSTRUCTION_PROMPT"))


def _text_priority_reconstruction_prompt() -> str:
    return str(_lazy_attr("image_reconstruction_backend", "TEXT_PRIORITY_RECONSTRUCTION_PROMPT"))


def default_image_model(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("image_reconstruction_backend", "default_image_model", *args, **kwargs)


def normalize_image_model(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("image_reconstruction_backend", "normalize_image_model", *args, **kwargs)


def normalize_image_provider(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("image_reconstruction_backend", "normalize_image_provider", *args, **kwargs)


def reconstruct_problem_image(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("image_reconstruction_backend", "reconstruct_problem_image", *args, **kwargs)


def build_content_safe_upscale(*args: Any, **kwargs: Any) -> Any:
    return _lazy_call("image_reconstruction_backend", "build_content_safe_upscale", *args, **kwargs)


def load_env_local() -> None:
    # edb_make 전용 .env.local 만 읽어옵니다. (Classin_Home 프로젝트와 완전히 분리)
    env_path = Path(__file__).resolve().parent / ".env.local"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ[key.strip()] = val.strip()

load_env_local()

APP_NAME = "ClassIn EDB MVP Local App"
APP_UPDATE_CONFIG_FILE = "app_update_config.json"
APP_UPDATE_CONFIG_ALIAS_GROUPS = (
    ("appId", ("appId", "app_id")),
    ("appName", ("appName", "app_name")),
    ("updateFeedUrl", ("updateFeedUrl", "update_feed_url")),
    ("downloadUrl", ("downloadUrl", "download_url")),
    ("releaseNotesUrl", ("releaseNotesUrl", "release_notes_url")),
)
APP_UPDATE_CONFIG_ERROR_KEY = "_configError"
# Frontend uploads files as base64 inside JSON, so a 1 MB limit rejects many
# normal PDFs/photos before parsing or AI recognition can start.
MAX_JSON_BODY_BYTES = 64 * 1024 * 1024
MAX_UPDATE_FEED_BYTES = 262_144
DEFAULT_RECOGNITION_MAX_DIMENSION = 4096
UPDATE_STATUS_CACHE_TTL_SECONDS = 60.0
RUNTIME_DIAGNOSTICS_CACHE_TTL_SECONDS = 45.0
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UPDATE_ARTIFACT_TYPE_SUFFIXES = {
    "dmg": (".dmg",),
    "setup-exe": (".exe",),
    "zip": (".zip",),
}
UPDATE_PLATFORM_ARTIFACT_TYPES = {
    "macos": ("dmg", "zip"),
    "windows": ("setup-exe",),
}
INPUT_INTENTS = {"auto", "single-problem", "multi-problem", "page-as-is"}
CONTENT_TARGETS = {"all", "questions", "shared-passages"}
OUTER_EDB_PREFIX_LEN = 11
_update_status_cache_lock = threading.Lock()
_update_status_cache: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
_runtime_diagnostics_cache_lock = threading.Lock()
_runtime_diagnostics_cache: tuple[float, dict[str, Any]] | None = None


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def default_frozen_app_home() -> Path:
    configured = os.environ.get("EDB_APP_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / "Documents" / "ClassInEDBMVP").resolve()


def app_root() -> Path:
    if is_frozen_app():
        return default_frozen_app_home()
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    if is_frozen_app() and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


BASE_DIR = app_root()
RESOURCE_DIR = resource_root()
UI_DIR = RESOURCE_DIR / "ui_prototype"
LEGACY_UI_ASSET_ALIASES = {
    "/app.js": "/app.bundle.js",
}
RUNTIME_DIR = BASE_DIR / ".app_runtime"


def _global_ai_enabled() -> bool:
    """Authoritative user preference for every provider-backed AI path."""
    return ai_enabled_from_settings(load_user_settings(RUNTIME_DIR))
UPLOAD_DIR = RUNTIME_DIR / "uploads"
LATEST_SESSION_JSON = RUNTIME_DIR / "latest_session.json"
SESSION_HISTORY_JSON = RUNTIME_DIR / "session_history.json"
GENERATED_SESSION_JS = RUNTIME_DIR / "generated_session.js"
APP_LOG_FILE = RUNTIME_DIR / "app.log"
EMPTY_GENERATED_SESSION_JS = "window.EDB_UI_SESSION = null;\n"
DEFAULT_OUTPUT_ROOT_NAME = "outputs"
RUNTIME_ARTIFACT_ROOT_NAMES = ("uploads", DEFAULT_OUTPUT_ROOT_NAME, "exports")
DEFAULT_ARTIFACT_RETENTION_DAYS = 30.0
FILE_PREVIEW_MIN_DIMENSION = 256
FILE_PREVIEW_MAX_DIMENSION = 2048
FILE_PREVIEW_JPEG_QUALITY = 82
_session_storage_lock = threading.RLock()


class RequestPayloadTooLarge(Exception):
    def __init__(self, content_length: int, limit: int) -> None:
        self.content_length = content_length
        self.limit = limit
        super().__init__(f"request body is {content_length} bytes; limit is {limit} bytes")


class ArtifactCleanupBusy(RuntimeError):
    """Raised when cleanup would race an artifact-producing request."""


class SessionRevisionConflict(RuntimeError):
    """Raised when a destructive request no longer targets the current session."""


def default_output_root() -> Path:
    return RUNTIME_DIR / DEFAULT_OUTPUT_ROOT_NAME


def _fsync_parent_directory(path: Path) -> None:
    """Best-effort durability for a file-system entry created by rename."""
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except (AttributeError, OSError):
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        os.close(directory_fd)


def _atomic_write_text(path: Path, payload: str, *, encoding: str = "utf-8") -> None:
    """Durably replace a text file without exposing a partially-written JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=".atomic-write.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(descriptor, "w", encoding=encoding) as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        # fsync the containing directory as well: the file contents and rename
        # are otherwise atomic but the new directory entry can still be lost
        # after a sudden power failure on POSIX filesystems. Windows does not
        # generally allow opening directories this way, so it keeps the safe
        # atomic-replace behavior and skips only this extra durability step.
        _fsync_parent_directory(path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Durably replace a binary file without exposing partial cache entries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=".atomic-write.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_parent_directory(path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _write_zip_atomically(path: Path, *, compression: int, populate) -> None:
    """Build a ZIP beside its destination and expose it only after close/fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, raw_temporary_path = tempfile.mkstemp(
            prefix=".atomic-zip.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        os.close(descriptor)
        temporary_path = Path(raw_temporary_path)
        with zipfile.ZipFile(temporary_path, "w", compression=compression) as archive:
            populate(archive)
        # Windows FlushFileBuffers requires a handle opened with write access.
        # ``r+b`` keeps the completed archive unchanged while providing that
        # writable handle for os.fsync on every supported platform.
        with temporary_path.open("r+b") as archive_file:
            os.fsync(archive_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_parent_directory(path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _file_matches_digest(
    path: Path,
    expected_digest: str,
    expected_size: int,
    *,
    algorithm: str = "sha256",
) -> bool:
    """Validate a managed upload cache hit before reusing its digest-named path."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_size:
            return False
        digest = hashlib.new(algorithm)
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected_digest


def _session_reset_transaction_path() -> Path:
    return LATEST_SESSION_JSON.parent / ".session_reset_transaction.json"


def _valid_session_reset_entries(payload: dict[str, Any]) -> list[tuple[Path, Path]] | None:
    allowed_targets = {str(path): path for path in (LATEST_SESSION_JSON, SESSION_HISTORY_JSON, GENERATED_SESSION_JS)}
    entries: list[tuple[Path, Path]] = []
    stamp = str(payload.get("stamp") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", stamp):
        return None
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return None
    seen_targets: set[Path] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            return None
        target = allowed_targets.get(str(raw_entry.get("target") or ""))
        tombstone = Path(str(raw_entry.get("tombstone") or ""))
        if target is None or target in seen_targets:
            return None
        expected_tombstone = target.with_name(f".{target.name}.{stamp}.reset")
        if tombstone != expected_tombstone:
            return None
        seen_targets.add(target)
        entries.append((target, tombstone))
    return entries


def _quarantine_reset_tombstone(tombstone: Path, *, marker: Path, stamp: str) -> Path:
    quarantine_dir = marker.parent / ".session-reset-recovery-conflicts" / stamp
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    destination = quarantine_dir / tombstone.name
    if destination.exists():
        destination = quarantine_dir / f"{tombstone.name}.{uuid.uuid4().hex[:12]}"
    os.replace(tombstone, destination)
    _fsync_parent_directory(destination)
    print(
        f"[app-server] preserved stale reset tombstone without overwriting newer state: {destination}",
        file=sys.stderr,
    )
    return destination


def _recover_interrupted_session_reset() -> bool:
    """Finish or roll back a reset interrupted by process termination."""
    marker = _session_reset_transaction_path()
    with _session_storage_lock:
        if not marker.exists():
            return False
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[app-server] session reset recovery marker unreadable: {exc}", file=sys.stderr)
            return False
        if not isinstance(payload, dict) or payload.get("version") != 1:
            print("[app-server] session reset recovery marker is invalid", file=sys.stderr)
            return False
        entries = _valid_session_reset_entries(payload)
        if entries is None:
            print("[app-server] session reset recovery marker identity is invalid", file=sys.stderr)
            return False
        stamp = str(payload.get("stamp") or "")
        phase = str(payload.get("phase") or "")
        try:
            if phase == "committed":
                for _target, tombstone in entries:
                    if tombstone.exists():
                        tombstone.unlink()
            elif phase == "preparing":
                for target, tombstone in reversed(entries):
                    if tombstone.exists():
                        if target.exists():
                            _quarantine_reset_tombstone(
                                tombstone,
                                marker=marker,
                                stamp=stamp,
                            )
                        else:
                            os.replace(tombstone, target)
                    elif not target.exists():
                        raise FileNotFoundError(
                            f"reset recovery lost both target and tombstone: {target}"
                        )
            else:
                print(f"[app-server] session reset recovery phase is invalid: {phase}", file=sys.stderr)
                return False
            marker.unlink()
            _fsync_parent_directory(marker)
        except OSError as exc:
            print(f"[app-server] interrupted session reset recovery deferred: {exc}", file=sys.stderr)
            return False
    print(f"[app-server] recovered interrupted session reset ({phase})", file=sys.stderr)
    return True


def _atomically_clear_persisted_session_state() -> None:
    """Clear all persisted session pointers as one recoverable transaction."""
    targets = (LATEST_SESSION_JSON, SESSION_HISTORY_JSON, GENERATED_SESSION_JS)
    stamp = uuid.uuid4().hex
    moved: list[tuple[Path, Path]] = []
    marker = _session_reset_transaction_path()
    with _session_storage_lock:
        _recover_interrupted_session_reset()
        generated_existed_before = GENERATED_SESSION_JS.exists()
        planned = [
            (target, target.with_name(f".{target.name}.{stamp}.reset"))
            for target in targets
            if target.exists()
        ]
        marker_payload: dict[str, Any] = {
            "version": 1,
            "stamp": stamp,
            "phase": "preparing",
            "generatedExistedBefore": generated_existed_before,
            "entries": [
                {"target": str(target), "tombstone": str(tombstone)}
                for target, tombstone in planned
            ],
        }
        try:
            _atomic_write_text(marker, json.dumps(marker_payload, ensure_ascii=False, indent=2))
            for target, tombstone in planned:
                os.replace(target, tombstone)
                moved.append((target, tombstone))
            _atomic_write_text(GENERATED_SESSION_JS, EMPTY_GENERATED_SESSION_JS)
            marker_payload["phase"] = "committed"
            _atomic_write_text(marker, json.dumps(marker_payload, ensure_ascii=False, indent=2))
        except BaseException:
            try:
                generated_was_moved = any(target == GENERATED_SESSION_JS for target, _ in moved)
                if GENERATED_SESSION_JS.exists() and (
                    generated_was_moved or not generated_existed_before
                ):
                    GENERATED_SESSION_JS.unlink()
            except OSError:
                pass
            for target, tombstone in reversed(moved):
                if tombstone.exists():
                    os.replace(tombstone, target)
            try:
                marker.unlink()
                _fsync_parent_directory(marker)
            except OSError:
                pass
            raise
        tombstone_cleanup_failed = False
        for _target, tombstone in moved:
            try:
                tombstone.unlink()
            except OSError as exc:
                tombstone_cleanup_failed = True
                print(f"[app-server] reset tombstone cleanup failed: {exc}", file=sys.stderr)
        if not tombstone_cleanup_failed:
            try:
                marker.unlink()
                _fsync_parent_directory(marker)
            except OSError as exc:
                print(f"[app-server] reset transaction marker cleanup failed: {exc}", file=sys.stderr)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _clone_jsonish(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


def clear_app_update_status_cache() -> None:
    with _update_status_cache_lock:
        _update_status_cache.clear()


def _cached_update_status(cache_key: tuple[str, ...]) -> dict[str, Any] | None:
    now = time.monotonic()
    with _update_status_cache_lock:
        cached = _update_status_cache.get(cache_key)
        if cached is None:
            return None
        expires_at, status = cached
        if expires_at <= now:
            _update_status_cache.pop(cache_key, None)
            return None
        return _clone_jsonish(status)


def _remember_update_status(cache_key: tuple[str, ...], status: dict[str, Any]) -> dict[str, Any]:
    snapshot = _clone_jsonish(status)
    with _update_status_cache_lock:
        _update_status_cache[cache_key] = (time.monotonic() + UPDATE_STATUS_CACHE_TTL_SECONDS, snapshot)
    return _clone_jsonish(snapshot)


def _app_platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _normalized_app_arch(value: Any) -> str:
    arch = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "amd64": "x64",
        "x86-64": "x64",
        "x86_64": "x64",
        "aarch64": "arm64",
        "arm64-v8a": "arm64",
        "universal2": "universal",
        "all": "universal",
        "any": "universal",
    }
    return aliases.get(arch, arch)


def _app_arch_key() -> str:
    return _normalized_app_arch(platform.machine())


def _app_update_config_alias_conflict_error(label: str, source: dict[str, Any], *field_names: str) -> str:
    values = {
        field_name: str(source.get(field_name) or "").strip()
        for field_name in field_names
        if str(source.get(field_name) or "").strip()
    }
    if len(set(values.values())) <= 1:
        return ""
    details = ", ".join(f"{field_name}={value!r}" for field_name, value in values.items())
    return f"{APP_UPDATE_CONFIG_FILE} {label} aliases conflict: {details}"


def _normalize_app_update_config_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    normalized = {key: value for key, value in payload.items() if value is not None}
    normalized.pop(APP_UPDATE_CONFIG_ERROR_KEY, None)
    for canonical_name, field_names in APP_UPDATE_CONFIG_ALIAS_GROUPS:
        if alias_error := _app_update_config_alias_conflict_error(canonical_name, payload, *field_names):
            return normalized, alias_error
        value = _first_nonempty(*(payload.get(field_name) for field_name in field_names))
        for field_name in field_names:
            if field_name != canonical_name:
                normalized.pop(field_name, None)
        if value:
            normalized[canonical_name] = value
    return normalized, ""


def load_app_update_config() -> dict[str, Any]:
    config: dict[str, Any] = {
        "appId": "ClassInEDBMVP",
        "appName": "ClassInEDBMVP",
        "version": "0.1.0",
        "updateFeedUrl": "",
        "downloadUrl": "",
        "releaseNotesUrl": "",
        "bugReportUrl": DEFAULT_BUG_REPORT_URL,
    }
    seen: set[Path] = set()
    for path in (RESOURCE_DIR / APP_UPDATE_CONFIG_FILE, BASE_DIR / APP_UPDATE_CONFIG_FILE):
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        payload, config_error = _normalize_app_update_config_payload(_read_json_object(path))
        if config_error:
            config[APP_UPDATE_CONFIG_ERROR_KEY] = config_error
        config.update(payload)
    env_map = {
        "appId": "EDB_APP_ID",
        "version": "EDB_APP_VERSION",
        "updateFeedUrl": "EDB_UPDATE_FEED_URL",
        "downloadUrl": "EDB_DOWNLOAD_URL",
        "releaseNotesUrl": "EDB_RELEASE_NOTES_URL",
        "bugReportUrl": "EDB_BUG_REPORT_URL",
    }
    for key, env_name in env_map.items():
        if os.environ.get(env_name):
            config[key] = os.environ[env_name].strip()
    config["platform"] = _app_platform_key()
    config["system"] = platform.system() or sys.platform
    return config


def _version_components(version: Any) -> tuple[tuple[int, ...], tuple[str, ...] | None]:
    text = str(version or "").strip().lower()
    text = re.sub(r"^[^\d]+", "", text)
    text = text.split("+", 1)[0]
    base, prerelease = (text.split("-", 1) + [""])[:2] if "-" in text else (text, "")
    parts: list[int] = []
    for token in re.split(r"[._\s]+", base):
        if not token:
            continue
        match = re.match(r"(\d+)", token)
        parts.append(int(match.group(1)) if match else 0)
    while len(parts) < 3:
        parts.append(0)
    prerelease_parts = tuple(part for part in re.split(r"[.\s]+", prerelease) if part)
    return tuple(parts), prerelease_parts or None


def _compare_prerelease_versions(current: tuple[str, ...], latest: tuple[str, ...]) -> int:
    for current_part, latest_part in zip(current, latest):
        current_numeric = current_part.isdigit()
        latest_numeric = latest_part.isdigit()
        if current_numeric and latest_numeric:
            current_value = int(current_part)
            latest_value = int(latest_part)
            if latest_value != current_value:
                return 1 if latest_value > current_value else -1
            continue
        if current_numeric != latest_numeric:
            return -1 if latest_numeric else 1
        if latest_part != current_part:
            return 1 if latest_part > current_part else -1
    if len(latest) != len(current):
        return 1 if len(latest) > len(current) else -1
    return 0


def compare_app_versions(current: Any, latest: Any) -> int:
    current_parts, current_prerelease = _version_components(current)
    latest_parts, latest_prerelease = _version_components(latest)
    max_len = max(len(current_parts), len(latest_parts))
    current_parts += (0,) * (max_len - len(current_parts))
    latest_parts += (0,) * (max_len - len(latest_parts))
    if latest_parts > current_parts:
        return 1
    if latest_parts < current_parts:
        return -1
    if current_prerelease == latest_prerelease:
        return 0
    if current_prerelease and latest_prerelease is None:
        return 1
    if current_prerelease is None and latest_prerelease:
        return -1
    if current_prerelease and latest_prerelease:
        return _compare_prerelease_versions(current_prerelease, latest_prerelease)
    return 0


def _normalize_update_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.netloc:
        return ""
    if parsed.scheme == "https":
        return text
    if parsed.scheme == "http" and _is_loopback_hostname(parsed.hostname):
        return text
    return ""


def _is_loopback_hostname(hostname: str | None) -> bool:
    host = (hostname or "").strip().lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request_host_is_loopback(headers: Any) -> bool:
    raw_host = str(headers.get("Host") or "").strip()
    if not raw_host or any(character in raw_host for character in "/\\?#@,"):
        return False
    parsed = urlparse(f"http://{raw_host}")
    try:
        _ = parsed.port
    except ValueError:
        return False
    return bool(parsed.hostname) and _is_loopback_hostname(parsed.hostname)


def _request_is_same_origin(headers: Any) -> bool:
    host = str(headers.get("Host") or "").strip().lower()
    if not host or not _request_host_is_loopback(headers):
        return False
    for header_name in ("Origin", "Referer"):
        raw_value = str(headers.get(header_name) or "").strip()
        if not raw_value:
            continue
        parsed = urlparse(raw_value)
        return (
            parsed.scheme == "http"
            and parsed.netloc.lower() == host
            and _is_loopback_hostname(parsed.hostname)
        )
    return True


def _browser_write_request_is_trusted(headers: Any) -> bool:
    """Reject cross-site browser writes while preserving native local clients."""
    if not _request_host_is_loopback(headers):
        return False
    origin = str(headers.get("Origin") or "").strip()
    if origin:
        return _request_is_same_origin(headers)
    fetch_site = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site in {"cross-site", "same-site"}:
        return False
    return True


def _fetch_update_feed(feed_url: str) -> dict[str, Any]:
    request = Request(feed_url, headers={
        "Accept": "application/json",
        "User-Agent": f"ClassInEDBMVP/{load_app_update_config().get('version', '0')}",
    })
    with urlopen(request, timeout=4.0) as response:
        raw_body = response.read(MAX_UPDATE_FEED_BYTES + 1)
    if len(raw_body) > MAX_UPDATE_FEED_BYTES:
        raise ValueError("update feed is too large")
    data = json.loads(raw_body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("update feed must be a JSON object")
    return data


def _select_platform_update(feed: dict[str, Any], platform_key: str) -> dict[str, Any]:
    selected = dict(feed)
    platforms = feed.get("platforms")
    if isinstance(platforms, dict):
        platform_payload = platforms.get(platform_key)
        if platform_payload is None and platform_key == "macos":
            platform_payload = platforms.get("darwin")
        if platform_payload is None and platform_key == "windows":
            platform_payload = platforms.get("win32")
        if isinstance(platform_payload, dict):
            selected.update({k: v for k, v in platform_payload.items() if v is not None})
            selected["platformSupported"] = True
        else:
            selected["platformSupported"] = False
    else:
        selected["platformSupported"] = True
    return selected


def _copy_update_fields(target: dict[str, Any], source: dict[str, Any], field_names: tuple[str, ...]) -> None:
    for field_name in field_names:
        value = source.get(field_name)
        if value is not None and value != "":
            target[field_name] = value


def _update_source_text(source: dict[str, Any], *field_names: str) -> str:
    return _first_nonempty(*(source.get(field_name) for field_name in field_names))


def _update_source_value(source: dict[str, Any], *field_names: str) -> Any:
    for field_name in field_names:
        value = source.get(field_name)
        if value is not None and value != "":
            return value
    return None


def _update_alias_conflict_error(label: str, source: dict[str, Any], *field_names: str) -> str:
    values = {
        field_name: str(source.get(field_name) or "").strip()
        for field_name in field_names
        if str(source.get(field_name) or "").strip()
    }
    if len(set(values.values())) <= 1:
        return ""
    details = ", ".join(f"{field_name}={value!r}" for field_name, value in values.items())
    return f"update feed {label} aliases conflict: {details}"


def _update_metadata_alias_conflict_error(source: dict[str, Any]) -> str:
    alias_pairs = (
        ("appId", ("appId", "app_id")),
        ("appName", ("appName", "app_name")),
        ("version", ("version", "latestVersion", "latest_version")),
        ("manifestUrl", ("manifestUrl", "manifest_url")),
        ("manifestSha256", ("manifestSha256", "manifest_sha256")),
        ("downloadUrl", ("downloadUrl", "download_url", "url")),
        ("releaseNotesUrl", ("releaseNotesUrl", "release_notes_url", "notesUrl", "notes_url")),
        ("fileName", ("fileName", "file_name")),
        ("artifactType", ("artifactType", "artifact_type")),
        ("sizeBytes", ("sizeBytes", "size_bytes")),
    )
    for label, field_names in alias_pairs:
        if alias_error := _update_alias_conflict_error(label, source, *field_names):
            return alias_error
    return ""


def _copy_update_field_alias(
    target: dict[str, Any],
    source: dict[str, Any],
    output_name: str,
    *field_names: str,
) -> None:
    value = _update_source_text(source, *field_names)
    if value:
        target[output_name] = value


def _copy_update_url_field(target: dict[str, Any], source: dict[str, Any], output_name: str, *field_names: str) -> None:
    value = _normalize_update_url(_update_source_text(source, *field_names))
    if value:
        target[output_name] = value


def _normalize_update_sha256(value: Any) -> str:
    digest = str(value or "").strip()
    return digest if SHA256_RE.fullmatch(digest) else ""


def _normalize_positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    return number if number > 0 else None


def _update_integrity_error(source: dict[str, Any]) -> str:
    integrity_fields = (
        ("manifestSha256", ("manifestSha256", "manifest_sha256")),
        ("sha256", ("sha256",)),
    )
    for field_name, aliases in integrity_fields:
        raw_value = _update_source_text(source, *aliases)
        if raw_value and not _normalize_update_sha256(raw_value):
            return f"update feed has invalid {field_name}"
    raw_size = _update_source_value(source, "sizeBytes", "size_bytes")
    if raw_size not in (None, "") and _normalize_positive_int(raw_size) is None:
        return "update feed has invalid sizeBytes"
    return ""


def _copy_update_integrity_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    manifest_digest = _normalize_update_sha256(_update_source_text(source, "manifestSha256", "manifest_sha256"))
    if manifest_digest:
        target["manifestSha256"] = manifest_digest
    artifact_digest = _normalize_update_sha256(_update_source_text(source, "sha256"))
    if artifact_digest:
        target["sha256"] = artifact_digest
    size_bytes = _normalize_positive_int(_update_source_value(source, "sizeBytes", "size_bytes"))
    if size_bytes is not None:
        target["sizeBytes"] = size_bytes


def _update_artifact_file_name(source: dict[str, Any]) -> str:
    file_name = _update_source_text(source, "fileName", "file_name")
    if file_name:
        return file_name
    return _update_download_url_file_name(source)


def _update_download_url_value(source: dict[str, Any]) -> str:
    return _update_source_text(source, "downloadUrl", "download_url", "url")


def _update_download_url_file_name(source: dict[str, Any]) -> str:
    raw_url = _update_download_url_value(source)
    if not raw_url:
        return ""
    path = urlparse(raw_url).path
    if not path or path.endswith("/"):
        return ""
    return Path(path).name


def _update_platform_artifact_suffixes(platform: str) -> tuple[str, ...]:
    allowed_types = UPDATE_PLATFORM_ARTIFACT_TYPES.get(platform, ())
    return tuple(
        suffix
        for allowed_type in allowed_types
        for suffix in UPDATE_ARTIFACT_TYPE_SUFFIXES.get(allowed_type, ())
    )


def _update_artifact_metadata_error(source: dict[str, Any], platform_key: str) -> str:
    platform = str(platform_key or "").strip().lower()
    artifact_type = _update_source_text(source, "artifactType", "artifact_type").lower()
    explicit_file_name = _update_source_text(source, "fileName", "file_name")
    download_file_name = _update_download_url_file_name(source)
    file_name = _update_artifact_file_name(source)
    has_download_url = bool(_update_download_url_value(source))
    allowed_types = UPDATE_PLATFORM_ARTIFACT_TYPES.get(platform)
    allowed_suffixes = _update_platform_artifact_suffixes(platform)
    if explicit_file_name and download_file_name and explicit_file_name != download_file_name:
        return (
            f"update feed fileName {explicit_file_name!r} "
            f"does not match download URL file name {download_file_name!r}"
        )
    if artifact_type:
        if allowed_types and artifact_type not in allowed_types:
            allowed = ", ".join(allowed_types)
            return f"update feed artifactType for {platform} must be one of: {allowed}"
        expected_suffixes = UPDATE_ARTIFACT_TYPE_SUFFIXES.get(artifact_type, ())
        suffix = Path(file_name).suffix.lower() if file_name else ""
        if file_name and expected_suffixes and not suffix:
            expected = ", ".join(expected_suffixes)
            if explicit_file_name:
                return f"update feed fileName must include {artifact_type} artifact extension ({expected})"
            return f"update feed download URL must include {artifact_type} artifact extension ({expected})"
        if file_name and expected_suffixes and suffix not in expected_suffixes:
            expected = ", ".join(expected_suffixes)
            return f"update feed fileName does not match {artifact_type} artifact extension ({expected})"
        if expected_suffixes and has_download_url and not file_name:
            expected = ", ".join(expected_suffixes)
            return f"update feed download URL must include {artifact_type} artifact extension ({expected})"
    elif file_name and allowed_suffixes:
        suffix = Path(file_name).suffix.lower()
        if not suffix:
            expected = ", ".join(allowed_suffixes)
            if explicit_file_name:
                return f"update feed fileName for {platform} must include one of: {expected}"
            return f"update feed download URL for {platform} must include artifact extension ({expected})"
        if suffix not in allowed_suffixes:
            expected = ", ".join(allowed_suffixes)
            return f"update feed fileName for {platform} must use one of: {expected}"
    elif has_download_url and allowed_suffixes:
        expected = ", ".join(allowed_suffixes)
        return f"update feed download URL for {platform} must include artifact extension ({expected})"
    return ""


def _update_identity_error(
    source: dict[str, Any],
    *,
    expected_app_id: str,
    expected_app_name: str,
) -> str:
    feed_app_id = str(_first_nonempty(source.get("appId"), source.get("app_id")) or "").strip()
    feed_app_name = str(_first_nonempty(source.get("appName"), source.get("app_name")) or "").strip()
    if feed_app_id and feed_app_id != expected_app_id:
        return f"update feed appId mismatch: expected {expected_app_id}, found {feed_app_id}"
    if feed_app_name and feed_app_name != expected_app_name:
        return f"update feed appName mismatch: expected {expected_app_name}, found {feed_app_name}"
    return ""


def build_app_update_status() -> dict[str, Any]:
    config = load_app_update_config()
    platform_key = str(config.get("platform") or _app_platform_key())
    current_version = str(config.get("version") or "0.0.0")
    config_error = str(config.get(APP_UPDATE_CONFIG_ERROR_KEY) or "").strip()
    feed_url = _normalize_update_url(config.get("updateFeedUrl") or config.get("update_feed_url"))
    fallback_download_url = _normalize_update_url(config.get("downloadUrl") or config.get("download_url"))
    fallback_notes_url = _normalize_update_url(config.get("releaseNotesUrl") or config.get("release_notes_url"))
    app_name = str(config.get("appName") or "ClassInEDBMVP")
    app_id = str(config.get("appId") or config.get("app_id") or app_name).strip() or app_name
    current_arch = _app_arch_key()
    cache_key = (
        app_id,
        app_name,
        platform_key,
        current_arch,
        current_version,
        feed_url,
        fallback_download_url,
        fallback_notes_url,
        config_error,
    )
    cached = _cached_update_status(cache_key)
    if cached is not None:
        return cached
    status: dict[str, Any] = {
        "ok": True,
        "appId": app_id,
        "appName": app_name,
        "platform": platform_key,
        "arch": current_arch,
        "currentVersion": current_version,
        "configured": bool(feed_url or fallback_download_url),
        "updateAvailable": False,
        "channelStatus": "not_configured",
        "feedUrl": feed_url,
        "downloadUrl": fallback_download_url,
        "releaseNotesUrl": fallback_notes_url,
        "latest": None,
    }
    if not feed_url:
        if config_error:
            status["channelStatus"] = "invalid_config"
            status["error"] = config_error
            return _remember_update_status(cache_key, status)
        if fallback_download_url:
            status["channelStatus"] = "manual_download"
        return _remember_update_status(cache_key, status)
    if config_error:
        status["channelStatus"] = "invalid_config"
        status["error"] = config_error
        return _remember_update_status(cache_key, status)
    try:
        feed = _fetch_update_feed(feed_url)
        selected = _select_platform_update(feed, platform_key)
    except Exception as exc:
        status["channelStatus"] = "error"
        status["error"] = str(exc)
        return _remember_update_status(cache_key, status)
    if selected.get("platformSupported") is False:
        status["channelStatus"] = "unsupported_platform"
        return _remember_update_status(cache_key, status)
    if alias_error := _update_metadata_alias_conflict_error(selected):
        status["channelStatus"] = "invalid_feed"
        status["error"] = alias_error
        return _remember_update_status(cache_key, status)
    if identity_error := _update_identity_error(
        selected,
        expected_app_id=app_id,
        expected_app_name=app_name,
    ):
        status["channelStatus"] = "invalid_feed"
        status["error"] = identity_error
        return _remember_update_status(cache_key, status)
    _copy_update_fields(
        status,
        selected,
        (
            "schemaVersion",
            "appId",
            "channel",
            "publishedAt",
        ),
    )
    _copy_update_url_field(status, selected, "manifestUrl", "manifestUrl", "manifest_url")
    latest_version = _first_nonempty(
        selected.get("version"),
        selected.get("latestVersion"),
        selected.get("latest_version"),
    )
    download_url = _normalize_update_url(_first_nonempty(
        _update_download_url_value(selected),
        fallback_download_url,
    ))
    notes_url = _normalize_update_url(_first_nonempty(
        _update_source_text(
            selected,
            "releaseNotesUrl",
            "release_notes_url",
            "notesUrl",
            "notes_url",
        ),
        fallback_notes_url,
    ))
    status["downloadUrl"] = download_url
    status["releaseNotesUrl"] = notes_url
    status["latest"] = {
        "version": latest_version,
        "downloadUrl": download_url,
        "releaseNotesUrl": notes_url,
        "summary": str(selected.get("summary") or selected.get("notes") or "").strip(),
    }
    _copy_update_fields(
        status["latest"],
        selected,
        (
            "arch",
            "publishedAt",
        ),
    )
    _copy_update_field_alias(status["latest"], selected, "fileName", "fileName", "file_name")
    _copy_update_field_alias(status["latest"], selected, "artifactType", "artifactType", "artifact_type")
    _copy_update_url_field(status["latest"], selected, "manifestUrl", "manifestUrl", "manifest_url")
    _copy_update_integrity_fields(status, selected)
    _copy_update_integrity_fields(status["latest"], selected)
    if not latest_version:
        status["channelStatus"] = "invalid_feed"
        status["error"] = "update feed does not include a version"
        return _remember_update_status(cache_key, status)
    if metadata_error := _update_integrity_error(selected):
        status["updateAvailable"] = False
        status["channelStatus"] = "invalid_feed"
        status["error"] = metadata_error
        return _remember_update_status(cache_key, status)
    if artifact_metadata_error := _update_artifact_metadata_error(selected, platform_key):
        status["updateAvailable"] = False
        status["channelStatus"] = "invalid_feed"
        status["error"] = artifact_metadata_error
        return _remember_update_status(cache_key, status)
    feed_arch = _normalized_app_arch(selected.get("arch"))
    if feed_arch and current_arch and feed_arch != "universal" and feed_arch != current_arch:
        status["updateAvailable"] = False
        status["channelStatus"] = "unsupported_architecture"
        status["code"] = "update_architecture_mismatch"
        status["error"] = (
            f"update architecture {feed_arch!r} is not compatible with this device ({current_arch!r})"
        )
        status["availableDownloadUrl"] = status.get("downloadUrl") or ""
        status["downloadUrl"] = ""
        if isinstance(status.get("latest"), dict):
            status["latest"]["downloadUrl"] = ""
        status["recoverySteps"] = [
            f"{current_arch}용 설치 파일을 선택해 주세요.",
            "지원 파일이 보이지 않으면 오류 정보와 함께 신고해 주세요.",
        ]
        return _remember_update_status(cache_key, status)
    comparison = compare_app_versions(current_version, latest_version)
    if comparison > 0 and not download_url:
        status["updateAvailable"] = False
        status["channelStatus"] = "invalid_feed"
        status["error"] = "update feed does not include a usable download URL"
        return _remember_update_status(cache_key, status)
    status["updateAvailable"] = comparison > 0
    status["channelStatus"] = "update_available" if comparison > 0 else "up_to_date"
    return _remember_update_status(cache_key, status)


def _allowed_update_urls() -> set[str]:
    status = build_app_update_status()
    latest = status.get("latest") if isinstance(status.get("latest"), dict) else {}
    candidates = {
        status.get("downloadUrl"),
        status.get("releaseNotesUrl"),
        latest.get("downloadUrl") if isinstance(latest, dict) else None,
        latest.get("releaseNotesUrl") if isinstance(latest, dict) else None,
    }
    return {str(url).strip() for url in candidates if str(url or "").strip()}


def ensure_runtime_dirs() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    default_output_root().mkdir(parents=True, exist_ok=True)


def hydrate_user_settings_env() -> None:
    """Load persisted user settings and promote secrets into ``os.environ``
    so pipeline modules pick them up via the usual env-var path."""
    apply_user_settings_to_env(load_user_settings(RUNTIME_DIR))


def write_placeholder_generated_session() -> None:
    try:
        GENERATED_SESSION_JS.parent.mkdir(parents=True, exist_ok=True)
        GENERATED_SESSION_JS.write_text(EMPTY_GENERATED_SESSION_JS, encoding="utf-8")
    except OSError:
        pass


def read_generated_session_js() -> str:
    return EMPTY_GENERATED_SESSION_JS


def configure_app_logging(log_file: str | Path | None = None) -> None:
    target = Path(log_file).expanduser() if log_file else APP_LOG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    stream = target.open("a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    print(f"\n[{datetime.now().isoformat(timespec='seconds')}] {APP_NAME} starting")


def _local_server_is_healthy(host: str, port: int, *, timeout: float = 0.35) -> bool:
    url = f"http://{host}:{port}/api/health"
    try:
        request = Request(url, headers={"Cache-Control": "no-cache"})
        with urlopen(request, timeout=timeout) as response:
            if response.status != HTTPStatus.OK:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    return bool(isinstance(payload, dict) and payload.get("ok"))


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _coerce_optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _coerce_placement_x_ratio(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("xRatio", "placementXRatio", "placement_x_ratio"):
            if value.get(key) is not None:
                value = value.get(key)
                break
        else:
            return None
    try:
        ratio = _coerce_optional_float(value)
    except (TypeError, ValueError):
        return None
    if ratio is None:
        return None
    return max(0.0, min(1.0, ratio))


def _coerce_placement_y_ratio(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("yRatio", "placementYRatio", "placement_y_ratio"):
            if value.get(key) is not None:
                value = value.get(key)
                break
        else:
            return None
    try:
        ratio = _coerce_optional_float(value)
    except (TypeError, ValueError):
        return None
    if ratio is None:
        return None
    return max(0.0, min(1.0, ratio))


def _coerce_placement_scale_ratio(value: Any) -> float | None:
    if isinstance(value, dict):
        for key in ("scaleRatio", "placementScaleRatio", "placement_scale_ratio"):
            if value.get(key) is not None:
                value = value.get(key)
                break
        else:
            return None
    try:
        ratio = _coerce_optional_float(value)
    except (TypeError, ValueError):
        return None
    if ratio is None:
        return None
    return max(0.6, min(3.0, ratio))


def _problem_preserves_legacy_placement_scale(problem: dict[str, Any]) -> bool:
    metadata = problem.get("metadata")
    sources = (problem, metadata if isinstance(metadata, dict) else {})
    for source in sources:
        for key in ("preserveLegacyPlacementScale", "preserve_legacy_placement_scale"):
            marker = source.get(key)
            if isinstance(marker, bool):
                return marker
            if isinstance(marker, (int, float)) and marker == 1:
                return True
            if isinstance(marker, str) and marker.strip().lower() in {"1", "true", "yes", "on"}:
                return True
    return False


def _problem_has_persisted_legacy_placement_scale(problem: dict[str, Any]) -> bool:
    input_intent = (
        str(problem.get("inputIntent") or problem.get("input_intent") or "")
        .strip()
        .lower()
        .replace("_", "-")
    )
    placement_mode = str(problem.get("placementMode") or problem.get("placement_mode") or "").strip().lower()
    if input_intent == "page-as-is" or placement_mode == "continuous-page-as-is":
        return False
    scale_ratio = _coerce_placement_scale_ratio(problem)
    return scale_ratio is not None and scale_ratio > REGULAR_PLACEMENT_SCALE_MAX


APP_DEFAULT_CROP_FORMAT = CROP_FORMAT_V1


def _normalize_crop_format(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {CROP_FORMAT_V1, CROP_FORMAT_V2}:
        return normalized
    return APP_DEFAULT_CROP_FORMAT


def _extract_crop_format(payload: dict[str, Any]) -> str:
    return _normalize_crop_format(payload.get("cropFormat") or payload.get("crop_format"))


def _command_info(command: list[str]) -> dict[str, Any]:
    executable = str(command[0]) if command else ""
    name = Path(executable).name
    command_args = [str(part) for part in command[1:]]
    if any("unhwp.extract_text" in part for part in command_args):
        name = "unhwp"
    if any("hwp_hwpx_parser" in part for part in command_args):
        name = "hwp-hwpx-parser"
    if any("hwpilot" in part and part.endswith("main.js") for part in command_args):
        name = "hwpilot"
    if any("render_hwp_with_rhwp_core.mjs" in part for part in command_args):
        name = "rhwp-core"
    return {
        "name": name,
        "path": executable,
        "args": command_args,
    }


def describe_runtime_diagnostics() -> dict[str, Any]:
    preprocess = _preprocess_module()
    pdf_converters = [_command_info(command) for command in preprocess._iter_hwp_pdf_converter_commands()]
    hwp_to_hwpx_converters = [_command_info(command) for command in preprocess._iter_hwp_hwpx_converter_commands()]
    html_converters = [_command_info(command) for command in preprocess._iter_pyhwp_html_converter_commands()]
    text_extractors = [_command_info(command) for command in preprocess._iter_hwp_text_converter_commands()]
    chrome_pdf_converters = [_command_info(command) for command in preprocess._iter_chrome_pdf_commands()]
    hwp_renderers = [_command_info(command) for command in preprocess._iter_rhwp_core_renderer_commands()]

    pdf_ready = bool(pdf_converters)
    html_pdf_ready = bool(html_converters and chrome_pdf_converters)
    hwp_renderer_ready = bool(hwp_renderers)
    hwp_ready = bool(pdf_ready or html_pdf_ready or hwp_renderer_ready)
    hwpx_ready = bool(pdf_ready or hwp_renderer_ready)
    warnings: list[str] = []
    recommended_actions: list[str] = []

    if not pdf_ready and not hwp_renderer_ready:
        warnings.append("LibreOffice, rhwp, hwp5pdf, airun-hwp, or rhwp-core renderer was not found.")
        recommended_actions.append("LibreOffice/rhwp/HWP PDF 변환기, airun-hwp, 또는 rhwp-core 렌더러를 설치하거나, HWP/HWPX를 PDF로 내보낸 뒤 업로드해 주세요.")
    if html_converters and not chrome_pdf_converters:
        warnings.append("pyhwp HTML fallback is available, but Chrome PDF printing was not found.")
        recommended_actions.append("Chrome을 설치하거나 EDB_CHROME 환경 변수로 Chrome 실행 파일 경로를 지정해 주세요.")
    if not text_extractors:
        warnings.append("hwp5txt/unhwp/rhwp/hwpilot/kordoc text extractor was not found; HWP 문항 수 사전 점검이 약해집니다.")
        recommended_actions.append("pyhwp/hwp5txt, unhwp, rhwp, HWPilot, 또는 kordoc를 설치하면 HWP 내부 텍스트 기반 문항 수 QA가 더 정확해집니다.")
    if not hwp_to_hwpx_converters:
        recommended_actions.append("선택 사항: HWPilot을 설치하면 HWP→HWPX 정규화 경로를 추가로 사용할 수 있습니다.")

    if hwp_ready and hwpx_ready:
        status = "ready"
        label = "준비됨"
    elif hwp_ready or hwpx_ready:
        status = "partial"
        label = "부분 준비"
    else:
        status = "blocked"
        label = "확인 필요"

    tool_counts = {
        "pdfConverters": len(pdf_converters),
        "hwpToHwpxConverters": len(hwp_to_hwpx_converters),
        "htmlConverters": len(html_converters),
        "textExtractors": len(text_extractors),
        "chromePdfConverters": len(chrome_pdf_converters),
        "hwpRenderers": len(hwp_renderers),
    }
    summary_parts = [
        f"PDF {tool_counts['pdfConverters']}",
        f"텍스트 {tool_counts['textExtractors']}",
        f"브리지 {tool_counts['hwpToHwpxConverters']}",
    ]
    if hwp_renderers:
        summary_parts.append(f"렌더 {tool_counts['hwpRenderers']}")
    if html_pdf_ready:
        summary_parts.append("HTML fallback")
    if warnings:
        summary_parts.append(f"주의 {len(warnings)}")

    return {
        "ok": True,
        "hangul": {
            "status": status,
            "label": label,
            "summary": " · ".join(summary_parts),
            "toolCounts": tool_counts,
            "pdfReady": pdf_ready,
            "hwpReady": hwp_ready,
            "hwpxReady": hwpx_ready,
            "hwpRendererReady": hwp_renderer_ready,
            "htmlPdfFallbackReady": html_pdf_ready,
            "pdfConverters": pdf_converters,
            "hwpToHwpxConverters": hwp_to_hwpx_converters,
            "htmlConverters": html_converters,
            "textExtractors": text_extractors,
            "chromePdfConverters": chrome_pdf_converters,
            "hwpRenderers": hwp_renderers,
            "warnings": warnings,
            "recommendedActions": recommended_actions,
        },
    }


def cached_runtime_diagnostics(*, force_refresh: bool = False) -> dict[str, Any]:
    global _runtime_diagnostics_cache
    now = time.monotonic()
    if not force_refresh:
        with _runtime_diagnostics_cache_lock:
            if _runtime_diagnostics_cache is not None:
                expires_at, payload = _runtime_diagnostics_cache
                if expires_at > now:
                    return _clone_jsonish(payload)
    payload = describe_runtime_diagnostics()
    snapshot = _clone_jsonish(payload)
    with _runtime_diagnostics_cache_lock:
        _runtime_diagnostics_cache = (time.monotonic() + RUNTIME_DIAGNOSTICS_CACHE_TTL_SECONDS, snapshot)
    return _clone_jsonish(snapshot)


def _export_error_payload(exc: Exception) -> dict[str, Any]:
    message = str(exc)
    payload: dict[str, Any] = {
        "ok": False,
        "error": message,
        "errorKind": "export_failed",
    }
    if (
        "HWP/HWPX" in message
        or "valid HWP" in message
        or "valid HWPX" in message
        or "한컴오피스" in message
    ):
        payload["errorKind"] = "hangul_conversion_failed"
        payload["recoverySteps"] = [
            "한컴오피스에서 원본 HWP/HWPX를 PDF로 내보낸 뒤 PDF를 다시 업로드해 주세요.",
            "또는 HWP/HWPX를 PDF로 변환할 수 있는 로컬 변환기를 설치한 뒤 다시 실행해 주세요.",
            "암호, 배포용, DRM, 복사 방지 문서라면 보호를 해제하거나 권한 있는 PDF 내보내기를 사용해 주세요.",
        ]
    return payload


def _extract_ai_fallback_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("aiFallback")
    if not isinstance(nested, dict):
        nested = payload.get("ai_fallback")
    if not isinstance(nested, dict):
        nested = {}

    def _field(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in payload and payload[name] is not None:
                return payload[name]
        for name in names:
            if name in nested and nested[name] is not None:
                return nested[name]
        return default

    return {
        "ai_fallback_enabled": _coerce_bool(_field("aiFallbackEnabled", "ai_fallback_enabled", "enabled"), default=False),
        "ai_fallback": _field("aiFallbackMode", "ai_fallback_mode", "mode"),
        "ai_fallback_provider": str(_field("aiFallbackProvider", "ai_fallback_provider", "provider", default="gemini")),
        "ai_fallback_model": str(_field("aiFallbackModel", "ai_fallback_model", "model", default="")),
        "ai_fallback_prompt": str(_field("aiFallbackPrompt", "ai_fallback_prompt", "prompt", default="")),
        "ai_fallback_max_tokens": _coerce_optional_int(_field("aiFallbackMaxTokens", "ai_fallback_max_tokens", "maxTokens", "max_tokens")),
        "ai_fallback_temperature": _coerce_optional_float(_field("aiFallbackTemperature", "ai_fallback_temperature", "temperature")),
        "ai_fallback_threshold": _coerce_optional_float(_field("aiFallbackThreshold", "ai_fallback_threshold", "threshold")),
        "ai_fallback_max_regions": _coerce_optional_int(_field("aiFallbackMaxRegions", "ai_fallback_max_regions", "maxRegions", "max_regions")),
        "ai_fallback_timeout_ms": _coerce_optional_int(_field("aiFallbackTimeoutMs", "ai_fallback_timeout_ms", "timeoutMs", "timeout_ms")),
        "ai_fallback_save_debug": _coerce_bool(_field("aiFallbackSaveDebug", "ai_fallback_save_debug", "saveDebug", "save_debug"), default=False),
        "fail_on_ai_error": _coerce_bool(_field("failOnAiError", "fail_on_ai_error"), default=False),
    }


def _extract_input_intent(payload: dict[str, Any]) -> str:
    raw = payload.get("inputIntent") or payload.get("input_intent") or "auto"
    normalized = str(raw).strip().lower().replace("_", "-")
    return normalized if normalized in INPUT_INTENTS else "auto"


def _extract_content_target(payload: dict[str, Any]) -> str:
    raw = payload.get("contentTarget") or payload.get("content_target") or "all"
    normalized = str(raw).strip().lower().replace("_", "-")
    return normalized if normalized in CONTENT_TARGETS else "all"


def _extract_input_notes(payload: dict[str, Any]) -> str:
    raw = payload.get("inputNotes")
    if raw is None:
        raw = payload.get("input_notes")
    if raw is None:
        raw = payload.get("pastedText")
    return str(raw or "").strip()


_WINDOWS_RESERVED_PATH_COMPONENTS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
SAFE_PATH_COMPONENT_MAX_BYTES = 240
EDB_FILE_STEM_MAX_BYTES = 220
UPLOAD_FILE_NAME_MAX_BYTES = 180


def _is_windows_reserved_path_component(value: str | None) -> bool:
    component = str(value or "").strip().rstrip(" .")
    if not component:
        return False
    return component.split(".", 1)[0].upper() in _WINDOWS_RESERVED_PATH_COMPONENTS


def _truncate_utf8_bytes(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _sanitize_path_component_token(value: str | None) -> str:
    raw = str(value or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in raw)
    return safe.rstrip(" .")


def _finalize_safe_path_component(value: str, *, max_bytes: int) -> str:
    safe = _truncate_utf8_bytes(value, max_bytes).rstrip(" .")
    if _is_windows_reserved_path_component(safe):
        safe = "_" + _truncate_utf8_bytes(safe, max(0, max_bytes - 1))
    safe = _truncate_utf8_bytes(safe, max_bytes).rstrip(" .")
    return safe


def sanitize_output_dir_name(
    value: str | None,
    *,
    suffix: str | None = None,
    max_bytes: int = SAFE_PATH_COMPONENT_MAX_BYTES,
) -> str:
    raw = (value or "").strip()
    if not raw:
        raw = f"mvp_export_{time.strftime('%Y%m%d_%H%M%S')}"
    safe = _sanitize_path_component_token(raw)
    suffix_token = _sanitize_path_component_token(suffix)
    if suffix_token:
        suffix_tail = _truncate_utf8_bytes(f"_{suffix_token}", max_bytes)
        available_bytes = max(0, max_bytes - len(suffix_tail.encode("utf-8")))
        safe = f"{_truncate_utf8_bytes(safe, available_bytes).rstrip(' ._')}{suffix_tail}"
    safe = _finalize_safe_path_component(safe, max_bytes=max_bytes)
    if safe:
        return safe
    return _finalize_safe_path_component("mvp_export", max_bytes=max_bytes) or "export"


def _unique_artifact_stamp() -> str:
    """Return a readable, process-unique suffix for immutable artifact paths."""
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}_{uuid.uuid4().hex[:12]}"


def sanitize_upload_file_name(value: str | None) -> str:
    raw = Path(value or "upload.bin").name
    invalid = '<>:"/\\|?*'
    safe = "".join(ch if ch not in invalid and ord(ch) >= 32 else "_" for ch in raw).strip(" .")
    if not safe:
        return "upload.bin"

    path = Path(safe)
    extension = _truncate_utf8_bytes(path.suffix[:12], 32).rstrip(" .")
    stem = path.stem or "upload"
    digest = hashlib.sha1(safe.encode("utf-8", errors="ignore")).hexdigest()[:10]
    suffix_tail = f"_{digest}{extension}"
    available_bytes = max(1, UPLOAD_FILE_NAME_MAX_BYTES - len(suffix_tail.encode("utf-8")))
    trimmed_stem = _truncate_utf8_bytes(stem, available_bytes).rstrip(" ._") or "upload"
    if _is_windows_reserved_path_component(trimmed_stem):
        trimmed_stem = f"_{_truncate_utf8_bytes(trimmed_stem, max(1, available_bytes - 1))}"
    return _truncate_utf8_bytes(
        f"{trimmed_stem}{suffix_tail}",
        UPLOAD_FILE_NAME_MAX_BYTES,
    ).rstrip(" .")


def sanitize_edb_file_name(value: str | None, *, fallback_stem: str = "classin") -> str:
    def _clean_stem(raw: str | None, fallback: str) -> str:
        candidate = Path(str(raw or "")).name.strip()
        if candidate.lower().endswith(".edb"):
            candidate = candidate[:-4]
        candidate = candidate.strip(" .")
        if not candidate:
            return fallback
        safe = sanitize_output_dir_name(
            candidate,
            max_bytes=EDB_FILE_STEM_MAX_BYTES,
        ).strip(" ._")
        safe = _truncate_utf8_bytes(safe, EDB_FILE_STEM_MAX_BYTES).rstrip(" ._") or fallback
        if _is_windows_reserved_path_component(safe):
            safe = f"_{_truncate_utf8_bytes(safe, EDB_FILE_STEM_MAX_BYTES - 1)}"
        return _finalize_safe_path_component(safe, max_bytes=EDB_FILE_STEM_MAX_BYTES)

    fallback = _clean_stem(fallback_stem, "classin")
    stem = _clean_stem(value, fallback) if value is not None else fallback
    return f"{stem}.edb"


def validate_edb_file(path: str | Path, *, expected_min_records: int = 1) -> dict[str, Any]:
    """Fast structural check for the EDB envelope we write.

    This does not attempt to fully emulate ClassIn, but it catches the common
    failure modes that make a file unreadable: missing outer marker, corrupt
    gzip payload, truncated inner header, or a record hint that is impossible
    for the requested publish.
    """
    edb_path = Path(path)
    data = edb_path.read_bytes()
    if len(data) <= OUTER_EDB_PREFIX_LEN:
        raise ValueError("EDB is too small")
    if data[4:7] != b"edb":
        raise ValueError("EDB outer marker is missing")

    inner = gzip.decompress(data[OUTER_EDB_PREFIX_LEN:])
    if len(inner) < 30:
        raise ValueError("EDB inner payload is truncated")
    version_len = inner[16]
    version_end = 17 + version_len
    if version_end + 17 > len(inner):
        raise ValueError("EDB header is truncated")

    page_count_hint = struct.unpack_from(">H", inner, 0)[0]
    record_count_hint = struct.unpack_from(">H", inner, 2)[0]
    if record_count_hint < expected_min_records:
        raise ValueError(
            f"EDB record hint {record_count_hint} is below expected {expected_min_records}"
        )
    if page_count_hint < 1:
        raise ValueError("EDB page hint must be positive")

    record_offset = version_end + 17
    current = record_offset
    actual_records = 0
    while current + 4 <= len(inner):
        size = struct.unpack_from(">I", inner, current)[0]
        if size < 5:
            break
        max_end = current + size
        if max_end > len(inner) + 1:
            raise ValueError("EDB record extends past payload")
        actual_records += 1
        current = max_end
        if current >= len(inner):
            break
    if actual_records < expected_min_records:
        raise ValueError(
            f"EDB contains {actual_records} records, expected at least {expected_min_records}"
        )
    return {
        "outerSize": len(data),
        "innerSize": len(inner),
        "pageCountHint": page_count_hint,
        "recordCountHint": record_count_hint,
        "recordCountActual": actual_records,
    }


def _normalize_edb_part_payload(part: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(part)
    part_path = Path(str(normalized.get("edbPath") or normalized.get("edb_path") or "")).resolve()
    part_index = int(normalized.get("partIndex") or normalized.get("part_index") or 1)
    part_count = int(normalized.get("partCount") or normalized.get("part_count") or 1)
    record_count = int(normalized.get("recordCount") or normalized.get("record_count") or 0)
    page_count_hint = int(normalized.get("pageCountHint") or normalized.get("page_count_hint") or 0)
    normalized.update({
        "partIndex": part_index,
        "part_index": part_index,
        "partCount": part_count,
        "part_count": part_count,
        "edbFileName": normalized.get("edbFileName") or normalized.get("edb_file_name") or part_path.name,
        "edb_file_name": normalized.get("edbFileName") or normalized.get("edb_file_name") or part_path.name,
        "edbPath": str(part_path),
        "edb_path": str(part_path),
        "edbFileUri": path_to_api_url(part_path),
        "edb_file_uri": path_to_api_url(part_path),
        "edbFileExists": part_path.is_file(),
        "edb_file_exists": part_path.is_file(),
        "recordCount": record_count,
        "record_count": record_count,
        "pageCountHint": page_count_hint,
        "page_count_hint": page_count_hint,
    })
    return normalized


def _validate_edb_parts(edb_parts: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    normalized_parts: list[dict[str, Any]] = []
    total_outer_size = 0
    total_inner_size = 0
    total_record_hint = 0
    total_record_actual = 0
    max_page_count_hint = 0
    for raw_part in edb_parts:
        part = _normalize_edb_part_payload(raw_part)
        expected_records = max(1, int(part.get("recordCount") or part.get("record_count") or 0))
        validation = validate_edb_file(part["edbPath"], expected_min_records=expected_records)
        part["edbValidation"] = dict(validation)
        part["edb_validation"] = dict(validation)
        part["pageCountHint"] = int(validation.get("pageCountHint") or part["pageCountHint"])
        part["page_count_hint"] = part["pageCountHint"]
        if part["pageCountHint"] > CLASSIN_MAX_BOARD_PAGE_COUNT:
            raise ValueError(
                f"{part['edbFileName']} pageCountHint {part['pageCountHint']} exceeds "
                f"ClassIn limit {CLASSIN_MAX_BOARD_PAGE_COUNT}"
            )
        part["recordCountActual"] = int(validation.get("recordCountActual") or part["recordCount"])
        part["record_count_actual"] = part["recordCountActual"]
        part["outerSize"] = int(validation.get("outerSize") or 0)
        part["outer_size"] = part["outerSize"]
        part["innerSize"] = int(validation.get("innerSize") or 0)
        part["inner_size"] = part["innerSize"]
        total_outer_size += part["outerSize"]
        total_inner_size += part["innerSize"]
        total_record_hint += int(validation.get("recordCountHint") or 0)
        total_record_actual += part["recordCountActual"]
        max_page_count_hint = max(max_page_count_hint, part["pageCountHint"])
        normalized_parts.append(part)
    aggregate = {
        "outerSize": total_outer_size,
        "innerSize": total_inner_size,
        "pageCountHint": max_page_count_hint,
        "recordCountHint": total_record_hint,
        "recordCountActual": total_record_actual,
        "edbPartCount": len(normalized_parts),
        "edbSplit": len(normalized_parts) > 1,
        "edbParts": normalized_parts,
    }
    return aggregate, normalized_parts


def _template_with_board_page_count(template: LayoutTemplate, board_page_count: int) -> LayoutTemplate:
    return LayoutTemplate(
        name=template.name,
        board_page_count=max(1, int(board_page_count)),
        base_slot_height_pages=template.base_slot_height_pages,
        fixed_left_zone_ratio=template.fixed_left_zone_ratio,
        preserve_right_writing_zone=template.preserve_right_writing_zone,
        default_overflow_subjects=set(template.default_overflow_subjects),
        metadata=dict(template.metadata),
    )


def _edb_part_file_name(edb_name: str, part_index: int, part_count: int) -> str:
    if part_count <= 1:
        return edb_name
    path = Path(edb_name)
    suffix = path.suffix or ".edb"
    stem = path.stem or "classin"
    width = max(2, len(str(part_count)))
    return f"{stem}_part{part_index + 1:0{width}d}{suffix}"


def _placement_summary_end_pages(placement: dict[str, Any]) -> float:
    values: list[float] = []
    for key in (
        "record_bottom_y_pages",
        "actual_bottom_y_pages",
        "snapped_next_start_y_pages",
    ):
        try:
            raw_value = placement.get(key)
            if raw_value is not None:
                values.append(float(raw_value))
        except (TypeError, ValueError):
            continue
    return max(values, default=0.0)


def _placement_summaries_flow_end_pages(placements: list[dict[str, Any]]) -> float:
    return max((_placement_summary_end_pages(placement) for placement in placements), default=0.0)


def _first_placement_over_page_limit(placements: list[dict[str, Any]], max_pages: int) -> int | None:
    for index, placement in enumerate(placements):
        if _placement_summary_end_pages(placement) > max_pages + 1e-6:
            return index
    return None


def _write_classin_limited_edb_files_local(
    problem_entries: list[Any],
    template: LayoutTemplate,
    output_dir: Path,
    edb_name: str,
    *,
    record_mode: str,
    text_confidence_threshold: float,
    dark_board: bool,
    board_theme: str,
    crop_format: str,
    existing_records: list[Any] | None = None,
    existing_placements: list[dict[str, Any]] | None = None,
    existing_header_flag: int | None = None,
) -> list[dict[str, Any]]:
    chunks = split_problem_entries_for_classin_page_limit(
        problem_entries,
        template,
        max_page_count=CLASSIN_MAX_BOARD_PAGE_COUNT,
    )
    if not chunks:
        return []

    rendered_chunks: list[dict[str, Any]] = []

    def build_rendered_chunk(chunk_entries: list[Any], *, allow_existing: bool = False) -> dict[str, Any]:
        part_template = _template_with_board_page_count(template, CLASSIN_MAX_BOARD_PAGE_COUNT)
        can_reuse_existing = (
            allow_existing
            and len(chunk_entries) == len(problem_entries)
            and existing_records is not None
            and existing_placements is not None
            and existing_header_flag is not None
            and int(template.board_page_count) == CLASSIN_MAX_BOARD_PAGE_COUNT
        )
        if can_reuse_existing:
            part_records = list(existing_records or [])
            part_placements = [dict(placement) for placement in (existing_placements or [])]
            part_header_flag = int(existing_header_flag or 0)
        else:
            part_records, part_placements, part_header_flag = build_records(
                chunk_entries,
                part_template,
                record_mode=record_mode,
                output_dir=output_dir,
                text_confidence_threshold=text_confidence_threshold,
                dark_board=dark_board,
                board_theme=board_theme,
                crop_format=crop_format,
                reserve_image_layout_height=True,
                expand_board_capacity=False,
            )
        _validate_record_page_count_hints(
            part_placements,
            expected_page_count=CLASSIN_MAX_BOARD_PAGE_COUNT,
        )
        if not part_template.metadata.get("preserve_source_layout"):
            _validate_sequential_record_placements(part_placements)
        return {
            "entries": list(chunk_entries),
            "records": part_records,
            "placements": part_placements,
            "header_flag": part_header_flag,
            "flow_end_pages": _placement_summaries_flow_end_pages(part_placements),
            "page_count_hint": CLASSIN_MAX_BOARD_PAGE_COUNT,
        }

    def render_chunk(chunk_entries: list[Any], *, allow_existing: bool = False) -> None:
        rendered_chunk = build_rendered_chunk(chunk_entries, allow_existing=allow_existing)
        part_placements = list(rendered_chunk["placements"])
        flow_end_pages = float(rendered_chunk["flow_end_pages"])
        if flow_end_pages > CLASSIN_MAX_BOARD_PAGE_COUNT + 1e-6 and len(chunk_entries) > 1:
            split_index = _first_placement_over_page_limit(part_placements, CLASSIN_MAX_BOARD_PAGE_COUNT)
            if split_index is None or split_index <= 0:
                split_index = 1
            render_chunk(chunk_entries[:split_index])
            render_chunk(chunk_entries[split_index:])
            return
        if flow_end_pages > CLASSIN_MAX_BOARD_PAGE_COUNT + 1e-6:
            problem_id = str(getattr(chunk_entries[0], "problem_id", "unknown") or "unknown")
            raise ValueError(
                f"Problem '{problem_id}' exceeds ClassIn's {CLASSIN_MAX_BOARD_PAGE_COUNT}-page limit "
                f"after rendering ({flow_end_pages:.3f} pages); split the source before publishing"
            )

        rendered_chunks.append(rendered_chunk)

    for chunk_entries in chunks:
        render_chunk(list(chunk_entries), allow_existing=len(chunks) == 1)

    compacted_chunks: list[dict[str, Any]] = []
    for rendered_chunk in rendered_chunks:
        if compacted_chunks:
            candidate_entries = [
                *list(compacted_chunks[-1]["entries"]),
                *list(rendered_chunk["entries"]),
            ]
            candidate = build_rendered_chunk(candidate_entries)
            if float(candidate["flow_end_pages"]) <= CLASSIN_MAX_BOARD_PAGE_COUNT + 1e-6:
                compacted_chunks[-1] = candidate
                continue
        compacted_chunks.append(rendered_chunk)
    rendered_chunks = compacted_chunks

    part_count = len(rendered_chunks)
    parts: list[dict[str, Any]] = []
    for part_index, rendered_chunk in enumerate(rendered_chunks):
        chunk_entries = list(rendered_chunk["entries"])
        part_records = list(rendered_chunk["records"])
        part_placements = list(rendered_chunk["placements"])
        part_header_flag = int(rendered_chunk["header_flag"])
        part_name = _edb_part_file_name(edb_name, part_index, part_count)
        part_path = output_dir / part_name
        write_edb(
            part_path,
            build_edb(
                part_records,
                header_flag=part_header_flag,
                version=version_string_for_crop_format(crop_format),
                page_count_hint=CLASSIN_MAX_BOARD_PAGE_COUNT,
            ),
        )
        problem_ids = [
            str(getattr(entry, "problem_id", "") or "")
            for entry in chunk_entries
        ]
        parts.append(
            {
                "partIndex": part_index + 1,
                "part_index": part_index + 1,
                "partCount": part_count,
                "part_count": part_count,
                "edbFileName": part_path.name,
                "edb_file_name": part_path.name,
                "edbPath": str(part_path.resolve()),
                "edb_path": str(part_path.resolve()),
                "recordCount": len(part_records),
                "record_count": len(part_records),
                "placementCount": len(part_placements),
                "placement_count": len(part_placements),
                "pageCountHint": CLASSIN_MAX_BOARD_PAGE_COUNT,
                "page_count_hint": CLASSIN_MAX_BOARD_PAGE_COUNT,
                "flowEndPages": float(rendered_chunk.get("flow_end_pages") or 0.0),
                "flow_end_pages": float(rendered_chunk.get("flow_end_pages") or 0.0),
                "problemIds": problem_ids,
                "problem_ids": problem_ids,
                "placements": part_placements,
            }
        )
    return parts


def _annotate_session_with_edb_part_metadata(session: dict[str, Any], edb_parts: list[dict[str, Any]]) -> None:
    if not isinstance(session, dict) or not edb_parts:
        return
    part_by_problem_id: dict[str, dict[str, Any]] = {}
    placement_by_problem_id: dict[str, dict[str, Any]] = {}
    for part in edb_parts:
        if not isinstance(part, dict):
            continue
        raw_problem_ids = part.get("problemIds") if isinstance(part.get("problemIds"), list) else part.get("problem_ids")
        for problem_id in raw_problem_ids or []:
            part_by_problem_id[str(problem_id)] = part
        raw_placements = part.get("placements") if isinstance(part.get("placements"), list) else []
        for placement in raw_placements:
            if not isinstance(placement, dict):
                continue
            problem_id = str(placement.get("problem_id") or placement.get("problemId") or "")
            if problem_id:
                placement_by_problem_id[problem_id] = placement

    for problem in session.get("problems", []) or []:
        if not isinstance(problem, dict):
            continue
        problem_id = str(problem.get("id") or problem.get("problem_id") or "")
        part = part_by_problem_id.get(problem_id)
        if not part:
            continue
        part_index = int(part.get("partIndex") or part.get("part_index") or 1)
        problem["edbPartIndex"] = part_index
        problem["edb_part_index"] = part_index
        problem["edbPartFileName"] = part.get("edbFileName") or part.get("edb_file_name") or ""
        problem["edb_part_file_name"] = problem["edbPartFileName"]
        placement = placement_by_problem_id.get(problem_id)
        if placement:
            problem["edbLocalStartYPages"] = placement.get("start_y_pages")
            problem["edb_local_start_y_pages"] = placement.get("start_y_pages")
            problem["edbLocalBottomYPages"] = placement.get("record_bottom_y_pages") or placement.get("actual_bottom_y_pages")
            problem["edb_local_bottom_y_pages"] = placement.get("record_bottom_y_pages") or placement.get("actual_bottom_y_pages")
            problem["edbLocalRecordBottomYPages"] = placement.get("record_bottom_y_pages")
            problem["edb_local_record_bottom_y_pages"] = placement.get("record_bottom_y_pages")


def _path_from_file_uri(value: str) -> tuple[Path, bool]:
    parsed = urlparse(value)
    host = unquote(parsed.netloc or "").strip()
    raw_path = url2pathname(unquote(parsed.path or ""))
    if host and host.lower() != "localhost":
        unc_suffix = raw_path if raw_path.startswith(("/", "\\")) else f"/{raw_path}"
        unc_path = f"//{host}{unc_suffix}"
        return Path(unc_path), True
    return Path(raw_path), False


def decode_file_reference(value: str | None) -> Path | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("/api/file"):
        parsed = urlparse(text)
        raw = parse_qs(parsed.query).get("path", [None])[0]
        return decode_file_reference(raw)
    parsed = urlparse(text)
    if parsed.scheme == "file":
        path, is_unc = _path_from_file_uri(text)
        return path if is_unc else path.resolve()
    path = Path(text)
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def path_to_api_url(path: str | Path | None) -> str | None:
    if path is None:
        return None
    resolved = decode_file_reference(str(path))
    if resolved is None:
        return None
    return f"/api/file?path={quote(str(resolved))}"


def _path_as_file_uri(path: Path) -> str:
    normalized = str(path).replace("\\", "/")
    if normalized.startswith("//"):
        return f"file://{quote(normalized[2:], safe='/:')}"
    return path.resolve().as_uri()


def _remap_artifact_paths(value: Any, source_root: Path, target_root: Path) -> Any:
    source_text = str(source_root.resolve())
    target_text = str(target_root.resolve())
    source_uri = _path_as_file_uri(source_root)
    target_uri = _path_as_file_uri(target_root)
    if isinstance(value, str):
        if value.startswith("/api/file"):
            parsed = urlparse(value)
            raw_path = parse_qs(parsed.query).get("path", [None])[0]
            if raw_path:
                remapped_path = _remap_artifact_paths(
                    str(decode_file_reference(raw_path) or raw_path),
                    source_root,
                    target_root,
                )
                if isinstance(remapped_path, str) and remapped_path != str(
                    decode_file_reference(raw_path) or raw_path
                ):
                    return f"/api/file?path={quote(remapped_path)}"
        if value == source_text or value.startswith(f"{source_text}{os.sep}"):
            return f"{target_text}{value[len(source_text):]}"
        if value == source_uri or value.startswith(f"{source_uri}/"):
            return f"{target_uri}{value[len(source_uri):]}"
        return value
    if isinstance(value, Path):
        remapped = _remap_artifact_paths(str(value), source_root, target_root)
        return Path(remapped) if isinstance(remapped, str) else value
    if isinstance(value, list):
        return [_remap_artifact_paths(item, source_root, target_root) for item in value]
    if isinstance(value, dict):
        return {
            key: _remap_artifact_paths(item, source_root, target_root)
            for key, item in value.items()
        }
    return value


def _discard_staged_publish(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[app-server] staged publish cleanup failed for {path}: {exc}", file=sys.stderr)


def _source_identity_suffix(source_paths: list[Path]) -> str:
    identity = hashlib.sha256()
    for source_path in source_paths:
        resolved = source_path.resolve()
        identity.update(str(resolved).encode("utf-8", errors="surrogatepass"))
        identity.update(b"\0")
        try:
            with resolved.open("rb") as source_file:
                while chunk := source_file.read(1024 * 1024):
                    identity.update(chunk)
        except OSError:
            try:
                stat = resolved.stat()
            except OSError:
                continue
            identity.update(f"{stat.st_size}\0{stat.st_mtime_ns}".encode("ascii"))
        identity.update(b"\0")
    return identity.hexdigest()[:10]


def _rewrite_staged_artifact_references(staging_dir: Path, final_dir: Path) -> None:
    staging_text = str(staging_dir.resolve())
    final_text = str(final_dir.resolve())
    staging_uri = _path_as_file_uri(staging_dir)
    final_uri = _path_as_file_uri(final_dir)
    for path in staging_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            remapped = _remap_artifact_paths(payload, staging_dir, final_dir)
            _atomic_write_text(path, json.dumps(remapped, ensure_ascii=False, indent=2))
        elif path.suffix.lower() in {".md", ".js"}:
            payload = path.read_text(encoding="utf-8")
            _atomic_write_text(
                path,
                payload.replace(staging_text, final_text).replace(staging_uri, final_uri),
            )


def _parse_file_preview_max_dimension(query: dict[str, list[str]]) -> int | None:
    raw_value = query.get("previewMax", query.get("preview_max", [None]))[0]
    if raw_value is None:
        return None
    try:
        requested = int(raw_value)
    except (TypeError, ValueError):
        return None
    return max(FILE_PREVIEW_MIN_DIMENSION, min(FILE_PREVIEW_MAX_DIMENSION, requested))


@lru_cache(maxsize=32)
def _build_file_preview_payload(
    path_value: str,
    modified_ns: int,
    file_size: int,
    max_dimension: int,
) -> tuple[bytes, str] | None:
    del modified_ns, file_size  # cache-key inputs; the path is opened below
    from PIL import Image, ImageOps

    path = Path(path_value)
    with Image.open(path) as loaded:
        if max(loaded.size) <= max_dimension:
            return None
        image = ImageOps.exif_transpose(loaded).copy()

    image.thumbnail(
        (max_dimension, max_dimension),
        Image.Resampling.LANCZOS,
        reducing_gap=3.0,
    )
    has_alpha = "A" in image.getbands() or (
        image.mode == "P" and "transparency" in image.info
    )
    output = BytesIO()
    if has_alpha:
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        image.save(
            output,
            format="PNG",
            compress_level=6,
            optimize=False,
        )
        mime_type = "image/png"
    else:
        if image.mode != "RGB":
            image = image.convert("RGB")
        image.save(
            output,
            format="JPEG",
            quality=FILE_PREVIEW_JPEG_QUALITY,
            optimize=True,
            progressive=True,
        )
        mime_type = "image/jpeg"
    return output.getvalue(), mime_type


def _classin_handoff_readiness(path: Path | None) -> tuple[str, bool | None]:
    if path is None or not path.is_file():
        return "", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", None
    if not isinstance(payload, dict):
        return "", None
    status = str(payload.get("status") or payload.get("classinHandoffStatus") or "").strip()
    ready_raw = payload.get("readyForClassIn", payload.get("ready_for_classin"))
    ready = None if ready_raw is None else bool(ready_raw)
    return status, ready


def _passage_group_problem_count(group: dict[str, Any]) -> int:
    for key in ("problemNumbers", "problem_numbers", "childProblemNumbers", "child_problem_numbers"):
        value = group.get(key)
        if isinstance(value, list) and value:
            return len({str(item).strip() for item in value if str(item).strip()})
    raw_count = int(group.get("problemCount") or group.get("problem_count") or 0)
    fragment_count = int(group.get("fragmentProblemCount") or group.get("fragment_problem_count") or 0)
    return max(0, raw_count - fragment_count)


def _passage_review_reason_label(reason: Any) -> str:
    normalized = str(reason or "").strip()
    return PASSAGE_REVIEW_REASON_LABELS.get(normalized, normalized)


def _passage_review_reason_codes(items: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        values = item.get("reviewReasonCodes")
        if not isinstance(values, list):
            values = item.get("review_reason_codes")
        if not isinstance(values, list):
            continue
        for value in values:
            code = str(value or "").strip()
            if not code or code in seen:
                continue
            seen.add(code)
            codes.append(code)
    return codes


def _passage_review_reason_summary(items: list[dict[str, Any]]) -> str:
    return ", ".join(
        label
        for label in (_passage_review_reason_label(code) for code in _passage_review_reason_codes(items))
        if label
    )


def _source_problem_overlap_detail_label(
    groups: list[dict[str, Any]],
) -> str:
    details: list[str] = []
    for group in groups:
        page_id = str(group.get("sourcePageId") or group.get("source_page_id") or "").strip()
        raw_ratio = group.get("overlapAreaRatio", group.get("overlap_area_ratio", 0))
        try:
            ratio = float(raw_ratio)
        except (TypeError, ValueError):
            ratio = 0.0
        percent = f"{int(ratio * 100 + 0.5)}%" if ratio > 0 else ""
        detail = " ".join(part for part in (page_id, percent) if part)
        if detail:
            details.append(detail)
    return ", ".join(details)


def _source_problem_overlap_label(
    groups: list[dict[str, Any]],
    *,
    group_count: int | None = None,
) -> str:
    count = max(0, int(group_count if group_count is not None else len(groups)))
    if count <= 0:
        return ""
    return f"문항 영역 겹침 {count}건"


def _path_exists(value: Any, *, directory: bool = False) -> bool:
    path = decode_file_reference(str(value)) if value else None
    if path is None:
        return False
    return path.is_dir() if directory else path.is_file()


def _publish_artifact_state(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(summary, dict):
        return summary
    annotated = dict(summary)
    edb_path = annotated.get("edbPath") or annotated.get("edb_path")
    output_dir = annotated.get("outputDir") or annotated.get("output_dir")
    classin_handoff_path = annotated.get("classinHandoffPath") or annotated.get("classin_handoff_path")
    classin_handoff_markdown_path = (
        annotated.get("classinHandoffMarkdownPath")
        or annotated.get("classin_handoff_markdown_path")
    )
    edb_exists = _path_exists(edb_path)
    output_exists = _path_exists(output_dir, directory=True)
    raw_parts = annotated.get("edbParts") if isinstance(annotated.get("edbParts"), list) else annotated.get("edb_parts")
    edb_parts = [
        _normalize_edb_part_payload(part)
        for part in (raw_parts or [])
        if isinstance(part, dict)
    ]
    if edb_parts:
        edb_exists = any(bool(part.get("edbFileExists")) for part in edb_parts)
        annotated["edbParts"] = edb_parts
        annotated["edb_parts"] = edb_parts
        annotated["edbPartCount"] = len(edb_parts)
        annotated["edb_part_count"] = len(edb_parts)
        annotated["edbSplit"] = len(edb_parts) > 1
        annotated["edb_split"] = len(edb_parts) > 1
    annotated["edbFileExists"] = edb_exists
    annotated["outputDirExists"] = output_exists
    annotated["edb_file_exists"] = edb_exists
    annotated["output_dir_exists"] = output_exists
    annotated["classinHandoffUri"] = (
        annotated.get("classinHandoffUri")
        or annotated.get("classin_handoff_uri")
        or path_to_api_url(classin_handoff_path)
    )
    annotated["classinHandoffMarkdownUri"] = (
        annotated.get("classinHandoffMarkdownUri")
        or annotated.get("classin_handoff_markdown_uri")
        or path_to_api_url(classin_handoff_markdown_path)
    )
    handoff_status, ready_for_classin = _classin_handoff_readiness(
        decode_file_reference(str(classin_handoff_path)) if classin_handoff_path else None
    )
    annotated["classinHandoffStatus"] = (
        annotated.get("classinHandoffStatus")
        or annotated.get("classin_handoff_status")
        or handoff_status
    )
    if "readyForClassIn" in annotated:
        ready_value = bool(annotated["readyForClassIn"])
    elif "ready_for_classin" in annotated:
        ready_value = bool(annotated["ready_for_classin"])
    elif ready_for_classin is not None:
        ready_value = ready_for_classin
    else:
        ready_value = annotated["classinHandoffStatus"] == "ready_for_classin_review"
    annotated["readyForClassIn"] = ready_value
    annotated["classin_handoff_uri"] = annotated["classinHandoffUri"]
    annotated["classin_handoff_markdown_uri"] = annotated["classinHandoffMarkdownUri"]
    annotated["classin_handoff_status"] = annotated["classinHandoffStatus"]
    annotated["ready_for_classin"] = annotated["readyForClassIn"]
    passage_review_reason_label = str(
        annotated.get("passageReviewReasonLabel")
        or annotated.get("passage_review_reason_label")
        or ""
    ).strip()
    if not passage_review_reason_label:
        passage_review_items = annotated.get("passageReviewItems") or annotated.get("passage_review_items")
        if isinstance(passage_review_items, list):
            passage_review_reason_label = _passage_review_reason_summary(
                [item for item in passage_review_items if isinstance(item, dict)]
            )
    annotated["passageReviewReasonLabel"] = passage_review_reason_label
    annotated["passage_review_reason_label"] = passage_review_reason_label
    source_problem_overlap_groups = annotated.get("sourceProblemOverlapGroups")
    if not isinstance(source_problem_overlap_groups, list):
        source_problem_overlap_groups = annotated.get("source_problem_overlap_groups")
    normalized_source_problem_overlap_groups = [
        dict(group)
        for group in (source_problem_overlap_groups or [])
        if isinstance(group, dict)
    ]
    raw_source_problem_overlap_group_count = (
        annotated.get("sourceProblemOverlapGroupCount")
        if annotated.get("sourceProblemOverlapGroupCount") is not None
        else annotated.get("source_problem_overlap_group_count")
    )
    if (
        isinstance(raw_source_problem_overlap_group_count, (int, float, str))
        and str(raw_source_problem_overlap_group_count).isdigit()
    ):
        source_problem_overlap_group_count = int(raw_source_problem_overlap_group_count)
    else:
        source_problem_overlap_group_count = len(normalized_source_problem_overlap_groups)
    source_problem_overlap_detail_label = str(
        annotated.get("sourceProblemOverlapDetailLabel")
        or annotated.get("source_problem_overlap_detail_label")
        or _source_problem_overlap_detail_label(normalized_source_problem_overlap_groups)
        or ""
    ).strip()
    raw_source_problem_overlap_label = str(
        annotated.get("sourceProblemOverlapLabel")
        or annotated.get("source_problem_overlap_label")
        or ""
    ).strip()
    if source_problem_overlap_group_count > 0:
        source_problem_overlap_label = _source_problem_overlap_label(
            normalized_source_problem_overlap_groups,
            group_count=source_problem_overlap_group_count,
        )
    else:
        source_problem_overlap_label = raw_source_problem_overlap_label
    annotated["sourceProblemOverlapGroups"] = normalized_source_problem_overlap_groups
    annotated["source_problem_overlap_groups"] = normalized_source_problem_overlap_groups
    annotated["sourceProblemOverlapGroupCount"] = max(0, source_problem_overlap_group_count)
    annotated["source_problem_overlap_group_count"] = annotated["sourceProblemOverlapGroupCount"]
    annotated["sourceProblemOverlapLabel"] = source_problem_overlap_label
    annotated["source_problem_overlap_label"] = source_problem_overlap_label
    annotated["sourceProblemOverlapDetailLabel"] = source_problem_overlap_detail_label
    annotated["source_problem_overlap_detail_label"] = source_problem_overlap_detail_label
    return annotated


def _passage_review_queue_issue(item: dict[str, Any]) -> dict[str, Any]:
    problem_ids = _passage_review_item_problem_ids(item)
    fragment_problem_ids: list[str] = []
    for key in ("fragmentProblemIds", "fragment_problem_ids"):
        values = item.get(key)
        if isinstance(values, list):
            fragment_problem_ids.extend(str(value or "").strip() for value in values)
    fragment_problem_ids = list(dict.fromkeys(value for value in fragment_problem_ids if value))
    review_reason_codes: list[str] = []
    for key in ("reviewReasonCodes", "review_reason_codes"):
        values = item.get(key)
        if isinstance(values, list):
            review_reason_codes.extend(str(value or "").strip() for value in values)
    review_reason_codes = list(dict.fromkeys(value for value in review_reason_codes if value))
    risk_flags: list[str] = []
    for key in ("riskFlags", "risk_flags"):
        values = item.get(key)
        if isinstance(values, list):
            risk_flags.extend(str(value or "").strip() for value in values)
    risk_flags = list(dict.fromkeys(value for value in risk_flags if value))
    source_page_ids: list[str] = []
    for key in ("sourcePageIds", "source_page_ids"):
        values = item.get(key)
        if isinstance(values, list):
            source_page_ids.extend(str(value or "").strip() for value in values)
    source_page_ids = [value for value in source_page_ids if value]
    number_label = str(item.get("numberLabel") or item.get("number_label") or "").strip()
    group_id = str(item.get("groupId") or item.get("group_id") or "").strip()
    title = number_label or group_id or "지문"
    detail_message = str(item.get("message") or item.get("reviewMessage") or item.get("review_message") or "").strip()
    if detail_message:
        message = f"{detail_message} EDB 제작 전에 지문 병합/문항 상태를 확인해 주세요."
    else:
        message = f"{title} 지문 확인 항목이 남아 있습니다. EDB 제작 전에 지문 병합/문항 상태를 확인해 주세요."
    problem_count = _coerce_optional_int(item.get("problemCount") or item.get("problem_count"))
    fragment_problem_count = _coerce_optional_int(item.get("fragmentProblemCount") or item.get("fragment_problem_count"))
    return {
        "type": "passage_review_queue_remaining",
        "severity": "warning",
        "message": message,
        "problemId": problem_ids[0] if problem_ids else "",
        "problemTitle": title,
        "numberLabel": number_label,
        "groupId": group_id,
        "problemIds": problem_ids,
        "fragmentProblemIds": fragment_problem_ids,
        "sourcePageIds": source_page_ids,
        "problemCount": problem_count if problem_count is not None else 0,
        "fragmentProblemCount": fragment_problem_count if fragment_problem_count is not None else len(fragment_problem_ids),
        "reviewReasonCodes": review_reason_codes,
        "riskFlags": risk_flags,
        "continuesAcrossPages": bool(item.get("continuesAcrossPages") or item.get("continues_across_pages")),
        "fragment_problem_ids": fragment_problem_ids,
        "source_page_ids": source_page_ids,
        "problem_count": problem_count if problem_count is not None else 0,
        "fragment_problem_count": fragment_problem_count if fragment_problem_count is not None else len(fragment_problem_ids),
        "review_reason_codes": review_reason_codes,
        "risk_flags": risk_flags,
        "continues_across_pages": bool(item.get("continuesAcrossPages") or item.get("continues_across_pages")),
        "blocking": True,
    }


def _session_passage_review_queue_issues(
    session: dict[str, Any] | None,
    *,
    problems: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    actionable_flags: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(session, dict):
        return []
    raw_items = session.get("passageReviewItems")
    if not isinstance(raw_items, list):
        raw_items = session.get("passage_review_items")
    if not isinstance(raw_items, list):
        return []
    unresolved_problem_ids = _session_unresolved_review_problem_ids(
        problems=problems,
        pages=pages,
        actionable_flags=actionable_flags,
    )
    publish_problem_ids = {
        str(problem.get("id") or problem.get("problem_id") or "").strip()
        for problem in problems
        if str(problem.get("id") or problem.get("problem_id") or "").strip()
    }
    issues: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        problem_ids = _passage_review_item_problem_ids(item)
        if problem_ids:
            if not any(problem_id in publish_problem_ids for problem_id in problem_ids):
                continue
            if not any(problem_id in unresolved_problem_ids for problem_id in problem_ids):
                continue
        issues.append(_passage_review_queue_issue(item))
    return issues


def _session_problem_id_issues(problems: list[Any]) -> list[dict[str, Any]]:
    problems_by_id: dict[str, list[dict[str, Any]]] = {}
    issues: list[dict[str, Any]] = []
    for index, problem in enumerate(problems):
        if not isinstance(problem, dict):
            issues.append(
                {
                    "type": "missing_problem_id",
                    "severity": "error",
                    "message": "문항 데이터가 손상되어 내부 식별자를 확인할 수 없습니다.",
                    "problemId": "",
                    "problemTitle": "",
                    "entryIndex": index,
                    "entry_index": index,
                    "malformedEntry": True,
                    "malformed_entry": True,
                }
            )
            continue
        problem_id = str(problem.get("id") or problem.get("problem_id") or "").strip()
        if not problem_id:
            issues.append(
                {
                    "type": "missing_problem_id",
                    "severity": "error",
                    "message": "문항 내부 식별자가 비어 있어 EDB 제작을 중단했습니다.",
                    "problemId": "",
                    "problemTitle": str(problem.get("title") or problem.get("problemNumber") or ""),
                    "entryIndex": index,
                    "entry_index": index,
                }
            )
            continue
        problems_by_id.setdefault(problem_id, []).append(problem)

    for problem_id, matches in problems_by_id.items():
        if len(matches) < 2:
            continue
        source_page_ids = list(
            dict.fromkeys(
                str(problem.get("sourcePageId") or problem.get("source_page_id") or "").strip()
                for problem in matches
                if str(problem.get("sourcePageId") or problem.get("source_page_id") or "").strip()
            )
        )
        issues.append(
            {
                "type": "duplicate_problem_id",
                "severity": "error",
                "message": (
                    "서로 다른 문항이 같은 내부 식별자를 사용하고 있어 EDB 제작을 중단했습니다. "
                    "문항을 다시 인식하거나 분할한 뒤 재시도해 주세요."
                ),
                "problemId": problem_id,
                "problemTitle": str(matches[0].get("title") or matches[0].get("problemNumber") or ""),
                "problemIds": [problem_id],
                "problem_ids": [problem_id],
                "occurrenceCount": len(matches),
                "occurrence_count": len(matches),
                "sourcePageIds": source_page_ids,
                "source_page_ids": source_page_ids,
            }
        )
    return issues


def _session_publish_blocking_preflight(
    problems: list[dict[str, Any]],
    session: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checked_problems = [
        problem
        for problem in problems
        if isinstance(problem, dict) and not _session_problem_is_supplemental(problem)
    ]
    pages = [page for page in ((session or {}).get("pages") or []) if isinstance(page, dict)]
    review_session = dict(session or {})
    review_session["problems"] = checked_problems
    review_session["pages"] = pages
    review_summary = _session_review_summary(review_session)
    actionable_flags = set(review_summary.get("actionableRiskFlagCounts") or {})
    duplicate_groups: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    session_payload = session or {}
    template_payload = session_payload.get("template") if isinstance(session_payload.get("template"), dict) else {}
    template_metadata = (
        template_payload.get("metadata") if isinstance(template_payload.get("metadata"), dict) else {}
    )
    page_as_is = (
        str(session_payload.get("input_intent") or session_payload.get("inputIntent") or "").strip().lower()
        == "page-as-is"
        or str(template_metadata.get("placement_mode") or "").strip().lower() == "continuous-page-as-is"
    )
    if not page_as_is:
        issues.extend(dict(issue) for issue in _classin_page_chrome_artifact_issues(checked_problems))
    issues.extend(_session_problem_id_issues(problems))
    issues.extend(dict(issue) for issue in _classin_passage_group_source_reuse_issues(checked_problems))
    issues.extend(dict(issue) for issue in _classin_source_bbox_overlap_issues(checked_problems))
    issues.extend(
        _session_passage_review_queue_issues(
            session,
            problems=checked_problems,
            pages=pages,
            actionable_flags=actionable_flags,
        )
    )

    blocking_issues: list[dict[str, Any]] = []
    for issue in issues:
        issue_copy = dict(issue)
        issue_copy["blocking"] = True
        blocking_issues.append(issue_copy)

    status = "passed" if not blocking_issues else "blocked"
    preflight = {
        "status": status,
        "passed": not blocking_issues,
        "checkedProblemCount": len(checked_problems),
        "checked_problem_count": len(checked_problems),
        "issueCount": len(blocking_issues),
        "issue_count": len(blocking_issues),
        "issues": blocking_issues,
        "gate": "session_publish",
        "gateLabel": "EDB publish",
        "gate_label": "EDB publish",
    }
    return preflight, duplicate_groups


def _session_publish_preflight_blocked_payload(
    preflight: dict[str, Any],
    duplicate_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    issues = preflight.get("issues") if isinstance(preflight.get("issues"), list) else []
    issue_types = sorted(
        {
            str(issue.get("type") or "")
            for issue in issues
            if isinstance(issue, dict) and str(issue.get("type") or "")
        }
    )
    blocking_problem_ids: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        raw_ids = issue.get("problemIds") or issue.get("problem_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = []
        for value in [
            *raw_ids,
            issue.get("problemId") or issue.get("problem_id"),
            issue.get("nextProblemId") or issue.get("next_problem_id"),
        ]:
            problem_id = str(value or "").strip()
            if problem_id and problem_id not in blocking_problem_ids:
                blocking_problem_ids.append(problem_id)
    return {
        "ok": False,
        "code": "publish_preflight_blocked",
        "operation": "session_publish",
        "retryable": False,
        "error": "ClassIn 사전점검에서 제작 전 확인 문제가 발견되어 EDB publish를 중단했습니다.",
        "errorKind": "publish_preflight_blocked",
        "error_kind": "publish_preflight_blocked",
        "classinPreflight": preflight,
        "classin_preflight": preflight,
        "classinPreflightStatus": preflight.get("status"),
        "classin_preflight_status": preflight.get("status"),
        "classinPreflightPassed": False,
        "classin_preflight_passed": False,
        "classinPreflightIssueCount": int(preflight.get("issueCount") or 0),
        "classin_preflight_issue_count": int(preflight.get("issueCount") or 0),
        "recoverySteps": [
            "표시된 문항 오류를 수정하거나 원본 PDF를 다시 등록해 주세요.",
            "수정 후 같은 작업에서 다시 제작해 주세요.",
        ],
        "blockingDuplicateProblemNumberGroups": duplicate_groups,
        "blocking_duplicate_problem_number_groups": duplicate_groups,
        "blockingIssueTypes": issue_types,
        "blocking_issue_types": issue_types,
        "blockingProblemIds": blocking_problem_ids,
        "blocking_problem_ids": blocking_problem_ids,
    }


def _session_publish_summary(
    *,
    edb_path: str | Path,
    output_dir: str | Path,
    edb_validation: dict[str, Any],
    record_count: int,
    core_problem_count: int | None = None,
    supplemental_item_count: int | None = None,
    classin_handoff_path: str | Path | None = None,
    classin_handoff_markdown_path: str | Path | None = None,
    classin_preflight: dict[str, Any] | None = None,
    passage_groups: list[dict[str, Any]] | None = None,
    passage_group_count: int | None = None,
    passage_problem_count: int | None = None,
    cross_page_passage_group_count: int | None = None,
    passage_review_items: list[dict[str, Any]] | None = None,
    passage_review_item_count: int | None = None,
    cross_page_passage_review_item_count: int | None = None,
    passage_group_source_reuse_groups: list[dict[str, Any]] | None = None,
    passage_group_source_reuse_group_count: int | None = None,
    source_problem_overlap_groups: list[dict[str, Any]] | None = None,
    source_problem_overlap_group_count: int | None = None,
    layout_diagnostics: dict[str, Any] | None = None,
    edb_parts: list[dict[str, Any]] | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    resolved_edb_path = Path(edb_path).resolve()
    resolved_output_dir = Path(output_dir).resolve()
    resolved_classin_handoff_path = Path(classin_handoff_path).resolve() if classin_handoff_path else None
    resolved_classin_handoff_markdown_path = (
        Path(classin_handoff_markdown_path).resolve()
        if classin_handoff_markdown_path
        else None
    )
    record_count_actual = int(edb_validation.get("recordCountActual") or record_count or 0)
    record_count_hint = int(edb_validation.get("recordCountHint") or record_count_actual or 0)
    page_count_hint = int(edb_validation.get("pageCountHint") or 0)
    normalized_edb_parts = [
        _normalize_edb_part_payload(part)
        for part in (edb_parts or [])
        if isinstance(part, dict)
    ]
    if not normalized_edb_parts:
        normalized_edb_parts = [
            _normalize_edb_part_payload({
                "partIndex": 1,
                "partCount": 1,
                "edbPath": str(resolved_edb_path),
                "recordCount": record_count_actual,
                "pageCountHint": page_count_hint,
            })
        ]
    supplemental_count = max(0, int(supplemental_item_count or 0))
    if core_problem_count is None:
        core_count = max(0, int(record_count or record_count_actual) - supplemental_count)
    else:
        core_count = max(0, int(core_problem_count or 0))
    record_count_label = (
        f"{core_count}문항 + 자료 {supplemental_count}"
        if supplemental_count
        else f"{int(record_count or record_count_actual)}개 자료"
    )
    preflight = dict(classin_preflight or {})
    preflight_status = str(preflight.get("status") or "")
    preflight_issue_count = int(preflight.get("issueCount") or preflight.get("issue_count") or 0)
    preflight_passed = bool(preflight.get("passed")) if preflight else False
    normalized_passage_groups = [
        dict(group)
        for group in (passage_groups or [])
        if isinstance(group, dict)
    ]
    if passage_group_count is None:
        passage_group_count = len(normalized_passage_groups)
    if passage_problem_count is None:
        passage_problem_count = sum(_passage_group_problem_count(group) for group in normalized_passage_groups)
    if cross_page_passage_group_count is None:
        cross_page_passage_group_count = sum(
            1
            for group in normalized_passage_groups
            if group.get("continuesAcrossPages") or group.get("continues_across_pages")
        )
    normalized_passage_review_items = [
        dict(item)
        for item in (passage_review_items or [])
        if isinstance(item, dict)
    ]
    if passage_review_item_count is None:
        passage_review_item_count = len(normalized_passage_review_items)
    if cross_page_passage_review_item_count is None:
        cross_page_passage_review_item_count = sum(
            1
            for item in normalized_passage_review_items
            if item.get("continuesAcrossPages") or item.get("continues_across_pages")
        )
    passage_review_reason_label = _passage_review_reason_summary(normalized_passage_review_items)
    normalized_passage_group_source_reuse_groups = [
        dict(group)
        for group in (passage_group_source_reuse_groups or [])
        if isinstance(group, dict)
    ]
    if passage_group_source_reuse_group_count is None:
        passage_group_source_reuse_group_count = len(normalized_passage_group_source_reuse_groups)
    normalized_source_problem_overlap_groups = [
        dict(group)
        for group in (source_problem_overlap_groups or [])
        if isinstance(group, dict)
    ]
    if source_problem_overlap_group_count is None:
        source_problem_overlap_group_count = len(normalized_source_problem_overlap_groups)
    source_problem_overlap_label = _source_problem_overlap_label(
        normalized_source_problem_overlap_groups,
        group_count=source_problem_overlap_group_count,
    )
    source_problem_overlap_detail_label = _source_problem_overlap_detail_label(
        normalized_source_problem_overlap_groups,
    )
    normalized_layout_diagnostics = (
        dict(layout_diagnostics)
        if isinstance(layout_diagnostics, dict)
        else {}
    )
    layout_diagnostics_label = str(normalized_layout_diagnostics.get("label") or "").strip()
    handoff_status, ready_for_classin = _classin_handoff_readiness(resolved_classin_handoff_path)
    if ready_for_classin is None and handoff_status:
        ready_for_classin = handoff_status == "ready_for_classin_review"
    published_at = published_at or datetime.now().astimezone().isoformat(timespec="seconds")
    summary = {
        "validated": True,
        "statusLabel": "검증 완료",
        "edbFileName": resolved_edb_path.name,
        "edbPath": str(resolved_edb_path),
        "edbFileUri": path_to_api_url(resolved_edb_path),
        "edbParts": normalized_edb_parts,
        "edbPartCount": len(normalized_edb_parts),
        "edbSplit": len(normalized_edb_parts) > 1,
        "outputDir": str(resolved_output_dir),
        "classinHandoffPath": str(resolved_classin_handoff_path) if resolved_classin_handoff_path else None,
        "classinHandoffUri": path_to_api_url(resolved_classin_handoff_path),
        "classinHandoffMarkdownPath": (
            str(resolved_classin_handoff_markdown_path)
            if resolved_classin_handoff_markdown_path
            else None
        ),
        "classinHandoffMarkdownUri": path_to_api_url(resolved_classin_handoff_markdown_path),
        "classinHandoffStatus": handoff_status,
        "readyForClassIn": bool(ready_for_classin) if ready_for_classin is not None else False,
        "classinPreflight": preflight,
        "classinPreflightStatus": preflight_status,
        "classinPreflightPassed": preflight_passed,
        "classinPreflightIssueCount": preflight_issue_count,
        "passageGroups": normalized_passage_groups,
        "passageGroupCount": max(0, int(passage_group_count or 0)),
        "passageProblemCount": max(0, int(passage_problem_count or 0)),
        "crossPagePassageGroupCount": max(0, int(cross_page_passage_group_count or 0)),
        "passageReviewItems": normalized_passage_review_items,
        "passageReviewItemCount": max(0, int(passage_review_item_count or 0)),
        "crossPagePassageReviewItemCount": max(0, int(cross_page_passage_review_item_count or 0)),
        "passageReviewReasonLabel": passage_review_reason_label,
        "passageGroupSourceReuseGroups": normalized_passage_group_source_reuse_groups,
        "passageGroupSourceReuseGroupCount": max(0, int(passage_group_source_reuse_group_count or 0)),
        "sourceProblemOverlapGroups": normalized_source_problem_overlap_groups,
        "sourceProblemOverlapGroupCount": max(0, int(source_problem_overlap_group_count or 0)),
        "sourceProblemOverlapLabel": source_problem_overlap_label,
        "sourceProblemOverlapDetailLabel": source_problem_overlap_detail_label,
        "layoutDiagnostics": normalized_layout_diagnostics,
        "layoutDiagnosticsLabel": layout_diagnostics_label,
        "edbFileExists": resolved_edb_path.is_file(),
        "outputDirExists": resolved_output_dir.is_dir(),
        "recordCount": int(record_count or record_count_actual),
        "recordCountActual": record_count_actual,
        "recordCountHint": record_count_hint,
        "coreProblemCount": core_count,
        "supplementalItemCount": supplemental_count,
        "recordCountLabel": record_count_label,
        "pageCountHint": page_count_hint,
        "outerSize": int(edb_validation.get("outerSize") or 0),
        "innerSize": int(edb_validation.get("innerSize") or 0),
        "publishedAt": published_at,
        "edbValidation": dict(edb_validation),
    }
    summary["canDownload"] = bool(
        summary["edbFileExists"]
        or any(
            bool(part.get("edbFileExists") or part.get("edb_file_exists"))
            for part in normalized_edb_parts
        )
    )
    summary.update({
        "status_label": summary["statusLabel"],
        "edb_file_name": summary["edbFileName"],
        "edb_path": summary["edbPath"],
        "edb_file_uri": summary["edbFileUri"],
        "edb_parts": summary["edbParts"],
        "edb_part_count": summary["edbPartCount"],
        "edb_split": summary["edbSplit"],
        "output_dir": summary["outputDir"],
        "classin_handoff_path": summary["classinHandoffPath"],
        "classin_handoff_uri": summary["classinHandoffUri"],
        "classin_handoff_markdown_path": summary["classinHandoffMarkdownPath"],
        "classin_handoff_markdown_uri": summary["classinHandoffMarkdownUri"],
        "classin_handoff_status": summary["classinHandoffStatus"],
        "ready_for_classin": summary["readyForClassIn"],
        "classin_preflight": summary["classinPreflight"],
        "classin_preflight_status": summary["classinPreflightStatus"],
        "classin_preflight_passed": summary["classinPreflightPassed"],
        "classin_preflight_issue_count": summary["classinPreflightIssueCount"],
        "passage_groups": summary["passageGroups"],
        "passage_group_count": summary["passageGroupCount"],
        "passage_problem_count": summary["passageProblemCount"],
        "cross_page_passage_group_count": summary["crossPagePassageGroupCount"],
        "passage_review_items": summary["passageReviewItems"],
        "passage_review_item_count": summary["passageReviewItemCount"],
        "cross_page_passage_review_item_count": summary["crossPagePassageReviewItemCount"],
        "passage_review_reason_label": summary["passageReviewReasonLabel"],
        "passage_group_source_reuse_groups": summary["passageGroupSourceReuseGroups"],
        "passage_group_source_reuse_group_count": summary["passageGroupSourceReuseGroupCount"],
        "source_problem_overlap_groups": summary["sourceProblemOverlapGroups"],
        "source_problem_overlap_group_count": summary["sourceProblemOverlapGroupCount"],
        "source_problem_overlap_label": summary["sourceProblemOverlapLabel"],
        "source_problem_overlap_detail_label": summary["sourceProblemOverlapDetailLabel"],
        "layout_diagnostics": summary["layoutDiagnostics"],
        "layout_diagnostics_label": summary["layoutDiagnosticsLabel"],
        "edb_file_exists": summary["edbFileExists"],
        "output_dir_exists": summary["outputDirExists"],
        "record_count": summary["recordCount"],
        "record_count_actual": summary["recordCountActual"],
        "record_count_hint": summary["recordCountHint"],
        "core_problem_count": summary["coreProblemCount"],
        "supplemental_item_count": summary["supplementalItemCount"],
        "record_count_label": summary["recordCountLabel"],
        "page_count_hint": summary["pageCountHint"],
        "outer_size": summary["outerSize"],
        "inner_size": summary["innerSize"],
        "published_at": summary["publishedAt"],
        "edb_validation": summary["edbValidation"],
        "can_download": summary["canDownload"],
    })
    return summary


def _session_publish_history(
    source_session: dict[str, Any] | None,
    current_summary: dict[str, Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    source_session = source_session or {}
    existing = source_session.get("publish_history")
    if not isinstance(existing, list):
        existing = source_session.get("publishHistory")
    if not isinstance(existing, list):
        previous_summary = source_session.get("publish_summary")
        if not isinstance(previous_summary, dict):
            previous_summary = source_session.get("publishSummary")
        existing = [previous_summary] if isinstance(previous_summary, dict) else []
    history: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for raw in [current_summary, *existing]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        key = str(item.get("edbPath") or item.get("edb_path") or item.get("edbFileName") or item.get("edb_file_name") or "")
        if key and key in seen_paths:
            continue
        if key:
            seen_paths.add(key)
        history.append(item)
        if len(history) >= limit:
            break
    return history


def _coerce_review_bool(payload: dict[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok", "passed", "완료"}
    return default


def _classin_review_payload(
    payload: dict[str, Any],
    *,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    requested_status = str(payload.get("status") or payload.get("classinReviewStatus") or "").strip().lower()
    if requested_status not in {"passed", "needs_fix", "pending"}:
        requested_status = "passed" if payload.get("passed", True) is not False else "needs_fix"
    passed = requested_status == "passed"
    status_labels = {
        "passed": "ClassIn 확인 완료",
        "needs_fix": "ClassIn 재검수 필요",
        "pending": "ClassIn 검수 대기",
    }
    reviewed_at = reviewed_at or datetime.now().astimezone().isoformat(timespec="seconds")
    review = {
        "status": requested_status,
        "statusLabel": status_labels[requested_status],
        "status_label": status_labels[requested_status],
        "manualReviewRequired": not passed,
        "manual_review_required": not passed,
        "classinOpened": _coerce_review_bool(payload, "classinOpened", default=passed),
        "recordCountOk": _coerce_review_bool(payload, "recordCountOk", default=passed),
        "orderOk": _coerce_review_bool(payload, "orderOk", default=passed),
        "readabilityOk": _coerce_review_bool(payload, "readabilityOk", default=passed),
        "supplementalItemsOk": _coerce_review_bool(payload, "supplementalItemsOk", default=passed),
        "notes": str(payload.get("notes") or "").strip(),
        "reviewedAt": reviewed_at,
        "reviewed_at": reviewed_at,
    }
    return review


def _attach_classin_review_to_publish_summary(summary: dict[str, Any], review: dict[str, Any]) -> None:
    summary["classinReview"] = dict(review)
    summary["classin_review"] = dict(review)
    summary["classinReviewStatus"] = review["status"]
    summary["classinReviewStatusLabel"] = review["statusLabel"]
    summary["classinReviewPassed"] = review["status"] == "passed"
    summary["classin_review_status"] = review["status"]
    summary["classin_review_status_label"] = review["statusLabel"]
    summary["classin_review_passed"] = review["status"] == "passed"


def _apply_classin_review_result(
    session: dict[str, Any],
    payload: dict[str, Any],
    *,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    review = _classin_review_payload(payload, reviewed_at=reviewed_at)
    session["classinReview"] = dict(review)
    session["classin_review"] = dict(review)
    for key in ("publishSummary", "publish_summary"):
        if isinstance(session.get(key), dict):
            _attach_classin_review_to_publish_summary(session[key], review)
    for history_key in ("publishHistory", "publish_history"):
        history = session.get(history_key)
        if isinstance(history, list) and history and isinstance(history[0], dict):
            _attach_classin_review_to_publish_summary(history[0], review)
    return review


def _session_history_key(session: dict[str, Any]) -> str:
    for key in ("output_dir", "outputDir", "pages_json_path", "pagesJsonPath"):
        value = str(session.get(key) or "").strip()
        if value:
            return value
    name = str(session.get("session_name") or session.get("sessionName") or "session")
    generated_at = str(session.get("generated_at") or session.get("generatedAt") or "")
    source_files = "|".join(str(path) for path in (session.get("input_files") or session.get("inputFiles") or []))
    return f"{name}|{generated_at}|{source_files}"


def _session_history_entry(session: dict[str, Any], *, updated_at: str | None = None) -> dict[str, Any]:
    session_snapshot = json.loads(json.dumps(session))
    problems = [problem for problem in (session_snapshot.get("problems") or []) if isinstance(problem, dict)]
    counts = _session_problem_count_payload(problems)
    output_dir = str(session_snapshot.get("output_dir") or session_snapshot.get("outputDir") or "")
    generated_at = str(session_snapshot.get("generated_at") or session_snapshot.get("generatedAt") or "")
    session_name = str(session_snapshot.get("session_name") or session_snapshot.get("sessionName") or "새 세션")
    history_key = _session_history_key(session_snapshot)
    entry_id = hashlib.sha1(history_key.encode("utf-8", errors="ignore")).hexdigest()[:16]
    publish_summary = session_snapshot.get("publishSummary")
    if not isinstance(publish_summary, dict):
        publish_summary = session_snapshot.get("publish_summary")
    review_summary = session_snapshot.get("reviewSummary")
    if not isinstance(review_summary, dict):
        review_summary = session_snapshot.get("review_summary")
    input_intent = str(
        session_snapshot.get("inputIntent") or session_snapshot.get("input_intent") or ""
    ).strip()
    content_target = str(
        session_snapshot.get("contentTarget") or session_snapshot.get("content_target") or ""
    ).strip()
    page_count = len([
        page for page in (session_snapshot.get("pages") or []) if isinstance(page, dict)
    ])
    updated_value = updated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "id": entry_id,
        "sessionName": session_name,
        "session_name": session_name,
        "outputDir": output_dir,
        "output_dir": output_dir,
        "generatedAt": generated_at,
        "generated_at": generated_at,
        "updatedAt": updated_value,
        "updated_at": updated_value,
        "detectedProblemCount": counts["detected_problem_count"],
        "coreProblemCount": counts["core_problem_count"],
        "supplementalItemCount": counts["supplemental_item_count"],
        "inputIntent": input_intent,
        "input_intent": input_intent,
        "contentTarget": content_target,
        "content_target": content_target,
        "pageCount": page_count,
        "page_count": page_count,
        "publishSummary": publish_summary or None,
        "reviewSummary": review_summary or None,
        "inputFileCount": int(session_snapshot.get("input_file_count") or len(session_snapshot.get("input_files") or [])),
        "session": session_snapshot,
    }


def _session_history_with_session(
    history: list[dict[str, Any]],
    session: dict[str, Any],
    *,
    updated_at: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    entry = _session_history_entry(session, updated_at=updated_at)
    seen = {entry["id"]}
    merged = [entry]
    for raw in history:
        if not isinstance(raw, dict):
            continue
        entry_id = str(raw.get("id") or "")
        if not entry_id or entry_id in seen:
            continue
        seen.add(entry_id)
        merged.append(raw)
        if len(merged) >= limit:
            break
    return merged


def _public_session_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public_entries: list[dict[str, Any]] = []
    for raw in history:
        if not isinstance(raw, dict):
            continue
        item = {key: value for key, value in raw.items() if key != "session"}
        if isinstance(item.get("publishSummary"), dict):
            item["publishSummary"] = _publish_artifact_state(item["publishSummary"])
        if isinstance(item.get("publish_summary"), dict):
            item["publish_summary"] = _publish_artifact_state(item["publish_summary"])
        public_entries.append(item)
    return public_entries


def content_disposition_attachment(filename: str) -> str:
    fallback = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in {'"', "\\", ";"} else "_"
        for ch in filename
    ).strip()
    if not fallback or fallback in {".", ".."}:
        fallback = "download.edb"
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def load_latest_session() -> dict[str, Any] | None:
    with _session_storage_lock:
        _recover_interrupted_session_reset()
        if not LATEST_SESSION_JSON.exists():
            return None
        try:
            payload = json.loads(LATEST_SESSION_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return payload if isinstance(payload, dict) else None


def save_latest_session(session: dict[str, Any], path: Path | None = None) -> None:
    target = path or LATEST_SESSION_JSON
    payload = json.dumps(session, ensure_ascii=False, indent=2)
    with _session_storage_lock:
        if path is None or target == LATEST_SESSION_JSON:
            _recover_interrupted_session_reset()
        _atomic_write_text(target, payload)


def load_session_history(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or SESSION_HISTORY_JSON
    with _session_storage_lock:
        if path is None or target == SESSION_HISTORY_JSON:
            _recover_interrupted_session_reset()
        if not target.exists():
            return []
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def save_session_history(history: list[dict[str, Any]], path: Path | None = None) -> None:
    target = path or SESSION_HISTORY_JSON
    payload = json.dumps(history, ensure_ascii=False, indent=2)
    with _session_storage_lock:
        if path is None or target == SESSION_HISTORY_JSON:
            _recover_interrupted_session_reset()
        _atomic_write_text(target, payload)


def remember_session_history(
    session: dict[str, Any],
    *,
    path: Path | None = None,
    updated_at: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    target = path or SESSION_HISTORY_JSON
    # Keep read/merge/write inside one re-entrant critical section so concurrent
    # requests cannot silently discard another request's history entry.
    with _session_storage_lock:
        if path is None or target == SESSION_HISTORY_JSON:
            _recover_interrupted_session_reset()
        history = _session_history_with_session(
            load_session_history(target),
            session,
            updated_at=updated_at,
            limit=limit,
        )
        save_session_history(history, target)
    return history


def persist_latest_session_with_history(
    session: dict[str, Any],
    *,
    updated_at: str | None = None,
    limit: int = 10,
) -> tuple[list[dict[str, Any]], OSError | None]:
    """Commit the authoritative latest snapshot, then best-effort its history.

    The latest snapshot is the source of truth.  A history write failure must
    not leave in-memory state behind a latest-session file that already
    committed, otherwise a subsequent CAS mutation could overwrite it.
    """

    with _session_storage_lock:
        # Recover before reading history. Recovering only inside the later
        # save_latest_session call can restore an OLD history tombstone after
        # this merge was already calculated, then overwrite it with [NEW].
        _recover_interrupted_session_reset()
        history = _session_history_with_session(
            load_session_history(),
            session,
            updated_at=updated_at,
            limit=limit,
        )
        save_latest_session(session)
        try:
            save_session_history(history)
        except OSError as exc:
            return history, exc
    return history, None


def collect_session_file_paths(session: dict[str, Any]) -> set[str]:
    paths: set[str] = set()

    def add_path(value: Any) -> None:
        if not value:
            return
        resolved = decode_file_reference(str(value))
        if resolved and resolved.exists():
            paths.add(str(resolved.resolve()))

    def add_edb_part_paths(summary: dict[str, Any]) -> None:
        raw_parts = summary.get("edbParts") if isinstance(summary.get("edbParts"), list) else summary.get("edb_parts")
        for part in raw_parts or []:
            if not isinstance(part, dict):
                continue
            for key in ("edbPath", "edb_path", "edbFileUri", "edb_file_uri"):
                add_path(part.get(key))

    for key in (
        "edb_path",
        "edbPath",
        "edbFileUri",
        "edb_file_uri",
        "pages_json_path",
        "pagesJsonPath",
        "placements_json_path",
        "placementsJsonPath",
        "classin_handoff_path",
        "classinHandoffPath",
        "classinHandoffUri",
        "classin_handoff_uri",
        "classin_handoff_markdown_path",
        "classinHandoffMarkdownPath",
        "classinHandoffMarkdownUri",
        "classin_handoff_markdown_uri",
    ):
        add_path(session.get(key))
    add_edb_part_paths(session)

    for summary_key in ("publishSummary", "publish_summary"):
        summary = session.get(summary_key)
        if not isinstance(summary, dict):
            continue
        for key in (
            "edbPath",
            "edb_path",
            "edbFileUri",
            "edb_file_uri",
            "classinHandoffPath",
            "classin_handoff_path",
            "classinHandoffUri",
            "classin_handoff_uri",
            "classinHandoffMarkdownPath",
            "classin_handoff_markdown_path",
            "classinHandoffMarkdownUri",
            "classin_handoff_markdown_uri",
        ):
            add_path(summary.get(key))
        add_edb_part_paths(summary)

    for history_key in ("publishHistory", "publish_history"):
        for summary in session.get(history_key, []) or []:
            if not isinstance(summary, dict):
                continue
            for key in (
                "edbPath",
                "edb_path",
                "edbFileUri",
                "edb_file_uri",
                "classinHandoffPath",
                "classin_handoff_path",
                "classinHandoffUri",
                "classin_handoff_uri",
                "classinHandoffMarkdownPath",
                "classin_handoff_markdown_path",
                "classinHandoffMarkdownUri",
                "classin_handoff_markdown_uri",
            ):
                add_path(summary.get(key))
            add_edb_part_paths(summary)

    for value in session.get("rendered_page_paths", []):
        add_path(value)
    for value in session.get("rendered_page_file_uris", []):
        add_path(value)
    for value in session.get("input_files", []) or session.get("inputFiles", []):
        add_path(value)

    for problem in session.get("problems", []):
        for key in ("imagePath", "sourceImagePath", "boardRenderPath", "originalImagePath"):
            add_path(problem.get(key))

    for page in session.get("pages", []):
        for key in ("sourceImageUri", "sourceImagePath"):
            add_path(page.get(key))

    return paths


def collect_session_history_file_paths(history: list[dict[str, Any]]) -> set[str]:
    paths: set[str] = set()
    for entry in history:
        if not isinstance(entry, dict):
            continue
        paths |= collect_session_file_paths(entry)
        snapshot = entry.get("session")
        if isinstance(snapshot, dict):
            paths |= collect_session_file_paths(snapshot)
    return paths


def _session_artifact_protection_paths(session: dict[str, Any] | None) -> set[Path]:
    if not isinstance(session, dict):
        return set()
    protected = {Path(value).resolve() for value in collect_session_file_paths(session)}
    for key in ("output_dir", "outputDir"):
        raw_value = session.get(key)
        if not raw_value:
            continue
        decoded = decode_file_reference(str(raw_value))
        if decoded is not None:
            try:
                protected.add(decoded.resolve())
            except (OSError, ValueError):
                continue
    return protected


def _history_artifact_protection_paths(history: list[dict[str, Any]]) -> set[Path]:
    protected: set[Path] = set()
    for entry in history:
        if not isinstance(entry, dict):
            continue
        protected |= _session_artifact_protection_paths(entry)
        protected |= _session_artifact_protection_paths(
            entry.get("session") if isinstance(entry.get("session"), dict) else None
        )
    return protected


def _path_is_protected(path: Path, protected_paths: set[Path]) -> bool:
    return path in protected_paths or any(parent in protected_paths for parent in path.parents)


def cleanup_runtime_artifacts(
    *,
    runtime_dir: Path | None = None,
    active_session: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    dry_run: bool = True,
    min_age_seconds: float = DEFAULT_ARTIFACT_RETENTION_DAYS * 86_400,
    max_bytes: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Plan or remove old, unreferenced runtime artifacts.

    The default is intentionally non-destructive. Files referenced by the
    active session or any retained history snapshot are never selected.
    """
    if min_age_seconds < 0:
        raise ValueError("minAgeSeconds must be non-negative")
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("maxBytes must be non-negative")

    base = (runtime_dir or RUNTIME_DIR).resolve()
    current_session = active_session if active_session is not None else load_latest_session()
    current_history = history if history is not None else load_session_history()
    protected_paths = _session_artifact_protection_paths(current_session)
    protected_paths |= _history_artifact_protection_paths(current_history)
    observed_at = time.time() if now is None else float(now)

    files: list[tuple[Path, int, float, bool]] = []
    for root_name in RUNTIME_ARTIFACT_ROOT_NAMES:
        root = base / root_name
        if not root.is_dir():
            continue
        resolved_root = root.resolve()
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                resolved = path.resolve()
                resolved.relative_to(resolved_root)
                stat = path.stat()
            except (OSError, ValueError):
                continue
            files.append((path, int(stat.st_size), float(stat.st_mtime), _path_is_protected(resolved, protected_paths)))

    total_bytes = sum(size for _, size, _, _ in files)
    protected_bytes = sum(size for _, size, _, protected in files if protected)
    eligible = [
        item
        for item in files
        if not item[3] and observed_at - item[2] >= min_age_seconds
    ]
    eligible.sort(key=lambda item: (item[2], str(item[0])))

    if max_bytes is None:
        selected = eligible
    else:
        bytes_to_reclaim = max(0, total_bytes - max_bytes)
        selected = []
        selected_bytes = 0
        for item in eligible:
            if selected_bytes >= bytes_to_reclaim:
                break
            selected.append(item)
            selected_bytes += item[1]

    deleted_paths: list[str] = []
    errors: list[dict[str, str]] = []
    if not dry_run:
        for path, _, _, _ in selected:
            try:
                path.unlink()
                deleted_paths.append(str(path.resolve()))
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})
        for root_name in RUNTIME_ARTIFACT_ROOT_NAMES:
            root = base / root_name
            if not root.is_dir():
                continue
            for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass

    selected_bytes = sum(size for _, size, _, _ in selected)
    deleted_path_set = set(deleted_paths)
    return {
        "ok": not errors,
        "dryRun": bool(dry_run),
        "runtimeDir": str(base),
        "minAgeSeconds": float(min_age_seconds),
        "maxBytes": max_bytes,
        "totalFileCount": len(files),
        "totalBytes": total_bytes,
        "protectedFileCount": sum(1 for _, _, _, protected in files if protected),
        "protectedBytes": protected_bytes,
        "eligibleFileCount": len(eligible),
        "selectedFileCount": len(selected),
        "selectedBytes": selected_bytes,
        "selectedPaths": [str(path.resolve()) for path, _, _, _ in selected],
        "deletedFileCount": len(deleted_paths),
        "deletedBytes": sum(size for path, size, _, _ in selected if str(path.resolve()) in deleted_path_set),
        "deletedPaths": deleted_paths,
        "errors": errors,
    }


def _file_uri_to_path(value: Any) -> Path | None:
    return decode_file_reference(str(value)) if value else None


def _target_within_allowed_roots(target: Path) -> bool:
    roots = [BASE_DIR.resolve(), RUNTIME_DIR.resolve()]
    return any(str(target) == str(root) or str(target).startswith(str(root) + os.sep) for root in roots)


def _resolve_open_target(raw_path: Any, *, kind: str) -> Path:
    if not raw_path:
        raise ValueError("path is required")
    target = _file_uri_to_path(raw_path)
    if target is None:
        raise ValueError("path is required")
    try:
        target = target.resolve() if target.is_absolute() else (BASE_DIR / target).resolve()
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid path: {exc}") from exc
    if not _target_within_allowed_roots(target):
        raise ValueError("path outside allowed roots")
    if kind == "folder":
        if target.is_file():
            target = target.parent
        if not target.exists() or not target.is_dir():
            raise FileNotFoundError(f"folder not found: {target}")
        return target
    if kind == "file":
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"file not found: {target}")
        return target
    raise ValueError(f"unknown open target kind: {kind}")


def _open_system_target(target: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(target))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def _actual_height_pages_from_problem_image(
    problem: dict[str, Any],
    crop_path: Path,
    template: LayoutTemplate,
) -> float:
    try:
        fallback = _coerce_optional_float(
            problem.get("actualHeightPages")
            or problem.get("actual_height_pages")
            or problem.get("actualContentHeightPages")
            or problem.get("actual_content_height_pages")
        )
    except (TypeError, ValueError):
        fallback = None
    try:
        from PIL import Image

        with Image.open(crop_path) as image:
            problem_intent = (
                str(problem.get("inputIntent") or problem.get("input_intent") or "")
                .strip()
                .lower()
                .replace("_", "-")
            )
            if problem_intent == "page-as-is":
                return float(estimate_page_as_is_height_pages(image.size, template))
            return float(estimate_height_pages(image.size, template))
    except Exception:
        return float(fallback or template.base_slot_height_pages or 1.2)


def _problems_to_entries(problems: list[dict[str, Any]], *, template: LayoutTemplate | None = None) -> list[ProblemEntry]:
    resolved_template = template or LayoutTemplate(name="academy-default")
    entries: list[ProblemEntry] = []
    for problem in problems:
        if _session_problem_is_supplemental(problem):
            continue
        crop_path = _file_uri_to_path(problem.get("imagePath"))
        board_render_path = _file_uri_to_path(problem.get("boardRenderPath")) or crop_path
        if crop_path is None or not crop_path.exists():
            raise FileNotFoundError(f"problem {problem.get('id')} crop missing at {crop_path}")
        if not board_render_path.exists():
            board_render_path = crop_path
        bbox = problem.get("bbox") or {}
        entries.append(
            ProblemEntry(
                problem_id=str(problem.get("id") or ""),
                title=str(problem.get("title") or ""),
                problem_number=(int(problem["problemNumber"])
                                if isinstance(problem.get("problemNumber"), (int, float, str))
                                and str(problem.get("problemNumber")).isdigit()
                                else None),
                subject=resolve_subject(problem.get("subject")),
                source_page_id=str(problem.get("sourcePageId") or ""),
                source_path=str(problem.get("sourceFileName") or problem.get("sourcePageId") or ""),
                prepared_page=None,  # image-only mode never touches this
                bounds=Box(
                    left=float(bbox.get("left", 0.0)),
                    top=float(bbox.get("top", 0.0)),
                    width=float(bbox.get("width", 1000.0)),
                    height=float(bbox.get("height", 1000.0)),
                ),
                crop_path=crop_path,
                board_render_path=board_render_path,
                blocks=[],  # image-only mode doesn't use OCR blocks
                actual_height_pages=_actual_height_pages_from_problem_image(
                    problem,
                    crop_path,
                    resolved_template,
                ),
                overflow_allowed=bool(problem.get("overflowAllowed", True)),
                reading_heavy=bool(problem.get("readingHeavy", False)),
                risk_flags=[str(flag) for flag in (problem.get("riskFlags") or []) if flag],
                placement_x_ratio=_coerce_placement_x_ratio(problem),
                placement_y_ratio=_coerce_placement_y_ratio(problem),
                placement_scale_ratio=_coerce_placement_scale_ratio(problem),
                preserve_legacy_placement_scale=_problem_preserves_legacy_placement_scale(problem),
                processing_step=_normalize_processing_step(
                    problem.get("processingStep")
                    or problem.get("processing_step")
                    or problem.get("step")
                ),
                input_intent=(
                    str(problem.get("inputIntent") or problem.get("input_intent") or "")
                    .strip()
                    .lower()
                    .replace("_", "-")
                    or None
                ),
                force_full_page_bounds=bool(problem.get("forceFullPageBounds") or problem.get("force_full_page_bounds")),
                preserve_media_regions=[
                    dict(region)
                    for region in (
                        problem.get("preserveMediaRegions")
                        or problem.get("preserve_media_regions")
                        or []
                    )
                    if isinstance(region, dict)
                ],
            )
        )
    return entries


def _template_from_session(session: dict[str, Any]) -> LayoutTemplate:
    template_data = session.get("template") or {}
    kwargs: dict[str, Any] = {"name": str(template_data.get("name") or "academy-default")}
    for key in ("board_page_count", "base_slot_height_pages", "fixed_left_zone_ratio"):
        if key in template_data and template_data[key] is not None:
            kwargs[key] = template_data[key]
    if "preserve_right_writing_zone" in template_data:
        kwargs["preserve_right_writing_zone"] = bool(template_data["preserve_right_writing_zone"])
    template = LayoutTemplate(**kwargs)
    template.base_slot_height_pages = ONE_PROBLEM_SLOT_HEIGHT_PAGES
    metadata = template_data.get("metadata") if isinstance(template_data.get("metadata"), dict) else {}
    template.metadata.update(metadata)
    session_intent = (
        str(session.get("inputIntent") or session.get("input_intent") or "")
        .strip()
        .lower()
        .replace("_", "-")
    )
    template.metadata.setdefault(
        "placement_mode",
        "continuous-page-as-is" if session_intent == "page-as-is" else "one-problem-per-page",
    )
    return template


def rewrite_session_for_http(session: dict[str, Any]) -> dict[str, Any]:
    rewritten = json.loads(json.dumps(session))
    _backfill_passage_source_segments(
        rewritten,
        pages_json_pages=_session_pages_json_pages(session),
    )
    rewritten["edb_file_uri"] = path_to_api_url(
        session.get("edb_path") or session.get("edbPath") or session.get("edb_file_uri") or session.get("edbFileUri")
    )
    rewritten["classin_handoff_uri"] = path_to_api_url(
        session.get("classin_handoff_path") or session.get("classinHandoffPath")
        or session.get("classin_handoff_uri") or session.get("classinHandoffUri")
    )
    rewritten["classin_handoff_markdown_uri"] = path_to_api_url(
        session.get("classin_handoff_markdown_path") or session.get("classinHandoffMarkdownPath")
        or session.get("classin_handoff_markdown_uri") or session.get("classinHandoffMarkdownUri")
    )
    rewritten["rendered_page_file_uris"] = [path_to_api_url(value) for value in session.get("rendered_page_paths", [])]

    for problem in rewritten.get("problems", []):
        if not problem.get("sourcePageId") and problem.get("source_page_id"):
            problem["sourcePageId"] = str(problem.get("source_page_id") or "")
        problem["imagePath"] = path_to_api_url(problem.get("imagePath"))
        problem["sourceImagePath"] = path_to_api_url(problem.get("sourceImagePath"))
        problem["boardRenderPath"] = path_to_api_url(problem.get("boardRenderPath"))
        problem["originalImagePath"] = path_to_api_url(problem.get("originalImagePath"))

    for page in rewritten.get("pages", []):
        raw_problem_ids = page.get("problemIds")
        if not isinstance(raw_problem_ids, list):
            raw_problem_ids = page.get("problem_ids")
        problem_ids = [str(pid) for pid in (raw_problem_ids or []) if str(pid or "").strip()]
        page["problemIds"] = problem_ids
        page["problem_ids"] = problem_ids
        # Front-end loads page images through /api/file; the original
        # sourceImagePath is kept (server-side absolute path) for mutation
        # endpoints to re-open with PIL.
        page["sourceImageUri"] = path_to_api_url(page.get("sourceImagePath") or page.get("sourceImageUri"))
    return rewritten


def _passage_range_key(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        start = int(value.get("start"))
        end = int(value.get("end"))
    except (TypeError, ValueError):
        return None
    return (start, end) if start > 0 and end >= start else None


def _problem_passage_range_key(problem: dict[str, Any]) -> tuple[int, int] | None:
    metadata = problem.get("metadata") if isinstance(problem.get("metadata"), dict) else {}
    for value in (
        problem.get("passageRange"),
        problem.get("passage_range"),
        metadata.get("passage_range"),
    ):
        passage_range = _passage_range_key(value)
        if passage_range is not None:
            return passage_range
    return None


def _backfill_passage_source_segments(
    session: dict[str, Any],
    *,
    pages_json_pages: list[dict[str, Any]] | None = None,
) -> int:
    """Recover exact passage fragments for sessions created before sourceSegments.

    The legacy UI session retained only a union bbox, even though pages.json still
    contains the original left-to-right and cross-page passage blocks.  Restrict
    recovery to matching passage ranges and known source pages so child-question
    pages are never promoted to passage fragments merely because they share a
    passage group.
    """
    structured_pages = pages_json_pages if pages_json_pages is not None else _session_pages_json_pages(session)
    if not structured_pages:
        return 0

    session_pages_by_id = {
        str(page.get("id") or page.get("page_id") or "").strip(): page
        for page in (session.get("pages") or [])
        if isinstance(page, dict) and str(page.get("id") or page.get("page_id") or "").strip()
    }

    candidates: dict[tuple[int, int], list[tuple[int, float, float, str, dict[str, Any], dict[str, Any]]]] = {}
    for page_index, page in enumerate(structured_pages):
        page_id = str(page.get("page_id") or page.get("id") or "").strip()
        if not page_id:
            continue
        for block_index, block in enumerate(page.get("blocks") or []):
            if not isinstance(block, dict):
                continue
            metadata = block.get("metadata") if isinstance(block.get("metadata"), dict) else {}
            segmenter = str(metadata.get("segmenter") or "").strip().lower()
            marker_kind = str(metadata.get("marker_kind") or "").strip().lower()
            if "passage" not in segmenter and marker_kind not in {"passage_range", "passage_continuation"}:
                continue
            passage_range = _passage_range_key(metadata.get("passage_range"))
            bbox = block.get("bbox") if isinstance(block.get("bbox"), dict) else {}
            try:
                width = float(bbox.get("width") or 0)
                height = float(bbox.get("height") or 0)
            except (TypeError, ValueError):
                continue
            if passage_range is None or width <= 0 or height <= 0:
                continue
            raw_order = metadata.get("passage_fragment_index", block.get("reading_order", block_index + 1))
            try:
                within_page_order = float(raw_order)
            except (TypeError, ValueError):
                within_page_order = float(block_index + 1)
            try:
                left = float(bbox.get("left") or 0)
            except (TypeError, ValueError):
                left = 0.0
            candidates.setdefault(passage_range, []).append(
                (page_index, within_page_order, left, page_id, block, metadata)
            )

    recovered = 0
    for problem in session.get("problems") or []:
        if not isinstance(problem, dict):
            continue
        existing = problem.get("sourceSegments") or problem.get("source_segments")
        if isinstance(existing, list) and existing:
            continue
        if problem.get("manualStitch") or problem.get("manual_stitch"):
            continue
        role = str(problem.get("passageRole") or problem.get("passage_role") or "").strip().lower()
        if role not in {"passage_fragment", "shared_passage", "passage"}:
            continue
        passage_range = _problem_passage_range_key(problem)
        if passage_range is None:
            continue
        source_page_ids = {
            str(page_id).strip()
            for page_id in (
                problem.get("passageSourcePageIds")
                or problem.get("passage_source_page_ids")
                or []
            )
            if str(page_id or "").strip()
        }
        matched = [
            item for item in candidates.get(passage_range, [])
            if not source_page_ids or item[3] in source_page_ids
        ]
        if not matched:
            continue
        matched.sort(key=lambda item: (item[0], item[1], item[2]))
        source_segments: list[dict[str, Any]] = []
        for fragment_index, (_page_index, _within_order, _left, page_id, block, metadata) in enumerate(matched, start=1):
            bbox = block.get("bbox") or {}
            normalized_bbox = {
                "left": float(bbox.get("left") or 0),
                "top": float(bbox.get("top") or 0),
                "width": float(bbox.get("width") or 0),
                "height": float(bbox.get("height") or 0),
            }
            raw_column_index = metadata.get("column_index", fragment_index)
            try:
                column_index = int(raw_column_index)
            except (TypeError, ValueError):
                column_index = fragment_index
            source_segments.append({
                "sourcePageId": page_id,
                "source_page_id": page_id,
                "sourceBlockId": str(block.get("block_id") or ""),
                "source_block_id": str(block.get("block_id") or ""),
                "fragmentIndex": fragment_index,
                "fragment_index": fragment_index,
                "columnIndex": column_index,
                "column_index": column_index,
                "bbox": normalized_bbox,
                "recoveredFromPagesJson": True,
                "recovered_from_pages_json": True,
            })
        problem["sourceSegments"] = source_segments
        problem["source_segments"] = list(source_segments)
        problem["sourceSegmentsRecovered"] = True
        problem["source_segments_recovered"] = True
        problem_id = str(problem.get("id") or "").strip()
        if problem_id:
            for page_id in dict.fromkeys(segment["sourcePageId"] for segment in source_segments):
                page = session_pages_by_id.get(page_id)
                if page is None:
                    continue
                raw_problem_ids = page.get("problemIds")
                if not isinstance(raw_problem_ids, list):
                    raw_problem_ids = page.get("problem_ids")
                problem_ids = [str(value) for value in (raw_problem_ids or []) if str(value or "").strip()]
                if problem_id not in problem_ids:
                    problem_ids.append(problem_id)
                page["problemIds"] = problem_ids
                page["problem_ids"] = list(problem_ids)
        recovered += 1
    return recovered


def _find_problem(session: dict[str, Any], problem_id: str) -> tuple[int, dict[str, Any]]:
    for index, problem in enumerate(session.get("problems", [])):
        if isinstance(problem, dict) and str(problem.get("id")) == problem_id:
            return index, problem
    raise ValueError(f"problem not found: {problem_id}")


def _find_page(session: dict[str, Any], page_id: str) -> dict[str, Any]:
    for page in session.get("pages", []):
        if isinstance(page, dict) and str(page.get("id")) == page_id:
            return page
    raise ValueError(f"page not found: {page_id}")


def _resolve_session_path(value: Any) -> Path | None:
    """Coerce a session-stored value (file URI, /api/file URL, raw path) to a
    real filesystem path. Returns None if the value cannot be resolved."""
    return decode_file_reference(str(value)) if value else None


def _session_reextract_source_paths(session: dict[str, Any] | None) -> list[Path]:
    """Return preserved source files for a session-only re-extraction preview.

    Prefer the first-generation input documents.  Older/restored sessions may
    not retain those paths, so fall back to the source page images in page
    order.  Only existing regular files are returned and aliases resolving to
    the same file are de-duplicated.
    """

    if not isinstance(session, dict):
        return []

    def existing_unique(values: list[Any]) -> list[Path]:
        resolved_paths: list[Path] = []
        seen: set[str] = set()
        for value in values:
            path = _resolve_session_path(value)
            if path is None:
                continue
            try:
                resolved = path.resolve()
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            resolved_paths.append(resolved)
        return resolved_paths

    input_paths = existing_unique(
        list(session.get("input_files") or session.get("inputFiles") or [])
    )
    if input_paths:
        return input_paths

    page_values: list[Any] = []
    for page in session.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        page_values.append(page.get("sourceImagePath") or page.get("sourceImageUri"))
    return existing_unique(page_values)


def _next_problem_id(session: dict[str, Any], base: str, suffix: str) -> str:
    """Generate a problem id that does not collide with any existing problem.

    Splits and merges produce children whose ids reflect the parent so the
    UI can keep stable mappings across mutations; we append an integer if
    needed to avoid collisions when the same parent is split twice."""
    candidate = f"{base}-{suffix}"
    existing = {str(p.get("id")) for p in session.get("problems", []) if isinstance(p, dict) and p.get("id")}
    if candidate not in existing:
        return candidate
    counter = 2
    while f"{candidate}-{counter}" in existing:
        counter += 1
    return f"{candidate}-{counter}"


def _crop_dir_for_session(session: dict[str, Any]) -> Path:
    out = session.get("output_dir")
    if out:
        target = Path(str(out)) / "problem_crops"
    else:
        target = RUNTIME_DIR / "mutated_crops"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _make_crop_filename(problem_id: str, suffix: str) -> str:
    digest = hashlib.sha1(f"{problem_id}|{suffix}|{time.time_ns()}".encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"mutated_{digest}.png"


def _crop_refreshes_board_render(problem: dict[str, Any]) -> bool:
    step = _normalize_processing_step(
        str(problem.get("step") or problem.get("processingStep") or problem.get("processing_step") or "raw")
    )
    return step in {"s2", "s3"}


def _render_board_crop_from_raw(
    raw_crop_path: Path,
    board_crop_path: Path,
    problem: dict[str, Any] | None = None,
) -> None:
    from PIL import Image

    board_crop_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(raw_crop_path) as image:
        board_image = _build_transparent_reconstruction_image(
            image,
            board_theme=DEFAULT_BOARD_THEME,
            text_priority=_problem_prefers_text_preservation(
                (problem or {}).get("subject"),
                (problem or {}).get("sourceFileName"),
                (problem or {}).get("title"),
            ),
        )
        board_image.save(board_crop_path)


def _problem_skeleton_from_parent(parent: dict[str, Any]) -> dict[str, Any]:
    """Carry over the fields that survive a split/merge unchanged — the
    surgical fields (id, bbox, image paths, title) are filled in by caller."""
    skeleton = {
        "title": parent.get("title"),
        "problemNumber": parent.get("problemNumber"),
        "subject": parent.get("subject"),
        "sourceFileName": parent.get("sourceFileName"),
        "sourceImagePath": parent.get("sourceImagePath"),
        "overflowAllowed": parent.get("overflowAllowed"),
        "readingHeavy": parent.get("readingHeavy"),
        "sourcePageId": parent.get("sourcePageId"),
        "recordMode": parent.get("recordMode"),
        "step": _normalize_processing_step(
            parent.get("processingStep")
            or parent.get("processing_step")
            or parent.get("step")
        ),
        "processingStep": _normalize_processing_step(
            parent.get("processingStep")
            or parent.get("processing_step")
            or parent.get("step")
        ),
        "textRecordCount": parent.get("textRecordCount", 0),
        "imageRecordCount": parent.get("imageRecordCount", 1),
        "placementXRatio": _coerce_placement_x_ratio(parent),
        "placementYRatio": _coerce_placement_y_ratio(parent),
        "placementScaleRatio": _coerce_placement_scale_ratio(parent),
        "riskFlags": [],  # mutated entries lose the auto-detected risk
    }
    return skeleton


_MUTATED_CROP_LAYOUT_KEYS = (
    "actualHeightPages",
    "actual_height_pages",
    "actualContentHeightPages",
    "actual_content_height_pages",
    "startYPages",
    "start_y_pages",
    "snappedNextStartYPages",
    "snapped_next_start_y_pages",
    "actualBottomYPages",
    "actual_bottom_y_pages",
    "renderedBottomYPages",
    "rendered_bottom_y_pages",
    "overflowAmountPages",
    "overflow_amount_pages",
    "overflowViolation",
    "overflow_violation",
    "slotSpanCount",
    "slot_span_count",
)


def _mutated_crop_layout_template() -> LayoutTemplate:
    template = LayoutTemplate(name="academy-default")
    template.base_slot_height_pages = ONE_PROBLEM_SLOT_HEIGHT_PAGES
    return template


def _estimate_mutated_crop_height_pages(crop_path: Path) -> float:
    template = _mutated_crop_layout_template()
    try:
        from PIL import Image

        with Image.open(crop_path) as image:
            return float(estimate_height_pages(image.size, template))
    except Exception:
        return float(template.base_slot_height_pages)


def _refresh_mutated_crop_layout(entry: dict[str, Any], crop_path: Path) -> None:
    for key in _MUTATED_CROP_LAYOUT_KEYS:
        entry.pop(key, None)
    actual_height_pages = _estimate_mutated_crop_height_pages(crop_path)
    entry["actualHeightPages"] = actual_height_pages
    entry["actual_height_pages"] = actual_height_pages


def _refresh_session_problem_counts(session: dict[str, Any]) -> None:
    problems = [problem for problem in (session.get("problems") or []) if isinstance(problem, dict)]
    pages = [page for page in (session.get("pages") or []) if isinstance(page, dict)]
    counts = _session_problem_count_payload(problems)
    session.update(counts)
    session["detectedProblemCount"] = counts["detected_problem_count"]
    session["coreProblemCount"] = counts["core_problem_count"]
    session["supplementalItemCount"] = counts["supplemental_item_count"]
    summary = _session_review_summary(session)
    session["review_summary"] = summary
    session["reviewSummary"] = summary
    _normalize_session_passage_review_queue(
        session,
        unresolved_problem_ids=_session_unresolved_review_problem_ids(
            problems=problems,
            pages=pages,
            actionable_flags=set(summary.get("actionableRiskFlagCounts") or {}),
        ),
    )


def _metadata_from_page(page: dict[str, Any]) -> dict[str, Any]:
    metadata = page.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return page


def _metadata_list_count(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key)
    if isinstance(value, list):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _hwp_quality_from_page(page: dict[str, Any]) -> dict[str, Any] | None:
    metadata = _metadata_from_page(page)
    quality = metadata.get("hwp_conversion_quality")
    return quality if isinstance(quality, dict) else None


def _session_pages_json_pages(session: dict[str, Any]) -> list[dict[str, Any]]:
    pages_json_value = session.get("pages_json_path") or session.get("pagesJsonPath")
    pages_json_path = decode_file_reference(str(pages_json_value)) if pages_json_value else None
    if pages_json_path is None or not pages_json_path.exists():
        return []
    try:
        payload = json.loads(pages_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [page for page in payload if isinstance(page, dict)]


def _session_hwp_quality_pages(session: dict[str, Any]) -> list[dict[str, Any]]:
    inline_pages = [page for page in (session.get("pages") or []) if isinstance(page, dict)]
    if any(_hwp_quality_from_page(page) for page in inline_pages):
        return inline_pages
    return _session_pages_json_pages(session)


def _metadata_flag_is_truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _page_has_hwp_cache_metadata(page: dict[str, Any]) -> bool:
    metadata = _metadata_from_page(page)
    return "hwp_renderer_cache_hit" in metadata or "hwp_normalized_cache_hit" in metadata


def _session_hwp_cache_pages(session: dict[str, Any]) -> list[dict[str, Any]]:
    inline_pages = [page for page in (session.get("pages") or []) if isinstance(page, dict)]
    if any(_page_has_hwp_cache_metadata(page) for page in inline_pages):
        return inline_pages
    return _session_pages_json_pages(session)


def _session_warning_messages(session: dict[str, Any]) -> list[str]:
    warnings = session.get("warning_messages")
    if not isinstance(warnings, list):
        warnings = session.get("warningMessages")
    if not isinstance(warnings, list):
        return []
    return [str(message) for message in warnings if str(message or "").strip()]


NON_ACTIONABLE_REVIEW_RISK_FLAGS = {
    "duplicate_problem_number",
    "marker_document_continuation",
    "ocr_disabled",
}

HWP_COUNT_MATCH_DISMISSIBLE_REVIEW_RISK_FLAGS = {
    "fallback_grouping",
    "large_block_dominance",
    "no_problem_markers",
    "problem_per_block",
    "sparse_segmentation",
}

PUBLISH_PRESERVED_PROBLEM_METADATA_KEYS = (
    ("passageGroupId", "passage_group_id"),
    ("passageRange", "passage_range"),
    ("passageRole", "passage_role"),
    ("sharedPassageBlockIds", "shared_passage_block_ids"),
    ("passageChildProblemNumbers", "passage_child_problem_numbers"),
    ("passageSourcePageIds", "passage_source_page_ids"),
    ("passageContinuesAcrossPages", "passage_continues_across_pages"),
    ("passagePreQuestionContinuationBlockIds", "passage_pre_question_continuation_block_ids"),
)


def _session_problem_is_supplemental(problem: dict[str, Any]) -> bool:
    risk_flags = problem.get("riskFlags") or problem.get("risk_flags") or []
    if isinstance(risk_flags, list) and "marker_document_continuation" in {str(flag) for flag in risk_flags}:
        return True
    metadata = problem.get("metadata")
    if isinstance(metadata, dict) and metadata.get("marker_document_continuation"):
        return True
    problem_id = str(problem.get("id") or problem.get("problem_id") or "")
    return problem_id.endswith("-continuation")


def _has_session_metadata_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, set, tuple)):
        return bool(value)
    return True


def _clone_session_metadata_value(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def _copy_session_metadata_aliases(
    target: dict[str, Any],
    source: dict[str, Any],
    aliases: tuple[str, ...],
) -> None:
    if any(_has_session_metadata_value(target.get(key)) for key in aliases):
        return
    source_metadata = source.get("metadata")
    if not isinstance(source_metadata, dict):
        source_metadata = {}
    value: Any = None
    found = False
    for key in aliases:
        candidate = source.get(key)
        if _has_session_metadata_value(candidate):
            value = candidate
            found = True
            break
    if not found:
        for key in aliases:
            candidate = source_metadata.get(key)
            if _has_session_metadata_value(candidate):
                value = candidate
                found = True
                break
    if not found:
        return
    for key in aliases:
        target[key] = _clone_session_metadata_value(value)


def _copy_publish_problem_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
    for aliases in PUBLISH_PRESERVED_PROBLEM_METADATA_KEYS:
        _copy_session_metadata_aliases(target, source, aliases)


def _copy_session_metadata_aliases_overwrite(
    target: dict[str, Any],
    source: dict[str, Any],
    aliases: tuple[str, ...],
) -> None:
    source_metadata = source.get("metadata")
    if not isinstance(source_metadata, dict):
        source_metadata = {}
    value: Any = None
    found = False
    for key in aliases:
        candidate = source.get(key)
        if _has_session_metadata_value(candidate):
            value = candidate
            found = True
            break
    if not found:
        for key in aliases:
            candidate = source_metadata.get(key)
            if _has_session_metadata_value(candidate):
                value = candidate
                found = True
                break
    if not found:
        return
    for key in aliases:
        target[key] = _clone_session_metadata_value(value)


def _copy_publish_problem_layout_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
    for aliases in (
        ("inputIntent", "input_intent"),
        ("placementMode", "placement_mode"),
        ("forceFullPageBounds", "force_full_page_bounds"),
        ("preserveLegacyPlacementScale", "preserve_legacy_placement_scale"),
    ):
        _copy_session_metadata_aliases_overwrite(target, source, aliases)


def _session_actionable_problem_ids(
    *,
    problems: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    actionable_flags: set[str],
) -> set[str]:
    actionable_problem_ids: set[str] = set()
    for index, problem in enumerate(problems):
        problem_id = str(problem.get("id") or problem.get("problem_id") or f"problem-index-{index}")
        problem_flags = {
            str(flag or "").strip()
            for flag in (problem.get("riskFlags") or problem.get("risk_flags") or [])
            if str(flag or "").strip()
        }
        if _problem_review_status(problem) == "failed" or problem_flags.intersection(actionable_flags):
            actionable_problem_ids.add(problem_id)
    for page in pages:
        page_flags = {
            str(flag or "").strip()
            for flag in (page.get("riskFlags") or page.get("risk_flags") or [])
            if str(flag or "").strip()
        }
        if not page_flags.intersection(actionable_flags):
            continue
        page_problem_ids = [str(pid) for pid in (page.get("problemIds") or page.get("problem_ids") or []) if pid]
        actionable_problem_ids.update(page_problem_ids)
    return actionable_problem_ids


def _session_unresolved_review_problem_ids(
    *,
    problems: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    actionable_flags: set[str],
) -> set[str]:
    unresolved_problem_ids = _session_actionable_problem_ids(
        problems=problems,
        pages=pages,
        actionable_flags=actionable_flags,
    )
    for index, problem in enumerate(problems):
        problem_id = str(problem.get("id") or problem.get("problem_id") or f"problem-index-{index}")
        if _problem_review_status(problem) != "normal":
            unresolved_problem_ids.add(problem_id)
    for page in pages:
        page_status = str(page.get("reviewStatus") or page.get("review_status") or "").strip().lower()
        page_flags = {
            str(flag or "").strip()
            for flag in (page.get("riskFlags") or page.get("risk_flags") or [])
            if str(flag or "").strip()
        }
        if page_status not in {"check_needed", "failed"} and not page_flags.intersection(actionable_flags):
            continue
        page_problem_ids = [str(pid) for pid in (page.get("problemIds") or page.get("problem_ids") or []) if pid]
        unresolved_problem_ids.update(page_problem_ids)
    return unresolved_problem_ids


def _passage_review_item_problem_ids(item: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("problemIds", "problem_ids", "fragmentProblemIds", "fragment_problem_ids"):
        values = item.get(key)
        if isinstance(values, list):
            ids.extend(str(value or "").strip() for value in values)
    return [value for value in ids if value]


def _normalize_session_passage_review_queue(
    session: dict[str, Any],
    *,
    unresolved_problem_ids: set[str],
) -> None:
    raw_items = session.get("passageReviewItems")
    if not isinstance(raw_items, list):
        raw_items = session.get("passage_review_items")
    if not isinstance(raw_items, list):
        has_count_only_metadata = any(
            key in session
            for key in (
                "passageReviewItemCount",
                "passage_review_item_count",
                "crossPagePassageReviewItemCount",
                "cross_page_passage_review_item_count",
            )
        )
        if not has_count_only_metadata:
            return
        raw_items = []

    unresolved_items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        problem_ids = _passage_review_item_problem_ids(item)
        if not problem_ids or any(problem_id in unresolved_problem_ids for problem_id in problem_ids):
            unresolved_items.append(dict(item))

    cross_page_count = sum(
        1
        for item in unresolved_items
        if item.get("continuesAcrossPages") or item.get("continues_across_pages")
    )
    session["passageReviewItems"] = unresolved_items
    session["passage_review_items"] = unresolved_items
    session["passageReviewItemCount"] = len(unresolved_items)
    session["passage_review_item_count"] = len(unresolved_items)
    session["crossPagePassageReviewItemCount"] = cross_page_count
    session["cross_page_passage_review_item_count"] = cross_page_count


def _session_review_summary(session: dict[str, Any]) -> dict[str, Any]:
    problems = [problem for problem in (session.get("problems") or []) if isinstance(problem, dict)]
    pages = [page for page in (session.get("pages") or []) if isinstance(page, dict)]
    counts = _session_problem_count_payload(problems)
    review_status_counts = {"all": 0, "normal": 0, "check_needed": 0, "failed": 0}
    supplemental_review_status_counts = {"all": 0, "normal": 0, "check_needed": 0, "failed": 0}
    core_review_status_counts = {"all": 0, "normal": 0, "check_needed": 0, "failed": 0}
    risk_flag_counts: dict[str, int] = {}
    for problem in problems:
        status = _problem_review_status(problem)
        target_counts = (
            supplemental_review_status_counts
            if _session_problem_is_supplemental(problem)
            else core_review_status_counts
        )
        review_status_counts["all"] += 1
        review_status_counts[status] = review_status_counts.get(status, 0) + 1
        target_counts["all"] += 1
        target_counts[status] = target_counts.get(status, 0) + 1
        for flag in problem.get("riskFlags") or problem.get("risk_flags") or []:
            flag_text = str(flag or "").strip()
            if flag_text:
                risk_flag_counts[flag_text] = risk_flag_counts.get(flag_text, 0) + 1

    for page in pages:
        for flag in page.get("riskFlags") or page.get("risk_flags") or []:
            flag_text = str(flag or "").strip()
            if flag_text:
                risk_flag_counts[flag_text] = risk_flag_counts.get(flag_text, 0) + 1

    top_risk_flags = [
        {"flag": flag, "count": count}
        for flag, count in sorted(risk_flag_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    ]
    hwp_problem_count_mismatch_count = int(risk_flag_counts.get("hwp_problem_count_mismatch") or 0)
    hwp_oversegmentation_count = int(risk_flag_counts.get("hwp_oversegmentation") or 0)
    needs_review_count = int(review_status_counts.get("check_needed", 0)) + int(review_status_counts.get("failed", 0))
    hwp_text_extractors: dict[str, int] = {}
    hwp_text_problem_signal_count = 0
    hwp_layout_extractors: dict[str, int] = {}
    hwp_layout_problem_signal_count = 0
    hwp_layout_duplicate_skip_count = 0
    hwp_layout_page_count = 0
    hwp_layout_text_line_count = 0
    hwp_renderer_cache_hit_count = 0
    hwp_normalized_cache_hit_count = 0
    hwp_cache_hit_page_count = 0

    for page in _session_hwp_cache_pages(session):
        metadata = _metadata_from_page(page)
        renderer_cache_hit = _metadata_flag_is_truthy(metadata.get("hwp_renderer_cache_hit"))
        normalized_cache_hit = _metadata_flag_is_truthy(metadata.get("hwp_normalized_cache_hit"))
        if renderer_cache_hit:
            hwp_renderer_cache_hit_count += 1
        if normalized_cache_hit:
            hwp_normalized_cache_hit_count += 1
        if renderer_cache_hit or normalized_cache_hit:
            hwp_cache_hit_page_count += 1

    for page in _session_hwp_quality_pages(session):
        metadata = _metadata_from_page(page)
        quality = _hwp_quality_from_page(page)
        if quality is None:
            continue
        extractor = str(quality.get("hwp_text_extractor") or "").strip()
        if extractor:
            hwp_text_extractors[extractor] = hwp_text_extractors.get(extractor, 0) + 1
        try:
            numbered = int(quality.get("hwp_text_numbered_problem_count") or 0)
            stem = int(quality.get("hwp_text_stem_problem_count") or 0)
        except (TypeError, ValueError):
            numbered = 0
            stem = 0
        hwp_text_problem_signal_count = max(hwp_text_problem_signal_count, numbered, stem)
        layout_extractor = str(quality.get("hwp_layout_extractor") or "").strip()
        if layout_extractor:
            hwp_layout_extractors[layout_extractor] = hwp_layout_extractors.get(layout_extractor, 0) + 1
        try:
            layout_markers = int(quality.get("hwp_layout_problem_marker_count") or 0)
            layout_pages = int(quality.get("hwp_layout_page_count") or 0)
            layout_lines = int(quality.get("hwp_layout_text_line_count") or 0)
        except (TypeError, ValueError):
            layout_markers = 0
            layout_pages = 0
            layout_lines = 0
        hwp_layout_problem_signal_count = max(hwp_layout_problem_signal_count, layout_markers)
        hwp_layout_duplicate_skip_count += _metadata_list_count(metadata, "duplicate_problem_numbers_skipped")
        hwp_layout_page_count = max(hwp_layout_page_count, layout_pages)
        hwp_layout_text_line_count = max(hwp_layout_text_line_count, layout_lines)
    if hwp_layout_problem_signal_count > 0 and hwp_layout_duplicate_skip_count > 0:
        hwp_layout_problem_signal_count = max(0, hwp_layout_problem_signal_count - hwp_layout_duplicate_skip_count)

    warning_messages = _session_warning_messages(session)
    hwp_text_problem_delta = 0
    hwp_text_problem_count_status = "unknown"
    hwp_text_problem_count_message = ""
    hwp_text_problem_count_matches = False
    hwp_layout_problem_delta = 0
    hwp_layout_problem_count_status = "unknown"
    hwp_layout_problem_count_message = ""
    hwp_layout_problem_count_matches = False
    if hwp_text_problem_signal_count > 0:
        core_count = int(counts["core_problem_count"])
        hwp_text_problem_delta = core_count - hwp_text_problem_signal_count
        hwp_text_problem_count_matches = hwp_text_problem_delta == 0
        if hwp_text_problem_count_matches:
            hwp_text_problem_count_status = "match"
            hwp_text_problem_count_message = "HWP 텍스트 문항 수와 검출 문항 수가 일치합니다."
        else:
            hwp_text_problem_count_status = "mismatch"
            if hwp_text_problem_delta > 0:
                hwp_text_problem_count_message = (
                    f"검출 문항이 HWP 텍스트 기준보다 {hwp_text_problem_delta}개 많습니다. "
                    "표지·안내문·보충 자료를 확인하세요."
                )
            else:
                hwp_text_problem_count_message = (
                    f"HWP 텍스트 기준 문항이 검출보다 {abs(hwp_text_problem_delta)}개 많습니다. "
                    "누락 문항을 확인하세요."
                )
            warning_messages = [*warning_messages, hwp_text_problem_count_message]
    if hwp_layout_problem_signal_count > 0:
        core_count = int(counts["core_problem_count"])
        hwp_layout_problem_delta = core_count - hwp_layout_problem_signal_count
        hwp_layout_problem_count_matches = hwp_layout_problem_delta == 0
        if hwp_layout_problem_count_matches:
            hwp_layout_problem_count_status = "match"
            hwp_layout_problem_count_message = "HWP 레이아웃 문항 수와 검출 문항 수가 일치합니다."
        else:
            hwp_layout_problem_count_status = "mismatch"
            if hwp_layout_problem_delta > 0:
                hwp_layout_problem_count_message = (
                    f"검출 문항이 HWP 레이아웃 기준보다 {hwp_layout_problem_delta}개 많습니다. "
                    "표지·안내문·보충 자료를 확인하세요."
                )
            else:
                hwp_layout_problem_count_message = (
                    f"HWP 레이아웃 기준 문항이 검출보다 {abs(hwp_layout_problem_delta)}개 많습니다. "
                    "누락 문항을 확인하세요."
                )
            if hwp_text_problem_signal_count <= 0:
                warning_messages = [*warning_messages, hwp_layout_problem_count_message]
    non_actionable_risk_flags = set(NON_ACTIONABLE_REVIEW_RISK_FLAGS)
    if hwp_text_problem_count_matches or hwp_layout_problem_count_matches:
        non_actionable_risk_flags.update(HWP_COUNT_MATCH_DISMISSIBLE_REVIEW_RISK_FLAGS)
    actionable_risk_flag_counts = {
        flag: count
        for flag, count in sorted(risk_flag_counts.items())
        if flag not in non_actionable_risk_flags
    }
    top_actionable_risk_flags = [
        {"flag": flag, "count": count}
        for flag, count in sorted(actionable_risk_flag_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    ]
    actionable_flags = set(actionable_risk_flag_counts)
    actionable_problem_ids: set[str] = set()
    for index, problem in enumerate(problems):
        problem_id = str(problem.get("id") or problem.get("problem_id") or f"problem-index-{index}")
        problem_flags = {
            str(flag or "").strip()
            for flag in (problem.get("riskFlags") or problem.get("risk_flags") or [])
            if str(flag or "").strip()
        }
        if _problem_review_status(problem) == "failed" or problem_flags.intersection(actionable_flags):
            actionable_problem_ids.add(problem_id)
    actionable_page_count = 0
    for page in pages:
        page_flags = {
            str(flag or "").strip()
            for flag in (page.get("riskFlags") or page.get("risk_flags") or [])
            if str(flag or "").strip()
        }
        page_status = str(page.get("reviewStatus") or page.get("review_status") or "").strip().lower()
        page_confirmed = bool(page.get("pageReviewConfirmed") or page.get("page_review_confirmed"))
        page_problem_ids = [str(pid) for pid in (page.get("problemIds") or page.get("problem_ids") or []) if pid]
        if page_problem_ids:
            if page_flags.intersection(actionable_flags) or page_status in {"check_needed", "failed"}:
                actionable_problem_ids.update(page_problem_ids)
        elif not page_confirmed and (
            page_status in {"check_needed", "failed"}
            or bool(page_flags.intersection(actionable_flags))
        ):
            actionable_page_count += 1
    actionable_needs_review_count = len(actionable_problem_ids) + actionable_page_count
    return {
        "detectedProblemCount": counts["detected_problem_count"],
        "coreProblemCount": counts["core_problem_count"],
        "supplementalItemCount": counts["supplemental_item_count"],
        "reviewStatusCounts": review_status_counts,
        "coreReviewStatusCounts": core_review_status_counts,
        "supplementalReviewStatusCounts": supplemental_review_status_counts,
        "needsReviewCount": needs_review_count,
        "actionableNeedsReviewCount": actionable_needs_review_count,
        "riskFlagCounts": risk_flag_counts,
        "topRiskFlags": top_risk_flags,
        "hwpProblemCountMismatchCount": hwp_problem_count_mismatch_count,
        "hwpOversegmentationCount": hwp_oversegmentation_count,
        "actionableRiskFlagCounts": actionable_risk_flag_counts,
        "topActionableRiskFlags": top_actionable_risk_flags,
        "warningCount": len(warning_messages),
        "warningMessages": warning_messages,
        "hwpTextExtractors": hwp_text_extractors,
        "hwpTextProblemSignalCount": hwp_text_problem_signal_count,
        "hwpTextProblemCountStatus": hwp_text_problem_count_status,
        "hwpTextProblemCountMatches": hwp_text_problem_count_matches,
        "hwpTextProblemDelta": hwp_text_problem_delta,
        "hwpTextProblemCountMessage": hwp_text_problem_count_message,
        "hwpLayoutExtractors": hwp_layout_extractors,
        "hwpLayoutProblemSignalCount": hwp_layout_problem_signal_count,
        "hwpLayoutPageCount": hwp_layout_page_count,
        "hwpLayoutTextLineCount": hwp_layout_text_line_count,
        "hwpLayoutProblemCountStatus": hwp_layout_problem_count_status,
        "hwpLayoutProblemCountMatches": hwp_layout_problem_count_matches,
        "hwpLayoutProblemDelta": hwp_layout_problem_delta,
        "hwpLayoutProblemCountMessage": hwp_layout_problem_count_message,
        "hwpCacheHitPageCount": hwp_cache_hit_page_count,
        "hwpRendererCacheHitCount": hwp_renderer_cache_hit_count,
        "hwpNormalizedCacheHitCount": hwp_normalized_cache_hit_count,
    }


def _replace_problem(session: dict[str, Any], original_index: int, replacements: list[dict[str, Any]]) -> None:
    """Replace the problem at original_index with one or more replacements,
    keeping the rest of the list intact. Also updates the page's problemIds
    array to match the new ordering."""
    problems = list(session.get("problems") or [])
    original = problems[original_index]
    page_id = str(original.get("sourcePageId") or "")
    new_ids = [str(r["id"]) for r in replacements]

    problems[original_index : original_index + 1] = replacements
    session["problems"] = problems

    for page in session.get("pages", []):
        if not isinstance(page, dict):
            continue
        if str(page.get("id")) != page_id:
            continue
        ids = list(page.get("problemIds") or [])
        if str(original.get("id")) in ids:
            pos = ids.index(str(original.get("id")))
            ids[pos : pos + 1] = new_ids
        else:
            ids.extend(new_ids)
        page["problemIds"] = ids
        break
    _refresh_session_problem_counts(session)


def _remove_problems(session: dict[str, Any], problem_ids: set[str]) -> list[dict[str, Any]]:
    """Drop matching problems from session.problems and from each page's
    problemIds list. Returns the removed entries (in original order)."""
    normalized_problem_ids = {str(problem_id) for problem_id in problem_ids}
    removed: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for problem in session.get("problems") or []:
        if isinstance(problem, dict) and str(problem.get("id")) in normalized_problem_ids:
            removed.append(problem)
        else:
            kept.append(problem)
    session["problems"] = kept
    for page in session.get("pages", []):
        if not isinstance(page, dict):
            continue
        raw_page_ids = page.get("problemIds")
        if not isinstance(raw_page_ids, list):
            raw_page_ids = page.get("problem_ids")
        next_page_ids = [
            str(pid)
            for pid in (raw_page_ids or [])
            if str(pid) not in normalized_problem_ids
        ]
        page["problemIds"] = next_page_ids
        page["problem_ids"] = next_page_ids
    review_focus = session.get("reviewFocus")
    if not isinstance(review_focus, dict):
        review_focus = session.get("review_focus")
    if isinstance(review_focus, dict):
        raw_focus_ids = review_focus.get("problemIds")
        if not isinstance(raw_focus_ids, list):
            raw_focus_ids = review_focus.get("problem_ids")
        next_focus_ids = [
            str(pid)
            for pid in (raw_focus_ids or [])
            if str(pid) not in normalized_problem_ids
        ]
        if next_focus_ids:
            review_focus["problemIds"] = next_focus_ids
            review_focus["problem_ids"] = next_focus_ids
            session["reviewFocus"] = review_focus
            session["review_focus"] = review_focus
        else:
            session.pop("reviewFocus", None)
            session.pop("review_focus", None)
    _refresh_session_problem_counts(session)
    return removed


def _mutate_split(session: dict[str, Any], problem_id: str, split_y_ratio: float) -> dict[str, Any]:
    if not (0.05 < split_y_ratio < 0.95):
        raise ValueError("splitYRatio must be between 0.05 and 0.95")
    index, problem = _find_problem(session, problem_id)
    page_id = str(problem.get("sourcePageId") or "")
    page = _find_page(session, page_id)
    page_image_path = _resolve_session_path(page.get("sourceImagePath") or page.get("sourceImageUri"))
    if page_image_path is None or not page_image_path.exists():
        raise FileNotFoundError(f"page image missing for {page_id}: {page_image_path}")

    bbox = problem.get("bbox") or {}
    left = float(bbox.get("left", 0.0))
    top = float(bbox.get("top", 0.0))
    width = float(bbox.get("width", 0.0))
    height = float(bbox.get("height", 0.0))
    if width <= 0 or height <= 0:
        raise ValueError("problem bbox is empty — cannot split")
    cut = height * split_y_ratio
    upper = Box(left=left, top=top, width=width, height=cut)
    lower = Box(left=left, top=top + cut, width=width, height=height - cut)

    crop_dir = _crop_dir_for_session(session)
    from PIL import Image  # local import: PIL is already in build_problem_board_edb's deps

    page_image = Image.open(page_image_path).convert("RGB")
    upper_id = _next_problem_id(session, str(problem.get("id")), "u")
    upper_crop_path = crop_dir / _make_crop_filename(upper_id, "u")
    recrop_problem(page_image, upper, upper_crop_path)
    lower_id = _next_problem_id({**session, "problems": session.get("problems", []) + [{"id": upper_id}]}, str(problem.get("id")), "l")
    lower_crop_path = crop_dir / _make_crop_filename(lower_id, "l")
    recrop_problem(page_image, lower, lower_crop_path)

    parent_title = str(problem.get("title") or problem_id)

    def make_entry(new_id: str, new_bbox: Box, crop_path: Path, suffix: str) -> dict[str, Any]:
        entry = _problem_skeleton_from_parent(problem)
        entry["id"] = new_id
        entry["title"] = f"{parent_title} ({suffix})"
        entry["imagePath"] = crop_path.resolve().as_uri()
        # cutout regeneration is reserved for the AI workflow — for now the
        # board render path mirrors the rectangular crop. EDB build composites
        # onto the dark theme using the same source.
        entry["boardRenderPath"] = crop_path.resolve().as_uri()
        entry["bbox"] = {
            "left": new_bbox.left,
            "top": new_bbox.top,
            "width": new_bbox.width,
            "height": new_bbox.height,
        }
        _refresh_mutated_crop_layout(entry, crop_path)
        return entry

    upper_entry = make_entry(upper_id, upper, upper_crop_path, "위")
    lower_entry = make_entry(lower_id, lower, lower_crop_path, "아래")
    _replace_problem(session, index, [upper_entry, lower_entry])
    return session


def _mutate_merge(session: dict[str, Any], problem_ids: list[str]) -> dict[str, Any]:
    if len(problem_ids) < 2:
        raise ValueError("merge requires at least 2 problems")
    targets: list[tuple[int, dict[str, Any]]] = []
    for pid in problem_ids:
        index, problem = _find_problem(session, pid)
        targets.append((index, problem))
    page_ids = {str(p.get("sourcePageId")) for _, p in targets}
    if len(page_ids) != 1:
        raise ValueError("merge requires all problems on the same source page")
    page_id = next(iter(page_ids))
    page = _find_page(session, page_id)
    page_image_path = _resolve_session_path(page.get("sourceImagePath") or page.get("sourceImageUri"))
    if page_image_path is None or not page_image_path.exists():
        raise FileNotFoundError(f"page image missing for {page_id}: {page_image_path}")

    lefts, tops, rights, bottoms = [], [], [], []
    for _, problem in targets:
        bbox = problem.get("bbox") or {}
        left = float(bbox.get("left", 0.0))
        top = float(bbox.get("top", 0.0))
        width = float(bbox.get("width", 0.0))
        height = float(bbox.get("height", 0.0))
        if width <= 0 or height <= 0:
            raise ValueError(f"problem {problem.get('id')} has an empty bbox — cannot merge")
        lefts.append(left)
        tops.append(top)
        rights.append(left + width)
        bottoms.append(top + height)
    merged = Box(left=min(lefts), top=min(tops), width=max(rights) - min(lefts), height=max(bottoms) - min(tops))

    first_index = min(idx for idx, _ in targets)
    primary = targets[0][1]  # take the first listed problem as the metadata source
    crop_dir = _crop_dir_for_session(session)
    new_id = _next_problem_id(session, str(primary.get("id")), "m")
    new_crop_path = crop_dir / _make_crop_filename(new_id, "m")

    from PIL import Image
    page_image = Image.open(page_image_path).convert("RGB")
    recrop_problem(page_image, merged, new_crop_path)

    merged_entry = _problem_skeleton_from_parent(primary)
    merged_entry["id"] = new_id
    parent_title = str(primary.get("title") or new_id)
    merged_entry["title"] = f"{parent_title} 외 {len(targets) - 1}건"
    merged_entry["imagePath"] = new_crop_path.resolve().as_uri()
    merged_entry["boardRenderPath"] = new_crop_path.resolve().as_uri()
    merged_entry["bbox"] = {
        "left": merged.left,
        "top": merged.top,
        "width": merged.width,
        "height": merged.height,
    }
    _refresh_mutated_crop_layout(merged_entry, new_crop_path)
    # remove all originals; the page's problemIds will be cleaned up too.
    _remove_problems(session, {str(pid) for pid in problem_ids})
    # insert the merged entry at the position of the first removed problem
    # (relative to the post-removal list — adjust because earlier entries
    # may have been removed too).
    problems = session.get("problems") or []
    insert_at = min(first_index, len(problems))
    problems.insert(insert_at, merged_entry)
    session["problems"] = problems
    _refresh_session_problem_counts(session)
    # also slot the new id into the page's problemIds
    for p in session.get("pages", []):
        if not isinstance(p, dict):
            continue
        if str(p.get("id")) != page_id:
            continue
        ids = list(p.get("problemIds") or [])
        # find a reasonable insertion point — right after the first id that
        # already appears in the new problems list, or at the end.
        existing_ids_in_problems = [str(prob.get("id")) for prob in problems if isinstance(prob, dict)]
        insertion_index = len(ids)
        for idx, pid_in_page in enumerate(ids):
            if pid_in_page in existing_ids_in_problems:
                position_in_problems = existing_ids_in_problems.index(pid_in_page)
                if position_in_problems >= insert_at:
                    insertion_index = idx
                    break
        ids.insert(insertion_index, new_id)
        p["problemIds"] = ids
        break
    return session


def _coerce_problem_ids(problem_ids: Any) -> list[str]:
    if isinstance(problem_ids, str):
        raw_ids = [problem_ids]
    else:
        raw_ids = list(problem_ids or [])
    ids: list[str] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        problem_id = str(raw_id or "").strip()
        if not problem_id or problem_id in seen:
            continue
        ids.append(problem_id)
        seen.add(problem_id)
    return ids


MANUAL_CROP_RATIO_KEYS = {
    "leftRatio": ("leftRatio", "left", "cropLeftRatio", "crop_left_ratio"),
    "rightRatio": ("rightRatio", "right", "cropRightRatio", "crop_right_ratio"),
    "topRatio": ("topRatio", "top", "cropTopRatio", "crop_top_ratio"),
    "bottomRatio": ("bottomRatio", "bottom", "cropBottomRatio", "crop_bottom_ratio"),
}
MANUAL_CROP_EDGE_MAX = 0.85
MANUAL_CROP_OUTSET_MAX = 0.60


def _coerce_manual_crop_ratios(raw_crop: Any) -> dict[str, float]:
    crop = raw_crop if isinstance(raw_crop, dict) else {}
    ratios: dict[str, float] = {}
    for target_key, aliases in MANUAL_CROP_RATIO_KEYS.items():
        raw_value = None
        for alias in aliases:
            if alias in crop:
                raw_value = crop.get(alias)
                break
        try:
            value = float(raw_value if raw_value is not None else 0.0)
        except (TypeError, ValueError):
            value = 0.0
        ratios[target_key] = max(-MANUAL_CROP_OUTSET_MAX, min(MANUAL_CROP_EDGE_MAX, value))
    if ratios["leftRatio"] + ratios["rightRatio"] >= 0.92:
        raise ValueError("left/right crop is too large")
    if ratios["topRatio"] + ratios["bottomRatio"] >= 0.92:
        raise ValueError("top/bottom crop is too large")
    return ratios


def _bbox_from_problem(problem: dict[str, Any], *, prefer_crop_base: bool = False) -> Box:
    raw_bbox = problem.get("cropBaseBbox") if prefer_crop_base else None
    if not isinstance(raw_bbox, dict):
        raw_bbox = problem.get("bbox") or {}
    left = float(raw_bbox.get("left", 0.0))
    top = float(raw_bbox.get("top", 0.0))
    width = float(raw_bbox.get("width", 0.0))
    height = float(raw_bbox.get("height", 0.0))
    if width <= 0 or height <= 0:
        raise ValueError("problem bbox is empty")
    return Box(left=left, top=top, width=width, height=height)


def _bbox_with_manual_crop(base: Box, crop: dict[str, float]) -> Box:
    left_trim = base.width * crop["leftRatio"]
    right_trim = base.width * crop["rightRatio"]
    top_trim = base.height * crop["topRatio"]
    bottom_trim = base.height * crop["bottomRatio"]
    width = base.width - left_trim - right_trim
    height = base.height - top_trim - bottom_trim
    if width < 1 or height < 1:
        raise ValueError("manual crop leaves the problem too small")
    return Box(
        left=base.left + left_trim,
        top=base.top + top_trim,
        width=width,
        height=height,
    )


def _manual_crop_from_bbox(base: Box, box: Box) -> dict[str, float]:
    if base.width <= 0 or base.height <= 0:
        return {"leftRatio": 0.0, "rightRatio": 0.0, "topRatio": 0.0, "bottomRatio": 0.0}
    return _coerce_manual_crop_ratios({
        "leftRatio": (box.left - base.left) / base.width,
        "rightRatio": (base.right - box.right) / base.width,
        "topRatio": (box.top - base.top) / base.height,
        "bottomRatio": (base.bottom - box.bottom) / base.height,
    })


def _page_for_problem(session: dict[str, Any], problem: dict[str, Any]) -> dict[str, Any] | None:
    page_id = str(problem.get("sourcePageId") or problem.get("source_page_id") or "")
    if not page_id:
        return None
    try:
        return _find_page(session, page_id)
    except ValueError:
        return None


def _source_page_path_for_problem(session: dict[str, Any], problem: dict[str, Any]) -> Path | None:
    page = _page_for_problem(session, problem)
    if not page:
        return None
    return _resolve_session_path(page.get("sourceImagePath") or page.get("sourceImageUri"))


def _coerce_crop_box(raw_box: Any, *, image_width: int, image_height: int) -> Box:
    box = raw_box if isinstance(raw_box, dict) else {}
    try:
        left = float(box.get("left", box.get("x", 0.0)))
        top = float(box.get("top", box.get("y", 0.0)))
        width = float(box.get("width", box.get("w", 0.0)))
        height = float(box.get("height", box.get("h", 0.0)))
        right = box.get("right")
        bottom = box.get("bottom")
        if (width <= 0 or height <= 0) and right is not None and bottom is not None:
            width = float(right) - left
            height = float(bottom) - top
    except (TypeError, ValueError):
        raise ValueError("cropBox must include numeric left/top/width/height") from None
    if width <= 0 or height <= 0:
        raise ValueError("cropBox is empty")
    left = max(0.0, min(float(image_width) - 1.0, left))
    top = max(0.0, min(float(image_height) - 1.0, top))
    right_edge = max(left + 1.0, min(float(image_width), left + width))
    bottom_edge = max(top + 1.0, min(float(image_height), top + height))
    return Box(left=left, top=top, width=right_edge - left, height=bottom_edge - top)


def _manual_crop_is_empty(crop: dict[str, float]) -> bool:
    return all(abs(value) <= 0.0001 for value in crop.values())


def _crop_image_by_ratios(source_path: Path, crop: dict[str, float], output_path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(source_path) as image:
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            image = image.convert("RGBA")
        width, height = image.size
        left = int(round(width * crop["leftRatio"]))
        right = int(round(width * (1.0 - crop["rightRatio"])))
        top = int(round(height * crop["topRatio"]))
        bottom = int(round(height * (1.0 - crop["bottomRatio"])))
        left = max(0, min(width - 1, left))
        top = max(0, min(height - 1, top))
        right = max(left + 1, min(width, right))
        bottom = max(top + 1, min(height, bottom))
        cropped = image.crop((left, top, right, bottom))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(output_path)
        return cropped.size


def _crop_image_by_bbox(source_path: Path, bbox: Box, output_path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(source_path) as image:
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            image = image.convert("RGBA")
        left = int(max(0, min(image.width - 1, math.floor(bbox.left))))
        top = int(max(0, min(image.height - 1, math.floor(bbox.top))))
        right = int(max(left + 1, min(image.width, math.ceil(bbox.right))))
        bottom = int(max(top + 1, min(image.height, math.ceil(bbox.bottom))))
        cropped = image.crop((left, top, right, bottom))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(output_path)
        return cropped.size


def _materialize_crop_base_image(
    session: dict[str, Any],
    problem_id: str,
    source_path: Path,
    base_bbox: Box,
) -> str:
    crop_dir = _crop_dir_for_session(session)
    base_crop_path = crop_dir / _make_crop_filename(problem_id, "crop_base")
    _crop_image_by_bbox(source_path, base_bbox, base_crop_path)
    return base_crop_path.resolve().as_uri()


def _mutate_crop(session: dict[str, Any], problem_id: str, raw_crop: Any) -> dict[str, Any]:
    _index, problem = _find_problem(session, problem_id)
    crop_payload = raw_crop if isinstance(raw_crop, dict) else {}
    page_source_path = _source_page_path_for_problem(session, problem)
    if "cropBaseBbox" not in problem:
        base_bbox = _bbox_from_problem(problem)
        crop_base_image_path = problem.get("imagePath")
        if (
            not crop_base_image_path
            and page_source_path is not None
            and page_source_path.exists()
        ):
            crop_base_image_path = _materialize_crop_base_image(
                session,
                problem_id,
                page_source_path,
                base_bbox,
            )
        problem["cropBaseBbox"] = {
            "left": base_bbox.left,
            "top": base_bbox.top,
            "width": base_bbox.width,
            "height": base_bbox.height,
        }
        problem["cropBaseImagePath"] = crop_base_image_path
        problem["cropBaseBoardRenderPath"] = problem.get("boardRenderPath") or crop_base_image_path
        problem["cropBasePreserveMediaRegions"] = list(
            problem.get("preserveMediaRegions")
            or problem.get("preserve_media_regions")
            or []
        )
    else:
        base_bbox = _bbox_from_problem(problem, prefer_crop_base=True)
        if (
            not _resolve_session_path(problem.get("cropBaseImagePath") or problem.get("imagePath"))
            and page_source_path is not None
            and page_source_path.exists()
        ):
            crop_base_image_path = _materialize_crop_base_image(
                session,
                problem_id,
                page_source_path,
                base_bbox,
            )
            problem["cropBaseImagePath"] = crop_base_image_path
            problem["cropBaseBoardRenderPath"] = problem.get("cropBaseBoardRenderPath") or crop_base_image_path

    crop_box_payload = crop_payload.get("cropBox") or crop_payload.get("crop_box")
    next_bbox: Box
    crop: dict[str, float]
    if crop_box_payload is not None:
        if page_source_path is None or not page_source_path.exists():
            raise FileNotFoundError(f"source page image missing for manual crop: {problem_id}")
        from PIL import Image

        with Image.open(page_source_path) as page_image:
            next_bbox = _coerce_crop_box(
                crop_box_payload,
                image_width=page_image.width,
                image_height=page_image.height,
            )
        crop = _manual_crop_from_bbox(base_bbox, next_bbox)
        # Keep the stored ratios and the rendered bbox consistent: if the
        # ratio clamp altered the request, re-derive the bbox from the
        # clamped ratios so a later re-crop cannot jump to a different region
        # than what this crop actually rendered.
        next_bbox = _bbox_with_manual_crop(base_bbox, crop)
    else:
        crop = _coerce_manual_crop_ratios(crop_payload)
        next_bbox = _bbox_with_manual_crop(base_bbox, crop)

    base_image_path = _resolve_session_path(problem.get("cropBaseImagePath") or problem.get("imagePath"))
    base_board_path = _resolve_session_path(
        problem.get("cropBaseBoardRenderPath")
        or problem.get("boardRenderPath")
        or problem.get("cropBaseImagePath")
        or problem.get("imagePath")
    )
    if base_image_path is None or not base_image_path.exists():
        raise FileNotFoundError(f"problem image missing for {problem_id}: {base_image_path}")
    if base_board_path is None or not base_board_path.exists():
        base_board_path = base_image_path

    crop_dir = _crop_dir_for_session(session)
    raw_crop_path_for_board: Path | None = None
    if _manual_crop_is_empty(crop):
        raw_uri = base_image_path.resolve().as_uri()
        board_uri = base_board_path.resolve().as_uri()
        raw_crop_path_for_board = base_image_path
    elif page_source_path is not None and page_source_path.exists():
        raw_crop_path = crop_dir / _make_crop_filename(problem_id, "manual_crop")
        _crop_image_by_bbox(page_source_path, next_bbox, raw_crop_path)
        raw_uri = raw_crop_path.resolve().as_uri()
        raw_crop_path_for_board = raw_crop_path
        board_uri = raw_uri
    else:
        if any(value < -0.0001 for value in crop.values()):
            raise FileNotFoundError(f"source page image missing for expanded manual crop: {problem_id}")
        raw_crop_path = crop_dir / _make_crop_filename(problem_id, "manual_crop")
        _crop_image_by_ratios(base_image_path, crop, raw_crop_path)
        raw_uri = raw_crop_path.resolve().as_uri()
        raw_crop_path_for_board = raw_crop_path
        if base_board_path == base_image_path:
            board_uri = raw_uri
        else:
            board_crop_path = crop_dir / _make_crop_filename(problem_id, "manual_crop_board")
            _crop_image_by_ratios(base_board_path, crop, board_crop_path)
            board_uri = board_crop_path.resolve().as_uri()

    if (
        raw_crop_path_for_board is not None
        and raw_crop_path_for_board.exists()
        and _crop_refreshes_board_render(problem)
    ):
        board_crop_path = crop_dir / _make_crop_filename(problem_id, "manual_crop_board")
        _render_board_crop_from_raw(raw_crop_path_for_board, board_crop_path, problem)
        board_uri = board_crop_path.resolve().as_uri()

    problem["imagePath"] = raw_uri
    problem["boardRenderPath"] = board_uri
    problem["bbox"] = {
        "left": next_bbox.left,
        "top": next_bbox.top,
        "width": next_bbox.width,
        "height": next_bbox.height,
    }
    problem["manualCrop"] = crop
    problem["manual_crop"] = crop
    # A manual crop changes the local coordinate system. Until those regions
    # are re-detected, stale PDF media boxes must not be pasted elsewhere.
    preserved_regions = (
        list(problem.get("cropBasePreserveMediaRegions") or [])
        if _manual_crop_is_empty(crop)
        else []
    )
    problem["preserveMediaRegions"] = preserved_regions
    problem["preserve_media_regions"] = list(preserved_regions)
    if raw_crop_path_for_board is not None and raw_crop_path_for_board.exists():
        _refresh_mutated_crop_layout(problem, raw_crop_path_for_board)
    _refresh_session_problem_counts(session)
    return session


def _mutate_stitch_crop(
    session: dict[str, Any],
    problem_id: str,
    segments: Any,
) -> dict[str, Any]:
    """Replace one problem with an ordered stitch of page-level crop boxes."""
    _index, problem = _find_problem(session, problem_id)
    raw_segments = segments if isinstance(segments, list) else []
    if not raw_segments:
        raise ValueError("segments is required")
    if len(raw_segments) > 32:
        raise ValueError("segments must contain at most 32 regions")

    page_cache: dict[str, tuple[dict[str, Any], Path, int, int, float | None]] = {}
    normalized: list[tuple[str, Box, int, int]] = []
    from PIL import Image

    for index, raw_segment in enumerate(raw_segments, start=1):
        segment = raw_segment if isinstance(raw_segment, dict) else {}
        page_id = str(
            segment.get("pageId")
            or segment.get("page_id")
            or segment.get("sourcePageId")
            or segment.get("source_page_id")
            or ""
        ).strip()
        if not page_id:
            raise ValueError(f"segments[{index - 1}].pageId is required")
        if page_id not in page_cache:
            page = _find_page(session, page_id)
            source_path = _resolve_session_path(
                page.get("sourceImagePath") or page.get("sourceImageUri")
            )
            if source_path is None or not source_path.exists():
                raise FileNotFoundError(f"page image missing for {page_id}: {source_path}")
            with Image.open(source_path) as page_image:
                image_width, image_height = page_image.size
                column_divider_x = _lazy_call(
                    "segment",
                    "detect_pdf_visual_column_divider_x",
                    page_image.convert("RGB"),
                )
                try:
                    column_divider_x = float(column_divider_x)
                except (TypeError, ValueError):
                    column_divider_x = None
                if (
                    column_divider_x is not None
                    and (
                        not math.isfinite(column_divider_x)
                        or column_divider_x < image_width * MANUAL_PASSAGE_CENTER_DIVIDER_MIN_RATIO
                        or column_divider_x > image_width * MANUAL_PASSAGE_CENTER_DIVIDER_MAX_RATIO
                    )
                ):
                    # Side rules and answer-box borders can resemble a column
                    # divider. Only a line in the central page band is allowed
                    # to constrain a manual passage crop.
                    column_divider_x = None
            page_cache[page_id] = (
                page,
                source_path,
                image_width,
                image_height,
                column_divider_x,
            )
        _page, _source_path, image_width, image_height, column_divider_x = page_cache[page_id]
        raw_box = (
            segment.get("bbox")
            or segment.get("cropBox")
            or segment.get("crop_box")
            or segment
        )
        box = _coerce_crop_box(raw_box, image_width=image_width, image_height=image_height)
        raw_column_index = segment.get("columnIndex", segment.get("column_index"))
        try:
            column_index = int(raw_column_index) if raw_column_index is not None else 0
        except (TypeError, ValueError):
            column_index = 0
        if column_divider_x is not None:
            # Legacy UI sessions sometimes defaulted every fragment to the
            # left column. Treat a hint as stale when the whole box is already
            # on the opposite side; a box crossing the divider still honors
            # the explicit column selected by the user.
            if column_index == 1 and box.left >= column_divider_x:
                column_index = 2
            elif column_index == 2 and box.right <= column_divider_x:
                column_index = 1
            if column_index not in {1, 2}:
                column_index = 1 if (box.left + box.right) * 0.5 < column_divider_x else 2
            if column_index == 1:
                safe_right = max(
                    1.0,
                    float(column_divider_x) - MANUAL_PASSAGE_CENTER_DIVIDER_EXCLUSION_PX,
                )
                right = min(float(box.right), safe_right)
                left = min(float(box.left), right - 1.0)
            else:
                safe_left = min(
                    float(image_width) - 1.0,
                    float(column_divider_x) + MANUAL_PASSAGE_CENTER_DIVIDER_EXCLUSION_PX,
                )
                left = max(float(box.left), safe_left)
                right = max(left + 1.0, float(box.right))
            box = Box.from_points(left, box.top, right, box.bottom)
        elif column_index < 1:
            column_index = 1
        if box.width < 8 or box.height < 8:
            raise ValueError(
                f"segments[{index - 1}] is too small after page-boundary cleanup (minimum 8x8 px)"
            )
        raw_order = segment.get("order", segment.get("fragmentIndex", segment.get("fragment_index", index)))
        try:
            order = int(raw_order)
        except (TypeError, ValueError):
            raise ValueError(f"segments[{index - 1}].order must be an integer") from None
        if order < 1:
            raise ValueError(f"segments[{index - 1}].order must be positive")
        normalized.append((page_id, box, order, column_index))

    orders = [order for _page_id, _box, order, _column_index in normalized]
    if len(set(orders)) != len(orders):
        raise ValueError("segments must have unique order values")

    for left_index, (left_page_id, left_box, _left_order, _left_column) in enumerate(normalized):
        for right_page_id, right_box, _right_order, _right_column in normalized[left_index + 1:]:
            if left_page_id != right_page_id:
                continue
            intersection_width = max(0.0, min(left_box.right, right_box.right) - max(left_box.left, right_box.left))
            intersection_height = max(0.0, min(left_box.bottom, right_box.bottom) - max(left_box.top, right_box.top))
            intersection_area = intersection_width * intersection_height
            union_area = left_box.area + right_box.area - intersection_area
            overlap = intersection_area / union_area if union_area > 0 else 0.0
            if overlap >= 0.98:
                raise ValueError("segments contain duplicate regions on the same page")

    normalized.sort(key=lambda item: item[2])
    crop_dir = _crop_dir_for_session(session)
    stitched_path = crop_dir / _make_crop_filename(problem_id, "manual_stitch")
    with tempfile.TemporaryDirectory(prefix=".manual-stitch-", dir=str(crop_dir)) as raw_tmp:
        temp_dir = Path(raw_tmp)
        fragment_paths: list[Path] = []
        for index, (page_id, box, _order, _column_index) in enumerate(normalized, start=1):
            source_path = page_cache[page_id][1]
            fragment_path = temp_dir / f"fragment-{index:03d}.png"
            _crop_image_by_bbox(source_path, box, fragment_path)
            with Image.open(fragment_path) as fragment_image:
                cleaned_fragment = _lazy_call(
                    "build_problem_board_edb",
                    "_erase_corner_page_badges",
                    fragment_image.copy(),
                )
                cleaned_fragment = _lazy_call(
                    "build_problem_board_edb",
                    "_trim_text_priority_bottom_page_badge",
                    cleaned_fragment,
                )
                cleaned_fragment.save(fragment_path)
            fragment_paths.append(fragment_path)
        source_crop_boxes: list[tuple[int, int, int, int]] = []
        stitch_diagnostics: dict[str, Any] = {}
        _stitch_passage_image_files(
            fragment_paths,
            stitched_path,
            transparent=False,
            source_crop_boxes_output=source_crop_boxes,
            stitch_diagnostics_output=stitch_diagnostics,
        )

    effective_normalized: list[tuple[str, Box, int, int]] = []
    trimmed_fragment_count = 0
    for index, (page_id, box, order, column_index) in enumerate(normalized):
        source_left = math.floor(box.left)
        source_top = math.floor(box.top)
        source_right = math.ceil(box.right)
        source_bottom = math.ceil(box.bottom)
        crop_width = max(1, source_right - source_left)
        crop_height = max(1, source_bottom - source_top)
        crop_box = (
            source_crop_boxes[index]
            if index < len(source_crop_boxes)
            else (0, 0, crop_width, crop_height)
        )
        crop_left, crop_top, crop_right, crop_bottom = crop_box
        crop_left = max(0, min(crop_width - 1, int(crop_left)))
        crop_top = max(0, min(crop_height - 1, int(crop_top)))
        crop_right = max(crop_left + 1, min(crop_width, int(crop_right)))
        crop_bottom = max(crop_top + 1, min(crop_height, int(crop_bottom)))
        if (crop_left, crop_top, crop_right, crop_bottom) != (0, 0, crop_width, crop_height):
            trimmed_fragment_count += 1
        effective_normalized.append((
            page_id,
            Box.from_points(
                source_left + crop_left,
                source_top + crop_top,
                source_left + crop_right,
                source_top + crop_bottom,
            ),
            order,
            column_index,
        ))

    board_uri = stitched_path.resolve().as_uri()
    if _crop_refreshes_board_render(problem):
        board_path = crop_dir / _make_crop_filename(problem_id, "manual_stitch_board")
        _render_board_crop_from_raw(stitched_path, board_path, problem)
        board_uri = board_path.resolve().as_uri()

    source_segments: list[dict[str, Any]] = []
    for fragment_index, (page_id, box, _order, column_index) in enumerate(effective_normalized, start=1):
        bbox = {
            "left": box.left,
            "top": box.top,
            "width": box.width,
            "height": box.height,
        }
        source_segments.append({
            "sourcePageId": page_id,
            "source_page_id": page_id,
            "fragmentIndex": fragment_index,
            "fragment_index": fragment_index,
            "columnIndex": column_index,
            "column_index": column_index,
            "bbox": bbox,
        })

    first_page_id, first_box, _first_order, _first_column = effective_normalized[0]
    first_source_path = page_cache[first_page_id][1]
    first_bbox = {
        "left": first_box.left,
        "top": first_box.top,
        "width": first_box.width,
        "height": first_box.height,
    }
    source_page_ids = list(dict.fromkeys(
        page_id for page_id, _box, _order, _column in effective_normalized
    ))

    problem["sourcePageId"] = first_page_id
    problem["source_page_id"] = first_page_id
    problem["sourceImagePath"] = first_source_path.resolve().as_uri()
    problem["bbox"] = first_bbox
    problem["sourceSegments"] = source_segments
    problem["source_segments"] = list(source_segments)
    problem["imagePath"] = stitched_path.resolve().as_uri()
    problem["boardRenderPath"] = board_uri
    problem["recordMode"] = "image-only"
    problem["textRecordCount"] = 0
    problem["imageRecordCount"] = 1
    problem["manualStitch"] = True
    problem["manual_stitch"] = True
    problem["passageSourcePageIds"] = source_page_ids
    problem["passage_source_page_ids"] = list(source_page_ids)
    problem["passageFragmentsMerged"] = len(source_segments) > 1
    problem["passage_fragments_merged"] = len(source_segments) > 1
    problem["passageFragmentCount"] = len(source_segments)
    problem["passage_fragment_count"] = len(source_segments)
    problem["passageMergedSourcePageIds"] = source_page_ids
    problem["passage_merged_source_page_ids"] = list(source_page_ids)
    problem["passageMergedFragmentCount"] = len(source_segments)
    problem["passage_merged_fragment_count"] = len(source_segments)
    stitch_diagnostics = {
        **stitch_diagnostics,
        "trimmed_source_fragment_count": trimmed_fragment_count,
        "trimmedSourceFragmentCount": trimmed_fragment_count,
    }
    problem["manualStitchDiagnostics"] = stitch_diagnostics
    problem["manual_stitch_diagnostics"] = dict(stitch_diagnostics)
    problem["manualCrop"] = {
        "leftRatio": 0.0,
        "rightRatio": 0.0,
        "topRatio": 0.0,
        "bottomRatio": 0.0,
    }
    problem["manual_crop"] = dict(problem["manualCrop"])
    problem["preserveMediaRegions"] = []
    problem["preserve_media_regions"] = []
    problem["riskFlags"] = []
    problem["risk_flags"] = []
    problem["reviewStatus"] = "normal"
    problem["review_status"] = "normal"
    for key in (
        "cropBaseBbox",
        "cropBaseImagePath",
        "cropBaseBoardRenderPath",
        "cropBasePreserveMediaRegions",
    ):
        problem.pop(key, None)

    metadata = problem.get("metadata")
    if isinstance(metadata, dict):
        metadata["passage_source_page_ids"] = list(source_page_ids)
        metadata["passage_fragments_merged"] = len(source_segments) > 1
        metadata["passage_fragment_count"] = len(source_segments)
        metadata["passage_merged_source_page_ids"] = list(source_page_ids)
        metadata["passage_merged_fragment_count"] = len(source_segments)
        metadata["manual_stitch"] = True
        metadata["manual_stitch_diagnostics"] = dict(stitch_diagnostics)

    previous_positions: dict[str, int] = {}
    for page in session.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        ids = [str(raw_id) for raw_id in (page.get("problemIds") or page.get("problem_ids") or []) if raw_id]
        if problem_id in ids:
            previous_positions[str(page.get("id") or "")] = ids.index(problem_id)
        next_ids = [current_id for current_id in ids if current_id != problem_id]
        page["problemIds"] = next_ids
        page["problem_ids"] = list(next_ids)
    for page_id in source_page_ids:
        page = page_cache[page_id][0]
        ids = [str(raw_id) for raw_id in (page.get("problemIds") or []) if raw_id]
        insertion_index = min(previous_positions.get(page_id, len(ids)), len(ids))
        ids.insert(insertion_index, problem_id)
        page["problemIds"] = ids
        page["problem_ids"] = list(ids)

    _refresh_mutated_crop_layout(problem, stitched_path)
    _refresh_session_problem_counts(session)
    return session


def _bulk_crop_parent(
    session: dict[str, Any],
    page: dict[str, Any],
    source_path: Path,
    image_width: int,
    image_height: int,
    replace_problem_ids: list[str],
) -> dict[str, Any]:
    candidate_ids = list(replace_problem_ids)
    if not candidate_ids:
        candidate_ids = [str(pid) for pid in (page.get("problemIds") or []) if pid]
    for problem_id in candidate_ids:
        try:
            _index, problem = _find_problem(session, problem_id)
        except ValueError:
            if replace_problem_ids:
                raise
            continue
        return problem
    return {
        "id": str(page.get("id") or "page"),
        "title": str(page.get("title") or page.get("sourceFileName") or source_path.stem or "문항"),
        "sourcePageId": str(page.get("id") or ""),
        "sourceFileName": source_path.name,
        "sourceImagePath": source_path.resolve().as_uri(),
        "recordMode": "image-only",
        "textRecordCount": 0,
        "imageRecordCount": 1,
        "bbox": {
            "left": 0.0,
            "top": 0.0,
            "width": float(image_width),
            "height": float(image_height),
        },
    }


def _replace_bulk_crop_problems(
    session: dict[str, Any],
    page: dict[str, Any],
    replacements: list[dict[str, Any]],
    replace_problem_ids: list[str],
) -> None:
    replacement_ids = [str(problem.get("id")) for problem in replacements if problem.get("id")]
    if not replace_problem_ids:
        session["problems"] = list(session.get("problems") or []) + replacements
        page_ids = [str(pid) for pid in (page.get("problemIds") or []) if pid]
        page["problemIds"] = [*page_ids, *replacement_ids]
        _refresh_session_problem_counts(session)
        return

    replace_set = set(replace_problem_ids)
    next_problems: list[dict[str, Any]] = []
    inserted = False
    for problem in session.get("problems", []) or []:
        if isinstance(problem, dict) and str(problem.get("id")) in replace_set:
            if not inserted:
                next_problems.extend(replacements)
                inserted = True
            continue
        next_problems.append(problem)
    if not inserted:
        next_problems.extend(replacements)
    session["problems"] = next_problems

    next_page_ids: list[str] = []
    inserted_on_page = False
    for raw_id in page.get("problemIds") or []:
        current_id = str(raw_id)
        if current_id in replace_set:
            if not inserted_on_page:
                next_page_ids.extend(replacement_ids)
                inserted_on_page = True
            continue
        next_page_ids.append(current_id)
    if not inserted_on_page:
        next_page_ids.extend(replacement_ids)
    page["problemIds"] = next_page_ids
    _refresh_session_problem_counts(session)


def _mutate_bulk_crop(
    session: dict[str, Any],
    page_id: str,
    regions: Any,
    replace_problem_ids: Any = None,
) -> dict[str, Any]:
    page_id = str(page_id or "").strip()
    if not page_id:
        raise ValueError("pageId is required")
    page = _find_page(session, page_id)
    source_path = _resolve_session_path(page.get("sourceImagePath") or page.get("sourceImageUri"))
    if source_path is None or not source_path.exists():
        raise FileNotFoundError(f"page image missing for {page_id}: {source_path}")
    raw_regions = regions if isinstance(regions, list) else []
    if not raw_regions:
        raise ValueError("regions is required")

    replace_ids = _coerce_problem_ids(replace_problem_ids)
    for problem_id in replace_ids:
        _index, problem = _find_problem(session, problem_id)
        problem_page_id = str(problem.get("sourcePageId") or "")
        if problem_page_id and problem_page_id != page_id:
            raise ValueError(f"problem {problem_id} does not belong to page {page_id}")

    from PIL import Image

    with Image.open(source_path) as page_image:
        image_width, image_height = page_image.size

    boxes: list[tuple[Box, str]] = []
    for index, raw_region in enumerate(raw_regions, start=1):
        region = raw_region if isinstance(raw_region, dict) else {}
        raw_box = (
            region.get("bbox")
            or region.get("cropBox")
            or region.get("crop_box")
            or region
        )
        box = _coerce_crop_box(raw_box, image_width=image_width, image_height=image_height)
        title = str(region.get("title") or "").strip() or f"문항 {index:02d}"
        boxes.append((box, title))

    parent = _bulk_crop_parent(session, page, source_path, image_width, image_height, replace_ids)
    base_id = replace_ids[0] if len(replace_ids) == 1 else str(parent.get("id") or page_id)
    replacement_source_id = replace_ids[0] if replace_ids else ""
    crop_dir = _crop_dir_for_session(session)
    source_uri = source_path.resolve().as_uri()
    candidate_session = {**session, "problems": list(session.get("problems") or [])}
    replacements: list[dict[str, Any]] = []
    for index, (box, title) in enumerate(boxes, start=1):
        new_id = _next_problem_id(candidate_session, base_id, f"crop-{index:02d}")
        candidate_session["problems"].append({"id": new_id})
        crop_path = crop_dir / _make_crop_filename(new_id, f"bulk_crop_{index:02d}")
        _crop_image_by_bbox(source_path, box, crop_path)

        entry = _problem_skeleton_from_parent(parent)
        entry["id"] = new_id
        entry["title"] = title
        entry["sourcePageId"] = page_id
        entry["sourceImagePath"] = source_uri
        entry["sourceFileName"] = str(parent.get("sourceFileName") or source_path.name)
        if replacement_source_id:
            entry["replacesProblemId"] = replacement_source_id
            entry["replaces_problem_id"] = replacement_source_id
        entry["bbox"] = {
            "left": box.left,
            "top": box.top,
            "width": box.width,
            "height": box.height,
        }
        entry["imagePath"] = crop_path.resolve().as_uri()
        entry["boardRenderPath"] = crop_path.resolve().as_uri()
        entry["recordMode"] = "image-only"
        entry["textRecordCount"] = 0
        entry["imageRecordCount"] = 1
        entry["riskFlags"] = []
        entry["reviewStatus"] = "normal"
        _refresh_mutated_crop_layout(entry, crop_path)
        replacements.append(entry)

    _replace_bulk_crop_problems(session, page, replacements, replace_ids)
    return session


def _session_problem_image_export_items(
    session: dict[str, Any],
    problem_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    problems = [problem for problem in (session.get("problems") or []) if isinstance(problem, dict)]
    if problem_ids is None:
        ordered = problems
    else:
        by_id = {str(problem.get("id")): problem for problem in problems if problem.get("id")}
        ordered = []
        for problem_id in problem_ids:
            if problem_id not in by_id:
                raise ValueError(f"problem not found: {problem_id}")
            ordered.append(by_id[problem_id])
    return [problem for problem in ordered if not _session_problem_is_supplemental(problem)]


def _safe_export_filename(index: int, title: Any, problem_id: Any) -> str:
    def sanitize_part(value: Any) -> str:
        raw = str(value or "").strip()
        invalid = '<>:"/\\|?*'
        safe_chars = [ch if ch not in invalid and ord(ch) >= 32 else "_" for ch in raw]
        safe = re.sub(r"\s+", "_", "".join(safe_chars)).strip(" ._")
        return re.sub(r"_+", "_", safe)[:80].strip(" ._")

    safe = sanitize_part(title) or sanitize_part(problem_id) or "problem"
    if not safe:
        safe = "problem"
    return f"{index:03d}_{safe}.png"


def _safe_problem_image_download_filename(index: int, title: Any, problem_id: Any) -> str:
    filename = _safe_export_filename(index, title, problem_id)
    _, _, suffix = filename.partition("_")
    return f"{index:02d}_{suffix or 'problem.png'}"


def _existing_session_image_path(value: Any) -> Path | None:
    path = _resolve_session_path(value)
    if path is None:
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if resolved.exists() and resolved.is_file() else None


def _session_problem_processing_step(problem: dict[str, Any]) -> str:
    return _normalize_processing_step(
        problem.get("processingStep")
        or problem.get("processing_step")
        or problem.get("step")
    )


def _encode_png_payload(image: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _session_problem_s3_board_png_payload(problem: dict[str, Any], *, board_theme: str) -> bytes | None:
    if _session_problem_processing_step(problem) != "s3":
        return None
    source_path = (
        _existing_session_image_path(problem.get("imagePath"))
        or _existing_session_image_path(problem.get("boardRenderPath"))
    )
    if source_path is None:
        return None

    from PIL import Image

    with Image.open(source_path) as loaded:
        crop_image = loaded.convert("RGBA" if "A" in loaded.getbands() else "RGB")
    board_image = _build_transparent_reconstruction_image(
        crop_image,
        board_theme=board_theme or DEFAULT_BOARD_THEME,
        text_priority=_problem_prefers_text_preservation(
            problem.get("subject"),
            problem.get("sourceFileName"),
            problem.get("title"),
        ),
    )
    return _encode_png_payload(board_image)


def _session_problem_board_png_payload(problem: dict[str, Any], *, board_theme: str) -> bytes | None:
    step = _session_problem_processing_step(problem)
    if step == "s3":
        return _session_problem_s3_board_png_payload(problem, board_theme=board_theme)
    if step != "s2":
        return None
    crop_path = (
        _existing_session_image_path(problem.get("imagePath"))
        or _existing_session_image_path(problem.get("boardRenderPath"))
    )
    if crop_path is None:
        return None
    board_path = _existing_session_image_path(problem.get("boardRenderPath")) or crop_path

    from PIL import Image

    with Image.open(crop_path) as loaded:
        crop_image = loaded.convert("RGBA" if "A" in loaded.getbands() else "RGB")
    board_image = _load_board_export_image(
        board_path,
        crop_image,
        board_theme=board_theme or DEFAULT_BOARD_THEME,
    )
    return _encode_png_payload(board_image)


def _write_zip_png(zip_file: zipfile.ZipFile, source_path: Path, arcname: str) -> None:
    if source_path.suffix.lower() == ".png":
        zip_file.write(source_path, arcname)
        return

    from io import BytesIO
    from PIL import Image

    with Image.open(source_path) as image:
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            image = image.convert("RGBA")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
    zip_file.writestr(arcname, buffer.getvalue())


def _session_image_export_source_path(problem: dict[str, Any], kind: str) -> Path | None:
    image_path = _existing_session_image_path(problem.get("imagePath"))
    if kind == "raw":
        return image_path
    board_path = _existing_session_image_path(problem.get("boardRenderPath"))
    return board_path or image_path


def _session_problem_image_download_source_path(problem: dict[str, Any], variant: str) -> Path | None:
    normalized = str(variant or "board").strip().lower()
    if normalized == "raw":
        keys = ("imagePath", "sourceImagePath", "boardRenderPath")
    elif normalized == "source":
        keys = ("sourceImagePath", "imagePath", "boardRenderPath")
    else:
        keys = ("boardRenderPath", "imagePath", "sourceImagePath")
    for key in keys:
        path = _existing_session_image_path(problem.get(key))
        if path is not None:
            return path
    return None


def _encode_image_as_png_bytes(path: Path) -> bytes:
    from PIL import Image

    with Image.open(path) as image:
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            image = image.convert("RGBA")
        buffer = BytesIO()
        image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_session_image_export_zip(
    session: dict[str, Any],
    mode: str,
    problem_ids: list[str] | None = None,
) -> dict[str, Any]:
    normalized_mode = str(mode or "both").strip().lower()
    if normalized_mode not in {"edb", "raw", "both"}:
        raise ValueError("mode must be one of edb, raw, or both")

    items = _session_problem_image_export_items(session, problem_ids)
    if not items:
        raise ValueError("no problem images available to export")

    session_name = str(session.get("session_name") or session.get("sessionName") or "session")
    export_dir = RUNTIME_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = _unique_artifact_stamp()
    digest = hashlib.sha1(f"{session_name}|{stamp}|{time.time_ns()}".encode("utf-8")).hexdigest()[:8]
    zip_stem = sanitize_output_dir_name(
        session_name or "session",
        suffix=f"images_{stamp}_{digest}",
    )
    zip_path = (export_dir / f"{zip_stem}.zip").resolve()

    manifest: dict[str, Any] = {
        "sessionName": session_name,
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": normalized_mode,
        "count": 0,
        "items": [],
        "missing": [],
    }
    exported_problem_ids: set[str] = set()
    board_theme = str(session.get("board_theme") or session.get("boardTheme") or DEFAULT_BOARD_THEME)
    folders = []
    if normalized_mode in {"edb", "both"}:
        folders.append(("edb", "edb_images", "edbImage"))
    if normalized_mode in {"raw", "both"}:
        folders.append(("raw", "raw_crops", "rawCrop"))

    def populate_archive(archive) -> None:
        for index, problem in enumerate(items, start=1):
            problem_id = str(problem.get("id") or "")
            filename = _safe_export_filename(index, problem.get("title"), problem_id)
            manifest_item: dict[str, Any] = {
                "index": index,
                "problemId": problem_id,
                "title": str(problem.get("title") or ""),
                "sourcePageId": str(problem.get("sourcePageId") or ""),
                "bbox": problem.get("bbox") or None,
            }
            exported_any = False
            for kind, folder, manifest_key in folders:
                arcname = f"{folder}/{filename}"
                source_path: Path | None = None
                try:
                    rendered_payload = (
                        _session_problem_board_png_payload(problem, board_theme=board_theme)
                        if kind == "edb"
                        else None
                    )
                    if rendered_payload is not None:
                        archive.writestr(arcname, rendered_payload)
                    else:
                        source_path = _session_image_export_source_path(problem, kind)
                        if source_path is None:
                            manifest_item[manifest_key] = None
                            missing = {
                                "index": index,
                                "problemId": problem_id,
                                "title": manifest_item["title"],
                                "kind": kind,
                                "reason": "image file missing",
                            }
                            manifest["missing"].append(missing)
                            continue
                        _write_zip_png(archive, source_path, arcname)
                except Exception as exc:  # noqa: BLE001 - one failed render should not block the whole bundle.
                    manifest_item[manifest_key] = None
                    missing = {
                        "index": index,
                        "problemId": problem_id,
                        "title": manifest_item["title"],
                        "kind": kind,
                        "reason": str(exc),
                    }
                    if source_path is not None:
                        missing["sourcePath"] = str(source_path)
                    manifest["missing"].append(missing)
                    continue
                manifest_item[manifest_key] = arcname
                if kind == "edb":
                    manifest_item["processingStep"] = _session_problem_processing_step(problem)
                exported_any = True
            if exported_any:
                exported_problem_ids.add(problem_id)
            manifest["items"].append(manifest_item)

        manifest["count"] = len(exported_problem_ids)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    _write_zip_atomically(
        zip_path,
        compression=zipfile.ZIP_DEFLATED,
        populate=populate_archive,
    )

    return {
        "zipPath": str(zip_path),
        "fileName": zip_path.name,
        "count": manifest["count"],
        "missing": manifest["missing"],
        "manifest": manifest,
        "mode": normalized_mode,
    }


def _write_edb_export_zip(edb_paths: list[Path], bundle_name: str | None = None) -> dict[str, Any]:
    resolved_paths = [Path(path).resolve() for path in edb_paths]
    if len(resolved_paths) < 2:
        raise ValueError("at least two EDB files are required for a bundle")
    if any(path.suffix.lower() != ".edb" or not path.is_file() for path in resolved_paths):
        raise ValueError("EDB bundle contains a missing or invalid file")

    requested_name = sanitize_edb_file_name(bundle_name, fallback_stem=resolved_paths[0].stem)
    export_dir = RUNTIME_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    digest_source = "|".join(str(path) for path in resolved_paths)
    digest = hashlib.sha1(f"{digest_source}|{time.time_ns()}".encode("utf-8")).hexdigest()[:8]
    zip_stem = sanitize_output_dir_name(
        Path(requested_name).stem or "classin",
        suffix=f"files_{stamp}_{digest}",
    )
    zip_path = (export_dir / f"{zip_stem}.zip").resolve()

    used_names: set[str] = set()

    def populate_archive(archive) -> None:
        for index, path in enumerate(resolved_paths, start=1):
            arcname = path.name
            collision_index = index
            while arcname.casefold() in used_names:
                arcname = f"{Path(path.name).stem}_part{collision_index:02d}.edb"
                collision_index += 1
            used_names.add(arcname.casefold())
            archive.write(path, arcname)

    _write_zip_atomically(
        zip_path,
        compression=zipfile.ZIP_STORED,
        populate=populate_archive,
    )

    return {
        "zipPath": str(zip_path),
        "fileName": zip_path.name,
        "count": len(resolved_paths),
    }


def _mutate_exclude_many(session: dict[str, Any], problem_ids: Any) -> dict[str, Any]:
    ids = _coerce_problem_ids(problem_ids)
    if not ids:
        raise ValueError("problemIds is required")
    for problem_id in ids:
        _find_problem(session, problem_id)  # raises if missing
    _remove_problems(session, set(ids))
    return session


def _mutate_exclude(session: dict[str, Any], problem_id: str) -> dict[str, Any]:
    return _mutate_exclude_many(session, [problem_id])


def _mutate_confirm(session: dict[str, Any], problem_ids: Any) -> dict[str, Any]:
    ids = set(_coerce_problem_ids(problem_ids))
    if not ids:
        raise ValueError("problemIds is required")
    problems = session.get("problems")
    if not isinstance(problems, list):
        raise ValueError("session problems are missing")
    available_ids = {
        str(problem.get("id"))
        for problem in problems
        if isinstance(problem, dict) and problem.get("id")
    }
    missing = sorted(ids - available_ids)
    if missing:
        raise ValueError(f"problem not found: {', '.join(missing)}")

    for problem in problems:
        if not isinstance(problem, dict) or str(problem.get("id") or "") not in ids:
            continue
        problem["riskFlags"] = []
        problem["risk_flags"] = []
        problem["reviewStatus"] = "normal"
        problem["review_status"] = "normal"
        problem["parseFailed"] = False
        problem["parse_failed"] = False

    normalized_by_id = {
        str(problem.get("id")): problem
        for problem in problems
        if isinstance(problem, dict) and problem.get("id")
    }
    for page in session.get("pages") or []:
        if not isinstance(page, dict):
            continue
        page_problem_ids = page.get("problemIds")
        if not isinstance(page_problem_ids, list):
            page_problem_ids = page.get("problem_ids")
        page_problems = [
            normalized_by_id[str(problem_id)]
            for problem_id in (page_problem_ids or [])
            if str(problem_id) in normalized_by_id
        ]
        if page_problems and all(_problem_review_status(problem) == "normal" for problem in page_problems):
            page["riskFlags"] = []
            page["risk_flags"] = []
            page["reviewStatus"] = "normal"
            page["review_status"] = "normal"
    return session


def _mutate_confirm_pages(
    session: dict[str, Any],
    page_ids: Any,
    *,
    decision: str = "no_passage",
) -> dict[str, Any]:
    """Resolve review-only pages that intentionally contain no extracted item.

    A page without problem ids cannot be sent through ``_mutate_confirm``.  Keep
    an explicit decision on the page so the review target remains part of the
    stable progress denominator after it is resolved.
    """
    ids = set(_coerce_problem_ids(page_ids))
    if not ids:
        raise ValueError("pageIds is required")
    normalized_decision = str(decision or "no_passage").strip().lower().replace("-", "_")
    if normalized_decision not in {"no_passage"}:
        raise ValueError(f"unsupported page review decision: {decision}")

    pages = session.get("pages")
    if not isinstance(pages, list):
        raise ValueError("session pages are missing")
    available_pages = {
        str(page.get("id")): page
        for page in pages
        if isinstance(page, dict) and page.get("id")
    }
    missing = sorted(ids - set(available_pages))
    if missing:
        raise ValueError(f"page not found: {', '.join(missing)}")

    for page_id in ids:
        page = available_pages[page_id]
        problem_ids = page.get("problemIds")
        if not isinstance(problem_ids, list):
            problem_ids = page.get("problem_ids")
        if any(problem_ids or []):
            raise ValueError(f"page still has review items: {page_id}")
        page["riskFlags"] = []
        page["risk_flags"] = []
        page["reviewStatus"] = "normal"
        page["review_status"] = "normal"
        page["pageReviewConfirmed"] = True
        page["page_review_confirmed"] = True
        page["pageReviewDecision"] = normalized_decision
        page["page_review_decision"] = normalized_decision
    return session


def _problem_review_status(problem: dict[str, Any]) -> str:
    explicit = str(problem.get("reviewStatus") or problem.get("review_status") or "").strip().lower()
    if explicit in {"normal", "check_needed", "failed"}:
        return explicit
    bbox = problem.get("bbox") or {}
    try:
        has_bbox = (
            isinstance(bbox, dict)
            and float(bbox.get("width") or 0) > 0
            and float(bbox.get("height") or 0) > 0
        )
    except (TypeError, ValueError):
        has_bbox = False
    if not has_bbox or problem.get("parseFailed") or problem.get("parse_failed"):
        return "failed"
    if problem.get("riskFlags") or problem.get("risk_flags"):
        return "check_needed"
    return "normal"


def _page_needs_ai_retry(session: dict[str, Any], page: dict[str, Any]) -> bool:
    page_flags = page.get("riskFlags") or page.get("risk_flags") or []
    if isinstance(page_flags, list) and page_flags:
        return True
    problem_ids = [str(pid) for pid in (page.get("problemIds") or []) if pid]
    if not problem_ids:
        return True
    by_id = {
        str(problem.get("id")): problem
        for problem in session.get("problems", [])
        if isinstance(problem, dict) and problem.get("id")
    }
    return any(_problem_review_status(by_id.get(pid, {})) != "normal" for pid in problem_ids)


def _retry_target_page_ids(session: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    raw_page_ids = payload.get("pageIds") or payload.get("page_ids") or []
    if payload.get("pageId") or payload.get("page_id"):
        raw_page_ids = [payload.get("pageId") or payload.get("page_id")]

    ids: list[str] = [str(pid) for pid in raw_page_ids if pid]
    raw_problem_ids = payload.get("problemIds") or payload.get("problem_ids") or []
    if payload.get("problemId") or payload.get("problem_id"):
        raw_problem_ids = [payload.get("problemId") or payload.get("problem_id")]
    for problem_id in raw_problem_ids:
        _, problem = _find_problem(session, str(problem_id))
        page_id = str(problem.get("sourcePageId") or "")
        if page_id:
            ids.append(page_id)

    if not ids:
        ids.extend(
            str(page.get("id"))
            for page in session.get("pages", [])
            if isinstance(page, dict) and page.get("id") and _page_needs_ai_retry(session, page)
        )

    return list(dict.fromkeys(ids))


def _enhance_target_problem_ids(session: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    raw_problem_ids = payload.get("problemIds") or payload.get("problem_ids") or []
    if payload.get("problemId") or payload.get("problem_id"):
        raw_problem_ids = [payload.get("problemId") or payload.get("problem_id")]
    ids = [str(pid) for pid in raw_problem_ids if pid]

    raw_page_ids = payload.get("pageIds") or payload.get("page_ids") or []
    if payload.get("pageId") or payload.get("page_id"):
        raw_page_ids = [payload.get("pageId") or payload.get("page_id")]
    page_ids = {str(pid) for pid in raw_page_ids if pid}
    if page_ids:
        for problem in session.get("problems", []):
            if not isinstance(problem, dict):
                continue
            if str(problem.get("sourcePageId") or "") in page_ids and problem.get("id"):
                ids.append(str(problem["id"]))

    if not ids:
        for problem in session.get("problems", []):
            if not isinstance(problem, dict) or not problem.get("id"):
                continue
            if _problem_review_status(problem) != "normal":
                ids.append(str(problem["id"]))

    return list(dict.fromkeys(ids))


def _replace_page_problems(session: dict[str, Any], page_id: str, replacements: list[dict[str, Any]]) -> None:
    page = _find_page(session, page_id)
    old_ids = {str(pid) for pid in (page.get("problemIds") or []) if pid}
    if not old_ids:
        old_ids = {
            str(problem.get("id"))
            for problem in session.get("problems", [])
            if isinstance(problem, dict) and str(problem.get("sourcePageId") or "") == page_id
        }

    inserted = False
    next_problems: list[dict[str, Any]] = []
    for problem in session.get("problems", []) or []:
        if isinstance(problem, dict) and str(problem.get("id")) in old_ids:
            if not inserted:
                next_problems.extend(replacements)
                inserted = True
            continue
        next_problems.append(problem)

    if not inserted:
        next_problems.extend(replacements)

    session["problems"] = next_problems
    page["problemIds"] = [str(problem.get("id")) for problem in replacements if problem.get("id")]
    _refresh_session_problem_counts(session)


def _replace_single_problem(session: dict[str, Any], old_problem_id: str, replacements: list[dict[str, Any]]) -> None:
    if not replacements:
        return
    _index, old_problem = _find_problem(session, old_problem_id)
    page = _page_for_problem(session, old_problem)
    replacement_ids = [str(problem.get("id")) for problem in replacements if problem.get("id")]
    next_problems: list[dict[str, Any]] = []
    inserted = False
    for problem in session.get("problems", []) or []:
        if isinstance(problem, dict) and str(problem.get("id")) == old_problem_id:
            next_problems.extend(replacements)
            inserted = True
            continue
        next_problems.append(problem)
    if not inserted:
        next_problems.extend(replacements)
    session["problems"] = next_problems
    if page is not None:
        next_ids: list[str] = []
        replaced = False
        for raw_id in page.get("problemIds") or []:
            current_id = str(raw_id)
            if current_id == old_problem_id:
                next_ids.extend(replacement_ids)
                replaced = True
            else:
                next_ids.append(current_id)
        if not replaced:
            next_ids.extend(replacement_ids)
        page["problemIds"] = next_ids
    _refresh_session_problem_counts(session)


def _image_reconstruction_dir(session: dict[str, Any]) -> Path:
    if session.get("output_dir"):
        target = Path(str(session["output_dir"])).resolve() / "ai_image_reconstructions"
    else:
        target = RUNTIME_DIR / "ai_image_reconstructions"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _payload_bool(payload: dict[str, Any], camel_key: str, snake_key: str, default: bool) -> bool:
    raw = payload.get(camel_key)
    if raw is None:
        raw = payload.get(snake_key, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


_AI_IMAGE_REVIEW_FLAGS = {
    "ai_image_reconstructed_check_text",
    "ai_image_formula_loss_suspected",
    "ai_image_content_mismatch_suspected",
    "ai_image_reconstruction_failed",
}

_IMAGE_ENHANCE_MODES = {"auto", "preserve", "ai"}
_TEXT_PRESERVATION_SUBJECTS = {
    "korean",
    "english",
    "math",
    "science",
    "physics",
    "chemistry",
    "biology",
    "earth_science",
}


def _normalize_image_enhance_mode(value: Any) -> str:
    normalized = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "safe": "preserve",
        "local": "preserve",
        "text": "preserve",
        "text_safe": "preserve",
        "content_safe": "preserve",
        "reconstruct": "ai",
        "generative": "ai",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in _IMAGE_ENHANCE_MODES:
        raise ValueError("image enhance mode must be auto, preserve, or ai")
    return normalized


def _subject_hint_from_text(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "국어" in text or "korean" in text:
        return "korean"
    if "영어" in text or "english" in text:
        return "english"
    if "수학" in text or "math" in text:
        return "math"
    if "과학" in text or "science" in text:
        return "science"
    return None


def _problem_image_subject(session: dict[str, Any], problem: dict[str, Any]) -> str:
    explicit = str(problem.get("subject") or "").strip().lower()
    if explicit and explicit not in {"unknown", "none", "auto"}:
        return explicit
    candidates = [
        problem.get("sourceFileName"),
        problem.get("source_file_name"),
        problem.get("title"),
        session.get("session_name"),
        session.get("input_notes"),
        *(session.get("input_files") or []),
    ]
    for candidate in candidates:
        hint = _subject_hint_from_text(candidate)
        if hint:
            return hint
    return explicit or "unknown"


def _resolved_image_enhance_mode(requested_mode: str, subject: str) -> str:
    if requested_mode != "auto":
        return requested_mode
    # Auto mode must be both economical and semantically safe. Generative
    # reconstruction remains an explicit opt-in because pixel similarity
    # cannot prove that formulae, labels, or answer choices were preserved.
    return "preserve"


def _original_problem_image_path(
    problem: dict[str, Any],
    session: dict[str, Any] | None = None,
) -> Path | None:
    # Always restart from the first-generation crop. Repeatedly upscaling an
    # already enhanced PNG compounds halos and deformed glyph strokes.
    for key in ("originalImagePath", "original_image_path"):
        path = _resolve_session_path(problem.get(key))
        if path is not None and path.exists():
            return path

    # Sessions created before originalImagePath was persisted can still recover
    # a full-page source losslessly. Do this only for page-as-is records: a
    # segmented problem must never be replaced with its whole source page.
    input_intent = str(problem.get("inputIntent") or problem.get("input_intent") or "").strip().lower()
    force_full_page = bool(problem.get("forceFullPageBounds") or problem.get("force_full_page_bounds"))
    if session is not None and (input_intent == "page-as-is" or force_full_page):
        source_page_id = str(problem.get("sourcePageId") or problem.get("source_page_id") or "")
        for page in session.get("pages") or []:
            if not isinstance(page, dict):
                continue
            page_id = str(page.get("id") or page.get("pageId") or page.get("page_id") or "")
            if source_page_id and page_id != source_page_id:
                continue
            page_path = _resolve_session_path(page.get("sourceImagePath") or page.get("sourceImageUri"))
            if page_path is not None and page_path.exists():
                return page_path

    for key in ("imagePath", "boardRenderPath"):
        path = _resolve_session_path(problem.get(key))
        if path is not None and path.exists():
            return path
    return None


def _semantic_text_preservation_gate(
    *,
    subject: str,
    resolved_mode: str,
    delivery_mode: str,
) -> dict[str, Any]:
    """Describe whether character identity was actually verified.

    The topology mask catches missing rows and large layout changes, but it
    cannot prove that a generative model kept every Korean/English glyph. Until
    source/output OCR or PDF text-layer comparison is available, generated
    text-priority pages must remain reviewable instead of becoming a false pass.
    """
    if resolved_mode != "ai" or delivery_mode in {"content_safe_primary", "content_safe_fallback"}:
        return {
            "status": "pass",
            "review_required": False,
            "method": "pixel_preserving_resize",
        }
    if subject not in _TEXT_PRESERVATION_SUBJECTS:
        return {
            "status": "unverified",
            "review_required": True,
            "method": "semantic_visual_comparison_unavailable",
            "reason": "generated_diagram_or_label_identity_not_verified",
        }
    return {
        "status": "unverified",
        "review_required": True,
        "method": "semantic_ocr_comparison_unavailable",
        "reason": "generated_text_character_identity_not_verified",
    }


def _result_content_preservation(result: Any) -> dict[str, Any]:
    postprocess = result.postprocess if isinstance(getattr(result, "postprocess", None), dict) else {}
    preservation = postprocess.get("content_preservation")
    return preservation if isinstance(preservation, dict) else {}


def _result_passes_content_gate(result: Any) -> bool:
    preservation = _result_content_preservation(result)
    if (
        str(getattr(result, "provider", "") or "") == "local"
        and str(getattr(result, "model", "") or "") == "content-safe-lanczos"
    ):
        return not preservation.get("review_required")
    return preservation.get("status") == "pass" and not preservation.get("review_required")


def _content_recovery_prompt(prompt: str, preservation: dict[str, Any]) -> str:
    reasons = ", ".join(str(value) for value in (preservation.get("reasons") or []) if value)
    return (
        f"{prompt.strip()}\n\n"
        "INTERNAL QUALITY RETRY: The prior candidate failed source-content preservation checks"
        f"{f' ({reasons})' if reasons else ''}. Rebuild only from the supplied original source. "
        "Copy every equation row and every glyph one by one before improving visual quality. "
        "Do not omit, reinterpret, simplify, reflow, or replace any formula, fraction, exponent, "
        "subscript, operator, option label, unit, graph label, or Korean character."
    )


def _image_attempt_summary(result: Any, *, attempt: int, mode: str) -> dict[str, Any]:
    preservation = _result_content_preservation(result)
    return {
        "attempt": attempt,
        "mode": mode,
        "status": "pass" if _result_passes_content_gate(result) else "rejected",
        "provider": str(getattr(result, "provider", "") or ""),
        "model": str(getattr(result, "model", "") or ""),
        "latencyMs": int(getattr(result, "latency_ms", 0) or 0),
        "contentPreservation": preservation,
    }


def _image_generation_output_path(output_dir: Path, problem_id: str, suffix: str) -> Path:
    problem_token = problem_id or "problem"
    problem_digest = hashlib.sha1(problem_token.encode("utf-8", errors="surrogatepass")).hexdigest()[:10]
    stem = sanitize_output_dir_name(
        problem_token,
        suffix=f"{problem_digest}_{suffix}",
    )
    return output_dir / f"{stem}.png"


def _content_safe_primary_batch(
    session: dict[str, Any],
    problem_ids: list[str],
    enhance_plan: dict[str, tuple[str, str]],
    *,
    output_dir: Path,
    stamp: str,
    transparent_background: bool,
    sharpen: bool,
) -> dict[str, dict[str, Any]]:
    jobs: list[tuple[str, Path, Path]] = []
    for problem_id in problem_ids:
        _subject, resolved_mode = enhance_plan[problem_id]
        if resolved_mode != "preserve":
            continue
        _index, problem = _find_problem(session, problem_id)
        source_path = _original_problem_image_path(problem, session)
        if source_path is None or not source_path.exists():
            continue
        output_path = _image_generation_output_path(
            output_dir,
            problem_id,
            f"{stamp}_text-preserve-2x",
        )
        jobs.append((problem_id, source_path, output_path))

    if not jobs:
        return {}
    raw_workers = os.environ.get("EDB_IMAGE_ENHANCE_WORKERS", "").strip()
    try:
        requested_workers = int(raw_workers) if raw_workers else 2
    except ValueError:
        requested_workers = 2
    worker_count = max(1, min(len(jobs), 4, requested_workers))

    def run(job: tuple[str, Path, Path]) -> tuple[str, dict[str, Any]]:
        problem_id, source_path, output_path = job
        try:
            result = build_content_safe_upscale(
                source_path,
                output_path,
                transparent_background=transparent_background,
                sharpen=sharpen,
            )
            return problem_id, {
                "source_path": source_path,
                "output_path": output_path,
                "result": result,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - returned to the main mutation loop
            return problem_id, {
                "source_path": source_path,
                "output_path": output_path,
                "result": None,
                "error": exc,
            }

    results: dict[str, dict[str, Any]] = {}
    if worker_count == 1:
        for job in jobs:
            problem_id, row = run(job)
            results[problem_id] = row
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(run, job) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            problem_id, row = future.result()
            results[problem_id] = row
    return results


def _active_image_enhance_size(provider: str, requested_size: str) -> tuple[str, bool]:
    """Keep image regeneration below the temporarily disabled 2K tier."""
    value = str(requested_size or "auto").strip()
    if provider != "gemini":
        return value, False
    high_resolution_skipped = value.upper() in HIGH_RES_IMAGE_SIZE_ALIASES
    if value.upper() in {"512", "512PX", "0.5K"}:
        return value, False
    return ACTIVE_IMAGE_ENHANCE_SIZE, high_resolution_skipped


def _mutate_enhance_image(session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    requested_mode = _normalize_image_enhance_mode(
        payload.get("mode") or payload.get("enhanceMode") or payload.get("enhance_mode") or "auto"
    )
    ai_enabled = _global_ai_enabled()
    if not ai_enabled and requested_mode == "ai":
        raise ValueError("AI 기능이 꺼져 있습니다. 설정에서 AI를 켠 뒤 다시 시도해 주세요.")
    if not ai_enabled and requested_mode == "auto":
        requested_mode = "preserve"
    provider = normalize_image_provider(
        payload.get("provider")
        or payload.get("imageProvider")
        or payload.get("image_provider")
        or DEFAULT_IMAGE_RECONSTRUCTION_PROVIDER
    )
    env_key = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
    provider_label = "Gemini" if provider == "gemini" else "OpenAI"
    api_key = os.environ.get(env_key, "").strip()

    problem_ids = _enhance_target_problem_ids(session, payload)
    if not problem_ids:
        raise ValueError("AI 업스케일할 문항이 없습니다.")

    enhance_plan: dict[str, tuple[str, str]] = {}
    for problem_id in problem_ids:
        _index, problem = _find_problem(session, problem_id)
        subject = _problem_image_subject(session, problem)
        enhance_plan[problem_id] = (subject, _resolved_image_enhance_mode(requested_mode, subject))
    if any(mode == "ai" for _subject, mode in enhance_plan.values()) and not api_key:
        raise ValueError(f"{provider_label} API 키가 필요합니다. 칠판 설정에서 {env_key}를 저장한 뒤 다시 시도해 주세요.")

    model = normalize_image_model(provider, str(payload.get("model") or payload.get("imageModel") or default_image_model(provider)))
    custom_prompt = str(payload.get("prompt") or payload.get("imagePrompt") or "").strip()
    quality = str(payload.get("quality") or "high")
    requested_size = str(payload.get("size") or "auto")
    size, high_resolution_skipped = _active_image_enhance_size(provider, requested_size)
    timeout_ms = int(payload.get("timeoutMs") or payload.get("timeout_ms") or 120000)
    transparent_background = _payload_bool(payload, "transparentBackground", "transparent_background", True)
    sharpen = _payload_bool(payload, "sharpen", "sharpen", True)
    output_dir = _image_reconstruction_dir(session)
    stamp = _unique_artifact_stamp()
    summaries: list[dict[str, Any]] = []
    preserve_results = _content_safe_primary_batch(
        session,
        problem_ids,
        enhance_plan,
        output_dir=output_dir,
        stamp=stamp,
        transparent_background=transparent_background,
        sharpen=sharpen,
    )

    for problem_id in problem_ids:
        _index, problem = _find_problem(session, problem_id)
        subject, resolved_mode = enhance_plan[problem_id]
        if resolved_mode == "preserve":
            problem_prompt = _default_reconstruction_prompt()
            prompt_profile = "content_safe_no_generation"
        elif custom_prompt:
            problem_prompt = custom_prompt
            prompt_profile = "custom"
        elif subject in _TEXT_PRESERVATION_SUBJECTS:
            problem_prompt = _text_priority_reconstruction_prompt()
            prompt_profile = "text_exact_copy"
        else:
            problem_prompt = _default_reconstruction_prompt()
            prompt_profile = "general_reconstruction"
        preserve_row = preserve_results.get(problem_id)
        source_path = preserve_row.get("source_path") if preserve_row else _original_problem_image_path(problem, session)
        if source_path is None or not source_path.exists():
            flags = list(problem.get("riskFlags") or [])
            flags.append("ai_image_missing_source")
            problem["riskFlags"] = list(dict.fromkeys(str(flag) for flag in flags if flag))
            problem["reviewStatus"] = "failed"
            problem["aiImageReconstruction"] = {
                "status": "missing_source",
                "error": f"problem image missing: {source_path}",
                "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            summaries.append({
                "problemId": problem_id,
                "status": "missing_source",
                "error": f"problem image missing: {source_path}",
            })
            continue

        output_model = "text-preserve-2x" if resolved_mode == "preserve" else model
        safe_model = sanitize_output_dir_name(output_model, max_bytes=80) or resolved_mode
        output_path = preserve_row.get("output_path") if preserve_row else _image_generation_output_path(
            output_dir,
            problem_id,
            f"{stamp}_{safe_model}",
        )
        attempts: list[dict[str, Any]] = []
        result: Any | None = None
        delivery_mode = "content_safe_primary" if resolved_mode == "preserve" else "ai_primary"
        auto_recovered = False
        final_error: Exception | None = None
        if resolved_mode == "preserve":
            if preserve_row is not None and preserve_row.get("result") is not None:
                result = preserve_row["result"]
                attempts.append(_image_attempt_summary(result, attempt=1, mode="content_safe_primary"))
            else:
                exc = preserve_row.get("error") if preserve_row else RuntimeError("content-safe batch did not return a result")
                final_error = exc
                attempts.append({
                    "attempt": 1,
                    "mode": "content_safe_primary",
                    "status": "failed",
                    "provider": "local",
                    "model": "content-safe-lanczos",
                    "error": str(exc),
                })
        else:
            try:
                result = reconstruct_problem_image(
                    source_path,
                    output_path,
                    api_key=api_key,
                    provider=provider,
                    model=model,
                    prompt=problem_prompt,
                    quality=quality,
                    size=size,
                    timeout_ms=timeout_ms,
                    transparent_background=transparent_background,
                    sharpen=sharpen,
                )
                attempts.append(_image_attempt_summary(result, attempt=1, mode="ai_primary"))
            except Exception as exc:  # noqa: BLE001 - automatically fall back below
                final_error = exc
                attempts.append({
                    "attempt": 1,
                    "mode": "ai_primary",
                    "status": "failed",
                    "provider": provider,
                    "model": model,
                    "error": str(exc),
                })

        if resolved_mode == "ai" and result is not None and not _result_passes_content_gate(result):
            retry_path = _image_generation_output_path(
                output_dir,
                problem_id,
                f"{stamp}_{safe_model}_retry",
            )
            retry_prompt = _content_recovery_prompt(problem_prompt, _result_content_preservation(result))
            try:
                retry_result = reconstruct_problem_image(
                    source_path,
                    retry_path,
                    api_key=api_key,
                    provider=provider,
                    model=model,
                    prompt=retry_prompt,
                    quality=quality,
                    size=size,
                    timeout_ms=timeout_ms,
                    transparent_background=transparent_background,
                    sharpen=sharpen,
                )
                attempts.append(_image_attempt_summary(retry_result, attempt=2, mode="ai_content_retry"))
                if _result_passes_content_gate(retry_result):
                    result = retry_result
                    delivery_mode = "ai_content_retry"
                    auto_recovered = True
            except Exception as exc:  # noqa: BLE001 - deterministic fallback remains available
                final_error = exc
                attempts.append({
                    "attempt": 2,
                    "mode": "ai_content_retry",
                    "status": "failed",
                    "provider": provider,
                    "model": model,
                    "error": str(exc),
                })

        if resolved_mode == "ai" and (result is None or not _result_passes_content_gate(result)):
            fallback_path = _image_generation_output_path(
                output_dir,
                problem_id,
                f"{stamp}_content_safe",
            )
            try:
                fallback_result = build_content_safe_upscale(
                    source_path,
                    fallback_path,
                    transparent_background=transparent_background,
                    sharpen=sharpen,
                )
                attempts.append(
                    _image_attempt_summary(
                        fallback_result,
                        attempt=len(attempts) + 1,
                        mode="content_safe_fallback",
                    )
                )
                result = fallback_result
                delivery_mode = "content_safe_fallback"
                auto_recovered = True
            except Exception as exc:  # noqa: BLE001 - report only after all automatic recovery failed
                final_error = exc

        if result is None:
            exc = final_error or RuntimeError("image reconstruction failed without a result")
            flags = list(problem.get("riskFlags") or [])
            flags.append("ai_image_reconstruction_failed")
            problem["riskFlags"] = list(dict.fromkeys(str(flag) for flag in flags if flag))
            problem["reviewStatus"] = "check_needed"
            problem["aiImageReconstruction"] = {
                "status": "failed",
                "provider": "local" if resolved_mode == "preserve" else provider,
                "model": "content-safe-lanczos" if resolved_mode == "preserve" else model,
                "requestedMode": requested_mode,
                "resolvedMode": resolved_mode,
                "subject": subject,
                "promptProfile": prompt_profile,
                "error": str(exc),
                "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "attempts": attempts,
            }
            summaries.append({
                "problemId": problem_id,
                "status": "failed",
                "provider": "local" if resolved_mode == "preserve" else provider,
                "model": "content-safe-lanczos" if resolved_mode == "preserve" else model,
                "requestedMode": requested_mode,
                "resolvedMode": resolved_mode,
                "subject": subject,
                "promptProfile": prompt_profile,
                "error": str(exc),
                "attempts": attempts,
            })
            continue

        if not problem.get("originalImagePath"):
            problem["originalImagePath"] = source_path.resolve().as_uri()
        uri = result.output_path.resolve().as_uri()
        problem["imagePath"] = uri
        problem["boardRenderPath"] = uri
        problem["step"] = "s3"
        problem["processingStep"] = "s3"
        problem["processing_step"] = "s3"
        flags = [
            str(flag)
            for flag in (problem.get("riskFlags") or [])
            if flag and str(flag) not in _AI_IMAGE_REVIEW_FLAGS
        ]
        postprocess = result.postprocess or {}
        content_preservation = postprocess.get("content_preservation")
        if not isinstance(content_preservation, dict):
            content_preservation = {}
        preservation_reasons = {
            str(reason or "").strip()
            for reason in (content_preservation.get("reasons") or [])
            if str(reason or "").strip()
        }
        loss_reasons = {
            "output_nearly_empty",
            "source_ink_missing",
            "localized_ink_loss",
            "formula_row_loss",
            "low_source_coverage",
        }
        formula_loss_suspected = "formula_row_loss" in preservation_reasons or (
            subject in {"math", "science"} and bool(preservation_reasons.intersection(loss_reasons))
        )
        if content_preservation.get("review_required") and formula_loss_suspected:
            flags.append("ai_image_formula_loss_suspected")
        elif content_preservation.get("review_required"):
            flags.append("ai_image_content_mismatch_suspected")
        elif not _result_passes_content_gate(result) and delivery_mode != "content_safe_fallback":
            flags.append("ai_image_reconstructed_check_text")
        semantic_text_preservation = _semantic_text_preservation_gate(
            subject=subject,
            resolved_mode=resolved_mode,
            delivery_mode=delivery_mode,
        )
        if semantic_text_preservation.get("review_required"):
            flags.append("ai_image_reconstructed_check_text")
        problem["riskFlags"] = list(dict.fromkeys(flags))
        problem["reviewStatus"] = "check_needed" if problem["riskFlags"] else "normal"
        problem["aiImageReconstruction"] = {
            **result.to_metadata(),
            "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "quality": quality,
            "size": size,
            "requestedSize": requested_size,
            "highResolutionSkipped": high_resolution_skipped,
            "requestedMode": requested_mode,
            "resolvedMode": resolved_mode,
            "subject": subject,
            "promptProfile": prompt_profile,
            "transparent_background": transparent_background,
            "sharpen": sharpen,
            "contentPreservation": content_preservation,
            "semanticTextPreservation": semantic_text_preservation,
            "deliveryMode": delivery_mode,
            "autoRecovered": auto_recovered,
            "attempts": attempts,
        }
        summaries.append({
            "problemId": problem_id,
            "status": "applied",
            "provider": result.provider,
            "model": result.model,
            "requestedMode": requested_mode,
            "resolvedMode": resolved_mode,
            "subject": subject,
            "promptProfile": prompt_profile,
            "outputPath": uri,
            "latencyMs": result.latency_ms,
            "postprocess": postprocess,
            "contentPreservation": content_preservation,
            "semanticTextPreservation": semantic_text_preservation,
            "deliveryMode": delivery_mode,
            "autoRecovered": auto_recovered,
            "attempts": attempts,
        })

    session["ai_image_reconstruction_summary"] = summaries
    return session


def _normalized_retry_problem(
    session: dict[str, Any],
    source_page: dict[str, Any],
    retry_problem: dict[str, Any],
    *,
    stamp: str,
    index: int,
    replacements_so_far: list[dict[str, Any]],
) -> dict[str, Any]:
    page_id = str(source_page.get("id") or "")
    source_path = _resolve_session_path(source_page.get("sourceImagePath") or source_page.get("sourceImageUri"))
    candidate_session = {**session, "problems": list(session.get("problems") or []) + replacements_so_far}
    new_id = _next_problem_id(candidate_session, f"{page_id}-ai", f"{stamp}-{index}")
    problem = dict(retry_problem)
    problem["id"] = new_id
    problem["sourcePageId"] = page_id
    problem["sourceFileName"] = source_path.name if source_path else str(source_page.get("id") or "AI retry")
    if source_path:
        problem["sourceImagePath"] = source_path.resolve().as_uri()
    risk_flags = [str(flag) for flag in (problem.get("riskFlags") or problem.get("risk_flags") or []) if flag]
    problem["riskFlags"] = list(dict.fromkeys(risk_flags))
    problem["reviewStatus"] = _problem_review_status(problem)
    problem["aiRetry"] = {
        "status": "applied",
        "sourcePageId": page_id,
        "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return problem


def _offset_retry_problem_to_source_page(problem: dict[str, Any], crop_box: Box) -> None:
    raw_bbox = problem.get("bbox")
    if isinstance(raw_bbox, dict):
        try:
            raw_bbox["left"] = float(raw_bbox.get("left") or 0.0) + crop_box.left
            raw_bbox["top"] = float(raw_bbox.get("top") or 0.0) + crop_box.top
            raw_bbox["width"] = float(raw_bbox.get("width") or 0.0)
            raw_bbox["height"] = float(raw_bbox.get("height") or 0.0)
        except (TypeError, ValueError):
            pass


def _partial_retry_identity_problem(
    source_problem: dict[str, Any],
    source_page: dict[str, Any],
    source_path: Path,
    retry_problems: list[dict[str, Any]],
    *,
    crop_box: Box,
    retry_dir: Path,
) -> dict[str, Any]:
    """Collapse partial-retry detections into one problem without changing identity.

    A partial retry is an edge-refinement operation, not a split operation.  AI
    providers may still return multiple candidates for one cropped region; the
    safe behavior is to use their union as the refined boundary while retaining
    the original problem id, title, number, metadata, and position in page order.
    """
    detected_boxes = [_bbox_from_problem(problem) for problem in retry_problems]
    left = min(box.left for box in detected_boxes)
    top = min(box.top for box in detected_boxes)
    right = max(box.right for box in detected_boxes)
    bottom = max(box.bottom for box in detected_boxes)

    from PIL import Image

    with Image.open(source_path) as source_image:
        refined_box = _coerce_crop_box(
            {
                "left": left,
                "top": top,
                "width": right - left,
                "height": bottom - top,
            },
            image_width=source_image.width,
            image_height=source_image.height,
        )

    problem_id = str(source_problem.get("id") or "")
    raw_crop_path = retry_dir / _make_crop_filename(problem_id, "ai_partial_identity")
    _crop_image_by_bbox(source_path, refined_box, raw_crop_path)
    raw_uri = raw_crop_path.resolve().as_uri()
    board_uri = raw_uri
    if _crop_refreshes_board_render(source_problem):
        board_crop_path = retry_dir / _make_crop_filename(problem_id, "ai_partial_identity_board")
        _render_board_crop_from_raw(raw_crop_path, board_crop_path, source_problem)
        board_uri = board_crop_path.resolve().as_uri()

    risk_flags = list(dict.fromkeys(
        str(flag)
        for problem in retry_problems
        for flag in (problem.get("riskFlags") or problem.get("risk_flags") or [])
        if flag
    ))
    if len(retry_problems) > 1:
        risk_flags.append("ai_partial_retry_multiple_candidates")
    risk_flags = list(dict.fromkeys(risk_flags))

    preserved = dict(source_problem)
    for key in (
        "manualCrop",
        "manual_crop",
        "cropBaseBbox",
        "cropBaseImagePath",
        "cropBaseBoardRenderPath",
        "cropBasePreserveMediaRegions",
    ):
        preserved.pop(key, None)
    preserved["id"] = problem_id
    preserved["sourcePageId"] = str(source_page.get("id") or source_problem.get("sourcePageId") or "")
    preserved["sourceImagePath"] = source_path.resolve().as_uri()
    preserved["sourceFileName"] = str(source_problem.get("sourceFileName") or source_path.name)
    preserved["bbox"] = {
        "left": refined_box.left,
        "top": refined_box.top,
        "width": refined_box.width,
        "height": refined_box.height,
    }
    preserved["imagePath"] = raw_uri
    preserved["boardRenderPath"] = board_uri
    preserved["cropBaseBbox"] = dict(preserved["bbox"])
    preserved["cropBaseImagePath"] = raw_uri
    preserved["cropBaseBoardRenderPath"] = board_uri
    preserved["cropBasePreserveMediaRegions"] = []
    preserved["preserveMediaRegions"] = []
    preserved["preserve_media_regions"] = []
    preserved["riskFlags"] = risk_flags
    preserved["risk_flags"] = list(risk_flags)
    preserved["reviewStatus"] = "check_needed" if risk_flags else "normal"
    preserved["review_status"] = preserved["reviewStatus"]
    preserved["parseFailed"] = False
    preserved["parse_failed"] = False
    preserved["replacesProblemId"] = problem_id
    preserved["replaces_problem_id"] = problem_id
    preserved["aiRetry"] = {
        "status": "applied",
        "partial": True,
        "preservedProblemIdentity": True,
        "replacesProblemId": problem_id,
        "sourcePageId": preserved["sourcePageId"],
        "detectedProblemCount": len(retry_problems),
        "collapsedMultipleCandidates": len(retry_problems) > 1,
        "cropBox": {
            "left": crop_box.left,
            "top": crop_box.top,
            "width": crop_box.width,
            "height": crop_box.height,
        },
        "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _refresh_mutated_crop_layout(preserved, raw_crop_path)
    return preserved


def _retry_target_problem_ids(payload: dict[str, Any]) -> list[str]:
    raw_problem_ids = payload.get("problemIds") or payload.get("problem_ids") or []
    if payload.get("problemId") or payload.get("problem_id"):
        raw_problem_ids = [payload.get("problemId") or payload.get("problem_id")]
    return list(dict.fromkeys(str(pid) for pid in raw_problem_ids if pid))


def _payload_crop_box_for_problem(payload: dict[str, Any], problem_id: str) -> Any:
    crop_boxes = payload.get("cropBoxes") or payload.get("crop_boxes") or {}
    if isinstance(crop_boxes, dict) and problem_id in crop_boxes:
        return crop_boxes[problem_id]
    return payload.get("cropBox") or payload.get("crop_box")


def _mutate_retry_ai_partial(session: dict[str, Any], payload: dict[str, Any], problem_ids: list[str]) -> dict[str, Any]:
    stamp = _unique_artifact_stamp()
    session_output_dir = session.get("output_dir")
    if session_output_dir:
        output_root = Path(session_output_dir).resolve() / "ai_retries"
    else:
        output_root = (RUNTIME_DIR / "ai_retries").resolve()
    ai_config = session.get("ai_fallback") if isinstance(session.get("ai_fallback"), dict) else {}
    summaries: list[dict[str, Any]] = []

    for job_index, problem_id in enumerate(problem_ids, start=1):
        try:
            _problem_index, problem = _find_problem(session, problem_id)
            page = _page_for_problem(session, problem)
            if page is None:
                raise FileNotFoundError(f"source page missing for partial retry: {problem_id}")
            source_path = _resolve_session_path(page.get("sourceImagePath") or page.get("sourceImageUri"))
            if source_path is None or not source_path.exists():
                raise FileNotFoundError(f"source page image missing for partial retry: {problem_id}")

            from PIL import Image

            raw_crop_box = _payload_crop_box_for_problem(payload, problem_id) or problem.get("bbox")
            with Image.open(source_path) as source_image:
                crop_box = _coerce_crop_box(
                    raw_crop_box,
                    image_width=source_image.width,
                    image_height=source_image.height,
                )
            retry_dir = output_root / sanitize_output_dir_name(problem_id, suffix=stamp)
            partial_source_path = retry_dir / "partial_source.png"
            _crop_image_by_bbox(source_path, crop_box, partial_source_path)
            result = run_problem_export(
                partial_source_path,
                output_dir=retry_dir,
                subject_name=str(payload.get("subject") or "unknown"),
                ocr=str(payload.get("ocr") or "auto"),
                pdf_dpi=int(payload.get("pdfDpi") or payload.get("pdf_dpi") or 200),
                detect_perspective=False,
                skip_deskew=True,
                skip_crop=True,
                max_dimension=None,
                export_edb=False,
                edb_name="ai_retry.edb",
                sync_ui=False,
                record_mode=str(session.get("record_mode") or "image-only"),
                text_confidence_threshold=float(session.get("text_confidence_threshold") or 0.78),
                input_intent=str(payload.get("inputIntent") or payload.get("input_intent") or "single-problem"),
                ai_fallback_enabled=True,
                ai_fallback="force",
                ai_fallback_provider=str(ai_config.get("provider") or "gemini"),
                ai_fallback_model=str(ai_config.get("model") or ""),
                ai_fallback_max_tokens=ai_config.get("max_tokens"),
                ai_fallback_temperature=ai_config.get("temperature"),
                ai_fallback_threshold=float(ai_config.get("threshold") or 0.72),
                ai_fallback_max_regions=int(ai_config.get("max_regions") or 30),
                ai_fallback_timeout_ms=int(ai_config.get("timeout_ms") or 30000),
                ai_fallback_save_debug=bool(ai_config.get("save_debug")),
            )
            retry_session = result.get("ui_session") or {}
            retry_problems = [p for p in retry_session.get("problems", []) if isinstance(p, dict)]
            if not retry_problems:
                raise ValueError("partial AI retry produced no problems")

            normalized_retry_problems: list[dict[str, Any]] = []
            for index, retry_problem in enumerate(retry_problems, start=1):
                normalized = _normalized_retry_problem(
                    session,
                    page,
                    retry_problem,
                    stamp=stamp,
                    index=(job_index * 1000) + index,
                    replacements_so_far=normalized_retry_problems,
                )
                _offset_retry_problem_to_source_page(normalized, crop_box)
                normalized_retry_problems.append(normalized)
            replacement = _partial_retry_identity_problem(
                problem,
                page,
                source_path,
                normalized_retry_problems,
                crop_box=crop_box,
                retry_dir=retry_dir,
            )
            _replace_single_problem(session, problem_id, [replacement])
            summaries.append({
                "problemId": problem_id,
                "pageId": str(page.get("id") or ""),
                "status": "applied",
                "partial": True,
                "preservedProblemIdentity": True,
                "detectedProblemCount": len(normalized_retry_problems),
                "collapsedMultipleCandidates": len(normalized_retry_problems) > 1,
                "replacedProblemCount": 1,
            })
        except Exception as exc:  # noqa: BLE001 - record per-problem retry failure
            try:
                _idx, failed_problem = _find_problem(session, problem_id)
                flags = list(failed_problem.get("riskFlags") or [])
                flags.append("ai_partial_retry_failed")
                failed_problem["riskFlags"] = list(dict.fromkeys(str(flag) for flag in flags if flag))
                failed_problem["reviewStatus"] = "check_needed"
                failed_problem["aiRetry"] = {
                    "status": "failed",
                    "partial": True,
                    "error": str(exc),
                    "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            except Exception:
                pass
            summaries.append({
                "problemId": problem_id,
                "status": "failed",
                "partial": True,
                "error": str(exc),
            })

    session["ai_retry_summary"] = summaries
    _refresh_session_problem_counts(session)
    return session


def _mutate_classify(session: dict[str, Any], problem_id: str, classification: str) -> dict[str, Any]:
    normalized = str(classification or "").strip().lower().replace("_", "-")
    if normalized not in {"question", "shared-passage"}:
        raise ValueError("classification must be question or shared-passage")
    if not problem_id:
        raise ValueError("problemId is required")

    _index, problem = _find_problem(session, problem_id)
    metadata = problem.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
        problem["metadata"] = metadata

    existing_group_id = str(
        problem.get("passageGroupId")
        or problem.get("passage_group_id")
        or metadata.get("passageGroupId")
        or metadata.get("passage_group_id")
        or ""
    ).strip()

    if normalized == "shared-passage":
        group_id = existing_group_id or f"manual-passage-{problem_id}"
        role: str | None = "passage_fragment"
        supplemental = True
        problem["passageGroupId"] = group_id
        problem["passage_group_id"] = group_id
        metadata["passageGroupId"] = group_id
        metadata["passage_group_id"] = group_id
    else:
        manual_group = existing_group_id.startswith("manual-passage-")
        role = "child_question" if existing_group_id and not manual_group else None
        supplemental = False
        if manual_group:
            for container in (problem, metadata):
                container.pop("passageGroupId", None)
                container.pop("passage_group_id", None)
                container.pop("passageRange", None)
                container.pop("passage_range", None)

    for container in (problem, metadata):
        if role is None:
            container.pop("passageRole", None)
            container.pop("passage_role", None)
        else:
            container["passageRole"] = role
            container["passage_role"] = role
        container["supplementalItem"] = supplemental
        container["supplemental_item"] = supplemental
        container["classificationSource"] = "manual"
        container["classification_source"] = "manual"

    _refresh_session_problem_counts(session)
    return session


def _mutate_classify_many(
    session: dict[str, Any],
    problem_ids: Any,
    classification: str,
) -> dict[str, Any]:
    if not isinstance(problem_ids, list) or not problem_ids:
        raise ValueError("problemIds must be a non-empty list")
    ordered_ids = list(dict.fromkeys(str(value or "").strip() for value in problem_ids))
    ordered_ids = [problem_id for problem_id in ordered_ids if problem_id]
    if not ordered_ids:
        raise ValueError("problemIds must contain at least one id")
    for problem_id in ordered_ids:
        _mutate_classify(session, problem_id, classification)
    _refresh_session_problem_counts(session)
    return session


def _mutate_retry_ai(session: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if not _global_ai_enabled():
        raise ValueError("AI 기능이 꺼져 있습니다. 설정에서 AI를 켠 뒤 다시 시도해 주세요.")
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise ValueError("Gemini API 키가 필요합니다. 칠판 설정에서 키를 저장한 뒤 다시 시도해 주세요.")

    if _coerce_bool(payload.get("partial") or payload.get("partialRetry") or payload.get("partial_retry")):
        problem_ids = _retry_target_problem_ids(payload)
        if not problem_ids:
            raise ValueError("부분 재인식할 문항이 없습니다")
        return _mutate_retry_ai_partial(session, payload, problem_ids)

    page_ids = _retry_target_page_ids(session, payload)
    if not page_ids:
        raise ValueError("AI 재인식할 페이지가 없습니다")

    stamp = _unique_artifact_stamp()
    session_output_dir = session.get("output_dir")
    if session_output_dir:
        output_root = Path(session_output_dir).resolve() / "ai_retries"
    else:
        output_root = (RUNTIME_DIR / "ai_retries").resolve()
    ai_config = session.get("ai_fallback") if isinstance(session.get("ai_fallback"), dict) else {}
    summaries: list[dict[str, Any]] = []
    retry_jobs: list[dict[str, Any]] = []
    retry_results_by_page_id: dict[str, dict[str, Any]] = {}

    for page_id in page_ids:
        page = _find_page(session, page_id)
        source_path = _resolve_session_path(page.get("sourceImagePath") or page.get("sourceImageUri"))
        if source_path is None or not source_path.exists():
            retry_results_by_page_id[page_id] = {
                "pageId": page_id,
                "status": "missing_source",
                "error": f"source page image missing for retry: {page_id}",
            }
        else:
            retry_jobs.append({
                "pageId": page_id,
                "sourcePath": source_path,
                "retryDir": output_root / sanitize_output_dir_name(page_id, suffix=stamp),
            })

    def _run_retry_job(job: dict[str, Any]) -> dict[str, Any]:
        page_id = str(job["pageId"])
        try:
            result = run_problem_export(
                job["sourcePath"],
                output_dir=job["retryDir"],
                subject_name=str(payload.get("subject") or "unknown"),
                ocr=str(payload.get("ocr") or "auto"),
                pdf_dpi=int(payload.get("pdfDpi") or payload.get("pdf_dpi") or 200),
                detect_perspective=False,
                skip_deskew=True,
                skip_crop=True,
                max_dimension=None,
                export_edb=False,
                edb_name="ai_retry.edb",
                sync_ui=False,
                record_mode=str(session.get("record_mode") or "image-only"),
                text_confidence_threshold=float(session.get("text_confidence_threshold") or 0.78),
                input_intent="multi-problem",
                ai_fallback_enabled=True,
                ai_fallback="force",
                ai_fallback_provider=str(ai_config.get("provider") or "gemini"),
                ai_fallback_model=str(ai_config.get("model") or ""),
                ai_fallback_max_tokens=ai_config.get("max_tokens"),
                ai_fallback_temperature=ai_config.get("temperature"),
                ai_fallback_threshold=float(ai_config.get("threshold") or 0.72),
                ai_fallback_max_regions=int(ai_config.get("max_regions") or 30),
                ai_fallback_timeout_ms=int(ai_config.get("timeout_ms") or 30000),
                ai_fallback_save_debug=bool(ai_config.get("save_debug")),
            )
        except Exception as exc:  # noqa: BLE001 - show the actionable pipeline message to the UI
            return {"pageId": page_id, "status": "failed", "error": str(exc)}
        return {"pageId": page_id, "status": "ok", "result": result}

    forced_retry_ai_config = build_ai_fallback_config(
        mode="force",
        provider=str(ai_config.get("provider") or "gemini"),
        model=str(ai_config.get("model") or ""),
        max_tokens=ai_config.get("max_tokens"),
        threshold=float(ai_config.get("threshold") or 0.72),
        max_regions=int(ai_config.get("max_regions") or 30),
        timeout_ms=int(ai_config.get("timeout_ms") or 30000),
        save_debug=bool(ai_config.get("save_debug")),
    )
    retry_worker_count = resolve_recognition_worker_count(
        len(retry_jobs),
        ocr_mode=str(payload.get("ocr") or "auto"),
        ai_config=forced_retry_ai_config,
    )
    if retry_worker_count <= 1:
        retry_job_results = [_run_retry_job(job) for job in retry_jobs]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=retry_worker_count) as executor:
            retry_job_results = list(executor.map(_run_retry_job, retry_jobs))
    for retry_result in retry_job_results:
        retry_results_by_page_id[str(retry_result["pageId"])] = retry_result

    for page_id in page_ids:
        page = _find_page(session, page_id)
        retry_result = retry_results_by_page_id.get(page_id) or {
            "pageId": page_id,
            "status": "failed",
            "error": "retry did not produce a result",
        }
        if retry_result.get("status") == "missing_source":
            page["reviewStatus"] = "failed"
            flags = list(page.get("riskFlags") or [])
            flags.append("ai_retry_missing_source")
            page["riskFlags"] = list(dict.fromkeys(str(flag) for flag in flags if flag))
            page["aiRetry"] = {
                "status": "missing_source",
                "error": str(retry_result.get("error") or f"source page image missing for retry: {page_id}"),
                "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "workerCount": retry_worker_count,
            }
            summaries.append({
                "pageId": page_id,
                "status": "missing_source",
                "error": str(retry_result.get("error") or f"source page image missing for retry: {page_id}"),
                "workerCount": retry_worker_count,
            })
            continue
        if retry_result.get("status") != "ok":
            page["reviewStatus"] = "failed"
            flags = list(page.get("riskFlags") or [])
            flags.append("ai_retry_failed")
            page["riskFlags"] = list(dict.fromkeys(str(flag) for flag in flags if flag))
            page["aiRetry"] = {
                "status": "failed",
                "error": str(retry_result.get("error") or "AI retry failed"),
                "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "workerCount": retry_worker_count,
            }
            summaries.append({
                "pageId": page_id,
                "status": "failed",
                "error": str(retry_result.get("error") or "AI retry failed"),
                "workerCount": retry_worker_count,
            })
            continue

        result = retry_result.get("result") or {}
        retry_session = result.get("ui_session") or {}
        retry_problems = [p for p in retry_session.get("problems", []) if isinstance(p, dict)]
        retry_pages = [p for p in retry_session.get("pages", []) if isinstance(p, dict)]
        retry_page = retry_pages[0] if retry_pages else {}

        if not retry_problems:
            page["reviewStatus"] = "failed"
            flags = list(page.get("riskFlags") or [])
            flags.append("ai_retry_empty")
            page["riskFlags"] = list(dict.fromkeys(str(flag) for flag in flags if flag))
            page["aiRetry"] = {
                "status": "empty",
                "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "workerCount": retry_worker_count,
            }
            summaries.append({
                "pageId": page_id,
                "status": "empty",
                "replacedProblemCount": 0,
                "workerCount": retry_worker_count,
            })
            continue

        previous_problem_count = len(page.get("problemIds") or [])
        replacements: list[dict[str, Any]] = []
        for index, retry_problem in enumerate(retry_problems, start=1):
            replacements.append(
                _normalized_retry_problem(
                    session,
                    page,
                    retry_problem,
                    stamp=stamp,
                    index=index,
                    replacements_so_far=replacements,
                )
            )

        _replace_page_problems(session, page_id, replacements)
        page["riskFlags"] = [str(flag) for flag in (retry_page.get("riskFlags") or []) if flag]
        page["reviewStatus"] = "check_needed" if page["riskFlags"] else "normal"
        page["aiRetry"] = {
            "status": "applied",
            "attemptedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "previousProblemCount": previous_problem_count,
            "replacedProblemCount": len(replacements),
            "aiSummary": retry_session.get("ai_summary"),
            "workerCount": retry_worker_count,
        }
        summaries.append({
            "pageId": page_id,
            "status": "applied",
            "replacedProblemCount": len(replacements),
            "workerCount": retry_worker_count,
        })

    session["ai_retry_summary"] = summaries
    _refresh_session_problem_counts(session)
    return session


def _denormalize_session_paths(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Rewrite any /api/file?path=... values in a session snapshot back to
    file:// URIs so the server's "latest_session" stays canonical regardless
    of whether the snapshot came from JS (where everything is /api/file URLs)
    or from a fresh build (where everything is file://)."""
    cloned = json.loads(json.dumps(snapshot))

    def fix(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value.startswith("/api/file"):
            decoded = decode_file_reference(value)
            if decoded is not None:
                return _path_as_file_uri(decoded)
        return value

    for problem in cloned.get("problems", []) or []:
        if not isinstance(problem, dict):
            continue
        for key in ("imagePath", "sourceImagePath", "boardRenderPath", "originalImagePath"):
            problem[key] = fix(problem.get(key))
    for page in cloned.get("pages", []) or []:
        if not isinstance(page, dict):
            continue
        page["sourceImageUri"] = fix(page.get("sourceImageUri"))
    cloned["edb_file_uri"] = fix(cloned.get("edb_file_uri"))
    cloned["rendered_page_file_uris"] = [fix(v) for v in cloned.get("rendered_page_file_uris", [])]
    return cloned


class AppHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass):
        super().__init__(server_address, RequestHandlerClass)
        _recover_interrupted_session_reset()
        self._state_lock = threading.RLock()
        # Artifact-producing handlers write files before committing session
        # metadata. Serialize the entire handler so a stale CAS loser cannot
        # overwrite the winner's files before its commit is rejected.
        self._artifact_job_lock = threading.Lock()
        self._active_artifact_jobs = 0
        # Encode process start order into the epoch so a browser can reject a
        # delayed response from a server incarnation it never observed.  The
        # UUID keeps concurrently-started processes unique.
        self._session_epoch = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
        latest_session = load_latest_session()
        history = load_session_history()
        if latest_session is not None:
            _refresh_session_problem_counts(latest_session)
        self.latest_session: dict[str, Any] | None = _clone_jsonish(latest_session) if latest_session else None
        self._session_revision = 1 if latest_session is not None else 0
        self.allowed_files: set[str] = collect_session_file_paths(latest_session) if latest_session else set()
        self.allowed_files |= collect_session_history_file_paths(history)

    def session_snapshot(self) -> dict[str, Any] | None:
        with self._state_lock:
            return _clone_jsonish(self.latest_session) if self.latest_session is not None else None

    def session_snapshot_with_revision(self) -> tuple[dict[str, Any] | None, int]:
        with self._state_lock:
            snapshot = _clone_jsonish(self.latest_session) if self.latest_session is not None else None
            return snapshot, self._session_revision

    def session_revision(self) -> int:
        with self._state_lock:
            return self._session_revision

    def session_epoch(self) -> str:
        return self._session_epoch

    def add_allowed_files(self, paths: set[str]) -> None:
        with self._state_lock:
            self.allowed_files.update(paths)

    def is_file_allowed(self, path: str) -> bool:
        with self._state_lock:
            return path in self.allowed_files

    def remember_session(self, session: dict[str, Any]) -> None:
        snapshot = _clone_jsonish(session)
        with self._state_lock:
            history, history_error = persist_latest_session_with_history(snapshot)
            self.latest_session = snapshot
            self._session_revision += 1
            self.allowed_files.update(collect_session_file_paths(snapshot))
            self.allowed_files.update(collect_session_history_file_paths(history))
            if history_error is not None:
                print(
                    f"[app-server] latest session committed but history update failed: {history_error}",
                    file=sys.stderr,
                )

    def remember_session_if_current(
        self,
        expected_session: dict[str, Any] | None,
        updated_session: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> int | None:
        """Persist ``updated_session`` only when the caller's base is current.

        Request handlers perform image/OCR work outside the server lock.  Two
        concurrent requests may therefore start from the same snapshot.  The
        equality check and write must share one critical section so the slower
        request cannot silently overwrite the result that committed first.
        """
        expected_snapshot = _clone_jsonish(expected_session) if isinstance(expected_session, dict) else None
        updated_snapshot = _clone_jsonish(updated_session)
        with self._state_lock:
            if expected_revision is not None and self._session_revision != expected_revision:
                return None
            if self.latest_session != expected_snapshot:
                return None
            history, history_error = persist_latest_session_with_history(updated_snapshot)
            self.latest_session = updated_snapshot
            self._session_revision += 1
            self.allowed_files.update(collect_session_file_paths(updated_snapshot))
            self.allowed_files.update(collect_session_history_file_paths(history))
            if history_error is not None:
                print(
                    f"[app-server] latest session committed but history update failed: {history_error}",
                    file=sys.stderr,
                )
            return self._session_revision

    def adopt_staged_publish_if_current(
        self,
        expected_session: dict[str, Any] | None,
        updated_session: dict[str, Any],
        *,
        staging_dir: Path,
        final_dir: Path,
        expected_revision: int | None = None,
    ) -> int | None:
        """CAS-check first, then atomically expose one immutable publish generation."""
        expected_snapshot = _clone_jsonish(expected_session) if isinstance(expected_session, dict) else None
        updated_snapshot = _clone_jsonish(updated_session)
        with self._state_lock:
            if expected_revision is not None and self._session_revision != expected_revision:
                return None
            if self.latest_session != expected_snapshot:
                return None
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            if final_dir.exists():
                raise FileExistsError(f"publish generation already exists: {final_dir}")
            os.replace(staging_dir, final_dir)
            try:
                history, history_error = persist_latest_session_with_history(updated_snapshot)
            except BaseException:
                try:
                    os.replace(final_dir, staging_dir)
                except OSError as rollback_exc:
                    print(
                        f"[app-server] publish adoption rollback failed: {rollback_exc}",
                        file=sys.stderr,
                    )
                raise
            self.latest_session = updated_snapshot
            self._session_revision += 1
            self.allowed_files.update(collect_session_file_paths(updated_snapshot))
            self.allowed_files.update(collect_session_history_file_paths(history))
            if history_error is not None:
                print(
                    f"[app-server] latest session committed but history update failed: {history_error}",
                    file=sys.stderr,
                )
            return self._session_revision

    def begin_artifact_job(self) -> None:
        self._artifact_job_lock.acquire()
        try:
            with self._state_lock:
                self._active_artifact_jobs += 1
        except BaseException:
            self._artifact_job_lock.release()
            raise

    def end_artifact_job(self) -> None:
        try:
            with self._state_lock:
                self._active_artifact_jobs = max(0, self._active_artifact_jobs - 1)
        finally:
            self._artifact_job_lock.release()

    def begin_artifact_read(self) -> None:
        # Reads may run concurrently with each other and with immutable
        # generation writes, but reset/retention cleanup must not unlink their
        # backing files until the response stream has finished.
        with self._state_lock:
            self._active_artifact_jobs += 1

    def end_artifact_read(self) -> None:
        with self._state_lock:
            self._active_artifact_jobs = max(0, self._active_artifact_jobs - 1)

    def register_current_session(self, session: dict[str, Any]) -> bool:
        """Persist history only if ``session`` is still the current snapshot."""
        snapshot = _clone_jsonish(session)
        with self._state_lock:
            if self.latest_session != snapshot:
                return False
            self.allowed_files.update(collect_session_file_paths(snapshot))
            with _session_storage_lock:
                if not LATEST_SESSION_JSON.exists():
                    save_latest_session(snapshot)
                history = _session_history_with_session(load_session_history(), snapshot)
                try:
                    save_session_history(history)
                except OSError as exc:
                    print(
                        f"[app-server] session history refresh failed: {exc}",
                        file=sys.stderr,
                    )
        return True

    def cleanup_artifacts(
        self,
        *,
        dry_run: bool = True,
        min_age_seconds: float = DEFAULT_ARTIFACT_RETENTION_DAYS * 86_400,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        # Hold the state lock through selection/deletion. Otherwise a concurrent
        # remember_session could make an already-selected file active.
        with self._state_lock:
            if not dry_run and self._active_artifact_jobs:
                raise ArtifactCleanupBusy(
                    f"artifact cleanup paused while {self._active_artifact_jobs} request(s) are using files"
                )
            return cleanup_runtime_artifacts(
                active_session=_clone_jsonish(self.latest_session) if self.latest_session is not None else {},
                history=load_session_history(),
                dry_run=dry_run,
                min_age_seconds=min_age_seconds,
                max_bytes=max_bytes,
            )

    def clear_session(
        self,
        *,
        expected_revision: int | None = None,
        cleanup_artifacts: bool = False,
        dry_run: bool = True,
        min_age_seconds: float = DEFAULT_ARTIFACT_RETENTION_DAYS * 86_400,
    ) -> dict[str, Any] | None:
        cleanup_result, _revision = self.clear_session_with_revision(
            expected_revision=expected_revision,
            cleanup_artifacts=cleanup_artifacts,
            dry_run=dry_run,
            min_age_seconds=min_age_seconds,
        )
        return cleanup_result

    def clear_session_with_revision(
        self,
        *,
        expected_revision: int | None = None,
        cleanup_artifacts: bool = False,
        dry_run: bool = True,
        min_age_seconds: float = DEFAULT_ARTIFACT_RETENTION_DAYS * 86_400,
    ) -> tuple[dict[str, Any] | None, int]:
        with self._state_lock:
            if expected_revision is not None and self._session_revision != expected_revision:
                raise SessionRevisionConflict(
                    f"session revision changed from {expected_revision} to {self._session_revision}"
                )
            if self._active_artifact_jobs:
                raise ArtifactCleanupBusy(
                    f"session reset paused while {self._active_artifact_jobs} request(s) are using files"
                )
            _atomically_clear_persisted_session_state()
            self.latest_session = None
            self._session_revision += 1
            cleared_revision = self._session_revision
            self.allowed_files.clear()
            if cleanup_artifacts:
                try:
                    cleanup_result = cleanup_runtime_artifacts(
                        active_session={},
                        history=[],
                        dry_run=dry_run,
                        min_age_seconds=min_age_seconds,
                    )
                except Exception as exc:  # reset is complete even if optional cleanup fails
                    _log_operation_exception("session_reset.artifact_cleanup", exc)
                    cleanup_result = {
                        "ok": False,
                        "code": "artifact_cleanup_failed",
                        "operation": "artifact_cleanup",
                        "retryable": True,
                        "dryRun": bool(dry_run),
                        "error": str(exc),
                        "errors": [{"error": str(exc)}],
                    }
                return (
                    cleanup_result,
                    cleared_revision,
                )
        return None, cleared_revision


class AppRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    @property
    def app_server(self) -> AppHTTPServer:
        return self.server  # type: ignore[return-value]

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        if not _request_host_is_loopback(self.headers):
            self.send_error(HTTPStatus.MISDIRECTED_REQUEST, "loopback Host header required")
            return False
        if self.command in {"POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            if not _browser_write_request_is_trusted(self.headers):
                self.send_error(HTTPStatus.FORBIDDEN, "cross-site state-changing request rejected")
                return False
        return True

    def _current_session_snapshot(self) -> dict[str, Any] | None:
        server = getattr(self, "server", None)
        if server is None:
            return load_latest_session()
        snapshotter = getattr(server, "session_snapshot", None)
        if callable(snapshotter):
            return snapshotter()
        session = getattr(server, "latest_session", None)
        return session if isinstance(session, dict) else load_latest_session()

    def _current_session_state(self) -> tuple[dict[str, Any] | None, int | None]:
        server = getattr(self, "server", None)
        if server is None:
            return self._current_session_snapshot(), None
        snapshotter = getattr(server, "session_snapshot_with_revision", None)
        if callable(snapshotter):
            return snapshotter()
        return self._current_session_snapshot(), None

    @staticmethod
    def _payload_session_revision(payload: dict[str, Any]) -> int | None:
        raw_revision = payload.get("expectedSessionRevision", payload.get("expected_session_revision"))
        if isinstance(raw_revision, bool) or raw_revision is None:
            return None
        try:
            revision = int(raw_revision)
        except (TypeError, ValueError):
            return None
        return revision if revision >= 0 else None

    @staticmethod
    def _payload_session_epoch(payload: dict[str, Any]) -> str | None:
        raw_epoch = payload.get("expectedSessionEpoch", payload.get("expected_session_epoch"))
        epoch = str(raw_epoch or "").strip()
        return epoch or None

    def _validate_expected_session_revision(
        self,
        payload: dict[str, Any],
        current_revision: int | None,
    ) -> bool:
        # Test doubles and older embedding hosts without revision support keep
        # their compatibility path. The real server requires a revision that
        # came from the client's last successful session response.
        if current_revision is None:
            return True
        epoch_getter = getattr(getattr(self, "server", None), "session_epoch", None)
        current_epoch = str(epoch_getter() or "").strip() if callable(epoch_getter) else ""
        epoch_matches = not current_epoch or self._payload_session_epoch(payload) == current_epoch
        if epoch_matches and self._payload_session_revision(payload) == current_revision:
            return True
        self._send_session_conflict()
        return False

    def _remember_session_if_current(
        self,
        expected_session: dict[str, Any] | None,
        updated_session: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> tuple[bool, int | None]:
        registrar = getattr(self.app_server, "remember_session_if_current", None)
        if callable(registrar):
            result = registrar(
                expected_session,
                updated_session,
                expected_revision=expected_revision,
            )
            if type(result) is int:
                return True, result
            return bool(result), None
        # Lightweight test doubles and older embedding hosts do not expose the
        # CAS helper. Preserve compatibility while the real server remains
        # concurrency-safe.
        self.app_server.remember_session(updated_session)
        return True, None

    def _adopt_staged_publish_if_current(
        self,
        expected_session: dict[str, Any] | None,
        updated_session: dict[str, Any],
        *,
        staging_dir: Path,
        final_dir: Path,
        expected_revision: int | None = None,
    ) -> tuple[bool, int | None]:
        adopter = getattr(self.app_server, "adopt_staged_publish_if_current", None)
        if callable(adopter):
            result = adopter(
                expected_session,
                updated_session,
                staging_dir=staging_dir,
                final_dir=final_dir,
                expected_revision=expected_revision,
            )
            if type(result) is int:
                return True, result
            return bool(result), None
        # Compatibility for lightweight embedding hosts. The generation path
        # is unique, so exposing it cannot overwrite an older successful EDB.
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_dir, final_dir)
        try:
            committed = self._remember_session_if_current(
                expected_session,
                updated_session,
                expected_revision=expected_revision,
            )
        except BaseException:
            os.replace(final_dir, staging_dir)
            raise
        if not committed[0]:
            try:
                os.replace(final_dir, staging_dir)
            except OSError:
                _discard_staged_publish(final_dir)
        return committed

    def _send_session_conflict(self) -> None:
        current, current_revision = self._current_session_state()
        payload: dict[str, Any] = {
            "ok": False,
            "code": "session_conflict",
            "error": "다른 작업이 먼저 세션을 변경했습니다. 최신 상태를 불러온 뒤 다시 시도해 주세요.",
            "session": rewrite_session_for_http(current) if isinstance(current, dict) else None,
            "recoverySteps": [
                "화면의 최신 세션 상태를 확인해 주세요.",
                "방금 수행한 작업을 최신 상태에서 다시 시도해 주세요.",
            ],
        }
        if current_revision is not None:
            payload["sessionRevision"] = current_revision
        self._send_json(
            payload,
            status=HTTPStatus.CONFLICT,
        )

    def _run_artifact_job(self, handler) -> None:
        server = getattr(self, "server", None)
        starter = getattr(server, "begin_artifact_job", None)
        finisher = getattr(server, "end_artifact_job", None)
        if not callable(starter) or not callable(finisher):
            handler()
            return
        starter()
        try:
            handler()
        finally:
            finisher()

    def _run_artifact_read(self, handler) -> None:
        server = getattr(self, "server", None)
        starter = getattr(server, "begin_artifact_read", None)
        finisher = getattr(server, "end_artifact_read", None)
        if not callable(starter) or not callable(finisher):
            handler()
            return
        starter()
        try:
            handler()
        finally:
            finisher()

    def _allow_session_files(self, paths: set[str]) -> None:
        adder = getattr(self.app_server, "add_allowed_files", None)
        if callable(adder):
            adder(paths)
            return
        self.app_server.allowed_files |= paths

    def _file_is_allowed(self, path: str) -> bool:
        checker = getattr(self.app_server, "is_file_allowed", None)
        if callable(checker):
            return bool(checker(path))
        return path in self.app_server.allowed_files

    def log_message(self, format: str, *args) -> None:
        client = self.client_address[0] if self.client_address else "-"
        print(f"[app-server] {client} - {format % args}")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        super().end_headers()

    def _rewrite_legacy_ui_asset_path(self, path: str) -> bool:
        alias = LEGACY_UI_ASSET_ALIASES.get(path)
        if not alias:
            return False
        self.path = alias
        return True

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if self._rewrite_legacy_ui_asset_path(parsed.path):
            return super().do_HEAD()
        if parsed.path in {"", "/"}:
            self.path = "/index.html"
        else:
            self.path = parsed.path
        return super().do_HEAD()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json({"ok": True, "app": APP_NAME})
            return
        if parsed.path == "/generated_session.js":
            self._send_text(read_generated_session_js(), content_type="application/javascript; charset=utf-8")
            return
        if self._rewrite_legacy_ui_asset_path(parsed.path):
            return super().do_GET()
        if parsed.path == "/api/runtime-diagnostics":
            params = parse_qs(parsed.query)
            force_refresh = str(params.get("refresh", [""])[0]).strip().lower() in {"1", "true", "yes"}
            self._send_json(cached_runtime_diagnostics(force_refresh=force_refresh))
            return
        if parsed.path == "/api/app/update":
            self._send_json(build_app_update_status())
            return
        if parsed.path == "/api/session/latest":
            self._handle_latest_session()
            return
        if parsed.path == "/api/session/history":
            self._run_artifact_read(self._handle_session_history)
            return
        if parsed.path == "/api/session/problem-image":
            self._run_artifact_read(lambda: self._handle_session_problem_image(parsed))
            return
        if parsed.path == "/api/file":
            self._run_artifact_read(lambda: self._handle_file(parsed))
            return
        if parsed.path == "/api/user-settings":
            self._handle_user_settings_get()
            return
        if parsed.path in {"", "/"}:
            self.path = "/index.html"
        else:
            self.path = parsed.path
        return super().do_GET()

    def do_POST(self) -> None:
        try:
            self._dispatch_post()
        except RequestPayloadTooLarge as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": "upload payload is too large",
                    "code": "payload_too_large",
                    "maxBytes": exc.limit,
                    "receivedBytes": exc.content_length,
                    "recoverySteps": [
                        "한 번에 올리는 파일 수를 줄여 다시 시도해 주세요.",
                        "큰 PDF는 여러 파일로 나누거나 이미지 해상도를 낮춰 주세요.",
                    ],
                },
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )

    def _dispatch_post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/export":
            self._run_artifact_job(self._handle_export)
            return
        if parsed.path == "/api/user-settings":
            self._handle_user_settings_post()
            return
        if parsed.path == "/api/bug-report":
            self._handle_bug_report()
            return
        if parsed.path == "/api/runtime/artifacts/cleanup":
            self._handle_runtime_artifact_cleanup()
            return
        if parsed.path == "/api/session/publish":
            self._run_artifact_job(self._handle_session_publish)
            return
        if parsed.path == "/api/session/classin-review":
            self._handle_session_classin_review()
            return
        if parsed.path == "/api/system/open-folder":
            self._handle_open_folder()
            return
        if parsed.path == "/api/system/open-file":
            self._handle_open_file()
            return
        if parsed.path == "/api/system/open-url":
            self._handle_open_url()
            return
        if parsed.path == "/api/system/shutdown":
            self._handle_shutdown()
            return
        if parsed.path == "/api/session/mutate":
            self._run_artifact_job(self._handle_session_mutate)
            return
        if parsed.path == "/api/session/export-images":
            self._run_artifact_job(self._handle_session_export_images)
            return
        if parsed.path == "/api/session/export-edb":
            self._run_artifact_job(self._handle_session_export_edb)
            return
        if parsed.path == "/api/session/problem-image":
            self._run_artifact_read(self._handle_session_problem_image_post)
            return
        if parsed.path == "/api/session/retry-ai":
            self._run_artifact_job(self._handle_session_retry_ai)
            return
        if parsed.path == "/api/session/enhance-image":
            self._run_artifact_job(self._handle_session_enhance_image)
            return
        if parsed.path == "/api/session/restore":
            self._run_artifact_job(self._handle_session_restore)
            return
        if parsed.path == "/api/session/history/restore":
            self._run_artifact_job(self._handle_session_history_restore)
            return
        self._send_json({"ok": False, "error": "unknown endpoint"}, status=HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/session/latest":
            self._handle_session_clear(parsed)
            return
        self._send_json({"ok": False, "error": "unknown endpoint"}, status=HTTPStatus.NOT_FOUND)

    def _handle_session_publish(self) -> None:
        session, current_revision = self._current_session_state()
        if session is None:
            self._send_json({"ok": False, "error": "no session available"}, status=HTTPStatus.NOT_FOUND)
            return
        base_session = _clone_jsonish(session)
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not self._validate_expected_session_revision(payload, current_revision):
            return

        payload_session = payload.get("session")
        if isinstance(payload_session, dict) and isinstance(payload_session.get("problems"), list):
            session = _denormalize_session_paths(payload_session)

        order = list(payload.get("order") or [])
        excluded = set(payload.get("excluded") or [])
        placement_payload = payload.get("placements")
        if not isinstance(placement_payload, dict):
            placement_payload = {}

        raw_problem_values = session.get("problems")
        if not isinstance(raw_problem_values, list):
            raw_problem_values = []
        raw_id_issues = _session_problem_id_issues(raw_problem_values)
        raw_problems = [problem for problem in raw_problem_values if isinstance(problem, dict)]
        if raw_id_issues:
            raw_id_preflight = {
                "status": "blocked",
                "passed": False,
                "checkedProblemCount": len(raw_problem_values),
                "checked_problem_count": len(raw_problem_values),
                "issueCount": len(raw_id_issues),
                "issue_count": len(raw_id_issues),
                "issues": [{**issue, "blocking": True} for issue in raw_id_issues],
                "gate": "session_publish",
                "gateLabel": "EDB publish",
                "gate_label": "EDB publish",
            }
            self._send_json(
                _session_publish_preflight_blocked_payload(raw_id_preflight, []),
                status=HTTPStatus.CONFLICT,
            )
            return

        by_id = {
            str(p.get("id") or p.get("problem_id") or "").strip(): p
            for p in raw_problems
        }
        sequence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for pid in order:
            if pid in by_id and pid not in excluded and pid not in seen:
                sequence.append(by_id[pid])
                seen.add(pid)
        for pid, problem in by_id.items():
            if pid in excluded or pid in seen:
                continue
            sequence.append(problem)
            seen.add(pid)

        if not sequence:
            self._send_json(
                {
                    "ok": False,
                    "code": "publish_empty_selection",
                    "operation": "session_publish",
                    "retryable": False,
                    "error": "제외 후 제작할 문항이 남아 있지 않습니다.",
                    "recoverySteps": ["제작할 문항을 하나 이상 포함한 뒤 다시 시도해 주세요."],
                },
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        sequence_with_placements: list[dict[str, Any]] = []
        for problem in sequence:
            problem_copy = dict(problem)
            problem_id = str(problem_copy.get("id") or "")
            preserve_legacy_scale = (
                _problem_preserves_legacy_placement_scale(problem_copy)
                or _problem_has_persisted_legacy_placement_scale(problem_copy)
            )
            x_ratio = _coerce_placement_x_ratio(placement_payload.get(problem_id))
            if x_ratio is None:
                x_ratio = _coerce_placement_x_ratio(problem_copy)
            if x_ratio is not None:
                problem_copy["placementXRatio"] = x_ratio
            y_ratio = _coerce_placement_y_ratio(placement_payload.get(problem_id))
            if y_ratio is None:
                y_ratio = _coerce_placement_y_ratio(problem_copy)
            if y_ratio is not None:
                problem_copy["placementYRatio"] = y_ratio
            scale_ratio = _coerce_placement_scale_ratio(placement_payload.get(problem_id))
            if scale_ratio is None:
                scale_ratio = _coerce_placement_scale_ratio(problem_copy)
            if scale_ratio is not None:
                problem_copy["placementScaleRatio"] = scale_ratio
            if preserve_legacy_scale:
                # Provenance comes from the persisted session value, never from
                # this request's placement patch. New explicit edits therefore
                # remain subject to the regular 1.6 editor/export ceiling.
                problem_copy["preserveLegacyPlacementScale"] = True
                problem_copy["preserve_legacy_placement_scale"] = True
            sequence_with_placements.append(problem_copy)
        sequence = sequence_with_placements

        publish_preflight, duplicate_groups = _session_publish_blocking_preflight(sequence, session=session)
        if not publish_preflight.get("passed"):
            self._send_json(
                _session_publish_preflight_blocked_payload(publish_preflight, duplicate_groups),
                status=HTTPStatus.CONFLICT,
            )
            return

        template = _template_from_session(session)
        try:
            entries = _problems_to_entries(sequence, template=template)
        except FileNotFoundError as exc:
            self._send_json(_publish_stage_failure_payload("build", exc), status=HTTPStatus.CONFLICT)
            return
        except (KeyError, ValueError) as exc:
            self._send_json(
                _publish_failure_payload(
                    code="publish_session_invalid",
                    message="현재 작업에 EDB 제작 필수 정보가 없습니다",
                    exc=exc,
                    retryable=False,
                    recovery_steps=[
                        "원본 PDF를 다시 등록해 새 작업을 만든 뒤 제작해 주세요.",
                        "반복되면 오류 정보를 복사해 신고해 주세요.",
                    ],
                ),
                status=HTTPStatus.CONFLICT,
            )
            return

        # Resize the logical canvas to match the actual problem count after the
        # user may have excluded items in the review UI. Mirrors the formula in
        # run_problem_export so mvp_board.edb and the published EDB agree.
        template.board_page_count = max(50, len(entries) * 2)
        output_dir = Path(session.get("output_dir") or RUNTIME_DIR / "publish_output").resolve()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        requested_edb_name = payload.get("edbName") if "edbName" in payload else payload.get("edb_name")
        edb_name = sanitize_edb_file_name(
            str(requested_edb_name) if requested_edb_name is not None else None,
            fallback_stem=f"{session.get('session_name') or 'classin'}-published-{stamp}",
        )
        generation_name = sanitize_output_dir_name(
            Path(edb_name).stem,
            suffix=_unique_artifact_stamp(),
        )
        staging_dir = output_dir / ".publish-staging" / generation_name
        final_dir = output_dir / "published" / generation_name
        try:
            staging_dir.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            _log_operation_exception("session_publish.prepare_output", exc)
            self._send_json(
                _publish_stage_failure_payload("prepare", exc),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        crop_format = _normalize_crop_format(session.get("crop_format"))

        try:
            records, placements, header_flag = build_records(
                entries,
                template,
                record_mode="image-only",
                output_dir=staging_dir,
                text_confidence_threshold=0.78,
                dark_board=True,
                board_theme=session.get("board_theme") or DEFAULT_BOARD_THEME,
                crop_format=crop_format,
            )
        except Exception as exc:  # noqa: BLE001 — report the exact pipeline stage
            _discard_staged_publish(staging_dir)
            _log_operation_exception("session_publish.build_records", exc)
            self._send_json(
                _publish_stage_failure_payload("build", exc),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        edb_parts: list[dict[str, Any]] = []
        edb_path: Path | None = None
        try:
            staged_edb_parts = write_classin_limited_edb_files(
                entries,
                template,
                staging_dir,
                edb_name,
                record_mode="image-only",
                text_confidence_threshold=0.78,
                dark_board=True,
                board_theme=session.get("board_theme") or DEFAULT_BOARD_THEME,
                crop_format=crop_format,
                existing_records=records,
                existing_placements=placements,
                existing_header_flag=header_flag,
            )
            if not staged_edb_parts:
                raise ValueError("no EDB parts were generated")
            staged_edb_validation, staged_edb_parts = _validate_edb_parts(staged_edb_parts)
            edb_parts = _remap_artifact_paths(staged_edb_parts, staging_dir, final_dir)
            edb_validation = _remap_artifact_paths(
                staged_edb_validation,
                staging_dir,
                final_dir,
            )
            edb_path = Path(str(edb_parts[0]["edbPath"]))
        except Exception as exc:  # noqa: BLE001
            _discard_staged_publish(staging_dir)
            _log_operation_exception("session_publish.write_edb", exc)
            self._send_json(
                _publish_stage_failure_payload("write", exc),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        core_problem_count = sum(1 for problem in sequence if not _session_problem_is_supplemental(problem))
        supplemental_item_count = sum(1 for problem in sequence if _session_problem_is_supplemental(problem))

        try:
            new_session = build_ui_session(
                prepared_pages=[],
                placements=placements,
                output_dir=output_dir,
                edb_path=edb_path,
                source_paths=session.get("input_files") or [],
                record_mode="image-only",
                ai_fallback_config=session.get("ai_fallback"),
                ai_summary=session.get("ai_summary"),
                template=template,
                board_theme=session.get("board_theme") or DEFAULT_BOARD_THEME,
                crop_format=crop_format,
            )
            new_session = _remap_artifact_paths(new_session, staging_dir, final_dir)
        except Exception as exc:  # noqa: BLE001
            _discard_staged_publish(staging_dir)
            _log_operation_exception("session_publish.build_session", exc)
            self._send_json(
                _publish_stage_failure_payload("build", exc),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        # Keep the stable working root for the next publish; all generated
        # artifact references themselves point at this immutable generation.
        new_session["output_dir"] = str(output_dir)
        new_session["outputDir"] = str(output_dir)
        # carry over user-facing labels so the rename doesn't get lost
        if session.get("session_name"):
            new_session["session_name"] = session["session_name"]
        _copy_session_metadata_aliases_overwrite(
            new_session,
            session,
            ("inputIntent", "input_intent"),
        )
        _copy_session_metadata_aliases_overwrite(
            new_session,
            session,
            ("contentTarget", "content_target"),
        )
        new_session["crop_format"] = crop_format
        # publish only re-renders records; page-level review metadata
        # (sourceImageUri, dimensions, riskFlags) is still meaningful for the
        # caller, so preserve it across the publish hop.
        if session.get("pages"):
            preserved_pages: list[dict[str, Any]] = []
            problem_ids_remaining = {str(problem.get("id")) for problem in new_session.get("problems", []) if problem.get("id")}
            for page in session["pages"]:
                page_copy = dict(page)
                page_copy["problemIds"] = [pid for pid in page.get("problemIds", []) if pid in problem_ids_remaining]
                preserved_pages.append(page_copy)
            new_session["pages"] = preserved_pages
        # propagate per-problem bbox/riskFlags from the prior session — they
        # are derived from segmentation, which publish does not re-run.
        prior_problems_by_id = {
            str(problem.get("id")): problem
            for problem in session.get("problems", [])
            if isinstance(problem, dict) and problem.get("id")
        }
        for problem in new_session.get("problems", []):
            prior = prior_problems_by_id.get(str(problem.get("id")))
            if not prior:
                continue
            _copy_publish_problem_metadata(problem, prior)
            _copy_publish_problem_layout_metadata(problem, prior)
            # build_ui_session points originalImagePath at the image used for
            # this publish. Replace that value with the true first-generation
            # source from the prior session (or the recovered page-as-is source)
            # so later enhancement never upscales an already enhanced image.
            prior_original_path = _original_problem_image_path(prior, session)
            if prior_original_path is not None and prior_original_path.exists():
                prior_original_uri = prior_original_path.resolve().as_uri()
                problem["originalImagePath"] = prior_original_uri
                problem["original_image_path"] = prior_original_uri
            if "bbox" not in problem or not problem["bbox"]:
                problem["bbox"] = prior.get("bbox") or {}
            problem["riskFlags"] = [
                str(flag)
                for flag in (prior.get("riskFlags") or [])
                if str(flag) not in {"duplicate_problem_number", "fallback_grouping"}
            ]
            problem["reviewStatus"] = "check_needed" if problem["riskFlags"] else "normal"
            if "placementXRatio" not in problem:
                x_ratio = _coerce_placement_x_ratio(prior)
                if x_ratio is not None:
                    problem["placementXRatio"] = x_ratio
            if "placementYRatio" not in problem:
                y_ratio = _coerce_placement_y_ratio(prior)
                if y_ratio is not None:
                    problem["placementYRatio"] = y_ratio
            if "placementScaleRatio" not in problem:
                scale_ratio = _coerce_placement_scale_ratio(prior)
                if scale_ratio is not None:
                    problem["placementScaleRatio"] = scale_ratio
        _annotate_session_with_edb_part_metadata(new_session, edb_parts)
        _refresh_session_problem_counts(new_session)

        source_paths_for_handoff = [
            path
            for path in (_file_uri_to_path(value) for value in (session.get("input_files") or session.get("inputFiles") or []))
            if path is not None
        ]
        try:
            staged_handoff_path, staged_handoff_markdown_path = write_classin_handoff_manifest(
                staging_dir,
                source_paths=source_paths_for_handoff,
                edb_path=edb_path,
                edb_parts=edb_parts,
                ui_session=new_session,
                summary={
                    "record_count": len(records),
                    "record_mode": "image-only",
                    "crop_format": crop_format,
                    "board_theme": session.get("board_theme") or DEFAULT_BOARD_THEME,
                    "placements": placements,
                    "edb_parts": edb_parts,
                    "edb_split": len(edb_parts) > 1,
                },
                template=template,
            )
        except Exception as exc:  # noqa: BLE001 — keep publish failures explicit for the UI.
            _discard_staged_publish(staging_dir)
            _log_operation_exception("session_publish.write_handoff", exc)
            self._send_json(
                _publish_stage_failure_payload("handoff", exc),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        classin_preflight: dict[str, Any] = {}
        handoff_payload: dict[str, Any] = {}
        try:
            raw_handoff_payload = json.loads(staged_handoff_path.read_text(encoding="utf-8"))
            handoff_payload = raw_handoff_payload if isinstance(raw_handoff_payload, dict) else {}
            handoff_payload = _remap_artifact_paths(handoff_payload, staging_dir, final_dir)
            _atomic_write_text(
                staged_handoff_path,
                json.dumps(handoff_payload, ensure_ascii=False, indent=2),
            )
            markdown_payload = staged_handoff_markdown_path.read_text(encoding="utf-8")
            markdown_payload = markdown_payload.replace(
                str(staging_dir.resolve()),
                str(final_dir.resolve()),
            ).replace(_path_as_file_uri(staging_dir), _path_as_file_uri(final_dir))
            _atomic_write_text(staged_handoff_markdown_path, markdown_payload)
            if isinstance(handoff_payload.get("classinPreflight"), dict):
                classin_preflight = dict(handoff_payload["classinPreflight"])
        except (OSError, json.JSONDecodeError) as exc:
            _discard_staged_publish(staging_dir)
            _log_operation_exception("session_publish.validate_handoff", exc)
            self._send_json(
                _publish_stage_failure_payload("handoff", exc),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        classin_handoff_path = final_dir / staged_handoff_path.relative_to(staging_dir)
        classin_handoff_markdown_path = final_dir / staged_handoff_markdown_path.relative_to(staging_dir)

        new_session["classin_handoff_path"] = str(classin_handoff_path)
        new_session["classinHandoffPath"] = str(classin_handoff_path)
        new_session["classin_handoff_markdown_path"] = str(classin_handoff_markdown_path)
        new_session["classinHandoffMarkdownPath"] = str(classin_handoff_markdown_path)
        new_session["edb_parts"] = edb_parts
        new_session["edbParts"] = edb_parts
        new_session["edb_part_count"] = len(edb_parts)
        new_session["edbPartCount"] = len(edb_parts)
        new_session["edb_split"] = len(edb_parts) > 1
        new_session["edbSplit"] = len(edb_parts) > 1
        new_session["classin_preflight"] = classin_preflight
        new_session["classinPreflight"] = classin_preflight
        if isinstance(handoff_payload.get("passageReviewItems"), list):
            new_session["passageReviewItems"] = [
                dict(item)
                for item in handoff_payload.get("passageReviewItems", [])
                if isinstance(item, dict)
            ]
            new_session["passage_review_items"] = new_session["passageReviewItems"]
            new_session["passageReviewItemCount"] = (
                int(handoff_payload.get("passageReviewItemCount"))
                if isinstance(handoff_payload.get("passageReviewItemCount"), (int, float, str))
                and str(handoff_payload.get("passageReviewItemCount")).isdigit()
                else len(new_session["passageReviewItems"])
            )
            new_session["passage_review_item_count"] = new_session["passageReviewItemCount"]
            new_session["crossPagePassageReviewItemCount"] = (
                int(handoff_payload.get("crossPagePassageReviewItemCount"))
                if isinstance(handoff_payload.get("crossPagePassageReviewItemCount"), (int, float, str))
                and str(handoff_payload.get("crossPagePassageReviewItemCount")).isdigit()
                else sum(
                    1
                    for item in new_session["passageReviewItems"]
                    if item.get("continuesAcrossPages") or item.get("continues_across_pages")
                )
            )
            new_session["cross_page_passage_review_item_count"] = new_session["crossPagePassageReviewItemCount"]
            publish_problems = [
                problem
                for problem in (new_session.get("problems") or [])
                if isinstance(problem, dict)
            ]
            publish_pages = [
                page
                for page in (new_session.get("pages") or [])
                if isinstance(page, dict)
            ]
            _normalize_session_passage_review_queue(
                new_session,
                unresolved_problem_ids=_session_unresolved_review_problem_ids(
                    problems=publish_problems,
                    pages=publish_pages,
                    actionable_flags=set((new_session.get("reviewSummary") or {}).get("actionableRiskFlagCounts") or {}),
                ),
            )

        passage_group_source_reuse_groups = None
        for source in (handoff_payload, new_session):
            for key in ("passageGroupSourceReuseGroups", "passage_group_source_reuse_groups"):
                value = source.get(key)
                if isinstance(value, list):
                    passage_group_source_reuse_groups = value
                    break
            if passage_group_source_reuse_groups is not None:
                break
        passage_group_source_reuse_group_count = None
        for source in (handoff_payload, new_session):
            for key in ("passageGroupSourceReuseGroupCount", "passage_group_source_reuse_group_count"):
                value = source.get(key)
                if isinstance(value, (int, float, str)) and str(value).isdigit():
                    passage_group_source_reuse_group_count = int(value)
                    break
            if passage_group_source_reuse_group_count is not None:
                break

        source_problem_overlap_groups = None
        for source in (handoff_payload, new_session):
            for key in ("sourceProblemOverlapGroups", "source_problem_overlap_groups"):
                value = source.get(key)
                if isinstance(value, list):
                    source_problem_overlap_groups = value
                    break
            if source_problem_overlap_groups is not None:
                break
        source_problem_overlap_group_count = None
        for source in (handoff_payload, new_session):
            for key in ("sourceProblemOverlapGroupCount", "source_problem_overlap_group_count"):
                value = source.get(key)
                if isinstance(value, (int, float, str)) and str(value).isdigit():
                    source_problem_overlap_group_count = int(value)
                    break
            if source_problem_overlap_group_count is not None:
                break
        layout_diagnostics = None
        for source in (handoff_payload, new_session):
            for key in ("layoutDiagnostics", "layout_diagnostics"):
                value = source.get(key)
                if isinstance(value, dict):
                    layout_diagnostics = value
                    break
            if layout_diagnostics is not None:
                break

        staged_publish_summary = _session_publish_summary(
            edb_path=Path(str(staged_edb_parts[0]["edbPath"])),
            output_dir=staging_dir,
            edb_validation=staged_edb_validation,
            record_count=len(records),
            core_problem_count=core_problem_count,
            supplemental_item_count=supplemental_item_count,
            classin_handoff_path=staged_handoff_path,
            classin_handoff_markdown_path=staged_handoff_markdown_path,
            classin_preflight=classin_preflight,
            passage_groups=(
                handoff_payload.get("passageGroups")
                if isinstance(handoff_payload.get("passageGroups"), list)
                else None
            ),
            passage_group_count=(
                int(handoff_payload.get("passageGroupCount"))
                if isinstance(handoff_payload.get("passageGroupCount"), (int, float, str))
                and str(handoff_payload.get("passageGroupCount")).isdigit()
                else None
            ),
            passage_problem_count=(
                int(handoff_payload.get("passageProblemCount"))
                if isinstance(handoff_payload.get("passageProblemCount"), (int, float, str))
                and str(handoff_payload.get("passageProblemCount")).isdigit()
                else None
            ),
            cross_page_passage_group_count=(
                int(handoff_payload.get("crossPagePassageGroupCount"))
                if isinstance(handoff_payload.get("crossPagePassageGroupCount"), (int, float, str))
                and str(handoff_payload.get("crossPagePassageGroupCount")).isdigit()
                else None
            ),
            passage_review_items=(
                new_session.get("passageReviewItems")
                if isinstance(new_session.get("passageReviewItems"), list)
                else None
            ),
            passage_review_item_count=(
                int(new_session.get("passageReviewItemCount"))
                if isinstance(new_session.get("passageReviewItemCount"), (int, float, str))
                and str(new_session.get("passageReviewItemCount")).isdigit()
                else None
            ),
            cross_page_passage_review_item_count=(
                int(new_session.get("crossPagePassageReviewItemCount"))
                if isinstance(new_session.get("crossPagePassageReviewItemCount"), (int, float, str))
                and str(new_session.get("crossPagePassageReviewItemCount")).isdigit()
                else None
            ),
            passage_group_source_reuse_groups=passage_group_source_reuse_groups,
            passage_group_source_reuse_group_count=passage_group_source_reuse_group_count,
            source_problem_overlap_groups=source_problem_overlap_groups,
            source_problem_overlap_group_count=source_problem_overlap_group_count,
            layout_diagnostics=layout_diagnostics,
            edb_parts=staged_edb_parts,
        )
        publish_summary = _remap_artifact_paths(
            staged_publish_summary,
            staging_dir,
            final_dir,
        )
        publish_history = _session_publish_history(session, publish_summary)
        new_session["publish_summary"] = publish_summary
        new_session["publishSummary"] = publish_summary
        new_session["publish_history"] = publish_history
        new_session["publishHistory"] = publish_history
        try:
            committed, committed_revision = self._adopt_staged_publish_if_current(
                base_session,
                new_session,
                staging_dir=staging_dir,
                final_dir=final_dir,
                expected_revision=current_revision,
            )
        except Exception as exc:  # noqa: BLE001
            _discard_staged_publish(staging_dir)
            _log_operation_exception("session_publish.commit_generation", exc)
            self._send_json(
                _publish_stage_failure_payload("commit", exc),
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        if not committed:
            _discard_staged_publish(staging_dir)
            self._send_session_conflict()
            return
        response = {
            "ok": True,
            "session": rewrite_session_for_http(new_session),
            "edbValidation": edb_validation,
            "edb_validation": edb_validation,
            "publishSummary": publish_summary,
            "publish_summary": publish_summary,
            "publishHistory": publish_history,
            "publish_history": publish_history,
        }
        if committed_revision is not None:
            response["sessionRevision"] = committed_revision
        self._send_json(response)

    # ── /api/session/mutate ──────────────────────────────────────────────
    # Body: { "action": "split" | "merge" | "crop" | "stitch-crop" | "bulk-crop" | "exclude" | "confirm" | "confirm-page" | "classify", ...args }
    # Returns the updated session (rewritten for HTTP).
    def _handle_session_mutate(self) -> None:
        session, current_revision = self._current_session_state()
        if session is None:
            self._send_json({"ok": False, "error": "no session available"}, status=HTTPStatus.NOT_FOUND)
            return
        base_session = _clone_jsonish(session)
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not self._validate_expected_session_revision(payload, current_revision):
            return

        action = str(payload.get("action") or "").strip().lower()
        try:
            if action == "split":
                problem_id = str(payload.get("problemId") or payload.get("problem_id") or "")
                raw_ratio = payload.get("splitYRatio")
                if raw_ratio is None:
                    raw_ratio = payload.get("split_y_ratio")
                if raw_ratio is None:
                    raw_ratio = 0.5
                split_ratio = float(raw_ratio)
                new_session = _mutate_split(session, problem_id, split_ratio)
            elif action == "merge":
                ids_raw = payload.get("problemIds") or payload.get("problem_ids") or []
                problem_ids = [str(pid) for pid in ids_raw if pid]
                new_session = _mutate_merge(session, problem_ids)
            elif action in {"crop", "manual-crop", "manual_crop"}:
                problem_id = str(payload.get("problemId") or payload.get("problem_id") or "")
                raw_crop = payload.get("crop")
                if raw_crop is None:
                    raw_crop = payload.get("manualCrop")
                if raw_crop is None:
                    raw_crop = payload.get("manual_crop")
                if raw_crop is None:
                    raw_crop = payload
                new_session = _mutate_crop(session, problem_id, raw_crop)
            elif action in {"stitch-crop", "stitch_crop"}:
                problem_id = str(payload.get("problemId") or payload.get("problem_id") or "")
                segments = payload.get("segments")
                new_session = _mutate_stitch_crop(session, problem_id, segments)
            elif action in {"bulk-crop", "bulk_crop"}:
                page_id = str(payload.get("pageId") or payload.get("page_id") or "")
                regions = payload.get("regions")
                replace_ids = payload.get("replaceProblemIds", payload.get("replace_problem_ids"))
                new_session = _mutate_bulk_crop(session, page_id, regions, replace_ids)
            elif action == "exclude":
                ids_raw = payload.get("problemIds", payload.get("problem_ids"))
                if ids_raw is not None:
                    new_session = _mutate_exclude_many(session, ids_raw)
                else:
                    problem_id = str(payload.get("problemId") or payload.get("problem_id") or "")
                    new_session = _mutate_exclude(session, problem_id)
            elif action == "confirm":
                ids_raw = payload.get("problemIds", payload.get("problem_ids"))
                if ids_raw is None:
                    ids_raw = [payload.get("problemId", payload.get("problem_id"))]
                new_session = _mutate_confirm(session, ids_raw)
            elif action in {"confirm-page", "confirm_page"}:
                ids_raw = payload.get("pageIds", payload.get("page_ids"))
                if ids_raw is None:
                    ids_raw = [payload.get("pageId", payload.get("page_id"))]
                decision = str(payload.get("decision") or "no_passage")
                new_session = _mutate_confirm_pages(session, ids_raw, decision=decision)
            elif action == "classify":
                classification = str(payload.get("classification") or "")
                ids_raw = payload.get("problemIds", payload.get("problem_ids"))
                if ids_raw is not None:
                    new_session = _mutate_classify_many(session, ids_raw, classification)
                else:
                    problem_id = str(payload.get("problemId") or payload.get("problem_id") or "")
                    new_session = _mutate_classify(session, problem_id, classification)
            elif action in {"retry-ai", "retry_ai"}:
                new_session = _mutate_retry_ai(session, payload)
            elif action in {"enhance-image", "enhance_image"}:
                new_session = _mutate_enhance_image(session, payload)
            else:
                self._send_json(
                    {"ok": False, "error": f"unknown action: {action!r} (expected split|merge|crop|stitch-crop|bulk-crop|exclude|confirm|confirm-page|classify|retry-ai|enhance-image)"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return

        committed, committed_revision = self._remember_session_if_current(
            base_session,
            new_session,
            expected_revision=current_revision,
        )
        if not committed:
            self._send_session_conflict()
            return
        response = {"ok": True, "session": rewrite_session_for_http(new_session)}
        if committed_revision is not None:
            response["sessionRevision"] = committed_revision
        self._send_json(response)

    def _handle_session_export_images(self) -> None:
        session = self._current_session_snapshot()
        if session is None:
            self._send_json({"ok": False, "error": "no session available"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return

        payload_session = payload.get("session")
        if isinstance(payload_session, dict) and isinstance(payload_session.get("problems"), list):
            session = _denormalize_session_paths(payload_session)

        raw_problem_ids = payload.get("problemIds", payload.get("problem_ids"))
        problem_ids = _coerce_problem_ids(raw_problem_ids) if raw_problem_ids is not None else None
        try:
            result = _write_session_image_export_zip(
                session,
                str(payload.get("mode") or "both"),
                problem_ids=problem_ids,
            )
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except OSError as exc:
            self._send_json({"ok": False, "error": f"failed to export images: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        zip_path = Path(result["zipPath"]).resolve()
        self._allow_session_files({str(zip_path)})
        self._send_json({
            "ok": True,
            "downloadUrl": path_to_api_url(zip_path),
            "zipPath": str(zip_path),
            "fileName": result["fileName"],
            "count": result["count"],
            "missing": result["missing"],
            "mode": result["mode"],
        })

    def _handle_session_export_edb(self) -> None:
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return

        raw_paths = payload.get("edbPaths", payload.get("edb_paths"))
        if not isinstance(raw_paths, list) or not raw_paths:
            self._send_json({"ok": False, "error": "edbPaths is required"}, status=HTTPStatus.BAD_REQUEST)
            return

        resolved_paths: list[Path] = []
        seen_paths: set[str] = set()
        for raw_path in raw_paths:
            path = decode_file_reference(str(raw_path or ""))
            if path is None or path.suffix.lower() != ".edb":
                self._send_json({"ok": False, "error": "invalid EDB file path"}, status=HTTPStatus.BAD_REQUEST)
                return
            normalized = str(path.resolve())
            if not self._file_is_allowed(normalized):
                self._send_json({"ok": False, "error": "EDB file not allowed"}, status=HTTPStatus.FORBIDDEN)
                return
            if not path.is_file():
                self._send_json({"ok": False, "error": "EDB file not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if normalized not in seen_paths:
                resolved_paths.append(path.resolve())
                seen_paths.add(normalized)

        if len(resolved_paths) == 1:
            path = resolved_paths[0]
            self._send_json({
                "ok": True,
                "downloadUrl": path_to_api_url(path),
                "fileName": path.name,
                "count": 1,
                "bundled": False,
            })
            return

        try:
            result = _write_edb_export_zip(resolved_paths, payload.get("fileName") or payload.get("file_name"))
        except (OSError, ValueError) as exc:
            self._send_json(
                {"ok": False, "error": f"failed to export EDB files: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        zip_path = Path(result["zipPath"]).resolve()
        self._allow_session_files({str(zip_path)})
        self._send_json({
            "ok": True,
            "downloadUrl": path_to_api_url(zip_path),
            "zipPath": str(zip_path),
            "fileName": result["fileName"],
            "count": result["count"],
            "bundled": True,
        })

    def _send_session_problem_image_response(self, session: dict[str, Any], problem_id: str, variant: str) -> None:
        if not problem_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "problemId is required")
            return
        problems = [problem for problem in session.get("problems", []) if isinstance(problem, dict)]
        problem = next((problem for problem in problems if str(problem.get("id") or "") == problem_id), None)
        if problem is None:
            self.send_error(HTTPStatus.NOT_FOUND, "problem not found")
            return
        index = problems.index(problem) + 1
        filename = _safe_problem_image_download_filename(index, problem.get("title"), problem_id)
        normalized_variant = str(variant or "board").strip().lower()
        if normalized_variant == "board":
            try:
                rendered_payload = _session_problem_board_png_payload(
                    problem,
                    board_theme=str(session.get("board_theme") or session.get("boardTheme") or DEFAULT_BOARD_THEME),
                )
            except Exception as exc:  # noqa: BLE001 - surface a direct download failure.
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"failed to render problem image: {exc}")
                return
            if rendered_payload is not None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(rendered_payload)))
                self.send_header("Content-Disposition", content_disposition_attachment(filename))
                self.end_headers()
                self.wfile.write(rendered_payload)
                return

        source_path = _session_problem_image_download_source_path(problem, variant)
        if source_path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "problem image not found")
            return
        try:
            if source_path.suffix.lower() == ".png":
                file_size = source_path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(file_size))
                self.send_header("Content-Disposition", content_disposition_attachment(filename))
                self.end_headers()
                with source_path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
                return
            payload = _encode_image_as_png_bytes(source_path)
        except OSError as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"failed to read problem image: {exc}")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", content_disposition_attachment(filename))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_session_problem_image(self, parsed) -> None:
        session = self._current_session_snapshot()
        if session is None:
            self.send_error(HTTPStatus.NOT_FOUND, "no session available")
            return
        query = parse_qs(parsed.query)
        problem_id = str(query.get("problemId", query.get("problem_id", [""]))[0] or "").strip()
        variant = str(query.get("variant", ["board"])[0] or "board")
        self._send_session_problem_image_response(session, problem_id, variant)

    def _handle_session_problem_image_post(self) -> None:
        session = self._current_session_snapshot()
        if session is None:
            self.send_error(HTTPStatus.NOT_FOUND, "no session available")
            return
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, f"invalid JSON: {exc}")
            return
        payload_session = payload.get("session")
        if isinstance(payload_session, dict) and isinstance(payload_session.get("problems"), list):
            session = _denormalize_session_paths(payload_session)
        problem_id = str(payload.get("problemId") or payload.get("problem_id") or "").strip()
        variant = str(payload.get("variant") or "board")
        self._send_session_problem_image_response(session, problem_id, variant)

    def _handle_session_retry_ai(self) -> None:
        session, current_revision = self._current_session_state()
        if session is None:
            self._send_json({"ok": False, "error": "no session available"}, status=HTTPStatus.NOT_FOUND)
            return
        base_session = _clone_jsonish(session)
        try:
            payload = self._read_json_body()
            if not self._validate_expected_session_revision(payload, current_revision):
                return
            preview_only = _coerce_bool(
                payload.get("preview")
                if "preview" in payload
                else payload.get("previewOnly")
                if "previewOnly" in payload
                else payload.get("dryRun"),
                default=False,
            )
            working_session = json.loads(json.dumps(session)) if preview_only else session
            new_session = _mutate_retry_ai(working_session, payload)
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return

        response_revision = current_revision
        if preview_only:
            self._allow_session_files(collect_session_file_paths(new_session))
        else:
            committed, committed_revision = self._remember_session_if_current(
                base_session,
                new_session,
                expected_revision=current_revision,
            )
            if not committed:
                self._send_session_conflict()
                return
            response_revision = committed_revision
        response = {
            "ok": True,
            "session": rewrite_session_for_http(new_session),
            "retry": new_session.get("ai_retry_summary") or [],
            "preview": preview_only,
        }
        if response_revision is not None:
            response["sessionRevision"] = response_revision
        self._send_json(response)

    def _handle_session_classin_review(self) -> None:
        session, current_revision = self._current_session_state()
        if session is None:
            self._send_json({"ok": False, "error": "no session available"}, status=HTTPStatus.NOT_FOUND)
            return
        base_session = _clone_jsonish(session)
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not self._validate_expected_session_revision(payload, current_revision):
            return
        review = _apply_classin_review_result(session, payload)
        committed, committed_revision = self._remember_session_if_current(
            base_session,
            session,
            expected_revision=current_revision,
        )
        if not committed:
            self._send_session_conflict()
            return
        response = {
            "ok": True,
            "session": rewrite_session_for_http(session),
            "review": review,
            "classinReview": review,
            "classin_review": review,
            "history": _public_session_history(load_session_history()),
        }
        if committed_revision is not None:
            response["sessionRevision"] = committed_revision
        self._send_json(response)

    def _handle_session_enhance_image(self) -> None:
        session, current_revision = self._current_session_state()
        if session is None:
            self._send_json({"ok": False, "error": "no session available"}, status=HTTPStatus.NOT_FOUND)
            return
        base_session = _clone_jsonish(session)
        try:
            payload = self._read_json_body()
            if not self._validate_expected_session_revision(payload, current_revision):
                return
            new_session = _mutate_enhance_image(session, payload)
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
            return

        committed, committed_revision = self._remember_session_if_current(
            base_session,
            new_session,
            expected_revision=current_revision,
        )
        if not committed:
            self._send_session_conflict()
            return
        response = {
            "ok": True,
            "session": rewrite_session_for_http(new_session),
            "enhance": new_session.get("ai_image_reconstruction_summary") or [],
        }
        if committed_revision is not None:
            response["sessionRevision"] = committed_revision
        self._send_json(response)

    # ── /api/session/restore ────────────────────────────────────────────
    # Body: { "session": { ... full session JSON ... } }
    # Replaces the server's "latest" session with the provided snapshot. Used
    # by the front-end Undo stack so a single round-trip is enough to revert.
    def _handle_session_restore(self) -> None:
        base_session, current_revision = self._current_session_state()
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not self._validate_expected_session_revision(payload, current_revision):
            return
        snapshot = payload.get("session")
        if not isinstance(snapshot, dict) or "problems" not in snapshot:
            self._send_json(
                {"ok": False, "error": "session payload must be a dict containing 'problems'"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        # Strip the HTTP-friendly URLs that may already be in the snapshot —
        # the server stores file-system paths and re-derives /api/file URLs on
        # the way out. Snapshots that round-trip through rewrite_session_for_http
        # would otherwise drift over time.
        restored = _denormalize_session_paths(snapshot)
        _refresh_session_problem_counts(restored)
        committed, committed_revision = self._remember_session_if_current(
            base_session,
            restored,
            expected_revision=current_revision,
        )
        if not committed:
            self._send_session_conflict()
            return
        response = {"ok": True, "session": rewrite_session_for_http(restored)}
        if committed_revision is not None:
            response["sessionRevision"] = committed_revision
        self._send_json(response)

    def _handle_session_history(self) -> None:
        history = load_session_history()
        self._allow_session_files(collect_session_history_file_paths(history))
        self._send_json({
            "ok": True,
            "history": _public_session_history(history),
        })

    def _handle_session_history_restore(self) -> None:
        base_session, current_revision = self._current_session_state()
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        if not self._validate_expected_session_revision(payload, current_revision):
            return
        session_id = str(payload.get("id") or payload.get("sessionId") or payload.get("session_id") or "").strip()
        if not session_id:
            self._send_json({"ok": False, "error": "session history id is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        entry = next((item for item in load_session_history() if str(item.get("id")) == session_id), None)
        if not isinstance(entry, dict) or not isinstance(entry.get("session"), dict):
            self._send_json({"ok": False, "error": "session history entry not found"}, status=HTTPStatus.NOT_FOUND)
            return
        restored = _denormalize_session_paths(entry["session"])
        _refresh_session_problem_counts(restored)
        committed, committed_revision = self._remember_session_if_current(
            base_session,
            restored,
            expected_revision=current_revision,
        )
        if not committed:
            self._send_session_conflict()
            return
        response = {
            "ok": True,
            "session": rewrite_session_for_http(restored),
            "history": _public_session_history(load_session_history()),
        }
        if committed_revision is not None:
            response["sessionRevision"] = committed_revision
        self._send_json(response)

    def _handle_runtime_artifact_cleanup(self) -> None:
        if not _request_is_same_origin(self.headers):
            self._send_json(
                {"ok": False, "error": "cross-origin request rejected"},
                status=HTTPStatus.FORBIDDEN,
            )
            return
        try:
            payload = self._read_json_body()
            dry_run = _coerce_bool(payload.get("dryRun", payload.get("dry_run")), default=True)
            raw_min_age_seconds = payload.get("minAgeSeconds", payload.get("min_age_seconds"))
            raw_min_age_days = payload.get("minAgeDays", payload.get("min_age_days"))
            if raw_min_age_seconds is not None:
                min_age_seconds = float(raw_min_age_seconds)
            elif raw_min_age_days is not None:
                min_age_seconds = float(raw_min_age_days) * 86_400
            else:
                min_age_seconds = DEFAULT_ARTIFACT_RETENTION_DAYS * 86_400
            raw_max_bytes = payload.get("maxBytes", payload.get("max_bytes"))
            max_bytes = int(raw_max_bytes) if raw_max_bytes is not None else None
            cleaner = getattr(self.app_server, "cleanup_artifacts", None)
            if callable(cleaner):
                result = cleaner(
                    dry_run=dry_run,
                    min_age_seconds=min_age_seconds,
                    max_bytes=max_bytes,
                )
            else:
                result = cleanup_runtime_artifacts(
                    active_session=self._current_session_snapshot(),
                    history=load_session_history(),
                    dry_run=dry_run,
                    min_age_seconds=min_age_seconds,
                    max_bytes=max_bytes,
                )
        except ArtifactCleanupBusy as exc:
            self._send_json(
                {
                    "ok": False,
                    "code": "artifact_cleanup_busy",
                    "operation": "artifact_cleanup",
                    "retryable": True,
                    "error": str(exc),
                    "recoverySteps": ["진행 중인 파일 다운로드, 변환 또는 내보내기가 끝난 뒤 다시 시도해 주세요."],
                },
                status=HTTPStatus.CONFLICT,
            )
            return
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(result)

    def _handle_session_clear(self, parsed=None) -> None:
        params = parse_qs(parsed.query) if parsed is not None else {}
        _, current_revision = self._current_session_state()
        revision_payload = {
            "expectedSessionRevision": params.get(
                "expectedSessionRevision",
                params.get("expected_session_revision", [None]),
            )[0],
            "expectedSessionEpoch": params.get(
                "expectedSessionEpoch",
                params.get("expected_session_epoch", [None]),
            )[0],
        }
        if not self._validate_expected_session_revision(revision_payload, current_revision):
            return
        expected_revision = self._payload_session_revision(revision_payload)
        cleanup_requested = _coerce_bool(
            params.get("cleanupArtifacts", params.get("cleanup_artifacts", [False]))[0],
            default=False,
        )
        dry_run = _coerce_bool(params.get("dryRun", params.get("dry_run", [True]))[0], default=True)
        try:
            raw_min_age_days = params.get("minAgeDays", params.get("min_age_days", [DEFAULT_ARTIFACT_RETENTION_DAYS]))[0]
            min_age_seconds = float(raw_min_age_days) * 86_400
            clearer_with_revision = getattr(self.app_server, "clear_session_with_revision", None)
            clearer = getattr(self.app_server, "clear_session", None)
            cleared_revision: int | None = None
            if callable(clearer_with_revision):
                cleanup_result, cleared_revision = clearer_with_revision(
                    expected_revision=expected_revision,
                    cleanup_artifacts=cleanup_requested,
                    dry_run=dry_run,
                    min_age_seconds=min_age_seconds,
                )
            elif callable(clearer):
                clear_kwargs: dict[str, Any] = {}
                if isinstance(self.app_server, AppHTTPServer):
                    clear_kwargs["expected_revision"] = expected_revision
                if cleanup_requested:
                    cleanup_result = clearer(
                        cleanup_artifacts=True,
                        dry_run=dry_run,
                        min_age_seconds=min_age_seconds,
                        **clear_kwargs,
                    )
                else:
                    clearer(**clear_kwargs)
                    cleanup_result = None
            else:
                _atomically_clear_persisted_session_state()
                self.app_server.latest_session = None
                self.app_server.allowed_files = set()
                cleanup_result = (
                    cleanup_runtime_artifacts(
                        active_session={},
                        history=[],
                        dry_run=dry_run,
                        min_age_seconds=min_age_seconds,
                    )
                    if cleanup_requested
                    else None
                )
        except SessionRevisionConflict:
            self._send_session_conflict()
            return
        except ArtifactCleanupBusy as exc:
            current_session, busy_revision = self._current_session_state()
            busy_payload: dict[str, Any] = {
                "ok": False,
                "code": "artifact_cleanup_busy",
                "operation": "session_reset",
                "retryable": True,
                "artifactCleanupRequested": cleanup_requested,
                "error": str(exc),
                "session": rewrite_session_for_http(current_session)
                if isinstance(current_session, dict)
                else None,
                "recoverySteps": ["진행 중인 파일 다운로드, 변환 또는 내보내기가 끝난 뒤 다시 시도해 주세요."],
            }
            if busy_revision is not None:
                busy_payload["sessionRevision"] = busy_revision
            self._send_json(
                busy_payload,
                status=HTTPStatus.CONFLICT,
            )
            return
        except (OSError, TypeError, ValueError) as exc:
            self._send_json(
                {
                    "ok": False,
                    "code": "reset_failed",
                    "operation": "session_reset",
                    "retryable": True,
                    "error": f"failed to clear: {exc}",
                    "recoverySteps": [
                        "진행 중인 작업이 끝난 뒤 초기화를 다시 시도해 주세요.",
                        "계속 실패하면 최근 작업을 유지한 채 오류 정보를 신고해 주세요.",
                    ],
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        response: dict[str, Any] = {
            "ok": True,
            "history": [],
            "session": None,
            "artifactCleanupRequested": cleanup_requested,
            "artifactCleanupDryRun": dry_run if cleanup_requested else None,
            "artifactCleanupPerformed": bool(
                cleanup_requested and not dry_run and cleanup_result is not None
            ),
            "artifactCleanupSucceeded": (
                bool(cleanup_result.get("ok"))
                if cleanup_requested and isinstance(cleanup_result, dict)
                else None
            ),
        }
        if cleared_revision is not None:
            response["sessionRevision"] = cleared_revision
        if cleanup_result is not None:
            response["artifactCleanup"] = cleanup_result
        self._send_json(response)

    def _handle_open_folder(self) -> None:
        if not _request_is_same_origin(self.headers):
            self._send_json({"ok": False, "error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
            return
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        raw_path = payload.get("path") or payload.get("folder") or ""
        try:
            target = _resolve_open_target(raw_path, kind="folder")
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        except ValueError as exc:
            status = HTTPStatus.FORBIDDEN if "outside allowed roots" in str(exc) else HTTPStatus.BAD_REQUEST
            self._send_json({"ok": False, "error": str(exc)}, status=status)
            return
        try:
            _open_system_target(target)
        except OSError as exc:
            self._send_json({"ok": False, "error": f"failed to open: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "path": str(target)})

    def _handle_open_file(self) -> None:
        if not _request_is_same_origin(self.headers):
            self._send_json({"ok": False, "error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
            return
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        raw_path = payload.get("path") or payload.get("file") or ""
        try:
            target = _resolve_open_target(raw_path, kind="file")
        except FileNotFoundError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        except ValueError as exc:
            status = HTTPStatus.FORBIDDEN if "outside allowed roots" in str(exc) else HTTPStatus.BAD_REQUEST
            self._send_json({"ok": False, "error": str(exc)}, status=status)
            return
        try:
            _open_system_target(target)
        except OSError as exc:
            self._send_json({"ok": False, "error": f"failed to open: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "path": str(target)})

    def _handle_open_url(self) -> None:
        if not _request_is_same_origin(self.headers):
            self._send_json({"ok": False, "error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
            return
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        url = _normalize_update_url(payload.get("url"))
        if not url:
            self._send_json({"ok": False, "error": "trusted HTTPS URL is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        if url not in _allowed_update_urls():
            self._send_json({"ok": False, "error": "URL is not in the configured update metadata"}, status=HTTPStatus.FORBIDDEN)
            return
        try:
            opened = webbrowser.open(url)
        except Exception as exc:
            self._send_json({"ok": False, "error": f"failed to open URL: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "url": url, "opened": bool(opened)})

    def _handle_shutdown(self) -> None:
        if not _request_is_same_origin(self.headers):
            self._send_json({"ok": False, "error": "cross-origin request rejected"}, status=HTTPStatus.FORBIDDEN)
            return
        self._send_json({"ok": True})
        threading.Thread(target=self.app_server.shutdown, name="app-shutdown", daemon=True).start()

    def _handle_user_settings_get(self) -> None:
        self._send_json(
            {"ok": True, "settings": summarize_user_settings(RUNTIME_DIR)}
        )

    def _handle_user_settings_post(self) -> None:
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json({"ok": False, "error": f"invalid JSON: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return
        has_gemini_key = "geminiApiKey" in payload or "gemini_api_key" in payload
        raw_key = payload.get("geminiApiKey") if "geminiApiKey" in payload else payload.get("gemini_api_key")
        has_openai_key = "openAiApiKey" in payload or "openai_api_key" in payload
        raw_openai_key = payload.get("openAiApiKey") if "openAiApiKey" in payload else payload.get("openai_api_key")
        has_ai_enabled = "aiEnabled" in payload or "ai_enabled" in payload
        raw_ai_enabled = payload.get("aiEnabled") if "aiEnabled" in payload else payload.get("ai_enabled")
        try:
            summary = update_api_keys(
                RUNTIME_DIR,
                gemini_api_key=(raw_key if isinstance(raw_key, str) else "") if has_gemini_key else None,
                openai_api_key=(raw_openai_key if isinstance(raw_openai_key, str) else "") if has_openai_key else None,
                ai_enabled=_coerce_bool(raw_ai_enabled) if has_ai_enabled else None,
            )
        except OSError as exc:
            self._send_json({"ok": False, "error": f"failed to persist settings: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send_json({"ok": True, "settings": summary})

    def _handle_bug_report(self) -> None:
        try:
            request_payload = self._read_json_body()
        except json.JSONDecodeError as exc:
            self._send_json(
                {"ok": False, "error": f"invalid JSON: {exc}"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        app_config = load_app_update_config()
        try:
            report = build_bug_report(
                request_payload,
                app_config=app_config,
                log_file=APP_LOG_FILE,
            )
        except BugReportValidationError as exc:
            self._send_json(
                {"ok": False, "error": str(exc), "code": "invalid_bug_report"},
                status=HTTPStatus.BAD_REQUEST,
            )
            return
        endpoint = str(app_config.get("bugReportUrl") or DEFAULT_BUG_REPORT_URL).strip()
        try:
            receipt = deliver_bug_report(report, endpoint=endpoint)
        except BugReportDeliveryError as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "code": "bug_report_delivery_failed",
                    "recoverySteps": [
                        "인터넷 연결을 확인한 뒤 다시 시도해 주세요.",
                        "계속 실패하면 앱 로그와 함께 관리자에게 알려 주세요.",
                    ],
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
            return
        print(f"[bug-report] accepted as {receipt['reportId']}")
        self._send_json(receipt, status=HTTPStatus.CREATED)

    def _read_json_body(self) -> dict[str, Any]:
        raw_content_length = self.headers.get("Content-Length", "0")
        try:
            content_length = int(raw_content_length)
        except (TypeError, ValueError) as exc:
            raise json.JSONDecodeError("invalid Content-Length", str(raw_content_length), 0) from exc
        if content_length < 0:
            raise json.JSONDecodeError("invalid Content-Length", str(raw_content_length), 0)
        if content_length > MAX_JSON_BODY_BYTES:
            raise RequestPayloadTooLarge(content_length, MAX_JSON_BODY_BYTES)
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))

    def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        response_payload = payload
        if "session" in payload and "sessionEpoch" not in payload:
            epoch_getter = getattr(getattr(self, "server", None), "session_epoch", None)
            if callable(epoch_getter):
                epoch = str(epoch_getter() or "").strip()
                if epoch:
                    response_payload = dict(payload)
                    response_payload["sessionEpoch"] = epoch
        body = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(
        self,
        payload: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_latest_session(self) -> None:
        session, current_revision = self._current_session_state()
        if session is None:
            response: dict[str, Any] = {"ok": True, "session": None}
            if current_revision is not None:
                response["sessionRevision"] = current_revision
            self._send_json(response)
            return
        _refresh_session_problem_counts(session)
        registrar = getattr(self.app_server, "register_current_session", None)
        if callable(registrar):
            if not registrar(session):
                session, current_revision = self._current_session_state()
                if session is None:
                    response = {"ok": True, "session": None}
                    if current_revision is not None:
                        response["sessionRevision"] = current_revision
                    self._send_json(response)
                    return
                _refresh_session_problem_counts(session)
        else:
            self._allow_session_files(collect_session_file_paths(session))
            if not LATEST_SESSION_JSON.exists():
                save_latest_session(session)
            remember_session_history(session)
        response = {"ok": True, "session": rewrite_session_for_http(session)}
        if current_revision is not None:
            response["sessionRevision"] = current_revision
        self._send_json(response)

    def _handle_file(self, parsed) -> None:
        query = parse_qs(parsed.query)
        requested = query.get("path", [None])[0]
        path = decode_file_reference(requested)
        if path is None or not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "file not found")
            return

        normalized = str(path.resolve())
        if not self._file_is_allowed(normalized):
            self.send_error(HTTPStatus.FORBIDDEN, "file not allowed")
            return

        mime_type, _ = mimetypes.guess_type(path.name)
        attachment_suffixes = {".edb", ".zip"}
        if path.suffix.lower() == ".edb":
            mime_type = "application/octet-stream"
        elif path.suffix.lower() == ".zip":
            mime_type = "application/zip"

        file_stat = path.stat()
        file_size = file_stat.st_size
        preview_max_dimension = _parse_file_preview_max_dimension(query)
        if preview_max_dimension is not None and str(mime_type or "").startswith("image/"):
            try:
                preview_payload = _build_file_preview_payload(
                    normalized,
                    file_stat.st_mtime_ns,
                    file_size,
                    preview_max_dimension,
                )
            except (OSError, ValueError):
                preview_payload = None
            if preview_payload is not None:
                payload, preview_mime_type = preview_payload
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", preview_mime_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.send_header("X-Preview-Max-Dimension", str(preview_max_dimension))
                self.end_headers()
                self.wfile.write(payload)
                return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(file_size))
        if path.suffix.lower() in attachment_suffixes:
            self.send_header("Content-Disposition", content_disposition_attachment(path.name))
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=1024 * 1024)

    def _save_uploaded_file(self, payload: dict[str, Any]) -> Path:
        file_name = payload.get("fileName") or "upload.bin"
        file_data_base64 = payload.get("fileDataBase64")
        if not file_data_base64:
            raise ValueError("fileDataBase64 is required when sourcePath is not provided")
        safe_name = sanitize_upload_file_name(file_name)
        try:
            file_bytes = base64.b64decode(file_data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("fileDataBase64 is not valid base64") from exc
        content_digest = hashlib.sha256(file_bytes).hexdigest()
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(safe_name).suffix
        for candidate in sorted(UPLOAD_DIR.glob(f"{content_digest}_*{suffix}")):
            if _file_matches_digest(candidate, content_digest, len(file_bytes)):
                return candidate
        # Older sessions may still reference SHA-1-prefixed cache files. Keep
        # those files in place, but every new upload is materialized under a
        # SHA-256 identity so a chosen-prefix SHA-1 collision cannot alias it.
        target_path = UPLOAD_DIR / f"{content_digest}_{safe_name}"
        _atomic_write_bytes(target_path, file_bytes)
        return target_path

    def _resolve_source_paths(
        self,
        payload: dict[str, Any],
        *,
        session: dict[str, Any] | None = None,
    ) -> list[Path]:
        reuse_session_sources = _coerce_bool(
            payload.get("reuseSessionSources")
            if "reuseSessionSources" in payload
            else payload.get("reuse_session_sources"),
            default=False,
        )
        if reuse_session_sources:
            resolved_paths = _session_reextract_source_paths(session)
            if not resolved_paths:
                raise FileNotFoundError("current session has no preserved source files available for re-extraction")
            return resolved_paths

        file_payloads = payload.get("files")
        if isinstance(file_payloads, list) and file_payloads:
            resolved_paths: list[Path] = []
            for file_payload in file_payloads:
                if isinstance(file_payload, dict):
                    resolved_paths.append(self._save_uploaded_file(file_payload).resolve())
                    continue
                path = decode_file_reference(str(file_payload))
                if path is None:
                    raise FileNotFoundError(f"sourcePath does not exist: {file_payload}")
                if not path.exists():
                    raise FileNotFoundError(f"sourcePath does not exist: {path}")
                resolved_paths.append(path.resolve())
            return resolved_paths

        source_paths = payload.get("sources") or payload.get("sourcePaths")
        if isinstance(source_paths, list) and source_paths:
            resolved_paths: list[Path] = []
            for source_path in source_paths:
                path = decode_file_reference(str(source_path))
                if path is None:
                    raise FileNotFoundError(f"sourcePath does not exist: {source_path}")
                if not path.exists():
                    raise FileNotFoundError(f"sourcePath does not exist: {path}")
                resolved_paths.append(path.resolve())
            return resolved_paths

        source_path = payload.get("source") or payload.get("sourcePath") or payload.get("source_path")
        if source_path:
            path = decode_file_reference(str(source_path))
            if path is None:
                raise FileNotFoundError(f"sourcePath does not exist: {source_path}")
            if not path.exists():
                raise FileNotFoundError(f"sourcePath does not exist: {path}")
            return [path.resolve()]
        return [self._save_uploaded_file(payload).resolve()]

    def _resolve_output_dir(self, payload: dict[str, Any], source_paths: list[Path]) -> Path:
        requested = payload.get("output_dir") or payload.get("outputDir")
        output_root = default_output_root()
        if requested:
            target = Path(str(requested))
            if not target.is_absolute():
                target = output_root / sanitize_output_dir_name(str(requested))
            return target.resolve()
        if not source_paths:
            return (output_root / sanitize_output_dir_name(None)).resolve()
        identity_suffix = _source_identity_suffix(source_paths)
        if len(source_paths) == 1:
            return (
                output_root
                / sanitize_output_dir_name(source_paths[0].stem, suffix=identity_suffix)
            ).resolve()
        batch_name = f"{source_paths[0].stem}_{len(source_paths)}files"
        return (
            output_root / sanitize_output_dir_name(batch_name, suffix=identity_suffix)
        ).resolve()

    def _resolve_preview_output_dir(self, payload: dict[str, Any], source_paths: list[Path]) -> Path:
        """Keep speculative exports immutable and separate from active output.

        A preview may later be adopted through session restore, but it must not
        touch the deterministic directory referenced by the current session.
        Unadopted previews remain under the managed runtime root and are
        eligible for the normal retention cleanup.
        """
        requested = payload.get("output_dir") or payload.get("outputDir")
        if requested:
            name_hint = Path(str(requested)).name
        elif len(source_paths) == 1:
            name_hint = source_paths[0].stem
        elif source_paths:
            name_hint = f"{source_paths[0].stem}_{len(source_paths)}files"
        else:
            name_hint = "preview"
        run_name = sanitize_output_dir_name(name_hint, suffix=_unique_artifact_stamp())
        return (default_output_root() / "previews" / run_name).resolve()

    def _handle_export(self) -> None:
        base_session, current_revision = self._current_session_state()
        export_staging_dir: Path | None = None
        export_final_dir: Path | None = None
        export_working_dir: Path | None = None
        try:
            payload = self._read_json_body()
            preview_only = _coerce_bool(
                payload.get("preview")
                if "preview" in payload
                else payload.get("previewOnly")
                if "previewOnly" in payload
                else payload.get("dryRun"),
                default=False,
            )
            reuse_session_sources = _coerce_bool(
                payload.get("reuseSessionSources")
                if "reuseSessionSources" in payload
                else payload.get("reuse_session_sources"),
                default=False,
            )
            if reuse_session_sources and not preview_only:
                raise ValueError("reuseSessionSources is only allowed for preview exports")
            if (not preview_only or reuse_session_sources) and not self._validate_expected_session_revision(
                payload,
                current_revision,
            ):
                return
            source_paths = self._resolve_source_paths(payload, session=base_session)
            if preview_only:
                output_dir = self._resolve_preview_output_dir(payload, source_paths)
            else:
                export_working_dir = self._resolve_output_dir(payload, source_paths)
                generation_name = sanitize_output_dir_name(
                    "export",
                    suffix=_unique_artifact_stamp(),
                )
                export_staging_dir = export_working_dir / ".export-staging" / generation_name
                export_final_dir = export_working_dir / "exports" / generation_name
                export_staging_dir.mkdir(parents=True, exist_ok=False)
                output_dir = export_staging_dir
            export_mode = str(payload.get("exportMode") or payload.get("export_mode") or payload.get("layoutMode") or "question").lower()
            input_intent = _extract_input_intent(payload)
            content_target = _extract_content_target(payload)
            input_notes = _extract_input_notes(payload)
            crop_format = _extract_crop_format(payload)
            detect_perspective = _coerce_bool(payload.get("detectPerspective") if "detectPerspective" in payload else payload.get("detect_perspective"))
            skip_deskew = _coerce_bool(payload.get("skipDeskew") if "skipDeskew" in payload else payload.get("skip_deskew"))
            skip_crop = _coerce_bool(payload.get("skipCrop") if "skipCrop" in payload else payload.get("skip_crop"))
            if input_intent == "page-as-is":
                detect_perspective = False
                skip_deskew = True
                skip_crop = True
            requested_max_dimension = (
                int(payload["maxDimension"])
                if payload.get("maxDimension")
                else int(payload["max_dimension"])
                if payload.get("max_dimension")
                else DEFAULT_RECOGNITION_MAX_DIMENSION
            )
            requested_pdf_dpi = int(payload.get("pdfDpi") or payload.get("pdf_dpi") or 200)
            # A page-as-is record is itself the display master. Never shrink it
            # after the document render, even if an older client still submits
            # the generic recognition limit or a lower PDF DPI.
            max_dimension = None if input_intent == "page-as-is" else requested_max_dimension
            pdf_dpi = max(200, requested_pdf_dpi) if input_intent == "page-as-is" else requested_pdf_dpi
            common_kwargs = {
                "output_dir": output_dir,
                "subject_name": str(payload.get("subject") or "unknown"),
                "ocr": str(payload.get("ocr") or "auto"),
                "pdf_dpi": pdf_dpi,
                "detect_perspective": detect_perspective,
                "skip_deskew": skip_deskew,
                "skip_crop": skip_crop,
                "max_dimension": max_dimension,
                "export_edb": _coerce_bool(payload.get("export_edb") if "export_edb" in payload else payload.get("exportEdb"), default=True),
                "edb_name": sanitize_edb_file_name(
                    str(payload.get("edbName") or payload.get("edb_name"))
                    if (payload.get("edbName") or payload.get("edb_name")) is not None
                    else None,
                    fallback_stem="mvp_board",
                ),
                "sync_ui": False,
            }
            common_kwargs.update(_extract_ai_fallback_kwargs(payload))
            ai_enabled = _global_ai_enabled()
            if not ai_enabled:
                requested_ocr = str(common_kwargs.get("ocr") or "auto").strip().lower()
                if requested_ocr not in {"none", "off", "disabled"}:
                    common_kwargs["ocr"] = "local"
                common_kwargs["ai_fallback_enabled"] = False
                common_kwargs["ai_fallback"] = "off"
            if export_mode == "page":
                result = run_export(
                    source_paths[0] if len(source_paths) == 1 else source_paths,
                    **common_kwargs,
                )
            else:
                result = run_problem_export(
                    source_paths[0] if len(source_paths) == 1 else source_paths,
                    record_mode=str(payload.get("recordMode") or payload.get("record_mode") or "image-only"),
                    text_confidence_threshold=float(payload.get("textConfidenceThreshold") or payload.get("text_confidence_threshold") or 0.78),
                    crop_format=crop_format,
                    input_intent=input_intent,
                    page_tile_mode=str(
                        payload.get("pageTileMode")
                        or payload.get("page_tile_mode")
                        or "off"
                    ),
                    content_target=content_target,
                    input_notes=input_notes,
                    **common_kwargs,
                )
            if isinstance(result, dict):
                result["ai_enabled"] = ai_enabled
                result["aiEnabled"] = ai_enabled
        except RequestPayloadTooLarge:
            if export_staging_dir is not None:
                _discard_staged_publish(export_staging_dir)
            raise
        except Exception as exc:
            if export_staging_dir is not None:
                _discard_staged_publish(export_staging_dir)
            self._send_json(_export_error_payload(exc), status=HTTPStatus.BAD_REQUEST)
            return

        session = result["ui_session"]
        session["ai_enabled"] = ai_enabled
        session["aiEnabled"] = ai_enabled
        session.setdefault("input_intent", input_intent)
        session.setdefault("content_target", content_target)
        session.setdefault("contentTarget", content_target)
        if input_notes:
            session["input_notes"] = input_notes
        _refresh_session_problem_counts(session)
        edb_validation = None
        edb_path = result.get("edb_path")
        raw_edb_parts = result.get("edb_parts") if isinstance(result.get("edb_parts"), list) else []
        normalized_parts: list[dict[str, Any]] = []
        if raw_edb_parts:
            try:
                edb_validation, normalized_parts = _validate_edb_parts(raw_edb_parts)
                result["edb_parts"] = normalized_parts
                result["edb_paths"] = [Path(str(part["edbPath"])) for part in normalized_parts]
            except Exception as exc:
                if export_staging_dir is not None:
                    _discard_staged_publish(export_staging_dir)
                self._send_json({"ok": False, "error": f"EDB validation failed: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
        elif edb_path:
            try:
                expected_records = len((result.get("summary") or {}).get("placements") or [])
                edb_validation, normalized_parts = _validate_edb_parts([
                    {
                        "partIndex": 1,
                        "partCount": 1,
                        "edbPath": str(edb_path),
                        "recordCount": max(1, expected_records),
                    }
                ])
                result["edb_parts"] = normalized_parts
                result["edb_paths"] = [Path(str(part["edbPath"])) for part in normalized_parts]
            except Exception as exc:
                if export_staging_dir is not None:
                    _discard_staged_publish(export_staging_dir)
                self._send_json({"ok": False, "error": f"EDB validation failed: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
        if export_staging_dir is not None and export_final_dir is not None:
            result = _remap_artifact_paths(result, export_staging_dir, export_final_dir)
            edb_validation = _remap_artifact_paths(
                edb_validation,
                export_staging_dir,
                export_final_dir,
            )
            normalized_parts = result.get("edb_parts") or []
            session = result["ui_session"]
            if export_working_dir is not None:
                session["output_dir"] = str(export_working_dir)
                session["outputDir"] = str(export_working_dir)
            result["ui_session"] = session
            try:
                _rewrite_staged_artifact_references(export_staging_dir, export_final_dir)
                final_ui_session_path = Path(str(result["ui_session_path"]))
                staged_ui_session_path = export_staging_dir / final_ui_session_path.relative_to(
                    export_final_dir
                )
                _atomic_write_text(
                    staged_ui_session_path,
                    json.dumps(session, ensure_ascii=False, indent=2),
                )
            except Exception as exc:  # noqa: BLE001
                _discard_staged_publish(export_staging_dir)
                _log_operation_exception("session_export.rewrite_generation", exc)
                self._send_json(
                    _export_error_payload(exc),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
        if normalized_parts:
            session["edb_parts"] = normalized_parts
            session["edbParts"] = normalized_parts
            session["edb_part_count"] = len(normalized_parts)
            session["edbPartCount"] = len(normalized_parts)
            session["edb_split"] = len(normalized_parts) > 1
            session["edbSplit"] = len(normalized_parts) > 1
            _annotate_session_with_edb_part_metadata(session, normalized_parts)
        response_revision = current_revision
        if preview_only:
            self._allow_session_files(collect_session_file_paths(session))
        else:
            if export_staging_dir is None or export_final_dir is None:
                self._send_json(
                    _export_error_payload(RuntimeError("export generation paths are missing")),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            try:
                committed, committed_revision = self._adopt_staged_publish_if_current(
                    base_session,
                    session,
                    staging_dir=export_staging_dir,
                    final_dir=export_final_dir,
                    expected_revision=current_revision,
                )
            except Exception as exc:  # noqa: BLE001
                _discard_staged_publish(export_staging_dir)
                _log_operation_exception("session_export.commit_generation", exc)
                self._send_json(
                    _export_error_payload(exc),
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            if not committed:
                _discard_staged_publish(export_staging_dir)
                self._send_session_conflict()
                return
            response_revision = committed_revision
        classin_preflight = session.get("classinPreflight")
        if not isinstance(classin_preflight, dict):
            classin_preflight = session.get("classin_preflight")
        if not isinstance(classin_preflight, dict):
            classin_preflight = {}
        classin_preflight_issue_count = int(
            classin_preflight.get("issueCount") or classin_preflight.get("issue_count") or 0
        )
        classin_preflight_passed = bool(classin_preflight.get("passed")) if classin_preflight else False
        classin_preflight_status = str(classin_preflight.get("status") or "")
        response = {
                "ok": True,
                "session": rewrite_session_for_http(session),
                "preview": preview_only,
                "output_dir": str(result["output_dir"]),
                "outputDir": str(result["output_dir"]),
                "ui_session_path": str(result["ui_session_path"]),
                "uiSessionPath": str(result["ui_session_path"]),
                "edb_path": str(result["edb_path"]) if result["edb_path"] else None,
                "edbPath": str(result["edb_path"]) if result["edb_path"] else None,
                "edb_paths": [str(path) for path in (result.get("edb_paths") or [])],
                "edbPaths": [str(path) for path in (result.get("edb_paths") or [])],
                "edb_parts": result.get("edb_parts") or [],
                "edbParts": result.get("edb_parts") or [],
                "edb_part_count": len(result.get("edb_parts") or []),
                "edbPartCount": len(result.get("edb_parts") or []),
                "edb_split": len(result.get("edb_parts") or []) > 1,
                "edbSplit": len(result.get("edb_parts") or []) > 1,
                "edb_validation": edb_validation,
                "edbValidation": edb_validation,
                "classin_preflight": classin_preflight,
                "classinPreflight": classin_preflight,
                "classin_preflight_status": classin_preflight_status,
                "classinPreflightStatus": classin_preflight_status,
                "classin_preflight_passed": classin_preflight_passed,
                "classinPreflightPassed": classin_preflight_passed,
                "classin_preflight_issue_count": classin_preflight_issue_count,
                "classinPreflightIssueCount": classin_preflight_issue_count,
                "export_mode": session.get("export_mode"),
                "exportMode": session.get("export_mode"),
            }
        if response_revision is not None:
            response["sessionRevision"] = response_revision
        self._send_json(response)


def _run_startup_artifact_cleanup(server: AppHTTPServer) -> dict[str, Any] | None:
    # Deletion is deliberately stricter than general boolean settings: only
    # an explicit administrator opt-in of exactly ``1`` may mutate artifacts
    # during startup. Any other value remains a non-destructive audit.
    delete_enabled = str(os.environ.get("EDB_AUTO_ARTIFACT_CLEANUP") or "").strip() == "1"
    try:
        result = server.cleanup_artifacts(
            dry_run=not delete_enabled,
            min_age_seconds=DEFAULT_ARTIFACT_RETENTION_DAYS * 86_400,
        )
    except Exception as exc:  # startup cleanup is maintenance, never availability-critical
        print(f"[app-server] startup artifact cleanup skipped: {exc}", file=sys.stderr)
        return None
    print(
        "[app-server] startup artifact cleanup "
        f"({'delete' if delete_enabled else 'dry-run'}): "
        f"selected={result.get('selectedFileCount', 0)} "
        f"deleted={result.get('deletedFileCount', 0)} freed={result.get('deletedBytes', 0)}",
        file=sys.stderr,
    )
    return result


def run_server(*, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    ensure_runtime_dirs()
    hydrate_user_settings_env()
    write_placeholder_generated_session()
    url = f"http://{host}:{port}/"
    if _local_server_is_healthy(host, port):
        print(f"{APP_NAME} already running at {url}")
        if open_browser:
            webbrowser.open(url)
        return
    handler = partial(AppRequestHandler)
    try:
        server = AppHTTPServer((host, port), handler)
    except OSError as exc:
        address_in_use = exc.errno in {errno.EADDRINUSE, 48, 98, 10048}
        if address_in_use and _local_server_is_healthy(host, port):
            print(f"{APP_NAME} already running at {url}")
            if open_browser:
                webbrowser.open(url)
            return
        raise
    _run_startup_artifact_cleanup(server)
    print(f"{APP_NAME} running at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local MVP app server for the ClassIn EDB builder.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--open-browser", dest="open_browser", action="store_true", default=None, help="Open the app in the default browser")
    parser.add_argument("--no-open-browser", dest="open_browser", action="store_false", help="Do not open the default browser")
    parser.add_argument("--log-file", default="", help="Write stdout/stderr to this file")
    args = parser.parse_args()
    ensure_runtime_dirs()
    if args.log_file or is_frozen_app():
        configure_app_logging(args.log_file or None)
    open_browser = is_frozen_app() if args.open_browser is None else bool(args.open_browser)
    run_server(host=args.host, port=args.port, open_browser=open_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
