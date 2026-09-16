"""Peak RSS when two parses overlap, as they can on one Fluid instance.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py ~/edb-trial-bench/inputs/*.pdf [--synthetic-2xa3]
"""

from __future__ import annotations

import argparse
import resource
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench.common import markdown_table, parse_in_scratch  # noqa: E402


def max_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(usage / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def write_2xa3(path: Path) -> Path:
    doc = fitz.open()
    for page_index in range(3):
        page = doc.new_page(width=1190, height=1684)  # twice A3 in area
        for line in range(90):
            page.insert_text((40, 40 + line * 18), f"{page_index * 30 + line // 3 + 1}. 큰 페이지 문항 본문 줄 {line} ① a ② b ③ c ④ d ⑤ e", fontsize=9)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", nargs="*", type=Path)
    parser.add_argument("--synthetic-2xa3", action="store_true")
    args = parser.parse_args(argv)
    rows = []
    with tempfile.TemporaryDirectory(prefix="trial-memory-") as temp_dir:
        pdfs = list(args.pdfs)
        if args.synthetic_2xa3:
            pdfs.append(write_2xa3(Path(temp_dir) / "2xa3.pdf"))
        for pdf in pdfs:
            before = max_rss_mb()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: parse_in_scratch(pdf, parse_problems), range(2)))
            rows.append([pdf.name, before, max_rss_mb(), results[0].timing_ms["total"], results[1].timing_ms["total"]])
    print(markdown_table(["file", "rss_before_mb", "rss_after_mb", "total_ms_a", "total_ms_b"], rows))
    print("rss is process-wide and cumulative: run small files first, and read each row as 'at most this much for two overlapping parses'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
