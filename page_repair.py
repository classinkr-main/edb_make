#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import math
import os
import re
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import zip_longest
from pathlib import Path
from typing import Any, NamedTuple
from urllib import error, request

from PIL import Image

from ai_usage import normalize_gemini_token_usage
from assemble_page import detect_choice_block, detect_problem_start, group_problem_units
from pipeline_cache import PipelineCache
from pipeline_router import decide_page_route
from preprocess import PreparedPage
from structured_schema import BlockType, ContentBlock, PageModel, ProblemUnit


GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_GEMINI_REPAIR_MODEL = "gemini-3.1-pro-preview"
FALLBACK_GEMINI_REPAIR_MODEL = "gemini-3.6-flash"
DEPRECATED_GEMINI_REPAIR_MODELS = {"gemini-3-pro-preview"}
AI_REPAIR_PROMPT_VERSION = "page_repair_v3_compact"
AI_REPAIR_IMAGE_MAX_DIMENSION = 2048
AI_REPAIR_IMAGE_JPEG_QUALITY = 78
_PROBLEM_UNIT_TRIGGER_REASONS = {
    "fallback_grouping",
    "full_page_image",
    "marker_conflicts",
    "merged_problem_block",
    "problem_per_block",
}

_SUPPORTED_PROVIDER_ALIASES = {"gemini", "google"}

# Gemini's finishReason values that mean "the response was cut off by the
# output token budget", as opposed to a genuinely malformed JSON response
# (finish_reason STOP) -- see GeminiRepairResponseError below.
_GEMINI_TRUNCATION_FINISH_REASONS = {"MAX_TOKENS", "LENGTH"}


class GeminiRepairResponseError(RuntimeError):
    """A Gemini page-repair response failed to parse as the expected JSON
    object, carrying the diagnostics needed to tell that failure apart from
    an output-token-budget truncation (``truncated``) instead of just a
    prose message. See ``_gemini_response_diagnostics`` for the fields.
    """

    def __init__(self, message: str, *, diagnostics: dict[str, Any], truncated: bool = False) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics
        self.truncated = truncated


class GeminiRepairTruncatedError(GeminiRepairResponseError):
    """The response's ``finishReason`` was MAX_TOKENS/LENGTH: Gemini stopped
    generating before the JSON closed because it ran out of output-token
    budget (``effective_max_output_tokens`` in ``diagnostics``), not because
    the model produced a malformed answer. Retrying the identical request is
    pointless -- ``temperature`` is 0.0, so the same budget reproduces the
    same truncation -- the fix is a bigger budget or a smaller unit of work.
    """

    def __init__(self, message: str, *, diagnostics: dict[str, Any]) -> None:
        super().__init__(message, diagnostics=diagnostics, truncated=True)


def _gemini_response_diagnostics(
    *,
    model: str,
    finish_reason: str,
    effective_max_output_tokens: int,
    configured_max_output_tokens: int,
    prompt: str,
    response_text: str,
    block_count: int,
    include_problem_units: bool,
) -> dict[str, Any]:
    """Everything needed to tell a genuine malformed-JSON response apart
    from an output-token-budget truncation, without re-running the request:
    the finishReason Gemini actually returned, the token budget the code
    computed for this call (vs. what force_config/build_ai_fallback_config
    configured), and enough of the raw response (sizes plus head/tail) to
    see where and how it broke without dumping the whole page's text into a
    log or an oracle failure record.
    """
    prompt_bytes = prompt.encode("utf-8")
    response_bytes = response_text.encode("utf-8")
    return {
        "model": model,
        "finish_reason": finish_reason,
        "effective_max_output_tokens": effective_max_output_tokens,
        "configured_max_output_tokens": configured_max_output_tokens,
        "block_count": block_count,
        "include_problem_units": include_problem_units,
        "prompt_char_count": len(prompt),
        "prompt_byte_count": len(prompt_bytes),
        "response_char_count": len(response_text),
        "response_byte_count": len(response_bytes),
        "response_head": response_text[:200],
        "response_tail": response_text[-200:],
    }


@dataclass(slots=True)
class AIFallbackConfig:
    mode: str = "off"
    provider: str = "gemini"
    model: str = ""
    threshold: float = 0.72
    max_regions: int = 48
    max_tokens: int = 4096
    timeout_ms: int = 30000
    save_debug: bool = False
    fail_on_error: bool = False
    # None (every desktop caller) means _repair_output_token_budget uses its
    # built-in per-block estimate and 2048/3072 hard cap, unchanged. Set only
    # by scripts/trial_bench/oracle.py's force_config, to bypass that
    # estimate for the forced-repair oracle path -- see
    # _repair_output_token_budget's docstring for why the estimate itself
    # (not just the hard cap) truncated real English pages.
    max_output_token_cap: int | None = None

    @property
    def resolved_model(self) -> str:
        if self.model.strip():
            return self.model.strip()
        return DEFAULT_GEMINI_REPAIR_MODEL

    @property
    def normalized_provider(self) -> str:
        # All AI repair traffic is served by Gemini now. Legacy provider
        # values are accepted but normalized to keep existing configs working.
        return "gemini"

    @property
    def normalized_mode(self) -> str:
        normalized = self.mode.strip().lower()
        if normalized in {"auto", "force"}:
            return normalized
        return "off"

    @property
    def enabled(self) -> bool:
        return self.normalized_mode != "off"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "mode": self.normalized_mode,
            "provider": self.normalized_provider,
            "model": self.resolved_model,
            "threshold": self.threshold,
            "max_regions": self.max_regions,
            "max_tokens": self.max_tokens,
            "timeout_ms": self.timeout_ms,
            "save_debug": self.save_debug,
            "fail_on_error": self.fail_on_error,
            "max_output_token_cap": self.max_output_token_cap,
        }


def build_ai_fallback_config(
    *,
    mode: str = "off",
    provider: str = "gemini",
    model: str = "",
    threshold: float = 0.72,
    max_regions: int = 48,
    max_tokens: int | None = None,
    timeout_ms: int = 30000,
    save_debug: bool = False,
    fail_on_error: bool = False,
    max_output_token_cap: int | None = None,
) -> AIFallbackConfig:
    return AIFallbackConfig(
        mode=mode,
        provider=provider,
        model=model,
        threshold=threshold,
        max_regions=max_regions,
        max_tokens=max(1024, int(max_tokens or 4096)),
        timeout_ms=timeout_ms,
        save_debug=save_debug,
        fail_on_error=fail_on_error,
        max_output_token_cap=int(max_output_token_cap) if max_output_token_cap else None,
    )


def _page_repair_stage_metadata(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": "page_repair",
        "order": 3,
        "label": "3단계 문항 경계 보정",
        "status": str(summary.get("status") or "unknown"),
        "provider": str(summary.get("provider") or "gemini"),
        "model": str(summary.get("model") or ""),
        "model_used": str(summary.get("model_used") or summary.get("model") or ""),
        "enabled": bool(summary.get("enabled")),
        "attempted": bool(summary.get("attempted")),
        "applied": bool(summary.get("applied")),
        "cache_hit": bool(summary.get("cache_hit")),
        "route": str(summary.get("route") or "unknown"),
        "route_tier": str(summary.get("route_tier") or "unknown"),
    }


class _PageOutputState(NamedTuple):
    """Snapshot of everything an AI page repair can write that reaches the
    scored output (``scripts/trial_bench/common.py``'s
    ``observation_from_result``, and from there score.py's keys, regions and
    review rate).

    Included, because each of these is only ever written when the AI
    actually supplied it:

    - ``block_types`` -- ``_apply_repair_payload``'s re-typing of blocks.
    - ``problem_partition`` -- the stem/choice/figure block-id partition of
      the regrouped ProblemUnits, i.e. the grouping itself.
    - ``block_titles`` -- ``block.metadata["display_title"]``, which the AI's
      ``display_titles`` writes and assemble_page.py's
      ``_problem_display_title`` turns into ``ProblemUnit.title``, which
      ``common.problem_key`` turns into the observation key (and which the
      passage-range detection reads).
    - ``problem_titles`` -- the per-problem title that results, in page order.
    - ``problem_boxes`` -- ``problem.metadata["bbox_px"]``, which
      build_problem_board_edb.py's ``_should_prefer_problem_metadata_bbox``
      makes *replace* the locally derived crop box on every AI-grouped
      problem -- i.e. every region the oracle observation scores.
    - ``problem_annotations`` -- ``problem.metadata["review_flags"]`` (score.py's
      review rate) and whether an ``ai_problem_unit`` was attached at all.

    Excluded: ``grouping_source``, ``grouping_reason`` and
    ``ai_grouping_role``. ``_apply_repair_payload`` and
    ``_annotate_problem_metadata`` stamp those onto every block and problem
    they touch whether or not the AI's answer differed from the local
    baseline, so diffing them would make every applied page read as
    "changed".
    """

    block_types: dict[str, BlockType]
    problem_partition: frozenset[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]
    block_titles: dict[str, str | None]
    problem_titles: tuple[str | None, ...]
    problem_boxes: tuple[str, ...]
    problem_annotations: tuple[str, ...]


def _metadata_fingerprint(value: Any) -> str:
    """Hashable, key-order-insensitive rendering of a metadata value, so two
    snapshots compare equal exactly when the value is the same."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        return repr(value)


def _classification_state(page: PageModel) -> _PageOutputState:
    block_types = {block.block_id: block.block_type for block in page.blocks}
    block_titles: dict[str, str | None] = {}
    for block in page.blocks:
        raw_title = block.metadata.get("display_title")
        block_titles[block.block_id] = raw_title.strip() if isinstance(raw_title, str) else None
    problem_partition = frozenset(
        (
            tuple(sorted(problem.stem_block_ids)),
            tuple(sorted(problem.choice_block_ids)),
            tuple(sorted(problem.figure_block_ids)),
        )
        for problem in page.problems
    )
    problem_titles = tuple(problem.title for problem in page.problems)
    problem_boxes = tuple(
        _metadata_fingerprint(problem.metadata.get("bbox_px")) for problem in page.problems
    )
    problem_annotations = tuple(
        _metadata_fingerprint(
            {
                "review_flags": problem.metadata.get("review_flags"),
                "ai_problem_unit": bool(problem.metadata.get("ai_problem_unit")),
            }
        )
        for problem in page.problems
    )
    return _PageOutputState(
        block_types=block_types,
        problem_partition=problem_partition,
        block_titles=block_titles,
        problem_titles=problem_titles,
        problem_boxes=problem_boxes,
        problem_annotations=problem_annotations,
    )


def _count_changed_blocks(
    baseline_by_block_id: Mapping[str, Any],
    repaired_by_block_id: Mapping[str, Any],
) -> int:
    all_ids = set(baseline_by_block_id) | set(repaired_by_block_id)
    return sum(
        1
        for block_id in all_ids
        if baseline_by_block_id.get(block_id) != repaired_by_block_id.get(block_id)
    )


_MISSING_SLOT = object()


def _count_changed_slots(baseline: Sequence[Any], repaired: Sequence[Any]) -> int:
    """Per-problem diff, index-aligned in page (reading) order. A repair that
    changes the number of problems counts every surplus/missing slot as
    changed -- that regrouping is also reported on its own through
    ``problems_regrouped``."""
    return sum(
        1
        for before, after in zip_longest(baseline, repaired, fillvalue=_MISSING_SLOT)
        if before != after
    )


def _repair_change_counters(
    baseline: _PageOutputState,
    repaired: _PageOutputState,
) -> dict[str, Any]:
    """The change signal behind the bench oracle's "NO AI EVIDENCE" verdict
    (scripts/trial_bench/oracle.py) and score.py's zero-evidence footnote.

    Kept as separate counters so "the AI regrouped the page" stays
    distinguishable from "the AI only supplied titles, crop boxes or review
    flags"; ``changed`` is their union, because any one of them is the AI's
    answer reaching the scored output.
    """
    blocks_changed = _count_changed_blocks(baseline.block_types, repaired.block_types)
    problems_regrouped = repaired.problem_partition != baseline.problem_partition
    titles_changed = _count_changed_blocks(
        baseline.block_titles, repaired.block_titles
    ) + _count_changed_slots(baseline.problem_titles, repaired.problem_titles)
    boxes_overridden = _count_changed_slots(baseline.problem_boxes, repaired.problem_boxes)
    problem_metadata_changed = _count_changed_slots(
        baseline.problem_annotations, repaired.problem_annotations
    )
    return {
        "blocks_changed": blocks_changed,
        "problems_regrouped": problems_regrouped,
        "titles_changed": titles_changed,
        "boxes_overridden": boxes_overridden,
        "problem_metadata_changed": problem_metadata_changed,
        "changed": bool(
            blocks_changed
            or problems_regrouped
            or titles_changed
            or boxes_overridden
            or problem_metadata_changed
        ),
    }


def _attach_ai_fallback_summary(page: PageModel, summary: dict[str, Any]) -> PageModel:
    summary.setdefault("stage", "page_repair")
    summary.setdefault("stage_label", "3단계 문항 경계 보정")
    # Every summary that never reaches an "applied" branch (disabled,
    # not_needed, missing_api_key, error, invalid_response, ...) never
    # changed the page's output either, so default every counter here; the
    # two "applied" branches in repair_page_model overwrite them with the
    # real diff against the pre-repair baseline.
    summary.setdefault("blocks_changed", 0)
    summary.setdefault("problems_regrouped", False)
    summary.setdefault("titles_changed", 0)
    summary.setdefault("boxes_overridden", 0)
    summary.setdefault("problem_metadata_changed", 0)
    summary.setdefault("changed", False)
    page.metadata["ai_fallback"] = summary
    raw_stages = page.metadata.get("ai_stages")
    stages = dict(raw_stages) if isinstance(raw_stages, dict) else {}
    stages["page_repair"] = _page_repair_stage_metadata(summary)
    page.metadata["ai_stages"] = stages
    return page


def repair_page_model(
    prepared_page: PreparedPage,
    page: PageModel,
    *,
    ocr_mode: str,
    config: AIFallbackConfig | None = None,
    cache: PipelineCache | None = None,
    request_semaphore: threading.BoundedSemaphore | None = None,
) -> PageModel:
    resolved_config = config or AIFallbackConfig()
    pipeline_cache = cache or PipelineCache.for_source(prepared_page.source_path)
    baseline = group_problem_units(page)
    # Snapshot the pre-repair output state now, before anything mutates
    # `baseline` in place: `_apply_repair_payload` below writes onto
    # `baseline.blocks` directly and returns the same object, so capturing
    # this after that call would compare the repaired page against itself.
    baseline_state = _classification_state(baseline)
    route_decision = decide_page_route(
        baseline,
        ocr_mode=ocr_mode,
        ai_enabled=resolved_config.enabled,
        ai_mode=resolved_config.normalized_mode,
    )
    baseline.metadata["difficulty_profile"] = route_decision.profile.to_metadata() if route_decision.profile else {}
    baseline.metadata["route_decision"] = route_decision.to_metadata()
    summary: dict[str, Any] = {
        "enabled": resolved_config.enabled,
        "mode": resolved_config.normalized_mode,
        "provider": resolved_config.provider,
        "model": resolved_config.resolved_model,
        "max_tokens": resolved_config.max_tokens,
        "ocr_mode": ocr_mode,
        "attempted": False,
        "applied": False,
        "cache_hit": False,
        "status": "disabled" if not resolved_config.enabled else "skipped",
        "route": route_decision.route,
        "route_tier": route_decision.profile.tier if route_decision.profile else "unknown",
        "trigger_reasons": list(route_decision.trigger_reasons),
        "baseline_problem_count": len(baseline.problems),
        "baseline_block_count": len(baseline.blocks),
    }
    if not resolved_config.enabled:
        return _attach_ai_fallback_summary(baseline, summary)

    trigger_reasons = list(route_decision.trigger_reasons)
    if not route_decision.should_use_ai:
        summary["status"] = "local_retry_recommended" if route_decision.next_best_action == "local_retry" else "not_needed"
        if route_decision.next_best_action:
            summary["next_best_action"] = route_decision.next_best_action
        return _attach_ai_fallback_summary(baseline, summary)

    # `force` mode is an explicit user opt-in to always attempt AI repair —
    # don't suppress it on busy pages.
    if (
        resolved_config.normalized_mode != "force"
        and resolved_config.max_regions > 0
        and len(baseline.blocks) > resolved_config.max_regions
    ):
        summary["status"] = "too_many_blocks"
        summary["skip_reason"] = "max_regions_exceeded"
        return _attach_ai_fallback_summary(baseline, summary)

    provider_key = resolved_config.normalized_provider
    summary["provider"] = provider_key
    if resolved_config.provider.strip().lower() not in _SUPPORTED_PROVIDER_ALIASES:
        summary["status"] = "provider_pending"
        summary["skip_reason"] = "provider_not_implemented"
        return _attach_ai_fallback_summary(baseline, summary)

    cached_repair: tuple[dict[str, Any], str | None] | None = None
    cached_model: str | None = None
    for candidate_model in _repair_model_candidates(resolved_config):
        cached_repair = pipeline_cache.load_ai_repair(
            page=baseline,
            provider=provider_key,
            model=candidate_model,
            trigger_reasons=trigger_reasons,
        )
        if cached_repair is not None:
            cached_model = candidate_model
            break
    if cached_repair is not None:
        repair_payload, response_id = cached_repair
        validation_error = _validate_repair_payload(repair_payload, baseline.blocks)
        if validation_error is None:
            problem_unit_metadata, problem_unit_warnings = _extract_problem_unit_metadata(
                repair_payload,
                baseline.blocks,
            )
            repaired = _apply_repair_payload(
                baseline,
                repair_payload,
                trigger_reasons=trigger_reasons,
            )
            repaired = group_problem_units(replace(repaired, problems=[]))
            repaired.metadata["difficulty_profile"] = baseline.metadata.get("difficulty_profile", {})
            repaired.metadata["route_decision"] = baseline.metadata.get("route_decision", {})
            # Snapshot *after* the AI's problem-unit metadata is written, not
            # straight after grouping: bbox_px and review_flags land here and
            # reach the crop boxes and review rate the oracle scores.
            _annotate_problem_metadata(repaired, trigger_reasons, problem_unit_metadata)
            summary.update(
                {
                    "applied": True,
                    "cache_hit": True,
                    "status": "cache_hit",
                    "response_id": response_id,
                    "model_used": cached_model or resolved_config.resolved_model,
                    "repaired_problem_count": len(repaired.problems),
                    "ai_notes": list(repair_payload.get("notes") or []),
                    "problem_units_accepted": len(problem_unit_metadata),
                    **_repair_change_counters(baseline_state, _classification_state(repaired)),
                }
            )
            if cached_model and cached_model != resolved_config.resolved_model:
                summary["model_fallback"] = {
                    "from": resolved_config.resolved_model,
                    "to": cached_model,
                    "reason": "fallback_model_cache_hit",
                }
            if problem_unit_warnings:
                summary["problem_units_warnings"] = problem_unit_warnings
            return _attach_ai_fallback_summary(repaired, summary)

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        summary["status"] = "missing_api_key"
        summary["skip_reason"] = "GEMINI_API_KEY not set"
        return _attach_ai_fallback_summary(baseline, summary)

    summary["attempted"] = True
    start_time = time.perf_counter()
    try:
        if request_semaphore is not None:
            request_semaphore.acquire()
        try:
            (
                repair_payload,
                response_id,
                used_model,
                model_attempts,
                token_usage,
            ) = _request_ai_repair_with_model_fallback(
                prepared_page=prepared_page,
                page=baseline,
                config=resolved_config,
                trigger_reasons=trigger_reasons,
                api_key=api_key,
            )
        finally:
            if request_semaphore is not None:
                request_semaphore.release()
        latency_ms = int(round((time.perf_counter() - start_time) * 1000.0))
    except Exception as exc:
        summary["status"] = "error"
        summary["error"] = str(exc)
        if resolved_config.fail_on_error:
            raise
        return _attach_ai_fallback_summary(baseline, summary)

    # The provider has already billed a completed response even when the
    # returned block IDs fail local validation. Record usage before validating
    # so cost reports include unsuccessful AI repairs as well.
    summary["token_usage"] = {
        **(token_usage if isinstance(token_usage, dict) else {}),
        "provider": resolved_config.provider,
        "model": used_model,
        "stage": "page_repair",
    }
    validation_error = _validate_repair_payload(repair_payload, baseline.blocks)
    if validation_error:
        summary["status"] = "invalid_response"
        summary["error"] = validation_error
        return _attach_ai_fallback_summary(baseline, summary)

    problem_unit_metadata, problem_unit_warnings = _extract_problem_unit_metadata(
        repair_payload,
        baseline.blocks,
    )
    repaired = _apply_repair_payload(
        baseline,
        repair_payload,
        trigger_reasons=trigger_reasons,
    )
    repaired = group_problem_units(replace(repaired, problems=[]))
    pipeline_cache.save_ai_repair(
        page=baseline,
        provider=provider_key,
        model=used_model,
        trigger_reasons=trigger_reasons,
        repair_payload=repair_payload,
        response_id=response_id,
    )

    repaired.metadata["difficulty_profile"] = baseline.metadata.get("difficulty_profile", {})
    repaired.metadata["route_decision"] = baseline.metadata.get("route_decision", {})
    # Snapshot *after* the AI's problem-unit metadata is written, not straight
    # after grouping: bbox_px and review_flags land here and reach the crop
    # boxes and review rate the oracle scores.
    _annotate_problem_metadata(repaired, trigger_reasons, problem_unit_metadata)
    summary.update(
        {
            "applied": True,
            "status": "applied",
            "latency_ms": latency_ms,
            "response_id": response_id,
            "model_used": used_model,
            "model_attempts": model_attempts,
            "repaired_problem_count": len(repaired.problems),
            "ai_notes": list(repair_payload.get("notes") or []),
            "problem_units_accepted": len(problem_unit_metadata),
            **_repair_change_counters(baseline_state, _classification_state(repaired)),
        }
    )
    if used_model != resolved_config.resolved_model:
        first_error = next(
            (
                attempt.get("error")
                for attempt in model_attempts
                if attempt.get("model") == resolved_config.resolved_model
            ),
            "",
        )
        summary["model_fallback"] = {
            "from": resolved_config.resolved_model,
            "to": used_model,
            "reason": str(first_error or "primary_model_error"),
        }
    if problem_unit_warnings:
        summary["problem_units_warnings"] = problem_unit_warnings
    _maybe_write_debug_artifacts(
        prepared_page=prepared_page,
        page=repaired,
        repair_payload=repair_payload,
        summary=summary,
        config=replace(resolved_config, model=used_model),
    )
    return _attach_ai_fallback_summary(repaired, summary)


def _select_repair_reasons(page: PageModel, config: AIFallbackConfig, *, ocr_mode: str) -> list[str]:
    reasons: list[str] = []
    if config.normalized_mode == "force":
        reasons.append("forced")

    if len(page.blocks) <= 1:
        return reasons

    if ocr_mode.strip().lower() in {"none", "noop"}:
        reasons.append("ocr_disabled")

    if not any(detect_problem_start(block) for block in page.blocks):
        reasons.append("no_problem_markers")

    if any(problem.metadata.get("fallback_grouping") for problem in page.problems):
        reasons.append("fallback_grouping")

    if len(page.problems) == len(page.blocks):
        reasons.append("problem_per_block")

    if _low_confidence_ratio(page) >= 0.5:
        reasons.append("low_confidence")

    if any(_block_has_overlap_marker(block) for block in page.blocks):
        reasons.append("choice_problem_marker_overlap")

    if _looks_like_full_page_image(page):
        reasons.append("full_page_image")

    return list(dict.fromkeys(reasons))


def _low_confidence_ratio(page: PageModel) -> float:
    eligible = [
        block
        for block in page.blocks
        if block.block_type not in {BlockType.IMAGE, BlockType.DIAGRAM, BlockType.TABLE}
    ]
    if not eligible:
        return 0.0
    low_confidence = 0
    for block in eligible:
        if not (block.text and block.text.strip()):
            low_confidence += 1
            continue
        if block.confidence is None or block.confidence < 0.55:
            low_confidence += 1
    return low_confidence / len(eligible)


def _block_has_overlap_marker(block: ContentBlock) -> bool:
    if not block.text:
        return False
    stripped = block.text.strip()
    return stripped.startswith(tuple(f"{index})" for index in range(1, 10)))


def _looks_like_full_page_image(page: PageModel) -> bool:
    if len(page.blocks) != 1:
        return False
    block = page.blocks[0]
    if block.block_type not in {BlockType.IMAGE, BlockType.DIAGRAM, BlockType.TABLE}:
        return False
    return block.bbox.area >= float(page.width_px * page.height_px) * 0.75


def _repair_model_candidates(config: AIFallbackConfig) -> list[str]:
    primary = config.resolved_model
    candidates = [primary]
    if (
        primary == DEFAULT_GEMINI_REPAIR_MODEL
        or primary in DEPRECATED_GEMINI_REPAIR_MODELS
        or primary.startswith("gemini-3")
        or "preview" in primary
    ):
        candidates.append(FALLBACK_GEMINI_REPAIR_MODEL)
    return list(dict.fromkeys(model for model in candidates if model))


def _request_ai_repair_with_model_fallback(
    *,
    prepared_page: PreparedPage,
    page: PageModel,
    config: AIFallbackConfig,
    trigger_reasons: list[str],
    api_key: str,
) -> tuple[dict[str, Any], str | None, str, list[dict[str, str]], dict[str, int]]:
    """Call Gemini, falling back from preview/3.x models to stable Pro if needed."""
    attempts: list[dict[str, str]] = []
    last_exc: Exception | None = None
    for model in _repair_model_candidates(config):
        model_config = replace(config, model=model)
        try:
            repair_payload, response_id, token_usage = _request_ai_repair_with_retry(
                prepared_page=prepared_page,
                page=page,
                config=model_config,
                trigger_reasons=trigger_reasons,
                api_key=api_key,
            )
            attempts.append({"model": model, "status": "ok"})
            return repair_payload, response_id, model, attempts, token_usage
        except Exception as exc:
            last_exc = exc
            attempts.append({"model": model, "status": "error", "error": str(exc)})
            if _is_fatal_ai_repair_error(exc):
                break
    wrapped = RuntimeError(f"AI repair failed after model fallback: {last_exc}")
    _copy_gemini_diagnostics(source=last_exc, target=wrapped)
    raise wrapped from last_exc


def _request_ai_repair_with_retry(
    *,
    prepared_page: PreparedPage,
    page: PageModel,
    config: AIFallbackConfig,
    trigger_reasons: list[str],
    api_key: str,
) -> tuple[dict[str, Any], str | None, dict[str, int]]:
    """Call Gemini for the repair. Retry once on transient failure."""
    last_exc: Exception | None = None
    for attempt in range(2):
        if attempt > 0:
            time.sleep(2.0)
        try:
            return _request_gemini_repair(
                prepared_page=prepared_page,
                page=page,
                config=config,
                trigger_reasons=trigger_reasons,
                api_key=api_key,
            )
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_ai_repair_error(exc):
                raise
    wrapped = RuntimeError(f"AI repair failed after retries: {last_exc}")
    _copy_gemini_diagnostics(source=last_exc, target=wrapped)
    raise wrapped from last_exc


def _copy_gemini_diagnostics(*, source: Exception | None, target: Exception) -> None:
    """Carry a GeminiRepairResponseError's diagnostics onto a wrapping
    RuntimeError, so oracle.py's failure record still sees the finishReason,
    token budget and response sizes after `_request_ai_repair_with_retry`/
    `_request_ai_repair_with_model_fallback` rewrap the underlying error with
    a summary message. A bare ``raise`` (the non-retryable/non-fatal exit
    paths) re-raises the original object and never needs this.
    """
    diagnostics = getattr(source, "diagnostics", None)
    if diagnostics is not None:
        target.diagnostics = diagnostics
        target.truncated = bool(getattr(source, "truncated", False))


def _is_retryable_ai_repair_error(exc: Exception) -> bool:
    if isinstance(exc, GeminiRepairResponseError) and exc.truncated:
        # temperature=0.0 makes the same request reproduce the same
        # truncation -- retrying it burns a call and a 2s sleep for a
        # deterministic loss. A different model/config might still help, so
        # this is not fatal to the caller's model-fallback loop, just to the
        # same-model retry.
        return False
    if _is_fatal_ai_repair_error(exc):
        return False
    message = str(exc)
    match = re.search(r"HTTP\s+(\d{3})", message)
    if not match:
        return True
    status_code = int(match.group(1))
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _is_fatal_ai_repair_error(exc: Exception) -> bool:
    message = str(exc).lower()
    match = re.search(r"http\s+(\d{3})", message)
    status_code = int(match.group(1)) if match else None
    if status_code in {400, 401, 403}:
        return True
    if status_code == 429 and any(
        marker in message
        for marker in (
            "api_key_invalid",
            "api key not found",
            "billing",
            "credits are depleted",
            "prepayment",
            "quota",
            "resource_exhausted",
        )
    ):
        return True
    return False


def _repair_thinking_level(model: str) -> str:
    normalized = str(model or "").strip().lower()
    if normalized.startswith("gemini-3"):
        # Page grouping needs light spatial reasoning; `low` avoids the default
        # medium/high token spend without reducing it to extraction-only mode.
        return "low"
    return ""


def _should_request_problem_units(trigger_reasons: list[str]) -> bool:
    normalized = {str(reason or "").strip() for reason in trigger_reasons}
    return bool(normalized.intersection(_PROBLEM_UNIT_TRIGGER_REASONS))


def _repair_output_token_budget(
    page: PageModel,
    *,
    configured_max_tokens: int,
    include_problem_units: bool,
    max_output_token_cap: int | None = None,
) -> int:
    """Bound structured repair output without truncating normal block arrays.

    ``max_output_token_cap`` (AIFallbackConfig.max_output_token_cap, unset by
    every desktop caller) bypasses the per-block estimate and hard cap below
    entirely, returning ``min(configured_max_tokens, max_output_token_cap)``
    instead. Only scripts/trial_bench/oracle.py's force_config sets it, so
    every existing caller computes exactly the value it always has.

    That bypass exists because the estimate itself, not just the 2048/3072
    hard cap, was the actual truncation cause diagnosed on
    english_2020suneung_go3_20191107 (oracle_failures/): with block_count=11
    and include_problem_units=False, "512 + 24*block_count" computed 776,
    under the 2048 hard cap and nowhere near force_config's configured 4096.
    "24 tokens per block" was calibrated against short synthetic block ids
    ("block-1"); a real per-page id
    ("<case>-page-001-block-011", 50+ characters) appears twice per block --
    once in problem_start_block_ids, once as a display_titles entry -- so
    the true per-block cost scales with the source filename length, which
    this estimate never accounts for. The response was captured
    mid-``display_titles`` array (its ``block_id`` string cut off
    mid-write), and Gemini's own finishReason was MAX_TOKENS -- see
    page_repair.GeminiRepairTruncatedError and the diagnostics it carries.
    """
    if max_output_token_cap is not None:
        return max(512, min(int(configured_max_tokens), int(max_output_token_cap)))
    block_count = max(1, len(page.blocks))
    estimated = 512 + 24 * block_count
    if include_problem_units:
        estimated += 32 * min(block_count, 24)
    hard_cap = 3072 if include_problem_units else 2048
    return max(512, min(int(configured_max_tokens), hard_cap, estimated))


def _request_gemini_repair(
    *,
    prepared_page: PreparedPage,
    page: PageModel,
    config: AIFallbackConfig,
    trigger_reasons: list[str],
    api_key: str,
) -> tuple[dict[str, Any], str | None, dict[str, int]]:
    """Call the Gemini generateContent API with a JSON response schema."""
    include_problem_units = _should_request_problem_units(trigger_reasons)
    output_token_limit = _repair_output_token_budget(
        page,
        configured_max_tokens=config.max_tokens,
        include_problem_units=include_problem_units,
        max_output_token_cap=config.max_output_token_cap,
    )
    prompt = _build_repair_prompt(
        page,
        trigger_reasons,
        include_problem_units=include_problem_units,
    )
    thinking_level = _repair_thinking_level(config.resolved_model)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": _image_to_base64(prepared_page.image),
                        }
                    },
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _repair_schema(
                include_problem_units=include_problem_units,
            ),
            "maxOutputTokens": output_token_limit,
            "temperature": 0.0,
        },
    }
    if thinking_level:
        payload["generationConfig"]["thinkingConfig"] = {
            "thinkingLevel": thinking_level,
        }
    if config.resolved_model.startswith("gemini-3") and "flash" in config.resolved_model:
        payload["generationConfig"].pop("temperature", None)
    url = f"{GEMINI_API_BASE}/{config.resolved_model}:generateContent?key={api_key}"
    raw_response = _post_json(
        url,
        payload,
        headers={"Content-Type": "application/json"},
        timeout_ms=config.timeout_ms,
    )
    candidates = raw_response.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini response did not include any candidates")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    json_text = "".join(
        part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")
    )
    finish_reason = str(candidates[0].get("finishReason") or "unknown")
    if not json_text:
        diagnostics = _gemini_response_diagnostics(
            model=config.resolved_model,
            finish_reason=finish_reason,
            effective_max_output_tokens=output_token_limit,
            configured_max_output_tokens=config.max_tokens,
            prompt=prompt,
            response_text=json_text,
            block_count=len(page.blocks),
            include_problem_units=include_problem_units,
        )
        message = f"Gemini response contained no text (finish={finish_reason})"
        if finish_reason in _GEMINI_TRUNCATION_FINISH_REASONS:
            raise GeminiRepairTruncatedError(message, diagnostics=diagnostics)
        raise GeminiRepairResponseError(message, diagnostics=diagnostics)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        diagnostics = _gemini_response_diagnostics(
            model=config.resolved_model,
            finish_reason=finish_reason,
            effective_max_output_tokens=output_token_limit,
            configured_max_output_tokens=config.max_tokens,
            prompt=prompt,
            response_text=json_text,
            block_count=len(page.blocks),
            include_problem_units=include_problem_units,
        )
        if finish_reason in _GEMINI_TRUNCATION_FINISH_REASONS:
            raise GeminiRepairTruncatedError(
                f"Gemini response truncated at {output_token_limit} output tokens "
                f"(finish_reason={finish_reason}, model={config.resolved_model}): {exc}",
                diagnostics=diagnostics,
            ) from exc
        raise GeminiRepairResponseError(
            f"Gemini response JSON decode failed (finish_reason={finish_reason}): {exc}",
            diagnostics=diagnostics,
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini response was not a JSON object")
    token_usage = normalize_gemini_token_usage(raw_response)
    token_usage.update(
        {
            "configured_max_output_tokens": int(config.max_tokens),
            "effective_max_output_tokens": output_token_limit,
            "prompt_char_count": len(prompt),
            "input_block_count": len(page.blocks),
            "problem_units_requested": int(include_problem_units),
            "image_max_dimension": AI_REPAIR_IMAGE_MAX_DIMENSION,
        }
    )
    return parsed, raw_response.get("responseId") or raw_response.get("id"), token_usage


def _repair_schema(*, include_problem_units: bool = True) -> dict[str, Any]:
    # Schema follows Gemini's OpenAPI 3.0 subset — no additionalProperties.
    schema = {
        "type": "object",
        "properties": {
            "problem_start_block_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "choice_block_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "figure_block_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "display_titles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "block_id": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["block_id", "title"],
                },
            },
            "problem_units": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "unit_id": {"type": "string"},
                        "problem_start_block_id": {"type": "string"},
                        "title": {"type": "string"},
                        "stem_block_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "choice_block_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "explanation_block_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "figure_block_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "bbox_px": {
                            "type": "object",
                            "properties": {
                                "left": {"type": "number"},
                                "top": {"type": "number"},
                                "width": {"type": "number"},
                                "height": {"type": "number"},
                            },
                            "required": ["left", "top", "width", "height"],
                        },
                        "review_flags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["problem_start_block_id", "bbox_px", "review_flags"],
                },
            },
            "notes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "problem_start_block_ids",
            "choice_block_ids",
            "figure_block_ids",
            "display_titles",
            "notes",
        ],
    }
    # Notes are diagnostic prose, not required to repair page structure.
    # Omitting them saves schema and response tokens on every request.
    schema["properties"].pop("notes", None)
    schema["required"] = [
        key for key in schema["required"] if key != "notes"
    ]
    if not include_problem_units:
        schema["properties"].pop("problem_units", None)
    return schema


def _build_repair_prompt(
    page: PageModel,
    trigger_reasons: list[str],
    *,
    include_problem_units: bool | None = None,
) -> str:
    include_units = (
        _should_request_problem_units(trigger_reasons)
        if include_problem_units is None
        else bool(include_problem_units)
    )
    block_lines: list[str] = []
    meta_key_map = {
        "column_index": "col",
        "question_band_index": "band",
        "fallback_reason": "fallback",
        "split_from_band": "split",
    }
    for index, block in enumerate(page.blocks, start=1):
        text = (block.text or "").strip()
        if not text:
            # OCR lines are a fallback only. Sending them alongside `text`
            # duplicates the largest text field on every normal block.
            text = "\n".join(
                line.text.strip()
                for line in (block.ocr_lines or [])[:3]
                if line.text and line.text.strip()
            )
        entry: dict[str, Any] = {
            "i": index,
            "id": block.block_id,
            "k": block.block_type.value,
            "t": text[:240],
            "b": [
                round(block.bbox.left, 1),
                round(block.bbox.top, 1),
                round(block.bbox.width, 1),
                round(block.bbox.height, 1),
            ],
        }
        if block.confidence is not None:
            entry["c"] = round(block.confidence, 2)
        compact_meta = {
            short_key: block.metadata[source_key]
            for source_key, short_key in meta_key_map.items()
            if source_key in block.metadata
        }
        if compact_meta:
            entry["m"] = compact_meta
        block_lines.append(
            json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        )

    unit_rule = (
        "Return problem_units only for confident complex grouping. Each unit uses a listed "
        "problem_start_block_id, page-pixel bbox_px covering title through choices, and short "
        "review_flags. Stop before the next problem in the same column."
        if include_units
        else "Do not return problem_units or notes."
    )
    return "\n".join(
        [
            f"Korean exam block repair. page={page.width_px}x{page.height_px}; "
            f"subject={page.subject.value}; triggers={','.join(trigger_reasons)}.",
            "Return schema JSON only. Use listed ids exactly; never invent an id.",
            "problem_start_block_ids = every visible NUMBERED question start in reading order. "
            "For 5+ questions include all, not only the first 2–3.",
            "choice_block_ids = final answer options ①–⑤ or A–E, not a ㄱ/ㄴ/ㄷ stem list. "
            "figure_block_ids = image/diagram/graph/table blocks.",
            "<보기> and [조건] are not question starts. Keep each choice with its own question. "
            "On two-column pages group within a column. Reclassify only clear errors.",
            unit_rule,
            "Block keys: i=order,id=block id,k=current type,t=OCR text,"
            "c=confidence,b=[left,top,width,height],m=layout hints.",
            *block_lines,
        ]
    )


def _image_to_base64(image: Image.Image) -> str:
    """Return a compact layout-reference JPEG (no data-URL prefix).

    OCR text and exact source-space boxes are already in the prompt, so the
    model only needs a page-level visual reference. Capping the long edge
    avoids paying multimodal tokens for print-resolution pixels.
    """
    from io import BytesIO

    prepared = image.convert("RGB")
    if max(prepared.size) > AI_REPAIR_IMAGE_MAX_DIMENSION:
        prepared.thumbnail(
            (AI_REPAIR_IMAGE_MAX_DIMENSION, AI_REPAIR_IMAGE_MAX_DIMENSION),
            Image.Resampling.LANCZOS,
        )
    buffer = BytesIO()
    prepared.save(
        buffer,
        format="JPEG",
        quality=AI_REPAIR_IMAGE_JPEG_QUALITY,
        optimize=True,
    )
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str],
    timeout_ms: int,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method="POST")
    timeout_seconds = max(1.0, timeout_ms / 1000.0)
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini request failed with HTTP {exc.code}: {response_body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Gemini request failed: {exc.reason}") from exc


def _validate_repair_payload(payload: dict[str, Any], blocks: list[ContentBlock]) -> str | None:
    known_ids = {block.block_id for block in blocks}
    start_ids = list(payload.get("problem_start_block_ids") or [])
    choice_ids = list(payload.get("choice_block_ids") or [])
    figure_ids = list(payload.get("figure_block_ids") or [])

    if not start_ids:
        return "problem_start_block_ids must include at least one block"

    invalid_ids = {
        block_id
        for block_id in [*start_ids, *choice_ids, *figure_ids]
        if block_id not in known_ids
    }
    if invalid_ids:
        return f"unknown block ids returned: {sorted(invalid_ids)}"

    if set(start_ids) & set(choice_ids):
        return "problem start and choice block ids overlap"

    if len(set(start_ids)) != len(start_ids):
        return "problem_start_block_ids must be unique"

    ordered_ids = [block.block_id for block in blocks]
    start_positions = [ordered_ids.index(block_id) for block_id in start_ids]
    if start_positions != sorted(start_positions):
        return "problem_start_block_ids must be in reading order"

    return None


def _extract_problem_unit_metadata(
    payload: dict[str, Any],
    blocks: list[ContentBlock],
) -> tuple[list[dict[str, Any]], list[str]]:
    raw_units = payload.get("problem_units")
    if raw_units is None:
        return [], []
    if not isinstance(raw_units, list):
        return [], ["problem_units ignored: expected array"]

    known_ids = {block.block_id for block in blocks}
    problem_start_ids = set(payload.get("problem_start_block_ids") or [])
    units: list[dict[str, Any]] = []
    warnings: list[str] = []

    for index, raw_unit in enumerate(raw_units, start=1):
        if not isinstance(raw_unit, dict):
            warnings.append(f"problem_units[{index}] ignored: expected object")
            continue

        unit: dict[str, Any] = {"source_index": index}
        start_id = _clean_optional_string(raw_unit.get("problem_start_block_id"))
        if start_id:
            if start_id not in known_ids:
                warnings.append(f"problem_units[{index}] ignored unknown problem_start_block_id")
                continue
            if problem_start_ids and start_id not in problem_start_ids:
                warnings.append(f"problem_units[{index}] problem_start_block_id not in start ids")
            unit["problem_start_block_id"] = start_id

        unit_id = _clean_optional_string(raw_unit.get("unit_id"))
        if unit_id:
            unit["unit_id"] = unit_id

        title = _clean_optional_string(raw_unit.get("title"))
        if title:
            unit["title"] = title

        referenced_block_ids: list[str] = []
        for key in (
            "stem_block_ids",
            "choice_block_ids",
            "explanation_block_ids",
            "figure_block_ids",
        ):
            block_ids, block_warnings = _clean_problem_unit_block_ids(raw_unit.get(key), known_ids)
            if block_warnings:
                warnings.extend(f"problem_units[{index}] {warning}" for warning in block_warnings)
            if block_ids:
                unit[key] = block_ids
                referenced_block_ids.extend(block_ids)

        bbox_px = _clean_bbox_px(raw_unit.get("bbox_px"))
        if bbox_px is not None:
            unit["bbox_px"] = bbox_px
        elif "bbox_px" in raw_unit:
            warnings.append(f"problem_units[{index}] bbox_px ignored: expected finite positive pixel box")

        review_flags, flag_warnings = _clean_review_flags(raw_unit.get("review_flags"))
        if flag_warnings:
            warnings.extend(f"problem_units[{index}] {warning}" for warning in flag_warnings)
        if "review_flags" in raw_unit or review_flags:
            unit["review_flags"] = review_flags

        unit["referenced_block_ids"] = list(dict.fromkeys(referenced_block_ids))
        if any(key in unit for key in ("bbox_px", "review_flags", "unit_id", "title")):
            units.append(unit)

    return units, warnings[:10]


def _clean_optional_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_problem_unit_block_ids(value: Any, known_ids: set[str]) -> tuple[list[str], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["block ids ignored: expected array"]

    block_ids: list[str] = []
    warnings: list[str] = []
    for raw_id in value:
        if not isinstance(raw_id, str):
            warnings.append("non-string block id ignored")
            continue
        block_id = raw_id.strip()
        if block_id not in known_ids:
            warnings.append(f"unknown block id ignored: {block_id}")
            continue
        block_ids.append(block_id)
    return list(dict.fromkeys(block_ids)), warnings


def _clean_bbox_px(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None

    cleaned: dict[str, float] = {}
    for key in ("left", "top", "width", "height"):
        raw_value = value.get(key)
        if isinstance(raw_value, bool):
            return None
        try:
            number = float(raw_value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        if key in {"width", "height"}:
            if number <= 0:
                return None
        elif number < 0:
            return None
        cleaned[key] = round(number, 2)
    return cleaned


def _clean_review_flags(value: Any) -> tuple[list[str], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["review_flags ignored: expected array"]

    flags: list[str] = []
    warnings: list[str] = []
    for raw_flag in value:
        if not isinstance(raw_flag, str):
            warnings.append("non-string review flag ignored")
            continue
        flag = raw_flag.strip()
        if flag:
            flags.append(flag[:80])
    return list(dict.fromkeys(flags)), warnings


def _apply_repair_payload(
    page: PageModel,
    payload: dict[str, Any],
    *,
    trigger_reasons: list[str],
) -> PageModel:
    start_ids = set(payload.get("problem_start_block_ids") or [])
    choice_ids = set(payload.get("choice_block_ids") or [])
    figure_ids = set(payload.get("figure_block_ids") or [])
    display_titles = {
        str(item["block_id"]): str(item["title"]).strip()
        for item in payload.get("display_titles") or []
        if isinstance(item, dict) and item.get("block_id") and str(item.get("title") or "").strip()
    }

    for block in page.blocks:
        block.metadata.pop("force_problem_start", None)
        block.metadata.pop("ai_grouping_role", None)
        block.metadata["grouping_source"] = "ai_fallback"
        block.metadata["grouping_reason"] = list(trigger_reasons)

        if block.block_id in display_titles:
            block.metadata["display_title"] = display_titles[block.block_id]

        if block.block_id in start_ids:
            block.metadata["force_problem_start"] = True
            block.metadata["ai_grouping_role"] = "problem_start"
            if block.block_type not in {BlockType.IMAGE, BlockType.DIAGRAM, BlockType.TABLE}:
                block.block_type = BlockType.TITLE
            continue

        if block.block_id in choice_ids:
            block.metadata["ai_grouping_role"] = "choice"
            block.block_type = BlockType.CHOICE
            continue

        if block.block_id in figure_ids:
            block.metadata["ai_grouping_role"] = "figure"
            if block.block_type not in {BlockType.DIAGRAM, BlockType.TABLE}:
                block.block_type = BlockType.IMAGE
            continue

        if block.block_type in {BlockType.TITLE, BlockType.SECTION} and not (block.text and block.text.strip()):
            block.block_type = BlockType.STEM

    return page


def _annotate_problem_metadata(
    page: PageModel,
    trigger_reasons: list[str],
    problem_unit_metadata: list[dict[str, Any]] | None = None,
) -> None:
    used_problem_units: set[int] = set()
    ai_units = problem_unit_metadata or []
    for index, problem in enumerate(page.problems):
        problem.metadata["grouping_source"] = "ai_fallback"
        problem.metadata["grouping_reason"] = list(trigger_reasons)
        unit_metadata = _match_problem_unit_metadata(problem, ai_units, used_problem_units, index)
        if unit_metadata is None:
            continue

        public_metadata = {
            key: value
            for key, value in unit_metadata.items()
            if key not in {"source_index", "referenced_block_ids"}
        }
        problem.metadata["ai_problem_unit"] = public_metadata
        if "bbox_px" in public_metadata:
            problem.metadata["bbox_px"] = public_metadata["bbox_px"]
        if "review_flags" in public_metadata:
            problem.metadata["review_flags"] = public_metadata["review_flags"]


def _match_problem_unit_metadata(
    problem: ProblemUnit,
    units: list[dict[str, Any]],
    used_units: set[int],
    problem_index: int,
) -> dict[str, Any] | None:
    problem_block_ids = set(
        [
            *problem.stem_block_ids,
            *problem.choice_block_ids,
            *problem.explanation_block_ids,
            *problem.figure_block_ids,
        ]
    )

    for index, unit in enumerate(units):
        if index in used_units:
            continue
        start_id = unit.get("problem_start_block_id")
        if start_id and start_id in problem_block_ids:
            used_units.add(index)
            return unit

    for index, unit in enumerate(units):
        if index in used_units:
            continue
        referenced_ids = set(unit.get("referenced_block_ids") or [])
        if referenced_ids and referenced_ids & problem_block_ids:
            used_units.add(index)
            return unit

    if problem_index < len(units) and problem_index not in used_units:
        used_units.add(problem_index)
        return units[problem_index]
    return None


def _maybe_write_debug_artifacts(
    *,
    prepared_page: PreparedPage,
    page: PageModel,
    repair_payload: dict[str, Any],
    summary: dict[str, Any],
    config: AIFallbackConfig,
) -> None:
    if not config.save_debug:
        return
    source_path = Path(prepared_page.source_path) if prepared_page.source_path else None
    if source_path is None:
        return
    debug_dir = source_path.parent / ".pipeline_cache" / "ai_debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_path = debug_dir / f"{page.page_id}_repair.json"
    debug_path.write_text(
        json.dumps(
            {
                "summary": summary,
                "repair_payload": repair_payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    summary["debug_path"] = str(debug_path)
    page.metadata.setdefault("ai_fallback", {})
    page.metadata["ai_fallback"]["debug_path"] = str(debug_path)
