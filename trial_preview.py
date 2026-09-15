"""Encode a ParseResult into the /api/parse JSON body within Vercel's body limit."""

from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass
from typing import Any

from PIL import Image

from problem_parser import ParseResult

# Vercel caps function response bodies at 4.5 MB; leave room for headers and slack.
RESPONSE_BUDGET_BYTES = 3_500_000

# Flags that mean the problem boundary itself is uncertain. Desktop review
# hints such as passage_cross_page_merge_check tag half of a normal Korean
# exam, so the trial does not badge them.
REVIEW_WORTHY_FLAGS = frozenset({"fallback_grouping", "merged_problem_block", "marker_conflicts", "hwp_oversegmentation"})


def needs_review(risk_flags: list[str]) -> bool:
    return any(flag in REVIEW_WORTHY_FLAGS for flag in risk_flags)


def _finite_bbox(region: Any) -> dict[str, float] | None:
    values = {
        "left": float(region.bbox.left),
        "top": float(region.bbox.top),
        "width": float(region.bbox.width),
        "height": float(region.bbox.height),
    }
    # Strict JSON has no NaN or Infinity; a region we cannot draw is better dropped.
    return values if all(math.isfinite(value) for value in values.values()) else None


@dataclass(frozen=True)
class PreviewStep:
    page_long_side: int
    problem_long_side: int
    quality: int


PREVIEW_STEPS = (
    PreviewStep(page_long_side=1200, problem_long_side=800, quality=78),
    PreviewStep(page_long_side=1200, problem_long_side=600, quality=65),
    PreviewStep(page_long_side=900, problem_long_side=600, quality=65),
    PreviewStep(page_long_side=700, problem_long_side=450, quality=55),
)


def encode_jpeg_data_uri(image: Image.Image, *, long_side: int, quality: int) -> str:
    preview = image.convert("RGB")
    scale = long_side / max(preview.size)
    if scale < 1:
        size = (max(1, round(preview.width * scale)), max(1, round(preview.height * scale)))
        preview = preview.resize(size, Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    preview.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=False)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _payload_for_step(
    result: ParseResult,
    step_index: int,
    *,
    remaining_today: int,
    elapsed_ms: int,
    processed_page_limit: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step = PREVIEW_STEPS[step_index]
    payload: dict[str, Any] = {
        "parser_version": result.parser_version,
        "elapsed_ms": elapsed_ms,
        "source_page_count": result.source_page_count,
        "processed_page_count": len(result.pages),
        "processed_page_limit": processed_page_limit,
        "remaining_today": remaining_today,
        "preview_step": step_index,
        "pages": [
            {
                "page_id": page.page_id,
                "index": page.index,
                "width": page.width,
                "height": page.height,
                "preview": encode_jpeg_data_uri(page.image, long_side=step.page_long_side, quality=step.quality),
            }
            for page in result.pages
        ],
        "problems": [
            {
                "problem_id": problem.problem_id,
                "number": problem.number,
                "title": problem.title,
                "regions": [
                    {"page_id": region.page_id, "bbox": bbox}
                    for region in problem.regions
                    if (bbox := _finite_bbox(region)) is not None
                ],
                "risk_flags": list(problem.risk_flags),
                "needs_review": needs_review(problem.risk_flags),
                "preview": encode_jpeg_data_uri(problem.image, long_side=step.problem_long_side, quality=step.quality),
            }
            for problem in result.problems
        ],
    }
    if extra:
        payload.update(extra)
    return payload


def build_parse_payload(
    result: ParseResult,
    *,
    remaining_today: int,
    elapsed_ms: int,
    processed_page_limit: int,
    budget_bytes: int = RESPONSE_BUDGET_BYTES,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the first preview step whose JSON fits the budget, else the smallest step."""
    payload: dict[str, Any] = {}
    for step_index in range(len(PREVIEW_STEPS)):
        payload = _payload_for_step(
            result,
            step_index,
            remaining_today=remaining_today,
            elapsed_ms=elapsed_ms,
            processed_page_limit=processed_page_limit,
            extra=extra,
        )
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= budget_bytes:
            return payload
    return payload
