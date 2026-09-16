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


def regions_iou(a_regions: list[dict[str, Any]], b_regions: list[dict[str, Any]]) -> float:
    """Sum of per-page intersections over per-page unions. A page present on one side only adds union."""
    by_a = {region["page_index"]: region["bbox"] for region in a_regions}
    by_b = {region["page_index"]: region["bbox"] for region in b_regions}
    inter = union = 0.0
    for page in set(by_a) | set(by_b):
        a, b = by_a.get(page), by_b.get(page)
        if a and b:
            overlap = _intersection(a, b)
            inter += overlap
            union += _area(a) + _area(b) - overlap
        else:
            union += _area(a or b)
    return inter / union if union > 0 else 0.0


def expected_from(oracle: dict[str, Any], trial: dict[str, Any], labels: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], str]:
    """Ground truth per key: the oracle, corrected by approved labels (truth: trial | oracle | both | neither)."""
    expected = {problem["key"]: problem for problem in oracle["problems"]}
    if not labels or labels.get("status") != "approved":
        return expected, "pending"
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    for item in labels.get("items", []):
        key, truth = item.get("key"), item.get("truth")
        if truth == "neither":
            expected.pop(key, None)
        elif truth == "trial" and key in trial_by_key:
            expected[key] = trial_by_key[key]
    return expected, "approved"


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def score_case(trial: dict[str, Any], expected: dict[str, dict[str, Any]]) -> dict[str, Any]:
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    questions_t = {key for key in trial_by_key if key.startswith("q")}
    questions_e = {key for key in expected if key.startswith("q")}
    passages_t = {key for key in trial_by_key if key.startswith("p")}
    passages_e = {key for key in expected if key.startswith("p")}
    matched = (questions_t & questions_e) | (passages_t & passages_e)
    ious = {key: regions_iou(trial_by_key[key]["regions"], expected[key]["regions"]) for key in matched}
    return {
        "case": trial["case"],
        "question_recall": _ratio(len(questions_t & questions_e), len(questions_e)),
        "question_precision": _ratio(len(questions_t & questions_e), len(questions_t)),
        "passage_recall": _ratio(len(passages_t & passages_e), len(passages_e)),
        "passage_precision": _ratio(len(passages_t & passages_e), len(passages_t)),
        "mean_iou": sum(ious.values()) / len(ious) if ious else 1.0,
        "low_iou": sum(1 for value in ious.values() if value < LOW_IOU),
        "review_rate": sum(1 for problem in trial["problems"] if needs_review(problem["risk_flags"])) / max(1, len(trial_by_key)),
        "missing": sorted(questions_e - questions_t) + sorted(passages_e - passages_t),
        "extra": sorted(questions_t - questions_e) + sorted(passages_t - passages_e),
        "trial_ms": trial["timing_ms"].get("total"),
    }


def render_report(rows: list[dict[str, Any]]) -> str:
    headers = ["case", "status", "q_recall", "q_prec", "p_recall", "p_prec", "mean_iou", "low_iou", "review", "missing", "extra", "trial_ms", "oracle_ms"]
    table_rows = [
        [
            row["case"], row["status"], f"{row['question_recall']:.2f}", f"{row['question_precision']:.2f}",
            f"{row['passage_recall']:.2f}", f"{row['passage_precision']:.2f}", f"{row['mean_iou']:.2f}", row["low_iou"],
            f"{row['review_rate']:.2f}", " ".join(row["missing"]), " ".join(row["extra"]), row["trial_ms"], row["oracle_ms"],
        ]
        for row in rows
    ]
    if rows:
        count = len(rows)
        mean = lambda field: sum(row[field] for row in rows) / count  # noqa: E731
        table_rows.append(
            [
                "합계", f"{sum(1 for row in rows if row['status'] == 'approved')}/{count} approved",
                f"{mean('question_recall'):.2f}", f"{mean('question_precision'):.2f}", f"{mean('passage_recall'):.2f}",
                f"{mean('passage_precision'):.2f}", f"{mean('mean_iou'):.2f}", sum(row["low_iou"] for row in rows),
                f"{mean('review_rate'):.2f}", sum(len(row["missing"]) for row in rows), sum(len(row["extra"]) for row in rows), "", "",
            ]
        )
    return markdown_table(headers, table_rows)


def score_all(cases: list[str], root: Path = BENCH_ROOT) -> list[dict[str, Any]]:
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
        labels = load_json(labels_path) if labels_path.is_file() else None
        expected, status = expected_from(oracle, trial, labels)
        row = score_case(trial, expected)
        row.update(status=status, oracle_ms=oracle["timing_ms"].get("total"))
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
    table = render_report(rows)
    print(table)
    (BENCH_ROOT / "report.md").write_text(table + "\n", encoding="utf-8")
    if args.doc:
        update_doc(args.doc, table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
