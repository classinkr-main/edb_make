"""Which passage units continue past the trial's page cap.

Pure functions on ParseResult (spec 2026-09-17 page-cut continuation §3). The
only reliable signal is the passage range in the unit title: numbers in
"지문 a~b" above the highest number found anywhere must sit on later pages.
Geometry is useless here because pages are margin-cropped, so complete exams
also end flush with the page bottom.
"""

from __future__ import annotations

import re
from typing import Any

from problem_parser import ParseResult

PASSAGE_RANGE = re.compile(r"지문\s*(\d+)\s*[~∼～\-–]\s*(\d+)")


def passage_range(title: str) -> tuple[int, int] | None:
    match = PASSAGE_RANGE.search(title or "")
    if match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return (start, end) if start <= end else None


def continuations(result: ParseResult) -> dict[str, dict[str, Any]]:
    """{problem_id: {"numbers": [...], "page": next_page}} for passages cut by the page cap."""
    processed = len(result.pages)
    if result.source_page_count <= processed:
        return {}
    found = [problem.number for problem in result.problems if problem.number is not None]
    if not found:
        return {}
    last_found = max(found)
    continued: dict[str, dict[str, Any]] = {}
    for problem in result.problems:
        if problem.number is not None:
            continue
        bounds = passage_range(problem.title)
        if bounds is None:
            continue
        missing = [number for number in range(bounds[0], bounds[1] + 1) if number > last_found]
        if missing:
            continued[problem.problem_id] = {"numbers": missing, "page": processed + 1}
    return continued
