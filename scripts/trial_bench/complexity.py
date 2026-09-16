"""How parse time grows with words and drawings per page (synthetic PDFs, default
pages = the trial's own page cap).

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 500 1000 2000 4000 8000 --drawings 0 2000 8000
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 500 --drawings 3000 4000 5000 6000 --pages 3
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 4500 --drawings 1500 2000 2500 --pages 4 --board
Vercel estimate = local × 4.3 (docs/web-trial-spike-results.md §2-3). ``--pages`` controls
both how many pages the synthetic PDF gets and the max_pages cap the parse runs with, so a
sweep can be reproduced at a page count other than the trial's current cap.
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import inspect_pdf, parse_problems  # noqa: E402
from scripts.trial_bench.common import MAX_PAGES, markdown_table, parse_in_scratch  # noqa: E402

VERCEL_FACTOR = 4.3
LINE = "이 문장은 복잡도 실험을 위한 채움 글입니다 하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열"  # 14 words
WORDS_PER_LINE = 15
TOP_Y = 40.0
BOTTOM_Y = 1150.0
MAX_PITCH = 11.0
MAX_FONTSIZE = 8.0


def line_layout(words_per_page: int) -> tuple[float, float]:
    """Line pitch and font size that fit ``words_per_page`` between TOP_Y and BOTTOM_Y.

    A fixed 11 pt pitch stops at ~101 lines, so every target above ~1500 words
    hit the page-bottom guard first and produced the same saturated page: the
    documented `--words 500 1000 2000 4000 8000` sweep collapsed its top three
    levels into one measurement. Deriving the pitch (and a font size that keeps
    the same 8/11 text-to-pitch ratio) from the target keeps the dense end of
    the sweep a real data point.
    """
    lines = max(1, math.ceil(words_per_page / WORDS_PER_LINE))
    pitch = min(MAX_PITCH, (BOTTOM_Y - TOP_Y) / lines)
    return pitch, min(MAX_FONTSIZE, pitch * MAX_FONTSIZE / MAX_PITCH)


def write_synthetic(path: Path, *, words_per_page: int, drawings_per_page: int, pages: int = MAX_PAGES) -> Path:
    doc = fitz.open()
    pitch, fontsize = line_layout(words_per_page)
    number = 1
    for page_index in range(pages):
        page = doc.new_page(width=842, height=1191)  # A3-ish like CSAT papers at 72 pt/in
        y = TOP_Y
        words = 0
        line_index = 0
        while words < words_per_page and y < BOTTOM_Y:
            if line_index % 5 == 0:
                page.insert_text((40, y), f"{number}. {LINE}", fontsize=fontsize)
                number += 1
            else:
                page.insert_text((40, y), LINE, fontsize=fontsize)
            words += WORDS_PER_LINE
            line_index += 1
            y += pitch
        for index in range(drawings_per_page):
            x = 40 + (index % 70) * 11
            yy = 40 + (index // 70) * 11
            page.draw_line((x, yy), (x + 8, yy), color=(0, 0, 0), width=0.3)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--words", type=int, nargs="+", default=[500, 1000, 2000, 4000, 8000])
    parser.add_argument("--drawings", type=int, nargs="+", default=[0, 2000, 8000])
    parser.add_argument(
        "--pages",
        type=int,
        default=MAX_PAGES,
        help=(
            "pages per synthetic PDF, and the max_pages cap the parse runs with "
            "(default: the trial's own page cap, currently %(default)s). Pass e.g. "
            "--pages 3 to reproduce a sweep run before the cap moved to 4."
        ),
    )
    parser.add_argument(
        "--board",
        action="store_true",
        help="also render the chalk cutouts, as the trial does with TRIAL_BOARD_PREVIEWS on",
    )
    args = parser.parse_args(argv)
    rows = []
    with tempfile.TemporaryDirectory(prefix="trial-complexity-") as temp_dir:
        for words in args.words:
            for drawings in args.drawings:
                pdf = write_synthetic(Path(temp_dir) / f"w{words}_d{drawings}.pdf", words_per_page=words, drawings_per_page=drawings, pages=args.pages)
                info = inspect_pdf(pdf, max_pages=args.pages)
                result = parse_in_scratch(pdf, parse_problems, max_pages=args.pages, render_board_assets=args.board)
                total = result.timing_ms["total"]
                rows.append([info.max_words_per_page, info.max_drawings_per_page, result.timing_ms.get("render"), result.timing_ms.get("segment"), result.timing_ms.get("assets"), total, round(total * VERCEL_FACTOR / 1000, 1), len(result.problems)])
    print(markdown_table(["words/pg", "drawings/pg", "render_ms", "segment_ms", "assets_ms", "total_ms", "vercel_est_s", "problems"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
