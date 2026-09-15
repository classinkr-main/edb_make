"""Daily quotas and funnel events for the web trial, stored in Supabase.

Talks to PostgREST with the standard library only. Every network or
protocol failure surfaces as QuotaUnavailable so the server can fail closed.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

KST = timezone(timedelta(hours=9))
GLOBAL_SUBJECT = "__global__"
USER_AGENT = "edb-parser-trial/1"

Opener = Callable[[urllib.request.Request, float], Any]


class QuotaUnavailable(RuntimeError):
    """Supabase could not be reached or answered unexpectedly."""


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    remaining: int
    reason: str | None


def kst_day(now: datetime) -> date:
    if now.tzinfo is None:
        raise ValueError("kst_day needs an aware datetime")
    return now.astimezone(KST).date()


def hash_ip(ip: str, *, salt: str, day: date) -> str:
    """Hash an IP with a secret salt and the day so rows cannot be linked across days."""
    digest = hashlib.sha256(f"{salt}|{day.isoformat()}|{ip}".encode("utf-8")).hexdigest()
    return digest[:16]


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


class SupabaseRest:
    """Minimal PostgREST client authenticated with a Supabase secret key."""

    def __init__(self, base_url: str, secret_key: str, *, timeout: float = 5.0, opener: Opener | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret_key = secret_key
        self._timeout = timeout
        self._opener = opener or _default_opener

    def _send(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        prefer: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        headers = {
            # Secret keys go on apikey only; Supabase rejects them in Authorization: Bearer.
            "apikey": self._secret_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if prefer:
            headers["Prefer"] = prefer
        try:
            # Built inside the try: a malformed SUPABASE_URL raises ValueError here.
            request = urllib.request.Request(self._base_url + path, data=data, method=method, headers=headers)
            with self._opener(request, self._timeout if timeout is None else timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise QuotaUnavailable(f"supabase {method} {path} returned {error.code}") from error
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as error:
            # HTTPException covers IncompleteRead and BadStatusLine, which are not OSErrors.
            raise QuotaUnavailable(f"supabase {method} {path} failed: {error!r}") from error

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as error:
            raise QuotaUnavailable("supabase returned a non-JSON body") from error

    def rpc(self, name: str, args: dict[str, Any]) -> Any:
        return self._decode(self._send("POST", f"/rest/v1/rpc/{name}", args))

    def insert(self, table: str, row: dict[str, Any], *, timeout: float | None = None) -> None:
        self._send("POST", f"/rest/v1/{table}", row, prefer="return=minimal", timeout=timeout)

    def select_one(self, table: str, column: str) -> Any:
        return self._decode(self._send("GET", f"/rest/v1/{table}?select={column}&limit=1"))


class SupabaseQuotaStore:
    def __init__(self, rest: Any, *, event_timeout: float = 2.0) -> None:
        self._rest = rest
        # Events are best effort and awaited before the response, so keep them short.
        self._event_timeout = event_timeout

    def consume(self, *, request_id: str, day: date, subject: str, limit: int, global_limit: int) -> QuotaDecision:
        rows = self._rest.rpc(
            "trial_consume",
            {
                "p_request_id": request_id,
                "p_day": day.isoformat(),
                "p_subject": subject,
                "p_limit": limit,
                "p_global_limit": global_limit,
            },
        )
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise QuotaUnavailable("unexpected trial_consume response")
        row = rows[0]
        try:
            return QuotaDecision(allowed=bool(row["allowed"]), remaining=int(row["remaining"]), reason=row.get("reason"))
        except (KeyError, TypeError, ValueError) as error:
            raise QuotaUnavailable("unexpected trial_consume row") from error

    def refund(self, *, request_id: str) -> bool:
        """Undo the charge made under request_id, if there was one; safe to call when unsure."""
        return self._rest.rpc("trial_refund", {"p_request_id": request_id}) is True

    def cleanup(self, *, today: date) -> None:
        self._rest.rpc("trial_cleanup", {"p_today": today.isoformat()})

    def record_event(self, row: dict[str, Any]) -> None:
        self._rest.insert("trial_events", row, timeout=self._event_timeout)

    def ping(self) -> bool:
        try:
            self._rest.select_one("trial_quota", "day")
        except QuotaUnavailable:
            return False
        return True


@dataclass
class MemoryQuotaStore:
    """In-process store with the SQL functions' semantics, for tests and local runs."""

    used: dict[tuple[date, str], int] = field(default_factory=dict)
    charges: dict[str, tuple[date, str, bool]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    available: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def _check(self) -> None:
        if not self.available:
            raise QuotaUnavailable("memory store switched off")

    def consume(self, *, request_id: str, day: date, subject: str, limit: int, global_limit: int) -> QuotaDecision:
        self._check()
        with self._lock:
            global_used = self.used.get((day, GLOBAL_SUBJECT), 0)
            subject_used = self.used.get((day, subject), 0)
            if request_id in self.charges:
                return QuotaDecision(True, max(limit - subject_used, 0), "duplicate")
            if global_used >= global_limit:
                return QuotaDecision(False, 0, "global")
            if subject_used >= limit:
                return QuotaDecision(False, 0, "ip")
            self.used[(day, GLOBAL_SUBJECT)] = global_used + 1
            self.used[(day, subject)] = subject_used + 1
            self.charges[request_id] = (day, subject, False)
            return QuotaDecision(True, limit - subject_used - 1, None)

    def refund(self, *, request_id: str) -> bool:
        self._check()
        with self._lock:
            charge = self.charges.get(request_id)
            if charge is None or charge[2]:
                return False
            day, subject, _ = charge
            self.charges[request_id] = (day, subject, True)
            for key in ((day, subject), (day, GLOBAL_SUBJECT)):
                if key in self.used:
                    self.used[key] = max(self.used[key] - 1, 0)
            return True

    def cleanup(self, *, today: date) -> None:
        self._check()
        cutoff = today - timedelta(days=7)
        with self._lock:
            self.used = {key: value for key, value in self.used.items() if key[0] >= cutoff}
            self.charges = {key: value for key, value in self.charges.items() if value[0] >= cutoff}

    def record_event(self, row: dict[str, Any]) -> None:
        self._check()
        self.events.append(dict(row))

    def ping(self) -> bool:
        return self.available
