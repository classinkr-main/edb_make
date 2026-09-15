"""Run the trial parser on every bench input; store observations and crops.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/observe.py [case ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, bench_dir, markdown_table, observation_from_result, parse_in_scratch, save_json  # noqa: E402


def observe_case(input_pdf: Path, root: Path = BENCH_ROOT) -> dict[str, Any]:
    case = input_pdf.stem
    result = parse_in_scratch(input_pdf, parse_problems)
    observation = observation_from_result(case, result, crops_dir=bench_dir("trial_crops", root) / case)
    save_json(bench_dir("trial", root) / f"{case}.json", observation)
    return observation


def select_inputs(cases: list[str], root: Path = BENCH_ROOT) -> list[Path]:
    inputs = sorted(bench_dir("inputs", root).glob("*.pdf"))
    if cases:
        wanted = set(cases)
        inputs = [path for path in inputs if path.stem in wanted]
    return inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    rows = []
    for pdf in select_inputs(args.cases):
        observation = observe_case(pdf)
        rows.append([pdf.stem, observation["pages"], len(observation["problems"]), len(observation["passage_ranges"]), observation["timing_ms"].get("total")])
    print(markdown_table(["case", "pages", "problems", "passages", "total_ms"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
