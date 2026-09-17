"""Measure whether a trial unit's own risk_flags predict a real detection error.

The review badge (trial_preview.needs_review) is supposed to warn a visitor
that a problem needs checking. This builds the contingency table needed to
check whether it could: for every unit the trial produced across the scored
corpus, it pairs the unit's risk_flags with whether that unit is a real error
against the label's ground truth -- score.py's own "extra" (a numbered or
passage key the ground truth does not confirm, or an unclassified fallback
key, which is always a false positive). A ground-truth key the trial never
produced a unit for at all (score.py's "missing") is reported separately: no
risk_flag, current or hypothetical, can ever be attached to a unit that does
not exist.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/risk_flag_predictivity.py [--doc docs/web-trial-quality.md] [case ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table  # noqa: E402
from scripts.trial_bench.score import score_all  # noqa: E402
from trial_preview import REVIEW_WORTHY_FLAGS, needs_review  # noqa: E402

DOC_START = "<!-- risk-flag-table -->"
DOC_END = "<!-- /risk-flag-table -->"


def _ratio(numerator: int, denominator: int) -> float | None:
    """None (an empty cell) when the metric is undefined -- never a fake perfect or zero score."""
    return numerator / denominator if denominator else None


def collect_units(
    cases: list[str],
    root: Path = BENCH_ROOT,
    warnings: list[str] | None = None,
    excluded: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(unit_rows, missing_rows) across every case score.py itself would score.

    Built on score_all's own rows rather than a second ground-truth
    computation this module could get subtly wrong on its own: a case
    score_all skips (no oracle, unreadable, unscorable) is skipped here for
    the same reason, with the same warning, and each row's already-computed
    "extra"/"missing" key sets -- the same ones score.py's own report
    prints -- decide which trial key is a real error. Disagreeing with
    score.py here would let a before/after comparison between the two
    reports silently cover different corpora.

    unit_rows: one dict per unit the trial actually produced --
    {"case", "key", "risk_flags", "is_error"}.

    missing_rows: one dict per ground-truth key the trial never produced any
    unit for -- {"case", "key"}. These are real errors too, but there is no
    unit for any risk_flag to have been attached to.
    """
    rows = score_all(cases, root=root, warnings=warnings, excluded=excluded)
    unit_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    for row in rows:
        case = row["case"]
        trial = load_json(bench_dir("trial", root) / f"{case}.json")
        extra_keys = set(row["extra"])
        for problem in trial["problems"]:
            unit_rows.append(
                {
                    "case": case,
                    "key": problem["key"],
                    "risk_flags": list(problem.get("risk_flags") or []),
                    "is_error": problem["key"] in extra_keys,
                }
            )
        missing_rows.extend({"case": case, "key": key} for key in row["missing"])
    return unit_rows, missing_rows


def all_observed_flags(unit_rows: list[dict[str, Any]]) -> set[str]:
    """Every distinct risk_flag string actually seen on a unit -- data-driven,
    so a flag trial_preview.REVIEW_WORTHY_FLAGS ignores (passage_cross_page_merge_check)
    is included automatically rather than needing to be named here by hand."""
    return {flag for row in unit_rows for flag in row["risk_flags"]}


def predictor_contingency(unit_rows: list[dict[str, Any]], predict: Callable[[list[str]], bool]) -> dict[str, Any]:
    """tp/fp/fn/tn + precision/recall for one badge-shaped predictor over ``unit_rows``.

    Named from the predictor's own point of view (``predict`` is "flag this
    unit"): tp = flagged and really an error, fp = flagged but correct (a
    false alarm), fn = a real error the predictor missed, tn = correct and
    left alone. fn only ever counts a unit that exists -- a ground-truth key
    with no trial unit at all (collect_units's missing_rows) can never enter
    here, because no predict() over a unit's own risk_flags can see a unit
    that was never created.
    """
    tp = fp = fn = tn = 0
    for row in unit_rows:
        flagged = bool(predict(row["risk_flags"]))
        if flagged and row["is_error"]:
            tp += 1
        elif flagged:
            fp += 1
        elif row["is_error"]:
            fn += 1
        else:
            tn += 1
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
    }


def flag_table(unit_rows: list[dict[str, Any]], flags: Iterable[str]) -> dict[str, dict[str, Any]]:
    return {flag: predictor_contingency(unit_rows, lambda risk_flags, _flag=flag: _flag in risk_flags) for flag in flags}


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else ""


def render_report(unit_rows: list[dict[str, Any]], missing_rows: list[dict[str, Any]], flags: Iterable[str]) -> str:
    flags = sorted(flags)
    per_flag = flag_table(unit_rows, flags)
    badge = predictor_contingency(unit_rows, needs_review)
    any_flag = predictor_contingency(unit_rows, bool)
    total_errors_with_units = sum(1 for row in unit_rows if row["is_error"])
    total_errors = total_errors_with_units + len(missing_rows)

    headers = ["predictor", "in badge today", "tp", "fp", "fn", "tn", "precision", "recall"]
    table_rows = [
        [flag, "yes" if flag in REVIEW_WORTHY_FLAGS else "no", stats["tp"], stats["fp"], stats["fn"], stats["tn"], _fmt(stats["precision"]), _fmt(stats["recall"])]
        for flag, stats in ((flag, per_flag[flag]) for flag in flags)
    ]
    table_rows.append(["현재 배지 (needs_review)", "--", badge["tp"], badge["fp"], badge["fn"], badge["tn"], _fmt(badge["precision"]), _fmt(badge["recall"])])
    table_rows.append(["아무 risk_flag나 (any)", "--", any_flag["tp"], any_flag["fp"], any_flag["fn"], any_flag["tn"], _fmt(any_flag["precision"]), _fmt(any_flag["recall"])])
    table = markdown_table(headers, table_rows)
    table += (
        f"\n\n> **단위 {len(unit_rows)}개 중 실제 오류 {total_errors_with_units}개, "
        f"플래그를 달 단위 자체가 없는 오류(정답에는 있으나 체험판이 아예 만들지 않은 키) {len(missing_rows)}개 -- "
        f"합쳐서 이 코퍼스가 아는 실제 오류는 총 {total_errors}건이다.** `fn`은 단위가 존재하는 오류만 센다: "
        "없는 단위에는 어떤 risk_flag도(현재도, 가상의 어떤 조합도) 붙을 수 없기 때문이다."
    )
    return table


def update_doc(doc_path: Path, table: str) -> None:
    stamp = f"측정일 {dt.date.today().isoformat()}"
    block = f"{DOC_START}\n{stamp}\n\n{table}\n{DOC_END}"
    text = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else "# 웹 체험판 정확성 코퍼스 결과\n\n"
    if DOC_START in text and DOC_END in text:
        head, rest = text.split(DOC_START, 1)
        _, tail = rest.split(DOC_END, 1)
        text = head + block + tail
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    doc_path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--doc", type=Path, default=None, help="markdown file whose risk-flag-table block is replaced")
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)

    excluded: list[dict[str, str]] = []
    unit_rows, missing_rows = collect_units(args.cases, excluded=excluded)
    for item in excluded:
        print(f"risk_flag_predictivity.py: case {item['case']!r} was excluded: {item['reason']}", file=sys.stderr)
    if not unit_rows and not missing_rows:
        print("risk_flag_predictivity.py: nothing scorable; leaving report unchanged", file=sys.stderr)
        return 1

    flags = sorted(all_observed_flags(unit_rows) | set(REVIEW_WORTHY_FLAGS))
    table = render_report(unit_rows, missing_rows, flags)
    print(table)
    (BENCH_ROOT / "risk_flag_report.md").write_text(table + "\n", encoding="utf-8")

    doc_refused = False
    if args.doc:
        if args.cases:
            print(
                f"risk_flag_predictivity.py: refusing to overwrite {args.doc}'s risk-flag table with a "
                "case-filtered subset; rerun without case names to refresh the whole table",
                file=sys.stderr,
            )
            doc_refused = True
        else:
            update_doc(args.doc, table)
    return 1 if doc_refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
