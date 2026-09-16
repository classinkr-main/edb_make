"""Live positive control for the oracle's repair-change detector.

The forced-Gemini oracle (scripts/trial_bench/oracle.py) has only ever been
observed reporting ``repair_changed 0`` on every corpus case -- every one of
those pages already had a text layer the local baseline could read, so
"AI repair agreed with the trial" and "the diff never fires on live model
output" are indistinguishable from that corpus alone. This script builds a
case the local baseline cannot possibly get right on its own -- one page of
an existing corpus PDF, re-emitted as an image-only PDF (rendered to PNG and
wrapped in a new PDF with no text layer at all, per page_repair.py's
docstring on ``_repair_change_counters`` and this task's brief) -- runs the
same forced-Gemini oracle over it, and prints the raw per-page counters so
the result is not just "changed: true/false" but which of blocks/grouping/
titles/boxes/metadata actually moved.

Usage:
  .venv/bin/python scripts/trial_bench/control.py build \\
      --source ~/edb-trial-bench/inputs/social_saengwoon_2020suneung_20191015.pdf \\
      --page 0

  GEMINI_API_KEY= ... # not needed here: run loads the key from --runtime-dir
  .venv/bin/python scripts/trial_bench/control.py run \\
      --runtime-dir /Users/clmagi/Desktop/Projects/edb_mak/.app_runtime \\
      --source ~/edb-trial-bench/inputs/social_saengwoon_2020suneung_20191015.pdf \\
      --page 0 --subject social

The control input is never a corpus case: everything this script touches
lives under ``bench_dir("control", root)`` (default
``~/edb-trial-bench/control/``), never under ``inputs/``, ``oracle/`` or
``cases.json`` where scripts/trial_bench/observe.py's ``select_inputs`` or
score.py would pick it up.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench import oracle  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, bench_dir, case_id, markdown_table, observation_from_result, parse_in_scratch, save_json  # noqa: E402
from user_settings import apply_to_env, load_user_settings  # noqa: E402

CONTROL_DIR = "control"


def build_control_pdf(
    source_pdf: Path,
    page_index: int,
    *,
    dpi: int = 200,
    root: Path = BENCH_ROOT,
    name: str | None = None,
) -> Path:
    """Re-emit page ``page_index`` (0-based) of ``source_pdf`` as a new,
    single-page, image-only PDF: render it to a PNG at ``dpi`` and wrap only
    that PNG in a brand new PDF page sized to match.

    The result carries no text layer and no vector drawings, so PyMuPDF's own
    ``page.get_text()`` returns "" on it (asserted by
    test_trial_bench_control.py) and preprocess.py's text-marker extraction
    (``extract_pdf_problem_markers`` / ``extract_pdf_text_lines``) has
    nothing to read -- the local baseline is whatever ``ocr_mode="auto"``
    recovers on its own, and only Gemini page repair ever sees the actual
    page image well enough to redo the structure. Saved under
    ``bench_dir("inputs", root)``, i.e. ``<control-root>/inputs/<name>.pdf``,
    never the corpus's own ``inputs/`` (the caller passes a control root).
    """
    case = name or f"{case_id(source_pdf)}-p{page_index + 1}-image-only"
    target = bench_dir("inputs", root) / f"{case}.pdf"
    with fitz.open(source_pdf) as source_doc:
        if not 0 <= page_index < source_doc.page_count:
            raise ValueError(
                f"page_index {page_index} out of range for {source_pdf} ({source_doc.page_count} pages)"
            )
        pixmap = source_doc[page_index].get_pixmap(dpi=dpi)
    with tempfile.TemporaryDirectory(prefix="trial-bench-control-") as temp_dir:
        png_path = Path(temp_dir) / "page.png"
        pixmap.save(png_path)
        out_doc = fitz.open()
        try:
            # Page size in points (72 pt/inch) at the pixmap's own pixel size
            # and dpi, so a downstream renderer asking for the same dpi
            # reproduces the same pixel dimensions this function rendered at.
            page_width_pt = pixmap.width * 72.0 / dpi
            page_height_pt = pixmap.height * 72.0 / dpi
            out_page = out_doc.new_page(width=page_width_pt, height=page_height_pt)
            out_page.insert_image(out_page.rect, filename=str(png_path))
            out_doc.save(target, garbage=4, deflate=True)
        finally:
            out_doc.close()
    return target


def run_control(
    control_pdf: Path,
    subject: str,
    *,
    ocr_mode: str = "auto",
    model: str = "",
    root: Path = BENCH_ROOT,
) -> dict[str, Any]:
    """Run the same forced-Gemini oracle path as oracle.oracle_case over one
    control input, and return both the saved observation and the raw
    per-page ``ai_fallback`` entries (page_repair.py's ``_repair_change_counters``
    output merged into each page's summary) so the caller can print which
    counter fired, not just the case-level aggregate.

    Deliberately not a call to ``oracle.oracle_case`` followed by a second
    call to get at ``result.page_repair``: that would run the (paid,
    non-deterministic) Gemini request twice and the two runs could disagree.
    This mirrors oracle_case's body instead, reusing its config and summary
    helpers so a control case's saved JSON is shaped exactly like a corpus
    case's, just parked under a control root.
    """
    case = control_pdf.stem
    try:
        result = parse_in_scratch(
            control_pdf,
            parse_problems,
            subject=subject,
            ocr_mode=ocr_mode,
            ai_fallback_config=oracle.force_config(model),
        )
    except Exception as exc:
        failure = {
            "case": case,
            "error": str(exc),
            "oracle": {
                "ocr_mode": ocr_mode,
                "model": model or "default",
                "ai_mode": "force",
                "page_repair": oracle._failed_page_repair_summary(exc),
            },
        }
        save_json(bench_dir(oracle.FAILURE_DIR, root) / f"{case}.json", failure)
        raise
    observation = observation_from_result(case, result, crops_dir=bench_dir("oracle_crops", root) / case)
    observation["oracle"] = {
        "ocr_mode": ocr_mode,
        "model": model or "default",
        "ai_mode": "force",
        "page_repair": oracle.summarize_page_repair(result.page_repair),
    }
    save_json(bench_dir("oracle", root) / f"{case}.json", observation)
    return {"observation": observation, "pages": [dict(entry) for entry in result.page_repair]}


def _page_rows(pages: list[dict[str, Any]]) -> list[list[Any]]:
    rows = []
    for index, entry in enumerate(pages):
        rows.append(
            [
                index,
                entry.get("status", ""),
                entry.get("changed", False),
                entry.get("blocks_changed", 0),
                entry.get("problems_regrouped", False),
                entry.get("titles_changed", 0),
                entry.get("boxes_overridden", 0),
                entry.get("problem_metadata_changed", 0),
                entry.get("model_used", ""),
                entry.get("error", ""),
            ]
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build", help="render one page of a source PDF into an image-only control PDF")
    build_parser.add_argument("--source", required=True, type=Path, help="a PDF already in the bench (e.g. inputs/<case>.pdf) or any exam PDF")
    build_parser.add_argument("--page", required=True, type=int, help="0-based page index within --source")
    build_parser.add_argument("--dpi", type=int, default=200)
    build_parser.add_argument("--name", default=None, help="control case name; default derived from --source and --page")

    run_parser = subparsers.add_parser("run", help="(re)build the control input and run the forced-Gemini oracle over it")
    run_parser.add_argument("--runtime-dir", required=True, type=Path, help="directory holding user_settings.json")
    run_parser.add_argument("--source", required=True, type=Path)
    run_parser.add_argument("--page", required=True, type=int)
    run_parser.add_argument("--subject", default="unknown")
    run_parser.add_argument("--dpi", type=int, default=200)
    run_parser.add_argument("--model", default="", help="empty = pipeline default repair model")
    run_parser.add_argument("--ocr-mode", default="auto")
    run_parser.add_argument("--name", default=None)

    args = parser.parse_args(argv)
    control_root = bench_dir(CONTROL_DIR, BENCH_ROOT)

    if args.command == "build":
        target = build_control_pdf(args.source, args.page, dpi=args.dpi, root=control_root, name=args.name)
        print(f"{args.source.name} page {args.page} -> {target}")
        return 0

    apply_to_env(load_user_settings(args.runtime_dir))
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY not found in runtime settings", file=sys.stderr)
        return 2

    control_pdf = build_control_pdf(args.source, args.page, dpi=args.dpi, root=control_root, name=args.name)
    print(f"control input: {control_pdf}")
    try:
        result = run_control(control_pdf, args.subject, ocr_mode=args.ocr_mode, model=args.model, root=control_root)
    except Exception as exc:
        print(f"control.py: oracle run failed -- {exc}", file=sys.stderr)
        return 1

    page_repair = result["observation"]["oracle"]["page_repair"]
    print()
    print(
        markdown_table(
            ["pages_total", "repair_attempted", "repair_changed", "repair_applied", "repair_model", "warning"],
            [
                [
                    page_repair["pages_total"],
                    f"{page_repair['pages_attempted']}/{page_repair['pages_total']}",
                    f"{page_repair['pages_changed']}/{page_repair['pages_total']}",
                    f"{page_repair['pages_applied']}/{page_repair['pages_total']}",
                    ", ".join(page_repair["models_used"]) or "-",
                    page_repair["warning"] or "-",
                ]
            ],
        )
    )
    print()
    print(
        markdown_table(
            [
                "page", "status", "changed", "blocks_changed", "problems_regrouped",
                "titles_changed", "boxes_overridden", "problem_metadata_changed", "model_used", "error",
            ],
            _page_rows(result["pages"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
