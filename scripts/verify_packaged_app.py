#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    from .verify_frontend_package import (
        REQUIRED_RUNTIME_SOURCE_FILES,
        REQUIRED_UI_FILES,
        SOURCE_DIGEST_RE,
        bundle_cache_bust_digest,
        frontend_bundle_source_digest,
    )
    from .verify_release_licenses import (
        UPSCAYL_MODEL_NAME,
        UPSCAYL_REQUIRED_COMPLIANCE_FILES,
        current_upscayl_platform,
    )
    from .build_release_metadata import collect_release_metadata_errors
except ImportError:  # pragma: no cover - direct script execution
    from verify_frontend_package import (
        REQUIRED_RUNTIME_SOURCE_FILES,
        REQUIRED_UI_FILES,
        SOURCE_DIGEST_RE,
        bundle_cache_bust_digest,
        frontend_bundle_source_digest,
    )
    from verify_release_licenses import (
        UPSCAYL_MODEL_NAME,
        UPSCAYL_REQUIRED_COMPLIANCE_FILES,
        current_upscayl_platform,
    )
    from build_release_metadata import collect_release_metadata_errors


REQUIRED_RUNTIME_FILES = (
    "app_update_config.json",
    *REQUIRED_RUNTIME_SOURCE_FILES,
)

REQUIRED_SOURCE_PACKAGE_FILES = (
    "app_server.py",
    "bug_reporting.py",
    "build_mvp_export.py",
    "build_problem_board_edb.py",
    "build_structured_page_json.py",
    "image_reconstruction_backend.py",
    "upscayl_backend.py",
    "page_repair.py",
    "pipeline_cache.py",
    "pipeline_router.py",
    "preprocess.py",
    "segment.py",
    "ocr_backend.py",
    "placement_engine.py",
    "layout_template_schema.py",
    "structured_schema.py",
    "user_settings.py",
    "assemble_page.py",
    "edb_builder.py",
    "inspect_edb.py",
    "requirements-local.txt",
    "requirements-release-bootstrap.lock",
    "requirements-release.lock",
    "release/dependency_inventory.json",
    "release/THIRD_PARTY_NOTICES.md",
    "run_local_app.ps1",
)

FORBIDDEN_PACKAGED_FRONTEND_FILES = (
    "ui_prototype/app.js",
    "ui_prototype/prototype_data.js",
    "ui_prototype/generated_session.js",
    "ui_prototype/vendor/babel.min.js",
    "ui_prototype/vendor/babel.min.js.map",
)

FORBIDDEN_PACKAGED_BUILD_TOOL_PATHS = (
    "scripts/build_frontend_bundle.mjs",
    "scripts/vendor",
    "scripts/verify_frontend_package.py",
    "scripts/verify_packaged_app.py",
)

FORBIDDEN_PACKAGED_SOURCE_ASSET_FILES = (
    "assets/app_icon.svg",
    "assets/brand_mark.svg",
    "assets/app_icon.iconset",
)

FORBIDDEN_BOARD_TOKENS = (
    "app.js?v=",
    "prototype_data.js",
    "generated_session.js",
    "vendor/babel.min.js",
    'type="text/babel"',
)

FORBIDDEN_PACKAGED_RUNTIME_PATHS = (
    ".app_runtime",
    "uploads",
    "outputs",
    "publish_output",
    "mutated_crops",
    "exports",
    "ai_retries",
    "ai_image_reconstructions",
    "latest_session.json",
    "session_history.json",
    "generated_session.js",
    "app.log",
)

FORBIDDEN_PACKAGED_SECRET_FILE_NAMES = (
    ".env",
    "user_settings.json",
)

FORBIDDEN_PACKAGED_SECRET_FILE_SUFFIXES = (
    ".pem",
    ".p8",
    ".p12",
    ".pfx",
)

FORBIDDEN_PACKAGED_SECRET_FILE_TOKENS = (
    "credential",
    "secret",
    "service-account",
)

PACKAGED_SECRET_TEXT_SUFFIXES = (
    ".cfg",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".plist",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
)

PACKAGED_SECRET_VALUE_PATTERNS = (
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{60,})\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _is_loopback_hostname(hostname: str | None) -> bool:
    host = (hostname or "").strip().lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")


def _packaged_update_url_error(field_name: str, value: object) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.netloc:
        return f"packaged app_update_config.json {field_name} must be an absolute URL"
    if parsed.scheme == "https" or (parsed.scheme == "http" and _is_loopback_hostname(parsed.hostname)):
        return ""
    return f"packaged app_update_config.json {field_name} must use https or loopback http"


def _config_text_value(config: dict, *field_names: str) -> str:
    for field_name in field_names:
        value = str(config.get(field_name) or "").strip()
        if value:
            return value
    return ""


def _config_alias_conflict_error(label: str, config: dict, *field_names: str) -> str:
    values = {
        field_name: str(config.get(field_name) or "").strip()
        for field_name in field_names
        if str(config.get(field_name) or "").strip()
    }
    if len(set(values.values())) <= 1:
        return ""
    details = ", ".join(f"{field_name}={value!r}" for field_name, value in values.items())
    return f"packaged app_update_config.json {label} aliases conflict: {details}"


def _packaged_update_pin_verifier_error(value: object) -> str:
    verifier = str(value or "").strip()
    if not verifier:
        return ""
    parts = verifier.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return "packaged app_update_config.json updatePinHash is not a valid PBKDF2 verifier"
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        digest = bytes.fromhex(parts[3])
    except ValueError:
        return "packaged app_update_config.json updatePinHash is not a valid PBKDF2 verifier"
    if not 100_000 <= iterations <= 2_000_000 or not 16 <= len(salt) <= 64 or len(digest) != 32:
        return "packaged app_update_config.json updatePinHash is not a valid PBKDF2 verifier"
    return ""


def _relative_label(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_forbidden_secret_path(path: Path) -> bool:
    lowered_parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    if name in FORBIDDEN_PACKAGED_SECRET_FILE_NAMES or name.startswith(".env."):
        return True
    if any(name.endswith(suffix) for suffix in FORBIDDEN_PACKAGED_SECRET_FILE_SUFFIXES):
        return True
    return any(token in part for token in FORBIDDEN_PACKAGED_SECRET_FILE_TOKENS for part in lowered_parts)


def _should_scan_packaged_text(path: Path) -> bool:
    return path.suffix.lower() in PACKAGED_SECRET_TEXT_SUFFIXES or path.name == "Info.plist"


def _collect_packaged_secret_errors(root: Path, scan_roots: list[Path]) -> list[str]:
    errors: list[str] = []
    seen: set[Path] = set()
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for candidate in scan_root.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved in seen:
                continue
            seen.add(resolved)
            relative_label = _relative_label(root, candidate)
            if _is_forbidden_secret_path(candidate):
                errors.append(f"forbidden packaged secret file exists: {relative_label}")
            if not _should_scan_packaged_text(candidate):
                continue
            try:
                text = _read(candidate)
            except OSError as exc:
                errors.append(f"could not inspect packaged text file {relative_label}: {exc}")
                continue
            for label, pattern in PACKAGED_SECRET_VALUE_PATTERNS:
                if pattern.search(text):
                    errors.append(f"forbidden packaged secret value in {relative_label}: {label}")
    return errors


def _packaged_update_config_paths(root: Path, resource_roots: list[Path]) -> list[Path]:
    candidates = [
        root / "app_update_config.json",
        root / "_internal" / "app_update_config.json",
        root / "Contents" / "Resources" / "app_update_config.json",
        root / "Contents" / "Resources" / "_internal" / "app_update_config.json",
        root / "Contents" / "Frameworks" / "app_update_config.json",
        root / "Contents" / "Frameworks" / "_internal" / "app_update_config.json",
        *(resource_root / "app_update_config.json" for resource_root in resource_roots),
    ]
    candidates.extend(root.rglob("app_update_config.json"))
    paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        paths.append(candidate)
    return paths


def _update_config_payload_key(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _resource_root_candidates(package_root: Path) -> list[Path]:
    explicit_candidates = (
        package_root,
        package_root / "_internal",
        package_root / "Contents" / "Resources",
        package_root / "Contents" / "Resources" / "_internal",
        package_root / "Contents" / "Frameworks",
        package_root / "Contents" / "Frameworks" / "_internal",
    )
    candidates: list[Path] = []
    seen: set[Path] = set()
    for candidate in explicit_candidates:
        board_path = candidate / "ui_prototype" / "board.html"
        if not board_path.is_file():
            continue
        resolved = board_path.resolve().parent.parent
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(resolved)
    if candidates:
        return candidates

    for board_path in package_root.rglob("board.html"):
        if board_path.parent.name != "ui_prototype":
            continue
        candidate = board_path.resolve().parent.parent
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)
    return candidates


def _looks_like_source_package(package_root: Path) -> bool:
    return package_root.name == "source-package" or any(
        (package_root / rel_path).exists()
        for rel_path in ("run_local_app.ps1", "requirements-local.txt", "app_server.py")
    )


def _discover_source_root(package_root: Path) -> Path | None:
    """Find the checkout that owns a local package under (usually) ``dist``."""

    for candidate in (package_root, *package_root.parents):
        if (
            (candidate / "ui_prototype" / "app.jsx").is_file()
            and (candidate / "scripts" / "build_frontend_bundle.mjs").is_file()
        ):
            return candidate
    return None


def _collect_source_package_import_errors(package_root: Path) -> list[str]:
    """Actually import the source fallback entrypoint in an isolated process."""

    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        (
            "import sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "import app_server; "
            "assert callable(getattr(app_server, 'main', None))"
        ),
        str(package_root),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=package_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"source-package app_server import smoke could not run: {exc}"]
    if completed.returncode == 0:
        return []
    detail = (completed.stderr or completed.stdout or "unknown import failure").strip()
    return [f"source-package app_server import smoke failed: {detail[-2_000:]}"]


def _collect_windows_onedir_errors(
    package_root: Path,
    resource_root: Path,
    *,
    expected_app_name: str = "",
) -> list[str]:
    if resource_root != package_root / "_internal":
        return []

    errors: list[str] = []
    expected_launcher = package_root / f"{expected_app_name}.exe" if expected_app_name else None
    launcher_candidates = [
        path
        for path in package_root.glob("*.exe")
        if path.is_file()
        and not path.name.lower().startswith("unins")
        and not path.name.lower().endswith("-setup.exe")
    ]
    if expected_launcher is not None and not expected_launcher.is_file():
        errors.append(f"missing Windows packaged launcher: {expected_launcher.name}")
    elif expected_launcher is None and not launcher_candidates:
        errors.append("missing Windows packaged launcher executable")

    python_dlls = [path for path in resource_root.glob("python3*.dll") if path.is_file()]
    if not python_dlls:
        errors.append("missing Windows packaged Python runtime DLL under _internal")

    for candidate in package_root.iterdir():
        if not candidate.is_file():
            continue
        lowered_name = candidate.name.lower()
        if lowered_name.endswith((".zip", ".dmg")) or lowered_name.endswith("-setup.exe"):
            errors.append(
                "forbidden nested distribution artifact exists at package root: "
                f"{candidate.name}"
            )
    return errors


def collect_package_errors(
    package_root: Path,
    *,
    expected_app_id: str = "",
    expected_app_name: str = "",
    expected_version: str = "",
    expected_update_feed_url: str = "",
    expected_download_url: str = "",
    expected_release_notes_url: str = "",
    expected_bundle_id: str = "",
    expected_git_commit: str = "",
    source_root: Path | None = None,
    smoke_source_import: bool = False,
) -> list[str]:
    root = package_root.resolve()
    errors: list[str] = []
    if not root.exists():
        return [f"package root does not exist: {package_root}"]
    if root.is_file():
        return [f"package root must be an inspectable directory, not a file: {package_root}"]

    resource_roots = _resource_root_candidates(root)
    if not resource_roots:
        return [f"could not locate packaged ui_prototype/board.html under: {package_root}"]
    if len(resource_roots) > 1:
        labels = ", ".join(str(path.relative_to(root)) for path in resource_roots)
        errors.append(f"multiple packaged frontend roots found: {labels}")

    packaged_version = ""
    resource_root = resource_roots[0]
    for rel_path in (*REQUIRED_UI_FILES, *REQUIRED_RUNTIME_FILES):
        if not (resource_root / rel_path).is_file():
            errors.append(f"missing packaged runtime file: {rel_path}")
    release_metadata_root = resource_root / "release_metadata"
    errors.extend(
        f"packaged {error}"
        for error in collect_release_metadata_errors(
            release_metadata_root,
            expected_version=expected_version,
            expected_git_commit=expected_git_commit,
        )
    )
    if _looks_like_source_package(root):
        for rel_path in REQUIRED_SOURCE_PACKAGE_FILES:
            if not (root / rel_path).is_file():
                errors.append(f"missing source-package runtime file: {rel_path}")
        if smoke_source_import:
            errors.extend(_collect_source_package_import_errors(root))
    else:
        errors.extend(
            _collect_windows_onedir_errors(
                root,
                resource_root,
                expected_app_name=expected_app_name,
            )
        )

    update_config_payloads: dict[str, list[str]] = {}
    for config_path in _packaged_update_config_paths(root, resource_roots):
        try:
            payload = _read_json(config_path)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(
                "packaged app_update_config.json is not valid JSON at "
                f"{_relative_label(root, config_path)}: {exc}"
            )
            continue
        if not isinstance(payload, dict):
            errors.append(
                "packaged app_update_config.json must contain a JSON object at "
                f"{_relative_label(root, config_path)}"
            )
            continue
        update_config_payloads.setdefault(_update_config_payload_key(payload), []).append(
            _relative_label(root, config_path)
        )
    if len(update_config_payloads) > 1:
        labels = "; ".join(", ".join(paths) for paths in update_config_payloads.values())
        errors.append(f"multiple packaged app_update_config.json files disagree: {labels}")

    update_config_path = resource_root / "app_update_config.json"
    if update_config_path.is_file():
        try:
            update_config = _read_json(update_config_path)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"packaged app_update_config.json is not valid JSON: {exc}")
        else:
            if not isinstance(update_config, dict):
                errors.append("packaged app_update_config.json must contain a JSON object")
            else:
                app_id = str(update_config.get("appId") or update_config.get("app_id") or "").strip()
                app_name = _config_text_value(update_config, "appName", "app_name")
                version = str(update_config.get("version") or "").strip()
                alias_pairs = (
                    ("appId", ("appId", "app_id")),
                    ("appName", ("appName", "app_name")),
                    ("updateFeedUrl", ("updateFeedUrl", "update_feed_url")),
                    ("downloadUrl", ("downloadUrl", "download_url")),
                    ("releaseNotesUrl", ("releaseNotesUrl", "release_notes_url")),
                    ("updatePinHash", ("updatePinHash", "update_pin_hash")),
                )
                for label, field_names in alias_pairs:
                    if alias_error := _config_alias_conflict_error(label, update_config, *field_names):
                        errors.append(alias_error)
                if not app_id:
                    errors.append("packaged app_update_config.json is missing appId")
                if not app_name:
                    errors.append("packaged app_update_config.json is missing appName")
                if not version:
                    errors.append("packaged app_update_config.json is missing version")
                else:
                    packaged_version = version
                update_pin_verifier = _config_text_value(update_config, "updatePinHash", "update_pin_hash")
                if pin_error := _packaged_update_pin_verifier_error(update_pin_verifier):
                    errors.append(pin_error)
                if expected_app_id and app_id != expected_app_id:
                    errors.append(
                        "packaged app_update_config.json appId mismatch: "
                        f"expected {expected_app_id!r}, found {app_id!r}"
                    )
                if expected_app_name and app_name != expected_app_name:
                    errors.append(
                        "packaged app_update_config.json appName mismatch: "
                        f"expected {expected_app_name!r}, found {app_name!r}"
                    )
                if expected_version and version != expected_version:
                    errors.append(
                        "packaged app_update_config.json version mismatch: "
                        f"expected {expected_version!r}, found {version!r}"
                    )
                for field_name in (
                    "updateFeedUrl",
                    "update_feed_url",
                    "downloadUrl",
                    "download_url",
                    "releaseNotesUrl",
                    "release_notes_url",
                ):
                    if url_error := _packaged_update_url_error(field_name, update_config.get(field_name)):
                        errors.append(url_error)
                expected_urls = (
                    (
                        "updateFeedUrl",
                        expected_update_feed_url,
                        _config_text_value(update_config, "updateFeedUrl", "update_feed_url"),
                    ),
                    (
                        "downloadUrl",
                        expected_download_url,
                        _config_text_value(update_config, "downloadUrl", "download_url"),
                    ),
                    (
                        "releaseNotesUrl",
                        expected_release_notes_url,
                        _config_text_value(update_config, "releaseNotesUrl", "release_notes_url"),
                    ),
                )
                for field_name, expected_url, packaged_url in expected_urls:
                    if expected_url and packaged_url != expected_url:
                        errors.append(
                            f"packaged app_update_config.json {field_name} mismatch: "
                            f"expected {expected_url!r}, found {packaged_url!r}"
                        )

    for rel_path in FORBIDDEN_PACKAGED_FRONTEND_FILES:
        if (resource_root / rel_path).exists():
            errors.append(f"forbidden packaged frontend file exists: {rel_path}")
    for rel_path in FORBIDDEN_PACKAGED_SOURCE_ASSET_FILES:
        if (resource_root / rel_path).exists():
            errors.append(f"forbidden packaged source asset exists: {rel_path}")

    packaged_upscayl_root = resource_root / "resources" / "upscayl"
    if packaged_upscayl_root.exists():
        if not packaged_upscayl_root.is_dir():
            errors.append("packaged Upscayl runtime path must be a directory: resources/upscayl")
        else:
            for compliance_name in UPSCAYL_REQUIRED_COMPLIANCE_FILES:
                compliance_path = packaged_upscayl_root / compliance_name
                if not compliance_path.is_file() or compliance_path.stat().st_size <= 0:
                    errors.append(
                        "packaged Upscayl runtime is missing compliance file: "
                        f"resources/upscayl/{compliance_name}"
                    )
            if root.suffix == ".app":
                platform_name = "mac"
            elif any(root.glob("*.exe")):
                platform_name = "win"
            else:
                platform_name = current_upscayl_platform()
            binary_name = "upscayl-bin.exe" if platform_name == "win" else "upscayl-bin"
            binary_path = packaged_upscayl_root / platform_name / "bin" / binary_name
            if not binary_path.is_file() or binary_path.stat().st_size <= 0:
                errors.append(
                    "packaged Upscayl runtime is missing platform binary: "
                    f"resources/upscayl/{platform_name}/bin/{binary_name}"
                )
            for suffix in ("bin", "param"):
                model_path = packaged_upscayl_root / "models" / f"{UPSCAYL_MODEL_NAME}.{suffix}"
                if not model_path.is_file() or model_path.stat().st_size <= 0:
                    errors.append(
                        "packaged Upscayl runtime is missing Lite model asset: "
                        f"resources/upscayl/models/{UPSCAYL_MODEL_NAME}.{suffix}"
                    )

    runtime_scan_roots = [root]
    for candidate in resource_roots:
        if candidate not in runtime_scan_roots:
            runtime_scan_roots.append(candidate)
    for scan_root in runtime_scan_roots:
        for rel_path in FORBIDDEN_PACKAGED_BUILD_TOOL_PATHS:
            candidate = scan_root / rel_path
            if candidate.exists():
                errors.append(f"forbidden packaged build-time tool exists: {rel_path}")
        for rel_path in FORBIDDEN_PACKAGED_SOURCE_ASSET_FILES:
            candidate = scan_root / rel_path
            if candidate.exists():
                errors.append(f"forbidden packaged source asset exists: {rel_path}")
        for rel_path in FORBIDDEN_PACKAGED_RUNTIME_PATHS:
            candidate = scan_root / rel_path
            if candidate.exists():
                errors.append(f"forbidden packaged runtime artifact exists: {candidate.relative_to(scan_root)}")
    errors.extend(_collect_packaged_secret_errors(root, runtime_scan_roots))

    packaged_bundle_digest = None
    bundle_path = resource_root / "ui_prototype" / "app.bundle.js"
    if bundle_path.is_file():
        bundle = _read(bundle_path)
        if "Generated by scripts/build_frontend_bundle.mjs" not in bundle:
            errors.append("packaged app.bundle.js is missing the generated bundle banner")
        digest_match = SOURCE_DIGEST_RE.search(bundle)
        if digest_match is None:
            errors.append("packaged app.bundle.js is missing the source digest")
        else:
            packaged_bundle_digest = digest_match.group(1)
        if "/* app.jsx */" not in bundle:
            errors.append("packaged app.bundle.js does not include the app.jsx section")
        if source_root is not None:
            resolved_source_root = source_root.expanduser().resolve()
            source_bundle_path = resolved_source_root / "ui_prototype" / "app.bundle.js"
            if not source_bundle_path.is_file():
                errors.append(f"current source bundle is missing: {source_bundle_path}")
            else:
                try:
                    source_bundle_hash = hashlib.sha256(source_bundle_path.read_bytes()).hexdigest()
                    packaged_bundle_hash = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
                except OSError as exc:
                    errors.append(f"could not hash frontend bundle bytes: {exc}")
                else:
                    if source_bundle_hash != packaged_bundle_hash:
                        errors.append(
                            "packaged app.bundle.js bytes do not match current source bundle: "
                            f"packaged {packaged_bundle_hash}, source {source_bundle_hash}"
                        )
            if packaged_bundle_digest:
                try:
                    source_digest = frontend_bundle_source_digest(resolved_source_root)
                except OSError as exc:
                    errors.append(f"could not calculate frontend source digest under {resolved_source_root}: {exc}")
                else:
                    if source_digest is None:
                        errors.append(
                            "could not calculate frontend source digest under "
                            f"{resolved_source_root}"
                        )
                    elif source_digest != packaged_bundle_digest:
                        errors.append(
                            "packaged frontend source digest does not match current source: "
                            f"packaged {packaged_bundle_digest}, source {source_digest}"
                        )

    board_path = resource_root / "ui_prototype" / "board.html"
    if board_path.is_file():
        board_html = _read(board_path)
        script_srcs = re.findall(r"<script\s+src=\"([^\"]+)\"", board_html)
        bundle_srcs = [src for src in script_srcs if src.startswith("app.bundle.js?v=frontend-bundle-")]
        if len(bundle_srcs) != 1:
            errors.append("packaged board.html must load exactly one cache-busted app.bundle.js")
        elif (board_digest := bundle_cache_bust_digest(bundle_srcs[0])) is None:
            errors.append("packaged board.html app.bundle.js cache bust must use the bundle source digest")
        elif packaged_bundle_digest and board_digest != packaged_bundle_digest:
            errors.append("packaged board.html app.bundle.js cache bust does not match packaged app.bundle.js")
        style_path = resource_root / "ui_prototype" / "board.css"
        style_hrefs = [
            href
            for href in re.findall(r"<link[^>]*href=\"([^\"]+)\"", board_html)
            if href.startswith("board.css")
        ]
        if len(style_hrefs) != 1:
            errors.append("packaged board.html must load exactly one cache-busted board.css")
        elif (match := re.fullmatch(r"board\.css\?v=board-css-([0-9a-f]{64})", style_hrefs[0])) is None:
            errors.append("packaged board.html board.css cache bust must use the stylesheet digest")
        elif style_path.is_file():
            packaged_style_digest = hashlib.sha256(style_path.read_bytes()).hexdigest()
            if match.group(1) != packaged_style_digest:
                errors.append("packaged board.html board.css cache bust does not match packaged board.css")
        for token in FORBIDDEN_BOARD_TOKENS:
            if token in board_html:
                errors.append(f"packaged board.html still references legacy runtime token: {token}")

    info_plist_path = root / "Contents" / "Info.plist"
    if root.suffix == ".app" or info_plist_path.exists():
        if not info_plist_path.is_file():
            errors.append("missing macOS app bundle Info.plist: Contents/Info.plist")
        else:
            try:
                info_plist = plistlib.loads(info_plist_path.read_bytes())
            except (OSError, plistlib.InvalidFileException) as exc:
                errors.append(f"macOS app bundle Info.plist is not valid: {exc}")
            else:
                if not isinstance(info_plist, dict):
                    errors.append("macOS app bundle Info.plist must contain a dictionary")
                else:
                    bundle_id = str(info_plist.get("CFBundleIdentifier") or "").strip()
                    if not bundle_id:
                        errors.append("macOS app bundle Info.plist is missing CFBundleIdentifier")
                    elif expected_bundle_id and bundle_id != expected_bundle_id:
                        errors.append(
                            "macOS app bundle Info.plist CFBundleIdentifier mismatch: "
                            f"expected {expected_bundle_id!r}, found {bundle_id!r}"
                        )
                    expected_bundle_version = expected_version or packaged_version
                    for key in ("CFBundleShortVersionString", "CFBundleVersion"):
                        value = str(info_plist.get(key) or "").strip()
                        if not value:
                            errors.append(f"macOS app bundle Info.plist is missing {key}")
                        elif expected_bundle_version and value != expected_bundle_version:
                            errors.append(
                                f"macOS app bundle Info.plist {key} mismatch: "
                                f"expected {expected_bundle_version!r}, found {value!r}"
                            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a built app package layout.")
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--expected-app-id", default="", help="Expected appId in packaged update metadata")
    parser.add_argument("--expected-app-name", default="", help="Expected appName in packaged update metadata")
    parser.add_argument("--expected-version", default="", help="Expected version in packaged update metadata")
    parser.add_argument("--expected-update-feed-url", default="", help="Expected updateFeedUrl in packaged metadata")
    parser.add_argument("--expected-download-url", default="", help="Expected downloadUrl in packaged metadata")
    parser.add_argument("--expected-release-notes-url", default="", help="Expected releaseNotesUrl in packaged metadata")
    parser.add_argument("--expected-bundle-id", default="", help="Expected macOS CFBundleIdentifier")
    parser.add_argument("--expected-git-commit", default="", help="Expected full git commit in release provenance")
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="Checkout root whose current frontend source digest must match the package",
    )
    parser.add_argument(
        "--smoke-source-import",
        action="store_true",
        help="Import app_server from a source-package fallback in an isolated process",
    )
    args = parser.parse_args(argv)

    source_root = args.source_root or _discover_source_root(args.package_root.resolve())

    errors = collect_package_errors(
        args.package_root,
        expected_app_id=args.expected_app_id,
        expected_app_name=args.expected_app_name,
        expected_version=args.expected_version,
        expected_update_feed_url=args.expected_update_feed_url,
        expected_download_url=args.expected_download_url,
        expected_release_notes_url=args.expected_release_notes_url,
        expected_bundle_id=args.expected_bundle_id,
        expected_git_commit=args.expected_git_commit,
        source_root=source_root,
        smoke_source_import=args.smoke_source_import,
    )
    if errors:
        for error in errors:
            print(f"[packaged-app] ERROR: {error}", file=sys.stderr)
        return 1
    print(f"[packaged-app] OK: {args.package_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
