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

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.trial_bench import common, score
from scripts.trial_bench import risk_flag_predictivity as rfp
from scripts.trial_bench.risk_flag_predictivity import (
    DOC_END,
    DOC_START,
    TRIAL_UNREACHABLE_FLAGS,
    all_observed_flags,
    collect_units,
    predictor_contingency,
    render_report,
    update_doc,
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


def _patch_bench_root(root: Path) -> contextlib.ExitStack:
    """Redirect every bench_dir/BENCH_ROOT lookup rfp.main touches to ``root``.

    collect_units's and score_all's own ``root`` parameters are bound to the
    *original* BENCH_ROOT at function-definition time, so patching the
    ``BENCH_ROOT`` name alone would not reach them -- exactly the same
    situation test_trial_bench.py's own TestScoreMainReporting works around
    (see its ``test_main_writes_report_and_doc_with_the_excluded_footnote_end_to_end``).
    Patching ``bench_dir`` itself instead works regardless of which root
    value a caller happened to pass in, because the lambda ignores it.
    """
    stack = contextlib.ExitStack()
    stack.enter_context(patch.object(rfp, "bench_dir", lambda name, _root=None: common.bench_dir(name, root)))
    stack.enter_context(patch.object(rfp, "BENCH_ROOT", root))
    stack.enter_context(patch.object(score, "bench_dir", lambda name, _root=None: common.bench_dir(name, root)))
    return stack


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

    def test_a_case_filter_returns_only_that_cases_units(self):
        # collect_units forwards `cases` straight to score_all -- a mutant
        # that dropped the argument (calling score_all([], ...) regardless)
        # would still pass every other test here, since none of them pass a
        # non-empty case list.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1]))
            common.save_json(common.bench_dir("trial", root) / "b.json", _obs("b", [("q9", 9, "9번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "b.json", _obs("b", [("q9", 9, "9번", [])]))
            common.save_json(common.bench_dir("labels", root) / "b.json", _label("b", [9]))

            unit_rows, _ = collect_units(["a"], root=root)

        self.assertEqual(["a"], sorted({row["case"] for row in unit_rows}))
        self.assertEqual(["q1"], [row["key"] for row in unit_rows])

    def test_survives_a_case_whose_internal_case_field_disagrees_with_its_filename_stem(self):
        # score_all's row["case"] is score_case's own "case": trial["case"]
        # -- the observation's *internal* field -- not the filename stem
        # score_all itself iterated by. collect_units used to re-derive the
        # trial path from row["case"] and reopen it, which raised
        # FileNotFoundError straight out of the loop when the two disagree,
        # aborting every other case's row along with this one -- exactly the
        # per-case containment this module's own docstring claims to
        # inherit from score_all.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("renamed", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1]))
            common.save_json(common.bench_dir("trial", root) / "b.json", _obs("b", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "b.json", _obs("b", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "b.json", _label("b", [1]))

            excluded: list[dict[str, str]] = []
            unit_rows, _ = collect_units([], root=root, excluded=excluded)

        self.assertEqual(["b"], sorted({row["case"] for row in unit_rows}))
        self.assertEqual([{"case": "renamed"}], [{"case": item["case"]} for item in excluded])
        self.assertIn("could not be re-read", excluded[0]["reason"])

    def test_a_nonzero_low_iou_case_is_reported_when_asked_for(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1]))
            # Move the oracle box far enough away that regions_iou(q1) < score.LOW_IOU
            # while the key sets still match -- score.py's own "low_iou", not "extra".
            oracle = common.load_json(common.bench_dir("oracle", root) / "a.json")
            oracle["problems"][0]["regions"][0]["bbox"] = {"left": 100.0, "top": 100.0, "width": 10.0, "height": 10.0}
            common.save_json(common.bench_dir("oracle", root) / "a.json", oracle)

            low_iou_rows: list[dict[str, Any]] = []
            unit_rows, _ = collect_units([], root=root, low_iou_rows=low_iou_rows)

        self.assertFalse(unit_rows[0]["is_error"])  # matched, not "extra" -- scored as correct above
        self.assertEqual([{"case": "a", "count": 1}], low_iou_rows)


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

    def test_marks_a_structurally_unreachable_flag_and_footnotes_it(self):
        # hwp_oversegmentation is one of REVIEW_WORTHY_FLAGS but can never be
        # attached to a trial unit at all (see TRIAL_UNREACHABLE_FLAGS) -- its
        # 0/0/0/N row must say so, not read like every other zero-occurrence
        # badge flag that merely never fired on this corpus.
        self.assertIn("hwp_oversegmentation", TRIAL_UNREACHABLE_FLAGS)
        unit_rows = [{"case": "a", "key": "q1", "risk_flags": [], "is_error": False}]
        table = render_report(unit_rows, missing_rows=[], flags=["hwp_oversegmentation", "fallback_grouping"])
        self.assertIn("| hwp_oversegmentation † | yes | 0 | 0 | 0 | 1 |  |  |", table)
        # fallback_grouping is equally unobserved here but is reachable, so
        # it gets the plain row, no marker.
        self.assertIn("| fallback_grouping | yes | 0 | 0 | 0 | 1 |  |  |", table)
        self.assertNotIn("fallback_grouping †", table)
        self.assertIn("구조적으로 도달 불가", table)
        self.assertIn("`hwp_oversegmentation`", table.split("구조적으로 도달 불가", 1)[1].split("\n", 1)[0])

    def test_no_unreachable_footnote_when_no_unreachable_flag_is_in_the_table(self):
        unit_rows = [{"case": "a", "key": "q1", "risk_flags": [], "is_error": False}]
        table = render_report(unit_rows, missing_rows=[], flags=["fallback_grouping"])
        self.assertNotIn("구조적으로 도달 불가", table)
        self.assertNotIn("†", table)

    def test_names_the_low_iou_cases_it_silently_scores_as_correct(self):
        unit_rows = [{"case": "a", "key": "q28", "risk_flags": [], "is_error": False}]
        low_iou_rows = [{"case": "a", "count": 1}]
        table = render_report(unit_rows, missing_rows=[], flags=[], low_iou_rows=low_iou_rows)
        self.assertIn("크롭 정확도(IoU) 오류를 오류로 세지 않는다", table)
        self.assertIn("`a` (1)", table)

    def test_no_low_iou_footnote_when_nothing_was_low_iou(self):
        unit_rows = [{"case": "a", "key": "q1", "risk_flags": [], "is_error": False}]
        table = render_report(unit_rows, missing_rows=[], flags=[], low_iou_rows=[])
        self.assertNotIn("크롭 정확도", table)

    def test_names_excluded_cases_and_why(self):
        unit_rows = [{"case": "a", "key": "q1", "risk_flags": [], "is_error": False}]
        excluded = [{"case": "b", "reason": "no oracle observation found"}]
        table = render_report(unit_rows, missing_rows=[], flags=[], excluded=excluded)
        self.assertIn("1 case(s) excluded from this report", table)
        self.assertIn("`b` (no oracle observation found)", table)

    def test_no_excluded_footnote_when_nothing_was_excluded(self):
        unit_rows = [{"case": "a", "key": "q1", "risk_flags": [], "is_error": False}]
        self.assertNotIn("excluded from this report", render_report(unit_rows, missing_rows=[], flags=[]))
        self.assertNotIn("excluded from this report", render_report(unit_rows, missing_rows=[], flags=[], excluded=[]))


class TestUpdateDoc(unittest.TestCase):
    def test_replaces_the_marked_block_instead_of_duplicating_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            doc_path = Path(temp_dir) / "doc.md"
            update_doc(doc_path, "TABLE ONE")
            update_doc(doc_path, "TABLE TWO")
            text = doc_path.read_text(encoding="utf-8")

        self.assertEqual(1, text.count(DOC_START))
        self.assertEqual(1, text.count(DOC_END))
        self.assertNotIn("TABLE ONE", text)
        block = text.split(DOC_START, 1)[1].split(DOC_END, 1)[0]
        self.assertIn("TABLE TWO", block)

    def test_appends_a_new_block_when_the_doc_has_none_yet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            doc_path = Path(temp_dir) / "doc.md"
            doc_path.write_text("# existing prose\n", encoding="utf-8")
            update_doc(doc_path, "TABLE")
            text = doc_path.read_text(encoding="utf-8")

        self.assertIn("# existing prose", text)
        self.assertEqual(1, text.count(DOC_START))
        self.assertIn("TABLE", text.split(DOC_START, 1)[1].split(DOC_END, 1)[0])


class TestMainCLI(unittest.TestCase):
    def test_the_flag_discovery_line_surfaces_both_observed_only_and_zero_occurrence_badge_flags(self):
        # This end-to-end path is what actually puts
        # passage_cross_page_merge_check (the flag the task singled out) into
        # the published table: a mutant hardcoding
        # `flags = sorted(set(REVIEW_WORTHY_FLAGS))` would silently drop it
        # from here, and a mutant hardcoding
        # `flags = sorted(all_observed_flags(unit_rows))` would drop every
        # zero-occurrence badge flag such as fallback_grouping -- neither is
        # caught by calling all_observed_flags directly with synthetic names.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(
                common.bench_dir("trial", root) / "a.json",
                _obs("a", [("q1", 1, "1번", ["passage_cross_page_merge_check"]), ("q2", 2, "2번", [])]),
            )
            common.save_json(
                common.bench_dir("oracle", root) / "a.json",
                _obs("a", [("q1", 1, "1번", []), ("q2", 2, "2번", [])]),
            )
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1, 2]))

            out = io.StringIO()
            with _patch_bench_root(root):
                with contextlib.redirect_stdout(out):
                    exit_code = rfp.main([])

            self.assertEqual(0, exit_code)
            printed = out.getvalue()
            self.assertIn("| passage_cross_page_merge_check | no |", printed)
            self.assertIn("| fallback_grouping | yes |", printed)
            report_text = (root / "risk_flag_report.md").read_text(encoding="utf-8")
            self.assertIn("| passage_cross_page_merge_check | no |", report_text)
            self.assertIn("| fallback_grouping | yes |", report_text)

    def test_writes_the_doc_block_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1]))
            tmp_doc = root / "doc.md"

            with _patch_bench_root(root):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    exit_code = rfp.main(["--doc", str(tmp_doc)])

            self.assertEqual(0, exit_code)
            doc_text = tmp_doc.read_text(encoding="utf-8")
            self.assertEqual(1, doc_text.count(DOC_START))
            block = doc_text.split(DOC_START, 1)[1].split(DOC_END, 1)[0]
            self.assertIn("현재 배지 (needs_review)", block)

    def test_refuses_to_overwrite_the_doc_with_a_case_filtered_subset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1]))
            tmp_doc = root / "doc.md"
            original_text = f"intro\n{DOC_START}\nold\n{DOC_END}\noutro\n"
            tmp_doc.write_text(original_text, encoding="utf-8")

            with _patch_bench_root(root):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = rfp.main(["--doc", str(tmp_doc), "a"])

            self.assertEqual(1, exit_code)
            self.assertIn("refusing to overwrite", err.getvalue())
            self.assertEqual(original_text, tmp_doc.read_text(encoding="utf-8"))

    def test_reports_a_typo_case_name_and_returns_nonzero(self):
        # score.py's main() computes `missing` from `matched` and reports
        # each named case not found; this script used to silently ignore an
        # unmatched name, print a complete-looking table for the cases that
        # did match, and exit 0.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common.save_json(common.bench_dir("trial", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("oracle", root) / "a.json", _obs("a", [("q1", 1, "1번", [])]))
            common.save_json(common.bench_dir("labels", root) / "a.json", _label("a", [1]))

            with _patch_bench_root(root):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    exit_code = rfp.main(["a", "typo-does-not-exist"])

            self.assertEqual(1, exit_code)
            self.assertIn("no trial observation found for case 'typo-does-not-exist'", err.getvalue())
            # "a" did match, so the table must still be printed and written --
            # a typo in one name must not blank out every other case's row.
            self.assertIn("현재 배지 (needs_review)", out.getvalue())
            self.assertTrue((root / "risk_flag_report.md").is_file())


if __name__ == "__main__":
    unittest.main()
