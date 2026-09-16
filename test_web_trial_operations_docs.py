"""Guard docs/web-trial-operations.md against drifting from the code it documents.

Not a behavior test -- the operations doc is prose for people, not something the
runtime reads. These assertions exist so a future change to an env var name, a
migration column, or the sentinel value used to flag pathological PDF pages
fails a test instead of silently going stale in the doc.
"""

import importlib
import os
import re
import unittest
from pathlib import Path
from unittest import mock

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


class TestWorkerEnvVarOutOfRangeBehaviourMatchesDoc(unittest.TestCase):
    """A <=0 value pins the pool to ONE worker; only a non-integer restores the default.

    The doc used to fold both halves into one claim -- "0 이하이거나 정수가 아닌
    값은 조용히 기본값으로 되돌아간다" -- which is wrong for the <=0 half. Both
    resolvers short-circuit with `if requested_workers <= 0: return 1`, so an
    operator who sets either var to 0 as an "auto/off" idiom fully serialises the
    stage instead of getting the default back. These assertions run the real
    resolvers so the doc cannot drift back to the wrong claim.

    os.cpu_count() is patched because the default is `min(cap, work, cores)` --
    unpatched, the expected defaults below would depend on the host's core count.
    """

    # (env var, module, resolver, work count passed in, default when 10 cores are visible)
    CASES = (
        ("EDB_PREPROCESS_PAGE_WORKERS", "preprocess", "_resolve_preprocess_page_worker_count", 4, 4),
        ("EDB_PROBLEM_ASSET_WORKERS", "build_problem_board_edb", "_resolve_problem_asset_worker_count", 6, 6),
    )

    def _resolve(self, module_name, resolver_name, work_count, env_name, env_value):
        module = importlib.import_module(module_name)
        resolver = getattr(module, resolver_name)
        with mock.patch.object(os, "cpu_count", return_value=10):
            with mock.patch.dict(os.environ, {}, clear=False):
                if env_value is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = env_value
                return resolver(work_count)

    def test_unset_gives_the_documented_default(self):
        # Pins the baseline the other two tests are measured against; without it
        # a resolver that returned 1 for everything would satisfy them trivially.
        for env_name, module_name, resolver_name, work_count, default in self.CASES:
            with self.subTest(env_name=env_name):
                self.assertEqual(
                    default, self._resolve(module_name, resolver_name, work_count, env_name, None)
                )

    def test_zero_or_negative_forces_one_worker_not_the_default(self):
        for env_name, module_name, resolver_name, work_count, default in self.CASES:
            for env_value in ("0", "-1", "-3"):
                with self.subTest(env_name=env_name, env_value=env_value):
                    resolved = self._resolve(
                        module_name, resolver_name, work_count, env_name, env_value
                    )
                    self.assertEqual(
                        1,
                        resolved,
                        f"{env_name}={env_value} must pin the pool to 1 (fully serial)",
                    )
                    # The distinction the doc got wrong: this is NOT the default.
                    self.assertNotEqual(default, resolved)

    def test_non_integer_falls_back_to_the_default(self):
        for env_name, module_name, resolver_name, work_count, default in self.CASES:
            for env_value in ("abc", "4.5", "  "):
                with self.subTest(env_name=env_name, env_value=env_value):
                    self.assertEqual(
                        default,
                        self._resolve(module_name, resolver_name, work_count, env_name, env_value),
                    )

    def test_doc_rows_state_the_split_and_never_merge_it_again(self):
        for env_name, _module_name, _resolver_name, _work_count, _default in self.CASES:
            with self.subTest(env_name=env_name):
                row_start = DOC.index(f"| `{env_name}`")
                row = DOC[row_start : DOC.index("\n", row_start)]

                self.assertIn("0 이하", row)
                # The <=0 outcome must be stated as 1 / fully serial, and must
                # explicitly deny that it is the default.
                self.assertIn("1(완전 직렬)", row)
                self.assertIn("기본값이 아니라", row)
                # Regression guard: the old wording tied "0 이하" directly to
                # "기본값으로 되돌아간다" in one clause. Reject that shape.
                self.assertIsNone(
                    re.search(r"0 이하[^.]{0,40}기본값으로 되돌아간다", row),
                    f"{env_name} row must not claim a <=0 value restores the default",
                )
                # The non-integer half keeps its (correct) default-fallback claim.
                self.assertIn("정수가 아닌 값은 조용히 기본값으로 되돌아가", row)


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
        # 28.3 s is the Vercel-estimate render time for a 4-page input that
        # fills both TRIAL_MAX_WORDS_PER_PAGE=4500 and
        # TRIAL_MAX_DRAWINGS_PER_PAGE=2500 at once -- the heaviest input the
        # deployed limits actually accept (docs/web-trial-load.md §3-2,
        # `complexity.py --words 4500 --drawings 2500 --pages 4`), not the
        # old doc's unmeasured 15 s guess.
        seconds_per_request = 28.3
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
        self.assertAlmostEqual(17.593, monthly, places=2)

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
