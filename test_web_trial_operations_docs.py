"""Guard docs/web-trial-operations.md against drifting from the code it documents.

Not a behavior test -- the operations doc is prose for people, not something the
runtime reads. These assertions exist so a future change to an env var name, a
migration column, or the sentinel value used to flag pathological PDF pages
fails a test instead of silently going stale in the doc.
"""

import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DOC = (PROJECT_ROOT / "docs" / "web-trial-operations.md").read_text(encoding="utf-8")


class TestWorkerEnvVarsDocumented(unittest.TestCase):
    """Two different env vars cap two different stages -- the doc must name both."""

    def test_asset_worker_env_var_name_matches_code(self):
        source = (PROJECT_ROOT / "build_problem_board_edb.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("EDB_PROBLEM_ASSET_WORKERS"', source)
        self.assertIn("EDB_PROBLEM_ASSET_WORKERS", DOC)

    def test_preprocess_worker_env_var_name_matches_code(self):
        source = (PROJECT_ROOT / "preprocess.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("EDB_PREPROCESS_PAGE_WORKERS"', source)
        self.assertIn("EDB_PREPROCESS_PAGE_WORKERS", DOC)

    def test_doc_distinguishes_which_stage_each_worker_var_bounds(self):
        # The render stage (preprocess.py) and the crop/asset stage
        # (build_problem_board_edb.py) are documented as separate levers.
        preprocess_idx = DOC.index("EDB_PREPROCESS_PAGE_WORKERS")
        asset_idx = DOC.index("EDB_PROBLEM_ASSET_WORKERS")
        window = DOC[min(preprocess_idx, asset_idx):max(preprocess_idx, asset_idx) + 400]
        self.assertIn("render", window)
        self.assertTrue("assets" in window or "crop" in window)


class TestDeploymentOrderingDocumented(unittest.TestCase):
    """Spec 4-2: migration before code deploy, or PostgREST silently drops inserts."""

    def test_ordering_rule_stated_at_least_twice(self):
        # Once in first-time setup, once in the post-deploy checklist.
        occurrences = DOC.count("마이그레이션")
        self.assertGreaterEqual(occurrences, 2)
        self.assertIn("PostgREST", DOC)

    def test_information_schema_verification_query_present(self):
        self.assertIn("information_schema.columns", DOC)
        migration = (
            PROJECT_ROOT / "supabase" / "migrations" / "20260915000000_web_trial.sql"
        ).read_text(encoding="utf-8")
        added_columns = re.findall(r"add column if not exists (\w+)", migration)
        self.assertEqual({"timing", "instance_id", "reject_detail", "complexity"}, set(added_columns))
        for column in added_columns:
            self.assertIn(column, DOC)


class TestStageTimingSqlHasP95PerStage(unittest.TestCase):
    def test_each_stage_has_both_percentiles(self):
        for stage in ("render", "segment", "assets", "encode"):
            self.assertIn(f"(timing->>'{stage}')::int) as {stage}_p50", DOC)
            self.assertIn(f"(timing->>'{stage}')::int) as {stage}_p95", DOC)


class TestComplexityQueryExcludesPathologicalSentinel(unittest.TestCase):
    def test_sentinel_value_matches_code(self):
        from problem_parser import PATHOLOGICAL_COUNT_SENTINEL

        self.assertEqual(1_000_000_000, PATHOLOGICAL_COUNT_SENTINEL)
        self.assertIn(str(PATHOLOGICAL_COUNT_SENTINEL), DOC)

    def test_doc_explains_sentinel_rows_were_refused_not_dense(self):
        idx = DOC.index(str(1_000_000_000))
        window = DOC[max(0, idx - 600):idx + 600]
        self.assertIn("거절", window)


class TestWorstCaseCostComputedCorrectly(unittest.TestCase):
    """The doc's figure must actually be the arithmetic it claims, not a guess."""

    def test_monthly_worst_case_matches_spec_unit_prices(self):
        active_cpu_per_hour = 0.128
        memory_per_gb_hour = 0.0106
        seconds_per_request = 15
        gb = 2
        requests_per_day = 500
        days_per_month = 30

        per_request = (
            seconds_per_request * (active_cpu_per_hour / 3600)
            + gb * (seconds_per_request / 3600) * memory_per_gb_hour
        )
        monthly = per_request * requests_per_day * days_per_month

        self.assertAlmostEqual(9.325, monthly, places=2)
        # The doc must show a monthly figure consistent with this computation,
        # not an unrelated or stale number.
        rendered = f"{monthly:.2f}".rstrip("0").rstrip(".")
        self.assertTrue(
            any(candidate in DOC for candidate in (f"${rendered}", f"{rendered}", "9.33", "9.3")),
            "operations doc worst-case monthly figure does not match the computed value",
        )

    def test_doc_relates_worst_case_to_spend_management(self):
        self.assertIn("Spend Management", DOC)
        cost_idx = DOC.index("500")
        self.assertIn("0.128", DOC)
        self.assertIn("0.0106", DOC)


class TestExpoDemoEnvVarsDocumented(unittest.TestCase):
    def test_all_demo_env_vars_listed(self):
        for name in (
            "TRIAL_DEMO_ENABLED",
            "TRIAL_DEMO_STARTS_AT",
            "TRIAL_DEMO_ENDS_AT",
            "TRIAL_DEMO_PASSWORD_HASH",
            "TRIAL_DEMO_SESSION_SECRET",
        ):
            self.assertIn(name, DOC)

    def test_password_hash_script_referenced(self):
        self.assertIn("scripts/hash_trial_demo_password.py", DOC)
        self.assertTrue((PROJECT_ROOT / "scripts" / "hash_trial_demo_password.py").is_file())

    def test_demo_stays_off_until_configured_is_stated(self):
        self.assertTrue(
            "꺼진" in DOC or "닫힌" in DOC,
            "doc should say the demo stays off/closed until the env vars validate",
        )

    def test_vercel_rewrite_for_demo_route_is_documented(self):
        vercel = (PROJECT_ROOT / "vercel.json").read_text(encoding="utf-8")
        self.assertIn('"/demo"', vercel)
        demo_section = DOC[DOC.index("박람회"):]
        self.assertIn("vercel.json", demo_section)
        self.assertIn("rewrite", demo_section.lower())


if __name__ == "__main__":
    unittest.main()
