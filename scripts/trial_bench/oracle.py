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

    ``max_regions`` is the one other value that differs (48, the pipeline
    default, against the desktop's 30) and is deliberately left alone: under
    ``mode="force"`` page_repair.py skips the ``max_regions`` gate entirely,
    so it changes nothing here. Matching the desktop's 30 would be the more
    dangerous choice, since it would only start to matter the day that
    bypass is removed -- and then it would silently skip exactly the dense
    pages the oracle most needs AI repair on. test_page_repair.py's
    ``test_force_mode_ignores_max_regions`` pins the bypass.

    ``max_tokens=8192`` and ``max_output_token_cap=8192`` are a fourth,
    oracle-only delta with no desktop equivalent at all (desktop's
    ``ai_fallback_config`` dict never carries ``max_output_token_cap`` --
    see ``build_problem_board_edb.py``'s ``_build_ai_fallback_config`` --
    so ``page_repair.AIFallbackConfig.max_output_token_cap`` stays ``None``
    and desktop's per-block output-token estimate is untouched). Both
    English bench cases failed with "Gemini response JSON decode failed:
    Unterminated string" before this; instrumenting the failure
    (page_repair.GeminiRepairTruncatedError's diagnostics) on
    english_2020suneung_go3_20191107 showed the response was cut off by
    finishReason=MAX_TOKENS at only 776 effective output tokens -- not the
    2048/3072 hard cap, but page_repair.py's ``_repair_output_token_budget``
    per-block *estimate* (512 + 24*11 blocks), which was calibrated against
    short synthetic block ids ("block-1") and badly undercounts a real
    page's id ("english_2020suneung_go3_20191107-page-001-block-011", 50+
    characters, repeated twice per block). ``max_output_token_cap`` bypasses
    that estimate for this path only; see oracle_failures/ for the captured
    record and this module's docstring/plan for the re-run after the fix.
    """
    return {
        "mode": "force",
        "provider": "gemini",
        "model": model,
        "threshold": 0.72,
        "max_regions": 48,
        "max_tokens": 8192,
        "timeout_ms": 60000,
        "save_debug": False,
        "fail_on_error": True,
        "max_output_token_cap": 8192,
    }


ZERO_CHANGED_WARNING = (
    "0/{total} pages were changed by AI page repair for this case -- "
    "agreement with the trial is NOT evidence of AI-grade recognition; "
    "AI repair either never ran, or ran and its answer matched the local "
    "baseline exactly (a validated 'applied' response is not the same thing "
    "-- {applied}/{total} pages were applied without changing anything). "
    "Page statuses: {statuses}."
)

# Distinct from ZERO_CHANGED_WARNING: pages_total == 0 means the
# instrumentation produced no per-page records at all (a renamed/missing
# ParseResult.page_repair field, or a build_pages branch that never attaches
# ai_fallback -- see build_problem_board_edb.py's page-as-is path), not that
# AI repair ran and changed nothing. Collapsing the two into one flag would
# make the instrumentation's own failure mode read as a normal, if boring,
# measurement.
NO_PAGE_RECORDS_WARNING = (
    "no page-repair metadata recorded for this case -- the oracle "
    "instrumentation did not run; this observation is not evidence of anything."
)


def format_statuses(statuses: Mapping[str, int]) -> str:
    return ", ".join(f"{status}={count}" for status, count in sorted(statuses.items())) or "none"


def summarize_page_repair(page_repair: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Turn ParseResult.page_repair (one ``ai_fallback`` metadata dict per
    page -- see page_repair.py's ``_attach_ai_fallback_summary``) into the
    counts that prove AI repair actually did something, not just that it
    was configured to run and its answer accepted.

    Field names and meaning match the existing per-run aggregates
    (``build_structured_page_json.py``'s ``build_run_summary`` and
    ``build_problem_board_edb.py``'s ``_summarize_ai_fallback_usage``):
    "attempted" is a fresh Gemini call this run, "cache_hit" reused a
    previous real answer without calling Gemini again, and "applied" means a
    response validated and was written onto the page -- from a fresh call or
    a cache hit either way. Applied does NOT mean the page's block
    classification changed: ``_apply_repair_payload`` writes unconditionally
    once validation passes, so an AI answer that reproduces the local
    baseline exactly still counts as applied. ``pages_changed`` is the field
    that means "actually changed" (page_repair.py's ``summary["changed"]``:
    the union of its per-page diff against the pre-repair baseline over block
    types, the ProblemUnit stem/choice/figure partition, display/problem
    titles, ``bbox_px`` crop boxes and ``review_flags`` -- every AI-written
    value that reaches this observation) -- ``pages_applied`` is kept
    alongside it only to show how many of the applied pages were
    rubber-stamps.

    Agreement with the trial on a case where ``pages_changed`` is 0 proves
    nothing about AI-grade recognition, so that case is flagged (``zero_applied``)
    rather than left to look identical to a case where AI repair genuinely
    changed something. ``pages_total == 0`` is a different, louder failure --
    the instrumentation recorded no per-page data at all -- and gets its own
    ``no_page_records`` flag instead of being silently exempted from
    ``zero_applied``.

    ``errors`` alone is not enough to explain a zero-changed case: page_repair
    reports most of its no-ops through ``status`` and never sets ``error``
    (``missing_api_key``, ``not_needed``, ``too_many_blocks``), so the per-
    status counts travel with the flag as the reason it fired.
    """
    pages_total = len(page_repair)
    pages_attempted = 0
    pages_cache_hit = 0
    pages_applied = 0
    pages_changed = 0
    models_used: set[str] = set()
    statuses: dict[str, int] = {}
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
        if entry.get("changed"):
            pages_changed += 1
        model_used = str(entry.get("model_used") or "").strip()
        if model_used:
            models_used.add(model_used)
        status = str(entry.get("status") or "").strip() or "unknown"
        statuses[status] = statuses.get(status, 0) + 1
        error = str(entry.get("error") or "").strip()
        if error:
            errors.append({"page_index": index, "status": str(entry.get("status") or ""), "error": error})
    no_page_records = pages_total == 0
    zero_applied = pages_total > 0 and pages_changed == 0
    if no_page_records:
        warning = NO_PAGE_RECORDS_WARNING
    elif zero_applied:
        warning = ZERO_CHANGED_WARNING.format(total=pages_total, applied=pages_applied, statuses=format_statuses(statuses))
    else:
        warning = None
    return {
        "pages_total": pages_total,
        "pages_attempted": pages_attempted,
        "pages_cache_hit": pages_cache_hit,
        "pages_applied": pages_applied,
        "pages_changed": pages_changed,
        "models_used": sorted(models_used),
        "statuses": dict(sorted(statuses.items())),
        "errors": errors,
        "zero_applied": zero_applied,
        "no_page_records": no_page_records,
        "warning": warning,
    }


FAILURE_STATUS = "oracle_failed"
# Failure records live in their own directory, never in bench_dir("oracle").
# An observation under oracle/ is this case's pending ground truth -- the
# thing score.py scores the trial against, regenerable only with a live
# Gemini key and the out-of-repo corpus. Writing a failure stub over it
# would let one transient Gemini error destroy that data, and score.py would
# then read a record with no "problems"/"timing_ms" at all.
FAILURE_DIR = "oracle_failures"


def _gemini_diagnostics_from(exc: Exception) -> dict[str, Any] | None:
    """Best-effort diagnostics recovered from a Gemini page-repair failure.

    Present when ``exc`` is a ``page_repair.GeminiRepairResponseError`` (or a
    wrapping RuntimeError that copied its diagnostics onto itself --
    ``page_repair._copy_gemini_diagnostics``, which the retry and
    model-fallback loops apply before re-raising): the response's
    finishReason, the effective/configured max output tokens, prompt/response
    sizes, and the first and last 200 characters of the raw response text.
    Absent for every other kind of failure (missing API key, network error,
    an invalid_response validation failure that never touched JSON parsing),
    so a case that failed for an unrelated reason does not carry a
    misleading "no diagnostics available" stand-in.
    """
    diagnostics = getattr(exc, "diagnostics", None)
    return dict(diagnostics) if isinstance(diagnostics, Mapping) else None


def _failed_page_repair_summary(exc: Exception) -> dict[str, Any]:
    """A page_repair summary for a case whose parse raised before it produced
    any pages at all (force_config's fail_on_error=True lets a Gemini
    exception propagate out of repair_page_model). Shaped like
    summarize_page_repair's normal output so main() and score.py can read it
    the same way, but status/warning name the failure instead of reporting a
    clean-looking 0/0 row.
    """
    diagnostics = _gemini_diagnostics_from(exc)
    error_entry: dict[str, Any] = {"page_index": None, "status": FAILURE_STATUS, "error": str(exc)}
    if diagnostics is not None:
        error_entry["gemini_diagnostics"] = diagnostics
    return {
        "pages_total": 0,
        "pages_attempted": 0,
        "pages_cache_hit": 0,
        "pages_applied": 0,
        "pages_changed": 0,
        "models_used": [],
        "statuses": {FAILURE_STATUS: 1},
        "errors": [error_entry],
        "zero_applied": False,
        "no_page_records": True,
        "status": FAILURE_STATUS,
        "gemini_truncated": bool(getattr(exc, "truncated", False)),
        "warning": f"oracle_case raised before any page was parsed -- {exc}",
    }


def oracle_case(input_pdf: Path, subject: str, *, ocr_mode: str = "auto", model: str = "", root: Path = BENCH_ROOT) -> dict[str, Any]:
    case = input_pdf.stem
    try:
        result = parse_in_scratch(input_pdf, parse_problems, subject=subject, ocr_mode=ocr_mode, ai_fallback_config=force_config(model))
    except Exception as exc:
        # Without this, a Gemini exception under fail_on_error=True propagates
        # straight out of parse_in_scratch and this case leaves no record of
        # what went wrong but a traceback in the terminal. Save a failure
        # record before re-raising -- into FAILURE_DIR, never over
        # oracle/<case>.json, so the previous successful observation (this
        # case's pending ground truth) survives a transient Gemini failure.
        failure = {
            "case": case,
            "error": str(exc),
            "oracle": {
                "ocr_mode": ocr_mode,
                "model": model or "default",
                "ai_mode": "force",
                "page_repair": _failed_page_repair_summary(exc),
            },
        }
        save_json(bench_dir(FAILURE_DIR, root) / f"{case}.json", failure)
        raise
    observation = observation_from_result(case, result, crops_dir=bench_dir("oracle_crops", root) / case)
    observation["oracle"] = {
        "ocr_mode": ocr_mode,
        "model": model or "default",
        "ai_mode": "force",
        "page_repair": summarize_page_repair(result.page_repair),
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
    warnings: list[tuple[str, str]] = []
    failed_cases: list[str] = []
    for pdf in select_inputs(args.cases):
        subject = str(cases.get(pdf.stem, {}).get("subject") or "unknown")
        try:
            observation = oracle_case(pdf, subject, ocr_mode=args.ocr_mode, model=args.model)
        except Exception as exc:
            # A failed case must not cost the run its whole table: oracle_case
            # has already saved this case's own failure observation to disk,
            # and the cases that already succeeded are still worth printing
            # and pasting into docs, so keep going instead of dying here.
            failed_cases.append(pdf.stem)
            warnings.append((pdf.stem, f"oracle_case raised before any page was parsed -- {exc}"))
            rows.append([pdf.stem, subject, "-", "-", "-", "-", "-", "ERROR", "-", "-", 1])
            continue
        page_repair = observation["oracle"]["page_repair"]
        changed = f"{page_repair['pages_changed']}/{page_repair['pages_total']}"
        rows.append(
            [
                pdf.stem,
                subject,
                len(observation["problems"]),
                len(observation["passage_ranges"]),
                observation["timing_ms"].get("total"),
                f"{page_repair['pages_attempted']}/{page_repair['pages_total']}",
                f"{page_repair['pages_cache_hit']}/{page_repair['pages_total']}",
                # The verdict rides inside the cell, not just in the note
                # below, because this table gets pasted into docs a row at a
                # time and "0/4" on its own reads as an unremarkable number.
                # This is the real change signal (page_repair.py's
                # summary["changed"]), not the "applied" (validated-and-written)
                # count that repair_applied shows next to it.
                f"{changed} (NO AI EVIDENCE)" if (page_repair["zero_applied"] or page_repair["no_page_records"]) else changed,
                f"{page_repair['pages_applied']}/{page_repair['pages_total']}",
                ", ".join(page_repair["models_used"]) or "-",
                len(page_repair["errors"]),
            ]
        )
        if page_repair["warning"]:
            warnings.append((pdf.stem, str(page_repair["warning"])))
    print(
        markdown_table(
            [
                "case", "subject", "problems", "passages", "total_ms",
                "repair_attempted", "repair_cached", "repair_changed", "repair_applied", "repair_model", "repair_errors",
            ],
            rows,
        )
    )
    # stdout carries the whole finding, because stdout is what gets redirected
    # into a file or pasted into docs/web-trial-quality.md; stderr carries the
    # operator's copy for the run they are watching (score.py's _warn split).
    if warnings:
        print()
        print(f"> **{len(warnings)} of {len(rows)} case(s) are NOT evidence of AI-grade recognition.**")
        for case_name, warning in warnings:
            print(f"> - `{case_name}`: {warning}")
    for case_name, warning in warnings:
        print(f"oracle.py: WARNING {case_name}: {warning}", file=sys.stderr)
    return 1 if failed_cases else 0


if __name__ == "__main__":
    raise SystemExit(main())
