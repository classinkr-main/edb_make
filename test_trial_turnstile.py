import io
import json
import unittest
import urllib.error
import urllib.parse

from trial_turnstile import (
    MAX_TOKEN_LENGTH,
    SITEVERIFY_URL,
    TurnstileOutcome,
    TurnstileUnavailable,
    verify_turnstile,
)


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class RecordingOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _json_response(body: dict) -> FakeResponse:
    return FakeResponse(json.dumps(body).encode("utf-8"))


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(SITEVERIFY_URL, code, "error", {}, io.BytesIO(json.dumps(body).encode("utf-8")))


class TestVerifyTurnstile(unittest.TestCase):
    def test_posts_form_with_secret_token_ip_and_idempotency_key(self):
        opener = RecordingOpener(_json_response({"success": True, "error-codes": [], "hostname": "trial.example.com"}))
        outcome = verify_turnstile("tok", secret="sec", remote_ip="203.0.113.7", opener=opener, timeout=3.0)
        request, timeout = opener.requests[0]
        form = urllib.parse.parse_qs(request.data.decode("ascii"))
        self.assertEqual(TurnstileOutcome(success=True, error_codes=(), hostname="trial.example.com"), outcome)
        self.assertEqual(SITEVERIFY_URL, request.full_url)
        self.assertEqual("POST", request.get_method())
        self.assertEqual("application/x-www-form-urlencoded", request.get_header("Content-type"))
        self.assertEqual(["sec"], form["secret"])
        self.assertEqual(["tok"], form["response"])
        self.assertEqual(["203.0.113.7"], form["remoteip"])
        self.assertEqual(36, len(form["idempotency_key"][0]))
        self.assertEqual(3.0, timeout)

    def test_remote_ip_is_optional(self):
        opener = RecordingOpener(_json_response({"success": True}))
        verify_turnstile("tok", secret="sec", remote_ip=None, opener=opener)
        form = urllib.parse.parse_qs(opener.requests[0][0].data.decode("ascii"))
        self.assertNotIn("remoteip", form)

    def test_token_failures_are_outcomes_not_exceptions(self):
        opener = RecordingOpener(_json_response({"success": False, "error-codes": ["timeout-or-duplicate"]}))
        outcome = verify_turnstile("tok", secret="sec", remote_ip=None, opener=opener)
        self.assertFalse(outcome.success)
        self.assertEqual(("timeout-or-duplicate",), outcome.error_codes)

    def test_empty_or_oversized_token_fails_without_network(self):
        for token in ("", None, "a" * (MAX_TOKEN_LENGTH + 1)):
            opener = RecordingOpener(AssertionError("must not call siteverify"))
            outcome = verify_turnstile(token, secret="sec", remote_ip=None, opener=opener)
            self.assertFalse(outcome.success)
            self.assertEqual(("invalid-input-response",), outcome.error_codes)
            self.assertEqual([], opener.requests)

    def test_token_at_max_length_is_sent(self):
        opener = RecordingOpener(_json_response({"success": True}))
        self.assertTrue(verify_turnstile("a" * MAX_TOKEN_LENGTH, secret="sec", remote_ip=None, opener=opener).success)

    def test_hostname_mismatch_fails(self):
        opener = RecordingOpener(_json_response({"success": True, "hostname": "evil.example"}))
        outcome = verify_turnstile(
            "tok", secret="sec", remote_ip=None, opener=opener, expected_hostnames=frozenset({"trial.example.com"})
        )
        self.assertFalse(outcome.success)
        self.assertEqual(("hostname-mismatch",), outcome.error_codes)

    def test_testing_key_results_skip_hostname_check(self):
        body = {"success": True, "hostname": "example.com", "metadata": {"result_with_testing_key": True}}
        outcome = verify_turnstile(
            "tok",
            secret="sec",
            remote_ip=None,
            opener=RecordingOpener(_json_response(body)),
            expected_hostnames=frozenset({"trial.example.com"}),
        )
        self.assertTrue(outcome.success)

    def test_invalid_secret_http_400_is_unavailable(self):
        opener = RecordingOpener(_http_error(400, {"success": False, "error-codes": ["invalid-input-secret"]}))
        with self.assertRaises(TurnstileUnavailable):
            verify_turnstile("tok", secret="bad", remote_ip=None, opener=opener)

    def test_internal_error_code_is_unavailable(self):
        opener = RecordingOpener(_json_response({"success": False, "error-codes": ["internal-error"]}))
        with self.assertRaises(TurnstileUnavailable):
            verify_turnstile("tok", secret="sec", remote_ip=None, opener=opener)

    def test_network_and_decoding_failures_are_unavailable(self):
        for failure in (urllib.error.URLError("down"), TimeoutError(), FakeResponse(b"<html>")):
            with self.assertRaises(TurnstileUnavailable):
                verify_turnstile("tok", secret="sec", remote_ip=None, opener=RecordingOpener(failure))


if __name__ == "__main__":
    unittest.main()
