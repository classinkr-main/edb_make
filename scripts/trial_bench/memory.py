"""Peak RSS of overlapping parses per input file, each in its own process.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py ~/edb-trial-bench/inputs/*.pdf [--synthetic-2xa3 [PAGES ...]]
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py exam.pdf --concurrency 1
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py exam.pdf --repeat 12 --rss-threshold-mb 1228.8

``--synthetic-2xa3`` takes zero or more explicit page counts and adds one synthetic
2x-A3 case per count, named ``2xa3_<pages>p.pdf`` (with no value, it uses the trial's
own page cap). Put real file arguments before this flag. ``--concurrency`` (1 or 2,
default 2) sets how many overlapping parses share the isolated child process: 2 models
``TRIAL_PARSE_CONCURRENCY=2``, 1 models the shipped default of one parse per instance.
``--repeat`` measures each case that many times and prints a min/median/mean/max summary
line per case instead of a single row; ``--rss-threshold-mb`` adds an over-threshold count
to that summary.
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
    process that has done nothing else -- see run_two_overlapping_parses and
    run_single_parse, which measure_case gives that fresh process to run in.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(usage / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def write_2xa3(path: Path, *, pages: int = DEFAULT_MAX_PAGES) -> Path:
    """Synthetic 2x-A3 case (spec section 6's worst-case geometry): ``pages``
    pages, 90 lines each. Defaults to the trial's own page cap so a bare call
    still reflects whatever the trial currently parses; pass ``pages``
    explicitly to reproduce a case at a different page count (e.g. the
    spec's literal 3-page worst case after the cap moved to 4).
    """
    doc = fitz.open()
    for page_index in range(pages):
        page = doc.new_page(width=1190, height=1684)  # twice A3 in area
        for line in range(90):
            page.insert_text((40, 40 + line * 18), f"{page_index * 30 + line // 3 + 1}. 큰 페이지 문항 본문 줄 {line} ① a ② b ③ c ④ d ⑤ e", fontsize=9)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def run_two_overlapping_parses(pdf: Path, *, board: bool = False) -> dict:
    """Parse ``pdf`` twice, overlapped, and read this process's own peak RSS.

    Models ``TRIAL_PARSE_CONCURRENCY=2``. ``parse_in_scratch`` pins
    ``max_pages`` to the trial's own cap by default, so ``pages`` below is
    however many of the file's leading pages the trial would actually parse.
    Meant to run inside a process that does nothing else: see measure_case.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: parse_in_scratch(pdf, parse_problems, render_board_assets=board), range(2)))
    return {
        "pages": len(results[0].pages),
        "rss_peak_mb": max_rss_mb(),
        "total_ms_a": results[0].timing_ms["total"],
        "total_ms_b": results[1].timing_ms["total"],
    }


def run_single_parse(pdf: Path, *, board: bool = False) -> dict:
    """Peak RSS of one isolated parse, in this process.

    Models the shipped default ``TRIAL_PARSE_CONCURRENCY=1`` (one parse per
    instance at a time), for contrast with run_two_overlapping_parses's
    two-at-once model of ``TRIAL_PARSE_CONCURRENCY=2``. Meant to run inside a
    process that does nothing else: see measure_case's ``concurrency`` handling.
    """
    result = parse_in_scratch(pdf, parse_problems, render_board_assets=board)
    return {
        "pages": len(result.pages),
        "rss_peak_mb": max_rss_mb(),
        "total_ms": result.timing_ms["total"],
    }


def measure_case(pdf: Path, *, concurrency: int = 2, board: bool = False) -> dict:
    """Run run_two_overlapping_parses(pdf) or run_single_parse(pdf) in a fresh child process.

    Re-invoking this same script with --worker is the isolation boundary: the
    child starts a brand new process, so its ru_maxrss can only reflect this
    one file's parse(s), never an earlier case's peak.

    Returns ``{"error": message}`` instead of raising when the child fails,
    or exits 0 with no output at all: main() runs this once per file in a
    plain loop with no try/except of its own, so an exception raised here
    used to discard every row already measured before it and print no table.
    A failed case must cost only its own row.
    """
    command = [sys.executable, str(SCRIPT_PATH), "--concurrency", str(concurrency), "--worker", str(pdf)]
    if board:
        command.append("--board")
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr, file=sys.stderr)
        return {"error": (exc.stderr or "").strip() or f"worker exited {exc.returncode}"}
    lines = proc.stdout.strip().splitlines()
    if not lines:
        # The child exited 0 but printed nothing -- e.g. a --worker code path
        # that returns before its own json.dumps print. Indexing the last
        # line unconditionally used to raise IndexError here instead.
        return {"error": "worker exited 0 but printed no output"}
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {"error": f"worker output was not valid JSON ({exc}): {lines[-1]!r}"}
    if not isinstance(data, dict):
        # json.loads succeeds for any JSON value, not just objects -- a
        # worker line of "null", "5", "\"done\"" or "[1, 2]" all parse
        # cleanly. main()'s `"error" in result` and `result["pages"]` both
        # assume a dict; anything else raises TypeError there instead of
        # being reported as this one case's row, discarding every row
        # already measured before it.
        return {"error": f"worker output was not a JSON object: {lines[-1]!r}"}
    return data


def _total_ms_headers(concurrency: int) -> list[str]:
    return ["total_ms"] if concurrency == 1 else ["total_ms_a", "total_ms_b"]


def _total_ms_values(result: dict, concurrency: int) -> list:
    return [result["total_ms"]] if concurrency == 1 else [result["total_ms_a"], result["total_ms_b"]]


def summarize_repeats(peaks: list[float], threshold_mb: float | None) -> str:
    """One-line min/median/mean/max (+ optional over-threshold count) summary
    of ``peaks`` (one rss_peak_mb per repeat), for a case measured with
    ``--repeat`` -- e.g. a borderline case whose RSS peak moves by tens of MB
    run to run (docs/web-trial-load.md section 4's "repeat noise" note).
    """
    ordered = sorted(peaks)
    n = len(ordered)
    median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    mean = sum(ordered) / n
    text = f"n={n} min={ordered[0]:.1f} median={median:.1f} mean={mean:.1f} max={ordered[-1]:.1f}"
    if threshold_mb is not None:
        over = sum(1 for value in peaks if value > threshold_mb)
        text += f" over_{threshold_mb:g}mb={over}/{n}"
    return text


def _case_kwargs(args: argparse.Namespace) -> dict:
    # Only mention ``board`` when asked, so callers that stub measure_case with the
    # older (pdf, *, concurrency) signature keep working for runs without --board.
    return {"board": True} if args.board else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", nargs="*", type=Path)
    parser.add_argument(
        "--synthetic-2xa3",
        type=int,
        nargs="*",
        default=None,
        metavar="PAGES",
        help=(
            "add a synthetic 2x-A3 case (spec section 6's worst-case geometry) for each PAGES "
            "value given, named 2xa3_<PAGES>p.pdf; with no value, uses the trial's own page cap "
            f"(currently {DEFAULT_MAX_PAGES}). Put real file arguments before this flag."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        choices=(1, 2),
        default=2,
        help=(
            "overlapping parses per case inside the isolated child process: 2 (default) models "
            "TRIAL_PARSE_CONCURRENCY=2, 1 models the shipped default of one parse per instance"
        ),
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "measure each case this many times (a fresh child process per run) and print a "
            "min/median/mean/max summary line per case instead of a single row"
        ),
    )
    parser.add_argument(
        "--rss-threshold-mb",
        type=float,
        default=None,
        help="with --repeat > 1, also report how many runs' rss_peak_mb exceeded this value",
    )
    parser.add_argument("--board", action="store_true", help="render chalk cutouts too (TRIAL_BOARD_PREVIEWS on)")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)  # internal: this script re-invokes itself
    args = parser.parse_args(argv)

    if args.worker is not None:
        run = run_single_parse if args.concurrency == 1 else run_two_overlapping_parses
        payload = run(args.worker, **_case_kwargs(args))
        print(json.dumps(payload))
        return 0

    ms_headers = _total_ms_headers(args.concurrency)
    rows: list[list] = []
    failed = False
    with tempfile.TemporaryDirectory(prefix="trial-memory-") as temp_dir:
        pdfs = list(args.pdfs)
        if args.synthetic_2xa3 is not None:
            for pages in (args.synthetic_2xa3 or [DEFAULT_MAX_PAGES]):
                pdfs.append(write_2xa3(Path(temp_dir) / f"2xa3_{pages}p.pdf", pages=pages))

        if args.repeat <= 1:
            for pdf in pdfs:
                result = measure_case(pdf, concurrency=args.concurrency, **_case_kwargs(args))
                if "error" in result:
                    # A failed case reports as its own row -- not a silently
                    # dropped one, and not an exception that would discard every
                    # row already measured before it.
                    print(f"memory.py: {pdf.name}: {result['error']}", file=sys.stderr)
                    rows.append([pdf.name, "ERROR", "-"] + ["-"] * len(ms_headers))
                    failed = True
                    continue
                rows.append([pdf.name, result["pages"], result["rss_peak_mb"]] + _total_ms_values(result, args.concurrency))
            print(markdown_table(["file", "pages", "rss_peak_mb"] + ms_headers, rows))
            print(
                f"pages is how many leading pages were parsed (the trial's own cap is {DEFAULT_MAX_PAGES}); "
                f"rss_peak_mb is that single file's own peak RSS for {args.concurrency} overlapping "
                "parse(s), measured in a fresh child process per row, so it cannot inherit an earlier row's peak."
            )
        else:
            summaries: list[str] = []
            for pdf in pdfs:
                peaks: list[float] = []
                for run_index in range(1, args.repeat + 1):
                    result = measure_case(pdf, concurrency=args.concurrency, **_case_kwargs(args))
                    if "error" in result:
                        print(f"memory.py: {pdf.name} run {run_index}/{args.repeat}: {result['error']}", file=sys.stderr)
                        rows.append([pdf.name, run_index, "ERROR", "-"] + ["-"] * len(ms_headers))
                        failed = True
                        continue
                    peaks.append(result["rss_peak_mb"])
                    rows.append([pdf.name, run_index, result["pages"], result["rss_peak_mb"]] + _total_ms_values(result, args.concurrency))
                if peaks:
                    summaries.append(f"{pdf.name}: {summarize_repeats(peaks, args.rss_threshold_mb)}")
            print(markdown_table(["file", "run", "pages", "rss_peak_mb"] + ms_headers, rows))
            for line in summaries:
                print(line)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
