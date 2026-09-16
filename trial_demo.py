"""Optional, time-limited demo password and session primitives.

No web framework or network dependencies. Invalid demo settings fail closed
without preventing the independent public trial from starting.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping

COOKIE_NAME = "edb_demo"
PASSWORD_ITERATIONS = 600_000
MAX_PASSWORD_CHARS = 1024
MAX_SESSION_CHARS = 2048
MAX_WINDOW = timedelta(hours=72)
_HASH_PATTERN = re.compile(r"pbkdf2_sha256\$([0-9]{6,7})\$([0-9a-f]{32})\$([0-9a-f]{64})")


def _password_bytes(password: str) -> bytes | None:
    if not isinstance(password, str) or not 8 <= len(password) <= MAX_PASSWORD_CHARS:
        return None
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError:
        return None


def hash_password(password: str) -> str:
    """Hash an 8–1024 character password with a fresh salt; never retain plaintext."""
    raw = _password_bytes(password)
    if raw is None:
        raise ValueError("Password must contain 8–1024 valid Unicode characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw, salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"


def _hash_parts(encoded: str | None) -> tuple[int, bytes, bytes] | None:
    if not isinstance(encoded, str) or len(encoded) > 128:
        return None
    match = _HASH_PATTERN.fullmatch(encoded)
    if match is None:
        return None
    iterations = int(match[1])
    # Bound work even for a malformed/mistyped environment value.
    if not PASSWORD_ITERATIONS <= iterations <= 2_000_000:
        return None
    return iterations, bytes.fromhex(match[2]), bytes.fromhex(match[3])


def _aware(value: datetime | None) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _env_text(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _env_datetime(env: Mapping[str, str], name: str) -> datetime | None:
    value = _env_text(env, name)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if _aware(parsed) else None


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


@dataclass(frozen=True)
class DemoConfig:
    enabled: bool = False
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    password_hash: str | None = field(default=None, repr=False)
    signing_secret: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "DemoConfig":
        return cls(
            enabled=_env_text(env, "TRIAL_DEMO_ENABLED") == "1",
            starts_at=_env_datetime(env, "TRIAL_DEMO_STARTS_AT"),
            ends_at=_env_datetime(env, "TRIAL_DEMO_ENDS_AT"),
            password_hash=_env_text(env, "TRIAL_DEMO_PASSWORD_HASH"),
            signing_secret=_env_text(env, "TRIAL_DEMO_SESSION_SECRET"),
        )

    def configuration_errors(self) -> tuple[str, ...]:
        """Return setting-name-only diagnostics, never secret values."""
        if not self.enabled:
            return ()
        errors = []
        if not _aware(self.starts_at):
            errors.append("TRIAL_DEMO_STARTS_AT")
        if not _aware(self.ends_at):
            errors.append("TRIAL_DEMO_ENDS_AT")
        if _aware(self.starts_at) and _aware(self.ends_at):
            if not timedelta(0) < self.ends_at - self.starts_at <= MAX_WINDOW:
                errors.append("TRIAL_DEMO_WINDOW")
        if _hash_parts(self.password_hash) is None:
            errors.append("TRIAL_DEMO_PASSWORD_HASH")
        if not isinstance(self.signing_secret, str) or len(self.signing_secret) < 32:
            errors.append("TRIAL_DEMO_SESSION_SECRET")
        else:
            try:
                self.signing_secret.encode("utf-8")
            except UnicodeEncodeError:
                errors.append("TRIAL_DEMO_SESSION_SECRET")
        return tuple(errors)

    def is_configured(self) -> bool:
        return self.enabled and not self.configuration_errors()

    def is_active(self, now: datetime) -> bool:
        return self.is_configured() and _aware(now) and self.starts_at <= now < self.ends_at

    def verify_password(self, password: str) -> bool:
        raw = _password_bytes(password)
        if raw is None or not self.is_configured():
            return False
        iterations, salt, expected = _hash_parts(self.password_hash)
        actual = hashlib.pbkdf2_hmac("sha256", raw, salt, iterations)
        return hmac.compare_digest(actual, expected)

    def _binding(self) -> str:
        # Changing the password or either boundary revokes already issued sessions.
        material = json.dumps(
            [1, self.starts_at.astimezone(timezone.utc).isoformat(),
             self.ends_at.astimezone(timezone.utc).isoformat(), self.password_hash],
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self.signing_secret.encode("utf-8"), material, hashlib.sha256).hexdigest()

    def issue_session(self, now: datetime) -> str:
        if not self.is_active(now):
            raise ValueError("Demo is not active")
        payload = {
            "v": 1,
            "nonce": secrets.token_urlsafe(24),
            "exp": self.ends_at.timestamp(),
            "binding": self._binding(),
        }
        body = _encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        signature = hmac.new(self.signing_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        return f"{body}.{_encode(signature)}"

    def session_valid(self, token: str | None, now: datetime) -> bool:
        if not self.is_active(now) or not isinstance(token, str) or len(token) > MAX_SESSION_CHARS:
            return False
        try:
            body, encoded_signature = token.split(".")
            signature = _decode(encoded_signature)
            expected = hmac.new(self.signing_secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return False
            payload = json.loads(_decode(body))
            if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload["v"] != 1:
                return False
            expiry = payload.get("exp")
            if type(expiry) not in (int, float) or not math.isfinite(expiry):
                return False
            if not now.timestamp() < expiry <= self.ends_at.timestamp():
                return False
            nonce = payload.get("nonce")
            binding = payload.get("binding")
            return (
                isinstance(nonce, str) and len(nonce) == 32
                and isinstance(binding, str) and hmac.compare_digest(binding, self._binding())
            )
        except (ValueError, TypeError, UnicodeError, binascii.Error, OverflowError):
            return False
