"""How parse time grows with words and drawings per page (synthetic 3-page PDFs).

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 500 1000 2000 4000 8000 --drawings 0 2000 8000
Vercel estimate = local × 4.3 (docs/web-trial-spike-results.md §2-3).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import inspect_pdf, parse_problems  # noqa: E402
from scripts.trial_bench.common import markdown_table, parse_in_scratch  # noqa: E402

VERCEL_FACTOR = 4.3
LINE = "이 문장은 복잡도 실험을 위한 채움 글입니다 하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열"  # 14 words


def write_synthetic(path: Path, *, words_per_page: int, drawings_per_page: int, pages: int = 3) -> Path:
    doc = fitz.open()
    number = 1
    for page_index in range(pages):
        page = doc.new_page(width=842, height=1191)  # A3-ish like CSAT papers at 72 pt/in
        y = 40.0
        words = 0
        line = 0
        while words < words_per_page and y < 1150:
            if line % 5 == 0:
                page.insert_text((40, y), f"{number}. {LINE}", fontsize=8)
                number += 1
            else:
                page.insert_text((40, y), LINE, fontsize=8)
            words += 15
            line += 1
            y += 11
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
    args = parser.parse_args(argv)
    rows = []
    with tempfile.TemporaryDirectory(prefix="trial-complexity-") as temp_dir:
        for words in args.words:
            for drawings in args.drawings:
                pdf = write_synthetic(Path(temp_dir) / f"w{words}_d{drawings}.pdf", words_per_page=words, drawings_per_page=drawings)
                info = inspect_pdf(pdf, max_pages=3)
                result = parse_in_scratch(pdf, parse_problems)
                total = result.timing_ms["total"]
                rows.append([info.max_words_per_page, info.max_drawings_per_page, result.timing_ms.get("render"), result.timing_ms.get("segment"), result.timing_ms.get("assets"), total, round(total * VERCEL_FACTOR / 1000, 1), len(result.problems)])
    print(markdown_table(["words/pg", "drawings/pg", "render_ms", "segment_ms", "assets_ms", "total_ms", "vercel_est_s", "problems"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
