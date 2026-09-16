import unittest

from trial_config import DEFAULT_INQUIRY_URL, TrialConfig
from trial_input import A3_AREA_PT

PRODUCTION_ENV = {
    "VERCEL_ENV": "production",
    "TRIAL_TURNSTILE_SITE_KEY": "site",
    "TRIAL_TURNSTILE_SECRET": "secret",
    "SUPABASE_URL": "https://proj.supabase.co",
    "SUPABASE_SECRET_KEY": "sb_secret_x",
    "TRIAL_IP_SALT": "salt",
    "CRON_SECRET": "cron-secret-value",
    "TRIAL_HOSTNAMES": "trial.example.com, www.trial.example.com ,",
}


class TestTrialConfig(unittest.TestCase):
    def test_defaults_without_environment(self):
        config = TrialConfig.from_env({})
        self.assertFalse(config.production)
        self.assertEqual(DEFAULT_INQUIRY_URL, config.inquiry_url)
        self.assertEqual("https://classin.co.kr/contact", config.inquiry_url)
        self.assertEqual(4_000_000, config.limits.max_bytes)
        self.assertEqual(4, config.limits.max_pages)
        self.assertEqual(100, config.limits.max_source_pages)
        self.assertAlmostEqual(2 * A3_AREA_PT, config.limits.max_page_area_pt)
        self.assertEqual(3, config.daily_limit)
        self.assertEqual(500, config.global_daily_limit)
        self.assertEqual(1, config.parse_concurrency)
        self.assertEqual(20.0, config.parse_wait_seconds)
        self.assertEqual(frozenset(), config.expected_hostnames)
        self.assertIsNone(config.turnstile_secret)
        self.assertEqual([], config.missing_production_settings())

    def test_reads_values_and_trims_hostnames(self):
        config = TrialConfig.from_env(
            {
                **PRODUCTION_ENV,
                "TRIAL_INQUIRY_URL": "https://example.com/contact",
                "TRIAL_MAX_BYTES": "1000",
                "TRIAL_MAX_PAGES": "2",
                "TRIAL_MAX_SOURCE_PAGES": "50",
                "TRIAL_DAILY_LIMIT": "5",
                "TRIAL_GLOBAL_DAILY_LIMIT": "900",
                "TRIAL_PARSE_CONCURRENCY": "1",
                "TRIAL_PARSE_WAIT_SECONDS": "7.5",
            }
        )
        self.assertTrue(config.production)
        self.assertEqual("https://example.com/contact", config.inquiry_url)
        self.assertEqual((1000, 2, 50), (config.limits.max_bytes, config.limits.max_pages, config.limits.max_source_pages))
        self.assertEqual((5, 900, 1, 7.5), (config.daily_limit, config.global_daily_limit, config.parse_concurrency, config.parse_wait_seconds))
        self.assertEqual(frozenset({"trial.example.com", "www.trial.example.com"}), config.expected_hostnames)
        self.assertEqual([], config.missing_production_settings())

    def test_production_lists_missing_secrets(self):
        config = TrialConfig.from_env({"VERCEL_ENV": "production", "SUPABASE_URL": "https://proj.supabase.co"})
        self.assertEqual(
            [
                "TRIAL_TURNSTILE_SITE_KEY",
                "TRIAL_TURNSTILE_SECRET",
                "SUPABASE_SECRET_KEY",
                "TRIAL_IP_SALT",
                "CRON_SECRET",
            ],
            config.missing_production_settings(),
        )

    def test_preview_is_not_production(self):
        self.assertFalse(TrialConfig.from_env({"VERCEL_ENV": "preview"}).production)

    def test_blank_values_count_as_missing(self):
        config = TrialConfig.from_env({**PRODUCTION_ENV, "TRIAL_IP_SALT": "  "})
        self.assertEqual(["TRIAL_IP_SALT"], config.missing_production_settings())

    def test_invalid_numbers_raise(self):
        for name, value in (("TRIAL_MAX_PAGES", "0"), ("TRIAL_DAILY_LIMIT", "abc"), ("TRIAL_PARSE_CONCURRENCY", "-1")):
            with self.assertRaises(ValueError, msg=name):
                TrialConfig.from_env({name: value})

    def test_non_https_inquiry_url_raises(self):
        with self.assertRaises(ValueError):
            TrialConfig.from_env({"TRIAL_INQUIRY_URL": "javascript:alert(1)"})

    def test_complexity_limits_from_env(self):
        config = TrialConfig.from_env({"TRIAL_MAX_WORDS_PER_PAGE": "1234", "TRIAL_MAX_DRAWINGS_PER_PAGE": "567"})
        self.assertEqual(1234, config.limits.max_words_per_page)
        self.assertEqual(567, config.limits.max_drawings_per_page)
        defaults = TrialConfig.from_env({})
        self.assertEqual(4500, defaults.limits.max_words_per_page)
        self.assertEqual(2500, defaults.limits.max_drawings_per_page)
        # The dataclass defaults and the from_env defaults are the same numbers, named once.
        self.assertEqual(TrialConfig().limits, TrialConfig.from_env({}).limits)


if __name__ == "__main__":
    unittest.main()
