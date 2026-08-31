#!/usr/bin/env python3
"""User-editable settings persisted under .app_runtime/user_settings.json.

The local app needs a place to keep secrets (currently AI provider API keys)
between runs without putting them in source control. Settings are written as
JSON next to the runtime cache so they are covered by the existing
``.app_runtime/`` gitignore entry.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

_SETTINGS_FILENAME = "user_settings.json"
_AI_ENABLED_KEY = "ai_enabled"
_SETTINGS_LOCK = threading.RLock()


def settings_path(runtime_dir: Path) -> Path:
    return runtime_dir / _SETTINGS_FILENAME


def load_user_settings(runtime_dir: Path) -> dict[str, Any]:
    path = settings_path(runtime_dir)
    if not path.exists():
        return {}
    try:
        # ``utf-8-sig`` also accepts plain UTF-8 and tolerates files saved by
        # Windows editors that prepend a BOM.
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, UnicodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def ai_enabled_from_settings(
    settings: dict[str, Any] | None,
    *,
    default: bool = True,
) -> bool:
    """Return the persisted global AI preference.

    Existing installations predate the switch, so a missing value keeps the
    historical behavior (AI available when a provider key exists).
    """
    if not isinstance(settings, dict) or _AI_ENABLED_KEY not in settings:
        return default
    value = settings.get(_AI_ENABLED_KEY)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value or "").strip().lower()
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    return default


def save_user_settings(runtime_dir: Path, settings: dict[str, Any]) -> Path:
    with _SETTINGS_LOCK:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        path = settings_path(runtime_dir)
        payload = json.dumps(settings, ensure_ascii=False, indent=2)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{_SETTINGS_FILENAME}.",
            suffix=".tmp",
            dir=runtime_dir,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            _restrict_permissions(temp_path)
            # A same-directory replace is atomic on NTFS as well as POSIX,
            # preventing a crash or concurrent reader from observing half JSON.
            os.replace(temp_path, path)
            return path
        except Exception:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise


def apply_to_env(settings: dict[str, Any], *, overwrite: bool = False) -> dict[str, str]:
    """Promote relevant settings to ``os.environ`` so downstream pipeline code
    that reads env vars (Gemini OCR/page repair, OpenAI image reconstruction)
    picks them up.

    By default an externally-set env var wins so users who exported
    provider keys in their shell are not silently overridden.
    """
    applied: dict[str, str] = {}
    _apply_key(
        settings,
        env_key="GEMINI_API_KEY",
        settings_key="gemini_api_key",
        overwrite=overwrite,
        applied=applied,
    )
    _apply_key(
        settings,
        env_key="OPENAI_API_KEY",
        settings_key="openai_api_key",
        overwrite=overwrite,
        applied=applied,
    )
    return applied


def _apply_key(
    settings: dict[str, Any],
    *,
    env_key: str,
    settings_key: str,
    overwrite: bool,
    applied: dict[str, str],
) -> None:
    key = str(settings.get(settings_key) or "").strip()
    if key:
        if overwrite or not os.environ.get(env_key, "").strip():
            os.environ[env_key] = key
            applied[env_key] = "user_settings"
        else:
            applied[env_key] = "env"
    elif os.environ.get(env_key, "").strip():
        applied[env_key] = "env"


def mask_key(value: str | None) -> str:
    """Return a short preview (e.g. ``AIza…wxyz``) for display."""
    if not value:
        return ""
    text = str(value).strip()
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}…{text[-4:]}"


def summarize_for_response(
    runtime_dir: Path,
    *,
    env_overwrites: bool = False,
) -> dict[str, Any]:
    """Build a sanitized snapshot of settings for the HTTP API.

    Never returns the raw API key — only a masked preview and a flag for
    whether one is currently active. ``source`` indicates where the active
    value comes from: ``env`` (shell-exported) or ``user_settings`` (saved
    via the UI).
    """
    settings = load_user_settings(runtime_dir)
    gemini = _summarize_key(
        settings,
        env_key="GEMINI_API_KEY",
        settings_key="gemini_api_key",
        env_overwrites=env_overwrites,
    )
    openai = _summarize_key(
        settings,
        env_key="OPENAI_API_KEY",
        settings_key="openai_api_key",
        env_overwrites=env_overwrites,
    )

    return {
        "aiEnabled": ai_enabled_from_settings(settings),
        "geminiApiKey": "",  # never echoed
        "geminiApiKeyPreview": gemini["preview"],
        "hasGeminiApiKey": gemini["has_key"],
        "geminiApiKeySource": gemini["source"],
        "geminiApiKeyStoredPreview": gemini["stored_preview"],
        "hasStoredGeminiApiKey": gemini["has_stored_key"],
        "openAiApiKey": "",  # never echoed
        "openAiApiKeyPreview": openai["preview"],
        "hasOpenAiApiKey": openai["has_key"],
        "openAiApiKeySource": openai["source"],
        "openAiApiKeyStoredPreview": openai["stored_preview"],
        "hasStoredOpenAiApiKey": openai["has_stored_key"],
    }


def update_gemini_api_key(runtime_dir: Path, raw_key: str | None) -> dict[str, Any]:
    """Persist (or clear) the Gemini API key and apply it to ``os.environ``.

    Returns the same payload shape as :func:`summarize_for_response` so the
    UI can refresh its status without an extra fetch.
    """
    return update_api_keys(runtime_dir, gemini_api_key=raw_key)


def update_api_keys(
    runtime_dir: Path,
    *,
    gemini_api_key: str | None = None,
    openai_api_key: str | None = None,
    ai_enabled: bool | None = None,
) -> dict[str, Any]:
    """Persist supplied API keys and apply them to ``os.environ``.

    Passing ``None`` leaves a provider unchanged; passing an empty string clears
    the stored key for that provider.
    """
    with _SETTINGS_LOCK:
        settings = load_user_settings(runtime_dir)
        env_overwrites = False
        if gemini_api_key is not None:
            _store_key(settings, "gemini_api_key", gemini_api_key)
            env_overwrites = True
        if openai_api_key is not None:
            _store_key(settings, "openai_api_key", openai_api_key)
            env_overwrites = True
        if ai_enabled is not None:
            settings[_AI_ENABLED_KEY] = bool(ai_enabled)
        save_user_settings(runtime_dir, settings)
        # Keep process state consistent with durable state if an atomic replace
        # is rejected by antivirus software or a transient Windows file lock.
        if gemini_api_key is not None:
            _sync_env_key("GEMINI_API_KEY", gemini_api_key)
        if openai_api_key is not None:
            _sync_env_key("OPENAI_API_KEY", openai_api_key)

    return summarize_for_response(runtime_dir, env_overwrites=env_overwrites)


def update_openai_api_key(runtime_dir: Path, raw_key: str | None) -> dict[str, Any]:
    return update_api_keys(runtime_dir, openai_api_key=raw_key)


def update_ai_enabled(runtime_dir: Path, enabled: bool) -> dict[str, Any]:
    return update_api_keys(runtime_dir, ai_enabled=enabled)


def _store_key(settings: dict[str, Any], key_name: str, raw_key: str | None) -> None:
    key = (raw_key or "").strip()
    if key:
        settings[key_name] = key
    else:
        settings.pop(key_name, None)


def _sync_env_key(env_key: str, raw_key: str | None) -> None:
    key = (raw_key or "").strip()
    if key:
        os.environ[env_key] = key
    else:
        os.environ.pop(env_key, None)


def _summarize_key(
    settings: dict[str, Any],
    *,
    env_key: str,
    settings_key: str,
    env_overwrites: bool,
) -> dict[str, Any]:
    stored_key = str(settings.get(settings_key) or "").strip()
    env_value = os.environ.get(env_key, "").strip()
    active_key = env_value if (env_value and not env_overwrites) else stored_key or env_value
    if not active_key:
        source = "none"
    elif stored_key and (active_key == stored_key or env_overwrites):
        # If the user saved a key via the UI, treat it as the canonical
        # source even when it has been promoted into os.environ.
        source = "user_settings"
    elif env_value:
        source = "env"
    else:
        source = "user_settings"
    return {
        "preview": mask_key(active_key),
        "has_key": bool(active_key),
        "source": source,
        "stored_preview": mask_key(stored_key),
        "has_stored_key": bool(stored_key),
    }


def _restrict_permissions(path: Path) -> None:
    # On POSIX, mark the file 0600 so it isn't world-readable. No-op on
    # Windows where filesystem ACLs already restrict to the current user.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
