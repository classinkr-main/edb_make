import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from PIL import Image

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as error:  # the CI lock has no web dependencies until Plan 2
    raise unittest.SkipTest(f"trial web dependencies missing: {error}")

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult, PdfInfo, PdfUnreadableError
from structured_schema import Box
from trial_config import TrialConfig
from trial_quota import MemoryQuotaStore, QuotaUnavailable, hash_ip
from trial_server import create_app
from trial_turnstile import TurnstileOutcome, TurnstileUnavailable

PDF_BODY = b"%PDF-1.7\n" + b"0" * 200
NOW = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
TODAY = date(2026, 9, 15)


def _result(problem_count: int = 2) -> ParseResult:
    page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
    problems = [
        ParsedProblem(
            problem_id=f"q{index}",
            number=index,
            title=f"{index}.",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.0, top=10.0 + index * 100, width=200.0, height=80.0))],
            risk_flags=["marker_conflicts"] if index == 1 else [],
            image=Image.new("RGB", (200, 80), "white"),
        )
        for index in range(1, problem_count + 1)
    ]
    return ParseResult(pages=[page], problems=problems, source_page_count=16, parser_version="abc1234", timing_ms={"total": 5})


class FakeParser:
    def __init__(self, result=None, error=None, gate=None):
        self.result = result or _result()
        self.error = error
        self.gate = gate
        self.calls = []

    def __call__(self, source: Path, *, work_dir: Path, max_pages: int):
        self.calls.append({"source": source, "work_dir": work_dir, "max_pages": max_pages, "bytes": source.read_bytes()})
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return self.result


class FakeInspector:
    def __init__(self, info=None, error=None):
        self.info = info or PdfInfo(page_count=16, scanned_pages=3, pages_without_text=0, max_page_area_pt=595.0 * 842.0)
        self.error = error
        self.calls = []

    def __call__(self, source: Path, *, max_pages: int):
        self.calls.append(max_pages)
        if self.error is not None:
            raise self.error
        return self.info


class FakeVerifier:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome or TurnstileOutcome(success=True)
        self.error = error
        self.calls = []

    def __call__(self, token, remote_ip):
        self.calls.append((token, remote_ip))
        if self.error is not None:
            raise self.error
        return self.outcome


class FlakyEventStore(MemoryQuotaStore):
    def record_event(self, row):
        raise QuotaUnavailable("events down")


class CommitThenTimeoutStore(MemoryQuotaStore):
    """trial_consume committed on the server, but the HTTP reply never arrived."""

    def __init__(self):
        super().__init__()
        self.refund_calls = []

    def consume(self, **kwargs):
        super().consume(**kwargs)
        raise QuotaUnavailable("read timed out after commit")

    def refund(self, *, request_id):
        self.refund_calls.append(request_id)
        return super().refund(request_id=request_id)


class ExplodingStore(MemoryQuotaStore):
    def consume(self, **kwargs):
        raise RuntimeError("unexpected bug")


class TrialApiCase(unittest.TestCase):
    def make_client(self, *, config=None, store=None, parser=None, inspector=None, verifier=None):
        self.config = config or TrialConfig(ip_salt="salt", cron_secret="cron-secret")
        self.store = store if store is not None else MemoryQuotaStore()
        self.parser = parser or FakeParser()
        self.inspector = inspector or FakeInspector()
        self.verifier = verifier if verifier is not None else FakeVerifier()
        app = create_app(
            self.config,
            quota_store=self.store,
            parser=self.parser,
            inspector=self.inspector,
            verifier=self.verifier,
            now=lambda: NOW,
        )
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def post_pdf(self, client, body=PDF_BODY, *, ip="203.0.113.7", token="tok"):
        return client.post(
            "/api/parse",
            content=body,
            headers={"content-type": "application/pdf", "x-turnstile-token": token, "x-real-ip": ip},
        )

    def used(self, ip="203.0.113.7"):
        return self.store.used.get((TODAY, hash_ip(ip, salt="salt", day=TODAY)), 0)


class TestConfigAndHealth(TrialApiCase):
    def test_config_exposes_public_settings_only(self):
        config = TrialConfig(turnstile_site_key="site", turnstile_secret="very-secret", ip_salt="salt")
        client = self.make_client(config=config)
        response = client.get("/api/config")
        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual(
            {
                "inquiry_url": "https://classin.co.kr/contact",
                "turnstile_site_key": "site",
                "max_bytes": 4_000_000,
                "max_pages": 3,
                "daily_limit": 3,
            },
            response.json(),
        )
        self.assertNotIn("very-secret", response.text)

    def test_health_reports_readiness_without_secret_names(self):
        ready = self.make_client().get("/api/health").json()
        self.assertEqual("ok", ready["status"])
        self.assertTrue(ready["ready"])
        production = TrialConfig(production=True)
        not_ready = self.make_client(config=production).get("/api/health").json()
        self.assertFalse(not_ready["ready"])
        self.assertNotIn("SUPABASE", str(not_ready))


class TestParseSuccess(TrialApiCase):
    def test_parses_first_pages_and_consumes_one_use(self):
        client = self.make_client()
        response = self.post_pdf(client)
        body = response.json()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("no-store", response.headers["cache-control"])
        self.assertEqual(2, body["remaining_today"])
        self.assertEqual(16, body["source_page_count"])
        self.assertEqual(3, body["processed_page_limit"])
        self.assertEqual([1, 2], [problem["number"] for problem in body["problems"]])
        self.assertTrue(body["problems"][0]["needs_review"])
        self.assertEqual(1, self.used())
        self.assertEqual(3, self.parser.calls[0]["max_pages"])
        self.assertEqual(PDF_BODY, self.parser.calls[0]["bytes"])
        self.assertEqual([3], self.inspector.calls)
        self.assertEqual([("tok", "203.0.113.7")], self.verifier.calls)
        event = self.store.events[-1]
        self.assertEqual(
            {"kind": "parse", "status": 200, "source_pages": 16, "pages": 1, "problems": 2, "risk_problems": 1},
            {key: event[key] for key in ("kind", "status", "source_pages", "pages", "problems", "risk_problems")},
        )
        self.assertEqual(hash_ip("203.0.113.7", salt="salt", day=TODAY), event["ip_hash"])
        self.assertIsNone(event["reject_code"])
        self.assertEqual(len(PDF_BODY), event["bytes"])

    def test_work_directory_is_removed_after_request(self):
        client = self.make_client()
        self.post_pdf(client)
        self.assertFalse(self.parser.calls[0]["work_dir"].parent.exists())

    def test_forwarded_for_used_when_real_ip_missing(self):
        client = self.make_client()
        client.post(
            "/api/parse",
            content=PDF_BODY,
            headers={"x-turnstile-token": "tok", "x-forwarded-for": "198.51.100.9, 10.0.0.1"},
        )
        self.assertEqual(1, self.used("198.51.100.9"))

    def test_event_store_failure_does_not_break_response(self):
        client = self.make_client(store=FlakyEventStore())
        self.assertEqual(200, self.post_pdf(client).status_code)

    def test_response_and_event_carry_timing_and_instance(self):
        client = self.make_client()
        response = self.post_pdf(client)
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual({"total": 5}, payload["timing_ms"])
        self.assertRegex(payload["instance_id"], r"^[0-9a-f]{8}$")
        self.assertGreaterEqual(payload["instance_age_s"], 0)
        event = self.store.events[-1]
        self.assertEqual(payload["instance_id"], event["instance_id"])
        self.assertEqual(5, event["timing"]["total"])
        for key in ("encode", "parse_total"):
            self.assertIsInstance(event["timing"][key], int)
            self.assertGreaterEqual(event["timing"][key], 0)


class TestParseRejections(TrialApiCase):
    def assertRejected(self, response, status, code, feature=None):
        body = response.json()
        self.assertEqual(status, response.status_code, response.text)
        self.assertEqual(code, body["error"]["code"])
        self.assertEqual(feature, body["error"]["feature"])
        self.assertEqual("no-store", response.headers["cache-control"])
        return body

    def test_oversized_body(self):
        client = self.make_client()
        response = self.post_pdf(client, body=b"%PDF-" + b"0" * 4_000_000)
        self.assertRejected(response, 413, "too_large", "limit_size")
        self.assertEqual([], self.parser.calls)
        self.assertEqual(0, self.used())
        self.assertEqual("too_large", self.store.events[-1]["reject_code"])

    def test_image_upload(self):
        client = self.make_client()
        self.assertRejected(self.post_pdf(client, body=b"\x89PNG\r\n\x1a\n" + b"0" * 10), 415, "image_not_supported", "scan")
        self.assertEqual(0, self.used())

    def test_unknown_upload(self):
        client = self.make_client()
        self.assertRejected(self.post_pdf(client, body=b"hello"), 415, "bad_type")

    def test_unreadable_pdf(self):
        client = self.make_client(inspector=FakeInspector(error=PdfUnreadableError("locked")))
        self.assertRejected(self.post_pdf(client), 422, "unreadable_pdf")
        self.assertEqual(0, self.used())

    def test_textless_pdf(self):
        info = PdfInfo(page_count=4, scanned_pages=3, pages_without_text=2, max_page_area_pt=500_000.0)
        client = self.make_client(inspector=FakeInspector(info=info))
        self.assertRejected(self.post_pdf(client), 422, "no_text_layer", "scan")
        self.assertEqual([], self.parser.calls)
        self.assertEqual(0, self.used())
        self.assertEqual(4, self.store.events[-1]["source_pages"])

    def test_daily_limit_after_three_uses(self):
        client = self.make_client()
        statuses = [self.post_pdf(client).status_code for _ in range(3)]
        body = self.assertRejected(self.post_pdf(client), 429, "daily_limit", "limit_daily")
        self.assertEqual([200, 200, 200], statuses)
        self.assertEqual(0, body["remaining_today"])
        self.assertEqual(3, len(self.parser.calls))

    def test_quota_is_per_ip(self):
        client = self.make_client()
        for _ in range(3):
            self.post_pdf(client, ip="203.0.113.7")
        self.assertEqual(200, self.post_pdf(client, ip="203.0.113.8").status_code)

    def test_global_limit_answers_busy(self):
        client = self.make_client(config=TrialConfig(ip_salt="salt", global_daily_limit=1))
        self.post_pdf(client, ip="203.0.113.1")
        self.assertRejected(self.post_pdf(client, ip="203.0.113.2"), 503, "busy")

    def test_bot_check_failure(self):
        verifier = FakeVerifier(outcome=TurnstileOutcome(success=False, error_codes=("timeout-or-duplicate",)))
        client = self.make_client(verifier=verifier)
        self.assertRejected(self.post_pdf(client), 400, "bot_check_failed")
        self.assertEqual([], self.inspector.calls)
        self.assertEqual(0, self.used())

    def test_bot_check_unavailable(self):
        client = self.make_client(verifier=FakeVerifier(error=TurnstileUnavailable("down")))
        self.assertRejected(self.post_pdf(client), 503, "busy")

    def test_quota_store_unavailable_fails_closed(self):
        store = MemoryQuotaStore(available=False)
        client = self.make_client(store=store)
        self.assertRejected(self.post_pdf(client), 503, "busy")
        self.assertEqual([], self.parser.calls)

    def test_parser_crash_keeps_the_use_charged(self):
        # A PDF that crashes the parser still spent parse CPU; refunding it would let
        # one crafted file bypass both daily caps.
        client = self.make_client(parser=FakeParser(error=RuntimeError("boom")))
        body = self.assertRejected(self.post_pdf(client), 500, "parse_failed", "ai")
        self.assertNotIn("boom", str(body))
        self.assertEqual(1, self.used())
        self.assertEqual("parse_failed", self.store.events[-1]["reject_code"])

    def test_consume_with_unknown_outcome_is_refunded_by_request_id(self):
        store = CommitThenTimeoutStore()
        client = self.make_client(store=store)
        self.assertRejected(self.post_pdf(client), 503, "busy")
        self.assertEqual(1, len(store.refund_calls))
        self.assertIn(store.refund_calls[0], store.charges)
        self.assertEqual(0, self.used())
        self.assertEqual([], self.parser.calls)

    def test_unexpected_exception_returns_json_500_and_records_event(self):
        client = self.make_client(store=ExplodingStore())
        body = self.assertRejected(self.post_pdf(client), 500, "parse_failed", "ai")
        self.assertNotIn("unexpected bug", str(body))
        self.assertEqual("parse_failed", self.store.events[-1]["reject_code"])
        self.assertEqual(500, self.store.events[-1]["status"])

    def test_non_finite_coordinates_do_not_break_the_response(self):
        result = _result()
        result.problems[0].regions[0] = ParsedRegion(page_id="p1", bbox=Box(left=float("nan"), top=1.0, width=2.0, height=3.0))
        client = self.make_client(parser=FakeParser(result=result))
        response = self.post_pdf(client)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()["problems"][0]["regions"])

    def test_production_without_secrets_is_busy(self):
        client = self.make_client(config=TrialConfig(production=True, ip_salt="salt"))
        self.assertRejected(self.post_pdf(client), 503, "busy")
        self.assertEqual([], self.verifier.calls)
        self.assertEqual([], self.parser.calls)

    def test_not_ready_records_reason(self):
        client = self.make_client(config=TrialConfig(production=True, ip_salt="salt", cron_secret="cron-secret"))
        response = self.post_pdf(client)
        self.assertEqual(503, response.status_code)
        self.assertEqual("not_ready", self.store.events[-1]["reject_detail"])

    def test_rejections_still_carry_instance_id(self):
        client = self.make_client(config=TrialConfig(production=True, ip_salt="salt", cron_secret="cron-secret"))
        self.post_pdf(client)
        self.assertRegex(self.store.events[-1]["instance_id"], r"^[0-9a-f]{8}$")
        self.assertIsNone(self.store.events[-1]["timing"])

    def test_turnstile_outage_records_reason_and_does_not_charge(self):
        client = self.make_client(verifier=FakeVerifier(error=TurnstileUnavailable("down")))
        response = self.post_pdf(client)
        self.assertEqual(503, response.status_code)
        self.assertEqual("turnstile", self.store.events[-1]["reject_detail"])
        self.assertEqual(0, self.used())

    def test_quota_store_outage_records_reason(self):
        client = self.make_client(store=CommitThenTimeoutStore())
        response = self.post_pdf(client)
        self.assertEqual(503, response.status_code)
        self.assertEqual("quota_store", self.store.events[-1]["reject_detail"])

    def test_global_limit_records_reason(self):
        client = self.make_client(config=TrialConfig(ip_salt="salt", cron_secret="cron-secret", global_daily_limit=1))
        self.assertEqual(200, self.post_pdf(client, ip="203.0.113.1").status_code)
        response = self.post_pdf(client, ip="203.0.113.2")
        self.assertEqual(503, response.status_code)
        self.assertEqual("global_limit", self.store.events[-1]["reject_detail"])

    def test_slot_wait_timeout_records_reason_and_does_not_charge(self):
        gate = threading.Event()
        client = self.make_client(
            config=TrialConfig(ip_salt="salt", cron_secret="cron-secret", parse_concurrency=1, parse_wait_seconds=0.3),
            parser=FakeParser(gate=gate),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.post_pdf, client, ip="203.0.113.1")
            time.sleep(0.2)  # let the first request take the only slot
            second = self.post_pdf(client, ip="203.0.113.2")
            # Check right away: the still-blocked first request only finishes (and
            # records its own event) after gate.set() below, which would otherwise
            # shadow the busy event we're checking for here.
            self.assertEqual(503, second.status_code)
            self.assertEqual("slot_wait", self.store.events[-1]["reject_detail"])
            self.assertEqual(0, self.used("203.0.113.2"))
            gate.set()
            self.assertEqual(200, first.result().status_code)

    def test_parser_crash_stays_charged_and_has_no_detail(self):
        client = self.make_client(parser=FakeParser(error=RuntimeError("boom")))
        response = self.post_pdf(client)
        self.assertEqual(500, response.status_code)
        self.assertEqual("parse_failed", response.json()["error"]["code"])
        self.assertEqual(1, self.used())  # parsing started, so the use stays charged (spec §6)
        self.assertIsNone(self.store.events[-1]["reject_detail"])

    def test_success_and_ordinary_rejections_have_no_detail(self):
        client = self.make_client()
        self.post_pdf(client)
        self.assertIsNone(self.store.events[-1]["reject_detail"])
        self.post_pdf(client, body=b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        self.assertEqual("image_not_supported", self.store.events[-1]["reject_code"])
        self.assertIsNone(self.store.events[-1]["reject_detail"])
        for _ in range(3):
            self.post_pdf(client)
        self.assertEqual("daily_limit", self.store.events[-1]["reject_code"])
        self.assertIsNone(self.store.events[-1]["reject_detail"])

    def test_concurrency_limit_answers_busy_without_charging(self):
        gate = threading.Event()
        config = TrialConfig(ip_salt="salt", parse_concurrency=1, parse_wait_seconds=0.2)
        client = self.make_client(config=config, parser=FakeParser(gate=gate))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.post_pdf, client, ip="203.0.113.1")
            deadline = time.monotonic() + 5
            while not self.parser.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            second = self.post_pdf(client, ip="203.0.113.2")
            gate.set()
            self.assertEqual(200, first.result().status_code)
        self.assertRejected(second, 503, "busy")
        self.assertEqual(0, self.used("203.0.113.2"))
        self.assertEqual(1, self.used("203.0.113.1"))
        self.assertEqual(1, len(self.store.charges))

    def test_waiting_requests_are_not_charged_and_do_not_block_other_routes(self):
        gate = threading.Event()
        config = TrialConfig(ip_salt="salt", parse_concurrency=1, parse_wait_seconds=3.0)
        client = self.make_client(config=config, parser=FakeParser(gate=gate))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.post_pdf, client, ip="203.0.113.1")
            deadline = time.monotonic() + 5
            while not self.parser.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            second = pool.submit(self.post_pdf, client, ip="203.0.113.2")
            time.sleep(0.3)
            self.assertEqual(0, self.used("203.0.113.2"))
            started = time.monotonic()
            self.assertEqual(200, client.get("/api/health").status_code)
            self.assertLess(time.monotonic() - started, 1.0)
            gate.set()
            self.assertEqual(200, first.result().status_code)
            self.assertEqual(200, second.result().status_code)
        self.assertEqual(1, self.used("203.0.113.2"))


class TestDefaultDependencies(unittest.TestCase):
    def test_production_config_builds_turnstile_and_supabase_clients(self):
        config = TrialConfig(
            production=True,
            turnstile_site_key="site",
            turnstile_secret="secret",
            supabase_url="https://proj.supabase.co",
            supabase_secret_key="sb_secret_x",
            ip_salt="salt",
            cron_secret="cron",
            expected_hostnames=frozenset({"trial.example.com"}),
        )
        with mock.patch("trial_server.verify_turnstile", return_value=TurnstileOutcome(success=False)) as verify, \
                mock.patch("trial_server.SupabaseRest") as rest:
            client = TestClient(create_app(config, now=lambda: NOW))
            response = client.post("/api/parse", content=PDF_BODY, headers={"x-turnstile-token": "tok", "x-real-ip": "203.0.113.7"})
        self.assertEqual(400, response.status_code)
        rest.assert_called_once_with("https://proj.supabase.co", "sb_secret_x")
        verify.assert_called_once_with(
            "tok", secret="secret", remote_ip="203.0.113.7", expected_hostnames=frozenset({"trial.example.com"})
        )

    def test_local_config_skips_bot_check_and_uses_memory_quota(self):
        parser = FakeParser()
        client = TestClient(create_app(TrialConfig(), parser=parser, inspector=FakeInspector(), now=lambda: NOW))
        response = client.post("/api/parse", content=PDF_BODY)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(parser.calls))


class TestEvents(TrialApiCase):
    def test_valid_popup_event_is_recorded(self):
        client = self.make_client()
        response = client.post("/api/event", json={"feature": "edb", "action": "open"}, headers={"x-real-ip": "203.0.113.7"})
        self.assertEqual(204, response.status_code)
        event = self.store.events[-1]
        self.assertEqual(("popup", "edb", "open"), (event["kind"], event["feature"], event["action"]))
        self.assertEqual(hash_ip("203.0.113.7", salt="salt", day=TODAY), event["ip_hash"])

    def test_beacon_text_plain_body_is_accepted(self):
        client = self.make_client()
        response = client.post("/api/event", content=b'{"feature":"scan","action":"inquiry"}', headers={"content-type": "text/plain"})
        self.assertEqual(204, response.status_code)
        self.assertEqual("scan", self.store.events[-1]["feature"])

    def test_invalid_events_are_dropped_silently(self):
        client = self.make_client()
        for payload in (b"not json", b'{"feature":"rm -rf","action":"open"}', b'{"feature":"edb","action":"click"}', b"[1]", b"x" * 5000):
            self.assertEqual(204, client.post("/api/event", content=payload).status_code)
        self.assertEqual([], self.store.events)

    def test_event_rate_limit_per_ip(self):
        client = self.make_client()
        for _ in range(25):
            client.post("/api/event", json={"feature": "edb", "action": "open"}, headers={"x-real-ip": "203.0.113.7"})
        self.assertEqual(20, len(self.store.events))
        client.post("/api/event", json={"feature": "edb", "action": "open"}, headers={"x-real-ip": "203.0.113.8"})
        self.assertEqual(21, len(self.store.events))


class TestCron(TrialApiCase):
    def test_requires_bearer_secret(self):
        client = self.make_client()
        self.assertEqual(401, client.get("/api/cron/daily").status_code)
        self.assertEqual(401, client.get("/api/cron/daily", headers={"authorization": "Bearer wrong"}).status_code)
        self.assertEqual(401, client.get("/api/cron/daily", headers={"authorization": "cron-secret"}).status_code)

    def test_missing_secret_hides_endpoint(self):
        client = self.make_client(config=TrialConfig(ip_salt="salt"))
        self.assertEqual(404, client.get("/api/cron/daily", headers={"authorization": "Bearer "}).status_code)

    def test_runs_cleanup_and_ping(self):
        store = MemoryQuotaStore()
        store.used[(date(2026, 9, 1), "old")] = 1
        client = self.make_client(store=store)
        response = client.get("/api/cron/daily", headers={"authorization": "Bearer cron-secret"})
        self.assertEqual(200, response.status_code)
        self.assertEqual({"ok": True}, response.json())
        self.assertNotIn((date(2026, 9, 1), "old"), store.used)

    def test_reports_unhealthy_store(self):
        client = self.make_client(store=MemoryQuotaStore(available=False))
        response = client.get("/api/cron/daily", headers={"authorization": "Bearer cron-secret"})
        self.assertEqual(503, response.status_code)
        self.assertEqual({"ok": False}, response.json())


if __name__ == "__main__":
    unittest.main()
