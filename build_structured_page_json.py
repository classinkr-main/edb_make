#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from ai_usage import (
    aggregate_token_usage,
    summarize_ai_cost,
    summarize_token_efficiency,
)
from ocr_backend import (
    DEFAULT_GEMINI_OCR_MODEL,
    ECONOMY_GEMINI_OCR_MODEL,
    GeminiOCRBackend,
    create_ocr_backend,
    ocr_cache_fingerprint,
    preferred_ocr_backend_name,
)
from page_repair import AIFallbackConfig, build_ai_fallback_config, repair_page_model
from pipeline_cache import PipelineCache
from preprocess import PreparedPage, prepare_source_pages
from segment import crop_block_image, draw_segment_debug, segment_page
from structured_schema import BlockType, ContentBlock, PageModel, Subject, classify_text_block, infer_math_like_text, save_pages_json, TextStyle


def load_env_local() -> None:
    # edb_make 전용 .env.local 만 읽어옵니다. (Classin_Home 프로젝트와 완전히 분리)
    env_path = Path(__file__).resolve().parent / ".env.local"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

load_env_local()

_LATEX_COMMAND_PATTERN = re.compile(
    r"\\(?:"
    r"alpha|beta|gamma|delta|theta|lambda|mu|pi|rho|sigma|phi|omega|"
    r"sin|cos|tan|log|ln|sqrt|frac|sum|prod|int|lim|"
    r"times|div|pm|cdot|leq|geq|neq|infty"
    r")\b",
    re.IGNORECASE,
)
_UNPUNCTUATED_OR_MALFORMED_STEM_END_PATTERN = re.compile(
    r"(?:구하|고르|쓰|설명하|서술하|계산하|선택하|판단하|확인하|작성하|나타내|말하)"
    r"[가-힣]{0,3}$"
)


def _resolve_subject(name: str | None) -> Subject:
    if not name:
        return Subject.UNKNOWN
    try:
        return Subject(name.lower())
    except ValueError:
        return Subject.UNKNOWN


def build_run_summary(
    pages: list[PageModel],
    *,
    output_dir: str | Path,
    source: str | Path,
    ocr_mode: str,
    ai_config: AIFallbackConfig | None = None,
) -> dict[str, object]:
    fallback_block_count = 0
    text_block_count = 0
    image_block_count = 0
    ai_attempted_pages = 0
    ai_applied_pages = 0
    ai_cache_hits = 0
    ocr_cache_hits = 0
    ocr_cache_misses = 0
    route_counts: dict[str, int] = {}
    route_tier_counts: dict[str, int] = {}
    token_usage_events: list[dict[str, Any]] = []

    for page in pages:
        ai_summary = page.metadata.get("ai_fallback")
        if isinstance(ai_summary, dict):
            if ai_summary.get("attempted"):
                ai_attempted_pages += 1
            if ai_summary.get("applied"):
                ai_applied_pages += 1
            if ai_summary.get("cache_hit"):
                ai_cache_hits += 1
            raw_token_usage = ai_summary.get("token_usage")
            if isinstance(raw_token_usage, dict):
                token_usage_events.append(dict(raw_token_usage))
        route_summary = page.metadata.get("route_decision")
        if isinstance(route_summary, dict):
            route = str(route_summary.get("route") or "unknown")
            route_counts[route] = route_counts.get(route, 0) + 1
            profile = route_summary.get("profile")
            if isinstance(profile, dict):
                tier = str(profile.get("tier") or "unknown")
                route_tier_counts[tier] = route_tier_counts.get(tier, 0) + 1
        for block in page.blocks:
            if block.text:
                text_block_count += 1
            if block.block_type == BlockType.IMAGE:
                image_block_count += 1
            if block.metadata.get("fallback_reason"):
                fallback_block_count += 1
            if block.metadata.get("ocr_cache_hit"):
                ocr_cache_hits += 1
            if block.metadata.get("ocr_cache_miss"):
                ocr_cache_misses += 1
            raw_events = block.metadata.get("ai_token_usage_events")
            if isinstance(raw_events, list):
                token_usage_events.extend(
                    dict(event) for event in raw_events if isinstance(event, dict)
                )

    return {
        "source": str(source),
        "output_dir": str(output_dir),
        "ocr_mode": ocr_mode,
        "page_count": len(pages),
        "problem_count": sum(len(page.problems) for page in pages),
        "block_count": sum(len(page.blocks) for page in pages),
        "text_block_count": text_block_count,
        "image_block_count": image_block_count,
        "fallback_block_count": fallback_block_count,
        "ai_fallback": (ai_config or AIFallbackConfig()).to_metadata(),
        "ai_attempted_page_count": ai_attempted_pages,
        "ai_applied_page_count": ai_applied_pages,
        "ai_cache_hit_count": ai_cache_hits,
        "ocr_cache_hit_count": ocr_cache_hits,
        "ocr_cache_miss_count": ocr_cache_misses,
        "route_counts": route_counts,
        "route_tier_counts": route_tier_counts,
        "token_usage": aggregate_token_usage(token_usage_events),
        "ai_token_efficiency": summarize_token_efficiency(token_usage_events),
        "ai_cost_summary": summarize_ai_cost(token_usage_events),
        "pages_json_path": str(Path(output_dir) / "pages.json"),
    }


def _maybe_build_gemini_escalation(
    *,
    ai_config: AIFallbackConfig | None,
    primary_backend_name: str,
    primary_model: str | None = None,
) -> GeminiOCRBackend | None:
    """Build a quality OCR backend for low-confidence blocks.

    Local OCR escalates to the balanced Gemini model. Economy Gemini OCR can
    likewise escalate only uncertain blocks to the current quality model.
    """
    if ai_config is None or not ai_config.enabled:
        return None
    if (
        primary_backend_name == "gemini"
        and str(primary_model or "").strip().lower() != ECONOMY_GEMINI_OCR_MODEL
    ):
        return None
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        return None
    try:
        return GeminiOCRBackend(
            model=(
                DEFAULT_GEMINI_OCR_MODEL
                if primary_backend_name == "gemini"
                else None
            )
        )
    except Exception:
        return None


def _ocr_exactness_risk(ocr_result) -> bool:
    r"""Detect economy-model output that likely rewrote visible exam notation.

    OCR output is displayed as plain text. A Lite-model LaTeX command such as
    ``\pi`` or ``\sin`` therefore indicates semantic normalization rather than
    exact transcription and must be checked by the balanced quality model.
    """
    model = str(ocr_result.metadata.get("model") or "").strip().lower()
    if model != ECONOMY_GEMINI_OCR_MODEL:
        return False
    text = str(ocr_result.text or "").strip()
    if _LATEX_COMMAND_PATTERN.search(text):
        return True
    block_type = str(ocr_result.metadata.get("block_type_hint") or "").strip().lower()
    return block_type == "stem" and bool(
        _UNPUNCTUATED_OR_MALFORMED_STEM_END_PATTERN.search(text)
    )


def _ocr_escalation_reason(ocr_result, *, threshold: float) -> str | None:
    """Return the reason a primary OCR result needs a quality-model check."""
    text = (ocr_result.text or "").strip()
    if not text:
        return "empty_primary"
    if _ocr_exactness_risk(ocr_result):
        return "exactness_risk"
    confidence = ocr_result.confidence
    if confidence is None:
        return None
    if float(confidence) < float(threshold):
        return "low_confidence_primary"
    return None


def _ocr_needs_escalation(ocr_result, *, threshold: float) -> bool:
    return _ocr_escalation_reason(ocr_result, threshold=threshold) is not None


def _should_accept_ocr_escalation(
    primary_result,
    escalated_result,
    *,
    reason: str,
) -> bool:
    escalated_text = (escalated_result.text or "").strip()
    if not escalated_text:
        return False
    if reason == "exactness_risk":
        return True
    primary_text = (primary_result.text or "").strip()
    if not primary_text:
        return True
    primary_conf = (
        primary_result.confidence if primary_result.confidence is not None else 0.0
    )
    escalated_conf = (
        escalated_result.confidence
        if escalated_result.confidence is not None
        else 0.0
    )
    return float(escalated_conf) >= float(primary_conf)


def _should_skip_ocr_for_trusted_block(
    block: ContentBlock,
    *,
    page_segmenter: str | None,
) -> bool:
    return (
        page_segmenter == "pdf-text-markers"
        and block.metadata.get("segmenter") == "pdf-text-markers"
        and bool(block.metadata.get("force_image_record"))
        and bool(block.metadata.get("force_problem_start"))
        and isinstance(block.metadata.get("problem_number"), int)
        and block.metadata.get("problem_number_source") == "pdf_text_marker"
        and bool((block.text or "").strip())
        and block.confidence is not None
    )


_OCR_IDENTITY_NORMALIZATION_KEYS = (
    "source_type",
    "dpi",
    "pdf_renderer",
    "pdf_page_width_pt",
    "pdf_page_height_pt",
    "perspective_corrected",
    "deskewed",
    "deskew_skipped_reason",
    "margin_cropped",
    "margin_crop_box",
    "resized_to_max_dimension",
    "document_like",
)


def _file_identity(path_value: object) -> dict[str, Any]:
    path_text = str(path_value or "").strip()
    if not path_text:
        return {"path": ""}
    path = Path(path_text).expanduser().resolve()
    identity: dict[str, Any] = {"path": str(path)}
    try:
        stat = path.stat()
    except OSError:
        return identity
    identity["stat"] = {
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    identity["sha256"] = _file_sha256(path)
    return identity


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "unavailable"
    return digest.hexdigest()


def _bucket_coordinate(value: float, *, bucket_size: int = 4) -> int:
    return int(round(float(value) / float(bucket_size)) * bucket_size)


def _normalization_identity(metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metadata[key]
        for key in _OCR_IDENTITY_NORMALIZATION_KEYS
        if key in metadata
    }


def _page_metadata_for_output(metadata: dict[str, Any]) -> dict[str, Any]:
    output_metadata = dict(metadata)
    pdf_text_lines = output_metadata.pop("pdf_text_lines", None)
    if isinstance(pdf_text_lines, list):
        output_metadata["pdf_text_line_count"] = len(pdf_text_lines)
    return output_metadata


def _build_ocr_cache_identity(
    prepared_page: PreparedPage,
    block: ContentBlock,
    *,
    backend_fingerprint: str = "",
    source_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(prepared_page.metadata)
    source_path = metadata.get("original_source_path") or prepared_page.source_path
    bucket_size = 4
    return {
        "version": "ocr_block_identity_v2",
        "backend_fingerprint": backend_fingerprint,
        "source": dict(source_identity or _file_identity(source_path)),
        "page": {
            "id": prepared_page.page_id,
            "number": int(prepared_page.page_number),
            "size": {
                "width": int(prepared_page.image.width),
                "height": int(prepared_page.image.height),
            },
        },
        "normalization": _normalization_identity(metadata),
        "block": {
            "bbox_bucket_size_px": bucket_size,
            "bbox_bucket_px": {
                "left": _bucket_coordinate(block.bbox.left, bucket_size=bucket_size),
                "top": _bucket_coordinate(block.bbox.top, bucket_size=bucket_size),
                "right": _bucket_coordinate(block.bbox.right, bucket_size=bucket_size),
                "bottom": _bucket_coordinate(block.bbox.bottom, bucket_size=bucket_size),
            },
        },
    }


def _elapsed_ms(started_at: float) -> int:
    return int(round(max(0.0, time.perf_counter() - started_at) * 1000.0))


def _is_ai_backend(backend_name: str | None) -> bool:
    return (backend_name or "").strip().lower() in {
        "gemini",
        "google",
    }


def _lazy_cache_backend_name(ocr_mode: str | None) -> str | None:
    normalized = (ocr_mode or "").strip().lower()
    if normalized in {"paddle", "paddleocr"}:
        return "paddleocr"
    if normalized == "tesseract":
        return "tesseract"
    if normalized in {"gemini", "google"}:
        return "gemini"
    if normalized.startswith("gemini-"):
        return "gemini"
    return None


def _backend_cache_fingerprint(backend: object, fallback_name: str) -> str:
    fingerprint = str(getattr(backend, "cache_fingerprint", "") or "").strip()
    return fingerprint or ocr_cache_fingerprint(fallback_name)


def _gemini_escalation_may_be_available(
    *,
    ai_config: AIFallbackConfig | None,
    primary_backend_name: str | None,
) -> bool:
    if ai_config is None or not ai_config.enabled:
        return False
    if (primary_backend_name or "").strip().lower() == "gemini":
        return False
    return bool(os.environ.get("GEMINI_API_KEY", "").strip())


def resolve_block_ocr_worker_count(
    item_count: int,
    *,
    ocr_mode: str,
    backend_name: str,
) -> int:
    bounded_item_count = max(0, int(item_count))
    if bounded_item_count <= 0:
        return 1
    max_workers = max(1, min(8, bounded_item_count or 1))
    normalized_mode = (ocr_mode or "").strip().lower()
    normalized_backend = (backend_name or "").strip().lower()
    if normalized_mode in {"none", "noop"} or normalized_backend in {"none", "noop"}:
        return 1
    if normalized_mode in {"paddle", "paddleocr"} or normalized_backend in {"paddle", "paddleocr"}:
        return 1
    network_backend = (
        normalized_mode in {"gemini", "google"}
        or normalized_mode.startswith("gemini-")
        or normalized_backend in {"gemini", "google"}
    )
    if network_backend:
        try:
            ai_cap = int(os.environ.get("EDB_AI_MAX_WORKERS", "3").strip() or "3")
        except ValueError:
            ai_cap = 3
        max_workers = min(max_workers, max(1, ai_cap))
    raw_worker_count = os.environ.get("EDB_RECOGNITION_BLOCK_WORKERS", "").strip()
    if raw_worker_count:
        try:
            requested_workers = int(raw_worker_count)
        except ValueError:
            requested_workers = 0
        if requested_workers > 0:
            return max(1, min(max_workers, requested_workers))

    if network_backend:
        return max_workers

    default_workers = min(max_workers, os.cpu_count() or 2)
    return max(1, default_workers)


def resolve_recognition_worker_count(
    item_count: int,
    *,
    ocr_mode: str = "auto",
    ai_config: AIFallbackConfig | None = None,
) -> int:
    if item_count <= 1:
        return 1

    normalized_ocr = (ocr_mode or "auto").strip().lower()
    gemini_primary = normalized_ocr in {"auto", "gemini", "google"} and bool(
        os.environ.get("GEMINI_API_KEY", "").strip()
    )
    ai_enabled = bool(ai_config and ai_config.enabled)
    network_bound = gemini_primary or ai_enabled

    raw_worker_count = (
        os.environ.get("EDB_RECOGNITION_PAGE_WORKERS", "").strip()
        or os.environ.get("EDB_RECOGNITION_WORKERS", "").strip()
    )
    if raw_worker_count:
        try:
            requested_workers = int(raw_worker_count)
        except ValueError:
            requested_workers = 2
        if network_bound:
            try:
                network_cap = int(os.environ.get("EDB_AI_MAX_WORKERS", "3").strip() or "3")
            except ValueError:
                network_cap = 3
            return max(1, min(item_count, requested_workers, max(1, network_cap)))
        return max(1, min(item_count, requested_workers))

    # Local/default recognition is CPU-heavy before OCR, so match the
    # preprocessing stage's modest four-worker cap. Network AI defaults to
    # three workers and remains bounded by EDB_AI_MAX_WORKERS.
    if network_bound:
        try:
            network_cap = int(os.environ.get("EDB_AI_MAX_WORKERS", "3").strip() or "3")
        except ValueError:
            network_cap = 3
        default_workers = min(3, max(1, network_cap))
    else:
        default_workers = min(4, os.cpu_count() or 2)
    return max(1, min(item_count, default_workers))


def resolve_global_ocr_worker_limit(
    *,
    ocr_mode: str,
    ai_config: AIFallbackConfig | None = None,
    resolved_backend_name: str | None = None,
) -> int:
    backend_name = (resolved_backend_name or preferred_ocr_backend_name(ocr_mode)).strip().lower()
    if backend_name in {"none", "noop", "paddle", "paddleocr"}:
        return 1
    if backend_name == "gemini" or bool(ai_config and ai_config.enabled):
        try:
            configured = int(os.environ.get("EDB_AI_MAX_WORKERS", "3").strip() or "3")
        except ValueError:
            configured = 3
        return max(1, min(8, configured))
    try:
        configured = int(
            os.environ.get("EDB_LOCAL_OCR_MAX_WORKERS", str(min(8, os.cpu_count() or 2))).strip()
        )
    except ValueError:
        configured = min(8, os.cpu_count() or 2)
    return max(1, min(8, configured))


def build_page_models_for_prepared_pages(
    prepared_pages: list[PreparedPage],
    *,
    subject: Subject,
    ocr_mode: str,
    ai_config: AIFallbackConfig | None = None,
    ocr_semaphore: threading.BoundedSemaphore | None = None,
    global_ocr_worker_limit: int | None = None,
) -> list[PageModel]:
    resolved_backend_name = preferred_ocr_backend_name(ocr_mode)
    worker_count = resolve_recognition_worker_count(
        len(prepared_pages),
        ocr_mode=ocr_mode,
        ai_config=ai_config,
    )
    if resolved_backend_name == "paddleocr":
        worker_count = 1
    resolved_global_ocr_worker_limit = global_ocr_worker_limit or resolve_global_ocr_worker_limit(
        ocr_mode=ocr_mode,
        ai_config=ai_config,
        resolved_backend_name=resolved_backend_name,
    )
    shared_ocr_semaphore = ocr_semaphore or threading.BoundedSemaphore(
        resolved_global_ocr_worker_limit
    )

    # A multipage document points every prepared page back to the same
    # original PDF/HWP. Hash each distinct original once before page workers
    # start instead of rereading the entire document in every worker.
    source_identities: dict[str, dict[str, Any]] = {}
    for prepared_page in prepared_pages:
        source_path = str(
            prepared_page.metadata.get("original_source_path")
            or prepared_page.source_path
        )
        if source_path not in source_identities:
            source_identities[source_path] = _file_identity(source_path)

    if worker_count <= 1:
        pages = [
            build_page_model(
                prepared_page,
                subject=subject,
                ocr_mode=ocr_mode,
                ai_config=ai_config,
                ocr_semaphore=shared_ocr_semaphore,
                global_ocr_worker_limit=resolved_global_ocr_worker_limit,
                source_identity=source_identities[
                    str(
                        prepared_page.metadata.get("original_source_path")
                        or prepared_page.source_path
                    )
                ],
            )
            for prepared_page in prepared_pages
        ]
    else:
        def _build(prepared_page: PreparedPage) -> PageModel:
            return build_page_model(
                prepared_page,
                subject=subject,
                ocr_mode=ocr_mode,
                ai_config=ai_config,
                ocr_semaphore=shared_ocr_semaphore,
                global_ocr_worker_limit=resolved_global_ocr_worker_limit,
                source_identity=source_identities[
                    str(
                        prepared_page.metadata.get("original_source_path")
                        or prepared_page.source_path
                    )
                ],
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            pages = list(executor.map(_build, prepared_pages))

    for page in pages:
        page.metadata["recognition_page_worker_count"] = worker_count
        page.metadata["global_ocr_worker_limit"] = resolved_global_ocr_worker_limit
    return pages


def build_page_model(
    prepared_page: PreparedPage,
    subject: Subject,
    ocr_mode: str,
    *,
    ai_config: AIFallbackConfig | None = None,
    cache: PipelineCache | None = None,
    ocr_semaphore: threading.BoundedSemaphore | None = None,
    global_ocr_worker_limit: int | None = None,
    source_identity: dict[str, Any] | None = None,
) -> PageModel:
    recognition_started_at = time.perf_counter()
    pipeline_cache = cache or PipelineCache.for_source(prepared_page.source_path)
    segmentation_started_at = time.perf_counter()
    segmented_page = segment_page(prepared_page, page_id=prepared_page.page_id, subject=subject)
    segmentation_elapsed_ms = _elapsed_ms(segmentation_started_at)
    blocks = segmented_page.blocks
    escalation_threshold = float(ai_config.threshold) if ai_config else 0.0
    page_segmenter = segmented_page.metadata.get("segmenter")

    ocr_eligible_block_count = sum(
        1
        for block in blocks
        if block.block_type not in {BlockType.IMAGE, BlockType.DIAGRAM, BlockType.TABLE}
        and not _should_skip_ocr_for_trusted_block(block, page_segmenter=page_segmenter)
    )
    ocr_skipped_block_count = len(blocks) - ocr_eligible_block_count
    lazy_backend_name = _lazy_cache_backend_name(ocr_mode) if ocr_eligible_block_count > 0 else None
    backend = None if lazy_backend_name else (
        create_ocr_backend(ocr_mode) if ocr_eligible_block_count > 0 else None
    )
    backend_name = lazy_backend_name or (backend.engine_name if backend is not None else "none")
    backend_cache_fingerprint = (
        ocr_cache_fingerprint(ocr_mode)
        if backend is None
        else _backend_cache_fingerprint(backend, backend_name)
    )
    source_path_for_cache = (
        prepared_page.metadata.get("original_source_path")
        or prepared_page.source_path
    )
    ocr_source_identity = (
        dict(source_identity)
        if source_identity is not None
        else _file_identity(source_path_for_cache)
    )
    backend_lock = threading.Lock()
    escalation_backend = (
        _maybe_build_gemini_escalation(
            ai_config=ai_config,
            primary_backend_name=backend_name,
        )
        if backend is not None
        else None
    )
    escalation_backend_checked = backend is not None
    escalation_backend_lock = threading.Lock()
    block_worker_count = resolve_block_ocr_worker_count(
        ocr_eligible_block_count,
        ocr_mode=ocr_mode,
        backend_name=backend_name,
    )
    resolved_global_ocr_worker_limit = global_ocr_worker_limit or resolve_global_ocr_worker_limit(
        ocr_mode=ocr_mode,
        ai_config=ai_config,
        resolved_backend_name=backend_name,
    )
    call_semaphore = ocr_semaphore or threading.BoundedSemaphore(
        resolved_global_ocr_worker_limit
    )

    def _get_backend():
        nonlocal backend, backend_name
        if backend is not None:
            return backend
        with backend_lock:
            if backend is None:
                backend = create_ocr_backend(ocr_mode)
                backend_name = backend.engine_name
        return backend

    def _get_escalation_backend(
        primary_backend_name: str | None,
        primary_model: str | None = None,
    ):
        nonlocal escalation_backend, escalation_backend_checked
        if escalation_backend_checked:
            return escalation_backend
        with escalation_backend_lock:
            if not escalation_backend_checked:
                escalation_backend = _maybe_build_gemini_escalation(
                    ai_config=ai_config,
                    primary_backend_name=primary_backend_name or backend_name,
                    primary_model=primary_model,
                )
                escalation_backend_checked = True
        return escalation_backend

    def _process_block(block):
        token_usage_events: list[dict[str, Any]] = []

        def _record_live_token_usage(result: Any, *, stage: str) -> None:
            raw_usage = result.metadata.get("token_usage")
            if not isinstance(raw_usage, dict):
                return
            event = dict(raw_usage)
            event["stage"] = stage
            model = str(result.metadata.get("model") or "").strip()
            if model:
                event["model"] = model
            token_usage_events.append(event)

        if _should_skip_ocr_for_trusted_block(block, page_segmenter=page_segmenter):
            block.metadata["ocr_backend"] = "pdf_text_marker"
            block.metadata["ocr_skipped"] = True
            block.metadata["ocr_skipped_reason"] = "trusted_pdf_text_marker"
            block.metadata["ocr_empty_text"] = False
            block.metadata["ocr_text_length"] = len((block.text or "").strip())
            block.metadata["ocr_line_count"] = len(block.ocr_lines)
            return

        if block.block_type in {BlockType.IMAGE, BlockType.DIAGRAM, BlockType.TABLE}:
            return

        crop = crop_block_image(prepared_page, block)
        cache_identity = _build_ocr_cache_identity(
            prepared_page,
            block,
            backend_fingerprint=backend_cache_fingerprint,
            source_identity=ocr_source_identity,
        )
        cached_ocr = pipeline_cache.load_ocr_result(
            crop,
            backend_name=backend_name,
            cache_identity=cache_identity,
        )
        block.metadata["ocr_stage"] = "primary_ocr"
        block.metadata["ocr_primary_backend"] = backend_name
        if cached_ocr is not None:
            ocr_result = cached_ocr
            block.metadata["ocr_cache_hit"] = True
            block.metadata["ocr_cache_miss"] = False
        else:
            call_semaphore.acquire()
            try:
                active_backend = _get_backend()
                started_at = time.perf_counter()
                ocr_result = active_backend.recognize(crop)
                elapsed_ms = int(round((time.perf_counter() - started_at) * 1000.0))
            finally:
                call_semaphore.release()
            ocr_result.metadata.setdefault("backend_latency_ms", elapsed_ms)
            _record_live_token_usage(ocr_result, stage="ocr")
            if ocr_result.metadata.get("cacheable", True):
                pipeline_cache.save_ocr_result(
                    crop,
                    ocr_result,
                    backend_name=active_backend.engine_name,
                    cache_identity=cache_identity,
                )
            else:
                block.metadata["ocr_cache_write_skipped"] = True
                block.metadata["ocr_cache_write_skipped_reason"] = "backend_failure"
            block.metadata["ocr_primary_backend"] = active_backend.engine_name
            block.metadata["ocr_cache_hit"] = False
            block.metadata["ocr_cache_miss"] = True

        active_escalation_backend = _get_escalation_backend(
            ocr_result.backend_name,
            str(ocr_result.metadata.get("model") or ""),
        )
        escalation_reason = _ocr_escalation_reason(
            ocr_result,
            threshold=escalation_threshold,
        )
        if active_escalation_backend is not None and escalation_reason is not None:
            block.metadata["ocr_escalation_attempted"] = True
            block.metadata["ocr_escalation_trigger"] = escalation_reason
            block.metadata["ocr_escalation_backend"] = active_escalation_backend.engine_name
            escalation_cache_identity = _build_ocr_cache_identity(
                prepared_page,
                block,
                backend_fingerprint=_backend_cache_fingerprint(
                    active_escalation_backend,
                    active_escalation_backend.engine_name,
                ),
                source_identity=ocr_source_identity,
            )
            escalated_cached = pipeline_cache.load_ocr_result(
                crop,
                backend_name="gemini_escalated",
                cache_identity=escalation_cache_identity,
            )
            if escalated_cached is not None:
                escalated_result = escalated_cached
                block.metadata["ocr_escalation_cache_hit"] = True
            else:
                call_semaphore.acquire()
                try:
                    escalation_started = time.perf_counter()
                    escalated_result = active_escalation_backend.recognize(crop)
                    escalation_elapsed_ms = int(
                        round((time.perf_counter() - escalation_started) * 1000.0)
                    )
                finally:
                    call_semaphore.release()
                escalated_result.metadata.setdefault(
                    "backend_latency_ms", escalation_elapsed_ms
                )
                _record_live_token_usage(
                    escalated_result,
                    stage="ocr_escalation",
                )
                if escalated_result.metadata.get("cacheable", True):
                    pipeline_cache.save_ocr_result(
                        crop,
                        escalated_result,
                        backend_name="gemini_escalated",
                        cache_identity=escalation_cache_identity,
                    )
                else:
                    block.metadata["ocr_escalation_cache_write_skipped"] = True
                    block.metadata["ocr_escalation_cache_write_skipped_reason"] = (
                        "backend_failure"
                    )
                block.metadata["ocr_escalation_cache_hit"] = False

            # Self-reported confidence did not catch the Lite model changing
            # visible notation (for example π -> \pi). For that evidence-backed
            # risk, prefer any non-empty balanced-model transcription.
            should_accept_escalation = _should_accept_ocr_escalation(
                ocr_result,
                escalated_result,
                reason=escalation_reason,
            )
            if should_accept_escalation:
                ocr_result = escalated_result
                block.metadata["ocr_escalated"] = True
                block.metadata["ocr_escalation_reason"] = escalation_reason
            else:
                block.metadata["ocr_escalated"] = False

        if token_usage_events:
            block.metadata["ai_token_usage_events"] = token_usage_events
        block.metadata["ocr_backend"] = ocr_result.backend_name
        if isinstance(ocr_result.metadata.get("backend_latency_ms"), (int, float)):
            block.metadata["ocr_latency_ms"] = int(ocr_result.metadata["backend_latency_ms"])
        block.metadata["ocr_line_count"] = int(ocr_result.metadata.get("line_count") or len(ocr_result.lines))
        block.metadata["ocr_empty_text"] = bool(ocr_result.metadata.get("empty_text")) or not bool(ocr_result.text.strip())
        block.metadata["ocr_text_length"] = int(ocr_result.metadata.get("text_length") or len((ocr_result.text or "").strip()))
        if ocr_result.metadata.get("fallback_reason"):
            block.metadata["ocr_fallback_reason"] = str(
                ocr_result.metadata["fallback_reason"]
            )
        if ocr_result.metadata.get("fallback_message"):
            block.metadata["ocr_fallback_message"] = str(
                ocr_result.metadata["fallback_message"]
            )
        if isinstance(ocr_result.metadata.get("retry_after_ms"), (int, float)):
            block.metadata["ocr_retry_after_ms"] = int(
                ocr_result.metadata["retry_after_ms"]
            )
        if ocr_result.metadata.get("circuit_open"):
            block.metadata["ocr_circuit_open"] = True
        block_type_hint = ocr_result.metadata.get("block_type_hint", "")
        if ocr_result.text.strip():
            block.text = ocr_result.text.strip()
            block.confidence = ocr_result.confidence
            block.ocr_lines = list(ocr_result.lines)
            block.style = TextStyle(
                font_size=max(10.0, block.bbox.height * 0.35),
                math_like=infer_math_like_text(block.text),
            )
            # Prefer the vision backend's block_type_hint when available;
            # otherwise infer from text.
            if block_type_hint and block_type_hint not in {"unknown", "stem"}:
                hint_map = {
                    "choice": BlockType.CHOICE,
                    "figure": BlockType.IMAGE,
                    "formula": BlockType.FORMULA,
                    "title": BlockType.TITLE,
                    "explanation": BlockType.EXPLANATION,
                }
                if block_type_hint in hint_map:
                    block.block_type = hint_map[block_type_hint]
                    block.metadata["block_type_source"] = "vision_hint"
                else:
                    inferred = classify_text_block(block.text)
                    if inferred != BlockType.STEM or block.block_type == BlockType.STEM:
                        block.block_type = inferred
            else:
                inferred = classify_text_block(block.text)
                if inferred != BlockType.STEM or block.block_type == BlockType.STEM:
                    block.block_type = inferred
        elif block_type_hint == "figure":
            block.block_type = BlockType.IMAGE
            block.metadata["fallback_reason"] = "vision_figure_hint"
        elif ocr_result.backend_name in {"none", "gemini"} and block.block_type == BlockType.STEM:
            block.block_type = BlockType.IMAGE
            block.metadata["fallback_reason"] = "noop_ocr" if ocr_result.backend_name == "none" else "gemini_no_text"

    block_ocr_started_at = time.perf_counter()
    if block_worker_count <= 1:
        for block in blocks:
            _process_block(block)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=block_worker_count) as executor:
            list(executor.map(_process_block, blocks))
    block_ocr_elapsed_ms = _elapsed_ms(block_ocr_started_at)

    ocr_cache_hit_count = sum(1 for block in blocks if block.metadata.get("ocr_cache_hit"))
    ocr_cache_miss_count = sum(1 for block in blocks if block.metadata.get("ocr_cache_miss"))
    ocr_escalation_attempted_block_count = sum(
        1 for block in blocks if block.metadata.get("ocr_escalation_attempted")
    )
    ocr_escalation_cache_hit_count = sum(
        1 for block in blocks if block.metadata.get("ocr_escalation_cache_hit")
    )
    ocr_escalated_block_count = sum(1 for block in blocks if block.metadata.get("ocr_escalated"))
    total_before_repair_ms = _elapsed_ms(recognition_started_at)
    ai_stages = {
        "ocr": {
            "stage": "ocr",
            "order": 1,
            "label": "1단계 OCR 인식",
            "status": "used" if ocr_eligible_block_count > 0 else "skipped",
            "provider": backend_name,
            "ai_powered": _is_ai_backend(backend_name),
            "eligible_block_count": ocr_eligible_block_count,
            "processed_block_count": ocr_cache_hit_count + ocr_cache_miss_count,
            "api_call_block_count": ocr_cache_miss_count,
            "cache_hit_count": ocr_cache_hit_count,
            "cache_miss_count": ocr_cache_miss_count,
            "skipped_block_count": ocr_skipped_block_count,
            "global_worker_limit": resolved_global_ocr_worker_limit,
        },
        "ocr_escalation": {
            "stage": "ocr_escalation",
            "order": 2,
            "label": "2단계 블록 보강",
            "status": "used" if ocr_escalation_attempted_block_count > 0 else "skipped",
            "provider": "gemini",
            "ai_powered": True,
            "enabled": bool(escalation_backend)
            or _gemini_escalation_may_be_available(
                ai_config=ai_config,
                primary_backend_name=backend_name,
            ),
            "attempted_block_count": ocr_escalation_attempted_block_count,
            "applied_block_count": ocr_escalated_block_count,
            "cache_hit_count": ocr_escalation_cache_hit_count,
        },
    }

    page = PageModel(
        page_id=prepared_page.page_id,
        width_px=prepared_page.image.width,
        height_px=prepared_page.image.height,
        subject=subject,
        source_path=prepared_page.source_path,
        blocks=blocks,
        metadata={
            **_page_metadata_for_output(dict(prepared_page.metadata)),
            **dict(segmented_page.metadata),
            "ocr_mode": ocr_mode,
            "pipeline_cache_dir": str(pipeline_cache.root_dir),
            "recognition_timing_ms": {
                "segmentation": segmentation_elapsed_ms,
                "block_ocr": block_ocr_elapsed_ms,
                "total_before_repair": total_before_repair_ms,
            },
            "ocr_block_worker_count": block_worker_count,
            "ocr_eligible_block_count": ocr_eligible_block_count,
            "ocr_skipped_block_count": ocr_skipped_block_count,
            "ocr_cache_hit_count": ocr_cache_hit_count,
            "ocr_cache_miss_count": ocr_cache_miss_count,
            "ocr_escalation_attempted_block_count": ocr_escalation_attempted_block_count,
            "ocr_escalation_cache_hit_count": ocr_escalation_cache_hit_count,
            "ocr_escalated_block_count": ocr_escalated_block_count,
            "ai_stages": ai_stages,
        },
    )
    return repair_page_model(
        prepared_page,
        page,
        ocr_mode=ocr_mode,
        config=ai_config,
        cache=pipeline_cache,
        request_semaphore=call_semaphore,
    )


def build_pages_from_source(
    source: str | Path,
    *,
    subject: Subject = Subject.UNKNOWN,
    ocr_mode: str = "auto",
    ai_config: AIFallbackConfig | None = None,
    pdf_dpi: int = 200,
    detect_perspective: bool = False,
    deskew: bool = True,
    crop_margins: bool = True,
    max_dimension: int | None = None,
) -> list[PageModel]:
    prepared_pages = prepare_source_pages(
        source,
        pdf_dpi=pdf_dpi,
        detect_perspective=detect_perspective,
        deskew=deskew,
        crop_margins=crop_margins,
        max_dimension=max_dimension,
    )
    return build_page_models_for_prepared_pages(
        prepared_pages,
        subject=subject,
        ocr_mode=ocr_mode,
        ai_config=ai_config,
    )


def process_source(
    source: str | Path,
    output_dir: str | Path,
    *,
    subject: Subject = Subject.UNKNOWN,
    ocr_mode: str = "auto",
    ai_config: AIFallbackConfig | None = None,
    pdf_dpi: int = 200,
    detect_perspective: bool = False,
    deskew: bool = True,
    crop_margins: bool = True,
    max_dimension: int | None = None,
    debug_segments: bool = False,
) -> list[PageModel]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prepared_pages = prepare_source_pages(
        source,
        pdf_dpi=pdf_dpi,
        detect_perspective=detect_perspective,
        deskew=deskew,
        crop_margins=crop_margins,
        max_dimension=max_dimension,
    )
    pages = build_page_models_for_prepared_pages(
        prepared_pages,
        subject=subject,
        ocr_mode=ocr_mode,
        ai_config=ai_config,
    )
    for page in pages:
        page.metadata["schema_version"] = "v0.2"
        page.metadata["ocr_mode"] = ocr_mode
        page.metadata["ai_config"] = (ai_config or AIFallbackConfig()).to_metadata()

    if debug_segments:
        debug_dir = out_dir / "debug_segments"
        for prepared_page, page in zip(prepared_pages, pages):
            debug_path = debug_dir / f"{page.page_id}_segments.png"
            draw_segment_debug(prepared_page, page.blocks, debug_path)

    save_pages_json(pages, out_dir / "pages.json")
    return pages


def main() -> int:
    parser = argparse.ArgumentParser(description="Build structured page JSON from a PDF or image source.")
    parser.add_argument("source", help="Path to a PDF or image file")
    parser.add_argument("--output-dir", default="pipeline_output", help="Directory for generated JSON and assets")
    parser.add_argument("--subject", default="unknown", help="Subject hint: math, science, korean, english, social, unknown")
    parser.add_argument("--ocr", default="auto", help="OCR backend: auto, paddleocr, tesseract, gemini, none")
    parser.add_argument("--pdf-dpi", type=int, default=200, help="PDF render DPI")
    parser.add_argument("--detect-perspective", action="store_true", help="Try perspective correction for photographed sources")
    parser.add_argument("--skip-deskew", action="store_true", help="Disable deskew")
    parser.add_argument("--skip-crop", action="store_true", help="Disable margin crop")
    parser.add_argument("--max-dimension", type=int, default=None, help="Resize long edge to this many pixels")
    parser.add_argument("--ai-fallback", default="off", help="AI fallback mode: off, auto, force")
    parser.add_argument("--ai-provider", default="gemini", help="AI fallback provider: gemini (GEMINI_API_KEY required)")
    parser.add_argument("--ai-model", default="", help="AI model override (default: gemini-3.1-pro-preview for page repair)")
    parser.add_argument("--ai-threshold", type=float, default=0.72, help="Low-confidence trigger threshold for AI fallback")
    parser.add_argument("--ai-max-tokens", type=int, default=None, help="Maximum output tokens for AI fallback")
    parser.add_argument("--ai-max-regions", type=int, default=48, help="Maximum number of blocks to send to AI fallback")
    parser.add_argument("--ai-timeout-ms", type=int, default=30000, help="Timeout in milliseconds for AI fallback requests")
    parser.add_argument("--ai-save-debug", action="store_true", help="Write AI fallback debug artifacts under .pipeline_cache/ai_debug")
    parser.add_argument("--fail-on-ai-error", action="store_true", help="Raise an error instead of silently skipping on AI fallback failures")
    parser.add_argument("--debug-segments", action="store_true", help="Save block overlay images to <output-dir>/debug_segments/ for segmentation inspection")
    args = parser.parse_args()
    ai_config = build_ai_fallback_config(
        mode=args.ai_fallback,
        provider=args.ai_provider,
        model=args.ai_model,
        threshold=args.ai_threshold,
        max_tokens=args.ai_max_tokens,
        max_regions=args.ai_max_regions,
        timeout_ms=args.ai_timeout_ms,
        save_debug=args.ai_save_debug,
        fail_on_error=args.fail_on_ai_error,
    )

    pages = process_source(
        args.source,
        args.output_dir,
        subject=_resolve_subject(args.subject),
        ocr_mode=args.ocr,
        ai_config=ai_config,
        pdf_dpi=args.pdf_dpi,
        detect_perspective=args.detect_perspective,
        deskew=not args.skip_deskew,
        crop_margins=not args.skip_crop,
        max_dimension=args.max_dimension,
        debug_segments=args.debug_segments,
    )
    run_summary = build_run_summary(
        pages,
        output_dir=args.output_dir,
        source=args.source,
        ocr_mode=args.ocr,
        ai_config=ai_config,
    )
    summary_path = Path(args.output_dir) / "run_summary.json"
    summary_path.write_text(json.dumps(run_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(run_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
