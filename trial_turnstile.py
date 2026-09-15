"""Cloudflare Turnstile server-side validation with the standard library.

A token problem is a normal failed outcome (the visitor retries the
challenge). A broken secret, Cloudflare error, or network failure raises
TurnstileUnavailable so the server answers 503 instead of blaming the visitor.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_TOKEN_LENGTH = 2048
# Cloudflare's documented dummy keys, for local development only.
TEST_SITE_KEY_ALWAYS_PASSES = "1x00000000000000000000AA"
TEST_SECRET_ALWAYS_PASSES = "1x0000000000000000000000000000000AA"

Opener = Callable[[urllib.request.Request, float], Any]


class TurnstileUnavailable(RuntimeError):
    """Siteverify could not give a verdict about the token."""


@dataclass(frozen=True)
class TurnstileOutcome:
    success: bool
    error_codes: tuple[str, ...] = ()
    hostname: str | None = None


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


def verify_turnstile(
    token: str | None,
    *,
    secret: str,
    remote_ip: str | None,
    expected_hostnames: frozenset[str] | None = None,
    timeout: float = 5.0,
    opener: Opener | None = None,
) -> TurnstileOutcome:
    if not token or len(token) > MAX_TOKEN_LENGTH:
        # Siteverify does not enforce the documented 2048-character limit itself.
        return TurnstileOutcome(success=False, error_codes=("invalid-input-response",))

    fields = {"secret": secret, "response": token, "idempotency_key": str(uuid.uuid4())}
    if remote_ip:
        fields["remoteip"] = remote_ip
    request = urllib.request.Request(
        SITEVERIFY_URL,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with (opener or _default_opener)(request, timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        # Siteverify answers 400 only for a missing or invalid secret: a configuration fault.
        raise TurnstileUnavailable(f"siteverify returned {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise TurnstileUnavailable(f"siteverify failed: {error}") from error

    try:
        body = json.loads(raw)
    except ValueError as error:
        raise TurnstileUnavailable("siteverify returned a non-JSON body") from error
    if not isinstance(body, dict):
        raise TurnstileUnavailable("siteverify returned an unexpected body")

    error_codes = tuple(str(code) for code in (body.get("error-codes") or []))
    if any(code in {"internal-error", "missing-input-secret", "invalid-input-secret"} for code in error_codes):
        raise TurnstileUnavailable(f"siteverify error: {', '.join(error_codes)}")

    hostname = body.get("hostname") if isinstance(body.get("hostname"), str) else None
    if body.get("success") is not True:
        return TurnstileOutcome(success=False, error_codes=error_codes, hostname=hostname)

    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    testing_key = bool(metadata.get("result_with_testing_key"))
    if expected_hostnames and not testing_key and hostname not in expected_hostnames:
        return TurnstileOutcome(success=False, error_codes=("hostname-mismatch",), hostname=hostname)
    return TurnstileOutcome(success=True, error_codes=(), hostname=hostname)
