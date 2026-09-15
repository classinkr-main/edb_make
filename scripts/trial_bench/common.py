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
# Require the passage marker itself, not just two numbers anywhere in the
# title: a free-text title ("표는 1-3족 원소의 성질을...") can contain a bare
# "<digits><sep><digits>" run that has nothing to do with a passage range.
PASSAGE_RANGE = re.compile(r"(?:지문|passage)\s*(\d+)\s*[~∼～\-–]\s*(\d+)", re.IGNORECASE)
# A numbered problem's title is normally just its marker ("1.", "12번"); this
# is the only free-form title shape considered safe to echo back verbatim.
NUMBER_MARKER_TITLE = re.compile(r"\d+\s*번?\.?")


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


def _safe_title(number: int | None, title: str | None, span: list[int] | None) -> str | None:
    """Label-only title: a passage marker or a short number marker.

    Anything else is exam text a fallback-grouped or mis-numbered unit can
    carry (segment.py's ``display_title = text[:120]``), which spec 5-1 says
    must not appear here, so it is dropped rather than echoed back.
    """
    if span:
        return f"지문 {span[0]}~{span[1]}"
    if number is not None and title and NUMBER_MARKER_TITLE.fullmatch(title.strip()):
        return title
    return None


def _crop_stem(key: str, used: set[str]) -> str:
    """Filesystem-safe, collision-free crop stem for ``key``.

    The counter is appended *after* the length cap, never before: an
    unnumbered unit's key carries its display title (up to 120 characters
    from segment.py), so a counter appended to the key itself would be
    sliced off by the cap and the duplicates would overwrite one another.
    Two different long keys that agree on their first characters truncate
    onto one stem for the same reason, so the loop checks the final stem.
    """
    base = re.sub(r"[^\w가-힣.-]+", "_", key).strip("_")[:72] or "problem"
    stem = base
    counter = 1
    while stem in used:
        counter += 1
        stem = f"{base}_{counter}"
    used.add(stem)
    return stem


def observation_from_result(case: str, result: Any, *, crops_dir: Path | None = None) -> dict[str, Any]:
    """Privacy-minimized view of a ParseResult: numbers, boxes, flags. No text."""
    page_index = {page.page_id: page.index for page in result.pages}
    problems: list[dict[str, Any]] = []
    passage_ranges: list[list[int]] = []
    seen_keys: dict[str, int] = {}
    crop_stems: set[str] = set()
    for problem in result.problems:
        key = problem_key(problem.number, problem.title)
        seen_keys[key] = seen_keys.get(key, 0) + 1
        if seen_keys[key] > 1:
            # Unnumbered fallback-grouped units (e.g. every "이어지는 자료"
            # marker-continuation page) can share the same title and thus the
            # same key; without this, later entries would silently collapse
            # onto the first in both this JSON and the crop file on disk.
            key = f"{key}#{seen_keys[key]}"
        span = None if problem.number is not None else passage_range_from_title(problem.title)
        if span:
            passage_ranges.append(span)
        crop_path: Path | None = None
        if crops_dir is not None:
            crops_dir.mkdir(parents=True, exist_ok=True)
            crop_path = crops_dir / f"{_crop_stem(key, crop_stems)}.png"
            problem.image.save(crop_path)
        problems.append(
            {
                "key": key,
                "number": problem.number,
                "title": _safe_title(problem.number, problem.title, span),
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
