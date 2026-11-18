#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import io
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from build_problem_board_edb import (
    _annotate_cross_page_passage_groups,
    _collapse_marker_document_continuation_page,
    _collect_problem_risk_flags,
    _count_core_hwp_problems,
    _is_hwp_oversegmentation,
    _is_marker_document_continuation_page,
    _problem_passage_payload,
    _remove_duplicate_marker_document_problem_numbers,
    _remove_hwp_template_instruction_problems,
    _session_passage_groups,
    _session_problem_count_payload,
    PDF_TEXT_MARKER_HORIZONTAL_PADDING_PX,
)
from build_structured_page_json import build_page_model
from edb_builder import ImageRecordSpec, build_edb, build_image_record, write_edb
from layout_template_schema import build_default_template
from page_repair import AIFallbackConfig, DEFAULT_GEMINI_REPAIR_MODEL, build_ai_fallback_config as build_page_ai_fallback_config
from passage_quality import assess_passage_crop_quality
from placement_engine import build_export_plan
from preprocess import prepare_source_pages, prepare_source_pages_batch
from structured_schema import Box, PageModel, ProblemUnit, Subject, save_pages_json


def _resolve_subject(name: str | None) -> Subject:
    if not name:
        return Subject.UNKNOWN
    try:
        return Subject(name.lower())
    except ValueError:
        return Subject.UNKNOWN


def _encode_jpeg(image: Image.Image, quality: int = 92) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _make_thumbnail(image: Image.Image, max_size: tuple[int, int] = (640, 640)) -> Image.Image:
    thumb = image.copy()
    thumb.thumbnail(max_size, Image.Resampling.LANCZOS)
    return thumb


def _to_file_uri(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return Path(path).resolve().as_uri()


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _hwp_source_key(page_model: PageModel) -> str | None:
    metadata = page_model.metadata if isinstance(page_model.metadata, dict) else {}
    if metadata.get("source_type") != "hwp":
        return None
    for key in ("source_hwp_path", "original_source_path", "source_pdf_path", "converted_pdf_path"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return page_model.source_path


def _hwp_text_problem_signal_count(metadata: dict[str, Any]) -> int:
    quality = metadata.get("hwp_conversion_quality")
    if not isinstance(quality, dict):
        return 0
    numbered = _coerce_int(quality.get("hwp_text_numbered_problem_count"))
    stem = _coerce_int(quality.get("hwp_text_stem_problem_count"))
    return numbered if numbered > 0 else stem


def _metadata_list_count(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key)
    if isinstance(value, list):
        return len(value)
    return _coerce_int(value)


def _collect_hwp_problem_count_mismatches(page_models: list[PageModel]) -> tuple[dict[str, list[str]], list[str]]:
    by_source: dict[str, dict[str, Any]] = {}
    for page_model in page_models:
        source_key = _hwp_source_key(page_model)
        if not source_key:
            continue
        bucket = by_source.setdefault(
            source_key,
            {
                "page_ids": [],
                "detected": 0,
                "signal": 0,
                "duplicate_skips": 0,
                "label": Path(source_key).name,
            },
        )
        bucket["page_ids"].append(page_model.page_id)
        bucket["detected"] += _count_core_hwp_problems(page_model)
        bucket["signal"] = max(int(bucket["signal"]), _hwp_text_problem_signal_count(page_model.metadata))
        bucket["duplicate_skips"] += _metadata_list_count(page_model.metadata, "duplicate_problem_numbers_skipped")

    flags_by_page_id: dict[str, list[str]] = {}
    messages: list[str] = []
    for bucket in by_source.values():
        signal = int(bucket["signal"])
        if signal <= 0:
            continue
        detected = int(bucket["detected"])
        expected = max(0, signal - int(bucket["duplicate_skips"]))
        if expected <= 0 or detected == expected:
            continue
        is_oversegmentation = _is_hwp_oversegmentation(expected, detected)
        page_flags = ["hwp_problem_count_mismatch"]
        if is_oversegmentation:
            page_flags.append("hwp_oversegmentation")
        for page_id in bucket["page_ids"]:
            flags_by_page_id.setdefault(str(page_id), []).extend(page_flags)
        if is_oversegmentation:
            messages.append(
                f"HWP 내부 텍스트 기준 문항 수는 {expected}개인데 최종 감지 문항 수는 {detected}개입니다. "
                "과분할 가능성이 큽니다. 검수 화면에서 원본 페이지의 분리 상태를 먼저 확인해 주세요."
            )
        else:
            messages.append(
                f"HWP 내부 텍스트 기준 문항 수는 {expected}개인데 최종 감지 문항 수는 {detected}개입니다. 검수 화면에서 분리 상태를 확인해 주세요."
            )
    return flags_by_page_id, messages


def _build_ai_fallback_config(
    *,
    enabled: bool,
    mode: str | None,
    provider: str,
    model: str,
    prompt: str,
    max_tokens: int | None,
    temperature: float | None,
    threshold: float,
    max_regions: int,
    timeout_ms: int,
    save_debug: bool,
    fail_on_error: bool,
) -> dict[str, Any] | None:
    threshold = 0.72 if threshold is None else float(threshold)
    max_tokens = 4096 if max_tokens is None else int(max_tokens)
    max_regions = 48 if max_regions is None else int(max_regions)
    timeout_ms = 30000 if timeout_ms is None else int(timeout_ms)
    resolved_mode = (mode or "").strip().lower() or ("auto" if enabled else "off")
    if resolved_mode not in {"off", "auto", "force"}:
        resolved_mode = "auto" if enabled else "off"
    effective_enabled = resolved_mode != "off"
    if (
        not effective_enabled
        and provider in {"gemini", "google"}
        and not model
        and not prompt
        and max_tokens == 4096
        and temperature is None
        and threshold == 0.72
        and max_regions == 48
        and timeout_ms == 30000
        and not save_debug
        and not fail_on_error
    ):
        return None
    return {
        "enabled": effective_enabled,
        "mode": resolved_mode,
        "provider": provider or "gemini",
        "model": model or DEFAULT_GEMINI_REPAIR_MODEL,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "threshold": threshold,
        "max_regions": max_regions,
        "timeout_ms": timeout_ms,
        "save_debug": save_debug,
        "fail_on_error": fail_on_error,
    }


def _to_page_ai_config(ai_fallback_config: dict[str, Any] | None) -> AIFallbackConfig:
    if not ai_fallback_config:
        return build_page_ai_fallback_config()
    return build_page_ai_fallback_config(
        mode=str(ai_fallback_config.get("mode") or ("auto" if bool(ai_fallback_config.get("enabled")) else "off")),
        provider=str(ai_fallback_config.get("provider") or "gemini"),
        model=str(ai_fallback_config.get("model") or ""),
        threshold=float(ai_fallback_config.get("threshold") or 0.72),
        max_regions=int(ai_fallback_config.get("max_regions") or 48),
        max_tokens=int(ai_fallback_config.get("max_tokens") or 4096),
        timeout_ms=int(ai_fallback_config.get("timeout_ms") or 30000),
        save_debug=bool(ai_fallback_config.get("save_debug")),
        fail_on_error=bool(ai_fallback_config.get("fail_on_error")),
    )


def _summarize_ai_fallback_usage(page_models: list[PageModel], ai_fallback_config: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ai_fallback_config:
        return None
    attempted_page_count = 0
    applied_page_count = 0
    ai_cache_hit_count = 0
    ocr_cache_hit_count = 0
    ocr_cache_miss_count = 0
    status_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    route_tier_counts: dict[str, int] = {}
    model_fallbacks: list[dict[str, Any]] = []

    for page_model in page_models:
        ai_summary = page_model.metadata.get("ai_fallback")
        if not isinstance(ai_summary, dict):
            ai_summary = {}
        if ai_summary.get("attempted"):
            attempted_page_count += 1
        if ai_summary.get("applied"):
            applied_page_count += 1
        if ai_summary.get("cache_hit"):
            ai_cache_hit_count += 1
        status = str(ai_summary.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        model_fallback = ai_summary.get("model_fallback")
        if isinstance(model_fallback, dict):
            model_fallbacks.append(dict(model_fallback))

        route_decision = page_model.metadata.get("route_decision")
        if isinstance(route_decision, dict):
            route = str(route_decision.get("route") or "unknown")
            route_counts[route] = route_counts.get(route, 0) + 1
            profile = route_decision.get("profile")
            if isinstance(profile, dict):
                tier = str(profile.get("tier") or "unknown")
                route_tier_counts[tier] = route_tier_counts.get(tier, 0) + 1

        for block in page_model.blocks:
            if block.metadata.get("ocr_cache_hit"):
                ocr_cache_hit_count += 1
            if block.metadata.get("ocr_cache_miss"):
                ocr_cache_miss_count += 1

    return {
        "requested": bool(ai_fallback_config.get("enabled")),
        "mode": ai_fallback_config.get("mode"),
        "provider": ai_fallback_config.get("provider"),
        "model": ai_fallback_config.get("model"),
        "attempted_page_count": attempted_page_count,
        "applied_page_count": applied_page_count,
        "ai_cache_hit_count": ai_cache_hit_count,
        "ocr_cache_hit_count": ocr_cache_hit_count,
        "ocr_cache_miss_count": ocr_cache_miss_count,
        "status_counts": status_counts,
        "route_counts": route_counts,
        "route_tier_counts": route_tier_counts,
        "model_fallbacks": model_fallbacks,
    }


def _coerce_source_paths(source: str | Path | Sequence[str | Path]) -> list[Path]:
    if isinstance(source, (str, Path)):
        return [Path(source).resolve()]
    resolved = [Path(item).resolve() for item in source]
    if not resolved:
        raise ValueError("At least one source path is required")
    return resolved


def _problem_block_ids(problem: ProblemUnit) -> list[str]:
    return (
        list(problem.stem_block_ids)
        + list(problem.choice_block_ids)
        + list(problem.explanation_block_ids)
        + list(problem.figure_block_ids)
    )


def _expand_problem_bounds(box: Box, *, page_width: int, page_height: int, padding_px: float, top_padding_px: float | None = None) -> Box:
    resolved_top_padding = padding_px if top_padding_px is None else top_padding_px
    left = max(0.0, box.left - padding_px)
    top = max(0.0, box.top - resolved_top_padding)
    right = min(float(page_width), box.right + padding_px)
    bottom = min(float(page_height), box.bottom + padding_px)
    return Box.from_points(left, top, right, bottom)


def _is_trusted_pdf_text_marker_problem(problem: ProblemUnit, blocks: Sequence[Any]) -> bool:
    if problem.metadata.get("problem_number_source") == "pdf_text_marker":
        return True
    return any(
        getattr(block, "metadata", {}).get("segmenter") == "pdf-text-markers"
        and getattr(block, "metadata", {}).get("problem_number_source") == "pdf_text_marker"
        for block in blocks
    )


def _problem_bounds(page_model: PageModel, problem: ProblemUnit) -> Box:
    metadata_box = problem.metadata.get("bbox_px")
    if isinstance(metadata_box, dict):
        try:
            left = float(metadata_box.get("left", 0.0))
            top = float(metadata_box.get("top", 0.0))
            width = float(metadata_box.get("width", 0.0))
            height = float(metadata_box.get("height", 0.0))
        except (TypeError, ValueError):
            width = 0.0
            height = 0.0
        if width > 1.0 and height > 1.0:
            clamped_left = max(0.0, min(float(page_model.width_px), left))
            clamped_top = max(0.0, min(float(page_model.height_px), top))
            clamped_right = min(float(page_model.width_px), max(0.0, left + width))
            clamped_bottom = min(float(page_model.height_px), max(0.0, top + height))
            return Box(
                left=clamped_left,
                top=clamped_top,
                width=max(0.0, clamped_right - clamped_left),
                height=max(0.0, clamped_bottom - clamped_top),
            ).expanded(24.0, max_width=page_model.width_px, max_height=page_model.height_px)

    block_lookup = {block.block_id: block for block in page_model.blocks}
    selected = [block_lookup[block_id] for block_id in _problem_block_ids(problem) if block_id in block_lookup]
    if not selected:
        return Box(left=0.0, top=0.0, width=float(page_model.width_px), height=float(page_model.height_px))

    left = min(block.bbox.left for block in selected)
    top = min(block.bbox.top for block in selected)
    right = max(block.bbox.right for block in selected)
    bottom = max(block.bbox.bottom for block in selected)
    trusted_pdf_marker_problem = _is_trusted_pdf_text_marker_problem(problem, selected)
    top_padding_px = 0.0 if trusted_pdf_marker_problem else 24.0
    padding_px = PDF_TEXT_MARKER_HORIZONTAL_PADDING_PX if trusted_pdf_marker_problem else 24.0
    return _expand_problem_bounds(
        Box.from_points(left, top, right, bottom),
        page_width=page_model.width_px,
        page_height=page_model.height_px,
        padding_px=padding_px,
        top_padding_px=top_padding_px,
    )


def _problem_title(page_model: PageModel, problem: ProblemUnit, index: int) -> str:
    if problem.title and problem.title.strip():
        return problem.title.strip()
    return f"{page_model.page_id} problem {index + 1}"


def _problem_is_reading_heavy(problem: ProblemUnit) -> bool:
    return (
        problem.subject in {Subject.KOREAN, Subject.ENGLISH}
        or len(problem.choice_block_ids) > 0
        or len(problem.figure_block_ids) > 0
    )


def _render_problem_crops(
    page_models: list[PageModel],
    prepared_pages,
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared_by_page_id = {page.page_id: page for page in prepared_pages}
    crop_paths: dict[str, Path] = {}

    for page_model in page_models:
        prepared_page = prepared_by_page_id.get(page_model.page_id)
        if prepared_page is None:
            continue
        block_by_id = {block.block_id: block for block in page_model.blocks}
        continuation_problems = _collapse_marker_document_continuation_page(
            page_model,
            list(page_model.problems),
            block_by_id,
        )
        if continuation_problems is not None:
            if not continuation_problems:
                page_model.metadata["problem_crop_skip_reason"] = "marker_document_continuation"
                continue
            page_model.problems = continuation_problems
        elif _is_marker_document_continuation_page(page_model, list(page_model.problems), block_by_id):
            page_model.metadata["problem_crop_skip_reason"] = "marker_document_continuation"
            continue
        for index, problem in enumerate(page_model.problems):
            bounds = _problem_bounds(page_model, problem)
            crop = prepared_page.image.crop(
                (
                    int(bounds.left),
                    int(bounds.top),
                    int(bounds.right),
                    int(bounds.bottom),
                )
            )
            crop_name = f"{page_model.page_id.split('-page-')[-1]}_{index + 1:03d}_{hashlib.sha1(problem.unit_id.encode('utf-8', errors='ignore')).hexdigest()[:8]}.png"
            crop_path = output_dir / crop_name
            crop.save(crop_path)
            crop_paths[problem.unit_id] = crop_path

    return crop_paths


def _normalize_marker_document_continuation_problems(page_models: list[PageModel]) -> None:
    for page_model in page_models:
        block_by_id = {block.block_id: block for block in page_model.blocks}
        continuation_problems = _collapse_marker_document_continuation_page(
            page_model,
            list(page_model.problems),
            block_by_id,
        )
        if continuation_problems is None:
            continue
        page_model.problems = continuation_problems


def _template_to_dict(template) -> dict[str, Any]:
    return {
        "name": template.name,
        "board_page_count": template.board_page_count,
        "base_slot_height_pages": template.base_slot_height_pages,
        "fixed_left_zone_ratio": template.fixed_left_zone_ratio,
        "preserve_right_writing_zone": template.preserve_right_writing_zone,
        "default_overflow_subjects": [subject.value for subject in template.default_overflow_subjects],
        "metadata": template.metadata,
    }


def render_board_page(page_model: PageModel, prepared_image: Image.Image, left_zone_ratio: float) -> Image.Image:
    canvas_width = 2160
    canvas_height = 3840
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)

    divider_x = int(round(canvas_width * left_zone_ratio))
    margin = 96
    max_width = max(1, divider_x - margin * 2)
    max_height = max(1, canvas_height - margin * 2)

    scaled = prepared_image.convert("RGB")
    scale = min(max_width / scaled.width, max_height / scaled.height)
    target_size = (max(1, int(round(scaled.width * scale))), max(1, int(round(scaled.height * scale))))
    scaled = scaled.resize(target_size, Image.Resampling.LANCZOS)

    canvas.paste(scaled, (margin, margin))
    draw.line([(divider_x, 0), (divider_x, canvas_height)], fill=(210, 210, 210), width=4)
    draw.text((24, 24), page_model.page_id, fill=(70, 70, 70))
    return canvas


def export_board_edb(board_images: list[Image.Image], output_path: Path, template_name: str) -> None:
    # Each board page record advances by ~1.2 logical pages (matches the
    # 0.024 norm step used previously when page_count_hint was hard-coded to 50).
    # Scale page_count_hint with the record count so ClassIn allocates a logical
    # canvas tall enough to contain every page record — same convention real
    # published EDBs use (~2x record count, floor 50).
    page_count_hint = max(50, len(board_images) * 2)
    step_pages = 1.2  # 0.024 * 50 — preserves the legacy 708px vertical step
    y_step = step_pages / page_count_hint
    records: list[bytes] = []
    for index, image in enumerate(board_images):
        primary = _encode_jpeg(image, quality=92)
        secondary = _encode_jpeg(_make_thumbnail(image), quality=86)
        records.append(
            build_image_record(
                ImageRecordSpec(
                    record_id=index,
                    image_primary=primary,
                    image_secondary=secondary,
                    x=0.0,
                    y=round(index * y_step, 6),
                    width_hint=1 / 3,
                    height_hint=round(y_step, 6),
                )
            )
        )
    payload = build_edb(
        records,
        header_flag=4,
        version="6.0.5.3911",
        page_count_hint=page_count_hint,
    )
    write_edb(output_path, payload)


def page_model_to_board_plan_dict(page_models: list[PageModel], export_plan) -> dict[str, object]:
    placements = []
    for placement in export_plan.placements:
        placements.append(
            {
                "problem_id": placement.problem_id,
                "subject": placement.subject,
                "start_y_pages": placement.start_y_pages,
                "nominal_slot_height_pages": placement.nominal_slot_height_pages,
                "actual_content_height_pages": placement.actual_content_height_pages,
                "actual_bottom_y_pages": placement.actual_bottom_y_pages,
                "snapped_next_start_y_pages": placement.snapped_next_start_y_pages,
                "overflow_allowed": placement.overflow_allowed,
                "overflow_amount_pages": placement.overflow_amount_pages,
                "overflow_violation": placement.overflow_violation,
                "slot_span_count": placement.slot_span_count,
                "board_capacity_exceeded": placement.board_capacity_exceeded,
                "metadata": placement.metadata,
            }
        )
    return {
        "template": _template_to_dict(export_plan.template),
        "placements": placements,
        "page_count": len(page_models),
        "problem_count": len(placements),
    }


def build_ui_session(
    page_models: list[PageModel],
    export_plan,
    rendered_board_paths: list[Path],
    problem_crop_paths: dict[str, Path],
    output_dir: Path,
    edb_path: Path | None,
    source_paths: Sequence[Path] | None = None,
    ai_fallback_config: dict[str, Any] | None = None,
    ai_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_source_paths = [Path(path).resolve() for path in (source_paths or [])]
    placements_by_id = {placement.problem_id: placement for placement in export_plan.placements}
    board_path_by_page_id = {
        page_model.page_id: rendered_board_paths[index]
        for index, page_model in enumerate(page_models)
        if index < len(rendered_board_paths)
    }

    problems: list[dict[str, Any]] = []
    problems_by_page_id: dict[str, list[str]] = {}
    for page_model in page_models:
        for index, problem in enumerate(page_model.problems):
            placement = placements_by_id.get(problem.unit_id)
            board_path = board_path_by_page_id.get(page_model.page_id)
            crop_path = problem_crop_paths.get(problem.unit_id)
            risk_flags = _collect_problem_risk_flags(problem)
            problem_number = _coerce_int(problem.metadata.get("problem_number"))
            if problem_number <= 0:
                problem_number = None
            if (
                str(problem.metadata.get("passage_role") or "").strip() == "passage_fragment"
                and crop_path is not None
                and crop_path.is_file()
            ):
                try:
                    with Image.open(crop_path) as passage_image:
                        problem.metadata["passage_quality"] = assess_passage_crop_quality(
                            passage_image,
                            source_dpi=page_model.metadata.get("dpi"),
                            detection_confidence=problem.metadata.get("passage_detection_confidence"),
                            text_line_count=int(problem.metadata.get("passage_text_line_count") or 0),
                            text_character_count=int(problem.metadata.get("passage_text_character_count") or 0),
                            text_bounds_score=problem.metadata.get("passage_text_bounds_score"),
                        )
                except (OSError, ValueError, TypeError):
                    problem.metadata["passage_quality"] = {
                        "overall_score": 0.0,
                        "score_10": 0.0,
                        "grade": "poor",
                        "warnings": ["passage_crop_quality_unavailable"],
                    }
            passage_payload = _problem_passage_payload(problem.metadata)
            problems_by_page_id.setdefault(page_model.page_id, []).append(problem.unit_id)
            problems.append(
                {
                    "id": problem.unit_id,
                    "title": _problem_title(page_model, problem, index),
                    "problemNumber": problem_number,
                    "subject": problem.subject.value,
                    "imagePath": _to_file_uri(crop_path),
                    "sourceImagePath": _to_file_uri(page_model.source_path),
                    "sourceFileName": Path(page_model.source_path).name,
                    "boardRenderPath": _to_file_uri(board_path),
                    "actualHeightPages": placement.actual_content_height_pages if placement else 1.0,
                    "overflowAllowed": placement.overflow_allowed if placement else False,
                    "readingHeavy": _problem_is_reading_heavy(problem),
                    "sourcePageId": page_model.page_id,
                    "startYPages": placement.start_y_pages if placement else 0.0,
                    "snappedNextStartYPages": placement.snapped_next_start_y_pages if placement else 0.0,
                    "overflowAmountPages": placement.overflow_amount_pages if placement else 0.0,
                    "overflowViolation": placement.overflow_violation if placement else False,
                    "slotSpanCount": placement.slot_span_count if placement else 1,
                    "riskFlags": risk_flags,
                    "reviewStatus": "check_needed" if risk_flags else "normal",
                    **passage_payload,
                }
            )

    hwp_flags_by_page_id, hwp_warning_messages = _collect_hwp_problem_count_mismatches(page_models)
    pages: list[dict[str, Any]] = []
    for page_number, page_model in enumerate(page_models, start=1):
        page_flags = list(dict.fromkeys(hwp_flags_by_page_id.get(page_model.page_id, [])))
        problem_ids = problems_by_page_id.get(page_model.page_id, [])
        pages.append(
            {
                "id": page_model.page_id,
                "pageNumber": page_number,
                "sourceImageUri": _to_file_uri(page_model.source_path),
                "sourceImagePath": str(Path(page_model.source_path).resolve()),
                "width": int(page_model.width_px),
                "height": int(page_model.height_px),
                "problemIds": problem_ids,
                "riskFlags": page_flags,
                "reviewStatus": "failed" if not problem_ids else "check_needed" if page_flags else "normal",
            }
        )

    problem_counts = _session_problem_count_payload(problems)
    passage_groups = _session_passage_groups(problems)
    cross_page_passage_group_count = sum(
        1 for group in passage_groups if group.get("continuesAcrossPages")
    )

    return {
        "session_name": output_dir.name,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "data_source": "build_mvp_export",
        "output_dir": str(output_dir.resolve()),
        "source_mode": "batch" if len(resolved_source_paths) > 1 else "single",
        "input_file_count": max(1, len(resolved_source_paths)) if page_models else len(resolved_source_paths),
        "input_files": [str(path) for path in resolved_source_paths],
        "pages_json_path": str((output_dir / "pages.json").resolve()),
        "placements_json_path": str((output_dir / "placements.json").resolve()),
        "edb_path": str(edb_path.resolve()) if edb_path else None,
        "edb_file_uri": _to_file_uri(edb_path),
        "rendered_page_paths": [str(path.resolve()) for path in rendered_board_paths],
        "rendered_page_file_uris": [_to_file_uri(path) for path in rendered_board_paths],
        "template": _template_to_dict(export_plan.template),
        "ai_fallback": ai_fallback_config,
        "ai_summary": ai_summary,
        **problem_counts,
        "passageGroups": passage_groups,
        "passage_groups": passage_groups,
        "passageGroupCount": len(passage_groups),
        "passage_group_count": len(passage_groups),
        "passageProblemCount": sum(int(group.get("problemCount") or 0) for group in passage_groups),
        "passage_problem_count": sum(int(group.get("problemCount") or 0) for group in passage_groups),
        "crossPagePassageGroupCount": cross_page_passage_group_count,
        "cross_page_passage_group_count": cross_page_passage_group_count,
        "warning_messages": hwp_warning_messages,
        "pages": pages,
        "problems": problems,
    }


def write_ui_session_bundle(output_dir: Path, ui_session: dict[str, Any], *, sync_ui: bool) -> tuple[Path, Path | None]:
    session_path = output_dir / "ui_session.json"
    session_path.write_text(json.dumps(ui_session, ensure_ascii=False, indent=2), encoding="utf-8")

    synced_path: Path | None = None
    if sync_ui:
        synced_path = output_dir / "generated_session.js"
        synced_path.write_text(
            "window.EDB_UI_SESSION = " + json.dumps(ui_session, ensure_ascii=False, indent=2) + ";\n",
            encoding="utf-8",
        )
    return session_path, synced_path


def run_export(
    source: str | Path | Sequence[str | Path],
    *,
    output_dir: str | Path = "mvp_export",
    subject_name: str = "unknown",
    ocr: str = "auto",
    pdf_dpi: int = 200,
    detect_perspective: bool = False,
    skip_deskew: bool = False,
    skip_crop: bool = False,
    max_dimension: int | None = None,
    export_edb: bool = False,
    edb_name: str = "mvp_board.edb",
    sync_ui: bool = False,
    ai_fallback_enabled: bool = False,
    ai_fallback: str | None = None,
    ai_fallback_provider: str = "gemini",
    ai_fallback_model: str = "",
    ai_fallback_prompt: str = "",
    ai_fallback_max_tokens: int | None = None,
    ai_fallback_temperature: float | None = None,
    ai_fallback_threshold: float = 0.72,
    ai_fallback_max_regions: int = 48,
    ai_fallback_timeout_ms: int = 30000,
    ai_fallback_save_debug: bool = False,
    fail_on_ai_error: bool = False,
) -> dict[str, Any]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "board_pages").mkdir(parents=True, exist_ok=True)
    (out_dir / "problem_crops").mkdir(parents=True, exist_ok=True)

    subject = _resolve_subject(subject_name)
    source_paths = _coerce_source_paths(source)
    ai_fallback_config = _build_ai_fallback_config(
        enabled=ai_fallback_enabled,
        mode=ai_fallback,
        provider=ai_fallback_provider,
        model=ai_fallback_model,
        prompt=ai_fallback_prompt,
        max_tokens=ai_fallback_max_tokens,
        temperature=ai_fallback_temperature,
        threshold=ai_fallback_threshold,
        max_regions=ai_fallback_max_regions,
        timeout_ms=ai_fallback_timeout_ms,
        save_debug=ai_fallback_save_debug,
        fail_on_error=fail_on_ai_error,
    )
    page_ai_config = _to_page_ai_config(ai_fallback_config)
    prepared_pages = (
        prepare_source_pages(
            source_paths[0],
            pdf_dpi=pdf_dpi,
            detect_perspective=detect_perspective,
            deskew=not skip_deskew,
            crop_margins=not skip_crop,
            max_dimension=max_dimension,
        )
        if len(source_paths) == 1
        else prepare_source_pages_batch(
            source_paths,
            pdf_dpi=pdf_dpi,
            detect_perspective=detect_perspective,
            deskew=not skip_deskew,
            crop_margins=not skip_crop,
            max_dimension=max_dimension,
        )
    )

    page_models: list[PageModel] = [
        build_page_model(prepared_page, subject=subject, ocr_mode=ocr, ai_config=page_ai_config)
        for prepared_page in prepared_pages
    ]
    _remove_duplicate_marker_document_problem_numbers(page_models)
    _remove_hwp_template_instruction_problems(page_models)
    _normalize_marker_document_continuation_problems(page_models)
    _annotate_cross_page_passage_groups(page_models)
    save_pages_json(page_models, out_dir / "pages.json")
    ai_summary = _summarize_ai_fallback_usage(page_models, ai_fallback_config)

    export_plan = build_export_plan(page_models, template=build_default_template())
    board_plan_dict = page_model_to_board_plan_dict(page_models, export_plan)
    (out_dir / "placements.json").write_text(json.dumps(board_plan_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    problem_crop_paths = _render_problem_crops(page_models, prepared_pages, out_dir / "problem_crops")

    rendered_board_paths: list[Path] = []
    board_images: list[Image.Image] = []
    for index, prepared_page in enumerate(prepared_pages):
        board_image = render_board_page(page_models[index], prepared_page.image, export_plan.template.fixed_left_zone_ratio)
        board_path = out_dir / "board_pages" / f"{prepared_page.page_id}.png"
        board_image.save(board_path)
        rendered_board_paths.append(board_path)
        board_images.append(board_image)

    board_plan_dict["rendered_page_paths"] = [str(path) for path in rendered_board_paths]
    (out_dir / "placements.json").write_text(json.dumps(board_plan_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    edb_path: Path | None = None
    if export_edb:
        edb_path = out_dir / edb_name
        export_board_edb(board_images, edb_path, export_plan.template.name)
        board_plan_dict["edb_path"] = str(edb_path)
        (out_dir / "placements.json").write_text(json.dumps(board_plan_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    ui_session = build_ui_session(
        page_models,
        export_plan,
        rendered_board_paths,
        problem_crop_paths,
        out_dir,
        edb_path,
        source_paths=source_paths,
        ai_fallback_config=ai_fallback_config,
        ai_summary=ai_summary,
    )
    ui_session_path, synced_ui_path = write_ui_session_bundle(out_dir, ui_session, sync_ui=sync_ui)

    return {
        "output_dir": out_dir,
        "source_paths": [str(path) for path in source_paths],
        "page_models": page_models,
        "problem_crop_paths": problem_crop_paths,
        "rendered_board_paths": rendered_board_paths,
        "edb_path": edb_path,
        "ui_session": ui_session,
        "ui_session_path": ui_session_path,
        "synced_ui_path": synced_ui_path,
        "ai_fallback": ai_fallback_config,
        "ai_summary": ai_summary,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the first MVP export for ClassIn EDB.")
    parser.add_argument("source", type=Path, nargs="+", help="Input image(s) or PDF")
    parser.add_argument("--output-dir", type=Path, default=Path("mvp_export"), help="Output directory")
    parser.add_argument("--subject", default="unknown", help="Subject override: math/science/korean/english/social")
    parser.add_argument("--ocr", default="auto", help="OCR backend: auto/paddle/tesseract/none")
    parser.add_argument("--pdf-dpi", type=int, default=200, help="PDF render DPI")
    parser.add_argument("--detect-perspective", action="store_true", help="Try perspective correction for photographed sources")
    parser.add_argument("--skip-deskew", action="store_true", help="Disable deskew")
    parser.add_argument("--skip-crop", action="store_true", help="Disable margin crop")
    parser.add_argument("--max-dimension", type=int, default=None, help="Resize long edge to this many pixels")
    parser.add_argument("--export-edb", action="store_true", help="Also export a board-image .edb")
    parser.add_argument("--edb-name", default="mvp_board.edb", help="Output .edb filename")
    parser.add_argument("--sync-ui", action="store_true", help="Legacy opt-in: write generated_session.js next to ui_session.json")
    parser.add_argument("--skip-ui-sync", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ai-fallback-enabled", action="store_true", help="Enable optional AI fallback settings")
    parser.add_argument("--ai-fallback", default=None, help="AI fallback mode override: off, auto, force")
    parser.add_argument("--ai-fallback-provider", default="gemini", help="AI fallback provider name")
    parser.add_argument("--ai-fallback-model", default="", help="AI fallback model name")
    parser.add_argument("--ai-fallback-prompt", default="", help="AI fallback prompt template")
    parser.add_argument("--ai-fallback-max-tokens", type=int, default=None, help="AI fallback max output tokens")
    parser.add_argument("--ai-fallback-temperature", type=float, default=None, help="AI fallback sampling temperature")
    parser.add_argument("--ai-fallback-threshold", type=float, default=0.72, help="Low-confidence trigger threshold for AI fallback")
    parser.add_argument("--ai-fallback-max-regions", type=int, default=48, help="Maximum number of regions sent to AI fallback")
    parser.add_argument("--ai-fallback-timeout-ms", type=int, default=30000, help="Timeout in milliseconds for AI fallback")
    parser.add_argument("--ai-fallback-save-debug", action="store_true", help="Write AI fallback debug artifacts")
    parser.add_argument("--fail-on-ai-error", action="store_true", help="Raise an error if AI fallback fails")
    args = parser.parse_args()

    result = run_export(
        args.source,
        output_dir=args.output_dir,
        subject_name=args.subject,
        ocr=args.ocr,
        pdf_dpi=args.pdf_dpi,
        detect_perspective=args.detect_perspective,
        skip_deskew=args.skip_deskew,
        skip_crop=args.skip_crop,
        max_dimension=args.max_dimension,
        export_edb=args.export_edb,
        edb_name=args.edb_name,
        sync_ui=bool(args.sync_ui) and not args.skip_ui_sync,
        ai_fallback_enabled=args.ai_fallback_enabled,
        ai_fallback=args.ai_fallback,
        ai_fallback_provider=args.ai_fallback_provider,
        ai_fallback_model=args.ai_fallback_model,
        ai_fallback_prompt=args.ai_fallback_prompt,
        ai_fallback_max_tokens=args.ai_fallback_max_tokens,
        ai_fallback_temperature=args.ai_fallback_temperature,
        ai_fallback_threshold=args.ai_fallback_threshold,
        ai_fallback_max_regions=args.ai_fallback_max_regions,
        ai_fallback_timeout_ms=args.ai_fallback_timeout_ms,
        ai_fallback_save_debug=args.ai_fallback_save_debug,
        fail_on_ai_error=args.fail_on_ai_error,
    )

    print(
        f"wrote pages.json, placements.json, ui_session.json, "
        f"{len(result['problem_crop_paths'])} problem crops, and {len(result['rendered_board_paths'])} board page renders -> {result['output_dir']}"
    )
    if args.export_edb:
        print(f"exported EDB -> {result['edb_path']}")
    print(f"wrote UI session -> {result['ui_session_path']}")
    if result["synced_ui_path"] is not None:
        print(f"synced UI session -> {result['synced_ui_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
