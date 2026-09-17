"""Tests for scripts/trial_bench/risk_flag_predictivity.py.

The review badge (trial_preview.needs_review) is supposed to warn a visitor
that a problem needs checking. These tests cover whether a unit's own
risk_flags actually predict that the unit is a real error against the bench's
ground truth -- built with fakes, the same tempdir-bench-root pattern
test_trial_bench.py uses for score_all, kept in its own file per this repo's
existing one-file-per-script test convention (test_trial_bench_board.py,
test_trial_bench_control.py).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.trial_bench import common
from scripts.trial_bench.risk_flag_predictivity import (
    all_observed_flags,
    collect_units,
    predictor_contingency,
    render_report,
)


def _obs(case: str, problems: list[tuple[str, int | None, str, list[str]]], total_ms: int = 100) -> dict[str, Any]:
    """Fake trial/oracle observation. Each problem tuple is (key, number, title, risk_flags)."""
    entries = [
        {
            "key": key,
            "number": number,
            "title": title,
            "passage_range": None,
            "regions": [{"page_index": 0, "bbox": {"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0}}],
            "risk_flags": risk_flags,
            "crop": None,
        }
        for key, number, title, risk_flags in problems
    ]
    return {
        "case": case, "pages": 1, "source_page_count": 1, "page_sizes": [[600, 800]],
        "problems": entries, "passage_ranges": [], "timing_ms": {"total": total_ms},
    }


def _label(case: str, question_numbers: list[int]) -> dict[str, Any]:
    return {
        "case": case,
        "status": "approved",
        "ground_truth": {"pages": 1, "question_numbers": question_numbers, "passage_ranges": [], "source": "test"},
        "items": [],
    }


class TestCollectUnits(unittest.TestCase):
    def test_a_matched_key_is_correct_an_unmatched_key_is_an_error_regardless_of_its_flags(self):
        # q1 is in ground truth (correct). q2 and q3 are not (both real
        # errors, score.py's own "extra") -- q2 happens to carry a risk_flag,
        # q3 carries none, which is exactly the case the review badge is
        # supposed to catch and (per the corpus today) does not.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(
                common.bench_dir("trial", root) / "a.json",
                _obs("a", [("q1", 1, "1번", []), ("q2", 2, "2번", ["fallback_grouping"]), ("q3", 3, "3번", [])]),
            )
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1]))

            unit_rows, missing_rows = collect_units([], root=root)

        self.assertEqual([], missing_rows)
        by_key = {row["key"]: row for row in unit_rows}
        self.assertEqual({"q1", "q2", "q3"}, set(by_key))
        self.assertFalse(by_key["q1"]["is_error"])
        self.assertTrue(by_key["q2"]["is_error"])
        self.assertEqual(["fallback_grouping"], by_key["q2"]["risk_flags"])
        self.assertTrue(by_key["q3"]["is_error"])
        self.assertEqual([], by_key["q3"]["risk_flags"])
        self.assertTrue(all(row["case"] == "a" for row in unit_rows))

    def test_a_ground_truth_key_the_trial_never_produced_is_a_missing_row_not_a_unit_row(self):
        # score.py calls this a "missing" key. No trial unit exists for it at
        # all, so it is reported separately: no risk_flag, current or
        # hypothetical, can ever be attached to a unit that was never created.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1, 2]))

            unit_rows, missing_rows = collect_units([], root=root)

        self.assertEqual(["q1"], [row["key"] for row in unit_rows])
        self.assertEqual([{"case": "a", "key": "q2"}], missing_rows)

    def test_skips_a_case_score_all_would_skip_and_says_why(self):
        # No oracle observation for "b" -- score_all already skips this and
        # warns; collect_units must not disagree about which cases are
        # scorable, or a before/after comparison would silently cover
        # different corpora.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("trial", root) / "b.json", _obs("b", [("q1", 1, "1번", [])]))

            excluded: list[dict[str, str]] = []
            unit_rows, _ = collect_units([], root=root, excluded=excluded)

        self.assertEqual(["a"], sorted({row["case"] for row in unit_rows}))
        self.assertEqual([{"case": "b", "reason": "no oracle observation found"}], excluded)

    def test_an_unnumbered_fallback_key_is_always_an_error(self):
        # score_case's own rule: a key that is neither a question nor a
        # passage range (segment.py's "t:<title>" fallback) is always a false
        # positive, never a legitimate detection -- collect_units must not
        # accidentally call it "correct" just because no ground-truth key
        # collides with it.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(
                common.bench_dir("trial", root) / "a.json",
                _obs("a", [("q1", 1, "1번", []), ("t:그림", None, "그림", [])]),
            )
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1]))

            unit_rows, _ = collect_units([], root=root)

        by_key = {row["key"]: row for row in unit_rows}
        self.assertTrue(by_key["t:그림"]["is_error"])


class TestPredictorContingency(unittest.TestCase):
    def test_counts_tp_fp_fn_tn_and_computes_precision_recall(self):
        unit_rows = [
            {"risk_flags": ["fallback_grouping"], "is_error": True},   # tp
            {"risk_flags": ["fallback_grouping"], "is_error": False},  # fp
            {"risk_flags": [], "is_error": True},                      # fn
            {"risk_flags": [], "is_error": False},                     # tn
        ]
        stats = predictor_contingency(unit_rows, lambda flags: "fallback_grouping" in flags)
        self.assertEqual({"tp": 1, "fp": 1, "fn": 1, "tn": 1, "precision": 0.5, "recall": 0.5}, stats)

    def test_precision_and_recall_are_none_not_a_fake_score_when_undefined(self):
        # Nothing was ever flagged (precision has no denominator) and no real
        # error existed to catch (recall has no denominator either) -- both
        # must render as an empty cell, never a fabricated 1.00 or 0.00.
        unit_rows = [{"risk_flags": [], "is_error": False}]
        stats = predictor_contingency(unit_rows, lambda flags: "x" in flags)
        self.assertIsNone(stats["precision"])
        self.assertIsNone(stats["recall"])


class TestAllObservedFlags(unittest.TestCase):
    def test_collects_every_distinct_flag_seen_across_units(self):
        unit_rows = [{"risk_flags": ["a", "b"]}, {"risk_flags": ["b"]}, {"risk_flags": []}]
        self.assertEqual({"a", "b"}, all_observed_flags(unit_rows))


class TestRenderReport(unittest.TestCase):
    def test_names_a_flag_outside_the_current_badge_and_the_badges_own_row(self):
        # passage_cross_page_merge_check is real-error-free and 100% false
        # alarm in this fixture -- exactly REVIEW_WORTHY_FLAGS's reason for
        # leaving it out -- while q2's real error carries no flag at all,
        # which every predictor (including "any flag") must report as a miss.
        unit_rows = [
            {"case": "a", "key": "p1-3", "risk_flags": ["passage_cross_page_merge_check"], "is_error": False},
            {"case": "a", "key": "q2", "risk_flags": [], "is_error": True},
        ]
        table = render_report(unit_rows, missing_rows=[], flags=["passage_cross_page_merge_check"])
        # tp=0 (it never lands on the one real error), fp=1 (the correct
        # passage unit), fn=1 (the unflagged real error), tn=0 -- precision
        # and recall are both a defined 0.00, not an empty "never flagged"
        # cell, because this predictor *was* used (fp+tp=1) and a real error
        # *did* exist (fn+tp=1).
        self.assertIn("| passage_cross_page_merge_check | no | 0 | 1 | 1 | 0 | 0.00 | 0.00 |", table)
        # The current badge (needs_review) never fires on either row in this
        # fixture (neither flag is in REVIEW_WORTHY_FLAGS), so it also shows
        # the real error as a miss and the flagged-but-correct row as a tn.
        # Its precision is undefined (nothing was ever flagged), not a fake 0.
        self.assertIn("| 현재 배지 (needs_review) | -- | 0 | 0 | 1 | 1 |  | 0.00 |", table)

    def test_missing_rows_are_named_but_excluded_from_every_predictors_fn(self):
        unit_rows = [{"case": "a", "key": "q1", "risk_flags": [], "is_error": False}]
        missing_rows = [{"case": "a", "key": "p16-17"}]
        table = render_report(unit_rows, missing_rows, flags=[])
        # One correct unit, no unit-level error, but one unflaggable error
        # named in the footnote -- 1 total real error, not 0.
        self.assertIn("실제 오류 0개", table)
        self.assertIn("단위 자체가 없는 오류", table)
        self.assertIn("1개", table)
        self.assertIn("총 1건", table)


if __name__ == "__main__":
    unittest.main()
