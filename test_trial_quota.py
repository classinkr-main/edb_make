import http.client
import io
import json
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone

from trial_quota import (
    GLOBAL_SUBJECT,
    MemoryQuotaStore,
    QuotaDecision,
    QuotaUnavailable,
    SupabaseQuotaStore,
    SupabaseRest,
    hash_ip,
    kst_day,
)


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class RecordingOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.supabase.co", code, "error", {}, io.BytesIO(b'{"message":"x"}'))


class TestKeys(unittest.TestCase):
    def test_kst_day_rolls_over_at_kst_midnight(self):
        self.assertEqual(date(2026, 9, 15), kst_day(datetime(2026, 9, 15, 14, 59, tzinfo=timezone.utc)))
        self.assertEqual(date(2026, 9, 16), kst_day(datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)))

    def test_kst_day_rejects_naive_datetimes(self):
        with self.assertRaises(ValueError):
            kst_day(datetime(2026, 9, 15, 12, 0))

    def test_hash_ip_is_stable_short_and_day_salted(self):
        first = hash_ip("203.0.113.7", salt="s", day=date(2026, 9, 15))
        self.assertEqual(first, hash_ip("203.0.113.7", salt="s", day=date(2026, 9, 15)))
        self.assertEqual(16, len(first))
        self.assertNotEqual(first, hash_ip("203.0.113.7", salt="s", day=date(2026, 9, 16)))
        self.assertNotEqual(first, hash_ip("203.0.113.7", salt="t", day=date(2026, 9, 15)))
        self.assertNotIn("203", first)

    def test_hash_ip_never_collides_with_global_subject(self):
        self.assertNotEqual(GLOBAL_SUBJECT, hash_ip(GLOBAL_SUBJECT, salt="", day=date(2026, 9, 15)))


class TestSupabaseRest(unittest.TestCase):
    def test_rpc_posts_json_with_secret_apikey_and_no_bearer(self):
        opener = RecordingOpener(FakeResponse(b'[{"allowed": true, "remaining": 2, "reason": null}]'))
        rest = SupabaseRest("https://proj.supabase.co/", "sb_secret_x", timeout=4.0, opener=opener)
        rows = rest.rpc("trial_consume", {"p_day": "2026-09-15"})
        request, timeout = opener.requests[0]
        self.assertEqual([{"allowed": True, "remaining": 2, "reason": None}], rows)
        self.assertEqual("https://proj.supabase.co/rest/v1/rpc/trial_consume", request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual("sb_secret_x", request.get_header("Apikey"))
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual("application/json", request.get_header("Content-type"))
        self.assertTrue(request.get_header("User-agent").startswith("edb-parser-trial/"))
        self.assertEqual({"p_day": "2026-09-15"}, json.loads(request.data))
        self.assertEqual(4.0, timeout)

    def test_empty_body_from_void_function_is_none(self):
        rest = SupabaseRest("https://proj.supabase.co", "k", opener=RecordingOpener(FakeResponse(b"", status=204)))
        self.assertIsNone(rest.rpc("trial_refund", {}))

    def test_insert_prefers_minimal_return(self):
        opener = RecordingOpener(FakeResponse(b"", status=201))
        SupabaseRest("https://proj.supabase.co", "k", opener=opener).insert("trial_events", {"kind": "parse"})
        request, _ = opener.requests[0]
        self.assertEqual("https://proj.supabase.co/rest/v1/trial_events", request.full_url)
        self.assertEqual("return=minimal", request.get_header("Prefer"))

    def test_select_one_uses_get(self):
        opener = RecordingOpener(FakeResponse(b"[]"))
        self.assertEqual([], SupabaseRest("https://proj.supabase.co", "k", opener=opener).select_one("trial_quota", "day"))
        request, _ = opener.requests[0]
        self.assertEqual("GET", request.get_method())
        self.assertEqual("https://proj.supabase.co/rest/v1/trial_quota?select=day&limit=1", request.full_url)
        self.assertIsNone(request.data)

    def test_http_and_network_errors_become_quota_unavailable(self):
        failures = (
            _http_error(500),
            _http_error(401),
            urllib.error.URLError("down"),
            TimeoutError(),
            http.client.IncompleteRead(b"{"),
            http.client.BadStatusLine("garbage"),
        )
        for failure in failures:
            rest = SupabaseRest("https://proj.supabase.co", "k", opener=RecordingOpener(failure))
            with self.assertRaises(QuotaUnavailable, msg=repr(failure)):
                rest.rpc("trial_consume", {})

    def test_reading_a_truncated_body_becomes_quota_unavailable(self):
        class TruncatedResponse(FakeResponse):
            def read(self, *args):
                raise http.client.IncompleteRead(b"[{")

        rest = SupabaseRest("https://proj.supabase.co", "k", opener=RecordingOpener(TruncatedResponse(b"")))
        with self.assertRaises(QuotaUnavailable):
            rest.rpc("trial_consume", {})

    def test_malformed_base_url_becomes_quota_unavailable(self):
        rest = SupabaseRest("proj.supabase.co", "k", opener=RecordingOpener(AssertionError("must not be called")))
        with self.assertRaises(QuotaUnavailable):
            rest.rpc("trial_consume", {})

    def test_invalid_json_becomes_quota_unavailable(self):
        rest = SupabaseRest("https://proj.supabase.co", "k", opener=RecordingOpener(FakeResponse(b"<html>paused</html>")))
        with self.assertRaises(QuotaUnavailable):
            rest.rpc("trial_consume", {})

    def test_insert_can_use_a_shorter_timeout(self):
        opener = RecordingOpener(FakeResponse(b"", status=201))
        SupabaseRest("https://proj.supabase.co", "k", timeout=5.0, opener=opener).insert("trial_events", {"kind": "parse"}, timeout=1.5)
        self.assertEqual(1.5, opener.requests[0][1])


class FakeRest:
    def __init__(self, rpc_result=None):
        self.calls = []
        self.rpc_result = rpc_result

    def rpc(self, name, args):
        self.calls.append(("rpc", name, args))
        return self.rpc_result

    def insert(self, table, row, timeout=None):
        self.calls.append(("insert", table, row, timeout))

    def select_one(self, table, column):
        self.calls.append(("select_one", table, column))
        return []


REQUEST_ID = "00000000-0000-4000-8000-000000000001"


class TestSupabaseQuotaStore(unittest.TestCase):
    def test_consume_sends_request_id_and_maps_row(self):
        rest = FakeRest([{"allowed": False, "remaining": 0, "reason": "ip"}])
        decision = SupabaseQuotaStore(rest).consume(
            request_id=REQUEST_ID, day=date(2026, 9, 15), subject="abc", limit=3, global_limit=500
        )
        self.assertEqual(QuotaDecision(allowed=False, remaining=0, reason="ip"), decision)
        self.assertEqual(
            (
                "rpc",
                "trial_consume",
                {"p_request_id": REQUEST_ID, "p_day": "2026-09-15", "p_subject": "abc", "p_limit": 3, "p_global_limit": 500},
            ),
            rest.calls[0],
        )

    def test_consume_rejects_unexpected_shape(self):
        for bad in (None, [], [{}], {"allowed": True}, [{"allowed": True, "remaining": 1}, {"allowed": True, "remaining": 1}]):
            with self.assertRaises(QuotaUnavailable):
                SupabaseQuotaStore(FakeRest(bad)).consume(
                    request_id=REQUEST_ID, day=date(2026, 9, 15), subject="a", limit=3, global_limit=5
                )

    def test_refund_returns_whether_a_charge_was_undone(self):
        self.assertTrue(SupabaseQuotaStore(FakeRest(True)).refund(request_id=REQUEST_ID))
        self.assertFalse(SupabaseQuotaStore(FakeRest(False)).refund(request_id=REQUEST_ID))
        rest = FakeRest(True)
        SupabaseQuotaStore(rest).refund(request_id=REQUEST_ID)
        self.assertEqual(("rpc", "trial_refund", {"p_request_id": REQUEST_ID}), rest.calls[0])

    def test_cleanup_event_and_ping(self):
        rest = FakeRest()
        store = SupabaseQuotaStore(rest, event_timeout=1.5)
        store.cleanup(today=date(2026, 9, 15))
        store.record_event({"kind": "popup", "feature": "edb", "action": "open"})
        self.assertTrue(store.ping())
        self.assertEqual(
            [
                ("rpc", "trial_cleanup", {"p_today": "2026-09-15"}),
                ("insert", "trial_events", {"kind": "popup", "feature": "edb", "action": "open"}, 1.5),
                ("select_one", "trial_quota", "day"),
            ],
            rest.calls,
        )


class TestMemoryQuotaStore(unittest.TestCase):
    """The in-memory store mirrors the SQL functions so server tests exercise real limit semantics."""

    def test_matches_sql_semantics(self):
        store = MemoryQuotaStore()
        day = date(2026, 9, 15)
        results = [
            store.consume(request_id=f"a{index}", day=day, subject="a", limit=3, global_limit=4) for index in range(4)
        ]
        self.assertEqual(
            [
                QuotaDecision(True, 2, None),
                QuotaDecision(True, 1, None),
                QuotaDecision(True, 0, None),
                QuotaDecision(False, 0, "ip"),
            ],
            results,
        )
        self.assertEqual(QuotaDecision(True, 2, None), store.consume(request_id="b0", day=day, subject="b", limit=3, global_limit=4))
        self.assertEqual(QuotaDecision(False, 0, "global"), store.consume(request_id="c0", day=day, subject="c", limit=3, global_limit=4))
        self.assertTrue(store.refund(request_id="b0"))
        self.assertFalse(store.refund(request_id="b0"))
        self.assertFalse(store.refund(request_id="c0"))
        self.assertFalse(store.refund(request_id="never"))
        self.assertEqual(QuotaDecision(True, 2, None), store.consume(request_id="c1", day=day, subject="c", limit=3, global_limit=4))
        self.assertEqual(4, store.used[(day, "__global__")])
        self.assertGreaterEqual(min(store.used.values()), 0)

    def test_duplicate_request_id_charges_once(self):
        store = MemoryQuotaStore()
        day = date(2026, 9, 15)
        self.assertEqual(QuotaDecision(True, 2, None), store.consume(request_id="x", day=day, subject="a", limit=3, global_limit=9))
        self.assertEqual(QuotaDecision(True, 2, "duplicate"), store.consume(request_id="x", day=day, subject="a", limit=3, global_limit=9))
        self.assertEqual(1, store.used[(day, "a")])

    def test_events_ping_cleanup_and_unavailable_switch(self):
        store = MemoryQuotaStore()
        store.record_event({"kind": "parse"})
        self.assertEqual([{"kind": "parse"}], store.events)
        self.assertTrue(store.ping())
        store.consume(request_id="old", day=date(2026, 9, 1), subject="a", limit=3, global_limit=9)
        store.cleanup(today=date(2026, 9, 15))
        self.assertEqual({}, store.used)
        self.assertEqual({}, store.charges)
        store.available = False
        self.assertFalse(store.ping())
        with self.assertRaises(QuotaUnavailable):
            store.consume(request_id="z", day=date(2026, 9, 15), subject="a", limit=3, global_limit=5)
        with self.assertRaises(QuotaUnavailable):
            store.refund(request_id="z")
        with self.assertRaises(QuotaUnavailable):
            store.record_event({"kind": "parse"})


if __name__ == "__main__":
    unittest.main()
