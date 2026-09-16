"""Score trial observations against approved labels (or the oracle while pending).

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/score.py [--doc docs/web-trial-quality.md] [case ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table  # noqa: E402
from trial_preview import needs_review  # noqa: E402

LOW_IOU = 0.8
DOC_START = "<!-- corpus-table -->"
DOC_END = "<!-- /corpus-table -->"
ACCEPTED_TRUTHS = {"trial", "oracle", "both", "neither"}


def _area(box: dict[str, float]) -> float:
    return max(0.0, box["width"]) * max(0.0, box["height"])


def _intersection(a: dict[str, float], b: dict[str, float]) -> float:
    width = min(a["left"] + a["width"], b["left"] + b["width"]) - max(a["left"], b["left"])
    height = min(a["top"] + a["height"], b["top"] + b["height"]) - max(a["top"], b["top"])
    return max(0.0, width) * max(0.0, height)


def bbox_iou(a: dict[str, float], b: dict[str, float]) -> float:
    inter = _intersection(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def _envelope(boxes: list[dict[str, float]]) -> dict[str, float]:
    """Smallest box covering every input box."""
    left = min(box["left"] for box in boxes)
    top = min(box["top"] for box in boxes)
    right = max(box["left"] + max(0.0, box["width"]) for box in boxes)
    bottom = max(box["top"] + max(0.0, box["height"]) for box in boxes)
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}


def _envelopes_by_page(regions: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    """One box per page: spec 5-1's "페이지별 합집합 박스".

    A problem can hold several regions on one page (two columns, a stem split
    around a figure), so they are reduced to their envelope before comparing.
    Keying straight off page_index would keep only the last region of each
    page and silently drop the rest.
    """
    by_page: dict[int, list[dict[str, float]]] = {}
    for region in regions:
        by_page.setdefault(region["page_index"], []).append(region["bbox"])
    return {page: _envelope(boxes) for page, boxes in by_page.items()}


def regions_iou(a_regions: list[dict[str, Any]], b_regions: list[dict[str, Any]]) -> float:
    """Sum of per-page intersections over per-page unions. A page present on one side only adds union."""
    by_a = _envelopes_by_page(a_regions)
    by_b = _envelopes_by_page(b_regions)
    inter = union = 0.0
    for page in set(by_a) | set(by_b):
        a, b = by_a.get(page), by_b.get(page)
        if a is not None and b is not None:
            overlap = _intersection(a, b)
            inter += overlap
            union += _area(a) + _area(b) - overlap
        else:
            union += _area(a if a is not None else b)
    return inter / union if union > 0 else 0.0


def _warn(message: str, sink: list[str] | None) -> None:
    """Report a recoverable problem: stderr for the operator, the sink for the caller."""
    print(message, file=sys.stderr)
    if sink is not None:
        sink.append(message)


def expected_from(
    oracle: dict[str, Any],
    trial: dict[str, Any],
    labels: dict[str, Any] | None,
    *,
    warnings: list[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Ground truth per key: the oracle, corrected by approved labels (truth: trial | oracle | both | neither).

    "oracle" is an explicit no-op (the oracle's own entry already stands).
    "both" scores the key against the trial's own regions, crediting the
    trial's detection instead of leaving it as a false positive, for a
    disagreement judged acceptable either way. Raises ValueError only for a
    truth value outside the four above -- a broken label file.

    A key that neither side reports any more is a *stale* label, not a broken
    one: that is what an approved "trial only" verdict becomes as soon as a
    parser fix removes the detection, which is the before/after workflow this
    script exists for. Raising there would leave the whole run unscored, so
    the key is skipped with a warning instead.
    """
    expected = {problem["key"]: problem for problem in oracle["problems"]}
    if not labels or labels.get("status") != "approved":
        return expected, "pending"
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    oracle_by_key = {problem["key"]: problem for problem in oracle["problems"]}
    case = labels.get("case")
    for item in labels.get("items", []):
        key, truth = item.get("key"), item.get("truth")
        if truth not in ACCEPTED_TRUTHS:
            raise ValueError(f"label case {case!r}: key {key!r} has unknown truth {truth!r}; expected one of {sorted(ACCEPTED_TRUTHS)}")
        if key not in oracle_by_key and key not in trial_by_key:
            _warn(f"score.py: case {case!r}: label key {key!r} is no longer present in the oracle or the trial; ignoring", warnings)
            continue
        if truth == "neither":
            expected.pop(key, None)
        elif truth in {"trial", "both"} and key in trial_by_key:
            expected[key] = trial_by_key[key]
        # truth == "oracle", or "both" with no matching trial detection:
        # keep the oracle's entry unchanged.
    return expected, "approved"


def _ratio(numerator: int, denominator: int) -> float | None:
    """None (rendered as an empty cell) when the metric is undefined -- never a fake perfect score."""
    return numerator / denominator if denominator else None


def ai_evidence_cell(page_repair: Any) -> tuple[str, bool | None]:
    """(cell text, has_evidence) from an oracle observation's ``oracle.page_repair``
    (scripts/trial_bench/oracle.py's ``summarize_page_repair`` output).

    ``has_evidence`` is ``None`` when there is nothing to judge -- the field
    is missing or malformed, e.g. an oracle observation from before this
    instrumentation existed -- which is distinct from ``False`` (measured,
    and found no real change). Only ``False`` triggers the report's
    zero-evidence footnote; a scored report must not silently read a `1.00`
    row as AI-confirmed when the oracle run behind it never proved AI page
    repair changed anything.
    """
    if not isinstance(page_repair, dict) or "pages_total" not in page_repair:
        return "?", None
    pages_total = page_repair.get("pages_total") or 0
    if pages_total == 0:
        return "no records", False
    pages_changed = page_repair.get("pages_changed") or 0
    has_evidence = pages_changed > 0
    cell = f"{pages_changed}/{pages_total}" if has_evidence else f"{pages_changed}/{pages_total} (NO EVIDENCE)"
    return cell, has_evidence


def score_case(trial: dict[str, Any], expected: dict[str, dict[str, Any]]) -> dict[str, Any]:
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    questions_t = {key for key in trial_by_key if key.startswith("q")}
    questions_e = {key for key in expected if key.startswith("q")}
    passages_t = {key for key in trial_by_key if key.startswith("p")}
    passages_e = {key for key in expected if key.startswith("p")}
    # Neither numbered nor a passage range -- common.problem_key's "t:<title>"
    # fallback for an unnumbered, non-passage unit. Never a legitimate
    # detection, so it always counts as a false positive rather than being
    # silently dropped from scoring.
    others_t = set(trial_by_key) - questions_t - passages_t
    matched = (questions_t & questions_e) | (passages_t & passages_e)
    ious = {key: regions_iou(trial_by_key[key]["regions"], expected[key]["regions"]) for key in matched}
    return {
        "case": trial["case"],
        "question_recall": _ratio(len(questions_t & questions_e), len(questions_e)),
        "question_precision": _ratio(len(questions_t & questions_e), len(questions_t)),
        "passage_recall": _ratio(len(passages_t & passages_e), len(passages_e)),
        "passage_precision": _ratio(len(passages_t & passages_e), len(passages_t)),
        "mean_iou": sum(ious.values()) / len(ious) if ious else None,
        "low_iou": sum(1 for value in ious.values() if value < LOW_IOU),
        "review_rate": sum(1 for problem in trial["problems"] if needs_review(problem["risk_flags"])) / max(1, len(trial_by_key)),
        "missing": sorted(questions_e - questions_t) + sorted(passages_e - passages_t),
        "extra": sorted(questions_t - questions_e) + sorted(passages_t - passages_e) + sorted(others_t),
        "trial_ms": trial["timing_ms"].get("total"),
    }


def _fmt(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else ""


def _keys_cell(keys: list[str]) -> str:
    """Key list for a rendered table cell, with unnumbered keys made opaque.

    An unnumbered unit's key is common.problem_key's "t:<display_title>"
    fallback, and display_title is up to 120 characters of raw exam text
    (segment.py). That must not reach the committed doc table, so each such
    key becomes "t:#<n>" -- the count and order survive, the exam text does
    not. The full keys stay in the out-of-repo observation JSON.
    """
    rendered = []
    unnumbered = 0
    for key in keys:
        if key.startswith("t:"):
            unnumbered += 1
            rendered.append(f"t:#{unnumbered}")
        else:
            rendered.append(key)
    return " ".join(rendered)


def render_report(rows: list[dict[str, Any]]) -> str:
    headers = ["case", "status", "q_recall", "q_prec", "p_recall", "p_prec", "mean_iou", "low_iou", "review", "missing", "extra", "trial_ms", "oracle_ms", "ai_evidence"]
    table_rows = [
        [
            row["case"], row["status"], _fmt(row["question_recall"]), _fmt(row["question_precision"]),
            _fmt(row["passage_recall"]), _fmt(row["passage_precision"]), _fmt(row["mean_iou"]), row["low_iou"],
            _fmt(row["review_rate"]), _keys_cell(row["missing"]), _keys_cell(row["extra"]), row["trial_ms"], row["oracle_ms"],
            # A row built by hand (as the tests here do) rather than by
            # score_all carries no ai_evidence field at all -- "?" says the
            # report cannot vouch for this row either way, distinct from
            # score_all's own "no records"/"NO EVIDENCE" cells.
            row.get("ai_evidence", "?"),
        ]
        for row in rows
    ]
    if rows:
        count = len(rows)

        def mean(field: str) -> float | None:
            values = [row[field] for row in rows if row[field] is not None]
            return sum(values) / len(values) if values else None

        table_rows.append(
            [
                "합계", f"{sum(1 for row in rows if row['status'] == 'approved')}/{count} approved",
                _fmt(mean("question_recall")), _fmt(mean("question_precision")), _fmt(mean("passage_recall")),
                _fmt(mean("passage_precision")), _fmt(mean("mean_iou")), sum(row["low_iou"] for row in rows),
                _fmt(mean("review_rate")), sum(len(row["missing"]) for row in rows), sum(len(row["extra"]) for row in rows), "", "", "",
            ]
        )
    table = markdown_table(headers, table_rows)
    # The caveat lives next to the numbers it qualifies and is regenerated
    # every run, so it can never go stale the way a hand-written banner in
    # docs/web-trial-quality.md did (docs/web-trial-quality.md's old "AI 근거
    # 없음" paragraph stayed put after update_doc rewrote only the table).
    zero_evidence_cases = [row["case"] for row in rows if row.get("ai_evidence_ok") is False]
    if zero_evidence_cases:
        table += (
            "\n\n> **AI page repair produced no evidence of a real change for "
            f"{len(zero_evidence_cases)} of {len(rows)} case(s): "
            + ", ".join(f"`{case}`" for case in zero_evidence_cases)
            + ".** On those cases the forced-AI oracle's block types, problem grouping, "
            "titles, crop boxes and review flags all came out identical to what the local "
            "baseline produced on its own, so those rows' scores show agreement with the "
            "trial's own local baseline, not confirmation by AI-grade recognition -- see "
            "`ai_evidence` and rerun scripts/trial_bench/oracle.py to refresh."
        )
    return table


def score_all(cases: list[str], root: Path = BENCH_ROOT, warnings: list[str] | None = None) -> list[dict[str, Any]]:
    rows = []
    for trial_path in sorted(bench_dir("trial", root).glob("*.json")):
        case = trial_path.stem
        if cases and case not in cases:
            continue
        oracle_path = bench_dir("oracle", root) / f"{case}.json"
        if not oracle_path.is_file():
            continue
        labels_path = bench_dir("labels", root) / f"{case}.json"
        trial, oracle = load_json(trial_path), load_json(oracle_path)
        # An unscorable observation must cost this case its row, not the whole
        # run: expected_from indexes oracle["problems"] and the row build
        # indexes oracle["timing_ms"], so a truncated or failure-shaped record
        # left here by an interrupted oracle run would raise KeyError and
        # abort the loop for every other case too -- no report.md, no doc
        # table. Skipping matches what a missing observation already does.
        if not isinstance(oracle, dict) or "problems" not in oracle or "timing_ms" not in oracle or oracle.get("error"):
            if not isinstance(oracle, dict):
                reason = "not a JSON object"
            else:
                reason = str(oracle.get("error") or "no problems/timing_ms")
            _warn(f"score.py: case {case!r}: {oracle_path} is not a scorable oracle observation ({reason}); skipping this case", warnings)
            continue
        labels = load_json(labels_path) if labels_path.is_file() else None
        expected, status = expected_from(oracle, trial, labels, warnings=warnings)
        row = score_case(trial, expected)
        oracle_page_repair = (oracle.get("oracle") or {}).get("page_repair") if isinstance(oracle.get("oracle"), dict) else None
        evidence_cell, evidence_ok = ai_evidence_cell(oracle_page_repair)
        row.update(status=status, oracle_ms=oracle["timing_ms"].get("total"), ai_evidence=evidence_cell, ai_evidence_ok=evidence_ok)
        rows.append(row)
    return rows


def update_doc(doc_path: Path, table: str) -> None:
    stamp = f"측정일 {dt.date.today().isoformat()} · 라벨 없는 케이스는 오라클을 임시 정답으로 채점(pending)"
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
    parser.add_argument("--doc", type=Path, default=None, help="markdown file whose corpus-table block is replaced")
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    rows = score_all(args.cases)
    matched = {row["case"] for row in rows}
    missing = [name for name in args.cases if name not in matched]
    for name in missing:
        print(f"score.py: no trial observation found for case {name!r}", file=sys.stderr)
    if not rows:
        print("score.py: nothing matched; leaving report.md" + (f" and {args.doc}" if args.doc else "") + " unchanged", file=sys.stderr)
        return 1
    table = render_report(rows)
    print(table)
    (BENCH_ROOT / "report.md").write_text(table + "\n", encoding="utf-8")
    doc_refused = False
    if args.doc:
        if args.cases:
            print(
                f"score.py: refusing to overwrite {args.doc}'s corpus table with a case-filtered subset "
                f"({', '.join(sorted(matched))}); rerun without case names to refresh the whole table",
                file=sys.stderr,
            )
            doc_refused = True
        else:
            update_doc(args.doc, table)
    return 1 if (missing or doc_refused) else 0


if __name__ == "__main__":
    raise SystemExit(main())
