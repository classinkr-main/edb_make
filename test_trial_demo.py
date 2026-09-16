import base64
import hashlib
import hmac
import importlib.util
import io
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from trial_demo import COOKIE_NAME, DemoConfig, hash_password

KST = timezone(timedelta(hours=9))
START = datetime(2026, 9, 17, tzinfo=KST)
END = datetime(2026, 9, 19, 18, tzinfo=KST)
NOW = START + timedelta(hours=1)
TEST_PASSWORD = "sample-only-password"
TEST_SECRET = "test-session-key-" * 3


class DemoCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.encoded = hash_password(TEST_PASSWORD)

    def config(self, **changes):
        return replace(DemoConfig(True, START, END, self.encoded, TEST_SECRET), **changes)

    def env(self, **changes):
        env = {
            "TRIAL_DEMO_ENABLED": "1",
            "TRIAL_DEMO_STARTS_AT": START.isoformat(),
            "TRIAL_DEMO_ENDS_AT": END.isoformat(),
            "TRIAL_DEMO_PASSWORD_HASH": self.encoded,
            "TRIAL_DEMO_SESSION_SECRET": TEST_SECRET,
        }
        env.update(changes)
        return env


class TestDemoConfig(DemoCase):
    def test_disabled_by_default(self):
        config = DemoConfig.from_env({})
        self.assertFalse(config.enabled)
        self.assertEqual((), config.configuration_errors())
        self.assertFalse(config.is_configured())
        self.assertFalse(config.is_active(NOW))
        self.assertFalse(config.verify_password(TEST_PASSWORD))
        self.assertFalse(config.session_valid("anything", NOW))
        with self.assertRaises(ValueError):
            config.issue_session(NOW)

    def test_only_explicit_one_enables_demo(self):
        for value in ("", "true", "yes", "0", "2"):
            with self.subTest(value=value):
                self.assertFalse(DemoConfig.from_env(self.env(TRIAL_DEMO_ENABLED=value)).enabled)

    def test_parses_complete_aware_configuration(self):
        config = DemoConfig.from_env(self.env())
        self.assertEqual(self.config(), config)
        self.assertTrue(config.is_configured())
        self.assertEqual((), config.configuration_errors())
        self.assertTrue(config.is_active(NOW.astimezone(timezone.utc)))
        self.assertEqual("edb_demo", COOKIE_NAME)
        self.assertNotIn(TEST_SECRET, repr(config))
        self.assertNotIn(self.encoded, repr(config))

    def test_active_window_includes_start_and_excludes_end(self):
        config = self.config()
        self.assertFalse(config.is_active(START - timedelta(microseconds=1)))
        self.assertTrue(config.is_active(START))
        self.assertTrue(config.is_active(END - timedelta(microseconds=1)))
        self.assertFalse(config.is_active(END))
        self.assertFalse(config.is_active(END + timedelta(hours=1)))
        self.assertFalse(config.is_active(NOW.replace(tzinfo=None)))

    def test_incomplete_or_malformed_environment_fails_closed_without_throwing(self):
        cases = [
            {"TRIAL_DEMO_ENABLED": "1"},
            self.env(TRIAL_DEMO_STARTS_AT="invalid"),
            self.env(TRIAL_DEMO_STARTS_AT="2026-09-17T00:00:00"),
            self.env(TRIAL_DEMO_ENDS_AT=""),
            self.env(TRIAL_DEMO_ENDS_AT=START.isoformat()),
            self.env(TRIAL_DEMO_ENDS_AT=(START - timedelta(hours=1)).isoformat()),
            self.env(TRIAL_DEMO_ENDS_AT=(START + timedelta(hours=72, seconds=1)).isoformat()),
            self.env(TRIAL_DEMO_PASSWORD_HASH="plaintext-is-not-a-hash"),
            self.env(TRIAL_DEMO_PASSWORD_HASH=self.encoded.replace("600000", "100000")),
            self.env(TRIAL_DEMO_PASSWORD_HASH=self.encoded.replace("600000", "9999999")),
            self.env(TRIAL_DEMO_PASSWORD_HASH="x" * 10000),
            self.env(TRIAL_DEMO_SESSION_SECRET="short"),
        ]
        for env in cases:
            with self.subTest(keys=tuple(env)):
                config = DemoConfig.from_env(env)
                self.assertTrue(config.configuration_errors())
                self.assertFalse(config.is_active(NOW))
                self.assertFalse(config.session_valid(None, NOW))
                self.assertFalse(config.verify_password(TEST_PASSWORD))
                with self.assertRaises(ValueError):
                    config.issue_session(NOW)

    def test_exactly_72_hours_allowed_and_direct_naive_configuration_rejected(self):
        self.assertTrue(self.config(ends_at=START + timedelta(hours=72)).is_configured())
        self.assertFalse(self.config(starts_at=START.replace(tzinfo=None)).is_configured())

    def test_unrelated_environment_values_are_not_consumed_or_modified(self):
        env = self.env(TRIAL_DEMO_STARTS_AT="broken")
        env["SUPABASE_SECRET_KEY"] = "unrelated-public-trial-setting"
        before = env.copy()
        config = DemoConfig.from_env(env)
        self.assertFalse(config.is_configured())
        self.assertEqual(before, env)
        self.assertNotIn("unrelated-public-trial-setting", repr(config))


class TestDemoPassword(DemoCase):
    def test_valid_and_wrong_password(self):
        self.assertTrue(self.config().verify_password(TEST_PASSWORD))
        self.assertFalse(self.config().verify_password("another-long-password"))
        self.assertNotIn(TEST_PASSWORD, self.encoded)
        self.assertEqual("600000", self.encoded.split("$")[1])

    def test_salts_differ_and_eight_character_password_is_supported(self):
        first = hash_password("example8")
        second = hash_password("example8")
        self.assertNotEqual(first, second)
        self.assertTrue(self.config(password_hash=first).verify_password("example8"))

    def test_length_and_invalid_unicode_are_bounded_before_pbkdf2(self):
        with mock.patch("trial_demo.hashlib.pbkdf2_hmac") as derive:
            for password in ("short", "x" * 1025, "broken-\ud800-password", None):
                with self.subTest(length=len(password) if isinstance(password, str) else None):
                    self.assertFalse(self.config().verify_password(password))
                    with self.assertRaises(ValueError):
                        hash_password(password)
            derive.assert_not_called()


class TestDemoSession(DemoCase):
    def test_valid_session_is_random_and_contains_no_password_or_hash(self):
        config = self.config()
        first = config.issue_session(NOW)
        second = config.issue_session(NOW)
        self.assertNotEqual(first, second)
        self.assertTrue(config.session_valid(first, NOW))
        self.assertTrue(config.session_valid(first, END - timedelta(seconds=1)))
        body = first.split(".")[0]
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
        self.assertNotIn(TEST_PASSWORD, decoded)
        self.assertNotIn(self.encoded, decoded)
        self.assertNotIn(TEST_SECRET, decoded)
        self.assertEqual(END.timestamp(), json.loads(decoded)["exp"])

    def test_expired_disabled_and_not_started_sessions_are_rejected(self):
        config = self.config()
        token = config.issue_session(START)
        self.assertTrue(config.session_valid(token, START))
        for now in (START - timedelta(seconds=1), END, END + timedelta(days=1), NOW.replace(tzinfo=None)):
            self.assertFalse(config.session_valid(token, now))
            with self.assertRaises(ValueError):
                config.issue_session(now)
        self.assertFalse(replace(config, enabled=False).session_valid(token, NOW))

    def test_config_changes_revoke_session(self):
        config = self.config()
        token = config.issue_session(NOW)
        variants = (
            replace(config, starts_at=START + timedelta(minutes=1)),
            replace(config, ends_at=END - timedelta(minutes=1)),
            replace(config, ends_at=END + timedelta(hours=1)),
            replace(config, password_hash=hash_password("changed-test-password")),
            replace(config, signing_secret="replacement-test-session-secret-123"),
        )
        for changed in variants:
            self.assertTrue(changed.is_active(NOW))
            self.assertFalse(changed.session_valid(token, NOW))

    def test_forged_tampered_and_malformed_tokens_are_safe_false(self):
        config = self.config()
        token = config.issue_session(NOW)
        body, signature = token.split(".")
        variants = (
            None, "", "wrong", ".", "a.b.c", "%%%.$$$", "unicode-한글.??",
            "x" * 10000, "!" + token, token + "!", body + "." + "A" * 43,
            body[:-1] + ("A" if body[-1] != "A" else "B") + "." + signature,
        )
        for value in variants:
            self.assertFalse(config.session_valid(value, NOW))

    def test_valid_signature_cannot_override_expiry_or_binding_rules(self):
        config = self.config()
        body = config.issue_session(NOW).split(".")[0]
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))

        def signed(value):
            encoded = base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
            sig = hmac.new(TEST_SECRET.encode(), encoded.encode(), hashlib.sha256).digest()
            return encoded + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()

        variants = [[], None, {**payload, "v": True}, {**payload, "nonce": "short"},
                    {**payload, "binding": "different"}, {**payload, "binding": "한글"}]
        for expiry in (END.timestamp() + 1, NOW.timestamp(), float("nan"), float("inf"), True, "tomorrow", None):
            variants.append({**payload, "exp": expiry})
        for variant in variants:
            self.assertFalse(config.session_valid(signed(variant), NOW))


class TestPasswordCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / "scripts" / "hash_trial_demo_password.py"
        spec = importlib.util.spec_from_file_location("demo_password_cli", path)
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    def run_cli(self, answers, argv=None):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(self.cli.sys, "argv", argv or ["hash_trial_demo_password.py"]), \
                mock.patch.object(self.cli.getpass, "getpass", side_effect=answers), \
                mock.patch("sys.stdout", output), mock.patch("sys.stderr", errors):
            code = self.cli.main()
        return code, output.getvalue(), errors.getvalue()

    def test_prints_only_hash_after_confirmation(self):
        code, output, errors = self.run_cli([TEST_PASSWORD, TEST_PASSWORD])
        self.assertEqual(0, code)
        self.assertEqual("", errors)
        self.assertEqual(1, len(output.splitlines()))
        self.assertTrue(output.startswith("pbkdf2_sha256$600000$"))
        self.assertNotIn(TEST_PASSWORD, output)

    def test_mismatch_and_short_password_print_no_hash(self):
        for answers in ([TEST_PASSWORD, "other-test-password"], ["short", "short"]):
            code, output, errors = self.run_cli(answers)
            self.assertEqual(2, code)
            self.assertEqual("", output)
            self.assertTrue(errors)

    def test_rejects_plaintext_argv(self):
        code, output, errors = self.run_cli([], ["hash_trial_demo_password.py", "do-not-pass-a-password"])
        self.assertEqual(2, code)
        self.assertEqual("", output)
        self.assertNotIn("do-not-pass-a-password", errors)


if __name__ == "__main__":
    unittest.main()
