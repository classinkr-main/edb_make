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
      --page 0 --subject social --attempts 6

A single ``run`` is not a repeatable experiment: a live response has to pass
``page_repair._validate_repair_payload`` before the diff runs at all, and on
the math control page it often does not (``problem start and choice block ids
overlap``), so the same command produces a positive on some runs and
``invalid_response`` on others. ``--attempts N`` keeps trying until one
response validates and records every attempt's status, and the raw validated
payload is saved next to the counters (``oracle.repair_payloads``) so the
recorded positive stays checkable without paying for another call.

The control input is never a corpus case: ``main()`` always computes
``control_root = bench_dir(CONTROL_DIR, BENCH_ROOT)`` (``~/edb-trial-bench/control/``
unless ``TRIAL_BENCH_ROOT`` is set) and passes it explicitly to every call, so
everything this script touches lives under that root, never under the
corpus's own ``inputs/``, ``oracle/`` or ``cases.json`` where
scripts/trial_bench/observe.py's ``select_inputs`` or score.py would pick it
up. ``build_control_pdf`` and ``run_control`` take ``root`` as a required
keyword argument with no default, precisely so a caller can never fall back
to the corpus root by omission.

Neither this script's "control" framing nor its baseline is AI-free: with
``ocr_mode="auto"`` (the default) and a ``GEMINI_API_KEY`` configured,
``ocr_backend.build_ocr_backend("auto")`` resolves to ``GeminiOCRBackend``,
so the pre-repair baseline this control diffs against was itself produced by
Gemini's vision OCR of the same page image. What this control isolates is
page *repair*: the forced-Gemini pass either does or does not change that
Gemini-OCR baseline. See docs/web-trial-quality.md's 2026-09-17 section for
the full framing and a genuinely AI-free baseline (``--ocr-mode none``) for
contrast.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench import oracle  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, MAX_PAGES, bench_dir, case_id, markdown_table, observation_from_result, save_json  # noqa: E402
from user_settings import apply_to_env, load_user_settings  # noqa: E402

CONTROL_DIR = "control"


def build_control_pdf(
    source_pdf: Path,
    page_index: int,
    *,
    dpi: int = 200,
    root: Path,
    name: str | None = None,
) -> Path:
    """Re-emit page ``page_index`` (0-based) of ``source_pdf`` as a new,
    single-page, image-only PDF: render it to a PNG at ``dpi`` and wrap only
    that PNG in a brand new PDF page sized to match.

    The result carries no text layer and no vector drawings, so PyMuPDF's own
    ``page.get_text()`` returns "" on it (asserted by
    test_trial_bench_control.py) and preprocess.py's text-marker extraction
    (``extract_pdf_problem_markers`` / ``extract_pdf_text_lines``) has
    nothing to read. This does NOT make the resulting run's pre-repair
    baseline AI-free: with ``ocr_mode="auto"`` and a ``GEMINI_API_KEY`` set,
    ``ocr_backend.build_ocr_backend`` resolves ``"auto"`` to
    ``GeminiOCRBackend``, so that baseline is itself Gemini's vision OCR of
    this same page image, run before page repair -- see the module
    docstring and docs/web-trial-quality.md's 2026-09-17 section for what
    this control actually isolates (page *repair*, not OCR). ``root`` has no
    default on purpose: the caller must pass a control root (``main()``
    passes ``bench_dir(CONTROL_DIR, BENCH_ROOT)``) so this can never write
    under the corpus's own ``inputs/`` by omission -- see
    test_trial_bench_control.py's coverage of ``main()``. Saved under
    ``bench_dir("inputs", root)``, i.e. ``<control-root>/inputs/<name>.pdf``,
    never the corpus's own ``inputs/``.
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


def control_force_config(model: str = "") -> dict[str, Any]:
    """``oracle.force_config`` plus one control-only key: ``save_debug=True``.

    ``save_debug`` makes page_repair.py's ``_maybe_write_debug_artifacts``
    write ``{"summary": ..., "repair_payload": ...}`` for every page whose
    response passed ``_validate_repair_payload``, which is the only place the
    *raw* model answer (``problem_start_block_ids`` / ``choice_block_ids`` /
    ``figure_block_ids`` / ``display_titles`` / ``notes``) is ever written
    down -- the per-page ``ai_fallback`` summary keeps only the counters
    derived from it. Without it, "which titles did the model actually send,
    and did they differ from the baseline?" can only be answered by paying
    for another non-deterministic Gemini call, which is exactly what made the
    first recorded positive un-recheckable.

    Deliberately NOT added to ``oracle.force_config`` itself: that config is
    the corpus oracle's, it runs over real exam pages, and the debug artifact
    contains raw exam text. ``_parse_control`` harvests these into the
    control observation under ``<control-root>/`` -- outside the repository,
    never committed, same as every other bench artifact.
    """
    return {**oracle.force_config(model), "save_debug": True}


REPAIR_DEBUG_SUFFIX = "_repair.json"


def collect_repair_payloads(scratch_dir: Path) -> dict[str, Any]:
    """Harvest the raw validated repair payloads under ``scratch_dir``.

    page_repair.py's ``_maybe_write_debug_artifacts`` writes one
    ``<page_id>_repair.json`` per applied page into
    ``<source>/.pipeline_cache/ai_debug/``, which for a bench run lives inside
    the throwaway parse directory. Returns ``{page_id: repair_payload}``; the
    surrounding ``summary`` is dropped because the observation already
    carries it as ``oracle.page_repair_pages``.

    Only *validated* responses have a file at all: ``repair_page_model``
    returns at its ``invalid_response`` branch long before
    ``_maybe_write_debug_artifacts`` runs, so a rejected answer leaves
    nothing here and is visible only as that page's ``status``/``error``.
    """
    payloads: dict[str, Any] = {}
    for path in sorted(scratch_dir.rglob("*" + REPAIR_DEBUG_SUFFIX)):
        if path.parent.name != "ai_debug":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        payload = record.get("repair_payload") if isinstance(record, dict) else None
        if isinstance(payload, dict):
            payloads[path.name[: -len(REPAIR_DEBUG_SUFFIX)]] = payload
    return payloads


def _parse_control(
    control_pdf: Path,
    subject: str,
    *,
    ocr_mode: str,
    config: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """``common.parse_in_scratch`` with the scratch directory kept open long
    enough to harvest the debug payloads out of it.

    Same reason for the copy as parse_in_scratch's: a ``.pipeline_cache``
    next to the real input would make second runs unrealistically fast. The
    difference is that ``parse_in_scratch`` deletes its temp dir on the way
    out of the ``with`` block, taking the one on-disk copy of the raw model
    answer with it -- so this reimplements the same two lines rather than
    wrapping it.
    """
    with tempfile.TemporaryDirectory(prefix="trial-bench-control-run-") as temp_dir:
        scratch = Path(temp_dir)
        copied = scratch / control_pdf.name
        shutil.copyfile(control_pdf, copied)
        result = parse_problems(
            copied,
            work_dir=scratch / "work",
            max_pages=MAX_PAGES,
            subject=subject,
            ocr_mode=ocr_mode,
            ai_fallback_config=config,
        )
        return result, collect_repair_payloads(scratch)


def run_control(
    control_pdf: Path,
    subject: str,
    *,
    ocr_mode: str = "auto",
    model: str = "",
    root: Path,
    attempts: int = 1,
) -> dict[str, Any]:
    """Run the same forced-Gemini oracle path as oracle.oracle_case over one
    control input, and return both the saved observation and the raw
    per-page ``ai_fallback`` entries (page_repair.py's ``_repair_change_counters``
    output, plus its always-present ``baseline_block_count``/
    ``baseline_problem_count``, merged into each page's summary) so the
    caller can print which counter fired, not just the case-level aggregate.
    The same per-page list is also saved into the observation (as
    ``oracle.page_repair_pages``), together with the raw validated payload
    (``oracle.repair_payloads``) and one record per attempt
    (``oracle.attempts``), so this is the one artifact on disk that still
    carries them after the process exits -- re-deriving them otherwise would
    need a fresh, paid, non-deterministic Gemini call.

    ``attempts`` is why this loop exists at all. The same command on the same
    input is *not* reliably reproducible: the model's answer has to survive
    ``_validate_repair_payload`` (non-overlapping id sets, reading order),
    and on the math control page it does so only some of the time -- the
    rest come back ``invalid_response``, which is a real observation but not
    a positive control. Each attempt is one full parse (one Gemini OCR pass
    plus one repair call); the loop stops at the first attempt with an
    applied page, and if none applies, the last attempt's observation is the
    one saved. Every attempt's statuses are recorded either way, so the
    saved artifact shows how many tries that positive took.

    A raised exception is not retried: ``force_config``'s
    ``fail_on_error=True`` turns transport/parse failures into exceptions,
    and those are recorded in ``oracle_failures/`` exactly as oracle_case
    records them. Only a *validation* rejection (which never raises) is an
    attempt worth spending again.

    Deliberately not a call to ``oracle.oracle_case`` followed by a second
    call to get at ``result.page_repair``: that would run the (paid,
    non-deterministic) Gemini request twice and the two runs could disagree.
    This mirrors oracle_case's body instead, reusing its config and summary
    helpers so a control case's saved JSON is shaped exactly like a corpus
    case's, just parked under a control root. ``root`` has no default on
    purpose -- see ``build_control_pdf``'s docstring; a stray default here
    would silently overwrite ``<BENCH_ROOT>/oracle/<case>.json``, the
    corpus's own ground truth.
    """
    case = control_pdf.stem
    config = control_force_config(model)
    budget = max(1, int(attempts))
    attempt_records: list[dict[str, Any]] = []
    last: tuple[Any, list[dict[str, Any]], dict[str, Any]] | None = None
    for attempt_index in range(1, budget + 1):
        try:
            result, payloads = _parse_control(control_pdf, subject, ocr_mode=ocr_mode, config=config)
        except Exception as exc:
            failure = {
                "case": case,
                "attempt": attempt_index,
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
        pages = [dict(entry) for entry in result.page_repair]
        summary = oracle.summarize_page_repair(result.page_repair)
        attempt_records.append(
            {
                "attempt": attempt_index,
                "statuses": dict(summary["statuses"]),
                "pages_applied": summary["pages_applied"],
                "pages_changed": summary["pages_changed"],
                "errors": [str(entry.get("error") or "") for entry in summary["errors"]],
            }
        )
        print(
            f"attempt {attempt_index}/{budget}: "
            f"statuses={summary['statuses']} applied={summary['pages_applied']}/{summary['pages_total']} "
            f"changed={summary['pages_changed']}/{summary['pages_total']}"
        )
        last = (result, pages, payloads)
        if summary["pages_applied"]:
            break

    assert last is not None  # the loop body always runs at least once
    result, pages, payloads = last
    observation = observation_from_result(case, result, crops_dir=bench_dir("oracle_crops", root) / case)
    observation["oracle"] = {
        "ocr_mode": ocr_mode,
        "model": model or "default",
        "ai_mode": "force",
        "page_repair": oracle.summarize_page_repair(result.page_repair),
        "page_repair_pages": pages,
        "attempts": attempt_records,
        # Raw model output, i.e. raw exam text: control root only, never the
        # repository -- see control_force_config.
        "repair_payloads": payloads,
    }
    save_json(bench_dir("oracle", root) / f"{case}.json", observation)
    return {
        "observation": observation,
        "pages": pages,
        "payloads": payloads,
        "attempts": attempt_records,
    }


def _page_rows(pages: list[dict[str, Any]]) -> list[list[Any]]:
    rows = []
    for index, entry in enumerate(pages):
        rows.append(
            [
                index,
                entry.get("status", ""),
                # The pre-repair baseline this page's diff is measured
                # against -- always present (page_repair.py sets both
                # unconditionally, before checking whether AI repair is even
                # enabled), and NOT AI-free: with ocr_mode="auto" and a
                # GEMINI_API_KEY set, this baseline was itself produced by
                # Gemini vision OCR of the page image, before page repair ran.
                entry.get("baseline_block_count", ""),
                entry.get("baseline_problem_count", ""),
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
    run_parser.add_argument(
        "--attempts",
        type=int,
        default=1,
        help=(
            "how many times to run the page before giving up on getting a validated response; "
            "stops at the first attempt with an applied page. Each attempt is a fresh, paid Gemini "
            "call, and every attempt's statuses are saved into the observation."
        ),
    )

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
        result = run_control(
            control_pdf,
            args.subject,
            ocr_mode=args.ocr_mode,
            model=args.model,
            root=control_root,
            attempts=args.attempts,
        )
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
                "page", "status", "baseline_block_count", "baseline_problem_count",
                "changed", "blocks_changed", "problems_regrouped",
                "titles_changed", "boxes_overridden", "problem_metadata_changed", "model_used", "error",
            ],
            _page_rows(result["pages"]),
        )
    )
    print()
    saved = bench_dir("oracle", control_root) / f"{control_pdf.stem}.json"
    payloads = result.get("payloads") or {}
    attempts = result.get("attempts") or []
    print(f"attempts: {len(attempts)} (statuses per attempt are saved as oracle.attempts)")
    if payloads:
        # The raw validated answer, the only copy of it that outlives this
        # process: without this the counter table above can never be
        # re-checked against what the model actually sent.
        print(f"raw validated repair payload saved for page(s): {', '.join(sorted(payloads))}")
    else:
        print("no validated repair payload this run (nothing passed _validate_repair_payload)")
    print(f"observation: {saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
