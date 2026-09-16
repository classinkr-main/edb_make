"""Desktop-grade recognition (Gemini page repair forced) on the bench inputs.

Usage:
  .venv/bin/python scripts/trial_bench/oracle.py --runtime-dir /Users/clmagi/Desktop/Projects/edb_mak/.app_runtime [case ...]

The Gemini key is promoted from the app's user settings into the environment
by user_settings.apply_to_env and is never printed or written anywhere.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table, observation_from_result, parse_in_scratch, save_json  # noqa: E402
from scripts.trial_bench.observe import select_inputs  # noqa: E402
from user_settings import apply_to_env, load_user_settings  # noqa: E402


def force_config(model: str = "") -> dict[str, Any]:
    """Force Gemini page repair with the desktop app's 'AI 정밀 인식' settings
    (app_server.py's ``ai_fallback="force"`` path), plus two deliberate
    deltas from what that path actually sends, so the oracle's own
    plumbing failure never gets misread as "AI repair changed nothing":

    - ``timeout_ms=60000`` -- double the desktop forced path's 30000
      (``ai_config.get("timeout_ms") or 30000`` in app_server.py). The
      oracle runs on a developer machine with no Vercel-style request
      budget, so it can afford to wait out a slow Gemini response instead
      of timing out and recording a spurious "error" that would then be
      indistinguishable from "AI repair legitimately found nothing to fix".
    - ``fail_on_error=True`` -- the opposite of the desktop default
      (``run_problem_export``'s ``fail_on_ai_error`` parameter, which every
      desktop caller leaves at its default ``False``). Desktop silently
      keeps the local baseline when AI repair errors, so a user's export
      never blocks on Gemini being down. The oracle exists specifically to
      prove AI repair ran; a silent fallback to baseline here would look
      exactly like "AI agreed with the trial" in the scored output. Raising
      instead makes a broken oracle run fail loudly rather than quietly
      banking an unearned "perfect agreement".
    """
    return {
        "mode": "force",
        "provider": "gemini",
        "model": model,
        "threshold": 0.72,
        "max_regions": 48,
        "max_tokens": 4096,
        "timeout_ms": 60000,
        "save_debug": False,
        "fail_on_error": True,
    }


ZERO_APPLIED_WARNING = (
    "0/{total} pages were changed by AI page repair for this case -- "
    "agreement with the trial is NOT evidence of AI-grade recognition; "
    "AI repair either never ran or ran and made no change."
)


def summarize_page_repair(page_repair: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Turn ParseResult.page_repair (one ``ai_fallback`` metadata dict per
    page -- see page_repair.py's ``_attach_ai_fallback_summary``) into the
    counts that prove AI repair actually did something, not just that it
    was configured to run.

    Field names and meaning match the existing per-run aggregates
    (``build_structured_page_json.py``'s ``build_run_summary`` and
    ``build_problem_board_edb.py``'s ``_summarize_ai_fallback_usage``):
    "attempted" is a fresh Gemini call this run, "cache_hit" reused a
    previous real answer without calling Gemini again, and "applied" is the
    only one of the three that means the page's block classification
    actually changed -- from a fresh call or a cache hit either way. A page
    can be attempted (or a cache hit) and still not applied (rejected,
    errored, or the model found nothing to change), so ``pages_applied`` is
    the number that matters: agreement with the trial on a case where it is
    0 proves nothing about AI-grade recognition, so that case is flagged
    rather than left to look identical to a case where AI repair genuinely
    fixed something.
    """
    pages_total = len(page_repair)
    pages_attempted = 0
    pages_cache_hit = 0
    pages_applied = 0
    models_used: set[str] = set()
    errors: list[dict[str, Any]] = []
    for index, entry in enumerate(page_repair):
        if not isinstance(entry, Mapping):
            continue
        if entry.get("attempted"):
            pages_attempted += 1
        if entry.get("cache_hit"):
            pages_cache_hit += 1
        if entry.get("applied"):
            pages_applied += 1
        model_used = str(entry.get("model_used") or "").strip()
        if model_used:
            models_used.add(model_used)
        error = str(entry.get("error") or "").strip()
        if error:
            errors.append({"page_index": index, "status": str(entry.get("status") or ""), "error": error})
    zero_applied = pages_total > 0 and pages_applied == 0
    return {
        "pages_total": pages_total,
        "pages_attempted": pages_attempted,
        "pages_cache_hit": pages_cache_hit,
        "pages_applied": pages_applied,
        "models_used": sorted(models_used),
        "errors": errors,
        "zero_applied": zero_applied,
        "warning": ZERO_APPLIED_WARNING.format(total=pages_total) if zero_applied else None,
    }


def oracle_case(input_pdf: Path, subject: str, *, ocr_mode: str = "auto", model: str = "", root: Path = BENCH_ROOT) -> dict[str, Any]:
    case = input_pdf.stem
    result = parse_in_scratch(input_pdf, parse_problems, subject=subject, ocr_mode=ocr_mode, ai_fallback_config=force_config(model))
    observation = observation_from_result(case, result, crops_dir=bench_dir("oracle_crops", root) / case)
    observation["oracle"] = {
        "ocr_mode": ocr_mode,
        "model": model or "default",
        "ai_mode": "force",
        "page_repair": summarize_page_repair(getattr(result, "page_repair", ()) or ()),
    }
    save_json(bench_dir("oracle", root) / f"{case}.json", observation)
    return observation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-dir", type=Path, required=True, help="directory holding user_settings.json")
    parser.add_argument("--ocr-mode", default="auto")
    parser.add_argument("--model", default="", help="empty = pipeline default repair model")
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    apply_to_env(load_user_settings(args.runtime_dir))
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY not found in runtime settings", file=sys.stderr)
        return 2
    cases = load_json(BENCH_ROOT / "cases.json") if (BENCH_ROOT / "cases.json").is_file() else {}
    rows = []
    warnings: list[str] = []
    for pdf in select_inputs(args.cases):
        subject = str(cases.get(pdf.stem, {}).get("subject") or "unknown")
        observation = oracle_case(pdf, subject, ocr_mode=args.ocr_mode, model=args.model)
        page_repair = observation["oracle"]["page_repair"]
        rows.append(
            [
                pdf.stem,
                subject,
                len(observation["problems"]),
                len(observation["passage_ranges"]),
                observation["timing_ms"].get("total"),
                f"{page_repair['pages_attempted']}/{page_repair['pages_total']}",
                f"{page_repair['pages_cache_hit']}/{page_repair['pages_total']}",
                f"{page_repair['pages_applied']}/{page_repair['pages_total']}",
                ", ".join(page_repair["models_used"]) or "-",
                len(page_repair["errors"]),
            ]
        )
        if page_repair["warning"]:
            warnings.append(f"{pdf.stem}: {page_repair['warning']}")
    print(
        markdown_table(
            [
                "case", "subject", "problems", "passages", "total_ms",
                "repair_attempted", "repair_cached", "repair_applied", "repair_model", "repair_errors",
            ],
            rows,
        )
    )
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
