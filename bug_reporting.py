#!/usr/bin/env python3
"""Privacy-preserving bug report construction and delivery.

Reports intentionally exclude source documents, session data, API keys, and
full local paths.  The UI supplies a small allowlisted context object; this
module adds bounded, redacted runtime diagnostics before forwarding it to the
remote report collector.
"""
from __future__ import annotations

import codecs
import json
import locale
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_BUG_REPORT_URL = "https://reports.classin.cloud/v1/edb-reports"
MAX_DESCRIPTION_CHARS = 4_000
MAX_CONTACT_CHARS = 240
MAX_LOG_CHARS = 24_000
MAX_REMOTE_RESPONSE_BYTES = 64_000
MAX_RUNTIME_ERRORS = 10
REPORT_TIMEOUT_SECONDS = 8.0

_SECRET_PATTERNS = (
    re.compile(r"\bAIza[0-9A-Za-z_-]{16,}\b"),
    re.compile(r"\bsk-[0-9A-Za-z_-]{16,}\b"),
    re.compile(
        r"(?i)\b(api[_ -]?key|authorization|bearer|password|secret|token)"
        r"(\s*[:=]\s*)([^\s,;\"']+)"
    ),
)
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_WEB_URL_START_PATTERN = re.compile(r"(?i)\bhttps?://")
_DOCUMENT_EXTENSION = r"(?:pdf|hwp|hwpx|png|jpe?g|webp|bmp|tiff?)"
_KNOWN_POSIX_PATH_ROOT = r"/(?:Users|home|private|var|tmp|Volumes|opt|mnt|media|srv)(?:/|(?=$))"
_STRONG_LOCAL_PATH_ROOT = (
    r"(?:file://|[A-Z]:[\\/]|\\\\(?:\?\\)?|//|" + _KNOWN_POSIX_PATH_ROOT + r")"
)
_LOCAL_DOCUMENT_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9:/\\])"
    + _STRONG_LOCAL_PATH_ROOT
    + r"[^\r\n\"'<>`|]*?\."
    + _DOCUMENT_EXTENSION
    + r"\b"
)
_GENERIC_POSIX_DOCUMENT_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9:/\\=?#&])/"
    r"[^\r\n\"'<>`|]*?\."
    + _DOCUMENT_EXTENSION
    + r"\b"
)
_KNOWN_LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9:/\\])"
    + _STRONG_LOCAL_PATH_ROOT
    + r"[^\r\n\"'<>`|]*"
)
_DOCUMENT_NAME_PATTERN = re.compile(
    r"(?i)(?<![/\\])\b[^\s\"'<>/\\]+"
    + r"\."
    + _DOCUMENT_EXTENSION
    + r"\b"
)
_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)^(?:api[_ -]?key|authorization|bearer|password|secret|token)$"
)


def _decoded_url_component(value: str) -> str:
    """Decode nested URL escaping enough to expose encoded local references."""

    decoded = value
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _contains_local_path(value: str) -> bool:
    return bool(
        _LOCAL_DOCUMENT_PATH_PATTERN.search(value)
        or _GENERIC_POSIX_DOCUMENT_PATH_PATTERN.search(value)
        or _KNOWN_LOCAL_PATH_PATTERN.search(value)
    )


def _sensitive_url_component_marker(value: str, *, include_document_name: bool) -> str | None:
    decoded = _decoded_url_component(value)
    if any(pattern.search(decoded) for pattern in _SECRET_PATTERNS):
        return "[redacted-secret]"
    if _EMAIL_PATTERN.search(decoded):
        return "[redacted-email]"
    if _contains_local_path(decoded):
        return "[local-path]"
    if include_document_name and _DOCUMENT_NAME_PATTERN.search(decoded):
        return "[document]"
    return None


def _redact_url_parameters(component: str) -> str:
    """Preserve parameter names/delimiters while replacing sensitive values."""

    ambiguous_tail = _ambiguous_local_parameter_tail(component)
    if ambiguous_tail is not None:
        component = component[:ambiguous_tail]
    parts = re.split(r"([&;])", component)
    for index in range(0, len(parts), 2):
        part = parts[index]
        if not part:
            continue
        key, separator, value = part.partition("=")
        decoded_key = _decoded_url_component(key).strip()
        key_marker = _sensitive_url_component_marker(key, include_document_name=True)
        marker = None
        if separator and _SECRET_FIELD_PATTERN.fullmatch(decoded_key):
            marker = "[redacted-secret]"
        if marker is None:
            marker = _sensitive_url_component_marker(
                value if separator else key,
                include_document_name=True,
            )
        if separator and (key_marker or marker):
            parts[index] = f"{key_marker or key}={marker or value}"
        elif marker:
            parts[index] = marker
    return "".join(parts)


def _parameter_value_has_local_path(part: str) -> bool:
    _key, separator, value = part.partition("=")
    return bool(separator and _contains_local_path(_decoded_url_component(value)))


def _ambiguous_local_parameter_tail(component: str) -> int | None:
    """Find a raw delimiter that is more likely part of a local path value.

    A syntactically clear ``&mode=retry`` remains a parameter.  Once a strong
    local value is followed by ``&`` plus free-form text, privacy wins and the
    ambiguous remainder is dropped rather than risking a student/path suffix.
    """

    part_start = 0
    local_value_seen = False
    for delimiter in re.finditer(r"[&;]", component):
        current = component[part_start:delimiter.start()]
        tail = component[delimiter.end():]
        clear_parameter = re.match(r"[A-Za-z][A-Za-z0-9_.-]*=", tail) is not None
        local_value_seen = local_value_seen or _parameter_value_has_local_path(current)
        if local_value_seen and not clear_parameter:
            return delimiter.start()
        part_start = delimiter.end()
    return None


def _query_has_local_path(component: str) -> bool:
    return any(_parameter_value_has_local_path(part) for part in re.split(r"[&;]", component))


def _redact_url_path(path: str) -> str:
    segments = path.split("/")
    for index, segment in enumerate(segments):
        marker = _sensitive_url_component_marker(segment, include_document_name=False)
        if marker:
            segments[index] = marker
    return "/".join(segments)


def _redact_web_url(value: str) -> str:
    """Keep the remote location useful but scrub query/fragment identifiers."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = f"[redacted-userinfo]@{netloc.rsplit('@', 1)[1]}"
    path = _redact_url_path(parsed.path)
    query = _redact_url_parameters(parsed.query)
    fragment = (
        "[local-path]"
        if (
            parsed.fragment
            and _query_has_local_path(parsed.query)
            and "=" not in parsed.fragment
        )
        else _redact_url_parameters(parsed.fragment)
    )
    return urlunsplit((parsed.scheme, netloc, path, query, fragment))


def _url_tail_has_strong_local_root(value: str) -> bool:
    if "?" not in value and "#" not in value:
        return False
    tail = re.split(r"[?&#=]", value)[-1]
    decoded = _decoded_url_component(tail).lstrip()
    return re.match(r"(?i)^" + _STRONG_LOCAL_PATH_ROOT, decoded) is not None


def _url_has_local_parameter_value(value: str) -> bool:
    components: list[str] = []
    if "?" in value:
        components.append(value.split("?", 1)[1].split("#", 1)[0])
    if "#" in value:
        components.append(value.split("#", 1)[1])
    return any(_query_has_local_path(component) for component in components)


def _protect_web_urls(value: str) -> tuple[str, list[str]]:
    """Replace URLs with markers before standalone path processing.

    Spaces are not valid in an ordinary URL, but diagnostics sometimes contain
    an unescaped local path in a query value.  Continue across such spaces only
    while the current query/fragment value starts with a strong local root.
    """

    protected: list[str] = []
    parts: list[str] = []
    cursor = 0
    for match in _WEB_URL_START_PATTERN.finditer(value):
        if match.start() < cursor:
            continue
        end = match.end()
        while end < len(value):
            character = value[end]
            if character in "\r\n\"'<>`|":
                break
            if character.isspace():
                current_url = value[match.start():end]
                if not (
                    _url_tail_has_strong_local_root(current_url)
                    or _url_has_local_parameter_value(current_url)
                ):
                    break
            end += 1
        parts.append(value[cursor:match.start()])
        marker = f"\x00edb-web-url-{len(protected)}\x00"
        protected.append(_redact_web_url(value[match.start():end]))
        parts.append(marker)
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts), protected


class BugReportValidationError(ValueError):
    """Raised when a local report request is not safe or useful."""


class BugReportDeliveryError(RuntimeError):
    """Raised when the remote report collector cannot accept a report."""


def _redact_filesystem_references(value: str) -> str:
    """Remove local paths/documents without mistaking web URLs for files."""

    # Protect and sanitize complete URLs first so standalone path rules cannot
    # damage their scheme, host, or normal remote path.
    text, protected_urls = _protect_web_urls(value)
    text = _LOCAL_DOCUMENT_PATH_PATTERN.sub("[local-path]", text)
    text = _GENERIC_POSIX_DOCUMENT_PATH_PATTERN.sub("[local-path]", text)
    text = _KNOWN_LOCAL_PATH_PATTERN.sub("[local-path]", text)
    text = _DOCUMENT_NAME_PATTERN.sub("[document]", text)
    for index, url in enumerate(protected_urls):
        text = text.replace(f"\x00edb-web-url-{index}\x00", url)
    return text


def redact_sensitive_text(value: Any) -> str:
    """Return bounded text with common secrets and local identifiers removed."""

    # Sanitize complete URLs before generic email/secret replacement can make
    # their authority unparsable (for example user:password@host).  The URL
    # sanitizer also decodes nested escaping in path/query/fragment values.
    text = _redact_filesystem_references(str(value or ""))
    for pattern in _SECRET_PATTERNS[:2]:
        text = pattern.sub("[redacted-secret]", text)
    text = _SECRET_PATTERNS[2].sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted-secret]",
        text,
    )
    text = _EMAIL_PATTERN.sub("[redacted-email]", text)
    home = str(Path.home())
    if home:
        home_variants = {home, home.replace("\\", "/"), home.replace("/", "\\")}
        for variant in sorted(home_variants, key=len, reverse=True):
            text = re.sub(re.escape(variant), "[home]", text, flags=re.IGNORECASE)
    return text


def _bounded_text(value: Any, limit: int) -> str:
    return redact_sensitive_text(value).strip()[:limit]


def _safe_contact(value: Any) -> str:
    """Preserve an explicitly consented reply address while removing secrets."""

    text = _redact_filesystem_references(
        str(value or "").replace("\r", " ").replace("\n", " ").strip()
    )
    for pattern in _SECRET_PATTERNS[:2]:
        text = pattern.sub("[redacted-secret]", text)
    text = _SECRET_PATTERNS[2].sub(
        lambda match: f"{match.group(1)}{match.group(2)}[redacted-secret]",
        text,
    )
    return " ".join(text.split())[:MAX_CONTACT_CHARS]


def _safe_runtime_errors(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    safe: list[dict[str, Any]] = []
    for raw in value[:MAX_RUNTIME_ERRORS]:
        if not isinstance(raw, dict):
            continue
        entry = {
            "type": _bounded_text(raw.get("type"), 80) or "runtime",
            "message": _bounded_text(raw.get("message"), 1_500),
        }
        for field, limit in (
            ("operation", 80),
            ("code", 120),
            ("timestamp", 100),
        ):
            text = _bounded_text(raw.get(field), limit)
            if text:
                entry[field] = text
        for field, limit in (
            ("filename", 240),
            ("componentStack", 4_000),
        ):
            text = _bounded_text(raw.get(field), limit)
            if text:
                entry[field] = text
        for field in ("lineno", "colno", "status"):
            try:
                number = int(raw.get(field))
            except (TypeError, ValueError):
                continue
            if 0 <= number <= 10_000_000:
                entry[field] = number
        if "retryable" in raw:
            entry["retryable"] = bool(raw.get("retryable"))
        safe.append(entry)
    return safe


def _safe_operation_error(value: Any) -> dict[str, Any] | None:
    errors = _safe_runtime_errors([value])
    if not errors:
        return None
    entry = errors[0]
    entry["type"] = "operation"
    return entry


def _safe_context(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    safe: dict[str, Any] = {}
    for field in ("view", "settingsTab", "inputIntent", "reviewStatus"):
        text = _bounded_text(raw.get(field), 80)
        if text:
            safe[field] = text
    for field in ("itemCount", "pendingCount"):
        try:
            count = int(raw.get(field))
        except (TypeError, ValueError):
            continue
        safe[field] = max(0, min(count, 100_000))
    hangul = raw.get("hangul")
    if isinstance(hangul, dict):
        safe["hangul"] = {
            "status": _bounded_text(hangul.get("status"), 40),
            "summary": _bounded_text(hangul.get("summary"), 240),
        }
    errors = _safe_runtime_errors(raw.get("runtimeErrors"))
    if errors:
        safe["runtimeErrors"] = errors
    last_operation_error = _safe_operation_error(raw.get("lastOperationError"))
    if last_operation_error:
        safe["lastOperationError"] = last_operation_error
    return safe


def _decode_log_bytes(raw: bytes, *, file_prefix: bytes = b"") -> str:
    """Decode UTF-8 and common Windows log encodings without losing Hangul."""

    marker = file_prefix[:4]
    if marker.startswith(codecs.BOM_UTF8) or raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig", errors="replace")
    if marker.startswith(codecs.BOM_UTF16_LE):
        return raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    if marker.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16-be", errors="replace").lstrip("\ufeff")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        preferred = locale.getpreferredencoding(False) or "utf-8"
        try:
            return raw.decode(preferred)
        except (LookupError, UnicodeDecodeError):
            return raw.decode("utf-8", errors="replace")


def _read_log_tail(log_file: Path | None) -> str:
    if log_file is None or not log_file.is_file():
        return ""
    try:
        with log_file.open("rb") as stream:
            file_prefix = stream.read(4)
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - (MAX_LOG_CHARS * 4)), os.SEEK_SET)
            tail = _decode_log_bytes(stream.read(), file_prefix=file_prefix)
    except OSError:
        return ""
    return redact_sensitive_text(tail)[-MAX_LOG_CHARS:]


def build_bug_report(
    request_payload: dict[str, Any],
    *,
    app_config: dict[str, Any],
    log_file: Path | None = None,
) -> dict[str, Any]:
    """Build the remote collector payload from an untrusted local request."""

    description = _bounded_text(request_payload.get("description"), MAX_DESCRIPTION_CHARS)
    if len(description) < 5:
        raise BugReportValidationError("무슨 문제가 있었는지 5자 이상 적어 주세요.")
    include_diagnostics = bool(request_payload.get("includeDiagnostics", True))
    context = _safe_context(request_payload.get("context"))
    contact = _safe_contact(request_payload.get("contact"))
    consent_to_contact = bool(request_payload.get("consentToContact"))
    if contact and not consent_to_contact:
        raise BugReportValidationError("회신 연락처를 보내려면 연락 동의에 체크해 주세요.")
    if consent_to_contact and not contact:
        raise BugReportValidationError("회신 받을 연락처를 입력해 주세요.")
    app_id = _bounded_text(app_config.get("appId"), 80) or "ClassInEDBMVP"
    version = _bounded_text(app_config.get("version"), 80) or "unknown"
    app_platform = _bounded_text(app_config.get("platform"), 40) or sys.platform

    report: dict[str, Any] = {
        "schemaVersion": 1,
        "submittedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "category": "bug",
        "description": description,
        "app": {
            "id": app_id,
            "version": version,
            "platform": app_platform,
        },
        "context": context,
    }
    if contact:
        report["reporter"] = {
            "contact": contact,
            "consentToContact": True,
        }
    if include_diagnostics:
        diagnostics: dict[str, Any] = {
            "system": _bounded_text(platform.system(), 80),
            "systemRelease": _bounded_text(platform.release(), 160),
            "pythonVersion": _bounded_text(platform.python_version(), 40),
        }
        log_tail = _read_log_tail(log_file)
        if log_tail:
            diagnostics["logTail"] = log_tail
        report["diagnostics"] = diagnostics
    return report


def deliver_bug_report(
    report: dict[str, Any],
    *,
    endpoint: str = DEFAULT_BUG_REPORT_URL,
    timeout: float = REPORT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """POST a sanitized report to the remote collector and return its receipt."""

    endpoint = str(endpoint or "").strip()
    if not endpoint.startswith("https://"):
        raise BugReportDeliveryError("버그 리포트 수신 주소가 안전한 HTTPS 주소가 아닙니다.")
    body = json.dumps(report, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": (
                f"{report.get('app', {}).get('id', 'ClassInEDBMVP')}/"
                f"{report.get('app', {}).get('version', 'unknown')}"
            ),
            "X-EDB-Report-Schema": "1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_REMOTE_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise BugReportDeliveryError(f"수신 서버가 신고를 받지 못했습니다. ({exc.code})") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise BugReportDeliveryError("수신 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.") from exc
    if len(raw) > MAX_REMOTE_RESPONSE_BYTES:
        raise BugReportDeliveryError("수신 서버 응답이 너무 큽니다.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BugReportDeliveryError("수신 서버 응답을 확인할 수 없습니다.") from exc
    if not isinstance(payload, dict) or not payload.get("ok") or not payload.get("reportId"):
        raise BugReportDeliveryError("수신 서버가 접수 번호를 반환하지 않았습니다.")
    return {
        "ok": True,
        "reportId": _bounded_text(payload.get("reportId"), 100),
        "receivedAt": _bounded_text(payload.get("receivedAt"), 100),
        "contactAccepted": bool(payload.get("contactAccepted")),
    }
