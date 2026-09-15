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
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table, observation_from_result, parse_in_scratch, save_json  # noqa: E402
from scripts.trial_bench.observe import select_inputs  # noqa: E402
from user_settings import apply_to_env, load_user_settings  # noqa: E402


def force_config(model: str = "") -> dict[str, Any]:
    """The desktop app's 'AI 정밀 인식' settings (app_server: ai_fallback='force'), failing loudly."""
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


def oracle_case(input_pdf: Path, subject: str, *, ocr_mode: str = "auto", model: str = "", root: Path = BENCH_ROOT) -> dict[str, Any]:
    case = input_pdf.stem
    result = parse_in_scratch(input_pdf, parse_problems, subject=subject, ocr_mode=ocr_mode, ai_fallback_config=force_config(model))
    observation = observation_from_result(case, result, crops_dir=bench_dir("oracle_crops", root) / case)
    observation["oracle"] = {"ocr_mode": ocr_mode, "model": model or "default", "ai_mode": "force"}
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
    for pdf in select_inputs(args.cases):
        subject = str(cases.get(pdf.stem, {}).get("subject") or "unknown")
        observation = oracle_case(pdf, subject, ocr_mode=args.ocr_mode, model=args.model)
        rows.append([pdf.stem, subject, len(observation["problems"]), len(observation["passage_ranges"]), observation["timing_ms"].get("total")])
    print(markdown_table(["case", "subject", "problems", "passages", "total_ms"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
