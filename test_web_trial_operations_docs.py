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

    def test_ordering_rule_stated_in_both_setup_and_post_deploy_sections(self):
        # Requirement 3 asks for the ordering line in BOTH the first-time setup
        # section and the post-deploy checklist -- bind to each half separately
        # so deleting either callout (and its verification query) fails loudly.
        split = DOC.index("## 3. 배포 후 점검")
        setup_section = DOC[:split]
        post_deploy_section = DOC[split:]

        self.assertGreaterEqual(DOC.count("마이그레이션"), 2)
        self.assertIn("PostgREST", setup_section)
        self.assertIn("PostgREST", post_deploy_section)
        self.assertIn("information_schema.columns", setup_section)
        self.assertIn("information_schema.columns", post_deploy_section)
        self.assertEqual(2, DOC.count("information_schema.columns"))

    def test_information_schema_verification_query_present(self):
        migration = (
            PROJECT_ROOT / "supabase" / "migrations" / "20260915000000_web_trial.sql"
        ).read_text(encoding="utf-8")
        added_columns = re.findall(r"add column if not exists (\w+)", migration)
        self.assertEqual({"timing", "instance_id", "reject_detail", "complexity"}, set(added_columns))

        # Pull the column list out of the doc's own query (both occurrences)
        # instead of just checking the names appear somewhere in the doc --
        # the names already occur independently elsewhere (timing, complexity,
        # instance_id, reject_detail are all documented as trial_events columns).
        query_column_lists = re.findall(
            r"information_schema\.columns.*?column_name in \(([^)]*)\)", DOC, re.S
        )
        self.assertEqual(
            2,
            len(query_column_lists),
            "expected the verification query once in setup and once post-deploy",
        )
        for column_list in query_column_lists:
            doc_columns = set(re.findall(r"'(\w+)'", column_list))
            self.assertEqual(set(added_columns), doc_columns)


class TestStageTimingSqlHasP95PerStage(unittest.TestCase):
    def test_each_stage_has_both_percentiles(self):
        # Assert the whole percentile_cont(...) line, not just the tail after
        # "as {stage}_p95" -- otherwise a line that computes 0.5 but labels it
        # _p95 (the exact defect requirement 4 exists to prevent) would pass.
        for stage in ("render", "segment", "assets", "encode"):
            self.assertIn(
                f"percentile_cont(0.5) within group (order by (timing->>'{stage}')::int) as {stage}_p50",
                DOC,
            )
            self.assertIn(
                f"percentile_cont(0.95) within group (order by (timing->>'{stage}')::int) as {stage}_p95",
                DOC,
            )

    def test_complexity_percentiles_have_correct_arguments(self):
        # The four complexity-query percentiles (words/drawings p50/p95) had
        # no coverage at all.
        for metric in ("words", "drawings"):
            self.assertIn(
                f"percentile_cont(0.5) within group (order by (complexity->>'{metric}')::bigint) as {metric}_p50",
                DOC,
            )
            self.assertIn(
                f"percentile_cont(0.95) within group (order by (complexity->>'{metric}')::bigint) as {metric}_p95",
                DOC,
            )


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
    """The doc's figures must actually be the arithmetic they claim, not a guess."""

    def _compute_worst_case(self):
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
        daily = per_request * requests_per_day
        monthly = daily * days_per_month
        return per_request, daily, monthly

    def test_monthly_worst_case_matches_spec_unit_prices(self):
        _, _, monthly = self._compute_worst_case()
        self.assertAlmostEqual(9.325, monthly, places=2)

        # Bind to the doc's own rendering of the monthly figure instead of a
        # hardcoded fallback tuple -- a fallback like "9.3" makes the check
        # pass for $9.30 or $9.39 too, which is not what was computed.
        match = re.search(r"월\(30일\).*?\*\*\$([0-9.]+)\*\*", DOC)
        self.assertIsNotNone(match, "could not find the doc's monthly worst-case figure")
        self.assertAlmostEqual(monthly, float(match.group(1)), delta=0.01)

    def test_all_three_worst_case_figures_match_computation(self):
        # The per-request and daily figures were previously asserted by
        # nothing; mutating $0.31 -> $3.10 or $0.00062 -> $0.00620 left the
        # suite green. Extract all three bolded figures from the §4-6 bullet
        # list in doc order and compare each numerically to what was computed.
        section = DOC[DOC.index("### 4-6"):]
        figures = re.findall(r"\*\*\$([0-9.]+)\*\*", section)
        self.assertEqual(3, len(figures), "expected per-request, daily, and monthly bolded figures")
        per_request_doc, daily_doc, monthly_doc = (float(value) for value in figures)

        per_request, daily, monthly = self._compute_worst_case()
        # Deltas are scaled to each figure's own printed precision so a
        # 10x-off mutation of any single figure is rejected.
        self.assertAlmostEqual(per_request, per_request_doc, delta=0.000005)
        self.assertAlmostEqual(daily, daily_doc, delta=0.005)
        self.assertAlmostEqual(monthly, monthly_doc, delta=0.01)

    def test_doc_relates_worst_case_to_spend_management(self):
        self.assertIn("Spend Management", DOC)
        self.assertIn("500", DOC)
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
        # "닫힌" alone is not a safe anchor: it already existed pre-change at
        # a different, unrelated sentence about TRIAL_DEMO_ENABLED=0 (outside
        # the demo section's fail-closed claim), so that phrase check passed
        # even against the base doc and would keep passing if the new
        # fail-closed sentence were deleted. Anchor to the actual sentence,
        # inside the demo section, and cross-check it against the code it
        # claims to describe (trial_demo.DemoConfig).
        demo_section = DOC[DOC.index("## 8. 박람회 전용 시연"):]
        self.assertIn(
            "시연은 꺼진 상태로 남고",
            demo_section,
            "the demo section should state the demo stays off until all five vars validate",
        )

        from trial_demo import DemoConfig

        # TRIAL_DEMO_ENABLED=1 alone (the other four vars blank/unset) must
        # not configure the demo -- this is the behavioral claim the doc makes.
        config = DemoConfig.from_env({"TRIAL_DEMO_ENABLED": "1"})
        self.assertFalse(config.is_configured())
        self.assertEqual(
            {
                "TRIAL_DEMO_STARTS_AT",
                "TRIAL_DEMO_ENDS_AT",
                "TRIAL_DEMO_PASSWORD_HASH",
                "TRIAL_DEMO_SESSION_SECRET",
            },
            set(config.configuration_errors()),
        )

    def test_vercel_rewrite_for_demo_route_is_documented(self):
        vercel = (PROJECT_ROOT / "vercel.json").read_text(encoding="utf-8")
        self.assertIn('"/demo"', vercel)
        demo_section = DOC[DOC.index("박람회"):]
        self.assertIn("vercel.json", demo_section)
        self.assertIn("rewrite", demo_section.lower())


if __name__ == "__main__":
    unittest.main()
