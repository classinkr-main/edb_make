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
import json
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table  # noqa: E402
from scripts.trial_bench.score import LOW_IOU, score_all  # noqa: E402
from trial_preview import REVIEW_WORTHY_FLAGS, needs_review  # noqa: E402

DOC_START = "<!-- risk-flag-table -->"
DOC_END = "<!-- /risk-flag-table -->"

# hwp_oversegmentation is a *page*-level flag appended only inside
# build_problem_board_edb.py's build_ui_session (via _collect_page_risk_flags
# / _collect_hwp_problem_count_mismatches) -- a desktop-session-only code path
# that problem_parser.parse_problems (what the trial calls) never runs: the
# trial only calls build_pages + build_problem_entries, and
# _collect_problem_risk_flags (the per-*problem* flag collector
# build_problem_entries actually uses) has no branch that can ever emit this
# name. So unlike the other three REVIEW_WORTHY_FLAGS members, no trial
# corpus, however large, can ever make this flag appear on a unit through
# this code path -- it is structurally unreachable here, not merely
# unobserved on this corpus. This is asserted by an explicit constant, not
# derived from all_observed_flags's zero count, so "0 occurrences" in the
# rendered table can't be misread as "never fired on this corpus" when the
# true fact is "cannot ever fire on this corpus, or any other, through this
# code path".
TRIAL_UNREACHABLE_FLAGS = frozenset({"hwp_oversegmentation"})


def _ratio(numerator: int, denominator: int) -> float | None:
    """None (an empty cell) when the metric is undefined -- never a fake perfect or zero score."""
    return numerator / denominator if denominator else None


def _warn(message: str, sink: list[str] | None) -> None:
    """Report a recoverable problem: stderr for the operator, the sink for the caller."""
    print(message, file=sys.stderr)
    if sink is not None:
        sink.append(message)


def collect_units(
    cases: list[str],
    root: Path = BENCH_ROOT,
    warnings: list[str] | None = None,
    excluded: list[dict[str, str]] | None = None,
    low_iou_rows: list[dict[str, Any]] | None = None,
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

    A matched-but-badly-cropped unit (score.py's own "low_iou": a key both
    sides agree on but whose regions_iou is below score.LOW_IOU) is *not* a
    third member of unit_rows's "is_error" -- "is_error" here means exactly
    score.py's "extra", nothing wider. Such a unit therefore counts as a true
    negative in every predictor_contingency below, the same as a fully
    correct one. When ``low_iou_rows`` is given, each case with a nonzero
    score.py "low_iou" count is appended to it as {"case", "count"} so a
    caller (render_report's footnote) can say this out loud instead of
    letting a badly-cropped unit look, silently, like a correctly-handled
    one.

    Each row's own trial observation is re-read here (score_all's row
    carries only the derived "extra"/"missing" key sets, not risk_flags) by
    reopening ``<root>/trial/<row['case']>.json``. ``row['case']`` is
    score_case's own "case" field -- the observation's *internal* "case"
    value, not necessarily the filename stem score_all itself iterated by.
    The two normally agree, but when they do not (a hand-edited or
    renamed observation), the reopen must not raise and abort every other
    case's row along with this one -- exactly the per-case containment
    score_all's own docstring is built around. So a failed reopen is
    reported and skipped, appended to ``excluded`` with a reason, the same
    way score_all treats an unreadable oracle or trial file.
    """
    rows = score_all(cases, root=root, warnings=warnings, excluded=excluded)
    unit_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    for row in rows:
        case = row["case"]
        trial_path = bench_dir("trial", root) / f"{case}.json"
        try:
            trial = load_json(trial_path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            reason = f"trial observation could not be re-read for risk-flag analysis ({exc})"
            _warn(f"risk_flag_predictivity.py: case {case!r}: {trial_path}: {reason}; skipping this case", warnings)
            if excluded is not None:
                excluded.append({"case": case, "reason": reason})
            continue
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
        if low_iou_rows is not None and row.get("low_iou"):
            low_iou_rows.append({"case": case, "count": row["low_iou"]})
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


def render_report(
    unit_rows: list[dict[str, Any]],
    missing_rows: list[dict[str, Any]],
    flags: Iterable[str],
    low_iou_rows: list[dict[str, Any]] | None = None,
    excluded: list[dict[str, str]] | None = None,
) -> str:
    flags = sorted(flags)
    per_flag = flag_table(unit_rows, flags)
    badge = predictor_contingency(unit_rows, needs_review)
    any_flag = predictor_contingency(unit_rows, bool)
    total_errors_with_units = sum(1 for row in unit_rows if row["is_error"])
    total_errors = total_errors_with_units + len(missing_rows)

    headers = ["predictor", "in badge today", "tp", "fp", "fn", "tn", "precision", "recall"]
    table_rows = [
        [
            f"{flag} †" if flag in TRIAL_UNREACHABLE_FLAGS else flag,
            "yes" if flag in REVIEW_WORTHY_FLAGS else "no",
            stats["tp"], stats["fp"], stats["fn"], stats["tn"], _fmt(stats["precision"]), _fmt(stats["recall"]),
        ]
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
    # Named by an explicit constant (TRIAL_UNREACHABLE_FLAGS), not derived
    # from a zero count above: a flag can show 0/0/0/N here either because
    # this corpus never happened to trigger it, or because the trial code
    # path can never emit it at all (see the constant's own docstring) --
    # those are materially different claims, and only the second one gets
    # marked here.
    unreachable_in_table = sorted(flag for flag in flags if flag in TRIAL_UNREACHABLE_FLAGS)
    if unreachable_in_table:
        table += (
            "\n\n> **† 표시된 플래그는 이 코퍼스 크기와 무관하게 체험판 경로에서 절대 나타날 수 없다(구조적으로 도달 불가 -- 관측된 0건이 아니다): "
            + ", ".join(f"`{flag}`" for flag in unreachable_in_table)
            + ".** `problem_parser.parse_problems`(체험판이 부르는 경로)는 `build_pages`+`build_problem_entries`만 호출하고 "
            "`build_ui_session`은 절대 호출하지 않는데, 이 플래그는 `build_ui_session` 전용 페이지 레벨 플래그"
            "(`_collect_page_risk_flags`/`_collect_hwp_problem_count_mismatches`)라서 체험판 쪽 `_collect_problem_risk_flags`에는 "
            "이를 만드는 분기 자체가 없다 -- 코퍼스를 아무리 늘려도 이 표의 위 행은 0/0/0/N에서 움직이지 않는다."
        )
    # A matched-but-low-IoU unit (score.py's own "low_iou": regions_iou below
    # score.LOW_IOU) is scored as correct (a true negative) everywhere above
    # -- "is_error" here means exactly score.py's "extra", nothing wider.
    # Named here rather than silently left to a reader who never opens
    # score.py's own report, so the badly-cropped unit doesn't quietly read
    # as "correctly left alone".
    if low_iou_rows:
        total_low_iou = sum(item["count"] for item in low_iou_rows)
        table += (
            f"\n\n> **이 표는 크롭 정확도(IoU) 오류를 오류로 세지 않는다.** score.py 자신의 리포트는 이 코퍼스에서 "
            f"짝지은 단위인데 IoU가 `LOW_IOU`({LOW_IOU}) 미만인 단위(`low_iou`)를 총 {total_low_iou}개 기록한다: "
            + ", ".join(f"`{item['case']}` ({item['count']})" for item in low_iou_rows)
            + ". 그런 단위는 `extra`/`missing` 어디에도 없으므로 위 표의 `is_error`는 그것을 정답(true negative)으로 센다 -- "
            "박스가 심하게 잘못 잘렸어도 이 표는 잡지 못한다. 어느 단위인지는 `scripts/trial_bench/score.py`의 리포트(`low_iou` 열)를 보라."
        )
    if excluded:
        table += (
            f"\n\n> **{len(excluded)} case(s) excluded from this report because an observation "
            "could not be scored: "
            + ", ".join(f"`{item['case']}` ({item['reason']})" for item in excluded)
            + ".** Rerun scripts/trial_bench/observe.py or scripts/trial_bench/oracle.py for these "
            "cases -- whichever side the reason names -- then rerun risk_flag_predictivity.py to include them."
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
    low_iou_rows: list[dict[str, Any]] = []
    unit_rows, missing_rows = collect_units(args.cases, excluded=excluded, low_iou_rows=low_iou_rows)
    # A named case can be missing from both unit_rows and missing_rows for
    # two different reasons -- score_all/collect_units excluded it (no
    # oracle, unreadable/unscorable observation, or a reopen failure), or the
    # name is simply a typo with no trial observation at all. Computing this
    # from `matched` alone (score.py's own main() pattern) reports the right
    # one instead of a blanket "no trial observation found" for a case that
    # in fact has one.
    matched = {row["case"] for row in unit_rows} | {row["case"] for row in missing_rows}
    excluded_reasons = {item["case"]: item["reason"] for item in excluded}
    missing_cases = [name for name in args.cases if name not in matched]
    for name in missing_cases:
        if name in excluded_reasons:
            print(f"risk_flag_predictivity.py: case {name!r} was excluded: {excluded_reasons[name]}", file=sys.stderr)
        else:
            print(f"risk_flag_predictivity.py: no trial observation found for case {name!r}", file=sys.stderr)
    if not unit_rows and not missing_rows:
        print("risk_flag_predictivity.py: nothing scorable; leaving report unchanged", file=sys.stderr)
        return 1

    flags = sorted(all_observed_flags(unit_rows) | set(REVIEW_WORTHY_FLAGS))
    table = render_report(unit_rows, missing_rows, flags, low_iou_rows=low_iou_rows, excluded=excluded)
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
    return 1 if (missing_cases or doc_refused) else 0


if __name__ == "__main__":
    raise SystemExit(main())
