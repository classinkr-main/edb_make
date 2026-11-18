#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, str] = {
    "appId": "ClassInEDBMVP",
    "appName": "ClassInEDBMVP",
    "version": "0.1.0",
    "updateFeedUrl": "",
    "downloadUrl": "",
    "releaseNotesUrl": "",
    "updatePinHash": "",
}

ALIAS_GROUPS = (
    ("appId", ("appId", "app_id")),
    ("appName", ("appName", "app_name")),
    ("updateFeedUrl", ("updateFeedUrl", "update_feed_url")),
    ("downloadUrl", ("downloadUrl", "download_url")),
    ("releaseNotesUrl", ("releaseNotesUrl", "release_notes_url")),
    ("updatePinHash", ("updatePinHash", "update_pin_hash")),
)

ENV_OVERRIDES = {
    "appId": "EDB_PACKAGE_APP_ID",
    "appName": "EDB_PACKAGE_APP_NAME",
    "version": "EDB_PACKAGE_APP_VERSION",
    "updateFeedUrl": "EDB_PACKAGE_UPDATE_FEED_URL",
    "downloadUrl": "EDB_PACKAGE_DOWNLOAD_URL",
    "releaseNotesUrl": "EDB_PACKAGE_RELEASE_NOTES_URL",
    "updatePinHash": "EDB_PACKAGE_UPDATE_PIN_HASH",
}

UPDATE_PIN_PBKDF2_ITERATIONS = 210_000


def parse_update_pin_verifier(value: str) -> tuple[int, bytes, bytes] | None:
    parts = str(value or "").strip().split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return None
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except ValueError:
        return None
    if not 100_000 <= iterations <= 2_000_000:
        return None
    if not 16 <= len(salt) <= 64 or len(expected) != 32:
        return None
    return iterations, salt, expected


def build_update_pin_verifier(pin: str, app_id: str) -> str:
    normalized = str(pin or "").strip()
    if not normalized.isascii() or not normalized.isdigit() or not 4 <= len(normalized) <= 12:
        raise ValueError("EDB_PACKAGE_UPDATE_PIN must contain 4-12 ASCII digits")
    salt = hashlib.sha256(f"{app_id}:classin-edb:update-pin".encode("utf-8")).digest()[:16]
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        normalized.encode("ascii"),
        salt,
        UPDATE_PIN_PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${UPDATE_PIN_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_update_pin(pin: str, verifier: str) -> bool:
    parsed = parse_update_pin_verifier(verifier)
    if parsed is None:
        return False
    iterations, salt, expected = parsed
    actual = hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def first_nonempty(payload: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = str(payload.get(name) or "").strip()
        if value:
            return value
    return ""


def normalize_update_config(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {key: value for key, value in payload.items() if value is not None}
    for canonical, aliases in ALIAS_GROUPS:
        values = {
            alias: str(payload.get(alias) or "").strip()
            for alias in aliases
            if str(payload.get(alias) or "").strip()
        }
        if len(set(values.values())) > 1:
            details = ", ".join(f"{alias}={value!r}" for alias, value in values.items())
            raise ValueError(f"app_update_config.json {canonical} aliases conflict: {details}")
        value = first_nonempty(payload, aliases)
        for alias in aliases:
            if alias != canonical:
                normalized.pop(alias, None)
        if value:
            normalized[canonical] = value
    return normalized


def read_source_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        print(f"[app-update-config] ignoring invalid JSON in {path}: {exc}", file=sys.stderr)
        return {}
    if not isinstance(payload, dict):
        print(f"[app-update-config] ignoring non-object JSON in {path}", file=sys.stderr)
        return {}
    return normalize_update_config(payload)


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    return {
        key: value
        for key, env_name in ENV_OVERRIDES.items()
        if (value := str(environ.get(env_name) or "").strip())
    }


def build_config(source_path: Path, environ: dict[str, str] | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    config: dict[str, Any] = dict(DEFAULT_CONFIG)
    config.update(read_source_config(source_path))
    config.update(env_overrides(environ))
    raw_pin = str(environ.get("EDB_PACKAGE_UPDATE_PIN") or "").strip()
    if raw_pin:
        generated_verifier = build_update_pin_verifier(raw_pin, str(config.get("appId") or "ClassInEDBMVP"))
        configured_verifier = str(config.get("updatePinHash") or "").strip()
        if configured_verifier and not verify_update_pin(raw_pin, configured_verifier):
            raise ValueError("EDB_PACKAGE_UPDATE_PIN does not match EDB_PACKAGE_UPDATE_PIN_HASH")
        config["updatePinHash"] = configured_verifier or generated_verifier
    verifier = str(config.get("updatePinHash") or "").strip()
    if verifier and parse_update_pin_verifier(verifier) is None:
        raise ValueError("updatePinHash must be a valid pbkdf2_sha256 verifier")
    return config


def write_config(output_path: Path, config: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build canonical app_update_config.json metadata.")
    parser.add_argument("source", type=Path, help="Project app_update_config.json path")
    parser.add_argument("output", type=Path, help="Generated app_update_config.json path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        write_config(args.output, build_config(args.source))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
