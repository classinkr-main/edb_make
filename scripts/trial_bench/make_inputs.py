"""Create the trial's exact input (first 4 pages, compacted) for each exam PDF.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/make_inputs.py --subject korean /path/a.pdf /path/b.pdf
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import _leading_pages_copy  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, MAX_PAGES, bench_dir, case_id, load_json, save_json  # noqa: E402


def make_input(source: Path, subject: str, root: Path = BENCH_ROOT) -> Path:
    case = case_id(source)
    target = bench_dir("inputs", root) / f"{case}.pdf"
    with tempfile.TemporaryDirectory(prefix="trial-bench-") as temp_dir:
        trimmed, page_count = _leading_pages_copy(source, Path(temp_dir), MAX_PAGES)
        shutil.copyfile(trimmed, target)
    cases_path = root / "cases.json"
    cases = load_json(cases_path) if cases_path.is_file() else {}
    cases[case] = {"subject": subject, "source_page_count": page_count, "source_name": source.name}
    save_json(cases_path, cases)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject", required=True, help="korean, english, math, science, social, unknown")
    parser.add_argument("pdfs", nargs="+", type=Path)
    args = parser.parse_args(argv)
    for source in args.pdfs:
        target = make_input(source, args.subject)
        print(f"{source.name} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
