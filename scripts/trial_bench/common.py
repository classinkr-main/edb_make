"""Shared helpers for the trial measurement scripts.

Everything measured lives outside the repository under BENCH_ROOT
(default ~/edb-trial-bench): exam PDFs, observations, labels, crops.
Only scripts, tests, and result tables are committed.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

BENCH_ROOT = Path(os.environ.get("TRIAL_BENCH_ROOT") or Path.home() / "edb-trial-bench")
MAX_PAGES = 3
PASSAGE_RANGE = re.compile(r"(\d+)\s*[~∼～\-–]\s*(\d+)")


def bench_dir(name: str, root: Path = BENCH_ROOT) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def case_id(source: Path) -> str:
    stem = re.sub(r"[^\w가-힣.-]+", "_", source.stem).strip("_")
    return stem or "case"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def passage_range_from_title(title: str | None) -> list[int] | None:
    match = PASSAGE_RANGE.search(str(title or ""))
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return [min(start, end), max(start, end)]


def problem_key(number: int | None, title: str | None) -> str:
    if number is not None:
        return f"q{number}"
    span = passage_range_from_title(title)
    if span:
        return f"p{span[0]}-{span[1]}"
    return f"t:{title or ''}"


def observation_from_result(case: str, result: Any, *, crops_dir: Path | None = None) -> dict[str, Any]:
    """Privacy-minimized view of a ParseResult: numbers, boxes, flags. No text."""
    page_index = {page.page_id: page.index for page in result.pages}
    problems: list[dict[str, Any]] = []
    passage_ranges: list[list[int]] = []
    for problem in result.problems:
        key = problem_key(problem.number, problem.title)
        span = None if problem.number is not None else passage_range_from_title(problem.title)
        if span:
            passage_ranges.append(span)
        crop_path: Path | None = None
        if crops_dir is not None:
            crops_dir.mkdir(parents=True, exist_ok=True)
            crop_path = crops_dir / f"{key.replace(':', '_')}.png"
            problem.image.save(crop_path)
        problems.append(
            {
                "key": key,
                "number": problem.number,
                "title": problem.title,
                "passage_range": span,
                "regions": [
                    {
                        "page_index": page_index.get(region.page_id, -1),
                        "bbox": {
                            "left": float(region.bbox.left),
                            "top": float(region.bbox.top),
                            "width": float(region.bbox.width),
                            "height": float(region.bbox.height),
                        },
                    }
                    for region in problem.regions
                ],
                "risk_flags": list(problem.risk_flags),
                "crop": str(crop_path) if crop_path else None,
            }
        )
    return {
        "case": case,
        "pages": len(result.pages),
        "source_page_count": result.source_page_count,
        "page_sizes": [[page.width, page.height] for page in result.pages],
        "problems": problems,
        "passage_ranges": passage_ranges,
        "timing_ms": dict(result.timing_ms),
    }


def parse_in_scratch(source: Path, parse: Callable[..., Any], **kwargs: Any) -> Any:
    """Copy the PDF into a fresh temp dir first.

    A .pipeline_cache next to the input would make second runs unrealistically
    fast (0.2 s recognize). Returned images are detached, so the dir can go.
    """
    with tempfile.TemporaryDirectory(prefix="trial-bench-") as temp_dir:
        copied = Path(temp_dir) / source.name
        shutil.copyfile(source, copied)
        return parse(copied, work_dir=Path(temp_dir) / "work", max_pages=MAX_PAGES, **kwargs)


def percentile(values: Iterable[float], pct: float) -> float:
    """Nearest-rank percentile; empty input gives nan."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return math.nan
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)
