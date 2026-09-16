"""Peak RSS of two overlapping parses per input file, each in its own process.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py ~/edb-trial-bench/inputs/*.pdf [--synthetic-2xa3]
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench.common import markdown_table, parse_in_scratch  # noqa: E402
from trial_input import DEFAULT_MAX_PAGES  # noqa: E402

SCRIPT_PATH = Path(__file__).resolve()


def max_rss_mb() -> float:
    """This process's own peak RSS so far.

    ru_maxrss is a process-lifetime high-water mark that never resets: reading
    it after measuring several files in one process makes every row inherit
    whichever prior row pushed it highest. Callers must only call this from a
    process that has done nothing else -- see run_two_overlapping_parses,
    which measure_case gives that fresh process to run in.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(usage / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def write_2xa3(path: Path) -> Path:
    doc = fitz.open()
    for page_index in range(DEFAULT_MAX_PAGES):
        page = doc.new_page(width=1190, height=1684)  # twice A3 in area
        for line in range(90):
            page.insert_text((40, 40 + line * 18), f"{page_index * 30 + line // 3 + 1}. 큰 페이지 문항 본문 줄 {line} ① a ② b ③ c ④ d ⑤ e", fontsize=9)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def run_two_overlapping_parses(pdf: Path) -> dict:
    """Parse ``pdf`` twice, overlapped, and read this process's own peak RSS.

    ``parse_in_scratch`` pins ``max_pages`` to the trial's own cap (it now
    imports ``trial_input.DEFAULT_MAX_PAGES`` itself), so ``pages`` below is
    however many of the file's leading pages the trial would actually parse.
    Meant to run inside a process that does nothing else: see measure_case.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: parse_in_scratch(pdf, parse_problems), range(2)))
    return {
        "pages": len(results[0].pages),
        "rss_peak_mb": max_rss_mb(),
        "total_ms_a": results[0].timing_ms["total"],
        "total_ms_b": results[1].timing_ms["total"],
    }


def measure_case(pdf: Path) -> dict:
    """Run run_two_overlapping_parses(pdf) in a fresh child process.

    Re-invoking this same script with --worker is the isolation boundary: the
    child starts a brand new process, so its ru_maxrss can only reflect this
    one file's two overlapping parses, never an earlier case's peak.
    """
    command = [sys.executable, str(SCRIPT_PATH), "--worker", str(pdf)]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr)
        raise
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", nargs="*", type=Path)
    parser.add_argument("--synthetic-2xa3", action="store_true")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)  # internal: this script re-invokes itself
    args = parser.parse_args(argv)

    if args.worker is not None:
        print(json.dumps(run_two_overlapping_parses(args.worker)))
        return 0

    rows = []
    with tempfile.TemporaryDirectory(prefix="trial-memory-") as temp_dir:
        pdfs = list(args.pdfs)
        if args.synthetic_2xa3:
            pdfs.append(write_2xa3(Path(temp_dir) / "2xa3.pdf"))
        for pdf in pdfs:
            result = measure_case(pdf)
            rows.append([pdf.name, result["pages"], result["rss_peak_mb"], result["total_ms_a"], result["total_ms_b"]])
    print(markdown_table(["file", "pages", "rss_peak_mb", "total_ms_a", "total_ms_b"], rows))
    print(
        f"pages is how many leading pages were parsed (the trial's own cap is {DEFAULT_MAX_PAGES}); "
        "rss_peak_mb is that single file's own peak RSS for two overlapping parses, measured in a "
        "fresh child process per row, so it cannot inherit an earlier row's peak."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
