"""Encode a ParseResult into the /api/parse JSON body within Vercel's body limit."""

from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass
from typing import Any

from PIL import Image, features

from problem_parser import ParseResult
from trial_continuation import continuations

# Vercel caps function response bodies at 4.5 MB; leave room for headers and slack.
RESPONSE_BUDGET_BYTES = 3_500_000

# WebP lossy is 25-40% smaller than JPEG at the same visual quality (spec 2026-09-16 §2-3).
# The pinned Pillow wheel ships libwebp; the JPEG fallback only guards a build without it.
PREVIEW_FORMAT = "WEBP" if features.check("webp") else "JPEG"
PREVIEW_MIME = "image/webp" if PREVIEW_FORMAT == "WEBP" else "image/jpeg"
# method 4 is ~1.7x slower for ~3% smaller output; method 6 takes minutes per request.
WEBP_METHOD = 2

# BOARD_THEME_PALETTES["charcoal"]["background"] in build_problem_board_edb. Kept as a
# constant so this module never imports the pipeline (rejected requests must not load
# OpenCV); test_trial_preview asserts the two stay equal.
BOARD_BACKGROUND_RGB = (24, 28, 32)


class PreviewBudgetExceeded(ValueError):
    """Even the smallest preview would exceed the function response budget."""

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
    # None drops the board previews at this step (last resort before rejecting).
    board_long_side: int | None = None


PREVIEW_STEPS = (
    PreviewStep(page_long_side=1000, problem_long_side=800, quality=78, board_long_side=800),
    PreviewStep(page_long_side=1000, problem_long_side=600, quality=65, board_long_side=600),
    PreviewStep(page_long_side=900, problem_long_side=600, quality=65, board_long_side=600),
    PreviewStep(page_long_side=700, problem_long_side=450, quality=55, board_long_side=None),
)


def _downscale(image: Image.Image, long_side: int) -> Image.Image:
    scale = long_side / max(image.size)
    if scale >= 1:
        return image
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    # HAMMING instead of LANCZOS: same preview size, roughly half the resize cost (LANCZOS
    # resize x25 cost about 0.17 s per request locally, ~0.7 s on Vercel) for no visible gain
    # at preview sizes. reducing_gap=2.0 additionally lets Pillow run an integer reduce()
    # first, which only engages on the >4x fallback steps.
    return image.resize(size, Image.Resampling.HAMMING, reducing_gap=2.0)


def encode_preview_data_uri(image: Image.Image, *, long_side: int, quality: int) -> str:
    preview = _downscale(image.convert("RGB"), long_side)
    buffer = io.BytesIO()
    if PREVIEW_FORMAT == "WEBP":
        preview.save(buffer, format="WEBP", quality=quality, method=WEBP_METHOD)
    else:
        preview.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=False)
    return f"data:{PREVIEW_MIME};base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def compose_board_preview(board_image: Image.Image) -> Image.Image:
    """Flatten a chalk-on-transparent cutout onto the charcoal board color."""
    rgba = board_image.convert("RGBA")
    flat = Image.new("RGBA", rgba.size, BOARD_BACKGROUND_RGB + (255,))
    flat.alpha_composite(rgba)
    return flat.convert("RGB")


def _payload_for_step(
    result: ParseResult,
    step_index: int,
    *,
    remaining_today: int | None,
    elapsed_ms: int,
    processed_page_limit: int,
    extra: dict[str, Any] | None = None,
    continuation_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    step = PREVIEW_STEPS[step_index]
    continuation_by_id = continuation_by_id or {}
    problems = []
    for problem in result.problems:
        board = None
        if step.board_long_side is not None and problem.board_image is not None:
            board = encode_preview_data_uri(
                compose_board_preview(problem.board_image), long_side=step.board_long_side, quality=step.quality
            )
        problems.append(
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
                "preview": encode_preview_data_uri(problem.image, long_side=step.problem_long_side, quality=step.quality),
                "board": board,
                # Passage whose remaining questions sit past the page cap (trial_continuation).
                "continuation": continuation_by_id.get(problem.problem_id),
            }
        )
    payload: dict[str, Any] = {
        "parser_version": result.parser_version,
        "elapsed_ms": elapsed_ms,
        "source_page_count": result.source_page_count,
        "processed_page_count": len(result.pages),
        "processed_page_limit": processed_page_limit,
        "remaining_today": remaining_today,
        "preview_step": step_index,
        "board_previews": any(problem["board"] is not None for problem in problems),
        "pages": [
            {
                "page_id": page.page_id,
                "index": page.index,
                "width": page.width,
                "height": page.height,
                "preview": encode_preview_data_uri(page.image, long_side=step.page_long_side, quality=step.quality),
            }
            for page in result.pages
        ],
        "problems": problems,
    }
    if extra:
        payload.update(extra)
    return payload


def build_parse_body(
    result: ParseResult,
    *,
    remaining_today: int | None,
    elapsed_ms: int,
    processed_page_limit: int,
    budget_bytes: int = RESPONSE_BUDGET_BYTES,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Return the first preview and JSON within budget, or reject an oversized result."""
    payload: dict[str, Any] = {}
    body = b""
    continuation_by_id = continuations(result)  # step-independent; computed once
    for step_index in range(len(PREVIEW_STEPS)):
        payload = _payload_for_step(
            result,
            step_index,
            remaining_today=remaining_today,
            elapsed_ms=elapsed_ms,
            processed_page_limit=processed_page_limit,
            extra=extra,
            continuation_by_id=continuation_by_id,
        )
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) <= budget_bytes:
            return payload, body
    raise PreviewBudgetExceeded("Smallest preview exceeds the response budget")


def build_parse_payload(
    result: ParseResult,
    *,
    remaining_today: int | None,
    elapsed_ms: int,
    processed_page_limit: int,
    budget_bytes: int = RESPONSE_BUDGET_BYTES,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_parse_body(
        result,
        remaining_today=remaining_today,
        elapsed_ms=elapsed_ms,
        processed_page_limit=processed_page_limit,
        budget_bytes=budget_bytes,
        extra=extra,
    )[0]
