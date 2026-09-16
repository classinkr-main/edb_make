"""Web trial server (Vercel Python function).

create_app() wires upload checks, Turnstile, Supabase quotas, the parser, and
response encoding. Static pages live in public/ and are served by Vercel's
CDN, not by this app.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from problem_parser import PdfUnreadableError, inspect_pdf, parse_problems, parser_version
from trial_config import TrialConfig
from trial_demo import COOKIE_NAME
from trial_input import REJECTIONS, TrialRejected, check_pdf_info, check_upload_head, reject
from trial_preview import PreviewBudgetExceeded, build_parse_body
from trial_quota import MemoryQuotaStore, QuotaUnavailable, SupabaseQuotaStore, SupabaseRest, hash_ip, kst_day
from trial_turnstile import TurnstileUnavailable, verify_turnstile

DEV_IP_SALT = "local-development-salt"
EVENT_FEATURES = frozenset({"edb", "image", "edit", "ai", "scan", "limit_pages", "limit_size", "limit_daily"})
EVENT_ACTIONS = frozenset({"open", "inquiry"})
EVENT_MAX_BYTES = 1024
EVENT_RATE_PER_MINUTE = 20
NO_STORE = {"Cache-Control": "no-store"}
SLOT_POLL_SECONDS = 0.05

logger = logging.getLogger("trial_server")

# One id per Python process: shows instance churn and cold starts in trial_events.
INSTANCE_ID = uuid.uuid4().hex[:8]
INSTANCE_STARTED_AT = time.time()


def _ms(started_at: float) -> int:
    return int(round((time.perf_counter() - started_at) * 1000))


def client_ip(request: Request) -> str:
    # Vercel overwrites x-real-ip and x-forwarded-for, so clients cannot spoof them.
    real_ip = (request.headers.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


async def _read_limited(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise reject("too_large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise reject("too_large")
    return bytes(body)


class _EventRateLimiter:
    """Per-instance sliding window; good enough to stop one client flooding events."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self._per_minute:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] <= 60}
            return True


def _default_quota_store(config: TrialConfig) -> Any:
    if config.supabase_url and config.supabase_secret_key:
        return SupabaseQuotaStore(SupabaseRest(config.supabase_url, config.supabase_secret_key))
    if config.production:
        return None
    return MemoryQuotaStore()


def _default_verifier(config: TrialConfig) -> Callable[[str | None, str], Any] | None:
    if not config.turnstile_secret:
        return None

    def verify(token: str | None, remote_ip: str) -> Any:
        return verify_turnstile(
            token,
            secret=config.turnstile_secret,
            remote_ip=remote_ip,
            expected_hostnames=config.expected_hostnames,
        )

    return verify


def create_app(
    config: TrialConfig,
    *,
    quota_store: Any = None,
    verifier: Callable[[str | None, str], Any] | None = None,
    parser: Callable[..., Any] = parse_problems,
    inspector: Callable[..., Any] = inspect_pdf,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = quota_store if quota_store is not None else _default_quota_store(config)
    verify = verifier if verifier is not None else _default_verifier(config)
    parse_slots = threading.BoundedSemaphore(config.parse_concurrency)
    event_limiter = _EventRateLimiter(EVENT_RATE_PER_MINUTE)
    login_limiter = _EventRateLimiter(5)
    login_total_limiter = _EventRateLimiter(30)
    login_slots = threading.BoundedSemaphore(2)
    salt = config.ip_salt or DEV_IP_SALT

    def ready() -> bool:
        return store is not None and not config.missing_production_settings()

    def record_event(row: dict[str, Any]) -> None:
        if store is None:
            return
        try:
            store.record_event(row)
        except Exception:  # events are best effort; the visitor still gets a response
            logger.warning("trial event not recorded", exc_info=True)

    async def acquire_parse_slot() -> bool:
        # Poll on the event loop instead of blocking a worker thread: Fluid compute
        # shares one process, and a blocked thread per queued request would starve
        # anyio's 40-thread pool that every other route and Supabase call needs.
        deadline = time.monotonic() + config.parse_wait_seconds
        while not parse_slots.acquire(blocking=False):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(SLOT_POLL_SECONDS)
        return True

    def parse_and_encode(
        source: Path, work_dir: Path, remaining_today: int | None, started_at: float, demo_mode: bool = False
    ) -> tuple[bytes, dict[str, int], dict[str, int]]:
        parse_started_at = time.perf_counter()
        try:
            result = parser(source, work_dir=work_dir, max_pages=config.limits.max_pages)
            timing: dict[str, int] = dict(result.timing_ms)
            encode_started_at = time.perf_counter()
            payload, body = build_parse_body(
                result,
                remaining_today=remaining_today,
                elapsed_ms=_ms(started_at),
                processed_page_limit=config.limits.max_pages,
                extra={
                    "timing_ms": dict(timing),
                    "instance_id": INSTANCE_ID,
                    "instance_age_s": round(time.time() - INSTANCE_STARTED_AT, 1),
                    **({"mode": "demo"} if demo_mode else {}),
                },
            )
            # encode is measured after the body exists, so the response cannot include it; the event does.
            timing["encode"] = _ms(encode_started_at)
            timing["parse_total"] = _ms(parse_started_at)
        except PreviewBudgetExceeded as error:
            raise reject("page_too_complex", "response_budget") from error
        except Exception as error:
            logger.exception("trial parse failed")
            raise reject("parse_failed") from error
        counts = {
            "pages": len(payload["pages"]),
            "problems": len(payload["problems"]),
            "risk_problems": sum(1 for problem in payload["problems"] if problem["needs_review"]),
        }
        return body, counts, timing

    def refund_quietly(request_id: str) -> None:
        try:
            store.refund(request_id=request_id)
        except Exception:
            logger.warning("trial refund failed", exc_info=True)

    def rejection_response(rejection: Any, event: dict[str, Any], detail: str | None = None) -> JSONResponse:
        event.update(status=rejection.status, reject_code=rejection.code, reject_detail=detail)
        extra = {"remaining_today": 0} if rejection.code == "daily_limit" else {}
        return JSONResponse(rejection.payload(**extra), status_code=rejection.status, headers=NO_STORE)

    @app.get("/api/config")
    async def public_config() -> JSONResponse:
        return JSONResponse(
            {
                "inquiry_url": config.inquiry_url,
                "turnstile_site_key": config.turnstile_site_key,
                "max_bytes": config.limits.max_bytes,
                "max_pages": config.limits.max_pages,
                "daily_limit": config.daily_limit,
            },
            headers=NO_STORE,
        )

    def demo_error(code: str, *, status: int | None = None) -> JSONResponse:
        rejection = REJECTIONS[code]
        return JSONResponse(rejection.payload(), status_code=status or rejection.status, headers=NO_STORE)

    def demo_request_is_same_origin(request: Request) -> bool:
        # The custom header prevents cross-origin form submissions, including
        # requests without Origin. No cross-origin preflight is permitted.
        if request.headers.get("x-demo-request") != "1":
            return False
        if request.headers.get("sec-fetch-site") == "cross-site":
            return False
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                return False
            scheme = "https" if config.production else request.url.scheme
            return parsed.scheme == scheme and parsed.netloc == request.url.netloc and parsed.path in ("", "/")
        return True

    def demo_authenticated(request: Request) -> bool:
        return config.demo.session_valid(request.cookies.get(COOKIE_NAME), now())

    @app.get("/api/demo/config")
    async def demo_config(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "inquiry_url": config.inquiry_url,
                "turnstile_site_key": None,
                "max_bytes": config.limits.max_bytes,
                "max_pages": config.limits.max_pages,
                "daily_limit": None,
                "mode": "demo",
                "active": config.demo.is_active(now()),
                "authenticated": demo_authenticated(request),
                "ends_at": config.demo.ends_at.isoformat() if config.demo.ends_at else None,
            },
            headers=NO_STORE,
        )

    @app.post("/api/demo/login")
    async def demo_login(request: Request) -> Response:
        if not demo_request_is_same_origin(request):
            return demo_error("demo_unavailable")
        if not config.demo.is_active(now()):
            return demo_error("demo_unavailable")
        ip = client_ip(request)
        instant = time.monotonic()
        if not login_limiter.allow(ip, instant) or not login_total_limiter.allow("login", instant):
            return JSONResponse(
                {"error": {"code": "demo_login_limited", "message": "잠시 후 비밀번호를 다시 입력해 주세요."}},
                status_code=429, headers={**NO_STORE, "Retry-After": "60"},
            )
        try:
            data = json.loads(await _read_limited(request, 4096))
        except (TrialRejected, ValueError):
            return demo_error("demo_auth_required")
        password = data.get("password") if isinstance(data, dict) else None
        if not isinstance(password, str) or len(password) > 1024:
            return demo_error("demo_auth_required")
        if not login_slots.acquire(blocking=False):
            return demo_error("busy")
        try:
            valid = await run_in_threadpool(config.demo.verify_password, password)
        finally:
            login_slots.release()
        if not valid:
            return demo_error("demo_auth_required")
        instant_now = now()
        # Recheck after password verification so no session is minted at expiry.
        if not config.demo.is_active(instant_now):
            return demo_error("demo_unavailable")
        response = JSONResponse(
            {"authenticated": True, "ends_at": config.demo.ends_at.isoformat()}, headers=NO_STORE,
        )
        response.set_cookie(
            COOKIE_NAME, config.demo.issue_session(instant_now),
            max_age=max(0, int((config.demo.ends_at - instant_now).total_seconds())),
            expires=config.demo.ends_at.astimezone(timezone.utc),
            path="/", secure=config.production or request.url.scheme == "https", httponly=True, samesite="strict",
        )
        return response

    @app.post("/api/demo/logout")
    async def demo_logout(request: Request) -> Response:
        if not demo_request_is_same_origin(request):
            return demo_error("demo_unavailable")
        response = Response(status_code=204, headers=NO_STORE)
        response.delete_cookie(COOKIE_NAME, path="/", secure=config.production or request.url.scheme == "https", httponly=True, samesite="strict")
        return response

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "commit": parser_version(),
                "ready": ready(),
                "instance_id": INSTANCE_ID,
                "instance_age_s": round(time.time() - INSTANCE_STARTED_AT, 1),
            },
            headers=NO_STORE,
        )

    async def parse_request(request: Request, *, demo_mode: bool = False) -> Response:
        started_at = time.perf_counter()
        request_id = str(uuid.uuid4())
        ip = client_ip(request)
        today = kst_day(now())
        subject = hash_ip(ip, salt=salt, day=today)
        event: dict[str, Any] = {
            "kind": "parse",
            "status": None,
            "reject_code": None,
            "reject_detail": None,
            "source_pages": None,
            "pages": None,
            "problems": None,
            "risk_problems": None,
            "bytes": None,
            "elapsed_ms": None,
            "ip_hash": subject,
            "timing": None,
            "instance_id": INSTANCE_ID,
            "complexity": None,
        }
        try:
            if demo_mode:
                if not demo_request_is_same_origin(request):
                    raise reject("demo_unavailable")
                if not demo_authenticated(request):
                    raise reject("demo_auth_required")
            elif not ready():
                raise reject("busy", "not_ready")
            body = await _read_limited(request, config.limits.max_bytes)
            event["bytes"] = len(body)
            if verify is not None and not demo_mode:
                try:
                    outcome = await run_in_threadpool(verify, request.headers.get("x-turnstile-token"), ip)
                except TurnstileUnavailable as error:
                    logger.warning("turnstile unavailable: %s", error)
                    raise reject("busy", "turnstile") from error
                if not outcome.success:
                    raise reject("bot_check_failed")
            check_upload_head(body[:1024])
            with tempfile.TemporaryDirectory(prefix="trial-") as temp_dir:
                source = Path(temp_dir) / "upload.pdf"
                source.write_bytes(body)
                # Inspection also interprets PDF content and can inflate streams, so
                # bound it with the same slot as parsing. Waiting and invalid input
                # still never count against anyone's daily limit.
                if not await acquire_parse_slot():
                    raise reject("busy", "slot_wait")
                try:
                    try:
                        info = await run_in_threadpool(inspector, source, max_pages=config.limits.max_pages)
                    except PdfUnreadableError as error:
                        raise reject("unreadable_pdf") from error
                    event["source_pages"] = info.page_count
                    event["complexity"] = {"words": info.max_words_per_page, "drawings": info.max_drawings_per_page}
                    check_pdf_info(info, config.limits)
                    if demo_mode:
                        # Queued uploads must not start after the event expires.
                        if not demo_authenticated(request):
                            raise reject("demo_auth_required")
                        body, counts, timing = await run_in_threadpool(
                            parse_and_encode, source, Path(temp_dir) / "work", None, started_at, True
                        )
                    else:
                        body, counts, timing = await charge_and_parse(
                            request_id, today, subject, source, Path(temp_dir) / "work", started_at
                        )
                finally:
                    parse_slots.release()
            event.update(status=200, timing=timing, **counts)
            response = Response(body, media_type="application/json", headers=NO_STORE)
        except TrialRejected as rejected:
            response = rejection_response(rejected.rejection, event, rejected.detail)
        except Exception:
            logger.exception("trial parse crashed")
            response = rejection_response(REJECTIONS["parse_failed"], event)
        if demo_mode:
            event["timing"] = {**(event["timing"] or {}), "demo": True}
        event["elapsed_ms"] = int(round((time.perf_counter() - started_at) * 1000))
        await run_in_threadpool(record_event, event)
        logger.info(json.dumps({"event": "trial_parse", **{k: v for k, v in event.items() if k != "ip_hash"}}))
        return response

    async def charge_and_parse(request_id, today, subject, source, work_dir, started_at):
        try:
            decision = await run_in_threadpool(
                lambda: store.consume(
                    request_id=request_id, day=today, subject=subject,
                    limit=config.daily_limit, global_limit=config.global_daily_limit,
                )
            )
        except QuotaUnavailable as error:
            # Consumption may have committed before its reply was lost.
            logger.warning("quota unavailable: %s", error)
            await run_in_threadpool(refund_quietly, request_id)
            raise reject("busy", "quota_store") from error
        if not decision.allowed:
            if decision.reason == "ip":
                raise reject("daily_limit")
            raise reject("busy", "global_limit")
        # CPU was spent even if parsing fails, so keep the charge.
        return await run_in_threadpool(
            parse_and_encode, source, work_dir, decision.remaining, started_at
        )

    @app.post("/api/parse")
    async def parse(request: Request) -> Response:
        return await parse_request(request)

    @app.post("/api/demo/parse")
    async def demo_parse(request: Request) -> Response:
        return await parse_request(request, demo_mode=True)

    @app.post("/api/event")
    async def popup_event(request: Request) -> Response:
        try:
            raw = await _read_limited(request, EVENT_MAX_BYTES)
            data = json.loads(raw)
        except (TrialRejected, ValueError):
            return Response(status_code=204)
        if not isinstance(data, dict) or data.get("feature") not in EVENT_FEATURES or data.get("action") not in EVENT_ACTIONS:
            return Response(status_code=204)
        ip = client_ip(request)
        if not event_limiter.allow(ip, time.monotonic()):
            return Response(status_code=204)
        row = {
            "kind": "popup",
            "feature": data["feature"],
            "action": data["action"],
            "ip_hash": hash_ip(ip, salt=salt, day=kst_day(now())),
        }
        await run_in_threadpool(record_event, row)
        return Response(status_code=204)

    @app.get("/api/cron/daily")
    async def cron_daily(request: Request) -> JSONResponse:
        if not config.cron_secret:
            return JSONResponse({"error": "not_found"}, status_code=404)
        provided = request.headers.get("authorization", "")
        if not hmac.compare_digest(provided.encode(), f"Bearer {config.cron_secret}".encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401, headers=NO_STORE)
        if store is None or not await run_in_threadpool(store.ping):
            logger.error("trial cron: quota store unreachable")
            return JSONResponse({"ok": False}, status_code=503, headers=NO_STORE)
        try:
            await run_in_threadpool(lambda: store.cleanup(today=kst_day(now())))
        except QuotaUnavailable:
            logger.error("trial cron: cleanup failed", exc_info=True)
            return JSONResponse({"ok": False}, status_code=503, headers=NO_STORE)
        return JSONResponse({"ok": True}, headers=NO_STORE)

    return app


app = create_app(TrialConfig.from_env(os.environ))
