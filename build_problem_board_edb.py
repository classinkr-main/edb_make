#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import math
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from ai_usage import aggregate_token_usage, summarize_ai_cost
try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    np = None

from build_structured_page_json import (
    build_page_models_for_prepared_pages,
    resolve_global_ocr_worker_limit,
    resolve_recognition_worker_count,
)
from assemble_page import extract_set_problem_range
from segment import detect_pdf_visual_column_divider_x, draw_segment_debug
from edb_builder import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    CROP_FORMAT_V1,
    CROP_FORMAT_V2,
    DEFAULT_CROP_FORMAT,
    ImageRecordSpec,
    TextRecordSpec,
    V2_TARGET_IMAGE_WIDTH_PX,
    build_edb,
    build_image_record,
    build_preview_image_bytes,
    build_text_record,
    build_tight_crop_image_bytes,
    build_v1_secondary_image_bytes,
    header_flag_for_crop_format,
    normalize_height_px,
    normalize_width_px,
    normalize_x_px,
    normalize_y_px,
    version_string_for_crop_format,
    write_edb,
)
from layout_template_schema import LayoutTemplate, ProblemLayoutInput
from image_reconstruction_backend import clean_problem_image_transparency
from page_repair import AIFallbackConfig, build_ai_fallback_config as build_page_ai_fallback_config
from page_repair import DEFAULT_GEMINI_REPAIR_MODEL
from page_tiling import PageTilingOptions, tile_page_columns
from passage_quality import assess_passage_crop_quality
from placement_engine import place_problems
from preprocess import PreparedPage, prepare_source_pages
from structured_schema import BlockType, Box, ContentBlock, PageModel, ProblemUnit, Subject, save_pages_json


LEFT_MARGIN_PX = 84.0
TOP_PADDING_PX = 20.0
RIGHT_PADDING_PX = 54.0
PROBLEM_PADDING_PX = 18.0
MIN_HEIGHT_PAGES = 0.72
MAX_HEIGHT_PAGES = 4.8
PLACEMENT_SCALE_MIN = 0.6
PLACEMENT_SCALE_MAX = 1.6
PLACEMENT_FIT_WIDTH_SCALE_MAX = 3.0
PRESERVE_LEGACY_PLACEMENT_SCALE_KEYS = (
    "preserve_legacy_placement_scale",
    "preserveLegacyPlacementScale",
)
MIN_PROBLEM_AREA_RATIO = 0.12
DOCUMENT_BAND_TOP_PADDING_PX = 44.0
DOCUMENT_BAND_BOTTOM_PADDING_PX = 20.0
DOCUMENT_BAND_NEXT_PROBLEM_GAP_PX = 6.0
PDF_TEXT_MARKER_TOP_PADDING_PX = 0.0
PDF_TEXT_MARKER_HORIZONTAL_PADDING_PX = 44.0
PDF_TEXT_MARKER_EDGE_TOP_EXTRA_PADDING_PX = 0.0
V1_LAYOUT_MARGIN_X_PX = 24.0
V1_LAYOUT_MARGIN_Y_PX = 24.0
V1_LAYOUT_MAX_HEIGHT_PAGES = 1.08
V1_DEFAULT_DISPLAY_WIDTH_PX = 540.0
ONE_PROBLEM_SLOT_HEIGHT_PAGES = 1.2
CLASSIN_MAX_BOARD_PAGE_COUNT = 50
CHOICE_BOTTOM_SAFE_PADDING_PX = 44.0
PROBLEM_CROP_TOP_SAFE_PADDING_PX = 36
PROBLEM_CROP_BOTTOM_SAFE_PADDING_PX = 52
PASSAGE_CROP_HORIZONTAL_SAFE_PADDING_PX = 24
TEXT_PRIORITY_CROP_HORIZONTAL_SAFE_PADDING_PX = 16
TEXT_PRIORITY_SOURCE_HORIZONTAL_RECOVERY_PX = 16
TEXT_PRIORITY_SOURCE_EDGE_SCAN_DEPTH_PX = 3
TEXT_PRIORITY_SOURCE_EDGE_MIN_ACTIVE_ROWS = 8
PROBLEM_EDGE_INK_SCAN_PX = 18
PROBLEM_EDGE_INK_DARK_THRESHOLD = 236
PROBLEM_EDGE_INK_MIN_DARK_PIXELS = 6
PROBLEM_EDGE_INK_MIN_DARK_RATIO = 0.0008
PROBLEM_EDGE_TOP_EXTRA_PADDING_PX = 34.0
PROBLEM_EDGE_BOTTOM_EXTRA_PADDING_PX = 32.0
PROBLEM_CHOICE_EDGE_BOTTOM_EXTRA_PADDING_PX = 42.0
PAGE_FOOTER_CHROME_BAND_RATIO = 0.86
PAGE_FOOTER_CHROME_TEXT_MARKERS = (
    "fillthevoid",
    "윤자매",
    "저작권",
    "문제지에관한저작권",
    "한국교육과정평가원",
)
PAGE_FOOTER_CHROME_LINE_MIN_WIDTH_RATIO = 0.55
PAGE_FOOTER_CHROME_LINE_MAX_HEIGHT_PX = 14.0
PAGE_FOOTER_CHROME_SCAN_PX = 180
PAGE_FOOTER_CHROME_LINE_DARK_THRESHOLD = 96
PAGE_FOOTER_CHROME_LINE_MIN_DARK_RATIO = 0.52
PAGE_FOOTER_CHROME_MIN_GAP_FROM_CONTENT_PX = 18.0
PAGE_FOOTER_CHROME_TRIM_ABOVE_LINE_PX = 36.0
PAGE_FOOTER_CHROME_CONTENT_PADDING_PX = 14.0
PAGE_SIDE_CHROME_TEXT_MARKERS = (
    "과학탐구",
    "사회탐구",
    "지구과학",
    "생명과학",
    "물리학",
    "화학",
)
PROCESSING_STEP_RAW = "raw"
PROCESSING_STEP_ORIGINAL = "s1"
PROCESSING_STEP_CHALK = "s2"
PROCESSING_STEP_RECONSTRUCT = "s3"
HWP_TEXT_FALLBACK_RISK_FLAG = "hwp_text_fallback_problem"
HWP_TEXT_FALLBACK_CARD_WIDTH_PX = 1200
HWP_TEXT_FALLBACK_CARD_MIN_HEIGHT_PX = 480
HWP_TEXT_FALLBACK_CARD_MAX_HEIGHT_PX = 2200
PROCESSING_STEPS = {
    PROCESSING_STEP_RAW,
    PROCESSING_STEP_ORIGINAL,
    PROCESSING_STEP_CHALK,
    PROCESSING_STEP_RECONSTRUCT,
}
CLASSIN_PREFLIGHT_MIN_IMAGE_WIDTH_PX = 240
CLASSIN_PREFLIGHT_MIN_IMAGE_HEIGHT_PX = 120
CLASSIN_PREFLIGHT_INK_THRESHOLD = 245
CLASSIN_PREFLIGHT_MIN_DARK_PIXEL_RATIO = 0.002
CLASSIN_PREFLIGHT_PLACEMENT_OVERLAP_TOLERANCE_PAGES = 0.01
CLASSIN_PREFLIGHT_SOURCE_BBOX_OVERLAP_RATIO = 0.65
CLASSIN_PREFLIGHT_PASSAGE_SOURCE_REUSE_RATIO = 0.65
CLASSIN_PREFLIGHT_STEP2_PAGE_CHROME_MAX_RATIO = 0.10
CLASSIN_PREFLIGHT_MAX_ISSUES = 50
CLASSIN_PREFLIGHT_NON_ACTIONABLE_REVIEW_RISK_FLAGS = {
    "fallback_grouping",
    "marker_document_continuation",
}
CLASSIN_PREFLIGHT_ISSUE_LABELS = {
    "board_placement_overlap": "판서 배치 겹침",
    "duplicate_problem_number": "중복 번호",
    "horizontal_crop_edge_risk": "좌우 원본 경계 확인",
    "low_ink_problem_image": "이미지 내용 부족",
    "missing_problem_image": "문항 이미지 없음",
    "passage_group_source_reuse": "지문 겹침",
    "passage_missing_child_questions": "문항 누락",
    "passage_quality_review": "지문 품질 확인",
    "passage_review_queue_remaining": "지문 확인 필요",
    "review_flags_remaining": "검수 플래그 남음",
    "small_problem_image": "문항 이미지 작음",
    "source_problem_bbox_overlap": "문항 영역 겹침",
    "step2_page_chrome_artifact_rate": "2단계 페이지 장식 허용률 초과",
    "step3_page_chrome_artifact": "3단계 페이지 장식",
    "unreadable_problem_image": "문항 이미지 흐림",
}
PASSAGE_CROSS_PAGE_MERGE_CHECK_RISK_FLAG = "passage_cross_page_merge_check"
PASSAGE_FRAGMENT_STITCH_GAP_PX = 16
PASSAGE_SOURCE_HORIZONTAL_RECOVERY_PX = 24
PASSAGE_SOURCE_INNER_EDGE_RECOVERY_PX = 64
PASSAGE_SOURCE_VERTICAL_RECOVERY_PX = 12
PASSAGE_CENTER_DIVIDER_EXCLUSION_PX = 6
PASSAGE_JOIN_BLANK_RUN_MIN_PX = 40
PASSAGE_JOIN_EDGE_PADDING_PX = 16
PASSAGE_JOIN_CONTENT_PADDING_PX = 8
PASSAGE_JOIN_EXCESS_BLANK_MIN_PX = 24
# Crop and cutout PNGs are temporary export assets. A moderate compression
# level keeps them pixel-identical while avoiding Pillow's slower default
# encoder setting on long Korean passage images.
# Runtime artifacts are short-lived and wrapped by the final EDB container.
# Level 3 keeps their encode cost moderate without the large file-size
# inflation observed at level 1 on dense, high-resolution exam pages.
INTERMEDIATE_PNG_COMPRESSION_LEVEL = 3
PASSAGE_JOIN_TOP_RULE_MAX_RATIO = 0.32
PASSAGE_JOIN_FOOTER_BLANK_MIN_START_RATIO = 0.45
PASSAGE_JOIN_FOOTER_CONTENT_MIN_WIDTH_RATIO = 0.25
CONTINUOUS_RECORD_GAP_PX = 20.0
PASSAGE_REVIEW_REASON_LABELS = {
    "cross_page_passage_group": "페이지 이어짐",
    "hwp_text_fallback_problem": "HWP 텍스트 fallback",
    "marker_document_continuation": "문서 이어짐 표시",
    "passage_cross_page_merge_check": "병합 확인",
    "passage_fragment": "지문 본문",
    "passage_group_source_reuse": "지문 겹침",
    "passage_missing_child_questions": "문항 누락",
    "passage_quality_review": "지문 품질 확인",
    "source_problem_bbox_overlap": "문항 영역 겹침",
}
RECONSTRUCT_TARGET_MIN_WIDTH_PX = 1600
RECONSTRUCT_MAX_UPSCALE = 3.5
TEXT_PRIORITY_SUBJECTS = {"korean", "english", "social"}
HIGH_FIDELITY_IMAGE_SUBJECTS = TEXT_PRIORITY_SUBJECTS | {"math", "science"}
UNKNOWN_SUBJECT_VALUES = {"", "unknown", "none", "auto"}
MATH_SOURCE_HINT_MARKERS = (
    "\uc218\ud559",
    "\uc218\ub9ac",
    "\ubbf8\uc801\ubd84",
    "\ud655\ub960\uacfc\ud1b5\uacc4",
    "\ud655\ub960\uacfc \ud1b5\uacc4",
    "\uae30\ud558",
    "\ub300\uc218",
    "calculus",
    "geometry",
    "algebra",
)
MATH_SOURCE_HINT_RE = re.compile(r"(?<![a-z])math(?:ematics)?(?![a-z])", re.IGNORECASE)
TEXT_DEHALO_ALPHA_CUTOFF = 12
TEXT_PRIORITY_UNSHARP_RADIUS = 0.6
TEXT_PRIORITY_UNSHARP_PERCENT = 70
TEXT_PRIORITY_UNSHARP_THRESHOLD = 3
V2_ENCODED_IMAGE_MIN_WIDTH_PX = 1024
# Preserve ordinary 200/300-DPI source pages instead of shrinking them to a
# fixed export tier. Very large scans are still bounded so ClassIn remains
# responsive on tablets.
V2_ENCODED_IMAGE_MAX_WIDTH_PX = 4096
V2_ENCODED_IMAGE_MAX_EDGE_PX = 4096
V2_ENCODED_IMAGE_OVERSAMPLE = 2.5
V2_ENCODED_IMAGE_MAX_PIXELS = 12_000_000
DEFAULT_IMAGE_RECORD_JPEG_QUALITY = 92
TEXT_PRIORITY_IMAGE_RECORD_JPEG_QUALITY = 95
# Brightness above this value (0-255) is treated as a light background that
# should be removed from the exported problem image.
DARK_BOARD_BRIGHTNESS_THRESHOLD = 160
DEFAULT_BOARD_THEME = "charcoal"
BOARD_THEME_PALETTES: dict[str, dict[str, tuple[int, int, int]]] = {
    "black": {
        "background": (10, 10, 12),
        "chalk": (250, 250, 248),
    },
    "charcoal": {
        "background": (24, 28, 32),
        "chalk": (248, 249, 246),
    },
    "green": {
        "background": (18, 42, 36),
        "chalk": (244, 248, 241),
    },
}


def _clamp_placement_x_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _clamp_placement_y_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def _clamp_placement_scale_ratio(value: float | None, max_ratio: float = PLACEMENT_SCALE_MAX) -> float | None:
    if value is None:
        return None
    resolved_max = max(PLACEMENT_SCALE_MIN, float(max_ratio))
    return max(PLACEMENT_SCALE_MIN, min(resolved_max, float(value)))


def _mapping_preserves_legacy_placement_scale(value: Mapping[str, Any] | None) -> bool:
    if not isinstance(value, Mapping):
        return False
    nested_metadata = value.get("metadata")
    sources = (value, nested_metadata if isinstance(nested_metadata, Mapping) else {})
    for source in sources:
        for key in PRESERVE_LEGACY_PLACEMENT_SCALE_KEYS:
            marker = source.get(key)
            if isinstance(marker, bool):
                return marker
            if isinstance(marker, (int, float)) and marker == 1:
                return True
            if isinstance(marker, str) and marker.strip().lower() in {"1", "true", "yes", "on"}:
                return True
    return False


def _entry_preserves_legacy_placement_scale(entry: "ProblemEntry") -> bool:
    return bool(entry.preserve_legacy_placement_scale) and not _entry_uses_continuous_page_flow(entry)


def _problem_origin_x_px(entry: "ProblemEntry", rendered_width_px: float) -> float:
    ratio = _clamp_placement_x_ratio(entry.placement_x_ratio)
    if ratio is None:
        return LEFT_MARGIN_PX
    max_x_px = max(LEFT_MARGIN_PX, CANVAS_HEIGHT - RIGHT_PADDING_PX - rendered_width_px)
    return LEFT_MARGIN_PX + ratio * (max_x_px - LEFT_MARGIN_PX)


def _problem_origin_y_px(entry: "ProblemEntry", placement: "ProblemPlacement", rendered_height_px: float) -> float:
    base_y_px = placement.start_y_pages * CANVAS_WIDTH + TOP_PADDING_PX
    ratio = _clamp_placement_y_ratio(entry.placement_y_ratio)
    if ratio is None:
        return base_y_px
    slot_bottom_y_px = placement.snapped_next_start_y_pages * CANVAS_WIDTH
    max_y_px = max(base_y_px, slot_bottom_y_px - rendered_height_px)
    return base_y_px + ratio * (max_y_px - base_y_px)


def _problem_scale_ratio(
    entry: "ProblemEntry",
    placement: "ProblemPlacement",
    rendered_width_px: float,
    rendered_height_px: float,
    *,
    ignore_height_limit: bool = False,
) -> float:
    allowed_max = (
        PLACEMENT_FIT_WIDTH_SCALE_MAX
        if ignore_height_limit or _entry_preserves_legacy_placement_scale(entry)
        else PLACEMENT_SCALE_MAX
    )
    fit_to_slot = _placement_fit_to_slot_scale(placement)
    requested = _clamp_placement_scale_ratio(entry.placement_scale_ratio, allowed_max)
    if requested is None:
        if fit_to_slot is None:
            return 1.0
        requested = 1.0
    max_width_scale = (CANVAS_HEIGHT - LEFT_MARGIN_PX - RIGHT_PADDING_PX) / max(rendered_width_px, 1.0)
    if ignore_height_limit:
        max_scale = max(PLACEMENT_SCALE_MIN, min(PLACEMENT_FIT_WIDTH_SCALE_MAX, max_width_scale))
        return max(PLACEMENT_SCALE_MIN, min(max_scale, requested))
    slot_span_px = (placement.snapped_next_start_y_pages - placement.start_y_pages) * CANVAS_WIDTH
    if fit_to_slot is not None:
        # The placement reserved a single slot for content that is slightly
        # taller than one visible page; shrink the render so it truly fits
        # (including the top padding offset) instead of bleeding into the
        # next problem's page.
        slot_height_px = max(1.0, slot_span_px - TOP_PADDING_PX)
    else:
        slot_height_px = max(rendered_height_px, slot_span_px)
    max_height_scale = slot_height_px / max(rendered_height_px, 1.0)
    max_scale = max(
        PLACEMENT_SCALE_MIN,
        min(allowed_max, max_width_scale, max_height_scale),
    )
    return max(PLACEMENT_SCALE_MIN, min(max_scale, requested))


def _placement_fit_to_slot_scale(placement: "ProblemPlacement") -> float | None:
    metadata = getattr(placement, "metadata", None) or {}
    for key in ("fit_to_slot_scale", "fitToSlotScale"):
        value = metadata.get(key)
        try:
            if value is not None and 0.0 < float(value) <= 1.0:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _entry_uses_continuous_page_flow(entry: "ProblemEntry") -> bool:
    return _normalize_input_intent(entry.input_intent) == "page-as-is"


def _resolve_board_theme(board_theme: str | None) -> str:
    normalized = (board_theme or "").strip().lower()
    if normalized in BOARD_THEME_PALETTES:
        return normalized
    return DEFAULT_BOARD_THEME


def _resolve_chalk_color(board_theme: str | None) -> tuple[int, int, int]:
    """Return the chalk RGB for ``board_theme``. ClassIn paints its own dark
    chalkboard under transparent PNGs, so the only color we need to bake into
    each problem image is this chalk tone."""
    return BOARD_THEME_PALETTES[_resolve_board_theme(board_theme)]["chalk"]


def _normalize_processing_step(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in PROCESSING_STEPS else PROCESSING_STEP_RAW


def _default_processing_step_for_problem(problem: ProblemUnit) -> str:
    raw_step = (
        problem.metadata.get("processing_step")
        or problem.metadata.get("processingStep")
        or problem.metadata.get("step")
    )
    if raw_step:
        return _normalize_processing_step(raw_step)
    if _is_page_as_is_problem(problem):
        return PROCESSING_STEP_CHALK
    return PROCESSING_STEP_RAW


def _is_page_as_is_problem(problem: ProblemUnit) -> bool:
    raw_intent = problem.metadata.get("input_intent") or problem.metadata.get("inputIntent")
    return _normalize_input_intent(str(raw_intent or "")) == "page-as-is"


def _default_placement_scale_for_problem(problem: ProblemUnit) -> float | None:
    raw_scale = (
        problem.metadata.get("placement_scale_ratio")
        or problem.metadata.get("placementScaleRatio")
        or problem.metadata.get("scaleRatio")
    )
    if raw_scale is not None:
        try:
            max_ratio = (
                PLACEMENT_FIT_WIDTH_SCALE_MAX
                if _is_page_as_is_problem(problem)
                or _mapping_preserves_legacy_placement_scale(problem.metadata)
                else PLACEMENT_SCALE_MAX
            )
            return _clamp_placement_scale_ratio(float(raw_scale), max_ratio)
        except (TypeError, ValueError):
            return None
    if _is_page_as_is_problem(problem):
        return PLACEMENT_FIT_WIDTH_SCALE_MAX
    return None


# Minimum width (px) for a problem crop before chalk rendering. Smaller crops
# get upscaled with LANCZOS so OCR and the dark-board composite have enough
# pixel-detail to render legibly. Chosen empirically: 1024 px wide is roughly
# the width of a printed Korean exam problem at 200 DPI.
PROBLEM_CROP_TARGET_MIN_WIDTH_PX = 1024
PROBLEM_CROP_MAX_UPSCALE = 2.6
EDGE_GUIDE_SCAN_RATIO = 0.16
EDGE_GUIDE_SCAN_MAX_PX = 120
EDGE_GUIDE_DARK_THRESHOLD = 200
EDGE_GUIDE_MIN_COLUMN_RATIO = 0.55
EDGE_GUIDE_CLUSTER_MIN_COLUMN_RATIO = 0.035
EDGE_GUIDE_CLUSTER_MIN_COVERAGE_RATIO = 0.55
EDGE_GUIDE_CLUSTER_MAX_WIDTH_PX = 24
EDGE_GUIDE_CLUSTER_GAP_PX = 2
EDGE_GUIDE_TRIM_PADDING_PX = 4
SIDE_PAGE_CHROME_SCAN_RATIO = 0.24
SIDE_PAGE_CHROME_SCAN_MAX_PX = 180
SIDE_PAGE_CHROME_TAB_MAX_WIDTH_RATIO = 0.16
SIDE_PAGE_CHROME_TAB_MAX_HEIGHT_RATIO = 0.48
SIDE_PAGE_CHROME_TAB_MIN_HEIGHT_RATIO = 0.08
SIDE_PAGE_CHROME_TRIM_PADDING_PX = 6
BOTTOM_WATERMARK_SCAN_RATIO = 0.22
BOTTOM_WATERMARK_MIN_Y_RATIO = 0.82
BOTTOM_WATERMARK_BLUE_DELTA = 22
BOTTOM_WATERMARK_TRIM_PADDING_PX = 10
CORNER_PAGE_BADGE_SCAN_RATIO = 0.18
CORNER_PAGE_BADGE_SCAN_MAX_PX = 180
CORNER_PAGE_BADGE_EDGE_SEED_PX = 10
CORNER_PAGE_BADGE_ERASE_PADDING_PX = 8


def _trim_edge_vertical_guides(image: Image.Image) -> Image.Image:
    """Remove page/column guide lines that hug the left or right crop edge.

    We only trim nearly full-height dark vertical strokes inside the outer
    edge band so internal diagram axes and graph lines stay intact.
    """
    width, height = image.size
    if width <= 12 or height <= 12:
        return image

    gray = image.convert("L")
    if ImageStat.Stat(gray).mean[0] <= DARK_BOARD_BRIGHTNESS_THRESHOLD:
        return image

    pixels = gray.load()
    scan_width = min(int(round(width * EDGE_GUIDE_SCAN_RATIO)), EDGE_GUIDE_SCAN_MAX_PX)
    if scan_width <= 0:
        return image

    min_dark_pixels = int(round(height * EDGE_GUIDE_MIN_COLUMN_RATIO))
    min_cluster_column_pixels = max(4, int(round(height * EDGE_GUIDE_CLUSTER_MIN_COLUMN_RATIO)))
    min_cluster_coverage = int(round(height * EDGE_GUIDE_CLUSTER_MIN_COVERAGE_RATIO))

    def is_guide_column(x: int) -> bool:
        dark_count = 0
        for y in range(height):
            if pixels[x, y] <= EDGE_GUIDE_DARK_THRESHOLD:
                dark_count += 1
        return dark_count >= min_dark_pixels

    def find_slanted_guide_cluster(x_values: range) -> tuple[int, int] | None:
        candidates: list[tuple[int, int, set[int]]] = []
        for x in x_values:
            dark_rows: set[int] = set()
            for y in range(height):
                if pixels[x, y] <= EDGE_GUIDE_DARK_THRESHOLD:
                    dark_rows.add(y)
            if len(dark_rows) >= min_cluster_column_pixels:
                candidates.append((x, len(dark_rows), dark_rows))

        clusters: list[list[tuple[int, int, set[int]]]] = []
        current: list[tuple[int, int, set[int]]] = []
        for candidate in candidates:
            if current and candidate[0] > current[-1][0] + EDGE_GUIDE_CLUSTER_GAP_PX + 1:
                clusters.append(current)
                current = []
            current.append(candidate)
        if current:
            clusters.append(current)

        valid_clusters: list[tuple[int, int]] = []
        for cluster in clusters:
            start_x = min(item[0] for item in cluster)
            end_x = max(item[0] for item in cluster)
            if end_x - start_x + 1 > EDGE_GUIDE_CLUSTER_MAX_WIDTH_PX:
                continue
            covered_rows: set[int] = set()
            for item in cluster:
                covered_rows.update(item[2])
            if len(covered_rows) >= min_cluster_coverage:
                valid_clusters.append((start_x, end_x))

        if not valid_clusters:
            return None
        if x_values.start <= 0:
            return min(valid_clusters, key=lambda item: item[0])
        return max(valid_clusters, key=lambda item: item[1])

    left_trim = 0
    for x in range(scan_width):
        if is_guide_column(x):
            left_trim = max(left_trim, x + EDGE_GUIDE_TRIM_PADDING_PX + 1)
    left_cluster = find_slanted_guide_cluster(range(scan_width))
    if left_cluster is not None:
        left_trim = max(left_trim, left_cluster[1] + EDGE_GUIDE_TRIM_PADDING_PX + 1)

    right_trim = width
    for x in range(width - scan_width, width):
        if is_guide_column(x):
            right_trim = min(right_trim, x - EDGE_GUIDE_TRIM_PADDING_PX)
    right_cluster = find_slanted_guide_cluster(range(width - scan_width, width))
    if right_cluster is not None:
        right_trim = min(right_trim, right_cluster[0] - EDGE_GUIDE_TRIM_PADDING_PX)

    if left_trim <= 0 and right_trim >= width:
        return image
    if right_trim - left_trim < width * 0.75:
        return image
    return image.crop((max(0, left_trim), 0, min(width, right_trim), height))


def _trim_edge_attached_page_chrome(image: Image.Image) -> Image.Image:
    """Trim side chrome such as subject tabs attached to the crop edge."""
    width, height = image.size
    if width <= 80 or height <= 80:
        return image

    rgb = image.convert("RGB")
    if np is not None:
        arr = np.asarray(rgb, dtype=np.uint8)
        rgb_float = arr.astype(np.float32)
        luminance = (
            0.299 * rgb_float[..., 0]
            + 0.587 * rgb_float[..., 1]
            + 0.114 * rgb_float[..., 2]
        )
        saturation = rgb_float.max(axis=2) - rgb_float.min(axis=2)
        foreground = (luminance <= 246.0) | (saturation >= 24.0)

        def is_foreground(x: int, y: int) -> bool:
            return bool(foreground[y, x])

        def new_visited() -> Any:
            return np.zeros((height, scan_width), dtype=bool)

        def visited_get(visited: Any, y: int, x: int) -> bool:
            return bool(visited[y, x])

        def visited_set(visited: Any, y: int, x: int) -> None:
            visited[y, x] = True
    else:
        pixels = rgb.load()

        def is_foreground(x: int, y: int) -> bool:
            red, green, blue = pixels[x, y]
            luminance = 0.299 * red + 0.587 * green + 0.114 * blue
            saturation = max(red, green, blue) - min(red, green, blue)
            return luminance <= 246.0 or saturation >= 24.0

        def new_visited() -> Any:
            return [[False] * scan_width for _ in range(height)]

        def visited_get(visited: Any, y: int, x: int) -> bool:
            return bool(visited[y][x])

        def visited_set(visited: Any, y: int, x: int) -> None:
            visited[y][x] = True

    scan_width = min(
        width,
        max(32, min(SIDE_PAGE_CHROME_SCAN_MAX_PX, int(round(width * SIDE_PAGE_CHROME_SCAN_RATIO)))),
    )
    min_tab_height = max(36, int(round(height * SIDE_PAGE_CHROME_TAB_MIN_HEIGHT_RATIO)))
    max_tab_height = max(min_tab_height, int(round(height * SIDE_PAGE_CHROME_TAB_MAX_HEIGHT_RATIO)))
    max_tab_width = max(18, int(round(width * SIDE_PAGE_CHROME_TAB_MAX_WIDTH_RATIO)))
    max_line_width = max(8, int(round(width * 0.018)))

    def edge_components(side: str) -> list[tuple[int, int, int, int, int]]:
        seed_width = max(4, min(scan_width, max(20, int(round(width * 0.035)))))
        if side == "left":
            x_min, x_max = 0, scan_width
            edge_columns = range(0, min(seed_width, scan_width))
        else:
            x_min, x_max = width - scan_width, width
            edge_columns = range(max(width - seed_width, x_min), width)

        visited = new_visited()
        components: list[tuple[int, int, int, int, int]] = []
        for y in range(height):
            for x in edge_columns:
                if not is_foreground(x, y):
                    continue
                local_x = x - x_min
                if local_x < 0 or local_x >= scan_width or visited_get(visited, y, local_x):
                    continue
                stack = [(x, y)]
                visited_set(visited, y, local_x)
                min_x = max_x = x
                min_y = max_y = y
                count = 0
                while stack:
                    cx, cy = stack.pop()
                    count += 1
                    min_x = min(min_x, cx)
                    max_x = max(max_x, cx)
                    min_y = min(min_y, cy)
                    max_y = max(max_y, cy)
                    for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                        if nx < x_min or nx >= x_max or ny < 0 or ny >= height:
                            continue
                        lx = nx - x_min
                        if visited_get(visited, ny, lx) or not is_foreground(nx, ny):
                            continue
                        visited_set(visited, ny, lx)
                        stack.append((nx, ny))
                components.append((min_x, min_y, max_x, max_y, count))
        return components

    left_trim = 0
    right_trim = width
    for side in ("left", "right"):
        for min_x, min_y, max_x, max_y, count in edge_components(side):
            component_width = max_x - min_x + 1
            component_height = max_y - min_y + 1
            thin_edge_line = component_width <= max_line_width and component_height >= height * 0.45
            side_tab = (
                component_width <= max_tab_width
                and min_tab_height <= component_height <= max_tab_height
                and count >= max(24, int(round(width * height * 0.001)))
            )
            if not (thin_edge_line or side_tab):
                continue
            if side == "left":
                left_trim = max(left_trim, max_x + SIDE_PAGE_CHROME_TRIM_PADDING_PX + 1)
            else:
                right_trim = min(right_trim, min_x - SIDE_PAGE_CHROME_TRIM_PADDING_PX)

    if left_trim <= 0 and right_trim >= width:
        return image
    if right_trim - left_trim < width * 0.70:
        return image
    return image.crop((max(0, left_trim), 0, min(width, right_trim), height))


def _trim_bottom_blue_watermark(image: Image.Image) -> Image.Image:
    """Remove the blue 평가원 copyright footer when it is outside the problem."""
    width, height = image.size
    if width <= 80 or height <= 120:
        return image

    rgb = image.convert("RGB")
    scan_top = int(round(height * (1.0 - BOTTOM_WATERMARK_SCAN_RATIO)))
    scan_top = max(scan_top, int(round(height * BOTTOM_WATERMARK_MIN_Y_RATIO)))
    if scan_top >= height - 2:
        return image
    if np is not None:
        arr = np.asarray(rgb, dtype=np.int16)
        lower = arr[scan_top:, :, :]
        red = lower[..., 0]
        green = lower[..., 1]
        blue = lower[..., 2]
        saturation = lower.max(axis=2) - lower.min(axis=2)
        blue_mask = (
            (blue >= red + BOTTOM_WATERMARK_BLUE_DELTA)
            & (blue >= green + 8)
            & (saturation >= 32)
        )
        if int(np.count_nonzero(blue_mask)) < max(18, int(round(width * height * 0.00035))):
            return image
        rows = np.where(np.count_nonzero(blue_mask, axis=1) >= max(4, int(round(width * 0.006))))[0]
        if rows.size == 0:
            return image
        first_y = scan_top + int(rows.min())
    else:
        pixels = rgb.load()
        min_row_blue_count = max(4, int(round(width * 0.006)))
        total_blue_count = 0
        first_y: int | None = None
        for y in range(scan_top, height):
            row_blue_count = 0
            for x in range(width):
                red, green, blue = pixels[x, y]
                saturation = max(red, green, blue) - min(red, green, blue)
                if (
                    blue >= red + BOTTOM_WATERMARK_BLUE_DELTA
                    and blue >= green + 8
                    and saturation >= 32
                ):
                    row_blue_count += 1
            total_blue_count += row_blue_count
            if first_y is None and row_blue_count >= min_row_blue_count:
                first_y = y
        if total_blue_count < max(18, int(round(width * height * 0.00035))) or first_y is None:
            return image
    if first_y < int(round(height * BOTTOM_WATERMARK_MIN_Y_RATIO)):
        return image
    target_bottom = max(1, first_y - BOTTOM_WATERMARK_TRIM_PADDING_PX)
    if target_bottom >= height - 4 or target_bottom <= height * 0.72:
        return image
    return image.crop((0, 0, width, target_bottom))


def _trim_text_priority_bottom_page_badge(image: Image.Image) -> Image.Image:
    """Trim an isolated bottom-corner page badge without touching edge glyphs."""
    width, height = image.size
    if width <= 120 or height <= 160:
        return image

    gray = image.convert("L")
    if np is not None:
        foreground = np.asarray(gray, dtype=np.uint8) <= 246
        row_counts = np.count_nonzero(foreground, axis=1)
        active_rows = [int(value) for value in np.flatnonzero(row_counts > 3)]
    else:
        pixels = gray.load()
        foreground = None
        active_rows = [
            y
            for y in range(height)
            if sum(1 for x in range(width) if int(pixels[x, y]) <= 246) > 3
        ]
    if not active_rows:
        return image

    runs: list[tuple[int, int]] = []
    run_start = previous = active_rows[0]
    for y in active_rows[1:]:
        if y > previous + 1:
            runs.append((run_start, previous + 1))
            run_start = y
        previous = y
    runs.append((run_start, previous + 1))

    grouped_runs: list[tuple[int, int]] = []
    for start, end in runs:
        if grouped_runs and start - grouped_runs[-1][1] <= 20:
            grouped_runs[-1] = (grouped_runs[-1][0], end)
        else:
            grouped_runs.append((start, end))
    if len(grouped_runs) < 2:
        return image

    badge_start, badge_end = grouped_runs[-1]
    content_end = grouped_runs[-2][1]
    gap = badge_start - content_end
    if (
        badge_start < int(round(height * 0.45))
        # Official listening-test page badges can occupy just over one fifth
        # of a short question crop. The corner, narrowness, and isolation
        # checks below still distinguish them from ordinary answer content.
        or badge_end - badge_start > int(round(height * 0.24))
        or gap < max(20, int(round(height * 0.02)))
    ):
        return image

    if foreground is not None:
        ys, xs = np.where(foreground[badge_start:badge_end, :])
        if xs.size == 0:
            return image
        badge_left = int(xs.min())
        badge_right = int(xs.max()) + 1
    else:
        badge_xs = [
            x
            for y in range(badge_start, badge_end)
            for x in range(width)
            if int(pixels[x, y]) <= 246
        ]
        if not badge_xs:
            return image
        badge_left = min(badge_xs)
        badge_right = max(badge_xs) + 1

    corner_inset = max(2, TEXT_PRIORITY_CROP_HORIZONTAL_SAFE_PADDING_PX + 2)
    touches_corner = (
        badge_left <= corner_inset
        or badge_right >= width - corner_inset
    )
    badge_width = badge_right - badge_left
    badge_height = badge_end - badge_start
    narrow = badge_width <= max(72, int(round(width * 0.20)))
    badge_sized = badge_width >= 24 and badge_height >= 24
    if not touches_corner or not narrow or not badge_sized:
        return image

    target_bottom = max(content_end + 12, badge_start - 8)
    if target_bottom <= 0 or target_bottom >= height - 4:
        return image
    return image.crop((0, 0, width, target_bottom))


def _erase_text_priority_unpaired_outer_vertical_guide(image: Image.Image) -> Image.Image:
    """Erase one long outer column guide while preserving crop dimensions.

    Paired left/right rules are treated as a real table or passage frame and
    retained. This targets only a lone PDF column separator outside reading
    content, avoiding the horizontal crop that previously clipped glyphs.
    """
    width, height = image.size
    if width <= 120 or height <= 160:
        return image
    gray = image.convert("L")
    if ImageStat.Stat(gray).mean[0] <= DARK_BOARD_BRIGHTNESS_THRESHOLD:
        return image
    if np is not None:
        foreground = np.asarray(gray, dtype=np.uint8) <= 246
        column_counts = foreground.sum(axis=0)
    else:
        pixels = gray.load()
        foreground = [
            [int(pixels[x, y]) <= 246 for x in range(width)]
            for y in range(height)
        ]
        column_counts = [
            sum(int(foreground[y][x]) for y in range(height))
            for x in range(width)
        ]

    scan_width = min(100, max(24, int(round(width * 0.10))))
    minimum_ink = max(48, int(round(height * 0.60)))

    def clusters(x_values: range) -> list[tuple[int, int]]:
        candidates = [
            x for x in x_values
            if int(column_counts[x]) >= minimum_ink
        ]
        if not candidates:
            return []
        grouped: list[tuple[int, int]] = []
        start = previous = candidates[0]
        for x in candidates[1:]:
            if x > previous + 2:
                grouped.append((start, previous))
                start = x
            previous = x
        grouped.append((start, previous))
        return [
            cluster
            for cluster in grouped
            if cluster[1] - cluster[0] + 1 <= 8
        ]

    left_clusters = clusters(range(scan_width))
    right_clusters = clusters(range(width - scan_width, width))
    # Strong rules on both sides are a legitimate boxed frame.
    if left_clusters and right_clusters:
        return image
    target_clusters = left_clusters or right_clusters
    if len(target_clusters) != 1:
        return image

    start, end = target_clusters[0]
    erase_left = max(0, start - 2)
    erase_right = min(width, end + 3)
    mode = image.mode
    output = image.convert("RGBA")
    output_pixels = output.load()
    source_pixels = image.convert("RGBA").load()
    fill = (255, 255, 255, 0) if "A" in image.getbands() else (255, 255, 255, 255)
    for y in range(height):
        for x in range(erase_left, erase_right):
            red, green, blue, alpha = source_pixels[x, y]
            luminance = 0.299 * red + 0.587 * green + 0.114 * blue
            saturation = max(red, green, blue) - min(red, green, blue)
            if alpha > 24 and (luminance <= 246 or saturation >= 24):
                output_pixels[x, y] = fill
    return output if "A" in image.getbands() else output.convert(mode)


def _erase_corner_page_badges(image: Image.Image) -> Image.Image:
    """Erase small page-number badges glued to the crop corners."""
    width, height = image.size
    if width <= 80 or height <= 80:
        return image

    mode = image.mode
    rgba = image.convert("RGBA")
    if np is not None:
        arr = np.asarray(rgba, dtype=np.uint8)
        rgb_float = arr[..., :3].astype(np.float32)
        luminance = (
            0.299 * rgb_float[..., 0]
            + 0.587 * rgb_float[..., 1]
            + 0.114 * rgb_float[..., 2]
        )
        saturation = rgb_float.max(axis=2) - rgb_float.min(axis=2)
        alpha = arr[..., 3]
        foreground = (alpha > 24) & ((luminance <= 246.0) | (saturation >= 24.0))

        def is_foreground(x: int, y: int) -> bool:
            return bool(foreground[y, x])

        def new_visited() -> Any:
            return np.zeros((roi_height, roi_width), dtype=bool)

        def visited_get(visited: Any, y: int, x: int) -> bool:
            return bool(visited[y, x])

        def visited_set(visited: Any, y: int, x: int) -> None:
            visited[y, x] = True
    else:
        pixels = rgba.load()

        def is_foreground(x: int, y: int) -> bool:
            red, green, blue, alpha = pixels[x, y]
            luminance = 0.299 * red + 0.587 * green + 0.114 * blue
            saturation = max(red, green, blue) - min(red, green, blue)
            return alpha > 24 and (luminance <= 246.0 or saturation >= 24.0)

        def new_visited() -> Any:
            return [[False] * roi_width for _ in range(roi_height)]

        def visited_get(visited: Any, y: int, x: int) -> bool:
            return bool(visited[y][x])

        def visited_set(visited: Any, y: int, x: int) -> None:
            visited[y][x] = True

    roi_width = min(width, max(48, min(CORNER_PAGE_BADGE_SCAN_MAX_PX, int(round(width * CORNER_PAGE_BADGE_SCAN_RATIO)))))
    roi_height = min(height, max(48, min(CORNER_PAGE_BADGE_SCAN_MAX_PX, int(round(height * CORNER_PAGE_BADGE_SCAN_RATIO)))))
    seed_px = max(3, min(CORNER_PAGE_BADGE_EDGE_SEED_PX, roi_width, roi_height))
    fill = (255, 255, 255, 0) if "A" in image.getbands() else (255, 255, 255, 255)
    output = rgba.copy()

    corner_specs = (
        ("left", "bottom", 0, height - roi_height),
        ("right", "bottom", width - roi_width, height - roi_height),
        ("left", "top", 0, 0),
        ("right", "top", width - roi_width, 0),
    )

    for horizontal, vertical, left, top in corner_specs:
        visited = new_visited()
        seeds: list[tuple[int, int]] = []
        x_edge = range(0, seed_px) if horizontal == "left" else range(roi_width - seed_px, roi_width)
        y_edge = range(roi_height - seed_px, roi_height) if vertical == "bottom" else range(0, seed_px)
        for y in range(roi_height):
            for x in x_edge:
                if is_foreground(left + x, top + y):
                    seeds.append((x, y))
        for y in y_edge:
            for x in range(roi_width):
                if is_foreground(left + x, top + y):
                    seeds.append((x, y))

        for seed_x, seed_y in seeds:
            if visited_get(visited, seed_y, seed_x) or not is_foreground(left + seed_x, top + seed_y):
                continue
            stack = [(seed_x, seed_y)]
            visited_set(visited, seed_y, seed_x)
            min_x = max_x = seed_x
            min_y = max_y = seed_y
            count = 0
            while stack:
                cx, cy = stack.pop()
                count += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if nx < 0 or nx >= roi_width or ny < 0 or ny >= roi_height:
                        continue
                    if visited_get(visited, ny, nx) or not is_foreground(left + nx, top + ny):
                        continue
                    visited_set(visited, ny, nx)
                    stack.append((nx, ny))

            component_width = max_x - min_x + 1
            component_height = max_y - min_y + 1
            touches_horizontal = min_x <= seed_px if horizontal == "left" else max_x >= roi_width - seed_px - 1
            touches_vertical = max_y >= roi_height - seed_px - 1 if vertical == "bottom" else min_y <= seed_px
            small_enough = (
                component_width <= max(42, int(round(width * 0.16)))
                and component_height <= max(42, int(round(height * 0.14)))
                and count <= max(1200, int(round(width * height * 0.018)))
            )
            if not (touches_horizontal or touches_vertical) or not small_enough:
                continue

            pad = CORNER_PAGE_BADGE_ERASE_PADDING_PX
            erase_left = max(0, left + min_x - pad)
            erase_top = max(0, top + min_y - pad)
            erase_right = min(width, left + max_x + pad + 1)
            erase_bottom = min(height, top + max_y + pad + 1)
            patch = Image.new("RGBA", (erase_right - erase_left, erase_bottom - erase_top), fill)
            output.paste(patch, (erase_left, erase_top))

    return output if "A" in image.getbands() else output.convert(mode)


def _trim_source_page_chrome(
    image: Image.Image,
    *,
    preserve_horizontal_bounds: bool = False,
) -> Image.Image:
    # Passage columns commonly end with real glyph strokes very close to the
    # crop boundary.  The generic vertical-guide detector can mistake those
    # strokes for scanner/page chrome and remove the final 1-3 characters.
    # Passage assets already use PDF column bounds plus a safety margin, so
    # preserve their horizontal extent while still cleaning other page chrome.
    if preserve_horizontal_bounds:
        # A passage glyph or footnote can legitimately touch the lower-right
        # edge of its PDF column. Corner-badge cleanup is intentionally skipped
        # here because it can erase that text; column-bounded passage crops do
        # not contain the source page-number corners in the first place.
        return _trim_bottom_blue_watermark(image)
    trimmed = _trim_edge_vertical_guides(image)
    trimmed = _trim_edge_attached_page_chrome(trimmed)
    trimmed = _trim_bottom_blue_watermark(trimmed)
    return _erase_corner_page_badges(trimmed)


def _integer_crop_rect_for_box(box: Box, *, image_width: int, image_height: int) -> tuple[int, int, int, int]:
    left = int(max(0, min(image_width - 1, math.floor(box.left))))
    top = int(max(0, min(image_height - 1, math.floor(box.top))))
    right = int(max(left + 1, min(image_width, math.ceil(box.right))))
    bottom = int(max(top + 1, min(image_height, math.ceil(box.bottom))))
    return left, top, right, bottom


def _expand_passage_source_bounds_horizontally(
    bounds: Box,
    *,
    image_width: int,
    padding_px: int = PASSAGE_SOURCE_HORIZONTAL_RECOVERY_PX,
    column_divider_x: float | None = None,
    column_index: int | None = None,
) -> Box:
    """Recover glyphs that sit just outside PDF-derived column bounds."""
    padding = max(0.0, float(padding_px))
    # PDF text ranges can omit the final glyph in the left column or the first
    # glyph in the right column. Recover both toward the page midpoint, but
    # clamp there so adjacent columns never duplicate each other's text.
    midpoint = (
        float(column_divider_x)
        if column_divider_x is not None
        else float(image_width) * 0.5
    )
    divider_exclusion = float(PASSAGE_CENTER_DIVIDER_EXCLUSION_PX)
    bounds_left = float(bounds.left)
    bounds_right = float(bounds.right)
    resolved_column_index = int(column_index) if column_index in {1, 2} else None
    if resolved_column_index == 1 or (resolved_column_index is None and bounds_right <= midpoint):
        safe_right = max(1.0, midpoint - divider_exclusion)
        left = max(0.0, bounds_left - padding)
        left = min(left, safe_right - 1.0)
        right = min(
            safe_right,
            bounds_right + max(padding, float(PASSAGE_SOURCE_INNER_EDGE_RECOVERY_PX)),
        )
    elif resolved_column_index == 2 or (resolved_column_index is None and bounds_left >= midpoint):
        safe_left = min(float(image_width) - 1.0, midpoint + divider_exclusion)
        left = max(
            safe_left,
            bounds_left - max(padding, float(PASSAGE_SOURCE_INNER_EDGE_RECOVERY_PX)),
        )
        right = min(float(image_width), bounds_right + padding)
        right = max(right, left + 1.0)
    else:
        left = max(0.0, bounds_left - padding)
        right = min(float(image_width), bounds_right + padding)
    return Box(
        left=left,
        top=float(bounds.top),
        width=max(1.0, right - left),
        height=float(bounds.height),
    )


def _expand_passage_segment_source_bounds(
    bounds: Box,
    *,
    image_width: int,
    image_height: int,
    horizontal_padding_px: int = PASSAGE_SOURCE_HORIZONTAL_RECOVERY_PX,
    vertical_padding_px: int = PASSAGE_SOURCE_VERTICAL_RECOVERY_PX,
    column_divider_x: float | None = None,
    column_index: int | None = None,
) -> Box:
    """Recover glyph strokes at every edge of an explicit passage segment.

    PDF passage ranges are tighter than ordinary problem crops. Horizontal
    recovery remains column-aware, while the small vertical outset protects
    ascenders, punctuation, and the final baseline without reaching far
    enough into the following question band.
    """
    expanded = _expand_passage_source_bounds_horizontally(
        bounds,
        image_width=image_width,
        padding_px=horizontal_padding_px,
        column_divider_x=column_divider_x,
        column_index=column_index,
    )
    vertical_padding = max(0.0, float(vertical_padding_px))
    top = max(0.0, float(expanded.top) - vertical_padding)
    bottom = min(float(image_height), float(expanded.bottom) + vertical_padding)
    return Box(
        left=float(expanded.left),
        top=top,
        width=float(expanded.width),
        height=max(1.0, bottom - top),
    )


def _source_horizontal_edge_has_recoverable_ink(
    image: Image.Image,
    bounds: Box,
    *,
    edge: str,
    scan_depth_px: int = TEXT_PRIORITY_SOURCE_EDGE_SCAN_DEPTH_PX,
) -> bool:
    """Return whether glyph-sized ink crosses a proposed source crop edge.

    Final white padding can hide that the source pixels were already clipped.
    This check looks on both sides of the *source* boundary, while rejecting
    long vertical page guides and the lower page-number/footer band.
    """
    if edge not in {"left", "right"} or image.width <= 1 or image.height <= 1:
        return False
    left, top, right, bottom = _integer_crop_rect_for_box(
        bounds,
        image_width=image.width,
        image_height=image.height,
    )
    depth = max(1, int(scan_depth_px))
    if edge == "left":
        if left <= 0:
            return False
        inside_left, inside_right = left, min(right, left + depth)
        outside_left, outside_right = max(0, left - depth), left
        inward_left, inward_right = min(right, left + depth), min(right, left + 24)
    else:
        if right >= image.width:
            return False
        inside_left, inside_right = max(left, right - depth), right
        outside_left, outside_right = right, min(image.width, right + depth)
        inward_left, inward_right = max(left, right - 24), max(left, right - depth)
    if (
        inside_right <= inside_left
        or outside_right <= outside_left
        or inward_right <= inward_left
    ):
        return False

    # Official page badges and footer rules occupy the last ~3% of the page
    # and can cross a column boundary without representing clipped content.
    footer_cutoff = image.height - max(48, int(round(image.height * 0.03)))
    scan_bottom = min(bottom, footer_cutoff)
    if scan_bottom <= top:
        return False

    gray = image.convert("L")
    if np is not None:
        pixels = np.asarray(gray, dtype=np.uint8)
        inside = pixels[top:scan_bottom, inside_left:inside_right] <= PROBLEM_EDGE_INK_DARK_THRESHOLD
        outside = pixels[top:scan_bottom, outside_left:outside_right] <= PROBLEM_EDGE_INK_DARK_THRESHOLD
        inward = pixels[top:scan_bottom, inward_left:inward_right] <= PROBLEM_EDGE_INK_DARK_THRESHOLD
        active_rows = [
            int(value)
            for value in np.flatnonzero(
                inside.any(axis=1) & outside.any(axis=1) & inward.any(axis=1)
            )
        ]
    else:
        pixels = gray.load()
        active_rows = []
        for y in range(top, scan_bottom):
            inside_dark = any(
                int(pixels[x, y]) <= PROBLEM_EDGE_INK_DARK_THRESHOLD
                for x in range(inside_left, inside_right)
            )
            outside_dark = any(
                int(pixels[x, y]) <= PROBLEM_EDGE_INK_DARK_THRESHOLD
                for x in range(outside_left, outside_right)
            )
            inward_dark = any(
                int(pixels[x, y]) <= PROBLEM_EDGE_INK_DARK_THRESHOLD
                for x in range(inward_left, inward_right)
            )
            if inside_dark and outside_dark and inward_dark:
                active_rows.append(y - top)
    if not active_rows:
        return False

    runs: list[tuple[int, int]] = []
    run_start = previous = active_rows[0]
    for y in active_rows[1:]:
        if y > previous + 1:
            runs.append((run_start, previous + 1))
            run_start = y
        previous = y
    runs.append((run_start, previous + 1))

    crop_height = max(1, scan_bottom - top)
    maximum_glyph_run = max(48, int(round(crop_height * 0.20)))
    glyph_sized_rows = sum(
        end - start
        for start, end in runs
        if end - start <= maximum_glyph_run
    )
    return glyph_sized_rows >= TEXT_PRIORITY_SOURCE_EDGE_MIN_ACTIVE_ROWS


def _expand_text_priority_source_bounds_horizontally(
    image: Image.Image,
    bounds: Box,
    *,
    padding_px: int = TEXT_PRIORITY_SOURCE_HORIZONTAL_RECOVERY_PX,
) -> Box:
    """Recover only text ink that demonstrably crosses a crop boundary."""
    padding = max(0.0, float(padding_px))
    if padding <= 0.0:
        return bounds
    recover_left = _source_horizontal_edge_has_recoverable_ink(
        image,
        bounds,
        edge="left",
    )
    recover_right = _source_horizontal_edge_has_recoverable_ink(
        image,
        bounds,
        edge="right",
    )
    if not recover_left and not recover_right:
        return bounds

    midpoint = float(image.width) * 0.5
    bounds_left = float(bounds.left)
    bounds_right = float(bounds.right)
    if bounds_right <= midpoint + 1.0:
        minimum_left, maximum_right = 0.0, midpoint
    elif bounds_left >= midpoint - 1.0:
        minimum_left, maximum_right = midpoint, float(image.width)
    else:
        minimum_left, maximum_right = 0.0, float(image.width)

    left = max(minimum_left, bounds_left - padding) if recover_left else bounds_left
    right = min(maximum_right, bounds_right + padding) if recover_right else bounds_right
    return Box(
        left=left,
        top=float(bounds.top),
        width=max(1.0, right - left),
        height=float(bounds.height),
    )


def _edge_band_has_dark_content(image: Image.Image, box: Box, *, edge: str) -> bool:
    if edge not in {"top", "bottom"}:
        return False
    left, top, right, bottom = _integer_crop_rect_for_box(
        box,
        image_width=image.width,
        image_height=image.height,
    )
    crop_width = right - left
    crop_height = bottom - top
    if crop_width <= 8 or crop_height <= 8:
        return False

    band_height = max(2, min(PROBLEM_EDGE_INK_SCAN_PX, crop_height // 4))
    if edge == "top":
        band_box = (left, top, right, min(bottom, top + band_height))
    else:
        band_box = (left, max(top, bottom - band_height), right, bottom)
    gray = image.crop(band_box).convert("L")
    total_pixels = max(1, gray.width * gray.height)
    min_dark_pixels = max(
        PROBLEM_EDGE_INK_MIN_DARK_PIXELS,
        int(round(total_pixels * PROBLEM_EDGE_INK_MIN_DARK_RATIO)),
    )
    if np is not None:
        dark_pixels = int(np.count_nonzero(np.asarray(gray, dtype=np.uint8) <= PROBLEM_EDGE_INK_DARK_THRESHOLD))
    else:
        values = gray.get_flattened_data() if hasattr(gray, "get_flattened_data") else gray.getdata()
        dark_pixels = sum(1 for value in values if int(value) <= PROBLEM_EDGE_INK_DARK_THRESHOLD)
    return dark_pixels >= min_dark_pixels


def _expand_box_for_edge_content(
    image: Image.Image,
    box: Box,
    *,
    top_extra_px: float = PROBLEM_EDGE_TOP_EXTRA_PADDING_PX,
    bottom_extra_px: float = PROBLEM_EDGE_BOTTOM_EXTRA_PADDING_PX,
) -> Box:
    top = box.top
    bottom = box.bottom
    if top > 0.0 and _edge_band_has_dark_content(image, box, edge="top"):
        top = max(0.0, top - float(top_extra_px))
    if bottom < float(image.height) and _edge_band_has_dark_content(image, box, edge="bottom"):
        bottom = min(float(image.height), bottom + float(bottom_extra_px))
    if top == box.top and bottom == box.bottom:
        return box
    return Box.from_points(box.left, top, box.right, bottom)


def _trim_box_bottom_page_chrome(
    image: Image.Image,
    box: Box,
    *,
    content_bottom: float,
) -> Box:
    left, top, right, bottom = _integer_crop_rect_for_box(
        box,
        image_width=image.width,
        image_height=image.height,
    )
    crop_width = right - left
    crop_height = bottom - top
    if crop_width <= 24 or crop_height <= 48:
        return box

    scan_height = min(crop_height, max(48, min(PAGE_FOOTER_CHROME_SCAN_PX, int(round(image.height * 0.22)))))
    scan_top = bottom - scan_height
    if scan_top < int(float(image.height) * PAGE_FOOTER_CHROME_BAND_RATIO):
        scan_top = int(float(image.height) * PAGE_FOOTER_CHROME_BAND_RATIO)
    if scan_top >= bottom - 2:
        return box

    gray = image.crop((left, scan_top, right, bottom)).convert("L")
    if np is not None:
        rows = np.asarray(gray, dtype=np.uint8)
        dark_counts = np.count_nonzero(rows <= PAGE_FOOTER_CHROME_LINE_DARK_THRESHOLD, axis=1)
    else:
        dark_counts_list: list[int] = []
        pixels = gray.load()
        for y in range(gray.height):
            dark_counts_list.append(
                sum(1 for x in range(gray.width) if int(pixels[x, y]) <= PAGE_FOOTER_CHROME_LINE_DARK_THRESHOLD)
            )
        dark_counts = dark_counts_list

    min_dark_count = max(12, int(round(crop_width * PAGE_FOOTER_CHROME_LINE_MIN_DARK_RATIO)))
    candidate_rows = [index for index, count in enumerate(dark_counts) if int(count) >= min_dark_count]
    if not candidate_rows:
        return box

    line_y = float(scan_top + max(candidate_rows))
    if line_y <= content_bottom + PAGE_FOOTER_CHROME_MIN_GAP_FROM_CONTENT_PX:
        return box

    target_bottom = line_y - PAGE_FOOTER_CHROME_TRIM_ABOVE_LINE_PX
    safe_bottom = content_bottom + PAGE_FOOTER_CHROME_CONTENT_PADDING_PX
    trimmed_bottom = max(safe_bottom, target_bottom)
    if trimmed_bottom >= box.bottom - 4.0 or trimmed_bottom <= box.top + 1.0:
        return box
    return Box.from_points(box.left, box.top, box.right, trimmed_bottom)


def _pad_problem_crop_edges(
    image: Image.Image,
    *,
    top_padding_px: int = PROBLEM_CROP_TOP_SAFE_PADDING_PX,
    bottom_padding_px: int = PROBLEM_CROP_BOTTOM_SAFE_PADDING_PX,
    left_padding_px: int = 0,
    right_padding_px: int = 0,
) -> Image.Image:
    if (
        top_padding_px <= 0
        and bottom_padding_px <= 0
        and left_padding_px <= 0
        and right_padding_px <= 0
    ) or image.width <= 0 or image.height <= 0:
        return image
    if "A" in image.getbands():
        fill = (255, 255, 255, 0)
        mode = "RGBA"
    else:
        fill = (255, 255, 255)
        mode = "RGB"
    converted = image.convert(mode)
    top_padding = max(0, int(top_padding_px))
    bottom_padding = max(0, int(bottom_padding_px))
    left_padding = max(0, int(left_padding_px))
    right_padding = max(0, int(right_padding_px))
    padded = Image.new(
        mode,
        (
            converted.width + left_padding + right_padding,
            converted.height + top_padding + bottom_padding,
        ),
        fill,
    )
    padded.paste(converted, (left_padding, top_padding))
    return padded


def _pad_problem_crop_bottom(image: Image.Image, padding_px: int = PROBLEM_CROP_BOTTOM_SAFE_PADDING_PX) -> Image.Image:
    return _pad_problem_crop_edges(image, top_padding_px=0, bottom_padding_px=padding_px)


def _enhance_problem_crop(
    image: Image.Image,
    *,
    target_min_width_px: int = PROBLEM_CROP_TARGET_MIN_WIDTH_PX,
    max_upscale: float = PROBLEM_CROP_MAX_UPSCALE,
    text_priority: bool = False,
    allow_text_upscale: bool = False,
) -> Image.Image:
    """Upscale small crops and sharpen ink so the chalk render reads cleanly.

    Run BEFORE alpha-extraction so the upscale uses the original ink edges
    rather than a binary cutout. Returns a new image; the input is not
    mutated.
    """
    if image.width <= 0 or image.height <= 0:
        return image

    has_alpha = "A" in image.getbands()
    converted = image.convert("RGBA" if has_alpha else "RGB")
    alpha = converted.getchannel("A") if has_alpha else None
    rgb = converted.convert("RGB")
    # Dense text is already rasterized at PDF render resolution. Upscaling it
    # here and shrinking it again for V1 exports creates a second antialiasing
    # fringe around every glyph, so keep its native pixel grid. Diagram-heavy
    # problems retain the existing upscale path for thin graph/math strokes.
    if (not text_priority or allow_text_upscale) and rgb.width < target_min_width_px:
        scale = min(
            max_upscale,
            target_min_width_px / max(rgb.width, 1),
        )
        if scale > 1.05:
            new_size = (int(round(rgb.width * scale)), int(round(rgb.height * scale)))
            rgb = rgb.resize(new_size, Image.Resampling.LANCZOS)
            if alpha is not None:
                alpha = alpha.resize(new_size, Image.Resampling.LANCZOS)

    # Unsharp mask brings ink-on-paper transitions back after upscale and also
    # helps thin-stroke text survive the alpha-mask threshold inside
    # _extract_problem_cutout.
    sharpened = rgb.filter(
        ImageFilter.UnsharpMask(
            radius=TEXT_PRIORITY_UNSHARP_RADIUS if text_priority else 1.4,
            percent=TEXT_PRIORITY_UNSHARP_PERCENT if text_priority else 140,
            threshold=TEXT_PRIORITY_UNSHARP_THRESHOLD if text_priority else 2,
        )
    )
    if alpha is not None:
        rgba = sharpened.convert("RGBA")
        rgba.putalpha(alpha)
        return rgba
    return sharpened


def _extract_problem_cutout(
    image: Image.Image,
    *,
    chalk_color: tuple[int, int, int] | None = None,
    text_priority: bool = False,
) -> Image.Image:
    """Recolor problem ink as chalk on a transparent canvas.

    The output is an RGBA PNG where ``alpha`` encodes how much ink the source
    crop has at each pixel and the RGB channels are uniformly the chalk
    color. ClassIn paints its own chalkboard background under transparent
    PNGs, so shipping the chalk-on-transparent cutout directly removes the
    legacy "composite onto a fake dark board" step — the original ink color
    would otherwise be invisible against ClassIn's real chalkboard.
    """
    resolved_chalk = (
        tuple(int(c) for c in chalk_color)
        if chalk_color is not None
        else BOARD_THEME_PALETTES[DEFAULT_BOARD_THEME]["chalk"]
    )

    original_alpha = image.getchannel("A") if "A" in image.getbands() else None
    has_existing_transparency = bool(original_alpha and original_alpha.getextrema()[0] < 245)
    cleaned, clean_stats = clean_problem_image_transparency(
        image,
        transparent_background=not has_existing_transparency,
        remove_corner_page_artifacts=True,
    )
    if "A" in cleaned.getbands():
        alpha_mask = cleaned.getchannel("A")
        alpha_min, alpha_max = alpha_mask.getextrema()
        if alpha_min < 245 and alpha_max > 12:
            # Reuse the model/paper background removal directly. This catches
            # both black model backgrounds and white PDF paper, and it removes
            # small lower-corner page number badges before they get encoded.
            if np is not None:
                alpha_array = np.asarray(alpha_mask, dtype=np.float32) / 255.0
                if clean_stats.get("background_kind") == "light" and not text_priority:
                    dilated = np.copy(alpha_array)
                    for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                        shifted = np.roll(np.roll(alpha_array, dy, axis=0), dx, axis=1)
                        if dy < 0:
                            shifted[-1, :] = 0.0
                        elif dy > 0:
                            shifted[0, :] = 0.0
                        if dx < 0:
                            shifted[:, -1] = 0.0
                        elif dx > 0:
                            shifted[:, 0] = 0.0
                        dilated = np.maximum(dilated, shifted)
                    alpha_array = np.clip((0.75 * alpha_array) + (0.25 * dilated), 0.0, 1.0)
                cutout = _compose_chalk_rgba(alpha_array, resolved_chalk)
            else:
                cutout = _compose_chalk_rgba_pil(alpha_mask, cleaned.size, resolved_chalk)
            return (
                _finalize_text_cutout(cutout, chalk_color=resolved_chalk)
                if text_priority
                else cutout
            )

    rgb = image.convert("RGB")
    gray = ImageOps.autocontrast(rgb.convert("L"))
    stat = ImageStat.Stat(gray)
    mean_brightness = stat.mean[0]

    if mean_brightness <= DARK_BOARD_BRIGHTNESS_THRESHOLD:
        # Source is already dark-background (e.g. a screen capture of a
        # chalkboard) — bright pixels are the ink so alpha follows brightness.
        if np is not None:
            np_alpha = np.asarray(gray, dtype=np.float32) / 255.0
            cutout = _compose_chalk_rgba(np_alpha, resolved_chalk)
        else:
            cutout = _compose_chalk_rgba_pil(gray, image.size, resolved_chalk)
        return (
            _finalize_text_cutout(cutout, chalk_color=resolved_chalk)
            if text_priority
            else cutout
        )

    if np is None:
        mask = gray.point(lambda px: 255 if px < 242 else 0, mode="L")
        if not text_priority:
            mask_dilated = mask.filter(ImageFilter.MaxFilter(3))
            mask = Image.blend(mask, mask_dilated, 0.35)
        cutout = _compose_chalk_rgba_pil(mask, image.size, resolved_chalk)
        return (
            _finalize_text_cutout(cutout, chalk_color=resolved_chalk)
            if text_priority
            else cutout
        )

    rgb_array = np.asarray(rgb, dtype=np.float32) / 255.0
    gray_array = np.asarray(gray, dtype=np.float32)
    darkness = 255.0 - gray_array

    if not text_priority:
        # Thicken thin math lines/symbols via a fast 4-connectivity
        # morphological dilation. Dense Korean/English text deliberately
        # skips this: the added outside pixel reads as a drop shadow when
        # thousands of glyphs are placed together.
        dilated_darkness = np.copy(darkness)
        for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            shifted = np.roll(np.roll(darkness, dy, axis=0), dx, axis=1)
            if dy < 0:
                shifted[-1, :] = 0.0
            elif dy > 0:
                shifted[0, :] = 0.0
            if dx < 0:
                shifted[:, -1] = 0.0
            elif dx > 0:
                shifted[:, 0] = 0.0
            dilated_darkness = np.maximum(dilated_darkness, shifted)

        # Smooth blend keeps edge antialiasing while retaining thin lines.
        darkness = 0.65 * darkness + 0.35 * dilated_darkness

    noise_floor = max(10.0, float(np.percentile(darkness, 62)) + 4.0)
    alpha_strength = np.clip((darkness - noise_floor) / max(1.0, 255.0 - noise_floor), 0.0, 1.0)
    alpha_strength = np.power(np.clip(alpha_strength * 1.45, 0.0, 1.0), 0.7)

    max_channel = rgb_array.max(axis=2)
    whiteness = gray_array / 255.0
    color_distance = np.linalg.norm(1.0 - rgb_array, axis=2) / np.sqrt(3.0)
    keep_color = np.clip((color_distance - 0.035) / 0.42, 0.0, 1.0)
    keep_dark = np.clip((1.0 - whiteness - 0.08) / 0.7, 0.0, 1.0)
    alpha = np.maximum(alpha_strength, np.maximum(keep_color * 0.92, keep_dark))
    alpha = np.where(max_channel > 0.985, alpha * 0.08, alpha)

    cutout = _compose_chalk_rgba(alpha, resolved_chalk)
    return (
        _finalize_text_cutout(cutout, chalk_color=resolved_chalk)
        if text_priority
        else cutout
    )


def _compose_chalk_rgba(alpha_array, chalk_color: tuple[int, int, int]) -> Image.Image:
    """RGBA from a numpy alpha array (values 0..1) + uniform chalk RGB."""
    if np is None:
        raise RuntimeError("numpy is required for _compose_chalk_rgba")
    height, width = alpha_array.shape
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    rgba[..., 0] = int(chalk_color[0])
    rgba[..., 1] = int(chalk_color[1])
    rgba[..., 2] = int(chalk_color[2])
    rgba[..., 3] = np.clip(alpha_array * 255.0, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(rgba, mode="RGBA")


def _compose_chalk_rgba_pil(
    alpha_mask: Image.Image, size: tuple[int, int], chalk_color: tuple[int, int, int]
) -> Image.Image:
    """PIL-only fallback for :func:`_compose_chalk_rgba`."""
    chalk = Image.new("RGBA", size, chalk_color + (0,))
    alpha = alpha_mask if alpha_mask.size == size else alpha_mask.resize(size)
    chalk.putalpha(alpha)
    return chalk


def _finalize_text_cutout(
    image: Image.Image,
    *,
    chalk_color: tuple[int, int, int],
    alpha_cutoff: int = TEXT_DEHALO_ALPHA_CUTOFF,
) -> Image.Image:
    """Remove low-alpha text halos and normalize all RGB to one chalk tone.

    Keeping the same RGB even in fully transparent pixels prevents straight-
    alpha Lanczos resizing from pulling white/cyan edge colors back into a
    glyph. The conservative cutoff removes resampling dust while retaining
    punctuation and thin antialiased strokes.
    """
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    cutoff = max(0, min(254, int(alpha_cutoff)))
    alpha = alpha.point(lambda value: 0 if value <= cutoff else value)
    finalized = Image.new("RGBA", rgba.size, tuple(int(value) for value in chalk_color) + (0,))
    finalized.putalpha(alpha)
    return finalized


def _prepare_image_for_dark_board(image: Image.Image, *, board_theme: str = DEFAULT_BOARD_THEME) -> Image.Image:
    """Deprecated. ClassIn paints its own chalkboard background under
    transparent PNGs, so we ship the chalk-on-transparent cutout directly.
    Kept as a thin shim so any external callers keep working."""
    return _extract_problem_cutout(_enhance_problem_crop(image), chalk_color=_resolve_chalk_color(board_theme))


def _write_render_image(image: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(
        path,
        format="PNG",
        compress_level=INTERMEDIATE_PNG_COMPRESSION_LEVEL,
        optimize=False,
    )
    return path


def _composite_on_board_background(image: Image.Image, *, board_theme: str = DEFAULT_BOARD_THEME) -> Image.Image:
    rgba = image.convert("RGBA")
    background = Image.new(
        "RGBA",
        rgba.size,
        BOARD_THEME_PALETTES[_resolve_board_theme(board_theme)]["background"] + (255,),
    )
    background.alpha_composite(rgba)
    return background.convert("RGB")


BOARD_RENDER_CROP_ASPECT_TOLERANCE = 0.03


def _board_render_matches_crop(rendered: Image.Image, crop_image: Image.Image) -> bool:
    """A stored board render is only reusable if it covers the same content.

    A stale render (made before the problem box was re-adjusted) has a
    different height-to-width ratio than the current crop; using it would
    publish a problem with its bottom cut off (or stretched)."""
    if min(rendered.width, rendered.height, crop_image.width, crop_image.height) <= 0:
        return False
    crop_aspect = crop_image.height / crop_image.width
    render_aspect = rendered.height / rendered.width
    return abs(render_aspect - crop_aspect) <= (
        BOARD_RENDER_CROP_ASPECT_TOLERANCE * max(crop_aspect, render_aspect)
    )


def _load_board_export_image(
    board_render_path: Path,
    crop_image: Image.Image,
    *,
    board_theme: str = DEFAULT_BOARD_THEME,
    target_size: tuple[int, int] | None = None,
    text_priority: bool = False,
    trusted_preprocessed_render: bool = False,
) -> Image.Image:
    chalk_color = _resolve_chalk_color(board_theme)
    rendered: Image.Image | None = None
    if trusted_preprocessed_render:
        try:
            with Image.open(board_render_path) as loaded_render:
                rendered = loaded_render.convert(
                    "RGBA" if "A" in loaded_render.getbands() else "RGB"
                )
        except OSError:
            rendered = None
        if rendered is not None and not _board_render_matches_crop(rendered, crop_image):
            rendered = None
        if (
            rendered is not None
            and "A" in rendered.getbands()
            and rendered.getchannel("A").getextrema()[0] < 245
        ):
            if target_size is not None and rendered.size != target_size:
                rendered = rendered.resize(target_size, Image.Resampling.LANCZOS)
            # Fresh pipeline renders have already completed the expensive
            # extraction/finalization pass. The record builder performs the
            # one final normalization after any required resize.
            return rendered

    if text_priority:
        # Rebuild dense text from the raw crop instead of reusing the generic
        # or external board render, which may already contain
        # diagram-oriented dilation. Fresh renders produced in this same
        # pipeline are explicitly trusted above because they already used the
        # identical text-priority extraction path.
        cutout = _extract_problem_cutout(
            _enhance_problem_crop(crop_image, text_priority=True),
            chalk_color=chalk_color,
            text_priority=True,
        )
        if target_size is not None and cutout.size != target_size:
            cutout = cutout.resize(target_size, Image.Resampling.LANCZOS)
        return _finalize_text_cutout(cutout, chalk_color=chalk_color)

    if rendered is None:
        try:
            rendered = Image.open(board_render_path)
        except OSError:
            rendered = None
        if rendered is not None and not _board_render_matches_crop(rendered, crop_image):
            rendered = None

    if rendered is not None:
        if target_size is not None and rendered.size != target_size:
            rendered = rendered.resize(target_size, Image.Resampling.LANCZOS)
        if "A" in rendered.getbands():
            cleaned, _stats = clean_problem_image_transparency(
                rendered,
                transparent_background=rendered.getchannel("A").getextrema()[0] >= 245,
                remove_corner_page_artifacts=True,
            )
            return cleaned
        rendered_rgb = rendered.convert("RGB")
        mean_brightness = ImageStat.Stat(rendered_rgb.convert("L")).mean[0]
        if mean_brightness <= DARK_BOARD_BRIGHTNESS_THRESHOLD:
            return _extract_problem_cutout(
                rendered_rgb,
                chalk_color=chalk_color,
            )

    cutout = _extract_problem_cutout(
        _enhance_problem_crop(crop_image),
        chalk_color=chalk_color,
    )
    if target_size is not None and cutout.size != target_size:
        cutout = cutout.resize(target_size, Image.Resampling.LANCZOS)
    return cutout


def _build_transparent_reconstruction_image(
    crop_image: Image.Image,
    *,
    board_theme: str = DEFAULT_BOARD_THEME,
    text_priority: bool = False,
    trim_page_chrome: bool = True,
) -> Image.Image:
    # Manual crops skip chrome trimming: the user chose the exact region, and
    # a false-positive "footer line" match would silently cut real content.
    if trim_page_chrome:
        crop_image = _trim_source_page_chrome(crop_image)
    # Stage 3 transparently attempts the local Lite model only for undersized
    # crops. The backend is fail-open, and this extra guard ensures packaging
    # or runtime surprises can never block the existing stage-3 path.
    if not text_priority:
        try:
            from upscayl_backend import auto_upscale_image

            crop_image = auto_upscale_image(crop_image).image
        except Exception:  # noqa: BLE001 - stage 3 must always retain its legacy fallback
            pass
    enhanced_crop = _enhance_problem_crop(
        crop_image,
        target_min_width_px=RECONSTRUCT_TARGET_MIN_WIDTH_PX,
        max_upscale=RECONSTRUCT_MAX_UPSCALE,
        text_priority=text_priority,
        allow_text_upscale=True,
    )
    return _extract_problem_cutout(
        enhanced_crop,
        chalk_color=_resolve_chalk_color(board_theme),
        text_priority=text_priority,
    )


def _problem_prefers_text_preservation(
    subject: Any,
    *source_hints: Any,
) -> bool:
    normalized_subject = str(subject or "").strip().lower()
    if normalized_subject in TEXT_PRIORITY_SUBJECTS:
        return True
    if normalized_subject not in {"", "unknown", "none", "auto"}:
        return False
    hint = " ".join(str(value or "") for value in source_hints).lower()
    return "국어" in hint or "영어" in hint or "korean" in hint or "english" in hint


def _encode_image_bytes(image: Image.Image, quality: int = 92) -> tuple[bytes, str]:
    """Encode a PIL image for use in an EDB image record."""
    buf = io.BytesIO()
    has_alpha = "A" in image.getbands()
    if has_alpha:
        # Final EDB payloads keep Pillow's normal compression level. Temporary
        # pipeline images use the faster intermediate setting above.
        image.save(buf, format="PNG")
        return buf.getvalue(), "PNG"
    image.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "JPEG"


TEXT_ELIGIBLE_BLOCK_TYPES = {
    BlockType.TITLE,
    BlockType.SECTION,
    BlockType.STEM,
    BlockType.CHOICE,
    BlockType.EXPLANATION,
    BlockType.NOTE,
}
IMAGE_ONLY_BLOCK_TYPES = {
    BlockType.IMAGE,
    BlockType.DIAGRAM,
    BlockType.TABLE,
    BlockType.DECORATION,
    BlockType.FORMULA,
}


@dataclass(slots=True)
class ProblemEntry:
    problem_id: str
    title: str
    problem_number: int | None
    subject: Subject
    source_page_id: str
    source_path: str
    prepared_page: PreparedPage
    bounds: Box
    crop_path: Path
    board_render_path: Path
    blocks: list[ContentBlock]
    actual_height_pages: float
    overflow_allowed: bool
    reading_heavy: bool
    risk_flags: list[str]
    placement_x_ratio: float | None = None
    placement_y_ratio: float | None = None
    placement_scale_ratio: float | None = None
    preserve_legacy_placement_scale: bool = False
    processing_step: str = PROCESSING_STEP_RAW
    input_intent: str | None = None
    force_full_page_bounds: bool = False
    preserve_media_regions: list[dict[str, Any]] = field(default_factory=list)
    source_segments: list[dict[str, Any]] = field(default_factory=list)
    board_render_preprocessed: bool = False


@dataclass(slots=True)
class _ProblemAssetTask:
    source_image: Image.Image
    bounds: Box
    crop_path: Path
    board_render_path: Path
    chalk_color: tuple[int, int, int]
    segment_bounds: tuple[Box, ...] | None = None
    segment_column_indices: tuple[int | None, ...] | None = None
    text_payload: str | None = None
    text_title: str | None = None
    trim_edge_guides: bool = True
    preserve_horizontal_bounds: bool = False
    protect_horizontal_bounds: bool = False
    horizontal_safe_padding_px: int = 0
    text_priority: bool = False
    pad_edges: bool = True
    subject: Subject = Subject.UNKNOWN
    source_hints: tuple[str, ...] = ()
    source_media_regions: tuple[dict[str, Any], ...] = ()
    rendered_media_regions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _ProblemEntryDraft:
    problem_id: str
    title: str
    problem_number: int | None
    subject: Subject
    source_page_id: str
    source_path: str
    prepared_page: PreparedPage
    bounds: Box
    crop_path: Path
    board_render_path: Path
    blocks: list[ContentBlock]
    overflow_allowed: bool
    reading_heavy: bool
    risk_flags: list[str]
    processing_step: str
    placement_scale_ratio: float | None
    input_intent: str | None
    force_full_page_bounds: bool
    asset_task: _ProblemAssetTask | None
    preserve_legacy_placement_scale: bool = False
    preserve_media_regions: list[dict[str, Any]] = field(default_factory=list)
    source_segments: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _ImageOnlyRecordImage:
    crop_path: Path
    board_render_path: Path
    image_bytes: bytes
    secondary_bytes: bytes
    width_px: int
    height_px: int
    scale_ratio: float | None = None
    display_width_px: float | None = None
    display_height_px: float | None = None
    source_width_px: int = 0
    source_height_px: int = 0
    resolution_policy: str = "source-preserving"


def _resolve_problem_asset_worker_count(task_count: int) -> int:
    if task_count <= 1:
        return 1
    max_workers = max(1, min(8, task_count, os.cpu_count() or 2))
    raw_worker_count = os.environ.get("EDB_PROBLEM_ASSET_WORKERS", "").strip()
    if raw_worker_count:
        try:
            requested_workers = int(raw_worker_count)
        except ValueError:
            requested_workers = max_workers
        if requested_workers <= 0:
            return 1
        return max(1, min(max_workers, requested_workers))
    return max_workers


def _resolve_image_record_worker_count(item_count: int) -> int:
    if item_count <= 1:
        return 1
    max_workers = max(1, min(4, item_count, os.cpu_count() or 2))
    raw_worker_count = os.environ.get("EDB_IMAGE_RECORD_WORKERS", "").strip()
    if raw_worker_count:
        try:
            requested_workers = int(raw_worker_count)
        except ValueError:
            requested_workers = max_workers
        if requested_workers <= 0:
            return 1
        return max(1, min(max_workers, requested_workers))
    return max_workers


def _load_text_fallback_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/Library/Fonts/AppleGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    if not text:
        return 0
    bbox = draw.textbbox((0, 0), text, font=font)
    return max(0, int(bbox[2] - bbox[0]))


def _wrap_text_for_card(
    text: str,
    *,
    draw: ImageDraw.ImageDraw,
    font: ImageFont.ImageFont,
    max_width_px: int,
) -> list[str]:
    wrapped: list[str] = []
    for raw_line in str(text or "").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            wrapped.append("")
            continue
        current = ""
        for char in line:
            candidate = current + char
            if current and _text_width(draw, candidate, font) > max_width_px:
                wrapped.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        if current:
            wrapped.append(current.rstrip())
    return wrapped


def _render_text_fallback_problem_card(text: str, *, title: str | None = None) -> Image.Image:
    width = HWP_TEXT_FALLBACK_CARD_WIDTH_PX
    margin_x = 58
    margin_y = 52
    font = _load_text_fallback_font(36)
    title_font = _load_text_fallback_font(42)
    probe = Image.new("RGB", (width, 100), "white")
    probe_draw = ImageDraw.Draw(probe)

    body_text = str(text or "").strip()
    if title and title.strip() and not body_text.startswith(title.strip()):
        body_text = f"{title.strip()}\n{body_text}".strip()
    lines = _wrap_text_for_card(
        body_text,
        draw=probe_draw,
        font=font,
        max_width_px=width - margin_x * 2,
    )

    line_height = max(42, int(font.getbbox("가")[3] - font.getbbox("가")[1]) + 10)
    height = margin_y * 2 + max(1, len(lines)) * line_height
    truncated = False
    if height > HWP_TEXT_FALLBACK_CARD_MAX_HEIGHT_PX:
        max_lines = max(1, (HWP_TEXT_FALLBACK_CARD_MAX_HEIGHT_PX - margin_y * 2) // line_height)
        lines = lines[:max_lines]
        if lines:
            lines[-1] = (lines[-1].rstrip() + " ...").strip()
        height = HWP_TEXT_FALLBACK_CARD_MAX_HEIGHT_PX
        truncated = True
    height = max(HWP_TEXT_FALLBACK_CARD_MIN_HEIGHT_PX, height)

    card = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(card)
    y = margin_y
    for index, line in enumerate(lines):
        resolved_font = title_font if index == 0 else font
        draw.text((margin_x, y), line, fill=(20, 20, 20), font=resolved_font)
        y += line_height
    if truncated:
        draw.text((margin_x, height - margin_y + 8), "...", fill=(20, 20, 20), font=font)
    return card


def _media_region_box(region: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    bbox = region.get("bbox")
    if not isinstance(bbox, Mapping):
        return None
    try:
        left = float(bbox.get("left", 0.0))
        top = float(bbox.get("top", 0.0))
        right = float(bbox.get("right", left + float(bbox.get("width", 0.0))))
        bottom = float(bbox.get("bottom", top + float(bbox.get("height", 0.0))))
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _locate_horizontal_crop_offset(before: Image.Image, after: Image.Image) -> int:
    """Return the exact left offset when a helper cropped only horizontally."""
    difference = before.width - after.width
    if difference <= 0 or before.height != after.height:
        return 0
    target_bytes = after.tobytes()
    candidates = [0, difference, *range(1, difference)]
    for left in dict.fromkeys(candidates):
        if before.crop((left, 0, left + after.width, after.height)).tobytes() == target_bytes:
            return int(left)
    return 0


def _trim_source_page_chrome_with_offset(
    image: Image.Image,
    *,
    preserve_horizontal_bounds: bool,
) -> tuple[Image.Image, int]:
    """Apply the normal crop cleanup while retaining its x-coordinate shift."""
    left_offset = 0
    if preserve_horizontal_bounds:
        return _trim_bottom_blue_watermark(image), left_offset
    next_image = _trim_edge_vertical_guides(image)
    left_offset += _locate_horizontal_crop_offset(image, next_image)
    trimmed = next_image
    next_image = _trim_edge_attached_page_chrome(trimmed)
    left_offset += _locate_horizontal_crop_offset(trimmed, next_image)
    trimmed = next_image
    trimmed = _trim_bottom_blue_watermark(trimmed)
    return _erase_corner_page_badges(trimmed), left_offset


def _map_source_media_regions_to_crop(
    regions: Sequence[Mapping[str, Any]],
    *,
    source_crop_rect: tuple[int, int, int, int],
    horizontal_trim_px: int,
    left_padding_px: int,
    top_padding_px: int,
    output_size: tuple[int, int],
) -> list[dict[str, Any]]:
    crop_left, crop_top, crop_right, crop_bottom = source_crop_rect
    output_width, output_height = output_size
    mapped: list[dict[str, Any]] = []
    for region in regions:
        if str(region.get("kind") or "") not in {"image", "table"}:
            continue
        try:
            confidence = float(region.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        if confidence < 0.9:
            continue
        box = _media_region_box(region)
        if box is None:
            continue
        left, top, right, bottom = box
        intersect_left = max(left, float(crop_left))
        intersect_top = max(top, float(crop_top))
        intersect_right = min(right, float(crop_right))
        intersect_bottom = min(bottom, float(crop_bottom))
        if intersect_right <= intersect_left or intersect_bottom <= intersect_top:
            continue
        overlap = (intersect_right - intersect_left) * (intersect_bottom - intersect_top)
        if overlap / max(1.0, (right - left) * (bottom - top)) < 0.8:
            continue
        local_left = intersect_left - crop_left - horizontal_trim_px + left_padding_px
        local_top = intersect_top - crop_top + top_padding_px
        local_right = intersect_right - crop_left - horizontal_trim_px + left_padding_px
        local_bottom = intersect_bottom - crop_top + top_padding_px
        local_left = max(0.0, min(float(output_width), local_left))
        local_top = max(0.0, min(float(output_height), local_top))
        local_right = max(local_left, min(float(output_width), local_right))
        local_bottom = max(local_top, min(float(output_height), local_bottom))
        if local_right - local_left < 2.0 or local_bottom - local_top < 2.0:
            continue
        payload = {key: value for key, value in region.items() if key != "bbox"}
        payload["bbox"] = {
            "left": local_left,
            "top": local_top,
            "right": local_right,
            "bottom": local_bottom,
            "width": local_right - local_left,
            "height": local_bottom - local_top,
        }
        mapped.append(payload)
    return mapped


# A media region is pasted back opaquely only when it looks like real media
# (photo, shading, colour). Plain dark-line-on-white figures — graphs,
# geometry, physics diagrams — read better as chalk strokes, and preserving
# them reintroduces an opaque white box on the dark board.
MEDIA_LINE_ART_MAX_COLOR_FRACTION = 0.05
MEDIA_LINE_ART_MIN_BRIGHT_FRACTION = 0.45
MEDIA_LINE_ART_MAX_MIDTONE_FRACTION = 0.18


def _media_patch_is_line_art(patch: Image.Image) -> bool:
    if patch.width < 8 or patch.height < 8:
        return False
    sample = patch.convert("RGB")
    sample.thumbnail((160, 160))
    total = max(1, sample.width * sample.height)
    saturation_histogram = sample.convert("HSV").getchannel("S").histogram()
    colorful_fraction = sum(saturation_histogram[64:]) / total
    if colorful_fraction > MEDIA_LINE_ART_MAX_COLOR_FRACTION:
        return False
    luminance_histogram = sample.convert("L").histogram()
    bright_fraction = sum(luminance_histogram[231:]) / total
    midtone_fraction = sum(luminance_histogram[100:221]) / total
    return (
        bright_fraction >= MEDIA_LINE_ART_MIN_BRIGHT_FRACTION
        and midtone_fraction <= MEDIA_LINE_ART_MAX_MIDTONE_FRACTION
    )


def _apply_selective_media_preservation(
    rendered: Image.Image,
    source_crop: Image.Image,
    regions: Sequence[Mapping[str, Any]],
) -> Image.Image:
    """Restore exact source pixels only inside high-confidence media boxes."""
    if not regions or source_crop.width <= 0 or source_crop.height <= 0:
        return rendered
    output = rendered.convert("RGBA")
    source = source_crop.convert("RGBA")
    x_scale = output.width / source.width
    y_scale = output.height / source.height
    for region in regions:
        box = _media_region_box(region)
        if box is None:
            continue
        left, top, right, bottom = box
        source_box = (
            max(0, min(source.width, int(math.floor(left)))),
            max(0, min(source.height, int(math.floor(top)))),
            max(0, min(source.width, int(math.ceil(right)))),
            max(0, min(source.height, int(math.ceil(bottom)))),
        )
        if source_box[2] <= source_box[0] or source_box[3] <= source_box[1]:
            continue
        target_box = (
            max(0, min(output.width, int(round(left * x_scale)))),
            max(0, min(output.height, int(round(top * y_scale)))),
            max(0, min(output.width, int(round(right * x_scale)))),
            max(0, min(output.height, int(round(bottom * y_scale)))),
        )
        target_size = (target_box[2] - target_box[0], target_box[3] - target_box[1])
        if target_size[0] <= 0 or target_size[1] <= 0:
            continue
        patch = source.crop(source_box)
        if _media_patch_is_line_art(patch):
            continue
        if patch.size != target_size:
            patch = patch.resize(target_size, Image.Resampling.LANCZOS)
        output.paste(patch, (target_box[0], target_box[1]))
    return output


def _source_hints_suggest_math(*source_hints: Any) -> bool:
    """Infer a math document locally without rewriting its subject metadata."""
    hint_text = unquote(" ".join(str(value or "") for value in source_hints)).lower()
    return any(marker in hint_text for marker in MATH_SOURCE_HINT_MARKERS) or bool(
        MATH_SOURCE_HINT_RE.search(hint_text)
    )


def _stage2_prefers_chalk_math_media(
    subject: Subject | str,
    processing_step: str,
    *source_hints: Any,
) -> bool:
    """Keep math figures in the Stage-2 chalk treatment.

    Recognition intentionally leaves the subject open-ended. When it is
    unknown, use source-local evidence only for this rendering decision; do
    not persist or otherwise coerce the problem subject.
    """
    subject_value = subject.value if isinstance(subject, Subject) else str(subject).strip().lower()
    if _normalize_processing_step(processing_step) != PROCESSING_STEP_CHALK:
        return False
    if subject_value == Subject.MATH.value:
        return True
    if subject_value not in UNKNOWN_SUBJECT_VALUES:
        return False
    return _source_hints_suggest_math(*source_hints)


def _render_problem_board_asset(crop: Image.Image, task: _ProblemAssetTask) -> None:
    enhanced_crop = _enhance_problem_crop(crop, text_priority=task.text_priority)
    if task.text_priority:
        cutout_image = _extract_problem_cutout(
            enhanced_crop,
            chalk_color=task.chalk_color,
            text_priority=True,
        )
    else:
        cutout_image = _extract_problem_cutout(
            enhanced_crop,
            chalk_color=task.chalk_color,
        )
    # Math diagrams are usually dark line art on white. The Stage-2 cutout
    # above turns them into bright chalk strokes on transparency, so restoring
    # their source pixels would reintroduce an opaque white box.
    if not _stage2_prefers_chalk_math_media(
        task.subject,
        PROCESSING_STEP_CHALK,
        *task.source_hints,
    ):
        cutout_image = _apply_selective_media_preservation(
            cutout_image,
            crop,
            task.rendered_media_regions,
        )
    _write_render_image(cutout_image, task.board_render_path)


def _render_problem_asset(task: _ProblemAssetTask) -> tuple[int, int]:
    if task.text_payload:
        crop = _render_text_fallback_problem_card(task.text_payload, title=task.text_title)
        task.crop_path.parent.mkdir(parents=True, exist_ok=True)
        crop.save(
            task.crop_path,
            format="PNG",
            compress_level=INTERMEDIATE_PNG_COMPRESSION_LEVEL,
            optimize=False,
        )
        _render_problem_board_asset(crop, task)
        return crop.size

    passage_column_divider_x = (
        detect_pdf_visual_column_divider_x(task.source_image)
        if task.preserve_horizontal_bounds
        else None
    )

    def crop_segment(
        bounds: Box,
        *,
        recover_vertical_edges: bool = False,
        column_index: int | None = None,
    ) -> tuple[Image.Image, list[dict[str, Any]]]:
        if task.preserve_horizontal_bounds:
            source_bounds = _expand_passage_segment_source_bounds(
                bounds,
                image_width=task.source_image.width,
                image_height=task.source_image.height,
                vertical_padding_px=(
                    PASSAGE_SOURCE_VERTICAL_RECOVERY_PX
                    if recover_vertical_edges
                    else 0
                ),
                column_divider_x=passage_column_divider_x,
                column_index=column_index,
            )
        elif task.text_priority:
            source_bounds = _expand_text_priority_source_bounds_horizontally(
                task.source_image,
                bounds,
            )
        else:
            source_bounds = bounds
        source_crop_rect = _integer_crop_rect_for_box(
            source_bounds,
            image_width=task.source_image.width,
            image_height=task.source_image.height,
        )
        segment = task.source_image.crop(source_crop_rect)
        horizontal_trim_px = 0
        if task.trim_edge_guides:
            segment, horizontal_trim_px = _trim_source_page_chrome_with_offset(
                segment,
                preserve_horizontal_bounds=(
                    task.preserve_horizontal_bounds
                    or task.protect_horizontal_bounds
                    or task.text_priority
                ),
            )
            if (
                task.protect_horizontal_bounds
                and not task.preserve_horizontal_bounds
                and not task.text_priority
            ):
                segment = _erase_corner_page_badges(segment)
            if task.text_priority and not task.preserve_horizontal_bounds:
                segment = _trim_text_priority_bottom_page_badge(segment)
        left_padding_px = 0
        top_padding_px = 0
        if task.pad_edges:
            horizontal_safe_padding_px = max(
                task.horizontal_safe_padding_px,
                (
                    TEXT_PRIORITY_CROP_HORIZONTAL_SAFE_PADDING_PX
                    if task.text_priority or task.protect_horizontal_bounds
                    else 0
                ),
            )
            left_padding_px = horizontal_safe_padding_px
            top_padding_px = PROBLEM_CROP_TOP_SAFE_PADDING_PX
            segment = _pad_problem_crop_edges(
                segment,
                left_padding_px=horizontal_safe_padding_px,
                right_padding_px=horizontal_safe_padding_px,
            )
            # A short source crop can make a normal page badge look too tall
            # in ratio terms. Recheck after safety padding establishes the
            # exported crop geometry; real answer rows remain protected by
            # the same corner, width, and isolation requirements.
            if task.text_priority and not task.preserve_horizontal_bounds:
                segment = _trim_text_priority_bottom_page_badge(segment)
            if (
                (task.text_priority or task.protect_horizontal_bounds)
                and not task.preserve_horizontal_bounds
            ):
                segment = _erase_text_priority_unpaired_outer_vertical_guide(segment)
        flattened = _flatten_passage_segment_on_white(segment)
        mapped_regions = _map_source_media_regions_to_crop(
            task.source_media_regions,
            source_crop_rect=source_crop_rect,
            horizontal_trim_px=horizontal_trim_px,
            left_padding_px=left_padding_px,
            top_padding_px=top_padding_px,
            output_size=flattened.size,
        )
        return flattened, mapped_regions

    if task.segment_bounds:
        segment_payloads = [
            # Passage segmenters already leave explicit top/bottom safety
            # space. A second unconditional +/-12 px recovery can reach above
            # the continuation text or below the lower separator/next problem.
            crop_segment(
                bounds,
                recover_vertical_edges=False,
                column_index=(
                    task.segment_column_indices[index]
                    if task.segment_column_indices and index < len(task.segment_column_indices)
                    else None
                ),
            )
            for index, bounds in enumerate(task.segment_bounds)
        ]
        stitch_placements: list[dict[str, int]] = []
        crop = _compose_passage_segments(
            [payload[0] for payload in segment_payloads],
            transparent=False,
            placements_output=stitch_placements,
        )
        task.rendered_media_regions = _media_regions_after_stitch(
            [payload[1] for payload in segment_payloads],
            stitch_placements,
        )
    else:
        crop, task.rendered_media_regions = crop_segment(
            task.bounds,
            column_index=(task.segment_column_indices or (None,))[0],
        )
    task.crop_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(
        task.crop_path,
        format="PNG",
        compress_level=INTERMEDIATE_PNG_COMPRESSION_LEVEL,
        optimize=False,
    )
    _render_problem_board_asset(crop, task)
    return crop.size


def _problem_asset_task_dedup_key(task: _ProblemAssetTask) -> tuple[Any, ...]:
    def box_key(bounds: Box) -> tuple[float, float, float, float]:
        return (
            round(float(bounds.left), 4),
            round(float(bounds.top), 4),
            round(float(bounds.width), 4),
            round(float(bounds.height), 4),
        )

    media_key = json.dumps(
        list(task.source_media_regions),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return (
        id(task.source_image),
        task.source_image.mode,
        task.source_image.size,
        box_key(task.bounds),
        tuple(box_key(bounds) for bounds in (task.segment_bounds or ())),
        tuple(task.segment_column_indices or ()),
        task.text_payload,
        task.text_title,
        task.chalk_color,
        task.trim_edge_guides,
        task.preserve_horizontal_bounds,
        task.protect_horizontal_bounds,
        task.horizontal_safe_padding_px,
        task.text_priority,
        task.pad_edges,
        task.subject.value if isinstance(task.subject, Subject) else str(task.subject),
        _source_hints_suggest_math(*task.source_hints),
        media_key,
    )


def _copy_problem_asset(source: Path, destination: Path) -> None:
    if source == destination:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def _render_problem_assets(tasks: list[_ProblemAssetTask]) -> list[tuple[int, int]]:
    if not tasks:
        return []

    canonical_tasks: list[_ProblemAssetTask] = []
    canonical_index_by_key: dict[tuple[Any, ...], int] = {}
    canonical_index_by_task: list[int] = []
    for task in tasks:
        key = _problem_asset_task_dedup_key(task)
        canonical_index = canonical_index_by_key.get(key)
        if canonical_index is None:
            canonical_index = len(canonical_tasks)
            canonical_index_by_key[key] = canonical_index
            canonical_tasks.append(task)
        canonical_index_by_task.append(canonical_index)

    worker_count = _resolve_problem_asset_worker_count(len(canonical_tasks))
    if worker_count <= 1:
        canonical_sizes = [_render_problem_asset(task) for task in canonical_tasks]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            canonical_sizes = list(executor.map(_render_problem_asset, canonical_tasks))

    for task, canonical_index in zip(tasks, canonical_index_by_task):
        canonical = canonical_tasks[canonical_index]
        if task is canonical:
            continue
        _copy_problem_asset(canonical.crop_path, task.crop_path)
        _copy_problem_asset(canonical.board_render_path, task.board_render_path)
        task.rendered_media_regions = [
            dict(region)
            for region in canonical.rendered_media_regions
        ]
    return [canonical_sizes[index] for index in canonical_index_by_task]


def resolve_subject(name: str | None) -> Subject:
    if not name:
        return Subject.UNKNOWN
    try:
        return Subject(name.lower())
    except ValueError:
        return Subject.UNKNOWN


def iter_problem_block_ids(page: PageModel, problem: ProblemUnit) -> list[str]:
    ordered: list[str] = []
    for block_id in (
        *problem.stem_block_ids,
        *problem.choice_block_ids,
        *problem.explanation_block_ids,
        *problem.figure_block_ids,
    ):
        if block_id not in ordered:
            ordered.append(block_id)
    if ordered:
        return ordered
    return [block.block_id for block in page.blocks]


def merge_boxes(
    boxes: list[Box],
    *,
    page_width: int,
    page_height: int,
    padding_px: int = PROBLEM_PADDING_PX,
    top_padding_px: int | float | None = None,
    bottom_padding_px: int | float | None = None,
) -> Box:
    left = min(box.left for box in boxes)
    top = min(box.top for box in boxes)
    right = max(box.right for box in boxes)
    bottom = max(box.bottom for box in boxes)
    resolved_top_padding = padding_px if top_padding_px is None else top_padding_px
    resolved_bottom_padding = padding_px if bottom_padding_px is None else bottom_padding_px
    return Box.from_points(
        max(0.0, left - float(padding_px)),
        max(0.0, top - float(resolved_top_padding)),
        min(float(page_width), right + float(padding_px)),
        min(float(page_height), bottom + float(resolved_bottom_padding)),
    )


def estimate_height_pages(image_size: tuple[int, int], template: LayoutTemplate) -> float:
    width_px, height_px = image_size
    available_width_px = CANVAS_HEIGHT * template.fixed_left_zone_ratio - LEFT_MARGIN_PX - RIGHT_PADDING_PX
    scaled_height_px = available_width_px * (height_px / max(width_px, 1))
    estimated = scaled_height_px / CANVAS_WIDTH
    return max(MIN_HEIGHT_PAGES, min(MAX_HEIGHT_PAGES, estimated))


def estimate_page_as_is_height_pages(image_size: tuple[int, int], template: LayoutTemplate) -> float:
    width_px, height_px = image_size
    display_width_px = _v1_default_display_width_px(template)
    scaled_height_px = display_width_px * (height_px / max(width_px, 1))
    estimated = scaled_height_px / CANVAS_WIDTH
    return max(MIN_HEIGHT_PAGES, min(MAX_HEIGHT_PAGES, estimated))


def resolve_source_build_worker_count(
    source_count: int,
    *,
    input_intent: str,
    ocr_mode: str,
    ai_fallback_config: dict[str, Any] | None,
) -> int:
    if source_count <= 1:
        return 1
    if _normalize_input_intent(input_intent) == "page-as-is":
        max_workers = max(1, min(8, source_count, os.cpu_count() or 2))
        raw_worker_count = os.environ.get("EDB_PAGE_AS_IS_SOURCE_WORKERS", "").strip()
        if raw_worker_count:
            try:
                requested_workers = int(raw_worker_count)
            except ValueError:
                requested_workers = max_workers
            if requested_workers <= 0:
                return 1
            return max(1, min(max_workers, requested_workers))
        return max_workers
    return resolve_recognition_worker_count(
        source_count,
        ocr_mode=ocr_mode,
        ai_config=_to_page_ai_config(ai_fallback_config),
    )


def build_pages(
    source: str | Path,
    *,
    subject: Subject,
    ocr_mode: str,
    ai_fallback_config: dict[str, Any] | None,
    pdf_dpi: int,
    detect_perspective: bool,
    deskew: bool,
    crop_margins: bool,
    max_dimension: int | None,
    page_tile_mode: str = "off",
    debug_segments_dir: Path | None = None,
    input_intent: str = "auto",
    ocr_semaphore: threading.BoundedSemaphore | None = None,
    global_ocr_worker_limit: int | None = None,
) -> tuple[list[PreparedPage], list[PageModel]]:
    prepared_pages = prepare_source_pages(
        source,
        pdf_dpi=pdf_dpi,
        detect_perspective=detect_perspective,
        deskew=deskew,
        crop_margins=crop_margins,
        max_dimension=max_dimension,
    )
    if _normalize_input_intent(input_intent) == "page-as-is":
        prepared_pages = _tile_page_as_is_prepared_pages(
            prepared_pages,
            page_tile_mode=page_tile_mode,
        )
        return prepared_pages, _build_page_as_is_models(prepared_pages, subject=subject)

    page_ai_config = _to_page_ai_config(ai_fallback_config)
    page_models = build_page_models_for_prepared_pages(
        prepared_pages,
        subject=subject,
        ocr_mode=ocr_mode,
        ai_config=page_ai_config,
        ocr_semaphore=ocr_semaphore,
        global_ocr_worker_limit=global_ocr_worker_limit,
    )
    if debug_segments_dir is not None:
        for prepared_page, page in zip(prepared_pages, page_models):
            debug_path = debug_segments_dir / f"{page.page_id}_segments.png"
            draw_segment_debug(prepared_page, page.blocks, debug_path)
    return prepared_pages, page_models


PAGE_TILE_MODES = {"off", "auto"}


def _normalize_page_tile_mode(value: str | None) -> str:
    normalized = str(value or "off").strip().lower().replace("_", "-")
    return normalized if normalized in PAGE_TILE_MODES else "off"


def _tile_media_regions_for_box(
    regions: Sequence[Mapping[str, Any]],
    source_box: tuple[int, int, int, int],
) -> list[dict[str, Any]]:
    box_left, box_top, box_right, box_bottom = source_box
    mapped: list[dict[str, Any]] = []
    for region in regions:
        raw_bbox = region.get("bbox") if isinstance(region, Mapping) else None
        if not isinstance(raw_bbox, Mapping):
            continue
        try:
            left = float(raw_bbox.get("left", 0.0))
            top = float(raw_bbox.get("top", 0.0))
            right = float(raw_bbox.get("right", left + float(raw_bbox.get("width", 0.0))))
            bottom = float(raw_bbox.get("bottom", top + float(raw_bbox.get("height", 0.0))))
        except (TypeError, ValueError):
            continue
        intersection = (
            max(left, float(box_left)),
            max(top, float(box_top)),
            min(right, float(box_right)),
            min(bottom, float(box_bottom)),
        )
        if intersection[2] <= intersection[0] or intersection[3] <= intersection[1]:
            continue
        source_area = max(1.0, (right - left) * (bottom - top))
        intersection_area = (intersection[2] - intersection[0]) * (intersection[3] - intersection[1])
        if intersection_area / source_area < 0.8:
            continue
        local_left = intersection[0] - box_left
        local_top = intersection[1] - box_top
        local_right = intersection[2] - box_left
        local_bottom = intersection[3] - box_top
        payload = {key: value for key, value in region.items() if key != "bbox"}
        payload["bbox"] = {
            "left": local_left,
            "top": local_top,
            "right": local_right,
            "bottom": local_bottom,
            "width": local_right - local_left,
            "height": local_bottom - local_top,
        }
        mapped.append(payload)
    return mapped


def _tile_page_as_is_prepared_pages(
    prepared_pages: Sequence[PreparedPage],
    *,
    page_tile_mode: str,
) -> list[PreparedPage]:
    if _normalize_page_tile_mode(page_tile_mode) != "auto":
        return list(prepared_pages)

    tiled_pages: list[PreparedPage] = []
    options = PageTilingOptions(allow_center_vertical_rule=True)
    for prepared_page in prepared_pages:
        result = tile_page_columns(prepared_page.image, options)
        if not result.was_split:
            prepared_page.metadata["page_tile_mode"] = "auto"
            prepared_page.metadata["page_tile_split"] = False
            prepared_page.metadata["page_tile_reason"] = result.reason
            tiled_pages.append(prepared_page)
            continue

        tile_dir = Path(prepared_page.source_path).resolve().parent / "page_tiles"
        tile_dir.mkdir(parents=True, exist_ok=True)
        source_regions = tuple(
            region
            for region in (prepared_page.metadata.get("pdf_media_regions") or [])
            if isinstance(region, dict)
        )
        for tile_number, tile in enumerate(result.tiles, start=1):
            tile_id = f"{prepared_page.page_id}-tile-{tile_number:02d}"
            tile_path = tile_dir / f"{tile_id}.png"
            tile.image.save(tile_path, format="PNG")
            tile_metadata = {
                **dict(prepared_page.metadata),
                "page_tile_mode": "auto",
                "page_tile_split": True,
                "page_tile_reason": result.reason,
                "page_tile_confidence": result.confidence,
                "page_tile_index": tile_number,
                "page_tile_count": len(result.tiles),
                "page_tile_column": "left" if tile.column_index == 0 else "right",
                "page_tile_parent_page_id": prepared_page.page_id,
                "page_tile_parent_source_path": prepared_page.source_path,
                "page_tile_source_box": {
                    "left": tile.source_box[0],
                    "top": tile.source_box[1],
                    "right": tile.source_box[2],
                    "bottom": tile.source_box[3],
                    "width": tile.source_box[2] - tile.source_box[0],
                    "height": tile.source_box[3] - tile.source_box[1],
                },
                "page_tile_split_x": result.split_x,
                "page_tile_gutter_bounds": result.gutter_bounds,
                "pdf_media_regions": _tile_media_regions_for_box(source_regions, tile.source_box),
            }
            tiled_pages.append(
                PreparedPage(
                    page_id=tile_id,
                    source_path=str(tile_path.resolve()),
                    page_number=prepared_page.page_number,
                    image=tile.image,
                    original_size=tile.image.size,
                    metadata=tile_metadata,
                )
            )
    return tiled_pages


def _build_page_as_is_models(prepared_pages: list[PreparedPage], *, subject: Subject) -> list[PageModel]:
    pages: list[PageModel] = []
    for index, prepared_page in enumerate(prepared_pages, start=1):
        block_id = f"{prepared_page.page_id}-full-page"
        problem_id = f"{prepared_page.page_id}-problem-1"
        page_metadata = {
            **dict(prepared_page.metadata),
            "input_intent": "page-as-is",
            "grouping_source": "user_intent",
            "grouping_mode": "single_page",
            "forced_single_problem": True,
            "page_as_is_fast_path": True,
            "segmentation_skipped": True,
            "ocr_skipped": True,
            "ocr_skipped_reason": "page_as_is_fast_path",
        }
        block = ContentBlock(
            block_id=block_id,
            block_type=BlockType.IMAGE,
            bbox=Box(
                left=0.0,
                top=0.0,
                width=float(prepared_page.image.width),
                height=float(prepared_page.image.height),
            ),
            reading_order=0,
            confidence=1.0,
            metadata={
                "input_intent": "page-as-is",
                "force_image_record": True,
                "force_full_page_bounds": True,
                "segmentation_skipped": True,
                "ocr_skipped": True,
                "ocr_backend": "none",
                "ocr_skipped_reason": "page_as_is_fast_path",
            },
        )
        source_page_number = int(prepared_page.page_number or index)
        tile_count = int(prepared_page.metadata.get("page_tile_count") or 1)
        tile_column = str(prepared_page.metadata.get("page_tile_column") or "")
        title = f"페이지 {source_page_number}"
        if tile_count > 1:
            title += " · 왼쪽" if tile_column == "left" else " · 오른쪽"
        problem = ProblemUnit(
            unit_id=problem_id,
            subject=subject,
            title=title,
            stem_block_ids=[block_id],
            metadata={
                "grouping_source": "user_intent",
                "grouping_reason": ["page-as-is"],
                "force_full_page_bounds": True,
                "input_intent": "page-as-is",
                "bbox_px": {
                    "left": 0.0,
                    "top": 0.0,
                    "width": float(prepared_page.image.width),
                    "height": float(prepared_page.image.height),
                },
            },
        )
        pages.append(
            PageModel(
                page_id=prepared_page.page_id,
                width_px=prepared_page.image.width,
                height_px=prepared_page.image.height,
                subject=subject,
                source_path=prepared_page.source_path,
                blocks=[block],
                problems=[problem],
                metadata=page_metadata,
            )
        )
    return pages


def _force_single_problem_per_page(pages: list[PageModel], *, input_intent: str) -> list[PageModel]:
    forced_pages: list[PageModel] = []
    title_prefix = "페이지" if input_intent == "page-as-is" else "문항"

    for index, page in enumerate(pages, start=1):
        ordered_blocks = page.sorted_blocks()
        block_ids = [block.block_id for block in ordered_blocks]
        unit_metadata: dict[str, Any] = {}
        if input_intent == "page-as-is" and page.problems:
            unit_metadata.update(dict(page.problems[0].metadata))
        unit_metadata.update({
            "grouping_source": "user_intent",
            "grouping_reason": [input_intent],
            "force_full_page_bounds": True,
            "input_intent": input_intent,
        })
        if input_intent == "single-problem":
            unit_metadata["problem_number"] = index
            unit_metadata["problem_number_source"] = "user_intent"

        forced_problem = ProblemUnit(
            unit_id=f"{page.page_id}-problem-1",
            subject=page.subject,
            title=f"{title_prefix} {index}",
            stem_block_ids=block_ids,
            metadata=unit_metadata,
        )
        page_metadata = dict(page.metadata)
        page_metadata.pop("route_decision", None)
        page_metadata["input_intent"] = input_intent
        page_metadata["grouping_source"] = "user_intent"
        page_metadata["grouping_mode"] = "single_page"
        page_metadata["forced_single_problem"] = True
        forced_pages.append(
            PageModel(
                page_id=page.page_id,
                width_px=page.width_px,
                height_px=page.height_px,
                subject=page.subject,
                source_path=page.source_path,
                blocks=list(page.blocks),
                problems=[forced_problem],
                metadata=page_metadata,
            )
        )
    return forced_pages


def _iter_problem_block_ids_raw(problem: ProblemUnit) -> list[str]:
    """Like iter_problem_block_ids but without the page-level fallback —
    used for purely problem-owned blocks (for top-y bookkeeping)."""
    ordered: list[str] = []
    for block_id in (
        *problem.stem_block_ids,
        *problem.choice_block_ids,
        *problem.explanation_block_ids,
        *problem.figure_block_ids,
    ):
        if block_id not in ordered:
            ordered.append(block_id)
    return ordered


def _problem_top_y(problem: ProblemUnit, block_by_id: dict[str, ContentBlock]) -> float:
    ids = _iter_problem_block_ids_raw(problem)
    blocks = [block_by_id[bid] for bid in ids if bid in block_by_id]
    if not blocks:
        return 0.0
    return min(block.bbox.top for block in blocks)


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _coerce_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _problem_first_block(problem: ProblemUnit, block_by_id: dict[str, ContentBlock]) -> ContentBlock | None:
    ids = _iter_problem_block_ids_raw(problem)
    blocks = [block_by_id[bid] for bid in ids if bid in block_by_id]
    if not blocks:
        return None
    return min(blocks, key=lambda block: (block.bbox.top, block.bbox.left, block.reading_order))


def _problem_column_value(problem: ProblemUnit, block_by_id: dict[str, ContentBlock]) -> int | None:
    value = _coerce_int(problem.metadata.get("column_index"))
    if value is not None:
        return value
    first_block = _problem_first_block(problem, block_by_id)
    if first_block is None:
        return None
    return _coerce_int(first_block.metadata.get("column_index"))


def _block_column_value(block: ContentBlock) -> int | None:
    return _coerce_int(block.metadata.get("column_index"))


def _is_page_footer_chrome_block(page: PageModel, block: ContentBlock) -> bool:
    center_y = (block.bbox.top + block.bbox.bottom) / 2.0
    if center_y < float(page.height_px) * PAGE_FOOTER_CHROME_BAND_RATIO:
        return False

    normalized_text = re.sub(r"\s+", "", str(block.text or "")).lower()
    if any(marker.lower() in normalized_text for marker in PAGE_FOOTER_CHROME_TEXT_MARKERS):
        return True
    if re.fullmatch(r"\d+[/／]\d+", normalized_text):
        return True

    max_line_height = max(PAGE_FOOTER_CHROME_LINE_MAX_HEIGHT_PX, float(page.height_px) * 0.012)
    if (
        block.bbox.width >= float(page.width_px) * PAGE_FOOTER_CHROME_LINE_MIN_WIDTH_RATIO
        and block.bbox.height <= max_line_height
    ):
        return True
    return False


def _is_page_side_chrome_block(page: PageModel, block: ContentBlock) -> bool:
    page_width = float(page.width_px)
    page_height = float(page.height_px)
    if page_width <= 0 or page_height <= 0:
        return False

    near_left = block.bbox.left <= page_width * 0.035
    near_right = block.bbox.right >= page_width * 0.965
    if not (near_left or near_right):
        return False

    normalized_text = re.sub(r"\s+", "", str(block.text or "")).lower()
    narrow = block.bbox.width <= max(32.0, page_width * 0.13)
    tall_enough = block.bbox.height >= max(36.0, page_height * 0.06)
    if narrow and tall_enough and any(marker.lower() in normalized_text for marker in PAGE_SIDE_CHROME_TEXT_MARKERS):
        return True

    very_narrow = block.bbox.width <= max(10.0, page_width * 0.015)
    very_tall = block.bbox.height >= page_height * 0.40
    return very_narrow and very_tall


def _filter_page_chrome_blocks(page: PageModel, blocks: list[ContentBlock]) -> list[ContentBlock]:
    filtered = [
        block
        for block in blocks
        if not _is_page_footer_chrome_block(page, block)
        and not _is_page_side_chrome_block(page, block)
    ]
    return filtered if filtered else blocks


def _is_probable_passage_continuation_page_header(
    page: PageModel,
    block: ContentBlock,
) -> bool:
    """Reject a tiny top-of-column header mislabeled as passage continuation."""
    metadata = block.metadata
    if (
        metadata.get("segmenter") != "pdf-passage-range"
        or metadata.get("marker_kind") != "passage_continuation"
    ):
        return False
    line_count = _coerce_int(metadata.get("passage_text_line_count")) or 0
    character_count = _coerce_int(metadata.get("passage_text_character_count")) or 0
    return (
        block.bbox.top <= float(page.height_px) * 0.08
        and block.bbox.height <= float(page.height_px) * 0.08
        and line_count <= 1
        and character_count <= 12
    )


def _problem_band_value(problem: ProblemUnit, block_by_id: dict[str, ContentBlock]) -> int:
    value = _coerce_int(problem.metadata.get("question_band_index"))
    if value is not None:
        return value
    first_block = _problem_first_block(problem, block_by_id)
    if first_block is None:
        return 0
    return _coerce_int(first_block.metadata.get("question_band_index")) or 0


def _problem_left_x(problem: ProblemUnit, block_by_id: dict[str, ContentBlock]) -> float:
    first_block = _problem_first_block(problem, block_by_id)
    return first_block.bbox.left if first_block is not None else 0.0


def _problem_order_key(problem: ProblemUnit, block_by_id: dict[str, ContentBlock]) -> tuple[object, ...]:
    if _problem_is_passage_fragment_unit(problem):
        passage_range = _passage_range_tuple(problem.metadata)
        if passage_range is not None:
            return (0, passage_range[0], 0, problem.unit_id)

    raw_number = problem.metadata.get("problem_number")
    if isinstance(raw_number, int):
        return (0, raw_number, 1, problem.unit_id)
    if isinstance(raw_number, str) and raw_number.isdigit():
        return (0, int(raw_number), 1, problem.unit_id)

    column_value = _problem_column_value(problem, block_by_id) or 0
    band_value = _problem_band_value(problem, block_by_id)
    return (1, column_value, band_value, _problem_top_y(problem, block_by_id), problem.unit_id)


def _problem_visual_order_key(problem: ProblemUnit, block_by_id: dict[str, ContentBlock]) -> tuple[object, ...]:
    column_value = _problem_column_value(problem, block_by_id) or 0
    band_value = _problem_band_value(problem, block_by_id)
    return (
        column_value,
        band_value,
        _problem_top_y(problem, block_by_id),
        _problem_left_x(problem, block_by_id),
        problem.unit_id,
    )


def _build_crop_next_problem_map(
    problems: list[ProblemUnit],
    block_by_id: dict[str, ContentBlock],
) -> dict[str, ProblemUnit]:
    """Map each problem to the next problem in the same detected column.

    Final output remains number-ordered, but crop boundaries are spatial: in a
    two-column worksheet, problem 9 should not use problem 10's top as its
    lower boundary just because 10 is the next number.
    """
    grouped: dict[int | None, list[ProblemUnit]] = {}
    for problem in problems:
        grouped.setdefault(_problem_column_value(problem, block_by_id), []).append(problem)

    next_by_id: dict[str, ProblemUnit] = {}
    for group in grouped.values():
        spatial_order = sorted(
            group,
            key=lambda problem: (
                _problem_top_y(problem, block_by_id),
                _problem_band_value(problem, block_by_id),
                _problem_left_x(problem, block_by_id),
                problem.unit_id,
            ),
        )
        for current, next_problem in zip(spatial_order, spatial_order[1:]):
            next_by_id[current.unit_id] = next_problem
    return next_by_id


def _expand_problem_blocks_by_gap(
    page: PageModel,
    problem: ProblemUnit,
    next_problem: ProblemUnit | None,
    block_by_id: dict[str, ContentBlock],
    other_problem_block_ids: set[str],
) -> list[ContentBlock]:
    """Include every page block whose vertical centre falls between this
    problem's start and the next problem's start.

    The grouping pass leaves choice / figure blocks orphaned when segmentation
    splits them into separate bands or columns. Filling the vertical gap
    between consecutive problem-starts ensures the crop captures the full
    question + figure + choices set.
    """
    own_ids = set(_iter_problem_block_ids_raw(problem))
    own_blocks = [block_by_id[bid] for bid in own_ids if bid in block_by_id]
    if not own_blocks:
        return []

    start_y = min(block.bbox.top for block in own_blocks)
    if next_problem is None:
        end_y = float(page.height_px)
    else:
        next_top = _problem_top_y(next_problem, block_by_id)
        end_y = next_top if next_top > start_y else float(page.height_px)

    problem_column = _problem_column_value(problem, block_by_id)
    included: list[ContentBlock] = []
    for block in page.blocks:
        if block.block_id in own_ids:
            included.append(block)
            continue
        if block.block_id in other_problem_block_ids:
            continue
        block_column = _block_column_value(block)
        if problem_column is not None and block_column is not None and block_column != problem_column:
            continue
        centre = (block.bbox.top + block.bbox.bottom) / 2.0
        if start_y <= centre < end_y:
            included.append(block)
    return included


def _clamp_box_to_next_problem(
    box: Box,
    next_problem: ProblemUnit | None,
    block_by_id: dict[str, ContentBlock],
    *,
    min_bottom: float | None = None,
) -> Box:
    if next_problem is None:
        return box
    next_top = _problem_top_y(next_problem, block_by_id)
    if next_top <= box.top + 1.0:
        return box
    limit = max(box.top + 1.0, next_top - DOCUMENT_BAND_NEXT_PROBLEM_GAP_PX)
    if min_bottom is not None:
        limit = max(limit, min(float(min_bottom), box.bottom))
    if box.bottom <= limit:
        return box
    return Box.from_points(box.left, box.top, box.right, limit)


def _problem_metadata_bbox(problem: ProblemUnit, page: PageModel) -> Box | None:
    # Passage fragments already own the exact shared-passage block(s). AI
    # problem grouping describes child questions and can attach the first
    # child's bbox to the supplemental passage unit. Treating that bbox as a
    # crop override turns the passage-only preview into a blank/question strip.
    if str(problem.metadata.get("passage_role") or "").strip() == "passage_fragment":
        return None
    raw = problem.metadata.get("bbox_px")
    if not isinstance(raw, dict):
        ai_unit = problem.metadata.get("ai_problem_unit")
        if isinstance(ai_unit, dict):
            raw = ai_unit.get("bbox_px")
    if not isinstance(raw, dict):
        return None

    try:
        left = float(raw.get("left", 0.0))
        top = float(raw.get("top", 0.0))
        width = float(raw.get("width", 0.0))
        height = float(raw.get("height", 0.0))
    except (TypeError, ValueError):
        return None
    if width <= 1.0 or height <= 1.0:
        return None

    right = min(float(page.width_px), max(0.0, left + width))
    bottom = min(float(page.height_px), max(0.0, top + height))
    left = max(0.0, min(float(page.width_px), left))
    top = max(0.0, min(float(page.height_px), top))
    if right <= left + 1.0 or bottom <= top + 1.0:
        return None
    return Box.from_points(left, top, right, bottom)


def _should_prefer_problem_metadata_bbox(problem: ProblemUnit) -> bool:
    if problem.metadata.get("grouping_source") != "ai_fallback":
        return False
    flags: list[str] = []
    raw_flags = problem.metadata.get("review_flags")
    if isinstance(raw_flags, list):
        flags.extend(str(flag) for flag in raw_flags)
    ai_unit = problem.metadata.get("ai_problem_unit")
    if isinstance(ai_unit, dict) and isinstance(ai_unit.get("review_flags"), list):
        flags.extend(str(flag) for flag in ai_unit["review_flags"])
    return "uncertain_bbox" not in {flag.strip() for flag in flags}


def _is_trusted_pdf_text_marker_problem(problem: ProblemUnit, blocks: Sequence[ContentBlock]) -> bool:
    if problem.metadata.get("problem_number_source") == "pdf_text_marker":
        return True
    return any(
        block.metadata.get("segmenter") == "pdf-text-markers"
        and block.metadata.get("problem_number_source") == "pdf_text_marker"
        for block in blocks
    )


def _append_problem_block_ids(target: list[str], values: Sequence[str]) -> None:
    for value in values:
        if value and value not in target:
            target.append(value)


def _passage_range_label(metadata: dict[str, Any]) -> str:
    passage_range = _passage_range_tuple(metadata)
    if passage_range is None:
        return ""
    start, end = passage_range
    return str(start) if start == end else f"{start}~{end}"


def _mark_pre_question_passage_continuations(
    target: ProblemUnit,
    continuations: Sequence[ProblemUnit],
) -> None:
    passage_range = _passage_range_tuple(target.metadata)
    target_number = _problem_metadata_number(target)
    if passage_range is None and target_number is not None:
        passage_range = (target_number, target_number)

    group_id = str(target.metadata.get("passage_group_id") or "").strip()
    if not group_id and passage_range is not None:
        start, end = passage_range
        group_id = f"pre-question-passage-{start}-{end}"

    child_numbers: list[int] = []
    if passage_range is not None:
        start, end = passage_range
        child_numbers = _passage_child_numbers(target.metadata, start, end)

    for continuation in continuations:
        if group_id:
            continuation.metadata.setdefault("passage_group_id", group_id)
        if passage_range is not None:
            start, end = passage_range
            continuation.metadata.setdefault("passage_range", {"start": start, "end": end})
        if child_numbers:
            continuation.metadata.setdefault("passage_child_problem_numbers", list(child_numbers))
        for key in (
            "passage_source_page_ids",
            "passage_continues_across_pages",
            "passage_fragment_count",
        ):
            if key in target.metadata:
                continuation.metadata.setdefault(key, target.metadata[key])
        continuation.metadata["passage_role"] = "passage_fragment"
        continuation.metadata["supplemental_item"] = True
        continuation.metadata["passage_fragment_source"] = "pre_question_continuation"
        continuation.metadata["passage_pre_question_continuation"] = True
        if not continuation.title or GENERIC_PROBLEM_TITLE_RE.match(str(continuation.title)):
            range_label = _passage_range_label(continuation.metadata)
            continuation.title = f"지문 {range_label}".strip() if range_label else "지문"


def _problem_has_number(problem: ProblemUnit) -> bool:
    raw = problem.metadata.get("problem_number")
    return (isinstance(raw, int) and raw >= 1) or (isinstance(raw, str) and raw.isdigit())


def _problem_is_passage_fragment_unit(problem: ProblemUnit) -> bool:
    return str(problem.metadata.get("passage_role") or "").strip() == "passage_fragment"


def _problem_allows_selective_media_preservation(problem: ProblemUnit) -> bool:
    """Keep shared child questions on the legacy Stage-2 treatment."""
    return str(problem.metadata.get("passage_role") or "").strip() != "child_question"


def _problem_is_passage_scoped_unit(problem: ProblemUnit) -> bool:
    role = str(problem.metadata.get("passage_role") or "").strip()
    return role in {"child_question", "passage_fragment"} or bool(problem.metadata.get("passage_group_id"))


def _problem_entry_title(problem: ProblemUnit, problem_number: int | None, entry_index: int) -> str:
    if _problem_is_passage_fragment_unit(problem):
        range_label = _passage_range_label(problem.metadata)
        if range_label:
            return f"지문 {range_label}"
        return str(problem.title or "지문")
    return problem.title or (f"\ubb38\ud56d {problem_number}" if problem_number is not None else f"\ubb38\ud56d {entry_index}")


def _problem_is_spatially_before(
    candidate: ProblemUnit,
    target: ProblemUnit,
    block_by_id: dict[str, ContentBlock],
) -> bool:
    target_top = _problem_top_y(target, block_by_id)
    candidate_top = _problem_top_y(candidate, block_by_id)
    if candidate_top >= target_top:
        return False
    target_column = _problem_column_value(target, block_by_id)
    candidate_column = _problem_column_value(candidate, block_by_id)
    return target_column is None or candidate_column is None or target_column == candidate_column


def _drop_pre_first_problem_headers(
    problems: list[ProblemUnit],
    block_by_id: dict[str, ContentBlock],
) -> list[ProblemUnit]:
    """Strip page-chrome ProblemUnits that precede the first numbered problem.

    Cover-page titles, exam form fields (성명 / 수험번호), and subject
    headers (물리학I / 과학탐구 / 수학II) often surface as their own
    ProblemUnits — especially in fallback grouping where every band becomes a
    pseudo-problem. They land above the first real numbered problem and have
    no ``problem_number``, so we trim them. If the page has no numbered
    problem at all we leave the list untouched: that's the best-effort
    behaviour while OCR is unavailable.
    """
    if not problems:
        return problems

    first_numbered_index: int | None = None
    for index, problem in enumerate(problems):
        if _problem_has_number(problem):
            first_numbered_index = index
            break

    if first_numbered_index is None:
        return problems

    first_numbered = problems[first_numbered_index]
    first_passage_range = _passage_range_tuple(first_numbered.metadata)
    first_problem_number = _problem_metadata_number(first_numbered)
    if (
        _problem_passage_continues_across_pages(first_numbered.metadata)
        and first_passage_range is not None
        and first_problem_number == first_passage_range[0]
    ):
        continuation_problems = [
            problem
            for index, problem in enumerate(problems)
            if index != first_numbered_index
            and not _problem_has_number(problem)
            and _problem_is_spatially_before(problem, first_numbered, block_by_id)
        ]
        if continuation_problems:
            _mark_pre_question_passage_continuations(first_numbered, continuation_problems)
            return problems

    if first_numbered_index == 0:
        return problems

    preserved_prefix = [
        problem
        for problem in problems[:first_numbered_index]
        if _problem_is_passage_fragment_unit(problem)
    ]
    return preserved_prefix + problems[first_numbered_index:]


def _problem_metadata_number(problem: ProblemUnit) -> int | None:
    raw = problem.metadata.get("problem_number")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw >= 1:
        return raw
    if isinstance(raw, str) and raw.isdigit():
        number = int(raw)
        return number if number >= 1 else None
    return None


def _page_has_own_pdf_problem_markers(metadata: dict[str, Any]) -> bool:
    if str(metadata.get("segmenter") or "") == "pdf-text-markers":
        return True
    marker_count = _coerce_int(metadata.get("pdf_text_marker_count"))
    if marker_count is not None and marker_count > 0:
        return True
    markers = metadata.get("pdf_problem_markers")
    return isinstance(markers, list) and len(markers) > 0


def _hwp_conversion_has_pdf_problem_markers(metadata: dict[str, Any]) -> bool:
    quality = metadata.get("hwp_conversion_quality")
    if not isinstance(quality, dict):
        return False
    if "pdf_text_markers_reliable" in quality:
        return bool(quality.get("pdf_text_markers_reliable"))
    if bool(quality.get("has_pdf_text_markers")):
        return True
    marker_count = _coerce_int(quality.get("pdf_text_marker_count"))
    if marker_count is not None and marker_count > 0:
        return True
    hwp_layout_marker_count = _coerce_int(quality.get("hwp_layout_problem_marker_count"))
    return hwp_layout_marker_count is not None and hwp_layout_marker_count > 0


def _is_document_band_problem(
    problem: ProblemUnit,
    block_by_id: dict[str, ContentBlock],
) -> bool:
    block_ids = _iter_problem_block_ids_raw(problem)
    if not block_ids:
        return False
    blocks = [block_by_id[block_id] for block_id in block_ids if block_id in block_by_id]
    if not blocks:
        return False
    return all(
        block.metadata.get("segmenter") == "document-bands"
        or "question_band_index" in block.metadata
        for block in blocks
    )


def _is_marker_document_continuation_page(
    page: PageModel,
    problems: list[ProblemUnit],
    block_by_id: dict[str, ContentBlock],
) -> bool:
    if not problems:
        return False
    if not _hwp_conversion_has_pdf_problem_markers(page.metadata):
        return False
    if _page_has_own_pdf_problem_markers(page.metadata):
        return False
    if any(_problem_metadata_number(problem) is not None for problem in problems):
        return False
    return all(_is_document_band_problem(problem, block_by_id) for problem in problems)


def _problem_has_fallback_grouping(problem: ProblemUnit) -> bool:
    return bool(problem.metadata.get("fallback_grouping"))


def _continuation_block_ids_by_role(page: PageModel) -> tuple[list[str], list[str], list[str], list[str]]:
    stem_ids: list[str] = []
    choice_ids: list[str] = []
    explanation_ids: list[str] = []
    figure_ids: list[str] = []

    for block in page.sorted_blocks():
        if block.block_type == BlockType.CHOICE:
            choice_ids.append(block.block_id)
        elif block.block_type == BlockType.EXPLANATION:
            explanation_ids.append(block.block_id)
        elif block.block_type in IMAGE_ONLY_BLOCK_TYPES:
            figure_ids.append(block.block_id)
        else:
            stem_ids.append(block.block_id)

    return stem_ids, choice_ids, explanation_ids, figure_ids


def _collapse_marker_document_continuation_page(
    page: PageModel,
    problems: list[ProblemUnit],
    block_by_id: dict[str, ContentBlock],
) -> list[ProblemUnit] | None:
    if not _is_marker_document_continuation_page(page, problems, block_by_id):
        return None

    page.metadata["marker_document_continuation_detected"] = True
    if (
        len(problems) == 1
        and problems[0].unit_id == f"{page.page_id}-continuation"
        and _is_marker_document_continuation_problem(problems[0])
    ):
        page.metadata["marker_document_continuation_preserved"] = True
        page.metadata.pop("problem_entry_skip_reason", None)
        page.metadata.pop("problem_crop_skip_reason", None)
        return problems

    if not any(_problem_has_fallback_grouping(problem) for problem in problems):
        page.metadata["problem_entry_skip_reason"] = "marker_document_continuation"
        return []

    stem_ids, choice_ids, explanation_ids, figure_ids = _continuation_block_ids_by_role(page)
    if not any((stem_ids, choice_ids, explanation_ids, figure_ids)):
        page.metadata["problem_entry_skip_reason"] = "marker_document_continuation"
        return []

    page.metadata["marker_document_continuation_preserved"] = True
    page.metadata.pop("problem_entry_skip_reason", None)
    page.metadata.pop("problem_crop_skip_reason", None)
    return [
        ProblemUnit(
            unit_id=f"{page.page_id}-continuation",
            subject=page.subject,
            title="이어지는 자료",
            stem_block_ids=stem_ids,
            choice_block_ids=choice_ids,
            explanation_block_ids=explanation_ids,
            figure_block_ids=figure_ids,
            metadata={
                "fallback_grouping": True,
                "grouping_mode": "fallback",
                "grouping_source": "marker_document_continuation",
                "marker_document_continuation": True,
                "source_problem_ids": [problem.unit_id for problem in problems],
                "force_full_page_bounds": True,
                "bbox_px": {
                    "left": 0.0,
                    "top": 0.0,
                    "width": float(page.width_px),
                    "height": float(page.height_px),
                },
                "risk_flags": ["marker_document_continuation"],
            },
        )
    ]


def _marker_document_dedupe_scope(page: PageModel) -> str:
    for key in ("source_hwp_path", "original_source_path", "source_pdf_path", "converted_pdf_path"):
        value = page.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(page.source_path or "__unknown_source__")


def _remove_duplicate_marker_document_problem_numbers(pages: list[PageModel]) -> None:
    # Repeated visible problem numbers are valid in official source documents
    # that contain alternate sections. Preserve page/file order and keep the
    # duplicate-number fact as metadata only; do not drop later pages/problems.
    seen_by_scope: dict[str, set[int]] = {}
    duplicate_scopes: set[str] = set()
    for page in pages:
        if not _hwp_conversion_has_pdf_problem_markers(page.metadata):
            continue
        scope = _marker_document_dedupe_scope(page)
        seen = seen_by_scope.setdefault(scope, set())
        for problem in page.problems:
            number = _problem_metadata_number(problem)
            if number is not None and number in seen:
                duplicate_scopes.add(scope)
            if number is not None:
                seen.add(number)
    if not duplicate_scopes:
        return
    for page in pages:
        if (
            _hwp_conversion_has_pdf_problem_markers(page.metadata)
            and _marker_document_dedupe_scope(page) in duplicate_scopes
        ):
            page.metadata["duplicate_problem_numbers_preserved"] = True
            page.metadata.pop("duplicate_problem_numbers_skipped", None)


def _has_hwp_template_instruction_text(page: PageModel) -> bool:
    if page.metadata.get("source_type") != "hwp":
        return False
    text = str(page.metadata.get("hwp_preview_text") or "")
    if not text:
        return False
    markers = (
        "개요 번호 모양",
        "Ctrl+3",
        "Ctrl+4",
        "복사 붙여넣",
        "위 네모칸",
    )
    return sum(1 for marker in markers if marker in text) >= 2


def _remove_hwp_template_instruction_problems(pages: list[PageModel]) -> None:
    for page in pages:
        if not _has_hwp_template_instruction_text(page):
            continue
        if _hwp_conversion_has_pdf_problem_markers(page.metadata):
            continue
        if len(page.problems) < 2:
            continue

        block_by_id = {block.block_id: block for block in page.blocks}
        ordered = sorted(page.problems, key=lambda problem: _problem_order_key(problem, block_by_id))
        retained: list[ProblemUnit] = []
        skipped_ids: list[str] = []
        skipped_tops: list[float] = []
        for index, problem in enumerate(ordered):
            if index == 0:
                retained.append(problem)
                continue
            if _problem_metadata_number(problem) is not None:
                retained.append(problem)
                continue
            if not _is_document_band_problem(problem, block_by_id):
                retained.append(problem)
                continue
            for block_id in _iter_problem_block_ids_raw(problem):
                block = block_by_id.get(block_id)
                if block is not None:
                    skipped_tops.append(block.bbox.top)
            skipped_ids.append(problem.unit_id)

        if skipped_ids:
            if retained and skipped_tops:
                first_problem = retained[0]
                first_blocks = [
                    block_by_id[block_id]
                    for block_id in _iter_problem_block_ids_raw(first_problem)
                    if block_id in block_by_id
                ]
                if first_blocks:
                    crop_bottom = max(block.bbox.bottom for block in first_blocks)
                    first_skipped_top = min(skipped_tops)
                    if first_skipped_top > crop_bottom + 8.0:
                        left = min(block.bbox.left for block in first_blocks)
                        top = min(block.bbox.top for block in first_blocks)
                        right = max(block.bbox.right for block in first_blocks)
                        first_problem.metadata["bbox_px"] = {
                            "left": round(max(0.0, left), 2),
                            "top": round(max(0.0, top), 2),
                            "width": round(min(float(page.width_px), right) - max(0.0, left), 2),
                            "height": round(min(float(page.height_px), first_skipped_top) - max(0.0, top), 2),
                        }
                        first_problem.metadata["bbox_source"] = "hwp_template_instruction_boundary"
            page.problems = retained
            page.metadata["template_instruction_problem_ids_skipped"] = skipped_ids


def _fill_missing_problem_numbers(problems: list[ProblemUnit]) -> None:
    """Patch in problem_number for entries whose marker OCR failed.

    Strategy: when leading problems have no number but later ones do,
    extrapolate backwards from the first known number. Forward-fill
    sequential gaps from the previous known number + 1.
    """
    fillable_indexes: list[int] = []
    numbers: list[int | None] = []
    for index, problem in enumerate(problems):
        if _problem_is_passage_fragment_unit(problem):
            continue
        fillable_indexes.append(index)
        raw = problem.metadata.get("problem_number")
        if isinstance(raw, int):
            numbers.append(raw)
        elif isinstance(raw, str) and raw.isdigit():
            numbers.append(int(raw))
        else:
            numbers.append(None)

    if not any(n is not None for n in numbers):
        return

    first_known_index = next((i for i, n in enumerate(numbers) if n is not None), None)
    if first_known_index is not None and first_known_index > 0:
        anchor = numbers[first_known_index]
        for offset in range(1, first_known_index + 1):
            candidate = anchor - offset
            if candidate >= 1:
                numbers[first_known_index - offset] = candidate

    for index in range(1, len(numbers)):
        if numbers[index] is None and numbers[index - 1] is not None:
            numbers[index] = numbers[index - 1] + 1

    for problem_index, number in zip(fillable_indexes, numbers):
        if number is None:
            continue
        problem = problems[problem_index]
        existing = problem.metadata.get("problem_number")
        if isinstance(existing, int):
            continue
        if isinstance(existing, str) and existing.isdigit():
            continue
        problem.metadata["problem_number"] = number
        problem.metadata.setdefault("problem_number_source", "inferred_sequence")


def _metadata_int_list(metadata: dict[str, Any], key: str) -> list[int]:
    values = metadata.get(key)
    if not isinstance(values, list):
        return []
    numbers: list[int] = []
    for value in values:
        number = _coerce_int(value)
        if number is not None and number >= 1:
            numbers.append(number)
    return list(dict.fromkeys(numbers))


def _hwp_text_problem_snippets_by_number(page: PageModel) -> dict[int, str]:
    quality = page.metadata.get("hwp_conversion_quality")
    if not isinstance(quality, dict):
        return {}
    snippets = quality.get("hwp_text_problem_snippets")
    if not isinstance(snippets, list):
        return {}
    by_number: dict[int, str] = {}
    for item in snippets:
        if not isinstance(item, dict):
            continue
        number = _coerce_int(item.get("number"))
        text = str(item.get("text") or "").strip()
        if number is None or number < 1 or not text:
            continue
        by_number.setdefault(number, text)
    return by_number


def _hwp_text_fallback_bbox(page: PageModel, number: int) -> Box:
    markers = page.metadata.get("pdf_problem_markers")
    if isinstance(markers, list):
        for marker in markers:
            if not isinstance(marker, dict) or _coerce_int(marker.get("number")) != number:
                continue
            bbox = marker.get("bbox")
            if not isinstance(bbox, dict):
                continue
            left = _coerce_float(bbox.get("left")) or 0.0
            top = _coerce_float(bbox.get("top")) or 0.0
            width = _coerce_float(bbox.get("width")) or 0.0
            height = _coerce_float(bbox.get("height")) or 0.0
            left = max(0.0, min(float(page.width_px), left))
            top = max(0.0, min(float(page.height_px) - 1.0, top))
            if width > 1.0 and height > 1.0:
                return Box(
                    left=left,
                    top=top,
                    width=min(float(page.width_px) - left, width),
                    height=max(1.0, min(float(page.height_px) - top, height)),
                )
    return Box(
        left=0.0,
        top=0.0,
        width=float(page.width_px),
        height=min(float(page.height_px), 640.0),
    )


def _restore_hwp_text_fallback_problems(pages: list[PageModel]) -> None:
    pages_by_scope: dict[str, list[PageModel]] = {}
    for page in pages:
        if page.metadata.get("source_type") != "hwp":
            continue
        pages_by_scope.setdefault(_marker_document_dedupe_scope(page), []).append(page)

    for scoped_pages in pages_by_scope.values():
        snippets_by_number: dict[int, str] = {}
        detected_numbers: set[int] = set()
        for page in scoped_pages:
            snippets_by_number.update(_hwp_text_problem_snippets_by_number(page))
            for problem in page.problems:
                number = _problem_metadata_number(problem)
                if number is not None:
                    detected_numbers.add(number)

        if not snippets_by_number:
            continue

        for page in scoped_pages:
            restored_numbers: list[int] = []
            for number in _metadata_int_list(page.metadata, "ignored_tiny_pdf_marker_numbers"):
                if number in detected_numbers:
                    continue
                snippet = snippets_by_number.get(number)
                if not snippet:
                    continue
                block_id = f"{page.page_id}-hwp-text-fallback-{number:03d}"
                if any(block.block_id == block_id for block in page.blocks):
                    continue

                fallback_block = ContentBlock(
                    block_id=block_id,
                    block_type=BlockType.STEM,
                    bbox=_hwp_text_fallback_bbox(page, number),
                    reading_order=len(page.blocks),
                    text=snippet,
                    confidence=1.0,
                    metadata={
                        "problem_number": number,
                        "problem_number_source": "hwp_text_snippet",
                        "force_problem_start": True,
                        "force_image_record": True,
                        "hwp_text_fallback_problem": True,
                    },
                )
                page.blocks.append(fallback_block)
                page.problems.append(
                    ProblemUnit(
                        unit_id=f"{page.page_id}-hwp-text-problem-{number:03d}",
                        subject=page.subject,
                        title=f"{number}.",
                        stem_block_ids=[block_id],
                        metadata={
                            "problem_number": number,
                            "problem_number_source": "hwp_text_snippet",
                            "hwp_text_fallback_problem": True,
                            "hwp_text_fallback_text": snippet,
                            "risk_flags": [HWP_TEXT_FALLBACK_RISK_FLAG],
                        },
                    )
                )
                detected_numbers.add(number)
                restored_numbers.append(number)
            if restored_numbers:
                page.metadata["restored_hwp_text_problem_numbers"] = restored_numbers


def _passage_range_tuple(metadata: dict[str, Any]) -> tuple[int, int] | None:
    value = metadata.get("passage_range")
    if not isinstance(value, dict):
        return None
    start = _coerce_int(value.get("start"))
    end = _coerce_int(value.get("end"))
    if start is None or end is None or start <= 0 or end < start:
        return None
    return start, end


def _passage_child_numbers(metadata: dict[str, Any], start: int, end: int) -> list[int]:
    value = metadata.get("passage_child_problem_numbers")
    if isinstance(value, list):
        numbers: list[int] = []
        for raw in value:
            number = _coerce_int(raw)
            if number is not None and start <= number <= end:
                numbers.append(number)
        if numbers:
            return list(dict.fromkeys(numbers))
    return list(range(start, end + 1))


def _append_unique_string(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _metadata_string_list(metadata: dict[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _refresh_cross_page_passage_group(group: dict[str, Any]) -> None:
    source_page_ids = list(group["source_page_ids"])
    continues_across_pages = len(source_page_ids) > 1
    member_page_ids = group.get("member_page_ids")
    if not isinstance(member_page_ids, dict):
        member_page_ids = {}
    fragment_source_page_ids: list[str] = []
    for member in group["members"]:
        if not _problem_is_passage_fragment_unit(member):
            continue
        member_page_id = str(member_page_ids.get(member.unit_id) or "").strip()
        if member_page_id:
            _append_unique_string(fragment_source_page_ids, member_page_id)
    for member in group["members"]:
        metadata = member.metadata
        metadata["passage_source_page_ids"] = list(source_page_ids)
        metadata["passage_continues_across_pages"] = continues_across_pages
        metadata["passage_fragment_count"] = len(source_page_ids)
        # The legacy source-page field is group-wide and also includes pages
        # that contain child questions only.  Image stitching needs the exact
        # pages on which passage fragments were actually materialized.
        metadata["passage_fragment_source_page_ids"] = list(fragment_source_page_ids)
        metadata["passage_has_cross_page_fragments"] = len(fragment_source_page_ids) > 1


def _cross_page_passage_group_actual_child_numbers(group: Mapping[str, Any]) -> set[int]:
    """Return child-question numbers already attached to a passage group.

    ``child_numbers`` describes the expected range, so it cannot tell us when
    that range has actually been consumed.  Keeping completed groups active is
    dangerous: small numbered labels in a chart on the next page can otherwise
    look like the first child question and revive the previous passage.
    """
    numbers: set[int] = set()
    members = group.get("members")
    if not isinstance(members, list):
        return numbers
    for member in members:
        if not isinstance(member, ProblemUnit):
            continue
        if str(member.metadata.get("passage_role") or "").strip() != "child_question":
            continue
        number = _problem_metadata_number(member)
        if number is not None:
            numbers.add(number)
    return numbers


def _cross_page_passage_group_is_complete(group: Mapping[str, Any]) -> bool:
    expected = {
        number
        for number in group.get("child_numbers", [])
        if isinstance(number, int) and number >= 1
    }
    if not expected:
        start = _coerce_int(group.get("start"))
        end = _coerce_int(group.get("end"))
        if start is None or end is None or end < start:
            return False
        expected = set(range(start, end + 1))
    return expected.issubset(_cross_page_passage_group_actual_child_numbers(group))


def _apply_cross_page_passage_group(
    group: dict[str, Any],
    *,
    page_id: str,
    problem: ProblemUnit,
) -> None:
    metadata = problem.metadata
    metadata.setdefault("passage_group_id", group["group_id"])
    metadata.setdefault("passage_range", {"start": group["start"], "end": group["end"]})
    metadata.setdefault("passage_role", "child_question")
    if group["shared_block_ids"]:
        metadata.setdefault("shared_passage_block_ids", list(group["shared_block_ids"]))
    metadata.setdefault("passage_child_problem_numbers", list(group["child_numbers"]))

    _append_unique_string(group["source_page_ids"], page_id)
    group.setdefault("member_page_ids", {})[problem.unit_id] = page_id
    if not any(member is problem for member in group["members"]):
        group["members"].append(problem)
    _refresh_cross_page_passage_group(group)


def _seed_cross_page_passage_group(
    active_groups: dict[str, dict[str, Any]],
    *,
    page_id: str,
    problem: ProblemUnit,
) -> dict[str, Any] | None:
    metadata = problem.metadata
    group_id = str(metadata.get("passage_group_id") or "").strip()
    passage_range = _passage_range_tuple(metadata)
    if not group_id or passage_range is None:
        return None

    start, end = passage_range
    group = active_groups.get(group_id)
    if group is None:
        group = {
            "group_id": group_id,
            "start": start,
            "end": end,
            "shared_block_ids": _metadata_string_list(metadata, "shared_passage_block_ids"),
            "child_numbers": _passage_child_numbers(metadata, start, end),
            "source_page_ids": [],
            "members": [],
            "member_page_ids": {},
        }
        active_groups[group_id] = group
    else:
        group["start"] = min(int(group["start"]), start)
        group["end"] = max(int(group["end"]), end)
        for block_id in _metadata_string_list(metadata, "shared_passage_block_ids"):
            _append_unique_string(group["shared_block_ids"], block_id)
        for number in _passage_child_numbers(metadata, start, end):
            if number not in group["child_numbers"]:
                group["child_numbers"].append(number)
        group["child_numbers"].sort()

    _apply_cross_page_passage_group(group, page_id=page_id, problem=problem)
    return group


def _infer_pdf_cross_page_passage_continuation(
    pages: Sequence[PageModel],
    page_index: int,
    active_groups: dict[str, dict[str, Any]],
) -> ProblemUnit | None:
    """Recover passage text at the top of the page following a range marker.

    KICE Korean PDFs frequently place ``[10~13]`` near the bottom of one page,
    continue the passage from the top of the next page, and only then print
    question 10.  Per-page segmentation can see the range marker and question
    markers but cannot associate the unmarked middle fragment.  This narrow
    structural inference fills that gap only when the prior range reaches the
    lower page body and the next page has substantive text before that range's
    first question.
    """
    if page_index <= 0 or not active_groups:
        return None
    page = pages[page_index]
    previous_page = pages[page_index - 1]
    if page.subject not in {Subject.KOREAN, Subject.ENGLISH}:
        return None
    if str(page.metadata.get("source_type") or "") != "pdf":
        return None
    if str(page.metadata.get("segmenter") or "") != "pdf-text-markers":
        return None

    block_by_id = {block.block_id: block for block in page.blocks}
    numbered = [
        (number, problem)
        for problem in page.problems
        if (number := _problem_metadata_number(problem)) is not None
    ]
    if not numbered:
        return None
    numbered.sort(key=lambda item: (item[0], item[1].unit_id))

    selected_group: dict[str, Any] | None = None
    first_problem: ProblemUnit | None = None
    for number, problem in numbered:
        for group in active_groups.values():
            if _cross_page_passage_group_is_complete(group):
                continue
            source_page_ids = [str(value) for value in group.get("source_page_ids", [])]
            if (
                number == int(group["start"])
                and source_page_ids
                and source_page_ids[-1] == previous_page.page_id
                and page.page_id not in source_page_ids
            ):
                selected_group = group
                first_problem = problem
                break
        if selected_group is not None:
            break
    if selected_group is None or first_problem is None:
        return None

    group_id = str(selected_group["group_id"])
    if any(
        _problem_is_passage_fragment_unit(problem)
        and str(problem.metadata.get("passage_group_id") or "") == group_id
        for problem in page.problems
    ):
        return None

    previous_block_by_id = {block.block_id: block for block in previous_page.blocks}
    previous_shared_blocks = [
        previous_block_by_id[block_id]
        for block_id in selected_group.get("shared_block_ids", [])
        if block_id in previous_block_by_id
    ]
    if not previous_shared_blocks:
        return None
    if max(block.bbox.bottom for block in previous_shared_blocks) < float(previous_page.height_px) * 0.60:
        return None

    first_block = _problem_first_block(first_problem, block_by_id)
    if first_block is None:
        return None
    min_fragment_height = max(36.0, float(page.height_px) * 0.025)
    fragment_specs: list[tuple[int, float, float, float, float]] = []
    raw_regions = page.metadata.get("pdf_pre_question_text_regions")
    if isinstance(raw_regions, list):
        for raw_region in raw_regions:
            if not isinstance(raw_region, dict):
                continue
            if _coerce_int(raw_region.get("before_problem_number")) != int(selected_group["start"]):
                continue
            raw_bbox = raw_region.get("bbox")
            if not isinstance(raw_bbox, dict):
                continue
            column_index = _coerce_int(raw_region.get("column_index")) or 1
            left = _coerce_float(raw_bbox.get("left"))
            top = _coerce_float(raw_bbox.get("top"))
            right = _coerce_float(raw_bbox.get("right"))
            bottom = _coerce_float(raw_bbox.get("bottom"))
            if None in {left, top, right, bottom}:
                continue
            assert left is not None and top is not None and right is not None and bottom is not None
            left = max(0.0, left)
            top = max(0.0, top)
            right = min(float(page.width_px), right)
            bottom = min(float(page.height_px), bottom)
            if right - left < float(page.width_px) * 0.20 or bottom - top < min_fragment_height:
                continue
            fragment_specs.append((column_index, left, top, right, bottom))
    if not fragment_specs:
        return None

    existing_blocks_by_id = {
        block.block_id: block
        for candidate_page in pages[:page_index]
        for block in candidate_page.blocks
    }
    previous_fragment_index = max(
        (
            _coerce_int(existing_blocks_by_id[block_id].metadata.get("passage_fragment_index")) or 0
            for block_id in selected_group.get("shared_block_ids", [])
            if block_id in existing_blocks_by_id
        ),
        default=0,
    )
    start = int(selected_group["start"])
    end = int(selected_group["end"])
    continuation_blocks: list[ContentBlock] = []
    for offset, (column_index, left, top, right, bottom) in enumerate(fragment_specs, start=1):
        block_id = f"{page.page_id}-cross-page-passage-{start}-{end}-{offset:02d}"
        continuation_blocks.append(
            ContentBlock(
                block_id=block_id,
                block_type=BlockType.IMAGE,
                bbox=Box.from_points(left, top, right, bottom),
                reading_order=len(page.blocks) + len(continuation_blocks),
                text=None,
                confidence=1.0,
                metadata={
                    "segmenter": "pdf-cross-page-passage-continuation",
                    "column_index": column_index,
                    "marker_kind": "passage_continuation",
                    "passage_range": {"start": start, "end": end},
                    "passage_fragment_index": previous_fragment_index + offset,
                    "shared_passage": True,
                    "cross_page_passage_inferred": True,
                    "force_image_record": True,
                },
            )
        )
    if not continuation_blocks:
        return None

    page.blocks.extend(continuation_blocks)
    for block in continuation_blocks:
        _append_unique_string(selected_group["shared_block_ids"], block.block_id)
    continuation = ProblemUnit(
        unit_id=f"{group_id}-continuation-{page.page_id}",
        subject=page.subject,
        title=f"지문 {start}~{end}",
        figure_block_ids=[block.block_id for block in continuation_blocks],
        metadata={
            "passage_group_id": group_id,
            "passage_range": {"start": start, "end": end},
            "passage_role": "passage_fragment",
            "supplemental_item": True,
            "shared_passage_block_ids": list(selected_group["shared_block_ids"]),
            "passage_child_problem_numbers": list(selected_group["child_numbers"]),
            "passage_grouping_source": "pdf_cross_page_structure",
            "passage_fragment_source": "pdf_cross_page_structure",
            "cross_page_passage_inferred": True,
        },
    )
    page.problems.append(continuation)
    page.metadata["pdf_cross_page_passage_continuation_count"] = (
        (_coerce_int(page.metadata.get("pdf_cross_page_passage_continuation_count")) or 0)
        + len(continuation_blocks)
    )
    _seed_cross_page_passage_group(
        active_groups,
        page_id=page.page_id,
        problem=continuation,
    )
    return continuation


def _hwp_preview_text_values(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    raw = metadata.get("hwp_preview_text")
    if isinstance(raw, str) and raw.strip():
        values.append(raw)
    quality = metadata.get("hwp_conversion_quality")
    if isinstance(quality, dict):
        for key in ("hwp_preview_text", "preview_text"):
            nested = quality.get(key)
            if isinstance(nested, str) and nested.strip():
                values.append(nested)
    return values


def _append_hwp_passage_range_item(
    items: list[dict[str, Any]],
    seen: set[tuple[int, int]],
    *,
    start: int,
    end: int,
    source: str,
    group_prefix: str,
) -> None:
    if end <= start:
        return
    key = (start, end)
    if key in seen:
        return
    seen.add(key)
    items.append(
        {
            "start": start,
            "end": end,
            "source": source,
            "group_prefix": group_prefix,
        }
    )


def _hwp_text_passage_range_values(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    raw = metadata.get("hwp_text_passage_ranges")
    if isinstance(raw, list):
        values.extend(item for item in raw if isinstance(item, dict))
    quality = metadata.get("hwp_conversion_quality")
    if isinstance(quality, dict):
        nested = quality.get("hwp_text_passage_ranges")
        if isinstance(nested, list):
            values.extend(item for item in nested if isinstance(item, dict))
    return values


def _hwp_passage_range_items(pages: Sequence[PageModel]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for page in pages:
        for text in _hwp_preview_text_values(page.metadata):
            for line in text.splitlines():
                passage_range = extract_set_problem_range(line)
                if passage_range is None:
                    continue
                start, end = passage_range
                _append_hwp_passage_range_item(
                    items,
                    seen,
                    start=start,
                    end=end,
                    source="hwp_preview_text",
                    group_prefix="hwp-preview",
                )
        for raw_range in _hwp_text_passage_range_values(page.metadata):
            start = _coerce_int(raw_range.get("start"))
            end = _coerce_int(raw_range.get("end"))
            if start is None or end is None:
                continue
            _append_hwp_passage_range_item(
                items,
                seen,
                start=start,
                end=end,
                source="hwp_text_passage_ranges",
                group_prefix="hwp-text",
            )
    items.sort(key=lambda item: (int(item["start"]), int(item["end"]), str(item["group_prefix"])))
    return items


def _problem_number_counts(pages: Sequence[PageModel]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for page in pages:
        for problem in page.problems:
            number = _problem_metadata_number(problem)
            if number is None:
                continue
            counts[number] = counts.get(number, 0) + 1
    return counts


def _annotate_hwp_preview_passage_ranges(pages: Sequence[PageModel]) -> None:
    range_items = _hwp_passage_range_items(pages)
    if not range_items:
        return
    number_counts = _problem_number_counts(pages)
    for page in pages:
        for problem in page.problems:
            if problem.metadata.get("passage_group_id"):
                continue
            problem_number = _problem_metadata_number(problem)
            if problem_number is None:
                continue
            for item in range_items:
                start = int(item["start"])
                end = int(item["end"])
                if start <= problem_number <= end:
                    group_prefix = str(item.get("group_prefix") or "hwp-preview")
                    source = str(item.get("source") or "hwp_preview_text")
                    if source == "hwp_text_passage_ranges" and number_counts.get(problem_number, 0) > 1:
                        continue
                    problem.metadata.update(
                        {
                            "passage_group_id": f"{group_prefix}-passage-{start}-{end}",
                            "passage_range": {"start": start, "end": end},
                            "passage_role": "child_question",
                            "passage_child_problem_numbers": list(range(start, end + 1)),
                            "passage_grouping_source": source,
                        }
                    )
                    break


def _following_numbered_problem_run(
    page: PageModel,
    *,
    min_length: int = 2,
) -> list[ProblemUnit]:
    numbered = [
        (number, problem)
        for problem in page.problems
        if (number := _problem_metadata_number(problem)) is not None
    ]
    if len(numbered) < min_length:
        return []
    numbered.sort(key=lambda item: (item[0], item[1].unit_id))
    run: list[tuple[int, ProblemUnit]] = [numbered[0]]
    previous = numbered[0][0]
    for number, problem in numbered[1:]:
        if number != previous + 1:
            break
        run.append((number, problem))
        previous = number
    if len(run) < min_length:
        return []
    return [problem for _, problem in run]


def _next_numbered_problem_run(
    pages: Sequence[PageModel],
    start_index: int,
    *,
    min_length: int = 2,
) -> list[ProblemUnit]:
    reading_subjects = {Subject.KOREAN, Subject.ENGLISH, Subject.SOCIAL, Subject.SCIENCE}
    for next_page in pages[start_index + 1:]:
        if next_page.subject not in reading_subjects:
            return []
        next_run = _following_numbered_problem_run(next_page, min_length=min_length)
        if next_run:
            return next_run
        if any(_problem_metadata_number(problem) is not None for problem in next_page.problems):
            return []
    return []


def _annotate_marker_continuation_pages_to_following_groups(pages: Sequence[PageModel]) -> None:
    for index, page in enumerate(pages):
        if page.subject not in {Subject.KOREAN, Subject.ENGLISH, Subject.SOCIAL, Subject.SCIENCE}:
            continue
        if len(page.problems) != 1:
            continue
        continuation = page.problems[0]
        if not _is_marker_document_continuation_problem(continuation):
            continue
        if continuation.metadata.get("passage_group_id"):
            continue

        following_run = _next_numbered_problem_run(pages, index, min_length=1)
        if len(following_run) < 1:
            continue

        first_problem = following_run[0]
        first_number = _problem_metadata_number(first_problem)
        last_number = _problem_metadata_number(following_run[-1])
        if first_number is None or last_number is None:
            continue
        existing_range = _passage_range_tuple(first_problem.metadata)
        start, end = existing_range or (first_number, last_number)
        child_numbers = _passage_child_numbers(first_problem.metadata, start, end)
        group_id = str(first_problem.metadata.get("passage_group_id") or "").strip()
        if not group_id:
            number_label = str(start) if start == end else f"{start}-{end}"
            group_id = f"hwp-continuation-passage-{number_label}"

        common_metadata = {
            "passage_group_id": group_id,
            "passage_range": {"start": start, "end": end},
            "passage_child_problem_numbers": child_numbers,
            "passage_grouping_source": "marker_document_continuation",
        }
        child_number_set = set(child_numbers)
        continuation.metadata.update(
            {
                **common_metadata,
                "passage_role": "passage_fragment",
                "passage_fragment_source": "marker_document_continuation",
            }
        )
        for problem in following_run:
            problem_number = _problem_metadata_number(problem)
            if problem_number not in child_number_set:
                continue
            if not problem.metadata.get("passage_group_id"):
                problem.metadata.update(
                    {
                        **common_metadata,
                        "passage_role": "child_question",
                    }
                )


def _demote_nested_passage_materials(
    page: PageModel,
    active_groups: Mapping[str, dict[str, Any]],
) -> int:
    """Keep an in-question range material out of passage-only results.

    A later page can start with ``[13~14] <보기>...`` while questions 13 and
    14 still belong to an earlier ``[11~14]`` reading passage. Per-page
    segmentation cannot know that the smaller range is nested. At document
    scope the still-active parent range is decisive: retain the material as a
    normal supplemental record, and link its child questions to the parent.
    """
    block_by_id = {block.block_id: block for block in page.blocks}
    demoted = 0
    for problem in page.problems:
        if not _problem_is_passage_fragment_unit(problem):
            continue
        nested_range = _passage_range_tuple(problem.metadata)
        nested_group_id = str(problem.metadata.get("passage_group_id") or "").strip()
        if nested_range is None or not nested_group_id:
            continue
        nested_start, nested_end = nested_range
        parent_candidates = [
            group
            for group in active_groups.values()
            if int(group["start"]) <= nested_start
            and nested_end <= int(group["end"])
            and (int(group["start"]), int(group["end"])) != nested_range
        ]
        if not parent_candidates:
            continue
        parent = min(
            parent_candidates,
            key=lambda group: (int(group["end"]) - int(group["start"]), str(group["group_id"])),
        )
        parent_start = int(parent["start"])
        parent_end = int(parent["end"])
        parent_group_id = str(parent["group_id"])

        metadata = problem.metadata
        metadata["question_material_range"] = {"start": nested_start, "end": nested_end}
        metadata["question_material_parent_group_id"] = parent_group_id
        metadata["nested_passage_suppressed"] = True
        metadata["passage_role"] = "question_material_fragment"
        for key in (
            "passage_group_id",
            "passage_range",
            "passage_child_problem_numbers",
            "shared_passage_block_ids",
            "passage_source_page_ids",
            "passage_fragment_source_page_ids",
            "passage_continues_across_pages",
            "passage_has_cross_page_fragments",
        ):
            metadata.pop(key, None)
        problem.title = f"공통 자료 {nested_start}~{nested_end}"

        for block_id in _iter_problem_block_ids_raw(problem):
            block = block_by_id.get(block_id)
            if block is None:
                continue
            block.metadata["shared_passage"] = False
            block.metadata["question_material_fragment"] = True
            block.metadata["nested_passage_suppressed"] = True

        for child in page.problems:
            child_metadata = child.metadata
            if str(child_metadata.get("passage_group_id") or "").strip() != nested_group_id:
                continue
            if str(child_metadata.get("passage_role") or "").strip() != "child_question":
                continue
            child_metadata.update(
                {
                    "passage_group_id": parent_group_id,
                    "passage_range": {"start": parent_start, "end": parent_end},
                    "passage_child_problem_numbers": list(parent["child_numbers"]),
                    "passage_role": "child_question",
                    "nested_question_material_range": {
                        "start": nested_start,
                        "end": nested_end,
                    },
                }
            )
            if parent["shared_block_ids"]:
                child_metadata["shared_passage_block_ids"] = list(parent["shared_block_ids"])
        demoted += 1

    if demoted:
        page.metadata["nested_passage_material_count"] = (
            (_coerce_int(page.metadata.get("nested_passage_material_count")) or 0) + demoted
        )
    return demoted


def _annotate_cross_page_passage_groups(pages: Sequence[PageModel]) -> None:
    _annotate_hwp_preview_passage_ranges(pages)
    _annotate_marker_continuation_pages_to_following_groups(pages)
    active_groups: dict[str, dict[str, Any]] = {}
    for page_index, page in enumerate(pages):
        _demote_nested_passage_materials(page, active_groups)
        _infer_pdf_cross_page_passage_continuation(pages, page_index, active_groups)
        ordered_problems = sorted(
            page.problems,
            key=lambda problem: (_problem_metadata_number(problem) or 10**9, problem.unit_id),
        )
        for problem in ordered_problems:
            problem_number = _problem_metadata_number(problem)
            if problem_number is not None:
                for group_id, group in list(active_groups.items()):
                    if problem_number > int(group["end"]):
                        active_groups.pop(group_id, None)

            seeded_group = _seed_cross_page_passage_group(
                active_groups,
                page_id=page.page_id,
                problem=problem,
            )
            if seeded_group is not None or problem_number is None:
                continue

            if problem.metadata.get("passage_group_id"):
                continue
            for group in active_groups.values():
                if int(group["start"]) <= problem_number <= int(group["end"]):
                    _apply_cross_page_passage_group(group, page_id=page.page_id, problem=problem)
                    break

        # A range that already owns every expected child question is finished.
        # Retiring it at the page boundary prevents numbered chart/data labels
        # on later pages from being attached to (or extending) the old passage.
        for group_id, group in list(active_groups.items()):
            if _cross_page_passage_group_is_complete(group):
                active_groups.pop(group_id, None)


def _pdf_text_line_box(value: Any) -> Box | None:
    if not isinstance(value, Mapping):
        return None
    raw_box = value.get("bbox")
    if not isinstance(raw_box, Mapping):
        return None
    try:
        left = float(raw_box.get("left", 0.0))
        top = float(raw_box.get("top", 0.0))
        right = float(raw_box.get("right", left))
        bottom = float(raw_box.get("bottom", top))
    except (TypeError, ValueError):
        return None
    if right <= left or bottom <= top:
        return None
    return Box.from_points(left, top, right, bottom)


def _materialize_pdf_pre_question_passage_continuations(
    pages: Sequence[PageModel],
    prepared_by_page_id: Mapping[str, PreparedPage],
) -> None:
    """Turn next-page text before the first child question into a passage fragment.

    PDF marker segmentation intentionally starts at numbered questions.  Korean
    exam passages can continue at the top of the next page before that marker,
    so the text would otherwise be visible in the source but absent from the
    passage-only image.
    """

    for page in pages:
        prepared_page = prepared_by_page_id.get(page.page_id)
        if prepared_page is None:
            continue
        text_lines = prepared_page.metadata.get("pdf_text_lines")
        if not isinstance(text_lines, list) or not text_lines:
            continue

        block_by_id = {block.block_id: block for block in page.blocks}
        group_children: dict[str, list[ProblemUnit]] = {}
        for problem in page.problems:
            metadata = problem.metadata
            if str(metadata.get("passage_role") or "") != "child_question":
                continue
            group_id = str(metadata.get("passage_group_id") or "").strip()
            source_page_ids = _metadata_string_list(metadata, "passage_source_page_ids")
            if (
                not group_id
                or len(source_page_ids) < 2
                or source_page_ids[0] == page.page_id
                or page.page_id not in source_page_ids
            ):
                continue
            group_children.setdefault(group_id, []).append(problem)

        for group_id, children in group_children.items():
            if any(
                _problem_is_passage_fragment_unit(problem)
                and str(problem.metadata.get("passage_group_id") or "").strip() == group_id
                for problem in page.problems
            ):
                continue
            first_child = min(
                children,
                key=lambda problem: (
                    _problem_metadata_number(problem) or 10**9,
                    _problem_top_y(problem, block_by_id),
                ),
            )
            passage_range = _passage_range_tuple(first_child.metadata)
            first_child_number = _problem_metadata_number(first_child)
            # Child questions often continue onto later pages even though the
            # shared passage itself ended earlier.  Only the page containing
            # the range's first question can legitimately hold passage text
            # before that question marker.
            if passage_range is None or first_child_number != passage_range[0]:
                continue
            child_blocks = [
                block_by_id[block_id]
                for block_id in _iter_problem_block_ids_raw(first_child)
                if block_id in block_by_id
            ]
            if not child_blocks:
                continue
            first_top = min(block.bbox.top for block in child_blocks)
            if first_top <= float(page.height_px) * 0.08:
                continue
            column_index = _problem_column_value(first_child, block_by_id)
            # A real numbered question before the first passage child means
            # the upper column is occupied by that question, not by a
            # cross-page passage continuation. Without this guard, Korean
            # elective pages such as Q42 followed by the 43~45 passage can
            # materialize Q42 as a supplemental passage and clamp its own
            # crop to a one-pixel strip.
            intervening_problem = any(
                problem.unit_id != first_child.unit_id
                and not _problem_is_passage_fragment_unit(problem)
                and _problem_metadata_number(problem) is not None
                and _problem_column_value(problem, block_by_id) == column_index
                and float(page.height_px) * 0.04
                <= _problem_top_y(problem, block_by_id)
                < first_top
                for problem in page.problems
            )
            if intervening_problem:
                continue
            column_blocks = [
                block
                for block in page.blocks
                if column_index is None or _coerce_int(block.metadata.get("column_index")) == column_index
            ]
            reference_blocks = column_blocks or child_blocks
            left_bound = min(block.bbox.left for block in reference_blocks)
            right_bound = max(block.bbox.right for block in reference_blocks)
            top_floor = float(page.height_px) * 0.04
            bottom_limit = first_top - max(8.0, float(page.height_px) * 0.003)

            continuation_lines: list[tuple[str, Box]] = []
            for raw_line in text_lines:
                line_box = _pdf_text_line_box(raw_line)
                text = str(raw_line.get("text") or "").strip() if isinstance(raw_line, Mapping) else ""
                if line_box is None or not text:
                    continue
                center_x = (line_box.left + line_box.right) / 2.0
                center_y = (line_box.top + line_box.bottom) / 2.0
                if (
                    left_bound - 8.0 <= center_x <= right_bound + 8.0
                    and top_floor <= center_y <= bottom_limit
                ):
                    continuation_lines.append((text, line_box))

            visible_character_count = sum(
                len(re.sub(r"\s+", "", text)) for text, _line_box in continuation_lines
            )
            if len(continuation_lines) < 2 or visible_character_count < 40:
                continue

            start, end = passage_range
            box = Box.from_points(
                max(0.0, min(left_bound, *(line_box.left for _text, line_box in continuation_lines))),
                max(0.0, min(line_box.top for _text, line_box in continuation_lines) - 12.0),
                min(float(page.width_px), max(right_bound, *(line_box.right for _text, line_box in continuation_lines))),
                min(bottom_limit, max(line_box.bottom for _text, line_box in continuation_lines) + 12.0),
            )
            block_id = f"{page.page_id}-passage-continuation-{start}-{end}"
            unit_id = f"{block_id}-fragment"
            if block_id in block_by_id or any(problem.unit_id == unit_id for problem in page.problems):
                continue

            source_page_ids = _metadata_string_list(first_child.metadata, "passage_source_page_ids")
            child_numbers = _passage_child_numbers(first_child.metadata, start, end)
            page.blocks.append(
                ContentBlock(
                    block_id=block_id,
                    block_type=BlockType.STEM,
                    bbox=box,
                    reading_order=-1,
                    text="\n".join(text for text, _line_box in continuation_lines),
                    confidence=1.0,
                    metadata={
                        "segmenter": "pdf-pre-question-passage-continuation",
                        "column_index": column_index,
                        "shared_passage": True,
                        "passage_range_start": start,
                        "passage_range_end": end,
                        "passage_range": {"start": start, "end": end},
                        "passage_text_line_count": len(continuation_lines),
                        "passage_text_character_count": visible_character_count,
                        "passage_text_bounds_score": 1.0,
                        "passage_detection_confidence": 0.98,
                        "force_image_record": True,
                    },
                )
            )
            page.problems.append(
                ProblemUnit(
                    unit_id=unit_id,
                    subject=page.subject,
                    title=f"지문 {start}~{end}",
                    stem_block_ids=[block_id],
                    metadata={
                        "passage_group_id": group_id,
                        "passage_range": {"start": start, "end": end},
                        "passage_child_problem_numbers": child_numbers,
                        "passage_source_page_ids": source_page_ids,
                        "passage_continues_across_pages": True,
                        "passage_fragment_count": len(source_page_ids),
                        "passage_role": "passage_fragment",
                        "supplemental_item": True,
                        "passage_fragment_source": "pdf_pre_question_continuation",
                        "passage_pre_question_continuation": True,
                        "passage_detection_confidence": 0.98,
                        "passage_text_line_count": len(continuation_lines),
                        "passage_text_character_count": visible_character_count,
                        "passage_text_bounds_score": 1.0,
                    },
                )
            )
            continuation_ids = first_child.metadata.setdefault(
                "passage_pre_question_continuation_block_ids", []
            )
            if isinstance(continuation_ids, list) and block_id not in continuation_ids:
                continuation_ids.append(block_id)


def _collect_problem_risk_flags(problem: ProblemUnit) -> list[str]:
    flags: list[str] = []
    if (
        _problem_passage_continues_across_pages(problem.metadata)
        and not bool(problem.metadata.get("passage_fragments_merged"))
    ):
        flags.append(PASSAGE_CROSS_PAGE_MERGE_CHECK_RISK_FLAG)
    if problem.metadata.get("fallback_grouping"):
        flags.append("fallback_grouping")
    if problem.metadata.get("merged_problem_block"):
        flags.append("merged_problem_block")
    if problem.metadata.get("marker_conflict"):
        flags.append("marker_conflicts")

    for key in ("review_flags", "risk_flags"):
        values = problem.metadata.get(key)
        if isinstance(values, list):
            flags.extend(str(value) for value in values if value)

    ai_unit = problem.metadata.get("ai_problem_unit")
    if isinstance(ai_unit, dict):
        values = ai_unit.get("review_flags")
        if isinstance(values, list):
            flags.extend(str(value) for value in values if value)

    return list(dict.fromkeys(flags))


def _problem_passage_continues_across_pages(metadata: dict[str, Any] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    if bool(metadata.get("passage_continues_across_pages")):
        return True
    source_page_ids = metadata.get("passage_source_page_ids")
    if not isinstance(source_page_ids, list):
        return False
    return len({str(page_id) for page_id in source_page_ids if str(page_id)}) > 1


def _flatten_passage_segment_on_white(image: Image.Image) -> Image.Image:
    if "A" not in image.getbands():
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    flattened = Image.new("RGB", rgba.size, "white")
    flattened.paste(rgba.convert("RGB"), (0, 0), rgba.getchannel("A"))
    return flattened


def _passage_foreground_row_counts(image: Image.Image) -> list[int]:
    rgba = image.convert("RGBA")
    if np is not None:
        arr = np.asarray(rgba, dtype=np.uint8)
        alpha = arr[..., 3]
        rgb = arr[..., :3].astype(np.float32)
        luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        saturation = rgb.max(axis=2) - rgb.min(axis=2)
        has_transparency = bool(int(alpha.min()) < 245)
        foreground = (
            alpha > 24
            if has_transparency
            else (luminance <= 246.0) | (saturation >= 24.0)
        )
        return [int(value) for value in foreground.sum(axis=1)]

    pixels = rgba.load()
    has_transparency = rgba.getchannel("A").getextrema()[0] < 245
    counts: list[int] = []
    for y in range(rgba.height):
        count = 0
        for x in range(rgba.width):
            red, green, blue, alpha = pixels[x, y]
            luminance = 0.299 * red + 0.587 * green + 0.114 * blue
            saturation = max(red, green, blue) - min(red, green, blue)
            if (alpha > 24) if has_transparency else (luminance <= 246 or saturation >= 24):
                count += 1
        counts.append(count)
    return counts


def _blank_row_runs(row_counts: Sequence[int], *, end: int) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(max(0, min(len(row_counts), end))):
        is_blank = row_counts[index] <= 3
        if is_blank and start is None:
            start = index
        elif not is_blank and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, max(0, min(len(row_counts), end))))
    return runs


def _passage_foreground_x_span(
    image: Image.Image,
    *,
    top: int,
    bottom: int,
) -> int:
    """Return the horizontal span of visible ink inside a passage row band."""
    top = max(0, min(image.height, int(top)))
    bottom = max(top, min(image.height, int(bottom)))
    if top >= bottom or image.width <= 0:
        return 0
    rgba = image.convert("RGBA")
    if np is not None:
        arr = np.asarray(rgba, dtype=np.uint8)[top:bottom]
        alpha = arr[..., 3]
        rgb = arr[..., :3].astype(np.float32)
        luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        saturation = rgb.max(axis=2) - rgb.min(axis=2)
        has_transparency = bool(int(alpha.min()) < 245)
        foreground = (
            alpha > 24
            if has_transparency
            else (luminance <= 246.0) | (saturation >= 24.0)
        )
        columns = np.flatnonzero(foreground.any(axis=0))
        if columns.size == 0:
            return 0
        return int(columns[-1] - columns[0] + 1)

    pixels = rgba.load()
    has_transparency = rgba.getchannel("A").getextrema()[0] < 245
    visible_columns: list[int] = []
    for x in range(rgba.width):
        for y in range(top, bottom):
            red, green, blue, alpha = pixels[x, y]
            luminance = 0.299 * red + 0.587 * green + 0.114 * blue
            saturation = max(red, green, blue) - min(red, green, blue)
            visible = (
                alpha > 24
                if has_transparency
                else luminance <= 246 or saturation >= 24
            )
            if visible:
                visible_columns.append(x)
                break
    if not visible_columns:
        return 0
    return visible_columns[-1] - visible_columns[0] + 1


def _foreground_row_runs(
    row_counts: Sequence[int],
    *,
    start: int,
    end: int,
) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    lower = max(0, min(len(row_counts), start))
    upper = max(lower, min(len(row_counts), end))
    for index in range(lower, upper):
        is_foreground = row_counts[index] > 3
        if is_foreground and run_start is None:
            run_start = index
        elif not is_foreground and run_start is not None:
            runs.append((run_start, index))
            run_start = None
    if run_start is not None:
        runs.append((run_start, upper))
    return runs


def _has_substantial_content_below_footer_rule(
    row_counts: Sequence[int],
    *,
    rule_end: int,
    image_width: int,
) -> bool:
    """Keep likely footnotes/captions below a rule instead of deleting them.

    Page numbers normally form one narrow ink run. Two separate text lines or
    a single line spanning at least a quarter of the crop width are treated as
    document content. This deliberately prefers leaving uncertain chrome over
    deleting a real passage footnote.
    """

    raw_runs = [
        (start, end)
        for start, end in _foreground_row_runs(
            row_counts,
            start=rule_end,
            end=len(row_counts),
        )
        if end - start >= 2
    ]
    runs: list[tuple[int, int]] = []
    for start, end in raw_runs:
        if runs and start - runs[-1][1] <= 5:
            runs[-1] = (runs[-1][0], end)
        else:
            runs.append((start, end))
    if len(runs) >= 2:
        return True
    below_rule = row_counts[max(0, rule_end) :]
    return bool(
        below_rule
        and max(below_rule) >= int(round(image_width * PASSAGE_JOIN_FOOTER_CONTENT_MIN_WIDTH_RATIO))
    )


def _trim_passage_segment_for_join(
    image: Image.Image,
    *,
    trim_top: bool,
    trim_bottom: bool,
    crop_box_output: list[tuple[int, int, int, int]] | None = None,
) -> Image.Image:
    """Remove page footer chrome and excess whitespace at a passage join.

    A page footer is recognized conservatively: a near-full-width rule in the
    lower quarter plus a long blank run before it. This removes page labels
    such as ``고2`` without treating normal paragraph spacing as chrome.
    """
    if image.width <= 0 or image.height <= 0:
        if crop_box_output is not None:
            crop_box_output.append((0, 0, image.width, image.height))
        return image
    row_counts = _passage_foreground_row_counts(image)
    top = 0
    bottom = image.height

    if trim_top:
        # Short continuation fragments may devote more than a quarter of
        # their crop to the page header before the first body line.
        upper_end = max(1, int(round(image.height * 0.45)))
        long_rule_threshold = max(48, int(round(image.width * 0.55)))
        top_rule_rows = [
            index
            for index in range(upper_end)
            if index <= int(round(image.height * PASSAGE_JOIN_TOP_RULE_MAX_RATIO))
            and row_counts[index] >= long_rule_threshold
        ]
        if top_rule_rows:
            first_rule = top_rule_rows[0]
            rule_end = first_rule + 1
            while rule_end < upper_end and row_counts[rule_end] >= long_rule_threshold:
                rule_end += 1
            # A section label such as ``(B)`` commonly sits above a boxed
            # passage whose top border is also a long rule. Treat that rule as
            # page chrome only when the ink before it spans most of the crop,
            # as a real left/right page header does.
            header_span = _passage_foreground_x_span(
                image,
                top=0,
                bottom=first_rule,
            )
            rule_width = max(row_counts[first_rule:rule_end], default=0)
            cropped_page_header_rule = (
                # The detector operates on the rendered raster, so the same
                # PDF header lands below 64 px at higher DPI. Keep this
                # threshold proportional while still limiting it to the
                # shallow page-header band.
                first_rule <= max(64, int(round(image.height * 0.08)))
                and rule_width >= int(round(image.width * 0.93))
            )
            if (
                header_span >= int(round(image.width * 0.55))
                or cropped_page_header_rule
            ):
                top = min(
                    image.height - 1,
                    rule_end + PASSAGE_JOIN_EDGE_PADDING_PX,
                )
        if top == 0:
            first_ink = next((index for index, value in enumerate(row_counts) if value > 3), 0)
            if 0 < first_ink <= int(round(image.height * 0.18)):
                top = max(0, first_ink - PASSAGE_JOIN_EDGE_PADDING_PX)

    if trim_bottom:
        lower_start = int(round(image.height * 0.65))
        long_rule_threshold = max(48, int(round(image.width * 0.55)))
        long_rule_rows = [
            index
            for index in range(lower_start, image.height)
            if row_counts[index] >= long_rule_threshold
        ]
        if long_rule_rows:
            blank_candidates = [
                (start, end)
                for start, end in _blank_row_runs(row_counts, end=image.height)
                if start >= int(round(image.height * PASSAGE_JOIN_FOOTER_BLANK_MIN_START_RATIO))
                and end - start >= PASSAGE_JOIN_BLANK_RUN_MIN_PX
                and any(rule_row >= end for rule_row in long_rule_rows)
            ]
            if blank_candidates:
                blank_start, blank_end = blank_candidates[-1]
                footer_rule_start = next(
                    rule_row for rule_row in long_rule_rows if rule_row >= blank_end
                )
                footer_rule_end = footer_rule_start + 1
                long_rule_row_set = set(long_rule_rows)
                while footer_rule_end in long_rule_row_set:
                    footer_rule_end += 1
            else:
                footer_rule_start = -1
                footer_rule_end = -1
        else:
            footer_rule_start = -1
            footer_rule_end = -1
        if footer_rule_start >= 0:
            if not _has_substantial_content_below_footer_rule(
                row_counts,
                rule_end=footer_rule_end,
                image_width=image.width,
            ):
                candidate_bottom = min(
                    image.height,
                    blank_start + PASSAGE_JOIN_EDGE_PADDING_PX,
                )
                if candidate_bottom >= top + 80 and image.height - candidate_bottom >= 32:
                    bottom = candidate_bottom

    # Asset crops deliberately retain generous safety padding so edge glyphs
    # cannot be clipped. At a stitched join that padding appears twice and
    # creates a conspicuous blank band. Collapse only clearly excessive outer
    # whitespace, retaining a small buffer around the first/last text row.
    meaningful_rows = [
        index
        for index in range(max(0, top), min(image.height, bottom))
        if row_counts[index] > 3
    ]
    if meaningful_rows:
        if trim_top:
            content_top = meaningful_rows[0]
            excess_top = content_top - top
            if excess_top >= PASSAGE_JOIN_EXCESS_BLANK_MIN_PX:
                top = max(top, content_top - PASSAGE_JOIN_CONTENT_PADDING_PX)
        if trim_bottom:
            content_bottom = meaningful_rows[-1] + 1
            excess_bottom = bottom - content_bottom
            if excess_bottom >= PASSAGE_JOIN_EXCESS_BLANK_MIN_PX:
                bottom = min(bottom, content_bottom + PASSAGE_JOIN_CONTENT_PADDING_PX)

    if top <= 0 and bottom >= image.height:
        if crop_box_output is not None:
            crop_box_output.append((0, 0, image.width, image.height))
        return image
    if crop_box_output is not None:
        crop_box_output.append((0, top, image.width, bottom))
    return image.crop((0, top, image.width, bottom))


def _passage_box_horizontal_bounds(image: Image.Image) -> tuple[int, int] | None:
    """Find the stable left/right edges of long boxed-passage rules."""
    if image.width < 80 or image.height < 40:
        return None
    rgba = image.convert("RGBA")
    if np is not None:
        arr = np.asarray(rgba, dtype=np.uint8)
        alpha = arr[..., 3]
        rgb = arr[..., :3].astype(np.float32)
        luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        saturation = rgb.max(axis=2) - rgb.min(axis=2)
        has_transparency = bool(int(alpha.min()) < 245)
        foreground = (
            alpha > 48
            if has_transparency
            else (luminance <= 246.0) | (saturation >= 24.0)
        )
        candidate_runs: list[tuple[int, int]] = []
        minimum_run = max(48, int(round(image.width * 0.45)))
        for row in foreground:
            padded = np.pad(row.astype(np.int8), (1, 1))
            changes = np.diff(padded)
            starts = np.flatnonzero(changes == 1)
            ends = np.flatnonzero(changes == -1)
            if starts.size == 0:
                continue
            lengths = ends - starts
            longest_index = int(lengths.argmax())
            if int(lengths[longest_index]) >= minimum_run:
                candidate_runs.append(
                    (int(starts[longest_index]), int(ends[longest_index] - 1))
                )
    else:
        pixels = rgba.load()
        has_transparency = rgba.getchannel("A").getextrema()[0] < 245
        candidate_runs = []
        minimum_run = max(48, int(round(image.width * 0.45)))
        for y in range(image.height):
            best_start = -1
            best_end = -1
            run_start = -1
            for x in range(image.width + 1):
                visible = False
                if x < image.width:
                    red, green, blue, alpha = pixels[x, y]
                    luminance = 0.299 * red + 0.587 * green + 0.114 * blue
                    saturation = max(red, green, blue) - min(red, green, blue)
                    visible = (
                        alpha > 48
                        if has_transparency
                        else luminance <= 246 or saturation >= 24
                    )
                if visible and run_start < 0:
                    run_start = x
                elif not visible and run_start >= 0:
                    if x - run_start > best_end - best_start:
                        best_start, best_end = run_start, x - 1
                    run_start = -1
            if best_start >= 0 and best_end - best_start + 1 >= minimum_run:
                candidate_runs.append((best_start, best_end))

    if not candidate_runs:
        return None
    # A passage can contain many long underlines. Taking the independent
    # median of every run endpoint can therefore synthesize a false, narrower
    # frame and make the outer-guide cleanup erase the real frame plus the
    # first/last glyphs on each line. Real box borders repeat at nearly the
    # same endpoints on adjacent antialiased rows (and often at both top and
    # bottom), so choose the best-supported endpoint pair instead.
    endpoint_tolerance_px = 3

    def support(run: tuple[int, int]) -> int:
        return sum(
            1
            for other in candidate_runs
            if abs(other[0] - run[0]) <= endpoint_tolerance_px
            and abs(other[1] - run[1]) <= endpoint_tolerance_px
        )

    left, right = max(
        candidate_runs,
        key=lambda run: (
            support(run),
            run[1] - run[0],
            -run[0],
        ),
    )
    if right - left < image.width * 0.55:
        return None
    return left, right


def _erase_passage_outer_margin_page_guides(image: Image.Image) -> Image.Image:
    """Erase isolated page/column rules outside a detected passage box.

    The canvas width is preserved. Only long guide columns beyond the boxed
    passage and their short connected horizontal arms are cleared, so nearby
    instruction text and edge glyphs cannot be cropped.
    """
    frame_bounds = _passage_box_horizontal_bounds(image)
    vertical_frame_bounds = _passage_frame_edges(image)
    if frame_bounds is not None:
        # A real outer frame normally contributes long vertical edges as well
        # as horizontal rules.  Underlines and small embedded material boxes
        # do not.  If either horizontal endpoint lacks that vertical support,
        # prefer the full-height frame detector instead.
        if "A" in image.getbands():
            frame_signal = image.convert("RGBA").getchannel("A")
        else:
            frame_signal = ImageOps.invert(image.convert("L"))
        column_values = list(
            frame_signal.resize((image.width, 1), Image.Resampling.BOX).get_flattened_data()
        )
        horizontal_left, horizontal_right = frame_bounds
        if (
            column_values[horizontal_left] < 72
            or column_values[horizontal_right] < 72
        ):
            frame_bounds = vertical_frame_bounds
    elif vertical_frame_bounds is not None:
        frame_bounds = vertical_frame_bounds
    if frame_bounds is None or image.width <= 0 or image.height <= 0:
        return image
    frame_left, frame_right = frame_bounds
    # Cleanup is safe only when the detected frame is the passage's outer
    # frame.  A small [A]/[B] material box can have more perfectly repeated
    # horizontal rules than the lightly antialiased outer border.  Treating
    # that inner box as the frame makes the real left border look like a page
    # guide and can erase speaker labels one glyph at a time.  When either
    # endpoint sits too far inside the crop, preserve the source verbatim.
    maximum_outer_inset = max(12, int(round(image.width * 0.06)))
    if (
        frame_left > maximum_outer_inset
        or image.width - 1 - frame_right > maximum_outer_inset
    ):
        return image
    rgba = image.convert("RGBA")
    if np is not None:
        arr = np.asarray(rgba, dtype=np.uint8)
        alpha = arr[..., 3]
        rgb = arr[..., :3].astype(np.float32)
        luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        saturation = rgb.max(axis=2) - rgb.min(axis=2)
        has_transparency = bool(int(alpha.min()) < 245)
        foreground = (
            alpha > 48
            if has_transparency
            else (luminance <= 246.0) | (saturation >= 24.0)
        )
        column_counts = foreground.sum(axis=0)
    else:
        pixels = rgba.load()
        has_transparency = rgba.getchannel("A").getextrema()[0] < 245
        foreground = [
            [False for _x in range(image.width)]
            for _y in range(image.height)
        ]
        column_counts = [0 for _x in range(image.width)]
        for y in range(image.height):
            for x in range(image.width):
                red, green, blue, alpha = pixels[x, y]
                luminance = 0.299 * red + 0.587 * green + 0.114 * blue
                saturation = max(red, green, blue) - min(red, green, blue)
                visible = (
                    alpha > 48
                    if has_transparency
                    else luminance <= 246 or saturation >= 24
                )
                foreground[y][x] = visible
                column_counts[x] += int(visible)

    safe_gap = max(6, int(round(image.width * 0.008)))
    minimum_guide_ink = max(32, int(round(image.height * 0.25)))
    left_candidates = [
        x
        for x in range(max(0, frame_left - safe_gap))
        if int(column_counts[x]) >= minimum_guide_ink
    ]
    right_candidates = [
        x
        for x in range(min(image.width, frame_right + safe_gap + 1), image.width)
        if int(column_counts[x]) >= minimum_guide_ink
    ]
    if not left_candidates and not right_candidates:
        return image

    erase = (
        np.zeros((image.height, image.width), dtype=bool)
        if np is not None
        else [[False for _x in range(image.width)] for _y in range(image.height)]
    )
    def foreground_at(x: int, y: int) -> bool:
        return bool(foreground[y, x])

    def mark(x: int, y: int) -> None:
        erase[y, x] = True

    def erase_guide(candidates: Sequence[int]) -> None:
        if not candidates:
            return
        guide_min = max(0, min(candidates) - 2)
        guide_max = min(image.width - 1, max(candidates) + 2)
        for y in range(image.height):
            for x in range(guide_min, guide_max + 1):
                if foreground_at(x, y):
                    mark(x, y)

    erase_guide(left_candidates)
    erase_guide(right_candidates)

    # Scanner/page-rule cleanup can leave a detached antialiased endpoint
    # below an otherwise closed passage box. Remove only a genuinely tiny,
    # isolated tail after the last strong horizontal frame rule.
    if np is not None:
        remaining = foreground & ~erase
        frame_width = frame_right - frame_left + 1
        frame_row_counts = remaining[:, frame_left : frame_right + 1].sum(axis=1)
        long_rule_rows = np.flatnonzero(
            frame_row_counts >= int(round(frame_width * 0.70))
        )
        if long_rule_rows.size:
            last_rule = int(long_rule_rows[-1])
            tail_points = np.argwhere(remaining[last_rule + 1 :, :])
            if tail_points.size:
                tail_points[:, 0] += last_rule + 1
                tail_top = int(tail_points[:, 0].min())
                tail_bottom = int(tail_points[:, 0].max())
                tail_left = int(tail_points[:, 1].min())
                tail_right = int(tail_points[:, 1].max())
                if (
                    tail_top - last_rule >= 20
                    and len(tail_points) <= 96
                    and tail_bottom - tail_top + 1 <= 32
                    and tail_right - tail_left + 1 <= 64
                ):
                    erase[tail_points[:, 0], tail_points[:, 1]] = True
    else:
        frame_width = frame_right - frame_left + 1
        long_rule_rows = [
            y
            for y in range(image.height)
            if sum(
                int(foreground[y][x] and not erase[y][x])
                for x in range(frame_left, frame_right + 1)
            )
            >= int(round(frame_width * 0.70))
        ]
        if long_rule_rows:
            last_rule = long_rule_rows[-1]
            tail_points = [
                (x, y)
                for y in range(last_rule + 1, image.height)
                for x in range(image.width)
                if foreground[y][x] and not erase[y][x]
            ]
            if tail_points:
                tail_left = min(point[0] for point in tail_points)
                tail_right = max(point[0] for point in tail_points)
                tail_top = min(point[1] for point in tail_points)
                tail_bottom = max(point[1] for point in tail_points)
                if (
                    tail_top - last_rule >= 20
                    and len(tail_points) <= 96
                    and tail_bottom - tail_top + 1 <= 32
                    and tail_right - tail_left + 1 <= 64
                ):
                    for x, y in tail_points:
                        erase[y][x] = True

    output = rgba.copy()
    output_pixels = output.load()
    fill = (255, 255, 255, 0) if has_transparency else (255, 255, 255, 255)
    if np is not None:
        ys, xs = np.nonzero(erase)
        for x, y in zip(xs.tolist(), ys.tolist()):
            output_pixels[x, y] = fill
    else:
        for y in range(image.height):
            for x in range(image.width):
                if erase[y][x]:
                    output_pixels[x, y] = fill
    return output if "A" in image.getbands() else output.convert(image.mode)


def _prepare_passage_segments_for_stitch(
    images: Sequence[Image.Image],
    *,
    crop_boxes_output: list[tuple[int, int, int, int]] | None = None,
) -> list[Image.Image]:
    if len(images) <= 1:
        if crop_boxes_output is not None:
            crop_boxes_output.extend((0, 0, image.width, image.height) for image in images)
        return [_erase_passage_outer_margin_page_guides(image) for image in images]
    prepared = [
        _trim_passage_segment_for_join(
            image,
            trim_top=index > 0,
            trim_bottom=index < len(images) - 1,
            crop_box_output=crop_boxes_output,
        )
        for index, image in enumerate(images)
    ]
    return [
        _erase_passage_outer_margin_page_guides(image)
        for image in prepared
    ]


def _passage_frame_edges(image: Image.Image) -> tuple[int, int] | None:
    """Return strong left/right passage-frame columns when both are present."""
    if image.width < 80 or image.height < 80:
        return None
    if "A" in image.getbands():
        signal = image.convert("RGBA").getchannel("A")
    else:
        signal = ImageOps.invert(image.convert("L"))
    # Reducing the full height to one row turns a long vertical rule into a
    # strong signal while ordinary glyph columns average out.
    column_signal = signal.resize((image.width, 1), Image.Resampling.BOX)
    values = list(column_signal.get_flattened_data())
    outer_band = max(12, int(round(image.width * 0.30)))
    edge_inset = max(1, int(round(image.width * 0.01)))
    left_end = min(image.width // 2, outer_band)
    right_start = max(image.width // 2, image.width - outer_band)
    left_candidates = range(edge_inset, max(edge_inset + 1, left_end))
    right_candidates = range(min(image.width - 1, right_start), max(right_start + 1, image.width - edge_inset))
    left = max(left_candidates, key=lambda x: values[x], default=-1)
    right = max(right_candidates, key=lambda x: values[x], default=-1)
    minimum_strength = 72
    if (
        left < 0
        or right <= left
        or values[left] < minimum_strength
        or values[right] < minimum_strength
        or right - left < image.width * 0.40
    ):
        return None
    return left, right


def _passage_frame_center_x(image: Image.Image) -> float | None:
    """Return the center of a strong left/right passage frame when present."""
    edges = _passage_frame_edges(image)
    return ((edges[0] + edges[1]) * 0.5) if edges is not None else None


def _passage_frame_ink_span(image: Image.Image, x: int) -> tuple[int, int] | None:
    """Find the first/last visible rule pixel in one detected frame column."""
    rgba = image.convert("RGBA")
    x = max(0, min(rgba.width - 1, int(x)))
    pixels = rgba.load()
    has_transparency = rgba.getchannel("A").getextrema()[0] < 245
    visible_rows: list[int] = []
    for y in range(rgba.height):
        red, green, blue, alpha = pixels[x, y]
        if has_transparency:
            visible = alpha > 48
        else:
            luminance = 0.299 * red + 0.587 * green + 0.114 * blue
            visible = luminance < 220 or max(red, green, blue) - min(red, green, blue) >= 24
        if visible:
            visible_rows.append(y)
    if not visible_rows:
        return None
    return visible_rows[0], visible_rows[-1]


def _passage_frame_ink_color(image: Image.Image, x: int) -> tuple[int, int, int, int]:
    """Sample the strongest frame pixel so a seam bridge matches its source."""
    rgba = image.convert("RGBA")
    x = max(0, min(rgba.width - 1, int(x)))
    pixels = rgba.load()
    has_transparency = rgba.getchannel("A").getextrema()[0] < 245

    def strength(pixel: tuple[int, int, int, int]) -> float:
        red, green, blue, alpha = pixel
        if has_transparency:
            return float(alpha)
        return 255.0 - (0.299 * red + 0.587 * green + 0.114 * blue)

    return max((pixels[x, y] for y in range(rgba.height)), key=strength)


def _passage_frame_has_horizontal_closure(
    image: Image.Image,
    edges: tuple[int, int],
    y: int,
) -> bool:
    """Return whether a strong horizontal rule closes a frame near ``y``."""
    left, right = edges
    left = max(0, min(image.width - 1, int(left)))
    right = max(left, min(image.width - 1, int(right)))
    if right - left < 24:
        return False
    rgba = image.convert("RGBA")
    top = max(0, int(y) - 2)
    bottom = min(image.height, int(y) + 3)
    if top >= bottom:
        return False
    if np is not None:
        arr = np.asarray(rgba, dtype=np.uint8)[top:bottom, left : right + 1]
        alpha = arr[..., 3]
        rgb = arr[..., :3].astype(np.float32)
        luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        saturation = rgb.max(axis=2) - rgb.min(axis=2)
        has_transparency = bool(int(alpha.min()) < 245)
        foreground = (
            alpha > 48
            if has_transparency
            else (luminance < 220.0) | (saturation >= 24.0)
        )
        return bool(
            int(foreground.sum(axis=1).max(initial=0))
            >= int(round((right - left + 1) * 0.60))
        )

    pixels = rgba.load()
    has_transparency = rgba.getchannel("A").getextrema()[0] < 245
    threshold = int(round((right - left + 1) * 0.60))
    for row in range(top, bottom):
        count = 0
        for x in range(left, right + 1):
            red, green, blue, alpha = pixels[x, row]
            luminance = 0.299 * red + 0.587 * green + 0.114 * blue
            saturation = max(red, green, blue) - min(red, green, blue)
            visible = (
                alpha > 48
                if has_transparency
                else luminance < 220 or saturation >= 24
            )
            count += int(visible)
        if count >= threshold:
            return True
    return False


def _bridge_aligned_passage_frames(
    stitched: Image.Image,
    *,
    prepared: Sequence[Image.Image],
    x_offsets: Sequence[int],
    y_offsets: Sequence[int],
    frame_edges: Sequence[tuple[int, int] | None],
) -> None:
    """Close only confidently aligned frame-rule gaps between adjacent fragments."""
    draw = ImageDraw.Draw(stitched)
    for index in range(len(prepared) - 1):
        upper_edges = frame_edges[index]
        lower_edges = frame_edges[index + 1]
        if upper_edges is None or lower_edges is None:
            continue
        upper = prepared[index]
        lower = prepared[index + 1]
        upper_spans = [
            _passage_frame_ink_span(upper, edge)
            for edge in upper_edges
        ]
        lower_spans = [
            _passage_frame_ink_span(lower, edge)
            for edge in lower_edges
        ]
        if any(span is None for span in (*upper_spans, *lower_spans)):
            continue
        resolved_upper_spans = [
            span for span in upper_spans if span is not None
        ]
        resolved_lower_spans = [
            span for span in lower_spans if span is not None
        ]
        upper_join_y = max(span[1] for span in resolved_upper_spans)
        lower_join_y = min(span[0] for span in resolved_lower_spans)
        # Closed (A)/(B) boxes are adjacent independent sections, not one
        # frame split across pages. Extending their side rules creates false
        # vertical and T-shaped artifacts in the reading gap.
        if _passage_frame_has_horizontal_closure(
            upper,
            upper_edges,
            upper_join_y,
        ) or _passage_frame_has_horizontal_closure(
            lower,
            lower_edges,
            lower_join_y,
        ):
            continue
        for edge_index, (upper_edge, lower_edge) in enumerate(
            zip(upper_edges, lower_edges)
        ):
            upper_global_x = x_offsets[index] + upper_edge
            lower_global_x = x_offsets[index + 1] + lower_edge
            # Different frame widths should keep their natural geometry. A
            # bridge is safe only when both source rules already share an axis.
            if abs(upper_global_x - lower_global_x) > 2:
                continue
            upper_span = resolved_upper_spans[edge_index]
            lower_span = resolved_lower_spans[edge_index]
            x = int(round((upper_global_x + lower_global_x) * 0.5))
            start_y = y_offsets[index] + upper_span[1]
            end_y = y_offsets[index + 1] + lower_span[0]
            if end_y <= start_y:
                continue
            color = _passage_frame_ink_color(upper, upper_edge)
            draw.line((x, start_y, x, end_y), fill=color, width=1)


def _compose_passage_segments(
    images: Sequence[Image.Image],
    *,
    transparent: bool,
    placements_output: list[dict[str, int]] | None = None,
) -> Image.Image:
    """Join passage fragments on one stable reading axis.

    Source columns and pages can differ by a few pixels after recovery and
    chrome cleanup. Centering each fragment avoids a visible left-edge jump
    while preserving the original font scale of every segment.
    """
    if not images:
        raise ValueError("At least one passage image is required for stitching")
    crop_boxes: list[tuple[int, int, int, int]] = []
    prepared = _prepare_passage_segments_for_stitch(
        images,
        crop_boxes_output=crop_boxes,
    )
    max_width = max(image.width for image in prepared)
    gap = PASSAGE_FRAGMENT_STITCH_GAP_PX if len(prepared) > 1 else 0
    total_height = sum(image.height for image in prepared) + gap * max(0, len(prepared) - 1)
    mode = "RGBA" if transparent else "RGB"
    fill = (255, 255, 255, 0) if transparent else (255, 255, 255)
    frame_edges = [_passage_frame_edges(image) for image in prepared]
    frame_centers = [
        ((edges[0] + edges[1]) * 0.5) if edges is not None else None
        for edges in frame_edges
    ]
    base_offsets = [max(0, (max_width - image.width) // 2) for image in prepared]
    effective_frame_centers = [
        base_offset + center if center is not None else None
        for base_offset, center in zip(base_offsets, frame_centers)
    ]
    valid_frame_centers = [center for center in effective_frame_centers if center is not None]
    frame_offsets = [0] * len(prepared)
    if len(valid_frame_centers) >= 2:
        target_center = max(valid_frame_centers)
        frame_offsets = [
            max(0, int(round(target_center - center))) if center is not None else 0
            for center in effective_frame_centers
        ]
    stitched_width = max(
        max_width,
        *(image.width + offset for image, offset in zip(prepared, frame_offsets)),
    )
    stitched = Image.new(mode, (stitched_width, total_height), fill)
    cursor_y = 0
    x_offsets: list[int] = []
    y_offsets: list[int] = []
    for image, base_offset, frame_offset in zip(prepared, base_offsets, frame_offsets):
        x = base_offset + frame_offset
        x_offsets.append(x)
        y_offsets.append(cursor_y)
        if transparent:
            stitched.alpha_composite(image.convert("RGBA"), (x, cursor_y))
        else:
            stitched.paste(image.convert("RGB"), (x, cursor_y))
        cursor_y += image.height + gap
    if placements_output is not None:
        placements_output.clear()
        placements_output.extend(
            {
                "x": x,
                "y": y,
                "crop_left": crop_box[0],
                "crop_top": crop_box[1],
                "crop_right": crop_box[2],
                "crop_bottom": crop_box[3],
            }
            for x, y, crop_box in zip(x_offsets, y_offsets, crop_boxes)
        )
    if gap > 0:
        _bridge_aligned_passage_frames(
            stitched,
            prepared=prepared,
            x_offsets=x_offsets,
            y_offsets=y_offsets,
            frame_edges=frame_edges,
        )
    return stitched


def _media_regions_after_stitch(
    region_groups: Sequence[Sequence[Mapping[str, Any]]],
    placements: Sequence[Mapping[str, int]],
) -> list[dict[str, Any]]:
    stitched_regions: list[dict[str, Any]] = []
    for regions, placement in zip(region_groups, placements):
        crop_left = float(placement.get("crop_left", 0))
        crop_top = float(placement.get("crop_top", 0))
        crop_right = float(placement.get("crop_right", 0))
        crop_bottom = float(placement.get("crop_bottom", 0))
        offset_x = float(placement.get("x", 0)) - crop_left
        offset_y = float(placement.get("y", 0)) - crop_top
        for region in regions:
            box = _media_region_box(region)
            if box is None:
                continue
            left, top, right, bottom = box
            left = max(left, crop_left)
            top = max(top, crop_top)
            right = min(right, crop_right)
            bottom = min(bottom, crop_bottom)
            if right <= left or bottom <= top:
                continue
            payload = {key: value for key, value in region.items() if key != "bbox"}
            payload["bbox"] = {
                "left": left + offset_x,
                "top": top + offset_y,
                "right": right + offset_x,
                "bottom": bottom + offset_y,
                "width": right - left,
                "height": bottom - top,
            }
            stitched_regions.append(payload)
    return stitched_regions


def _stitch_passage_image_files(
    paths: Sequence[Path],
    output_path: Path,
    *,
    transparent: bool,
    source_region_groups: Sequence[Sequence[Mapping[str, Any]]] | None = None,
    stitched_regions_output: list[dict[str, Any]] | None = None,
    stitch_diagnostics_output: dict[str, Any] | None = None,
    source_crop_boxes_output: list[tuple[int, int, int, int]] | None = None,
) -> tuple[int, int]:
    images: list[Image.Image] = []
    for path in paths:
        with Image.open(path) as loaded:
            images.append(
                loaded.convert("RGBA").copy()
                if transparent
                else _flatten_passage_segment_on_white(loaded).copy()
            )
    if not images:
        raise ValueError("At least one passage image is required for stitching")
    placements: list[dict[str, int]] = []
    stitched = _compose_passage_segments(
        images,
        transparent=transparent,
        placements_output=placements,
    )
    if source_crop_boxes_output is not None:
        source_crop_boxes_output.clear()
        source_crop_boxes_output.extend(
            (
                int(placement.get("crop_left", 0)),
                int(placement.get("crop_top", 0)),
                int(placement.get("crop_right", images[index].width)),
                int(placement.get("crop_bottom", images[index].height)),
            )
            for index, placement in enumerate(placements)
        )
    if stitched_regions_output is not None:
        stitched_regions_output.clear()
        if source_region_groups is not None:
            stitched_regions_output.extend(
                _media_regions_after_stitch(source_region_groups, placements)
            )
    if stitch_diagnostics_output is not None:
        row_counts = _passage_foreground_row_counts(stitched)
        blank_runs = _blank_row_runs(row_counts, end=stitched.height)
        join_blank_bands: list[int] = []
        for placement in placements[1:]:
            join_y = int(placement.get("y", 0))
            matching_run = next(
                (
                    (start, end)
                    for start, end in blank_runs
                    if start <= max(0, join_y - 1) < end or start <= join_y < end
                ),
                None,
            )
            join_blank_bands.append(
                matching_run[1] - matching_run[0]
                if matching_run is not None
                else 0
            )
        stitch_diagnostics_output.clear()
        stitch_diagnostics_output.update(
            {
                "join_count": max(0, len(placements) - 1),
                "join_blank_band_px": join_blank_bands,
                "max_join_blank_band_px": max(join_blank_bands, default=0),
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stitched.save(
        output_path,
        format="PNG",
        compress_level=INTERMEDIATE_PNG_COMPRESSION_LEVEL,
        optimize=False,
    )
    return stitched.size


def _coalesce_cross_page_passage_drafts(
    drafts: list[_ProblemEntryDraft],
    crop_sizes: list[tuple[int, int]],
    pages: Sequence[PageModel],
) -> tuple[list[_ProblemEntryDraft], list[tuple[int, int]]]:
    problem_units_by_id = {
        problem.unit_id: problem
        for page in pages
        for problem in page.problems
    }
    fragment_indices_by_group: dict[str, list[int]] = {}
    for index, draft in enumerate(drafts):
        problem = problem_units_by_id.get(draft.problem_id)
        if problem is None or not _problem_is_passage_fragment_unit(problem):
            continue
        group_id = str(problem.metadata.get("passage_group_id") or "").strip()
        if group_id:
            fragment_indices_by_group.setdefault(group_id, []).append(index)

    removed_indices: set[int] = set()
    updated_crop_sizes = list(crop_sizes)
    for group_id, fragment_indices in fragment_indices_by_group.items():
        ordered_candidates = sorted(
            fragment_indices,
            key=lambda index: (
                int(drafts[index].prepared_page.page_number),
                index,
            ),
        )
        ordered_indices: list[int] = []
        seen_fragment_ids: set[str] = set()
        for index in ordered_candidates:
            fragment_id = drafts[index].problem_id
            if fragment_id in seen_fragment_ids:
                removed_indices.add(index)
                continue
            seen_fragment_ids.add(fragment_id)
            ordered_indices.append(index)
        source_page_ids = list(
            dict.fromkeys(drafts[index].source_page_id for index in ordered_indices)
        )
        if len(ordered_indices) < 2 or len(source_page_ids) < 2:
            continue

        group_problems = [
            problem
            for problem in problem_units_by_id.values()
            if str(problem.metadata.get("passage_group_id") or "").strip() == group_id
        ]
        expected_source_page_ids: list[str] = []
        expected_fragment_count = 0
        precise_fragment_source_page_ids: list[str] = []
        for problem in group_problems:
            raw_fragment_source_page_ids = problem.metadata.get(
                "passage_fragment_source_page_ids"
            )
            if isinstance(raw_fragment_source_page_ids, list):
                for page_id in raw_fragment_source_page_ids:
                    normalized_page_id = str(page_id or "").strip()
                    if (
                        normalized_page_id
                        and normalized_page_id not in precise_fragment_source_page_ids
                    ):
                        precise_fragment_source_page_ids.append(normalized_page_id)
            raw_source_page_ids = problem.metadata.get("passage_source_page_ids")
            if isinstance(raw_source_page_ids, list):
                for page_id in raw_source_page_ids:
                    normalized_page_id = str(page_id or "").strip()
                    if normalized_page_id and normalized_page_id not in expected_source_page_ids:
                        expected_source_page_ids.append(normalized_page_id)
            expected_fragment_count = max(
                expected_fragment_count,
                _coerce_int(problem.metadata.get("passage_fragment_count")) or 0,
            )
        if precise_fragment_source_page_ids:
            expected_source_page_ids = precise_fragment_source_page_ids
            expected_fragment_count = len(precise_fragment_source_page_ids)
        missing_source_page_ids = [
            page_id for page_id in expected_source_page_ids if page_id not in source_page_ids
        ]
        coverage_incomplete = bool(
            missing_source_page_ids
            or (expected_fragment_count > 0 and len(source_page_ids) < expected_fragment_count)
        )
        if coverage_incomplete:
            for problem in group_problems:
                problem.metadata["passage_merge_incomplete"] = True
                problem.metadata["passage_merge_missing_source_page_ids"] = list(
                    missing_source_page_ids
                )
                problem.metadata["passage_merge_detected_source_page_ids"] = list(source_page_ids)
            continue

        primary_index = ordered_indices[0]
        primary = drafts[primary_index]
        fragment_ids = [drafts[index].problem_id for index in ordered_indices]
        primary_crop_paths = [drafts[index].crop_path for index in ordered_indices]
        stitched_regions: list[dict[str, Any]] = []
        stitch_diagnostics: dict[str, Any] = {}
        updated_crop_sizes[primary_index] = _stitch_passage_image_files(
            primary_crop_paths,
            primary.crop_path,
            transparent=False,
            source_region_groups=[drafts[index].preserve_media_regions for index in ordered_indices],
            stitched_regions_output=stitched_regions,
            stitch_diagnostics_output=stitch_diagnostics,
        )
        primary.preserve_media_regions = stitched_regions
        primary.source_segments = [
            dict(segment)
            for index in ordered_indices
            for segment in drafts[index].source_segments
            if isinstance(segment, dict)
        ]
        if primary.asset_task is not None:
            primary.asset_task.rendered_media_regions = list(stitched_regions)
        _stitch_passage_image_files(
            [drafts[index].board_render_path for index in ordered_indices],
            primary.board_render_path,
            transparent=True,
        )

        # A stitched passage is a single rendered image. Keeping blocks from
        # later pages would make mixed-mode export crop those bboxes from the
        # first page again, so deliberately fall back to the composite image.
        primary.blocks = []
        merged_risk_flags: list[str] = []
        fragment_problems = [
            problem_units_by_id.get(drafts[index].problem_id)
            for index in ordered_indices
        ]
        fragment_problems = [problem for problem in fragment_problems if problem is not None]
        primary_problem = problem_units_by_id.get(primary.problem_id)
        if primary_problem is not None and fragment_problems:
            primary_problem.metadata["passage_stitch_diagnostics"] = stitch_diagnostics
            primary_problem.metadata["passage_text_line_count"] = sum(
                max(0, _coerce_int(problem.metadata.get("passage_text_line_count")) or 0)
                for problem in fragment_problems
            )
            primary_problem.metadata["passage_text_character_count"] = sum(
                max(0, _coerce_int(problem.metadata.get("passage_text_character_count")) or 0)
                for problem in fragment_problems
            )
            bounds_scores = [
                float(problem.metadata["passage_text_bounds_score"])
                for problem in fragment_problems
                if isinstance(problem.metadata.get("passage_text_bounds_score"), (int, float))
            ]
            if bounds_scores:
                primary_problem.metadata["passage_text_bounds_score"] = min(bounds_scores)
            detection_scores = [
                float(problem.metadata["passage_detection_confidence"])
                for problem in fragment_problems
                if isinstance(problem.metadata.get("passage_detection_confidence"), (int, float))
            ]
            if detection_scores:
                primary_problem.metadata["passage_detection_confidence"] = min(detection_scores)
        for index in ordered_indices:
            for flag in drafts[index].risk_flags:
                if flag != PASSAGE_CROSS_PAGE_MERGE_CHECK_RISK_FLAG and flag not in merged_risk_flags:
                    merged_risk_flags.append(flag)
        primary.risk_flags = merged_risk_flags
        removed_indices.update(ordered_indices[1:])

        for problem in group_problems:
            metadata = problem.metadata
            metadata.pop("passage_merge_incomplete", None)
            metadata.pop("passage_merge_missing_source_page_ids", None)
            metadata.pop("passage_merge_detected_source_page_ids", None)
            metadata["passage_fragments_merged"] = True
            metadata["passage_merged_fragment_ids"] = list(fragment_ids)
            metadata["passage_merged_source_page_ids"] = list(source_page_ids)
            metadata["passage_merged_fragment_count"] = len(fragment_ids)
            if _problem_is_passage_fragment_unit(problem) and problem.unit_id != primary.problem_id:
                metadata["passage_merged_into_problem_id"] = primary.problem_id

        for draft in drafts:
            problem = problem_units_by_id.get(draft.problem_id)
            if problem is None:
                continue
            if str(problem.metadata.get("passage_group_id") or "").strip() != group_id:
                continue
            draft.risk_flags = [
                flag
                for flag in draft.risk_flags
                if flag != PASSAGE_CROSS_PAGE_MERGE_CHECK_RISK_FLAG
            ]

    if not removed_indices:
        return drafts, crop_sizes
    kept_indices = [index for index in range(len(drafts)) if index not in removed_indices]
    return (
        [drafts[index] for index in kept_indices],
        [updated_crop_sizes[index] for index in kept_indices],
    )


def _annotate_passage_crop_quality(
    drafts: Sequence[_ProblemEntryDraft],
    pages: Sequence[PageModel],
) -> None:
    problems_by_id = {
        problem.unit_id: problem
        for page in pages
        for problem in page.problems
    }
    for draft in drafts:
        problem = problems_by_id.get(draft.problem_id)
        if problem is None or not _problem_is_passage_fragment_unit(problem):
            continue
        try:
            with Image.open(draft.crop_path) as loaded:
                quality = assess_passage_crop_quality(
                    loaded,
                    source_dpi=draft.prepared_page.metadata.get("dpi"),
                    detection_confidence=problem.metadata.get("passage_detection_confidence"),
                    text_line_count=int(problem.metadata.get("passage_text_line_count") or 0),
                    text_character_count=int(problem.metadata.get("passage_text_character_count") or 0),
                    text_bounds_score=problem.metadata.get("passage_text_bounds_score"),
                    stitch_diagnostics=problem.metadata.get("passage_stitch_diagnostics"),
                )
        except (OSError, ValueError, TypeError):
            quality = {
                "overall_score": 0.0,
                "score_10": 0.0,
                "grade": "poor",
                "warnings": ["passage_crop_quality_unavailable"],
            }
        problem.metadata["passage_quality"] = quality


def build_problem_entries(
    prepared_pages: list[PreparedPage],
    pages: list[PageModel],
    output_dir: Path,
    template: LayoutTemplate,
    *,
    board_theme: str = DEFAULT_BOARD_THEME,
    content_target: str = "all",
) -> list[ProblemEntry]:
    crop_dir = output_dir / "problem_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    cutout_dir = output_dir / "problem_cutouts"
    cutout_dir.mkdir(parents=True, exist_ok=True)
    chalk_color = _resolve_chalk_color(board_theme)
    resolved_content_target = _normalize_content_target(content_target)
    prepared_by_page_id = {page.page_id: page for page in prepared_pages}
    drafts: list[_ProblemEntryDraft] = []
    _remove_duplicate_marker_document_problem_numbers(pages)
    _remove_hwp_template_instruction_problems(pages)
    _restore_hwp_text_fallback_problems(pages)
    _annotate_cross_page_passage_groups(pages)
    _materialize_pdf_pre_question_passage_continuations(pages, prepared_by_page_id)

    for page in pages:
        prepared_page = prepared_by_page_id.get(page.page_id)
        if prepared_page is None:
            continue
        prepared_page.image.load()
        block_by_id = {block.block_id: block for block in page.blocks}

        # Reorder problems by their first block's top y so the "next problem"
        # boundary used for gap-filling matches reading order even when the
        # grouping pass produced them out of order.
        ordered_problems = sorted(
            page.problems,
            key=lambda p: _problem_order_key(p, block_by_id),
        )

        # Drop pre-first-problem header bands (e.g. cover-page title, 성명 /
        # 수험번호 form, "물리학I" / "과학탐구" subject header). When the page
        # contains a numbered problem, anything that lands above the first
        # numbered problem with no number of its own and no choice marker is
        # treated as page chrome — it would otherwise get bundled into the
        # first problem's crop (or worse, surface as its own pseudo-problem).
        ordered_problems = _drop_pre_first_problem_headers(ordered_problems, block_by_id)

        continuation_problems = _collapse_marker_document_continuation_page(page, ordered_problems, block_by_id)
        if continuation_problems is not None:
            if not continuation_problems:
                page.problems = []
                continue
            page.problems = continuation_problems
            ordered_problems = continuation_problems

        visual_ordered_problems = sorted(
            ordered_problems,
            key=lambda p: _problem_visual_order_key(p, block_by_id),
        )
        _fill_missing_problem_numbers(visual_ordered_problems)
        ordered_problems = sorted(
            visual_ordered_problems,
            key=lambda p: _problem_order_key(p, block_by_id),
        )
        next_problem_for_crop = _build_crop_next_problem_map(ordered_problems, block_by_id)

        all_assigned_ids: set[str] = set()
        for prob in ordered_problems:
            all_assigned_ids.update(_iter_problem_block_ids_raw(prob))

        for problem in ordered_problems:
            if not _problem_matches_content_target(problem, resolved_content_target):
                continue
            is_shared_passage = _problem_is_passage_fragment_unit(problem)
            trusted_pdf_marker_problem = False
            passage_fragment_problem = is_shared_passage
            next_problem = next_problem_for_crop.get(problem.unit_id)
            own_ids = set(_iter_problem_block_ids_raw(problem))
            other_problem_block_ids = all_assigned_ids - own_ids
            problem_block_ids = iter_problem_block_ids(page, problem)
            own_blocks = [block_by_id[block_id] for block_id in problem_block_ids if block_id in block_by_id]
            gap_filled = _expand_problem_blocks_by_gap(
                page, problem, next_problem, block_by_id, other_problem_block_ids
            )
            blocks = gap_filled if gap_filled else own_blocks
            passage_segment_blocks = sorted(
                (
                    block
                    for block in own_blocks
                    if block.metadata.get("segmenter")
                    in {"pdf-passage-range", "pdf-cross-page-passage-continuation"}
                    and not _is_probable_passage_continuation_page_header(page, block)
                ),
                key=lambda block: (
                    int(block.metadata.get("passage_fragment_index") or 0),
                    int(block.metadata.get("column_index") or 0),
                    block.bbox.left,
                    block.reading_order,
                ),
            )
            stitched_segment_bounds = (
                tuple(block.bbox for block in passage_segment_blocks)
                if str(problem.metadata.get("passage_role") or "") == "passage_fragment"
                and len(passage_segment_blocks) > 1
                else None
            )
            passage_segment_column_indices = (
                tuple(_coerce_int(block.metadata.get("column_index")) for block in passage_segment_blocks)
                if passage_segment_blocks
                else None
            )
            passage_source_segments = [
                {
                    "source_page_id": page.page_id,
                    "column_index": _coerce_int(block.metadata.get("column_index")) or 1,
                    "fragment_index": (
                        _coerce_int(block.metadata.get("passage_fragment_index")) or index
                    ),
                    "bbox": {
                        "left": float(block.bbox.left),
                        "top": float(block.bbox.top),
                        "width": float(block.bbox.width),
                        "height": float(block.bbox.height),
                    },
                }
                for index, block in enumerate(passage_segment_blocks, start=1)
            ]
            if passage_fragment_problem and passage_segment_blocks:
                blocks = passage_segment_blocks
            explicit_passage_range = (
                passage_fragment_problem and bool(passage_segment_blocks)
            )
            blocks = _filter_page_chrome_blocks(page, blocks)
            raw_problem_number = problem.metadata.get("problem_number")
            if isinstance(raw_problem_number, int):
                problem_number = raw_problem_number
            elif isinstance(raw_problem_number, str) and raw_problem_number.isdigit():
                problem_number = int(raw_problem_number)
            else:
                problem_number = None
            force_full_page_bounds = bool(problem.metadata.get("force_full_page_bounds"))
            preserve_page_as_is = (
                force_full_page_bounds
                and str(problem.metadata.get("input_intent") or page.metadata.get("input_intent") or "") == "page-as-is"
            )
            if force_full_page_bounds:
                blocks = list(page.sorted_blocks())
                merged_box = Box(left=0.0, top=0.0, width=float(page.width_px), height=float(page.height_px))
            else:
                boxes = [block.bbox for block in blocks]
                metadata_box = _problem_metadata_bbox(problem, page)
                if metadata_box is not None:
                    if _should_prefer_problem_metadata_bbox(problem):
                        boxes = [metadata_box]
                    else:
                        boxes.append(metadata_box)
                if not boxes:
                    boxes = [Box(left=0.0, top=0.0, width=float(page.width_px), height=float(page.height_px))]
                has_document_band_metadata = any("question_band_index" in block.metadata for block in blocks)
                trusted_pdf_marker_problem = _is_trusted_pdf_text_marker_problem(problem, blocks)
                has_choice_blocks = any(block.block_type == BlockType.CHOICE for block in blocks)
                bottom_padding_px = (
                    0
                    if explicit_passage_range
                    else max(DOCUMENT_BAND_BOTTOM_PADDING_PX, CHOICE_BOTTOM_SAFE_PADDING_PX)
                    if has_choice_blocks
                    else DOCUMENT_BAND_BOTTOM_PADDING_PX
                    if has_document_band_metadata
                    else PROBLEM_PADDING_PX
                )
                top_padding_px = (
                    0
                    if explicit_passage_range
                    else PDF_TEXT_MARKER_TOP_PADDING_PX
                    if trusted_pdf_marker_problem
                    else int(DOCUMENT_BAND_TOP_PADDING_PX)
                    if has_document_band_metadata
                    else PROBLEM_PADDING_PX
                )
                horizontal_padding_px = (
                    0
                    if explicit_passage_range
                    else PDF_TEXT_MARKER_HORIZONTAL_PADDING_PX
                    if trusted_pdf_marker_problem
                    else PROBLEM_PADDING_PX
                )
                content_bottom = (
                    max(block.bbox.bottom for block in blocks)
                    if blocks
                    else max(box.bottom for box in boxes)
                )
                min_bottom = (
                    min(float(page.height_px), content_bottom + float(bottom_padding_px))
                    if has_choice_blocks
                    else None
                )
                merged_box = merge_boxes(
                    boxes,
                    page_width=page.width_px,
                    page_height=page.height_px,
                    padding_px=horizontal_padding_px,
                    top_padding_px=top_padding_px,
                    bottom_padding_px=bottom_padding_px,
                )
                if has_document_band_metadata and not explicit_passage_range:
                    merged_box = _clamp_box_to_next_problem(
                        merged_box,
                        next_problem,
                        block_by_id,
                        min_bottom=min_bottom,
                    )
                if not explicit_passage_range:
                    merged_box = _expand_box_for_edge_content(
                        prepared_page.image,
                        merged_box,
                        top_extra_px=(
                            PDF_TEXT_MARKER_EDGE_TOP_EXTRA_PADDING_PX
                            if trusted_pdf_marker_problem
                            else PROBLEM_EDGE_TOP_EXTRA_PADDING_PX
                        ),
                        bottom_extra_px=(
                            PROBLEM_CHOICE_EDGE_BOTTOM_EXTRA_PADDING_PX
                            if has_choice_blocks
                            else PROBLEM_EDGE_BOTTOM_EXTRA_PADDING_PX
                        ),
                    )
                if has_document_band_metadata and not explicit_passage_range:
                    merged_box = _clamp_box_to_next_problem(
                        merged_box,
                        next_problem,
                        block_by_id,
                        min_bottom=min_bottom,
                    )
                if not explicit_passage_range:
                    merged_box = _trim_box_bottom_page_chrome(
                        prepared_page.image,
                        merged_box,
                        content_bottom=content_bottom,
                    )
                if (
                    not has_document_band_metadata
                    and not _problem_is_passage_scoped_unit(problem)
                    and merged_box.area < float(page.width_px * page.height_px) * MIN_PROBLEM_AREA_RATIO
                ):
                    merged_box = Box(left=0.0, top=0.0, width=float(page.width_px), height=float(page.height_px))
                    blocks = list(page.sorted_blocks())

            entry_index = len(drafts) + 1
            crop_name = f"problem_{entry_index:03d}_{hashlib.sha1(problem.unit_id.encode('utf-8', errors='ignore')).hexdigest()[:8]}.png"
            source_asset_path = Path(prepared_page.source_path).resolve()
            reuse_full_page_asset = (
                preserve_page_as_is
                and source_asset_path.is_file()
                and prepared_page.image.size == prepared_page.original_size
            )
            crop_path = source_asset_path if reuse_full_page_asset else crop_dir / crop_name
            board_render_path = crop_path if reuse_full_page_asset else cutout_dir / crop_name
            reading_heavy = problem.subject in {Subject.KOREAN, Subject.ENGLISH, Subject.SOCIAL, Subject.SCIENCE}
            problem_title = _problem_entry_title(problem, problem_number, entry_index)
            text_fallback_payload = (
                str(problem.metadata.get("hwp_text_fallback_text") or "").strip()
                if problem.metadata.get("hwp_text_fallback_problem")
                else ""
            )
            problem_input_intent = _normalize_input_intent(
                str(problem.metadata.get("input_intent") or problem.metadata.get("inputIntent") or "")
            )
            if problem_input_intent == "auto":
                problem_input_intent = ""
            drafts.append(
                _ProblemEntryDraft(
                    problem_id=problem.unit_id,
                    title=problem_title,
                    problem_number=problem_number,
                    subject=problem.subject,
                    source_page_id=page.page_id,
                    source_path=prepared_page.source_path,
                    prepared_page=prepared_page,
                    bounds=merged_box,
                    crop_path=crop_path,
                    board_render_path=board_render_path,
                    blocks=sorted(blocks, key=lambda block: (block.reading_order, block.bbox.top, block.bbox.left)),
                    overflow_allowed=reading_heavy,
                    reading_heavy=reading_heavy,
                    risk_flags=_collect_problem_risk_flags(problem),
                    processing_step=_default_processing_step_for_problem(problem),
                    placement_scale_ratio=_default_placement_scale_for_problem(problem),
                    preserve_legacy_placement_scale=_mapping_preserves_legacy_placement_scale(problem.metadata),
                    input_intent=problem_input_intent or None,
                    force_full_page_bounds=bool(problem.metadata.get("force_full_page_bounds")),
                    preserve_media_regions=(
                        [
                            dict(region)
                            for region in (prepared_page.metadata.get("pdf_media_regions") or [])
                            if isinstance(region, dict)
                        ]
                        if reuse_full_page_asset and _problem_allows_selective_media_preservation(problem)
                        else []
                    ),
                    source_segments=passage_source_segments,
                    asset_task=None
                    if reuse_full_page_asset
                    else _ProblemAssetTask(
                        source_image=prepared_page.image,
                        bounds=merged_box,
                        segment_bounds=stitched_segment_bounds,
                        segment_column_indices=passage_segment_column_indices,
                        crop_path=crop_path,
                        board_render_path=board_render_path,
                        chalk_color=chalk_color,
                        text_payload=text_fallback_payload or None,
                        text_title=problem_title if text_fallback_payload else None,
                        trim_edge_guides=not preserve_page_as_is,
                        preserve_horizontal_bounds=passage_fragment_problem,
                        protect_horizontal_bounds=trusted_pdf_marker_problem,
                        horizontal_safe_padding_px=(
                            PASSAGE_CROP_HORIZONTAL_SAFE_PADDING_PX
                            if passage_fragment_problem
                            else 0
                        ),
                        text_priority=_problem_prefers_text_preservation(
                            problem.subject,
                            prepared_page.source_path,
                            problem_title,
                        ),
                        pad_edges=not preserve_page_as_is,
                        subject=problem.subject,
                        source_hints=(
                            prepared_page.source_path,
                            page.page_id,
                            problem_title,
                        ),
                        source_media_regions=(
                            tuple(
                                region
                                for region in (prepared_page.metadata.get("pdf_media_regions") or [])
                                if isinstance(region, dict)
                            )
                            if _problem_allows_selective_media_preservation(problem)
                            else ()
                        ),
                    ),
                )
            )

    rendered_crop_sizes = iter(
        _render_problem_assets([draft.asset_task for draft in drafts if draft.asset_task is not None])
    )
    crop_sizes = [
        draft.prepared_page.image.size if draft.asset_task is None else next(rendered_crop_sizes)
        for draft in drafts
    ]
    for draft in drafts:
        if draft.asset_task is not None:
            draft.preserve_media_regions = list(draft.asset_task.rendered_media_regions)
    drafts, crop_sizes = _coalesce_cross_page_passage_drafts(drafts, crop_sizes, pages)
    _annotate_passage_crop_quality(drafts, pages)
    entries: list[ProblemEntry] = []
    for draft, crop_size in zip(drafts, crop_sizes):
        actual_height_pages = (
            estimate_page_as_is_height_pages(crop_size, template)
            if draft.input_intent == "page-as-is"
            else estimate_height_pages(crop_size, template)
        )
        entries.append(
            ProblemEntry(
                problem_id=draft.problem_id,
                title=draft.title,
                problem_number=draft.problem_number,
                subject=draft.subject,
                source_page_id=draft.source_page_id,
                source_path=draft.source_path,
                prepared_page=draft.prepared_page,
                bounds=draft.bounds,
                crop_path=draft.crop_path,
                board_render_path=draft.board_render_path,
                blocks=draft.blocks,
                actual_height_pages=actual_height_pages,
                overflow_allowed=draft.overflow_allowed,
                reading_heavy=draft.reading_heavy,
                risk_flags=draft.risk_flags,
                placement_scale_ratio=draft.placement_scale_ratio,
                preserve_legacy_placement_scale=draft.preserve_legacy_placement_scale,
                processing_step=draft.processing_step,
                input_intent=draft.input_intent,
                force_full_page_bounds=draft.force_full_page_bounds,
                preserve_media_regions=list(draft.preserve_media_regions),
                source_segments=list(draft.source_segments),
                board_render_preprocessed=draft.asset_task is not None,
            )
        )
    return entries


def _to_file_uri(path: str | Path | None) -> str | None:
    if path is None:
        return None
    return Path(path).resolve().as_uri()


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
        and provider in {"openai", "gemini"}
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


def _summarize_ocr_usage(pages: list[PageModel]) -> dict[str, Any]:
    """Report which OCR backend(s) actually ran. Exposes the common 'auto
    silently resolved to NoOcrBackend' failure so the UI/log can flag it."""
    backend_counts: dict[str, int] = {}
    empty_text_count = 0
    populated_text_count = 0
    total_blocks = 0
    token_usage_events: list[dict[str, Any]] = []
    for page in pages:
        for block in page.blocks:
            total_blocks += 1
            backend = str(block.metadata.get("ocr_backend") or "unknown")
            backend_counts[backend] = backend_counts.get(backend, 0) + 1
            if block.metadata.get("ocr_empty_text"):
                empty_text_count += 1
            elif block.text and block.text.strip():
                populated_text_count += 1
            raw_events = block.metadata.get("ai_token_usage_events")
            if isinstance(raw_events, list):
                token_usage_events.extend(
                    dict(event) for event in raw_events if isinstance(event, dict)
                )
    primary_backend = (
        max(backend_counts.items(), key=lambda item: item[1])[0]
        if backend_counts
        else "unknown"
    )
    return {
        "resolved_backend": primary_backend,
        "backend_counts": backend_counts,
        "block_count": total_blocks,
        "empty_text_block_count": empty_text_count,
        "populated_text_block_count": populated_text_count,
        "no_ocr_fallback_active": primary_backend == "none",
        "token_usage": aggregate_token_usage(token_usage_events),
        "token_usage_events": token_usage_events,
    }


def _summarize_ai_fallback_usage(pages: list[PageModel], ai_fallback_config: dict[str, Any] | None) -> dict[str, Any] | None:
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
    stage_totals: dict[str, dict[str, Any]] = {}
    token_usage_events: list[dict[str, Any]] = []

    def _record_stage(raw_stage: dict[str, Any]) -> None:
        stage = str(raw_stage.get("stage") or "").strip()
        if not stage:
            return
        entry = stage_totals.setdefault(
            stage,
            {
                "stage": stage,
                "order": int(raw_stage.get("order") or 999),
                "label": str(raw_stage.get("label") or stage),
                "provider": str(raw_stage.get("provider") or ""),
                "page_count": 0,
                "used_page_count": 0,
                "status_counts": {},
                "eligible_block_count": 0,
                "processed_block_count": 0,
                "api_call_block_count": 0,
                "cache_hit_count": 0,
                "cache_miss_count": 0,
                "skipped_block_count": 0,
                "attempted_block_count": 0,
                "applied_block_count": 0,
                "attempted_page_count": 0,
                "applied_page_count": 0,
            },
        )
        entry["order"] = min(int(entry.get("order") or 999), int(raw_stage.get("order") or 999))
        if raw_stage.get("label"):
            entry["label"] = str(raw_stage["label"])
        if raw_stage.get("provider"):
            entry["provider"] = str(raw_stage["provider"])
        status = str(raw_stage.get("status") or "unknown")
        status_counts_for_stage = entry["status_counts"]
        status_counts_for_stage[status] = status_counts_for_stage.get(status, 0) + 1
        entry["page_count"] += 1
        if status not in {"skipped", "disabled", "not_needed", "unknown"}:
            entry["used_page_count"] += 1
        if raw_stage.get("attempted"):
            entry["attempted_page_count"] += 1
        if raw_stage.get("applied"):
            entry["applied_page_count"] += 1
        for key in (
            "eligible_block_count",
            "processed_block_count",
            "api_call_block_count",
            "cache_hit_count",
            "cache_miss_count",
            "skipped_block_count",
            "attempted_block_count",
            "applied_block_count",
        ):
            try:
                entry[key] += int(raw_stage.get(key) or 0)
            except (TypeError, ValueError):
                pass

    for page in pages:
        raw_stages = page.metadata.get("ai_stages")
        if isinstance(raw_stages, dict):
            for raw_stage in raw_stages.values():
                if isinstance(raw_stage, dict):
                    _record_stage(raw_stage)

        ai_summary = page.metadata.get("ai_fallback")
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
        raw_token_usage = ai_summary.get("token_usage")
        if isinstance(raw_token_usage, dict):
            token_usage_events.append(dict(raw_token_usage))

        route_decision = page.metadata.get("route_decision")
        if isinstance(route_decision, dict):
            route = str(route_decision.get("route") or "unknown")
            route_counts[route] = route_counts.get(route, 0) + 1
            profile = route_decision.get("profile")
            if isinstance(profile, dict):
                tier = str(profile.get("tier") or "unknown")
                route_tier_counts[tier] = route_tier_counts.get(tier, 0) + 1

        for block in page.blocks:
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
        "token_usage": aggregate_token_usage(token_usage_events),
        "token_usage_events": token_usage_events,
        "stages": sorted(
            stage_totals.values(),
            key=lambda item: (int(item.get("order") or 999), str(item.get("stage") or "")),
        ),
    }


def _combined_ai_usage_events(
    ocr_summary: dict[str, Any] | None,
    ai_summary: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for summary in (ocr_summary, ai_summary):
        if not isinstance(summary, dict):
            continue
        raw_events = summary.get("token_usage_events")
        if isinstance(raw_events, list):
            events.extend(dict(event) for event in raw_events if isinstance(event, dict))
    return events


def _template_to_dict(template: LayoutTemplate) -> dict[str, Any]:
    return {
        "name": template.name,
        "board_page_count": template.board_page_count,
        "base_slot_height_pages": template.base_slot_height_pages,
        "fixed_left_zone_ratio": template.fixed_left_zone_ratio,
        "preserve_right_writing_zone": template.preserve_right_writing_zone,
        "default_overflow_subjects": [subject.value for subject in template.default_overflow_subjects],
        "metadata": dict(template.metadata),
    }


GENERIC_PROBLEM_TITLE_RE = re.compile(r"^\s*(?:문항|문제|臾명빆)\s*\d+(?:\s*[·쨌:\-].*)?$")
INPUT_INTENTS = {"auto", "single-problem", "multi-problem", "page-as-is"}
CONTENT_TARGETS = {"all", "questions", "shared-passages"}


def _normalize_input_intent(value: str | None) -> str:
    normalized = (value or "auto").strip().lower().replace("_", "-")
    return normalized if normalized in INPUT_INTENTS else "auto"


def _normalize_content_target(value: str | None) -> str:
    normalized = (value or "all").strip().lower().replace("_", "-")
    return normalized if normalized in CONTENT_TARGETS else "all"


def _problem_matches_content_target(problem: ProblemUnit, content_target: str | None) -> bool:
    """Select only separately classified shared passages, never in-question content."""
    resolved = _normalize_content_target(content_target)
    is_shared_passage = _problem_is_passage_fragment_unit(problem)
    if resolved == "questions":
        return not is_shared_passage
    if resolved == "shared-passages":
        return is_shared_passage
    return True


def _elapsed_ms(started_at: float) -> int:
    return int(round((time.perf_counter() - started_at) * 1000.0))


def _summarize_page_timing_ms(pages: list[PageModel]) -> dict[str, int]:
    totals = {
        "page_segmentation_sum": 0,
        "page_block_ocr_sum": 0,
        "page_total_before_repair_sum": 0,
        "page_ai_repair_sum": 0,
    }
    for page in pages:
        timing = page.metadata.get("recognition_timing_ms")
        if isinstance(timing, dict):
            for source_key, target_key in (
                ("segmentation", "page_segmentation_sum"),
                ("block_ocr", "page_block_ocr_sum"),
                ("total_before_repair", "page_total_before_repair_sum"),
            ):
                value = timing.get(source_key)
                if isinstance(value, (int, float)):
                    totals[target_key] += int(round(float(value)))
        ai_summary = page.metadata.get("ai_fallback")
        if isinstance(ai_summary, dict):
            value = ai_summary.get("latency_ms")
            if isinstance(value, (int, float)):
                totals["page_ai_repair_sum"] += int(round(float(value)))
    return totals


def _normalize_problem_title(title: str | None, index: int, source_page_id: str, problem_number: int | None = None) -> str:
    raw = (title or "").strip()
    if raw and "problem" not in raw.lower() and not GENERIC_PROBLEM_TITLE_RE.match(raw):
        return raw
    if isinstance(problem_number, int) and problem_number > 0:
        return f"문항 {problem_number}"
    return f"문항 {index + 1:02d} · {source_page_id}"


def recrop_problem(
    page_image: Image.Image,
    bbox: Box,
    crop_path: Path,
) -> tuple[int, int]:
    """Re-crop a problem from its source page image for split/merge mutations.

    Writes a rectangular crop to ``crop_path``. Background removal / cutout
    regeneration is intentionally skipped — that path is reserved for the
    AI (Step 2) workflow. Callers that need a board_render_path can reuse the
    crop_path itself; the board renderer will composite onto the dark theme
    at EDB build time.
    """
    crop = page_image.crop(
        _integer_crop_rect_for_box(
            bbox,
            image_width=page_image.width,
            image_height=page_image.height,
        )
    )
    crop = _trim_source_page_chrome(crop)
    crop = _pad_problem_crop_edges(crop)
    crop_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(crop_path)
    return crop.size


_GLOBAL_RISK_REASONS = {"merged_problem_block", "marker_conflicts"}


def _collect_page_risk_flags(page_metadata: dict[str, Any]) -> list[str]:
    route_decision = page_metadata.get("route_decision")
    if not isinstance(route_decision, dict):
        return []
    profile = route_decision.get("profile") or {}
    reasons = profile.get("reasons") if isinstance(profile, dict) else None
    if not isinstance(reasons, list):
        return []
    return [str(reason) for reason in reasons if isinstance(reason, str)]


def _hwp_source_key(page: PageModel) -> str | None:
    if page.metadata.get("source_type") != "hwp":
        return None
    for key in ("source_hwp_path", "original_source_path", "source_pdf_path", "converted_pdf_path"):
        value = page.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return page.source_path


def _hwp_text_problem_signal_count(metadata: dict[str, Any]) -> int:
    quality = metadata.get("hwp_conversion_quality")
    if not isinstance(quality, dict):
        return 0
    numbered = _coerce_int(quality.get("hwp_text_numbered_problem_count")) or 0
    stem = _coerce_int(quality.get("hwp_text_stem_problem_count")) or 0
    return numbered if numbered > 0 else stem


def _metadata_list_count(metadata: dict[str, Any], key: str) -> int:
    value = metadata.get(key)
    if isinstance(value, list):
        return len(value)
    return _coerce_int(value) or 0


def _is_marker_document_continuation_problem(problem: ProblemUnit) -> bool:
    if bool(problem.metadata.get("marker_document_continuation")):
        return True
    for key in ("review_flags", "risk_flags"):
        values = problem.metadata.get(key)
        if isinstance(values, list) and "marker_document_continuation" in {str(value) for value in values}:
            return True
    return False


def _count_core_hwp_problems(page: PageModel, final_problem_ids: list[str] | None = None) -> int:
    continuation_ids = {
        problem.unit_id
        for problem in page.problems
        if _is_marker_document_continuation_problem(problem)
    }
    if final_problem_ids is not None:
        return sum(1 for problem_id in final_problem_ids if problem_id not in continuation_ids)
    return sum(1 for problem in page.problems if not _is_marker_document_continuation_problem(problem))


HWP_OVERSEGMENTATION_MIN_EXTRA = 10
HWP_OVERSEGMENTATION_RATIO = 2.0


def _is_hwp_oversegmentation(expected: int, detected: int) -> bool:
    if expected <= 0 or detected <= expected:
        return False
    return detected - expected >= HWP_OVERSEGMENTATION_MIN_EXTRA and detected >= expected * HWP_OVERSEGMENTATION_RATIO


def _hwp_problem_counts_match(
    pages: list[PageModel],
    final_problem_ids_by_page_id: dict[str, list[str]] | None = None,
) -> bool:
    by_source: dict[str, dict[str, int]] = {}
    for page in pages:
        source_key = _hwp_source_key(page)
        if not source_key:
            continue
        bucket = by_source.setdefault(source_key, {"detected": 0, "signal": 0})
        if final_problem_ids_by_page_id is None:
            bucket["detected"] += _count_core_hwp_problems(page)
        else:
            bucket["detected"] += _count_core_hwp_problems(
                page,
                final_problem_ids_by_page_id.get(page.page_id, []),
            )
        bucket["signal"] = max(int(bucket["signal"]), _hwp_text_problem_signal_count(page.metadata))
    for bucket in by_source.values():
        signal = int(bucket["signal"])
        if signal <= 0:
            continue
        expected = max(0, signal)
        if expected > 0 and int(bucket["detected"]) == expected:
            return True
    return False


def _session_problem_is_supplemental(problem: dict[str, Any]) -> bool:
    role = str(_session_problem_field(problem, "passageRole", "passage_role") or "").strip()
    if role == "passage_fragment":
        return True
    if bool(_session_problem_field(problem, "supplementalItem", "supplemental_item")):
        return True
    risk_flags = problem.get("riskFlags") or problem.get("risk_flags") or []
    if isinstance(risk_flags, list) and "marker_document_continuation" in {str(flag) for flag in risk_flags}:
        return True
    metadata = problem.get("metadata")
    if isinstance(metadata, dict) and metadata.get("marker_document_continuation"):
        return True
    problem_id = str(problem.get("id") or problem.get("problem_id") or "")
    return problem_id.endswith("-continuation")


def _session_problem_count_payload(problems: list[dict[str, Any]]) -> dict[str, int]:
    detected_count = len(problems)
    supplemental_count = sum(1 for problem in problems if _session_problem_is_supplemental(problem))
    return {
        "detected_problem_count": detected_count,
        "core_problem_count": max(0, detected_count - supplemental_count),
        "supplemental_item_count": supplemental_count,
    }


def _problem_passage_payload(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    payload: dict[str, Any] = {}

    group_id = str(metadata.get("passage_group_id") or "").strip()
    if group_id:
        payload["passageGroupId"] = group_id
        payload["passage_group_id"] = group_id

    passage_range = metadata.get("passage_range")
    if isinstance(passage_range, dict):
        normalized_range = dict(passage_range)
        payload["passageRange"] = normalized_range
        payload["passage_range"] = normalized_range

    role = str(metadata.get("passage_role") or "").strip()
    if role:
        payload["passageRole"] = role
        payload["passage_role"] = role

    passage_quality = metadata.get("passage_quality")
    if isinstance(passage_quality, dict):
        normalized_quality = dict(passage_quality)
        payload["passageQuality"] = normalized_quality
        payload["passage_quality"] = normalized_quality

    shared_block_ids = metadata.get("shared_passage_block_ids")
    if isinstance(shared_block_ids, list):
        normalized_shared_block_ids = [str(block_id) for block_id in shared_block_ids if str(block_id)]
        if normalized_shared_block_ids:
            payload["sharedPassageBlockIds"] = normalized_shared_block_ids
            payload["shared_passage_block_ids"] = normalized_shared_block_ids

    child_numbers = metadata.get("passage_child_problem_numbers")
    if isinstance(child_numbers, list):
        normalized_child_numbers: list[int] = []
        for raw in child_numbers:
            try:
                normalized_child_numbers.append(int(raw))
            except (TypeError, ValueError):
                continue
        if normalized_child_numbers:
            payload["passageChildProblemNumbers"] = normalized_child_numbers
            payload["passage_child_problem_numbers"] = normalized_child_numbers

    source_page_ids = metadata.get("passage_source_page_ids")
    if isinstance(source_page_ids, list):
        normalized_source_page_ids = [str(page_id) for page_id in source_page_ids if str(page_id)]
        if normalized_source_page_ids:
            payload["passageSourcePageIds"] = normalized_source_page_ids
            payload["passage_source_page_ids"] = normalized_source_page_ids

    if "passage_continues_across_pages" in metadata:
        continues_across_pages = bool(metadata.get("passage_continues_across_pages"))
        payload["passageContinuesAcrossPages"] = continues_across_pages
        payload["passage_continues_across_pages"] = continues_across_pages

    fragment_count = _coerce_int(metadata.get("passage_fragment_count"))
    if fragment_count is not None and fragment_count > 0:
        payload["passageFragmentCount"] = fragment_count
        payload["passage_fragment_count"] = fragment_count

    if "passage_fragments_merged" in metadata:
        fragments_merged = bool(metadata.get("passage_fragments_merged"))
        payload["passageFragmentsMerged"] = fragments_merged
        payload["passage_fragments_merged"] = fragments_merged

    merged_fragment_ids = metadata.get("passage_merged_fragment_ids")
    if isinstance(merged_fragment_ids, list):
        normalized_fragment_ids = [str(problem_id) for problem_id in merged_fragment_ids if str(problem_id)]
        if normalized_fragment_ids:
            payload["passageMergedFragmentIds"] = normalized_fragment_ids
            payload["passage_merged_fragment_ids"] = normalized_fragment_ids

    merged_source_page_ids = metadata.get("passage_merged_source_page_ids")
    if isinstance(merged_source_page_ids, list):
        normalized_merged_page_ids = [str(page_id) for page_id in merged_source_page_ids if str(page_id)]
        if normalized_merged_page_ids:
            payload["passageMergedSourcePageIds"] = normalized_merged_page_ids
            payload["passage_merged_source_page_ids"] = normalized_merged_page_ids

    merged_fragment_count = _coerce_int(metadata.get("passage_merged_fragment_count"))
    if merged_fragment_count is not None and merged_fragment_count > 0:
        payload["passageMergedFragmentCount"] = merged_fragment_count
        payload["passage_merged_fragment_count"] = merged_fragment_count

    merged_into_problem_id = str(metadata.get("passage_merged_into_problem_id") or "").strip()
    if merged_into_problem_id:
        payload["passageMergedIntoProblemId"] = merged_into_problem_id
        payload["passage_merged_into_problem_id"] = merged_into_problem_id

    continuation_block_ids = metadata.get("passage_pre_question_continuation_block_ids")
    if isinstance(continuation_block_ids, list):
        normalized_continuation_block_ids = [
            str(block_id)
            for block_id in continuation_block_ids
            if str(block_id)
        ]
        if normalized_continuation_block_ids:
            payload["passagePreQuestionContinuationBlockIds"] = normalized_continuation_block_ids
            payload["passage_pre_question_continuation_block_ids"] = normalized_continuation_block_ids

    return payload


def _coerce_problem_number(value: Any) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _normalize_problem_source_segments(
    value: Any,
    *,
    fallback_page_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, raw_segment in enumerate(value, start=1):
        if not isinstance(raw_segment, Mapping):
            continue
        raw_bbox = raw_segment.get("bbox")
        if not isinstance(raw_bbox, Mapping):
            continue
        left = _coerce_float(raw_bbox.get("left"))
        top = _coerce_float(raw_bbox.get("top"))
        width = _coerce_float(raw_bbox.get("width"))
        height = _coerce_float(raw_bbox.get("height"))
        if None in {left, top, width, height} or width <= 0 or height <= 0:
            continue
        source_page_id = str(
            raw_segment.get("source_page_id")
            or raw_segment.get("sourcePageId")
            or fallback_page_id
        ).strip()
        column_index = _coerce_int(
            raw_segment.get("column_index") or raw_segment.get("columnIndex")
        ) or 1
        fragment_index = _coerce_int(
            raw_segment.get("fragment_index") or raw_segment.get("fragmentIndex")
        ) or index
        bbox = {
            "left": float(left),
            "top": float(top),
            "width": float(width),
            "height": float(height),
        }
        normalized.append(
            {
                "sourcePageId": source_page_id,
                "source_page_id": source_page_id,
                "columnIndex": column_index,
                "column_index": column_index,
                "fragmentIndex": fragment_index,
                "fragment_index": fragment_index,
                "bbox": bbox,
            }
        )
    return normalized


def _ordered_unique_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _ordered_unique_ints(values: Iterable[Any]) -> list[int]:
    seen: set[int] = set()
    unique: list[int] = []
    for value in values:
        number = _coerce_problem_number(value)
        if number is None or number in seen:
            continue
        seen.add(number)
        unique.append(number)
    return unique


def _is_missing_session_field(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, list) and not value:
        return True
    if isinstance(value, dict) and not value:
        return True
    return False


def _session_problem_field(problem: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in problem and not _is_missing_session_field(problem[key]):
            return problem[key]
    metadata = problem.get("metadata")
    if isinstance(metadata, dict):
        for key in keys:
            if key in metadata and not _is_missing_session_field(metadata[key]):
                return metadata[key]
    return None


def _session_problem_passage_group_id(problem: dict[str, Any]) -> str:
    return str(
        _session_problem_field(problem, "passageGroupId", "passage_group_id") or ""
    ).strip()


def _session_problem_passage_range(problem: dict[str, Any]) -> tuple[int, int] | None:
    value = _session_problem_field(problem, "passageRange", "passage_range")
    if not isinstance(value, dict):
        return None
    start = _coerce_problem_number(value.get("start"))
    end = _coerce_problem_number(value.get("end"))
    if start is None or end is None or end < start:
        return None
    return start, end


def _session_problem_passage_numbers(problem: dict[str, Any], *keys: str) -> list[int]:
    value = _session_problem_field(problem, *keys)
    if isinstance(value, list):
        return _ordered_unique_ints(value)
    return []


def _session_problem_passage_source_page_ids(problem: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    explicit = _session_problem_field(problem, "passageSourcePageIds", "passage_source_page_ids")
    if isinstance(explicit, list):
        values.extend(explicit)
    values.append(_session_problem_field(problem, "sourcePageId", "source_page_id"))
    return _ordered_unique_strings(values)


def _passage_number_label(start: int | None, end: int | None, child_numbers: list[int]) -> str:
    if start is not None and end is not None:
        return str(start) if start == end else f"{start}-{end}"
    if child_numbers:
        first = min(child_numbers)
        last = max(child_numbers)
        return str(first) if first == last else f"{first}-{last}"
    return ""


def _session_passage_groups(problems: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        group_id = _session_problem_passage_group_id(problem)
        if not group_id:
            continue
        group = groups.setdefault(
            group_id,
            {
                "groupId": group_id,
                "problemNumbers": [],
                "childProblemNumbers": [],
                "problemIds": [],
                "coreProblemIds": [],
                "fragmentProblemIds": [],
                "sourcePageIds": [],
                "roles": [],
                "continuesAcrossPages": False,
                "_rangeStart": None,
                "_rangeEnd": None,
            },
        )

        problem_number = _coerce_problem_number(
            _session_problem_field(problem, "problemNumber", "problem_number")
        )
        if problem_number is not None and problem_number not in group["problemNumbers"]:
            group["problemNumbers"].append(problem_number)

        child_numbers = _session_problem_passage_numbers(
            problem,
            "passageChildProblemNumbers",
            "passage_child_problem_numbers",
        )
        for child_number in child_numbers:
            if child_number not in group["childProblemNumbers"]:
                group["childProblemNumbers"].append(child_number)

        problem_id = str(_session_problem_field(problem, "id", "problem_id") or "").strip()
        if problem_id and problem_id not in group["problemIds"]:
            group["problemIds"].append(problem_id)

        role = str(
            _session_problem_field(problem, "passageRole", "passage_role") or ""
        ).strip()
        is_fragment = role == "passage_fragment" or _session_problem_is_supplemental(problem)
        if problem_id:
            target_ids = group["fragmentProblemIds"] if is_fragment else group["coreProblemIds"]
            if problem_id not in target_ids:
                target_ids.append(problem_id)

        for page_id in _session_problem_passage_source_page_ids(problem):
            if page_id not in group["sourcePageIds"]:
                group["sourcePageIds"].append(page_id)

        if role and role not in group["roles"]:
            group["roles"].append(role)

        passage_range = _session_problem_passage_range(problem)
        if passage_range is not None:
            start, end = passage_range
            current_start = group["_rangeStart"]
            current_end = group["_rangeEnd"]
            group["_rangeStart"] = start if current_start is None else min(current_start, start)
            group["_rangeEnd"] = end if current_end is None else max(current_end, end)

        group["continuesAcrossPages"] = bool(group["continuesAcrossPages"]) or bool(
            _session_problem_field(
                problem,
                "passageContinuesAcrossPages",
                "passage_continues_across_pages",
            )
        )

    items: list[dict[str, Any]] = []
    for group in groups.values():
        child_numbers = _ordered_unique_ints(group["childProblemNumbers"])
        start = group.pop("_rangeStart")
        end = group.pop("_rangeEnd")
        if (start is None or end is None) and child_numbers:
            start = min(child_numbers)
            end = max(child_numbers)
        source_page_ids = _ordered_unique_strings(group["sourcePageIds"])
        continues_across_pages = bool(group["continuesAcrossPages"]) or len(source_page_ids) > 1
        label = _passage_number_label(start, end, child_numbers)
        source_page_count = len(source_page_ids)
        core_problem_ids = _ordered_unique_strings(group["coreProblemIds"])
        fragment_problem_ids = _ordered_unique_strings(group["fragmentProblemIds"])
        detected_problem_count = len(group["problemIds"])
        problem_numbers = _ordered_unique_ints(group["problemNumbers"])
        problem_number_set = set(problem_numbers)
        missing_child_problem_numbers = [
            number for number in child_numbers if number not in problem_number_set
        ]
        problem_count = len(problem_numbers) if problem_numbers else len(core_problem_ids)
        fragment_problem_count = len(fragment_problem_ids)
        message_label = label or str(group["groupId"])
        problem_count_label = f"{problem_count}개 하위 문항"
        if fragment_problem_count:
            problem_count_label += f", 자료 {fragment_problem_count}개"
        group.update(
            {
                "numberStart": start,
                "numberEnd": end,
                "numberLabel": label,
                "problemNumbers": problem_numbers,
                "childProblemNumbers": child_numbers,
                "missingChildProblemNumbers": missing_child_problem_numbers,
                "missingChildProblemCount": len(missing_child_problem_numbers),
                "coreProblemIds": core_problem_ids,
                "fragmentProblemIds": fragment_problem_ids,
                "sourcePageIds": source_page_ids,
                "sourcePageCount": source_page_count,
                "problemCount": problem_count,
                "detectedProblemCount": detected_problem_count,
                "fragmentProblemCount": fragment_problem_count,
                "continuesAcrossPages": continues_across_pages,
                "message": (
                    f"지문 묶음 {message_label}이 {source_page_count}개 원본 페이지와 "
                    f"{problem_count_label}에 걸쳐 있습니다."
                ),
            }
        )
        items.append(group)
    return items


PASSAGE_REVIEW_RISK_FLAGS = {
    HWP_TEXT_FALLBACK_RISK_FLAG,
    PASSAGE_CROSS_PAGE_MERGE_CHECK_RISK_FLAG,
    "marker_document_continuation",
    "passage_missing_child_questions",
    "passage_group_source_reuse",
    "source_problem_bbox_overlap",
}


def _session_problem_risk_flags(problem: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    raw = problem.get("riskFlags") or problem.get("risk_flags")
    if isinstance(raw, list):
        values.extend(raw)
    metadata = problem.get("metadata")
    if isinstance(metadata, dict):
        for key in ("riskFlags", "risk_flags", "review_flags"):
            nested = metadata.get(key)
            if isinstance(nested, list):
                values.extend(nested)
    return _ordered_unique_strings(values)


def _session_passage_review_items(
    problems: Sequence[dict[str, Any]],
    passage_groups: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    problems_by_id = {
        str(problem.get("id") or problem.get("problem_id") or "").strip(): problem
        for problem in problems
        if isinstance(problem, dict)
    }
    items: list[dict[str, Any]] = []
    for group in passage_groups:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("groupId") or group.get("group_id") or "").strip()
        if not group_id:
            continue
        core_problem_ids = _ordered_unique_strings(group.get("coreProblemIds") or group.get("core_problem_ids") or [])
        fragment_problem_ids = _ordered_unique_strings(
            group.get("fragmentProblemIds") or group.get("fragment_problem_ids") or []
        )
        risk_flags: set[str] = set()
        for problem_id in [*core_problem_ids, *fragment_problem_ids]:
            problem = problems_by_id.get(problem_id)
            if not problem:
                continue
            risk_flags.update(_session_problem_risk_flags(problem))

        reason_codes: list[str] = []
        if bool(group.get("continuesAcrossPages") or group.get("continues_across_pages")):
            reason_codes.append("cross_page_passage_group")
        if fragment_problem_ids or int(group.get("fragmentProblemCount") or group.get("fragment_problem_count") or 0) > 0:
            reason_codes.append("passage_fragment")
        missing_child_problem_numbers = _ordered_unique_ints(
            group.get("missingChildProblemNumbers")
            or group.get("missing_child_problem_numbers")
            or []
        )
        if missing_child_problem_numbers:
            reason_codes.append("passage_missing_child_questions")
        for flag in sorted(risk_flags):
            if flag in PASSAGE_REVIEW_RISK_FLAGS and flag not in reason_codes:
                if flag == "marker_document_continuation" and "passage_fragment" in reason_codes:
                    continue
                reason_codes.append(flag)
        if not reason_codes:
            continue

        source_page_ids = _ordered_unique_strings(group.get("sourcePageIds") or group.get("source_page_ids") or [])
        problem_count = int(group.get("problemCount") or group.get("problem_count") or len(core_problem_ids))
        fragment_count = int(
            group.get("fragmentProblemCount")
            or group.get("fragment_problem_count")
            or len(fragment_problem_ids)
        )
        label = str(group.get("numberLabel") or group.get("number_label") or group_id).strip()
        page_count = len(source_page_ids)
        message = f"{label} 지문 묶음은 {page_count}개 페이지와 {problem_count}개 하위 문항"
        if fragment_count:
            message += f", 지문 본문 {fragment_count}개"
        if missing_child_problem_numbers:
            missing_label = ", ".join(f"{number}번" for number in missing_child_problem_numbers)
            message += f", 누락 문항 {missing_label}"
        message += "을 확인해야 합니다." if missing_child_problem_numbers else "를 확인해야 합니다."
        items.append(
            {
                "groupId": group_id,
                "numberLabel": label,
                "problemIds": core_problem_ids,
                "fragmentProblemIds": fragment_problem_ids,
                "sourcePageIds": source_page_ids,
                "problemCount": problem_count,
                "missingChildProblemNumbers": missing_child_problem_numbers,
                "missingChildProblemCount": len(missing_child_problem_numbers),
                "fragmentProblemCount": fragment_count,
                "continuesAcrossPages": bool(
                    group.get("continuesAcrossPages")
                    or group.get("continues_across_pages")
                    or len(source_page_ids) > 1
                ),
                "reviewReasonCodes": reason_codes,
                "riskFlags": sorted(flag for flag in risk_flags if flag in PASSAGE_REVIEW_RISK_FLAGS),
                "message": message,
            }
        )
    return items


def _session_duplicate_problem_number_groups(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    numbered: list[dict[str, Any]] = []
    for index, problem in enumerate(problems):
        number = _coerce_problem_number(problem.get("problemNumber") or problem.get("problem_number"))
        if number is None:
            continue
        numbered.append(
            {
                "index": index,
                "number": number,
                "problemId": str(problem.get("id") or problem.get("problem_id") or ""),
                "sourcePageId": str(problem.get("sourcePageId") or problem.get("source_page_id") or ""),
            }
        )
    if not numbered:
        return []

    counts: dict[int, int] = {}
    for item in numbered:
        counts[item["number"]] = counts.get(item["number"], 0) + 1
    duplicate_numbers = sorted(number for number, count in counts.items() if count > 1)
    if not duplicate_numbers:
        return []

    ranges: list[tuple[int, int]] = []
    start = previous = duplicate_numbers[0]
    for number in duplicate_numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append((start, previous))
        start = previous = number
    ranges.append((start, previous))

    groups: list[dict[str, Any]] = []
    for start, end in ranges:
        numbers = [number for number in range(start, end + 1) if counts.get(number, 0) > 1]
        if not numbers:
            continue
        group_items = [item for item in numbered if item["number"] in set(numbers)]
        occurrence_counts = [counts[number] for number in numbers]
        min_occurrences = min(occurrence_counts)
        max_occurrences = max(occurrence_counts)
        label = str(start) if start == end else f"{start}-{end}"
        if min_occurrences == max_occurrences:
            message = (
                f"문항 번호 {label}가 각 {min_occurrences}회 등장합니다. "
                "EDB 제작 순서는 문항 번호가 아니라 전체 페이지 순서를 따릅니다."
            )
        else:
            message = (
                f"문항 번호 {label} 범위에서 중복 번호가 {sum(counts[number] - 1 for number in numbers)}개 있습니다. "
                "EDB 제작 순서는 문항 번호가 아니라 전체 페이지 순서를 따릅니다."
            )
        looks_like_alternate_section = (
            _duplicate_number_group_looks_like_alternate_section(
                start=start,
                end=end,
                min_occurrences=min_occurrences,
                max_occurrences=max_occurrences,
            )
            or _duplicate_number_group_looks_like_common_plus_alternate_sections(
                numbers=numbers,
                counts=counts,
                min_occurrences=min_occurrences,
                max_occurrences=max_occurrences,
            )
        )
        classification = "alternate_section" if looks_like_alternate_section else "duplicate_number_reuse"
        groups.append(
            {
                "numberStart": start,
                "numberEnd": end,
                "numberLabel": label,
                "problemNumbers": numbers,
                "occurrencesPerNumber": max_occurrences,
                "duplicateRecordCount": sum(counts[number] - 1 for number in numbers),
                "totalRecordCount": sum(counts[number] for number in numbers),
                "sourcePageIds": _ordered_unique_strings(item["sourcePageId"] for item in group_items),
                "problemIds": _ordered_unique_strings(item["problemId"] for item in group_items),
                "classification": classification,
                "blocking": False,
                "pageOrderPreserved": True,
                "orderBasis": "edb_page_order",
                "message": message,
            }
        )
    return groups


def _duplicate_number_group_looks_like_alternate_section(
    *,
    start: int,
    end: int,
    min_occurrences: int,
    max_occurrences: int,
) -> bool:
    range_size = end - start + 1
    return (
        start >= 20
        and end <= 45
        and 6 <= range_size <= 12
        and min_occurrences == max_occurrences
        and max_occurrences in {2, 3}
    )


def _duplicate_number_group_looks_like_common_plus_alternate_sections(
    *,
    numbers: Sequence[int],
    counts: dict[int, int],
    min_occurrences: int,
    max_occurrences: int,
) -> bool:
    if min_occurrences < 2 or max_occurrences <= min_occurrences:
        return False
    ordered = sorted(number for number in numbers if counts.get(number, 0) > 1)
    if not ordered or ordered[0] != 1 or ordered[-1] < 25 or ordered[-1] > 45:
        return False
    if ordered != list(range(ordered[0], ordered[-1] + 1)):
        return False

    high_count_numbers = [number for number in ordered if counts.get(number, 0) == max_occurrences]
    low_count_numbers = [number for number in ordered if counts.get(number, 0) == min_occurrences]
    if not high_count_numbers or not low_count_numbers:
        return False
    high_start = high_count_numbers[0]
    high_end = high_count_numbers[-1]
    high_range = list(range(high_start, high_end + 1))
    if high_count_numbers != high_range:
        return False
    if high_end != ordered[-1]:
        return False
    if high_start < 20:
        return False

    high_range_size = high_end - high_start + 1
    low_range_size = high_start - ordered[0]
    if not (6 <= high_range_size <= 12 and low_range_size >= 15):
        return False
    if any(counts.get(number, 0) != min_occurrences for number in range(ordered[0], high_start)):
        return False

    elective_multiplier = max_occurrences / max(1, min_occurrences)
    return elective_multiplier in {2, 3}


DUPLICATE_PROBLEM_NUMBER_RISK_FLAG = "duplicate_problem_number"
PASSAGE_MISSING_CHILD_QUESTIONS_RISK_FLAG = "passage_missing_child_questions"
PASSAGE_GROUP_SOURCE_REUSE_RISK_FLAG = "passage_group_source_reuse"
SOURCE_PROBLEM_BBOX_OVERLAP_RISK_FLAG = "source_problem_bbox_overlap"


def _mark_duplicate_problem_number_review_flags(
    problems: list[dict[str, Any]],
    groups: Sequence[dict[str, Any]],
) -> None:
    duplicate_problem_ids: set[str] = set()
    for group in groups:
        if group.get("blocking") is False:
            continue
        for problem_id in group.get("problemIds") or group.get("problem_ids") or []:
            problem_id_text = str(problem_id or "").strip()
            if problem_id_text:
                duplicate_problem_ids.add(problem_id_text)
    if not duplicate_problem_ids:
        return

    for problem in problems:
        problem_id = str(problem.get("id") or problem.get("problem_id") or "").strip()
        if problem_id not in duplicate_problem_ids:
            continue
        flags = [str(flag) for flag in (problem.get("riskFlags") or problem.get("risk_flags") or []) if flag]
        flags.append(DUPLICATE_PROBLEM_NUMBER_RISK_FLAG)
        problem["riskFlags"] = list(dict.fromkeys(flags))
        if str(problem.get("reviewStatus") or problem.get("review_status") or "").strip() != "failed":
            problem["reviewStatus"] = "check_needed"


def _session_passage_group_source_reuse_groups(problems: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for issue in _classin_passage_group_source_reuse_issues(problems):
        problem_id = str(issue.get("problemId") or "").strip()
        next_problem_id = str(issue.get("nextProblemId") or "").strip()
        passage_group_id = str(issue.get("passageGroupId") or "").strip()
        if not problem_id or not next_problem_id or not passage_group_id:
            continue
        source_page_id = str(issue.get("sourcePageId") or "").strip()
        overlap_area_ratio = _coerce_float(issue.get("overlapAreaRatio")) or 0.0
        passage_range = issue.get("passageRange") if isinstance(issue.get("passageRange"), dict) else {}
        passage_child_numbers = _ordered_unique_ints(
            issue.get("passageChildProblemNumbers") or issue.get("passage_child_problem_numbers") or []
        )
        groups.append(
            {
                "passageGroupId": passage_group_id,
                "passage_group_id": passage_group_id,
                "passageRange": dict(passage_range),
                "passage_range": dict(passage_range),
                "passageChildProblemNumbers": passage_child_numbers,
                "passage_child_problem_numbers": passage_child_numbers,
                "sourcePageId": source_page_id,
                "source_page_id": source_page_id,
                "problemIds": [problem_id, next_problem_id],
                "problem_ids": [problem_id, next_problem_id],
                "problemTitles": [
                    str(issue.get("problemTitle") or problem_id),
                    str(issue.get("nextProblemTitle") or next_problem_id),
                ],
                "overlapAreaRatio": round(overlap_area_ratio, 6),
                "overlap_area_ratio": round(overlap_area_ratio, 6),
                "intersectionOverUnion": issue.get("intersectionOverUnion", 0.0),
                "intersection_over_union": issue.get("intersectionOverUnion", 0.0),
                "bbox": issue.get("bbox") or {},
                "nextBbox": issue.get("nextBbox") or {},
                "next_bbox": issue.get("nextBbox") or {},
                "message": str(issue.get("message") or ""),
            }
        )
    return groups


def _mark_passage_group_source_reuse_review_flags(
    problems: list[dict[str, Any]],
    groups: Sequence[dict[str, Any]],
) -> None:
    reuse_problem_ids: set[str] = set()
    for group in groups:
        for problem_id in group.get("problemIds") or group.get("problem_ids") or []:
            problem_id_text = str(problem_id or "").strip()
            if problem_id_text:
                reuse_problem_ids.add(problem_id_text)
    if not reuse_problem_ids:
        return

    for problem in problems:
        problem_id = str(problem.get("id") or problem.get("problem_id") or "").strip()
        if problem_id not in reuse_problem_ids:
            continue
        flags = [str(flag) for flag in (problem.get("riskFlags") or problem.get("risk_flags") or []) if flag]
        flags.append(PASSAGE_GROUP_SOURCE_REUSE_RISK_FLAG)
        problem["riskFlags"] = list(dict.fromkeys(flags))
        if str(problem.get("reviewStatus") or problem.get("review_status") or "").strip() != "failed":
            problem["reviewStatus"] = "check_needed"


def _mark_missing_passage_child_question_review_flags(
    problems: list[dict[str, Any]],
    passage_groups: Sequence[dict[str, Any]],
) -> None:
    problem_ids_to_flag: set[str] = set()
    for group in passage_groups:
        missing_numbers = _ordered_unique_ints(
            group.get("missingChildProblemNumbers") or group.get("missing_child_problem_numbers") or []
        )
        if not missing_numbers:
            continue
        for problem_id in group.get("coreProblemIds") or group.get("core_problem_ids") or []:
            problem_id_text = str(problem_id or "").strip()
            if problem_id_text:
                problem_ids_to_flag.add(problem_id_text)
    if not problem_ids_to_flag:
        return

    for problem in problems:
        problem_id = str(problem.get("id") or problem.get("problem_id") or "").strip()
        if problem_id not in problem_ids_to_flag:
            continue
        flags = [str(flag) for flag in (problem.get("riskFlags") or problem.get("risk_flags") or []) if flag]
        flags.append(PASSAGE_MISSING_CHILD_QUESTIONS_RISK_FLAG)
        problem["riskFlags"] = list(dict.fromkeys(flags))
        if str(problem.get("reviewStatus") or problem.get("review_status") or "").strip() != "failed":
            problem["reviewStatus"] = "check_needed"


def _session_source_problem_overlap_groups(problems: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for issue in _classin_source_bbox_overlap_issues(problems):
        problem_id = str(issue.get("problemId") or "").strip()
        next_problem_id = str(issue.get("nextProblemId") or "").strip()
        if not problem_id or not next_problem_id:
            continue
        source_page_id = str(issue.get("sourcePageId") or "").strip()
        overlap_area_ratio = _coerce_float(issue.get("overlapAreaRatio")) or 0.0
        groups.append(
            {
                "sourcePageId": source_page_id,
                "source_page_id": source_page_id,
                "problemIds": [problem_id, next_problem_id],
                "problem_ids": [problem_id, next_problem_id],
                "problemTitles": [
                    str(issue.get("problemTitle") or problem_id),
                    str(issue.get("nextProblemTitle") or next_problem_id),
                ],
                "overlapAreaRatio": round(overlap_area_ratio, 6),
                "overlap_area_ratio": round(overlap_area_ratio, 6),
                "intersectionOverUnion": issue.get("intersectionOverUnion", 0.0),
                "intersection_over_union": issue.get("intersectionOverUnion", 0.0),
                "bbox": issue.get("bbox") or {},
                "nextBbox": issue.get("nextBbox") or {},
                "next_bbox": issue.get("nextBbox") or {},
                "message": str(issue.get("message") or ""),
            }
        )
    return groups


def _mark_source_problem_overlap_review_flags(
    problems: list[dict[str, Any]],
    groups: Sequence[dict[str, Any]],
) -> None:
    overlap_problem_ids: set[str] = set()
    for group in groups:
        for problem_id in group.get("problemIds") or group.get("problem_ids") or []:
            problem_id_text = str(problem_id or "").strip()
            if problem_id_text:
                overlap_problem_ids.add(problem_id_text)
    if not overlap_problem_ids:
        return

    for problem in problems:
        problem_id = str(problem.get("id") or problem.get("problem_id") or "").strip()
        if problem_id not in overlap_problem_ids:
            continue
        flags = [str(flag) for flag in (problem.get("riskFlags") or problem.get("risk_flags") or []) if flag]
        flags.append(SOURCE_PROBLEM_BBOX_OVERLAP_RISK_FLAG)
        problem["riskFlags"] = list(dict.fromkeys(flags))
        if str(problem.get("reviewStatus") or problem.get("review_status") or "").strip() != "failed":
            problem["reviewStatus"] = "check_needed"


def _duplicate_problem_number_note(groups: Sequence[dict[str, Any]]) -> str:
    parts: list[str] = []
    for group in groups:
        label = str(group.get("numberLabel") or "").strip()
        occurrences = int(group.get("occurrencesPerNumber") or 0)
        if label and occurrences > 1:
            parts.append(f"{label} x{occurrences}")
    return f"Duplicate problem numbers preserved in page order: {', '.join(parts)}" if parts else ""


def _duplicate_problem_number_groups_as_nonblocking(groups: Sequence[object]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        item = dict(group)
        item["blocking"] = False
        item["pageOrderPreserved"] = True
        item["orderBasis"] = str(item.get("orderBasis") or item.get("order_basis") or "edb_page_order")
        if not str(item.get("classification") or "").strip():
            item["classification"] = "duplicate_number_reuse"
        normalized.append(item)
    return normalized


def _session_asset_path(value: Any) -> Path | None:
    if not value:
        return None
    raw = str(value)
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        return Path(url2pathname(unquote(parsed.path))).resolve()
    if parsed.scheme:
        return None
    return Path(raw).resolve()


def _dark_pixel_ratio(image: Image.Image, *, threshold: int = CLASSIN_PREFLIGHT_INK_THRESHOLD) -> float:
    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = max(1, sum(histogram))
    dark_pixels = sum(histogram[: max(0, min(256, threshold))])
    return float(dark_pixels) / float(total)


def _problem_payload_processing_step(problem: dict[str, Any]) -> str:
    return _normalize_processing_step(
        problem.get("processingStep")
        or problem.get("processing_step")
        or problem.get("step")
    )


def _problem_payload_image_path(problem: dict[str, Any]) -> Path | None:
    return (
        _session_asset_path(problem.get("imagePath") or problem.get("image_path"))
        or _session_asset_path(problem.get("boardRenderPath") or problem.get("board_render_path"))
    )


def _problem_payload_uses_text_edge_guard(problem: dict[str, Any]) -> bool:
    input_intent = _normalize_input_intent(
        str(problem.get("inputIntent") or problem.get("input_intent") or "")
    )
    force_full_page_bounds = bool(
        problem.get("forceFullPageBounds") or problem.get("force_full_page_bounds")
    )
    if input_intent == "page-as-is" and force_full_page_bounds:
        # A full-page export has no synthetic horizontal crop seam. Scanning
        # every page for seam-adjacent glyphs cannot detect a real clipping
        # risk here and becomes noticeable on long exam documents.
        return False
    subject = str(problem.get("subject") or "").strip().lower()
    return subject in {
        Subject.KOREAN.value,
        Subject.ENGLISH.value,
        Subject.SOCIAL.value,
        Subject.SCIENCE.value,
        "국어",
        "영어",
        "사회",
        "과학",
    }


def _problem_image_horizontal_inner_edge_risk_stats(
    image: Image.Image,
    *,
    expected_padding_px: int,
) -> dict[str, Any]:
    """Detect glyph-sized ink on the seam between source pixels and padding."""
    width, height = image.size
    padding = max(0, int(expected_padding_px))
    empty = {
        "hasRisk": False,
        "has_risk": False,
        "riskSides": [],
        "risk_sides": [],
        "leftActiveRows": 0,
        "left_active_rows": 0,
        "rightActiveRows": 0,
        "right_active_rows": 0,
        "expectedPaddingPx": padding,
        "expected_padding_px": padding,
    }
    if padding <= 0 or width <= padding * 2 + 8 or height <= 16:
        return empty

    rgba = image.convert("RGBA")
    if np is not None:
        arr = np.asarray(rgba, dtype=np.uint8)
        alpha = arr[..., 3]
        rgb = arr[..., :3].astype(np.float32)
        luminance = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        mean_luminance = float(luminance.mean())
        has_transparency = bool(int(alpha.min()) < 245)
        foreground = (
            alpha > 48
            if has_transparency
            else (
                luminance >= 180.0
                if mean_luminance <= DARK_BOARD_BRIGHTNESS_THRESHOLD
                else luminance <= float(PROBLEM_EDGE_INK_DARK_THRESHOLD)
            )
        )
    else:
        grayscale = rgba.convert("L")
        mean_luminance = ImageStat.Stat(grayscale).mean[0]
        has_transparency = rgba.getchannel("A").getextrema()[0] < 245
        pixels = rgba.load()
        foreground = [
            [
                (
                    pixels[x, y][3] > 48
                    if has_transparency
                    else (
                        int(grayscale.getpixel((x, y))) >= 180
                        if mean_luminance <= DARK_BOARD_BRIGHTNESS_THRESHOLD
                        else int(grayscale.getpixel((x, y))) <= PROBLEM_EDGE_INK_DARK_THRESHOLD
                    )
                )
                for x in range(width)
            ]
            for y in range(height)
        ]

    maximum_glyph_run = max(48, int(round(height * 0.20)))

    def active_row_count(center_x: int, *, side: str) -> int:
        left = max(0, center_x - 2)
        right = min(width, center_x + 3)
        if side == "left":
            inward_left = min(width, center_x + 3)
            inward_right = min(width, center_x + 24)
        else:
            inward_left = max(0, center_x - 24)
            inward_right = max(0, center_x - 2)
        if inward_right <= inward_left:
            return 0
        if np is not None:
            active_rows = [
                int(value)
                for value in np.flatnonzero(
                    foreground[:, left:right].any(axis=1)
                    & foreground[:, inward_left:inward_right].any(axis=1)
                )
            ]
        else:
            active_rows = [
                y
                for y in range(height)
                if any(bool(foreground[y][x]) for x in range(left, right))
                and any(
                    bool(foreground[y][x])
                    for x in range(inward_left, inward_right)
                )
            ]
        if not active_rows:
            return 0
        runs: list[tuple[int, int]] = []
        run_start = previous = active_rows[0]
        for y in active_rows[1:]:
            if y > previous + 1:
                runs.append((run_start, previous + 1))
                run_start = y
            previous = y
        runs.append((run_start, previous + 1))
        return sum(
            end - start
            for start, end in runs
            if end - start <= maximum_glyph_run
        )

    left_rows = active_row_count(padding, side="left")
    right_rows = active_row_count(width - padding - 1, side="right")
    risk_sides = []
    if left_rows >= TEXT_PRIORITY_SOURCE_EDGE_MIN_ACTIVE_ROWS:
        risk_sides.append("left")
    if right_rows >= TEXT_PRIORITY_SOURCE_EDGE_MIN_ACTIVE_ROWS:
        risk_sides.append("right")
    return {
        "hasRisk": bool(risk_sides),
        "has_risk": bool(risk_sides),
        "riskSides": risk_sides,
        "risk_sides": risk_sides,
        "leftActiveRows": int(left_rows),
        "left_active_rows": int(left_rows),
        "rightActiveRows": int(right_rows),
        "right_active_rows": int(right_rows),
        "expectedPaddingPx": padding,
        "expected_padding_px": padding,
    }


def _problem_image_page_chrome_artifact_stats(image: Image.Image) -> dict[str, Any]:
    width, height = image.size
    if width <= 24 or height <= 24:
        return {
            "hasArtifact": False,
            "has_artifact": False,
            "artifactTypes": [],
            "artifact_types": [],
        }

    rgba = image.convert("RGBA")
    if np is not None:
        arr = np.asarray(rgba, dtype=np.uint8)
        alpha = arr[..., 3]
        rgb = arr[..., :3].astype(np.int16)
        red = rgb[..., 0]
        green = rgb[..., 1]
        blue = rgb[..., 2]
        luminance = (0.299 * red + 0.587 * green + 0.114 * blue).astype(np.float32)
        saturation = rgb.max(axis=2) - rgb.min(axis=2)
        has_transparency = int(alpha.min()) < 245
        if has_transparency:
            foreground = alpha >= 48
        elif float(luminance.mean()) <= DARK_BOARD_BRIGHTNESS_THRESHOLD:
            foreground = (luminance >= 180.0) | (saturation >= 42)
        else:
            foreground = (luminance <= 110.0) | (saturation >= 72)

        scan_width = min(width, max(8, min(80, int(round(width * 0.08)))))
        left_counts = np.count_nonzero(foreground[:, :scan_width], axis=0)
        right_counts = np.count_nonzero(foreground[:, width - scan_width:], axis=0)
        min_edge_coverage = max(16, int(round(height * 0.55)))
        left_guide_columns = [
            int(value)
            for value in np.flatnonzero(left_counts >= min_edge_coverage)
        ]
        right_guide_columns = [
            width - scan_width + int(value)
            for value in np.flatnonzero(right_counts >= min_edge_coverage)
        ]
        paired_content_frame = bool(
            left_guide_columns
            and right_guide_columns
            and min(left_guide_columns) >= 12
            and width - 1 - max(right_guide_columns) >= 12
        )
        edge_guide_column_count = (
            0
            if paired_content_frame
            else len(left_guide_columns) + len(right_guide_columns)
        )

        bottom_scan_top = max(0, height - max(36, min(180, int(round(height * 0.18)))))
        bottom_foreground = foreground[bottom_scan_top:, :]
        row_counts = np.count_nonzero(bottom_foreground, axis=1)
        bottom_line_rows = np.where(row_counts >= max(18, int(round(width * 0.72))))[0]
        bottom_line_count = 0
        if bottom_line_rows.size:
            absolute_rows = bottom_scan_top + bottom_line_rows
            near_bottom = absolute_rows >= height - max(18, int(round(height * 0.06)))
            bottom_line_count = int(np.count_nonzero(near_bottom))

        blue_mask = (
            (alpha > 24)
            & (blue >= red + BOTTOM_WATERMARK_BLUE_DELTA)
            & (blue >= green + 8)
            & (saturation >= 32)
        )
        bottom_blue_mask = blue_mask[bottom_scan_top:, :]
        bottom_blue_count = int(np.count_nonzero(bottom_blue_mask))

        corner_badge_count = _count_lower_corner_page_badges_from_mask(foreground)
    else:
        pixels = rgba.load()
        alpha_values: list[int] = []
        luminance_values: list[float] = []
        for y in range(height):
            for x in range(width):
                red, green, blue, alpha = pixels[x, y]
                alpha_values.append(alpha)
                luminance_values.append(0.299 * red + 0.587 * green + 0.114 * blue)
        has_transparency = min(alpha_values) < 245
        mean_luminance = sum(luminance_values) / max(1, len(luminance_values))

        def is_foreground(x: int, y: int) -> bool:
            red, green, blue, alpha = pixels[x, y]
            luminance = 0.299 * red + 0.587 * green + 0.114 * blue
            saturation = max(red, green, blue) - min(red, green, blue)
            if has_transparency:
                return alpha >= 48
            if mean_luminance <= DARK_BOARD_BRIGHTNESS_THRESHOLD:
                return luminance >= 180.0 or saturation >= 42
            return luminance <= 110.0 or saturation >= 72

        scan_width = min(width, max(8, min(80, int(round(width * 0.08)))))
        min_edge_coverage = max(16, int(round(height * 0.55)))
        left_guide_columns = [
            x
            for x in range(scan_width)
            if sum(1 for y in range(height) if is_foreground(x, y)) >= min_edge_coverage
        ]
        right_guide_columns = [
            x
            for x in range(width - scan_width, width)
            if sum(1 for y in range(height) if is_foreground(x, y)) >= min_edge_coverage
        ]
        paired_content_frame = bool(
            left_guide_columns
            and right_guide_columns
            and min(left_guide_columns) >= 12
            and width - 1 - max(right_guide_columns) >= 12
        )
        edge_guide_column_count = (
            0
            if paired_content_frame
            else len(left_guide_columns) + len(right_guide_columns)
        )

        bottom_scan_top = max(0, height - max(36, min(180, int(round(height * 0.18)))))
        bottom_line_count = 0
        for y in range(bottom_scan_top, height):
            if y < height - max(18, int(round(height * 0.06))):
                continue
            if sum(1 for x in range(width) if is_foreground(x, y)) >= max(18, int(round(width * 0.72))):
                bottom_line_count += 1

        bottom_blue_count = 0
        for y in range(bottom_scan_top, height):
            for x in range(width):
                red, green, blue, alpha = pixels[x, y]
                saturation = max(red, green, blue) - min(red, green, blue)
                if (
                    alpha > 24
                    and blue >= red + BOTTOM_WATERMARK_BLUE_DELTA
                    and blue >= green + 8
                    and saturation >= 32
                ):
                    bottom_blue_count += 1

        foreground_mask = [[is_foreground(x, y) for x in range(width)] for y in range(height)]
        corner_badge_count = _count_lower_corner_page_badges_from_mask(foreground_mask)

    bottom_blue_threshold = max(18, int(round(width * height * 0.0003)))
    artifact_types: list[str] = []
    if edge_guide_column_count > 0:
        artifact_types.append("edge_vertical_guide")
    if bottom_line_count > 0:
        artifact_types.append("bottom_page_line")
    if bottom_blue_count >= bottom_blue_threshold:
        artifact_types.append("bottom_blue_footer")
    if corner_badge_count > 0:
        artifact_types.append("corner_page_badge")
    artifact_types = list(dict.fromkeys(artifact_types))

    return {
        "hasArtifact": bool(artifact_types),
        "has_artifact": bool(artifact_types),
        "artifactTypes": artifact_types,
        "artifact_types": artifact_types,
        "edgeGuideColumnCount": int(edge_guide_column_count),
        "edge_guide_column_count": int(edge_guide_column_count),
        "pairedContentFrame": bool(paired_content_frame),
        "paired_content_frame": bool(paired_content_frame),
        "bottomLineRowCount": int(bottom_line_count),
        "bottom_line_row_count": int(bottom_line_count),
        "bottomBluePixelCount": int(bottom_blue_count),
        "bottom_blue_pixel_count": int(bottom_blue_count),
        "cornerPageBadgeCount": int(corner_badge_count),
        "corner_page_badge_count": int(corner_badge_count),
    }


def _count_lower_corner_page_badges_from_mask(mask: Any) -> int:
    height = len(mask)
    if height <= 24:
        return 0
    width = len(mask[0]) if height else 0
    if width <= 24:
        return 0

    roi_width = min(width, max(48, min(180, int(round(width * 0.18)))))
    roi_height = min(height, max(48, min(160, int(round(height * 0.16)))))
    seed_px = max(4, min(12, roi_width, roi_height))
    badge_count = 0

    def mask_get(y: int, x: int) -> bool:
        if np is not None and hasattr(mask, "shape"):
            return bool(mask[y, x])
        return bool(mask[y][x])

    for side in ("left", "right"):
        left = 0 if side == "left" else width - roi_width
        top = height - roi_height
        visited = [[False] * roi_width for _ in range(roi_height)]
        seeds: list[tuple[int, int]] = []
        x_edge = range(0, seed_px) if side == "left" else range(roi_width - seed_px, roi_width)
        for y in range(roi_height):
            for x in x_edge:
                if mask_get(top + y, left + x):
                    seeds.append((x, y))
        for y in range(roi_height - seed_px, roi_height):
            for x in range(roi_width):
                if mask_get(top + y, left + x):
                    seeds.append((x, y))

        for seed_x, seed_y in seeds:
            if visited[seed_y][seed_x] or not mask_get(top + seed_y, left + seed_x):
                continue
            stack = [(seed_x, seed_y)]
            visited[seed_y][seed_x] = True
            min_x = max_x = seed_x
            min_y = max_y = seed_y
            count = 0
            while stack:
                cx, cy = stack.pop()
                count += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if nx < 0 or nx >= roi_width or ny < 0 or ny >= roi_height:
                        continue
                    if visited[ny][nx] or not mask_get(top + ny, left + nx):
                        continue
                    visited[ny][nx] = True
                    stack.append((nx, ny))

            component_width = max_x - min_x + 1
            component_height = max_y - min_y + 1
            touches_side = min_x <= seed_px if side == "left" else max_x >= roi_width - seed_px - 1
            touches_bottom = max_y >= roi_height - seed_px - 1
            small_enough = (
                component_width <= max(42, int(round(width * 0.18)))
                and component_height <= max(36, int(round(height * 0.14)))
                and count <= max(1200, int(round(width * height * 0.02)))
            )
            if touches_side and touches_bottom and small_enough:
                badge_count += 1
                break

    return badge_count


def _classin_page_chrome_artifact_issues(problems: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    s2_total = 0
    s2_artifacts: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []

    for problem in problems:
        if not isinstance(problem, dict):
            continue
        step = _problem_payload_processing_step(problem)
        if step not in {PROCESSING_STEP_CHALK, PROCESSING_STEP_RECONSTRUCT}:
            continue
        image_path = _problem_payload_image_path(problem)
        if image_path is None or not image_path.is_file():
            continue
        is_passage_fragment = (
            str(
                problem.get("passageRole")
                or problem.get("passage_role")
                or ""
            ).strip()
            == "passage_fragment"
        )
        if step == PROCESSING_STEP_CHALK and is_passage_fragment:
            # Shared passages often contain an intentional rectangular source
            # border. The generic page-chrome detector sees its full-height
            # sides and bottom rule as page guides, even though those lines are
            # semantic content that must remain in the stage-2 crop.
            s2_total += 1
            continue
        try:
            with Image.open(image_path) as image:
                stats = _problem_image_page_chrome_artifact_stats(image)
        except OSError:
            continue
        if step == PROCESSING_STEP_CHALK:
            s2_total += 1
            if stats.get("hasArtifact"):
                s2_artifacts.append({"problem": problem, "path": image_path, "stats": stats})
            continue
        if step == PROCESSING_STEP_RECONSTRUCT and stats.get("hasArtifact"):
            issues.append(
                _classin_preflight_issue(
                    "step3_page_chrome_artifact",
                    severity="error",
                    message=(
                        "3단계 문항 이미지에 분할선, 하단 페이지 선, 페이지 번호 또는 footer 후보가 남아 있습니다. "
                        "3단계 산출물은 EDB 등록 전 반드시 다시 정리해야 합니다."
                    ),
                    problem=problem,
                    path=image_path,
                    details={
                        "processingStep": step,
                        "processing_step": step,
                        "artifactTypes": stats.get("artifactTypes", []),
                        "artifact_types": stats.get("artifactTypes", []),
                        "pageChromeArtifactStats": stats,
                        "page_chrome_artifact_stats": stats,
                    },
                )
            )

    if s2_total > 0 and s2_artifacts:
        artifact_ratio = len(s2_artifacts) / float(s2_total)
        if artifact_ratio > CLASSIN_PREFLIGHT_STEP2_PAGE_CHROME_MAX_RATIO:
            sample = s2_artifacts[:10]
            problem_ids = [
                str(item["problem"].get("id") or item["problem"].get("problem_id") or "")
                for item in sample
            ]
            artifact_types = sorted(
                {
                    str(artifact_type)
                    for item in s2_artifacts
                    for artifact_type in item["stats"].get("artifactTypes", [])
                    if str(artifact_type)
                }
            )
            issues.append(
                _classin_preflight_issue(
                    "step2_page_chrome_artifact_rate",
                    severity="warning",
                    message=(
                        "2단계 문항 이미지의 페이지 장식 후보가 허용률(10개 중 1개)을 초과했습니다. "
                        "대표 문항을 확인하고 crop/재구성 단계를 다시 실행해 주세요."
                    ),
                    problem=sample[0]["problem"] if sample else None,
                    path=sample[0]["path"] if sample else None,
                    details={
                        "processingStep": PROCESSING_STEP_CHALK,
                        "processing_step": PROCESSING_STEP_CHALK,
                        "artifactProblemCount": len(s2_artifacts),
                        "artifact_problem_count": len(s2_artifacts),
                        "checkedProblemCount": s2_total,
                        "checked_problem_count": s2_total,
                        "artifactRatio": round(artifact_ratio, 6),
                        "artifact_ratio": round(artifact_ratio, 6),
                        "maxArtifactRatio": CLASSIN_PREFLIGHT_STEP2_PAGE_CHROME_MAX_RATIO,
                        "max_artifact_ratio": CLASSIN_PREFLIGHT_STEP2_PAGE_CHROME_MAX_RATIO,
                        "problemIds": problem_ids,
                        "problem_ids": problem_ids,
                        "artifactTypes": artifact_types,
                        "artifact_types": artifact_types,
                    },
                )
            )

    return issues


def _classin_preflight_issue(
    issue_type: str,
    *,
    severity: str,
    message: str,
    problem: dict[str, Any] | None = None,
    path: Path | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "type": issue_type,
        "severity": severity,
        "message": message,
    }
    if problem is not None:
        issue["problemId"] = str(problem.get("id") or "")
        issue["problemTitle"] = str(problem.get("title") or problem.get("problemNumber") or "")
    if path is not None:
        issue["path"] = str(path)
    if details:
        issue.update(details)
    return issue


def _classin_preflight_issue_label(issue_type: object) -> str:
    normalized = str(issue_type or "").strip()
    return CLASSIN_PREFLIGHT_ISSUE_LABELS.get(normalized, normalized)


def _passage_review_reason_label(reason: object) -> str:
    normalized = str(reason or "").strip()
    return PASSAGE_REVIEW_REASON_LABELS.get(normalized, normalized)


def _format_passage_review_reason(reason: object) -> str:
    normalized = str(reason or "").strip()
    label = _passage_review_reason_label(normalized)
    return f"{label} (`{normalized}`)" if normalized and label != normalized else normalized


def _problem_float(problem: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _coerce_float(problem.get(key))
        if value is not None:
            return value
    return None


def _problem_source_page_id(problem: dict[str, Any]) -> str:
    return str(
        problem.get("sourcePageId")
        or problem.get("source_page_id")
        or problem.get("pageId")
        or problem.get("page_id")
        or ""
    ).strip()


def _problem_bbox(problem: dict[str, Any]) -> tuple[float, float, float, float] | None:
    raw_bbox = problem.get("bbox") or problem.get("sourceBbox") or problem.get("source_bbox")
    if not isinstance(raw_bbox, dict):
        return None

    left = _coerce_float(raw_bbox.get("left"))
    if left is None:
        left = _coerce_float(raw_bbox.get("x"))
    top = _coerce_float(raw_bbox.get("top"))
    if top is None:
        top = _coerce_float(raw_bbox.get("y"))
    width = _coerce_float(raw_bbox.get("width"))
    if width is None:
        width = _coerce_float(raw_bbox.get("w"))
    height = _coerce_float(raw_bbox.get("height"))
    if height is None:
        height = _coerce_float(raw_bbox.get("h"))
    right = _coerce_float(raw_bbox.get("right"))
    bottom = _coerce_float(raw_bbox.get("bottom"))

    if left is None or top is None:
        return None
    if width is not None:
        right = left + width
    if height is not None:
        bottom = top + height
    if right is None or bottom is None:
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _bbox_payload(box: tuple[float, float, float, float]) -> dict[str, float]:
    left, top, right, bottom = box
    return {
        "left": round(left, 6),
        "top": round(top, 6),
        "width": round(right - left, 6),
        "height": round(bottom - top, 6),
    }


def _classin_source_bbox_overlap_issues(problems: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates_by_page: dict[str, list[tuple[tuple[float, float, float, float], dict[str, Any]]]] = {}
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        risk_flags = {str(flag) for flag in (problem.get("riskFlags") or []) if str(flag)}
        if HWP_TEXT_FALLBACK_RISK_FLAG in risk_flags:
            continue
        if _session_problem_is_supplemental(problem):
            # Shared passages can be assembled from disjoint column rectangles
            # on one page (for example, lower-left then upper-right). Their
            # enclosing bbox legitimately overlaps nearby question bboxes even
            # though the source segments and exported crops do not. Dedicated
            # passage-group checks cover duplicated child-question content.
            continue
        source_page_id = _problem_source_page_id(problem)
        bbox = _problem_bbox(problem)
        if not source_page_id or bbox is None:
            continue
        candidates_by_page.setdefault(source_page_id, []).append((bbox, problem))

    issues: list[dict[str, Any]] = []
    threshold = CLASSIN_PREFLIGHT_SOURCE_BBOX_OVERLAP_RATIO
    for source_page_id, candidates in candidates_by_page.items():
        candidates.sort(
            key=lambda item: (
                item[0][1],
                item[0][0],
                str(item[1].get("id") or item[1].get("problem_id") or ""),
            )
        )
        for index, (bbox, problem) in enumerate(candidates):
            left, top, right, bottom = bbox
            area = (right - left) * (bottom - top)
            if area <= 0:
                continue
            for next_bbox, next_problem in candidates[index + 1:]:
                group_id = _session_problem_passage_group_id(problem)
                next_group_id = _session_problem_passage_group_id(next_problem)
                if group_id and group_id == next_group_id:
                    continue
                next_left, next_top, next_right, next_bottom = next_bbox
                next_area = (next_right - next_left) * (next_bottom - next_top)
                if next_area <= 0:
                    continue
                intersection_width = max(0.0, min(right, next_right) - max(left, next_left))
                intersection_height = max(0.0, min(bottom, next_bottom) - max(top, next_top))
                intersection_area = intersection_width * intersection_height
                if intersection_area <= 0:
                    continue
                overlap_area_ratio = intersection_area / min(area, next_area)
                if overlap_area_ratio < threshold:
                    continue
                union_area = area + next_area - intersection_area
                problem_ids = [
                    str(problem.get("id") or problem.get("problem_id") or ""),
                    str(next_problem.get("id") or next_problem.get("problem_id") or ""),
                ]
                issues.append(
                    _classin_preflight_issue(
                        "source_problem_bbox_overlap",
                        severity="warning",
                        message=(
                            "두 문항의 원본 인식 영역이 크게 겹칩니다. "
                            "긴 지문 병합/하위 문항 분리 결과가 EDB에 중복 등록되지 않는지 확인해 주세요."
                        ),
                        problem=problem,
                        details={
                            "nextProblemId": problem_ids[1],
                            "nextProblemTitle": str(
                                next_problem.get("title") or next_problem.get("problemNumber") or ""
                            ),
                            "problemIds": problem_ids,
                            "problem_ids": problem_ids,
                            "sourcePageId": source_page_id,
                            "overlapAreaRatio": round(overlap_area_ratio, 6),
                            "intersectionOverUnion": round(intersection_area / union_area, 6) if union_area > 0 else 0.0,
                            "intersectionAreaPx": round(intersection_area, 6),
                            "sourceBBoxOverlapThreshold": threshold,
                            "bbox": _bbox_payload(bbox),
                            "nextBbox": _bbox_payload(next_bbox),
                        },
                    )
                )
    return issues


def _classin_passage_group_source_reuse_issues(problems: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates_by_group: dict[
        tuple[str, str],
        list[tuple[tuple[float, float, float, float], dict[str, Any]]],
    ] = {}
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        risk_flags = {str(flag) for flag in (problem.get("riskFlags") or []) if str(flag)}
        if HWP_TEXT_FALLBACK_RISK_FLAG in risk_flags:
            continue
        group_id = _session_problem_passage_group_id(problem)
        source_page_id = _problem_source_page_id(problem)
        bbox = _problem_bbox(problem)
        role = str(_session_problem_field(problem, "passageRole", "passage_role") or "").strip()
        if not group_id or not source_page_id or bbox is None:
            continue
        if role == "passage_fragment" or _session_problem_is_supplemental(problem):
            continue
        candidates_by_group.setdefault((group_id, source_page_id), []).append((bbox, problem))

    issues: list[dict[str, Any]] = []
    threshold = CLASSIN_PREFLIGHT_PASSAGE_SOURCE_REUSE_RATIO
    for (group_id, source_page_id), candidates in candidates_by_group.items():
        if len(candidates) < 2:
            continue
        candidates.sort(
            key=lambda item: (
                item[0][1],
                item[0][0],
                str(item[1].get("id") or item[1].get("problem_id") or ""),
            )
        )
        for index, (bbox, problem) in enumerate(candidates):
            left, top, right, bottom = bbox
            area = (right - left) * (bottom - top)
            if area <= 0:
                continue
            for next_bbox, next_problem in candidates[index + 1:]:
                next_left, next_top, next_right, next_bottom = next_bbox
                next_area = (next_right - next_left) * (next_bottom - next_top)
                if next_area <= 0:
                    continue
                intersection_width = max(0.0, min(right, next_right) - max(left, next_left))
                intersection_height = max(0.0, min(bottom, next_bottom) - max(top, next_top))
                intersection_area = intersection_width * intersection_height
                if intersection_area <= 0:
                    continue
                overlap_area_ratio = intersection_area / min(area, next_area)
                if overlap_area_ratio < threshold:
                    continue
                union_area = area + next_area - intersection_area
                passage_range = _session_problem_passage_range(problem) or _session_problem_passage_range(next_problem)
                passage_range_payload = (
                    {"start": passage_range[0], "end": passage_range[1]} if passage_range is not None else {}
                )
                passage_child_numbers = _ordered_unique_ints(
                    [
                        *_session_problem_passage_numbers(
                            problem,
                            "passageChildProblemNumbers",
                            "passage_child_problem_numbers",
                        ),
                        *_session_problem_passage_numbers(
                            next_problem,
                            "passageChildProblemNumbers",
                            "passage_child_problem_numbers",
                        ),
                    ]
                )
                problem_ids = [
                    str(problem.get("id") or problem.get("problem_id") or ""),
                    str(next_problem.get("id") or next_problem.get("problem_id") or ""),
                ]
                issues.append(
                    _classin_preflight_issue(
                        "passage_group_source_reuse",
                        severity="warning",
                        message=(
                            "같은 지문 묶음의 하위 문항 영역이 크게 겹칩니다. "
                            "공통 지문/문항 crop이 EDB에 반복 등록되지 않도록 지문 병합 상태를 확인해 주세요."
                        ),
                        problem=problem,
                        details={
                            "nextProblemId": problem_ids[1],
                            "nextProblemTitle": str(
                                next_problem.get("title") or next_problem.get("problemNumber") or ""
                            ),
                            "problemIds": problem_ids,
                            "problem_ids": problem_ids,
                            "passageGroupId": group_id,
                            "passageRange": passage_range_payload,
                            "passage_range": passage_range_payload,
                            "passageChildProblemNumbers": passage_child_numbers,
                            "passage_child_problem_numbers": passage_child_numbers,
                            "sourcePageId": source_page_id,
                            "overlapAreaRatio": round(overlap_area_ratio, 6),
                            "intersectionOverUnion": round(intersection_area / union_area, 6) if union_area > 0 else 0.0,
                            "intersectionAreaPx": round(intersection_area, 6),
                            "passageGroupSourceReuseThreshold": threshold,
                            "bbox": _bbox_payload(bbox),
                            "nextBbox": _bbox_payload(next_bbox),
                        },
                    )
                )
    return issues


def _classin_board_placement_overlap_issues(problems: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    placements: list[tuple[float, float, dict[str, Any]]] = []
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        start_y_pages = _problem_float(problem, "startYPages", "start_y_pages")
        height_pages = _problem_float(
            problem,
            "actualHeightPages",
            "actual_height_pages",
            "actualContentHeightPages",
            "actual_content_height_pages",
        )
        if start_y_pages is None or height_pages is None:
            continue
        scale_ratio = _problem_float(problem, "placementScaleRatio", "placement_scale_ratio")
        if scale_ratio is None:
            scale_ratio = 1.0
        rendered_bottom_y_pages = start_y_pages + max(0.0, height_pages) * max(0.0, scale_ratio)
        placements.append((start_y_pages, rendered_bottom_y_pages, problem))

    placements.sort(key=lambda item: (item[0], str(item[2].get("id") or "")))

    issues: list[dict[str, Any]] = []
    tolerance = CLASSIN_PREFLIGHT_PLACEMENT_OVERLAP_TOLERANCE_PAGES
    for current, next_item in zip(placements, placements[1:]):
        current_start_y_pages, current_bottom_y_pages, problem = current
        next_start_y_pages, _next_bottom_y_pages, next_problem = next_item
        overlap_pages = current_bottom_y_pages - next_start_y_pages
        if overlap_pages <= tolerance:
            continue
        issues.append(
            _classin_preflight_issue(
                "board_placement_overlap",
                severity="warning",
                message=(
                    "문항 배치가 다음 문항 시작 위치를 침범할 수 있습니다. "
                    "긴 지문/확대 배율을 줄이거나 자동 재배치를 확인해 주세요."
                ),
                problem=problem,
                details={
                    "nextProblemId": str(next_problem.get("id") or ""),
                    "nextProblemTitle": str(
                        next_problem.get("title") or next_problem.get("problemNumber") or ""
                    ),
                    "startYPages": round(current_start_y_pages, 6),
                    "renderedBottomYPages": round(current_bottom_y_pages, 6),
                    "nextStartYPages": round(next_start_y_pages, 6),
                    "overlapPages": round(overlap_pages, 6),
                    "placementOverlapTolerancePages": tolerance,
                },
            )
        )
    return issues


def _round_layout_pages(value: float) -> float:
    return round(float(value), 6)


def _layout_diagnostic_from_placement(placement: Mapping[str, Any]) -> dict[str, Any]:
    actual_height_pages = max(0.0, float(placement.get("actual_content_height_pages") or 0.0))
    scale_ratio = max(0.0, float(placement.get("placement_scale_ratio") or 1.0))
    rendered_height_pages = actual_height_pages * scale_ratio
    start_y_pages = max(0.0, float(placement.get("start_y_pages") or 0.0))
    snapped_next_start_y_pages = max(start_y_pages, float(placement.get("snapped_next_start_y_pages") or start_y_pages))
    reserved_span_pages = max(0.0, snapped_next_start_y_pages - start_y_pages)
    placement_y_ratio = _clamp_placement_y_ratio(placement.get("placement_y_ratio"))
    if placement_y_ratio is None:
        placement_y_ratio = 0.0
    vertical_room_pages = max(0.0, reserved_span_pages - rendered_height_pages)
    rendered_top_y_pages = start_y_pages + vertical_room_pages * placement_y_ratio
    rendered_bottom_y_pages = rendered_top_y_pages + rendered_height_pages
    overlap_amount_pages = max(
        0.0,
        rendered_bottom_y_pages
        - snapped_next_start_y_pages
        - CLASSIN_PREFLIGHT_PLACEMENT_OVERLAP_TOLERANCE_PAGES,
    )
    long_image = rendered_height_pages > ONE_PROBLEM_SLOT_HEIGHT_PAGES + CLASSIN_PREFLIGHT_PLACEMENT_OVERLAP_TOLERANCE_PAGES
    auto_extended = reserved_span_pages > ONE_PROBLEM_SLOT_HEIGHT_PAGES + CLASSIN_PREFLIGHT_PLACEMENT_OVERLAP_TOLERANCE_PAGES
    scale_adjusted = abs(scale_ratio - 1.0) > 0.001
    payload = {
        "actualHeightPages": _round_layout_pages(actual_height_pages),
        "actual_height_pages": _round_layout_pages(actual_height_pages),
        "placementScaleRatio": _round_layout_pages(scale_ratio),
        "placement_scale_ratio": _round_layout_pages(scale_ratio),
        "renderedHeightPages": _round_layout_pages(rendered_height_pages),
        "rendered_height_pages": _round_layout_pages(rendered_height_pages),
        "startYPages": _round_layout_pages(start_y_pages),
        "start_y_pages": _round_layout_pages(start_y_pages),
        "renderedTopYPages": _round_layout_pages(rendered_top_y_pages),
        "rendered_top_y_pages": _round_layout_pages(rendered_top_y_pages),
        "renderedBottomYPages": _round_layout_pages(rendered_bottom_y_pages),
        "rendered_bottom_y_pages": _round_layout_pages(rendered_bottom_y_pages),
        "snappedNextStartYPages": _round_layout_pages(snapped_next_start_y_pages),
        "snapped_next_start_y_pages": _round_layout_pages(snapped_next_start_y_pages),
        "reservedSpanPages": _round_layout_pages(reserved_span_pages),
        "reserved_span_pages": _round_layout_pages(reserved_span_pages),
        "verticalRoomPages": _round_layout_pages(vertical_room_pages),
        "vertical_room_pages": _round_layout_pages(vertical_room_pages),
        "scaleExtraPages": _round_layout_pages(max(0.0, rendered_height_pages - actual_height_pages)),
        "scale_extra_pages": _round_layout_pages(max(0.0, rendered_height_pages - actual_height_pages)),
        "reservedExtraPages": _round_layout_pages(max(0.0, reserved_span_pages - ONE_PROBLEM_SLOT_HEIGHT_PAGES)),
        "reserved_extra_pages": _round_layout_pages(max(0.0, reserved_span_pages - ONE_PROBLEM_SLOT_HEIGHT_PAGES)),
        "overlapAmountPages": _round_layout_pages(overlap_amount_pages),
        "overlap_amount_pages": _round_layout_pages(overlap_amount_pages),
        "longImage": long_image,
        "long_image": long_image,
        "autoExtended": auto_extended,
        "auto_extended": auto_extended,
        "scaleAdjusted": scale_adjusted,
        "scale_adjusted": scale_adjusted,
        "overlapRisk": overlap_amount_pages > 0,
        "overlap_risk": overlap_amount_pages > 0,
    }
    return payload


def _layout_diagnostics_from_problems(problems: Sequence[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    for problem in problems:
        if not isinstance(problem, dict):
            continue
        diag = problem.get("layoutDiagnostics") or problem.get("layout_diagnostics") or {}
        if not isinstance(diag, dict):
            continue
        item = {
            "problemId": str(problem.get("id") or problem.get("problem_id") or ""),
            "problem_id": str(problem.get("id") or problem.get("problem_id") or ""),
            "title": str(problem.get("title") or ""),
            "actualHeightPages": float(diag.get("actualHeightPages") or diag.get("actual_height_pages") or 0.0),
            "renderedHeightPages": float(diag.get("renderedHeightPages") or diag.get("rendered_height_pages") or 0.0),
            "reservedSpanPages": float(diag.get("reservedSpanPages") or diag.get("reserved_span_pages") or 0.0),
            "reservedExtraPages": float(diag.get("reservedExtraPages") or diag.get("reserved_extra_pages") or 0.0),
            "placementScaleRatio": float(diag.get("placementScaleRatio") or diag.get("placement_scale_ratio") or 1.0),
            "overlapAmountPages": float(diag.get("overlapAmountPages") or diag.get("overlap_amount_pages") or 0.0),
            "longImage": bool(diag.get("longImage") or diag.get("long_image")),
            "autoExtended": bool(diag.get("autoExtended") or diag.get("auto_extended")),
            "scaleAdjusted": bool(diag.get("scaleAdjusted") or diag.get("scale_adjusted")),
            "overlapRisk": bool(diag.get("overlapRisk") or diag.get("overlap_risk")),
        }
        all_items.append(item)
        if item["longImage"] or item["autoExtended"] or item["scaleAdjusted"] or item["overlapRisk"]:
            items.append(item)
    long_image_count = sum(1 for item in all_items if item["longImage"])
    auto_extended_count = sum(1 for item in all_items if item["autoExtended"])
    scaled_item_count = sum(1 for item in all_items if item["scaleAdjusted"])
    overlap_risk_count = sum(1 for item in all_items if item["overlapRisk"])
    max_rendered_height_pages = max((item["renderedHeightPages"] for item in all_items), default=0.0)
    max_reserved_span_pages = max((item["reservedSpanPages"] for item in all_items), default=0.0)
    label_parts = []
    if auto_extended_count:
        label_parts.append(f"긴 이미지 자동 확장 {auto_extended_count}")
    if (auto_extended_count or long_image_count or overlap_risk_count) and max_rendered_height_pages > 0:
        label_parts.append(f"최대 {max_rendered_height_pages:.2f}p")
    if overlap_risk_count:
        label_parts.append(f"겹침 위험 {overlap_risk_count}")
    payload = {
        "itemCount": len(all_items),
        "item_count": len(all_items),
        "longImageCount": long_image_count,
        "long_image_count": long_image_count,
        "autoExtendedCount": auto_extended_count,
        "auto_extended_count": auto_extended_count,
        "scaledItemCount": scaled_item_count,
        "scaled_item_count": scaled_item_count,
        "overlapRiskCount": overlap_risk_count,
        "overlap_risk_count": overlap_risk_count,
        "maxRenderedHeightPages": _round_layout_pages(max_rendered_height_pages),
        "max_rendered_height_pages": _round_layout_pages(max_rendered_height_pages),
        "maxReservedSpanPages": _round_layout_pages(max_reserved_span_pages),
        "max_reserved_span_pages": _round_layout_pages(max_reserved_span_pages),
        "label": " · ".join(label_parts),
        "items": items,
    }
    return payload


def _classin_missing_passage_child_question_issues(problems: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    problems_by_id = {
        str(problem.get("id") or problem.get("problem_id") or "").strip(): problem
        for problem in problems
        if isinstance(problem, dict)
    }
    issues: list[dict[str, Any]] = []
    for group in _session_passage_groups(problems):
        missing_numbers = _ordered_unique_ints(
            group.get("missingChildProblemNumbers") or group.get("missing_child_problem_numbers") or []
        )
        if not missing_numbers:
            continue
        problem_ids = _ordered_unique_strings(group.get("coreProblemIds") or group.get("core_problem_ids") or [])
        issue_problem = problems_by_id.get(problem_ids[0]) if problem_ids else None
        missing_label = ", ".join(f"{number}번" for number in missing_numbers)
        issues.append(
            _classin_preflight_issue(
                "passage_missing_child_questions",
                severity="warning",
                message=(
                    f"지문 묶음에서 예상 하위 문항 {missing_label}이 감지되지 않았습니다. "
                    "EDB 등록 전 지문 병합/문항 분리 결과를 확인해 주세요."
                ),
                problem=issue_problem,
                details={
                    "passageGroupId": str(group.get("groupId") or ""),
                    "numberLabel": str(group.get("numberLabel") or ""),
                    "problemIds": problem_ids,
                    "problem_ids": problem_ids,
                    "sourcePageIds": _ordered_unique_strings(
                        group.get("sourcePageIds") or group.get("source_page_ids") or []
                    ),
                    "missingChildProblemNumbers": missing_numbers,
                    "missing_child_problem_numbers": missing_numbers,
                    "missingChildProblemCount": len(missing_numbers),
                    "missing_child_problem_count": len(missing_numbers),
                },
            )
        )
    return issues


def _classin_preflight_has_actionable_review_state(risk_flags: Sequence[str], review_status: str) -> bool:
    status = str(review_status or "").strip()
    if status == "failed":
        return True
    normalized_flags = {str(flag or "").strip() for flag in risk_flags if str(flag or "").strip()}
    review_flags = normalized_flags - {DUPLICATE_PROBLEM_NUMBER_RISK_FLAG}
    if review_flags and review_flags.issubset(CLASSIN_PREFLIGHT_NON_ACTIONABLE_REVIEW_RISK_FLAGS):
        return "marker_document_continuation" not in review_flags
    if review_flags:
        return True
    return status == "check_needed" and not risk_flags


def _classin_handoff_preflight(ui_session: dict[str, Any]) -> dict[str, Any]:
    raw_problems = ui_session.get("problems")
    problems = raw_problems if isinstance(raw_problems, list) else []
    issues: list[dict[str, Any]] = []
    min_width = CLASSIN_PREFLIGHT_MIN_IMAGE_WIDTH_PX
    min_height = CLASSIN_PREFLIGHT_MIN_IMAGE_HEIGHT_PX
    passage_only = _normalize_content_target(
        str(ui_session.get("contentTarget") or ui_session.get("content_target") or "all")
    ) == "shared-passages"

    for problem in problems:
        if not isinstance(problem, dict):
            continue
        if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
            break

        risk_flags = [str(flag) for flag in (problem.get("riskFlags") or []) if str(flag)]
        review_status = str(problem.get("reviewStatus") or "").strip()
        is_passage_fragment = (
            str(problem.get("passageRole") or problem.get("passage_role") or "").strip()
            == "passage_fragment"
        )
        if _classin_preflight_has_actionable_review_state(risk_flags, review_status):
            issues.append(
                _classin_preflight_issue(
                    "review_flags_remaining",
                    severity="warning",
                    message="검수 플래그가 남아 있어 ClassIn에서 열기 전 원본 박스를 확인해야 합니다.",
                    problem=problem,
                    details={
                        "riskFlags": risk_flags,
                        "reviewStatus": review_status,
                    },
                )
            )
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break

        image_path = _session_asset_path(problem.get("imagePath"))
        if image_path is None or not image_path.is_file():
            issues.append(
                _classin_preflight_issue(
                    "missing_problem_image",
                    severity="error",
                    message="문항 이미지 파일을 찾을 수 없습니다.",
                    problem=problem,
                    path=image_path,
                )
            )
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break
            continue

        horizontal_edge_stats: dict[str, Any] | None = None
        try:
            with Image.open(image_path) as image:
                width, height = image.size
                dark_pixel_ratio = _dark_pixel_ratio(image)
                if _problem_payload_uses_text_edge_guard(problem):
                    horizontal_edge_stats = _problem_image_horizontal_inner_edge_risk_stats(
                        image,
                        expected_padding_px=(
                            PASSAGE_CROP_HORIZONTAL_SAFE_PADDING_PX
                            if is_passage_fragment
                            else TEXT_PRIORITY_CROP_HORIZONTAL_SAFE_PADDING_PX
                        ),
                    )
        except OSError as exc:
            issues.append(
                _classin_preflight_issue(
                    "unreadable_problem_image",
                    severity="error",
                    message=f"문항 이미지 파일을 열 수 없습니다: {exc}",
                    problem=problem,
                    path=image_path,
                )
            )
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break
            continue

        if horizontal_edge_stats and horizontal_edge_stats.get("hasRisk"):
            issues.append(
                _classin_preflight_issue(
                    "horizontal_crop_edge_risk",
                    severity="warning",
                    message=(
                        "최종 여백 안쪽의 원본 경계에 글자 크기 잉크가 닿아 있습니다. "
                        "<보기> 테두리와 문장 첫·끝 글자가 잘리지 않았는지 원본과 대조해 주세요."
                    ),
                    problem=problem,
                    path=image_path,
                    details={
                        "horizontalEdgeRiskStats": horizontal_edge_stats,
                        "horizontal_edge_risk_stats": horizontal_edge_stats,
                    },
                )
            )
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break

        if width < min_width or height < min_height:
            issues.append(
                _classin_preflight_issue(
                    "small_problem_image",
                    severity="warning",
                    message=f"문항 이미지가 작습니다 ({width}x{height}px). ClassIn에서 확대 시 가독성을 확인해 주세요.",
                    problem=problem,
                    path=image_path,
                    details={
                        "width": width,
                        "height": height,
                        "minWidth": min_width,
                        "minHeight": min_height,
                    },
                )
            )

        if dark_pixel_ratio < CLASSIN_PREFLIGHT_MIN_DARK_PIXEL_RATIO:
            issues.append(
                _classin_preflight_issue(
                    "low_ink_problem_image",
                    severity="warning",
                    message=(
                        "문항 이미지에 실제 글자/선 픽셀이 거의 없습니다. "
                        "HWP 렌더 누락이나 잘못 잘린 박스인지 확인해 주세요."
                    ),
                    problem=problem,
                    path=image_path,
                    details={
                        "darkPixelRatio": round(dark_pixel_ratio, 6),
                        "minDarkPixelRatio": CLASSIN_PREFLIGHT_MIN_DARK_PIXEL_RATIO,
                        "darkPixelThreshold": CLASSIN_PREFLIGHT_INK_THRESHOLD,
                    },
                )
            )
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break

    if len(issues) < CLASSIN_PREFLIGHT_MAX_ISSUES:
        for issue in _classin_page_chrome_artifact_issues(problems):
            issues.append(issue)
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break
    if not passage_only and len(issues) < CLASSIN_PREFLIGHT_MAX_ISSUES:
        for issue in _classin_missing_passage_child_question_issues(problems):
            issues.append(issue)
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break
    if len(issues) < CLASSIN_PREFLIGHT_MAX_ISSUES:
        for issue in _classin_board_placement_overlap_issues(problems):
            issues.append(issue)
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break
    if len(issues) < CLASSIN_PREFLIGHT_MAX_ISSUES:
        for issue in _classin_passage_group_source_reuse_issues(problems):
            issues.append(issue)
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break
    if len(issues) < CLASSIN_PREFLIGHT_MAX_ISSUES:
        for issue in _classin_source_bbox_overlap_issues(problems):
            issues.append(issue)
            if len(issues) >= CLASSIN_PREFLIGHT_MAX_ISSUES:
                break

    status = "passed" if not issues else "needs_attention"
    return {
        "status": status,
        "passed": not issues,
        "checkedProblemCount": len(problems),
        "issueCount": len(issues),
        "issues": issues,
        "thresholds": {
            "minProblemImageWidth": min_width,
            "minProblemImageHeight": min_height,
            "minDarkPixelRatio": CLASSIN_PREFLIGHT_MIN_DARK_PIXEL_RATIO,
            "darkPixelThreshold": CLASSIN_PREFLIGHT_INK_THRESHOLD,
            "placementOverlapTolerancePages": CLASSIN_PREFLIGHT_PLACEMENT_OVERLAP_TOLERANCE_PAGES,
            "passageGroupSourceReuseRatio": CLASSIN_PREFLIGHT_PASSAGE_SOURCE_REUSE_RATIO,
            "sourceBboxOverlapRatio": CLASSIN_PREFLIGHT_SOURCE_BBOX_OVERLAP_RATIO,
            "step2PageChromeMaxRatio": CLASSIN_PREFLIGHT_STEP2_PAGE_CHROME_MAX_RATIO,
            "textPrioritySourceHorizontalRecoveryPx": TEXT_PRIORITY_SOURCE_HORIZONTAL_RECOVERY_PX,
            "textPrioritySourceEdgeMinActiveRows": TEXT_PRIORITY_SOURCE_EDGE_MIN_ACTIVE_ROWS,
        },
    }


def _collect_hwp_problem_count_mismatches(
    pages: list[PageModel],
    final_problem_ids_by_page_id: dict[str, list[str]] | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    by_source: dict[str, dict[str, Any]] = {}
    for page in pages:
        source_key = _hwp_source_key(page)
        if not source_key:
            continue
        bucket = by_source.setdefault(
            source_key,
            {
                "page_ids": [],
                "detected": 0,
                "signal": 0,
            },
        )
        bucket["page_ids"].append(page.page_id)
        if final_problem_ids_by_page_id is None:
            bucket["detected"] += _count_core_hwp_problems(page)
        else:
            bucket["detected"] += _count_core_hwp_problems(
                page,
                final_problem_ids_by_page_id.get(page.page_id, []),
            )
        bucket["signal"] = max(int(bucket["signal"]), _hwp_text_problem_signal_count(page.metadata))

    flags_by_page_id: dict[str, list[str]] = {}
    messages: list[str] = []
    for bucket in by_source.values():
        signal = int(bucket["signal"])
        if signal <= 0:
            continue
        detected = int(bucket["detected"])
        expected = max(0, signal)
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


def _page_quality_payload(page: PageModel | None) -> dict[str, Any]:
    metadata = page.metadata if page is not None else {}
    route_decision = metadata.get("route_decision") if isinstance(metadata, dict) else {}
    if not isinstance(route_decision, dict):
        route_decision = {}
    profile = route_decision.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    ai_fallback = metadata.get("ai_fallback") if isinstance(metadata, dict) else {}
    if not isinstance(ai_fallback, dict):
        ai_fallback = {}

    risk_score = float(profile.get("overall_risk") or 0.0)
    segmentation_risk = float(profile.get("segmentation_risk") or 0.0)
    ocr_risk = float(profile.get("ocr_risk") or 0.0)
    grouping_risk = float(profile.get("grouping_risk") or 0.0)
    return {
        "riskScore": round(max(0.0, min(1.0, risk_score)), 4),
        "riskTier": str(profile.get("tier") or "green"),
        "parseConfidence": round(max(0.0, min(1.0, 1.0 - risk_score)), 4),
        "confidence": {
            "overall": round(max(0.0, min(1.0, 1.0 - risk_score)), 4),
            "segmentation": round(max(0.0, min(1.0, 1.0 - segmentation_risk)), 4),
            "ocr": round(max(0.0, min(1.0, 1.0 - ocr_risk)), 4),
            "grouping": round(max(0.0, min(1.0, 1.0 - grouping_risk)), 4),
        },
        "aiStatus": str(ai_fallback.get("status") or "unknown"),
    }


def build_ui_session(
    prepared_pages: list[PreparedPage],
    placements: list[dict[str, object]],
    output_dir: Path,
    edb_path: Path | None,
    source_paths: Sequence[str | Path],
    *,
    record_mode: str,
    ai_fallback_config: dict[str, Any] | None = None,
    ai_summary: dict[str, Any] | None = None,
    ocr_summary: dict[str, Any] | None = None,
    token_usage: dict[str, Any] | None = None,
    ai_cost_summary: dict[str, Any] | None = None,
    pages: list[PageModel] | None = None,
    template: LayoutTemplate | None = None,
    input_intent: str = "auto",
    input_notes: str | None = None,
    board_theme: str = DEFAULT_BOARD_THEME,
    crop_format: str = DEFAULT_CROP_FORMAT,
) -> dict[str, Any]:
    rendered_page_paths = [Path(page.source_path).resolve() for page in prepared_pages]
    resolved_input_intent = _normalize_input_intent(input_intent)
    placement_problem_ids_by_page: dict[str, list[str]] = {}
    for placement in placements:
        source_page_id = str(placement.get("source_page_id") or "")
        problem_id = str(placement.get("problem_id") or "")
        if source_page_id and problem_id:
            placement_problem_ids_by_page.setdefault(source_page_id, []).append(problem_id)
    hwp_counts_match = _hwp_problem_counts_match(pages or [], placement_problem_ids_by_page)
    warning_messages: list[str] = []
    if (
        placements
        and len(placements) <= len(prepared_pages)
        and resolved_input_intent not in {"single-problem", "page-as-is"}
        and not hwp_counts_match
    ):
        warning_messages.append(
            "감지된 문항 수가 원본 페이지 수와 비슷합니다. 여러 문제가 있는 페이지라면 검수 화면에서 분리 상태를 확인해 주세요."
        )
    resolved_template = template or LayoutTemplate(
        name="academy-default",
        board_page_count=max(50, len(placements) * 2 or 50),
        base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
        metadata={
            "placement_mode": "continuous-page-as-is"
            if resolved_input_intent == "page-as-is"
            else "one-problem-per-page"
        },
    )

    # Map page_id → PageModel for risk-flag lookup, and page_id → list[problem_id]
    # so the UI can group detected problems by their source page in the review view.
    if pages:
        _annotate_cross_page_passage_groups(pages)
    pages_by_id: dict[str, PageModel] = {}
    if pages:
        for page in pages:
            pages_by_id[page.page_id] = page
    problem_metadata_by_id: dict[str, dict[str, Any]] = {}
    for page in pages_by_id.values():
        for problem in page.problems:
            problem_metadata_by_id[problem.unit_id] = dict(problem.metadata)
    base_page_risk_flags: dict[str, list[str]] = {
        page_id: _collect_page_risk_flags(page.metadata)
        for page_id, page in pages_by_id.items()
    }

    problems: list[dict[str, Any]] = []
    problems_by_page: dict[str, list[str]] = {}
    for index, placement in enumerate(placements):
        crop_path = Path(str(placement["crop_path"])).resolve()
        source_path = Path(str(placement["source_path"])).resolve()
        source_page_id = str(placement["source_page_id"])
        bbox = placement.get("bbox") or {}
        source_segments = _normalize_problem_source_segments(
            placement.get("source_segments") or placement.get("sourceSegments"),
            fallback_page_id=source_page_id,
        )
        page_quality = _page_quality_payload(pages_by_id.get(source_page_id))
        page_flags = base_page_risk_flags.get(source_page_id, [])
        # Only propagate "this specific problem may be merged / auto-grouped"
        # reasons to per-problem flags; page-wide signals stay on the page.
        problem_flags = list(placement.get("risk_flags") or [])
        problem_flags.extend(reason for reason in page_flags if reason in _GLOBAL_RISK_REASONS)
        problem_id = str(placement["problem_id"])
        problem_metadata = problem_metadata_by_id.get(problem_id) or {}
        problem_input_intent = _normalize_input_intent(
            str(problem_metadata.get("input_intent") or problem_metadata.get("inputIntent") or resolved_input_intent)
        )
        if (
            _problem_passage_continues_across_pages(problem_metadata)
            and not bool(problem_metadata.get("passage_fragments_merged"))
        ):
            problem_flags.append(PASSAGE_CROSS_PAGE_MERGE_CHECK_RISK_FLAG)
        problem_flags = list(dict.fromkeys(str(reason) for reason in problem_flags if reason))
        passage_payload = _problem_passage_payload(problem_metadata)
        processing_step = _normalize_processing_step(
            placement.get("processing_step") or placement.get("step")
        )
        layout_diagnostics = _layout_diagnostic_from_placement(placement)
        problem_placement_mode = (
            "continuous-page-as-is"
            if problem_input_intent == "page-as-is"
            else "one-problem-per-page"
        )
        problems.append(
            {
                "id": problem_id,
                "title": _normalize_problem_title(
                    str(placement.get("title") or ""),
                    index,
                    source_page_id,
                    int(placement["problem_number"]) if str(placement.get("problem_number") or "").isdigit() else None,
                ),
                "problemNumber": int(placement["problem_number"]) if str(placement.get("problem_number") or "").isdigit() else None,
                "subject": str(placement["subject"]),
                "imagePath": _to_file_uri(crop_path),
                # Keep an immutable pointer to the first-generation crop. Image
                # enhancement must always restart here; using the latest
                # imagePath would compound ringing and generative glyph edits.
                "originalImagePath": _to_file_uri(crop_path),
                "sourceImagePath": _to_file_uri(source_path),
                "sourceFileName": source_path.name,
                "boardRenderPath": _to_file_uri(placement.get("board_render_path")),
                "preserveMediaRegions": list(placement.get("preserve_media_regions") or []),
                "preserve_media_regions": list(placement.get("preserve_media_regions") or []),
                "actualHeightPages": float(placement["actual_content_height_pages"]),
                "renderedHeightPages": layout_diagnostics["renderedHeightPages"],
                "overflowAllowed": bool(placement["overflow_allowed"]),
                "readingHeavy": bool(placement["overflow_allowed"]),
                "sourcePageId": source_page_id,
                "startYPages": float(placement["start_y_pages"]),
                "renderedTopYPages": layout_diagnostics["renderedTopYPages"],
                "renderedBottomYPages": layout_diagnostics["renderedBottomYPages"],
                "snappedNextStartYPages": float(placement["snapped_next_start_y_pages"]),
                "overflowAmountPages": float(placement["overflow_amount_pages"]),
                "overflowViolation": bool(placement["overflow_violation"]),
                "layoutDiagnostics": layout_diagnostics,
                "layout_diagnostics": layout_diagnostics,
                "slotSpanCount": int(placement["slot_span_count"]),
                "placementXRatio": float(placement.get("placement_x_ratio") or 0.0),
                "placementYRatio": float(placement.get("placement_y_ratio") or 0.0),
                "placementScaleRatio": float(placement.get("placement_scale_ratio") or 1.0),
                "preserveLegacyPlacementScale": bool(placement.get("preserve_legacy_placement_scale")),
                "preserve_legacy_placement_scale": bool(placement.get("preserve_legacy_placement_scale")),
                "step": processing_step,
                "processingStep": processing_step,
                "inputIntent": problem_input_intent,
                "input_intent": problem_input_intent,
                "forceFullPageBounds": bool(problem_metadata.get("force_full_page_bounds")),
                "force_full_page_bounds": bool(problem_metadata.get("force_full_page_bounds")),
                "placementMode": problem_placement_mode,
                "placement_mode": problem_placement_mode,
                "recordMode": str(placement.get("record_mode") or record_mode),
                "textRecordCount": int(placement.get("text_record_count", 0)),
                "imageRecordCount": int(placement.get("image_record_count", 0)),
                "bbox": {
                    "left": float(bbox.get("left", 0.0)),
                    "top": float(bbox.get("top", 0.0)),
                    "width": float(bbox.get("width", 0.0)),
                    "height": float(bbox.get("height", 0.0)),
                },
                "sourceSegments": source_segments,
                "source_segments": source_segments,
                "riskFlags": problem_flags,
                "reviewStatus": "check_needed" if problem_flags else "normal",
                "parseConfidence": page_quality["parseConfidence"],
                "confidence": page_quality["confidence"],
                "aiStatus": page_quality["aiStatus"],
                **passage_payload,
            }
        )
        problem_source_page_ids = list(
            dict.fromkeys(
                str(segment.get("sourcePageId") or "")
                for segment in source_segments
                if str(segment.get("sourcePageId") or "")
            )
        ) or [source_page_id]
        for problem_source_page_id in problem_source_page_ids:
            page_problem_ids = problems_by_page.setdefault(problem_source_page_id, [])
            if problem_id not in page_problem_ids:
                page_problem_ids.append(problem_id)

    hwp_flags_by_page_id, hwp_warning_messages = _collect_hwp_problem_count_mismatches(
        list(pages_by_id.values()),
        problems_by_page,
    )
    warning_messages.extend(hwp_warning_messages)
    page_risk_flags: dict[str, list[str]] = {
        page_id: list(
            dict.fromkeys(
                [
                    *base_page_risk_flags.get(page_id, []),
                    *hwp_flags_by_page_id.get(page_id, []),
                ]
            )
        )
        for page_id in pages_by_id
    }

    pages_payload: list[dict[str, Any]] = []
    for prepared_page in prepared_pages:
        width, height = prepared_page.image.size
        resolved_path = Path(prepared_page.source_path).resolve()
        page_model = pages_by_id.get(prepared_page.page_id)
        page_quality = _page_quality_payload(page_model)
        page_metadata = dict(page_model.metadata) if page_model is not None else dict(prepared_page.metadata)
        problem_ids = problems_by_page.get(prepared_page.page_id, [])
        risk_flags = page_risk_flags.get(prepared_page.page_id, [])
        review_status = "failed" if not problem_ids else "check_needed" if risk_flags else "normal"
        pages_payload.append(
            {
                "id": prepared_page.page_id,
                "pageNumber": prepared_page.page_number,
                "sourceImageUri": _to_file_uri(resolved_path),
                "sourceImagePath": str(resolved_path),
                "width": int(width),
                "height": int(height),
                "problemIds": problem_ids,
                "riskFlags": risk_flags,
                "reviewStatus": review_status,
                "sourceType": page_metadata.get("source_type"),
                "source_type": page_metadata.get("source_type"),
                "pageAsIsFastPath": bool(page_metadata.get("page_as_is_fast_path")),
                "page_as_is_fast_path": bool(page_metadata.get("page_as_is_fast_path")),
                "pageTileMode": str(page_metadata.get("page_tile_mode") or "off"),
                "page_tile_mode": str(page_metadata.get("page_tile_mode") or "off"),
                "pageTileSplit": bool(page_metadata.get("page_tile_split")),
                "page_tile_split": bool(page_metadata.get("page_tile_split")),
                "pageTileColumn": page_metadata.get("page_tile_column"),
                "page_tile_column": page_metadata.get("page_tile_column"),
                "pageTileConfidence": page_metadata.get("page_tile_confidence"),
                "page_tile_confidence": page_metadata.get("page_tile_confidence"),
                "pageTileParentPageId": page_metadata.get("page_tile_parent_page_id"),
                "page_tile_parent_page_id": page_metadata.get("page_tile_parent_page_id"),
                "segmentationSkipped": bool(page_metadata.get("segmentation_skipped")),
                "segmentation_skipped": bool(page_metadata.get("segmentation_skipped")),
                "ocrSkipped": bool(page_metadata.get("ocr_skipped")),
                "ocr_skipped": bool(page_metadata.get("ocr_skipped")),
                "imagePassthrough": bool(page_metadata.get("image_passthrough")),
                "image_passthrough": bool(page_metadata.get("image_passthrough")),
                "imageNormalizedCacheHit": bool(page_metadata.get("image_normalized_cache_hit")),
                "image_normalized_cache_hit": bool(page_metadata.get("image_normalized_cache_hit")),
                **page_quality,
            }
        )

    problem_counts = _session_problem_count_payload(problems)
    duplicate_problem_number_groups = _session_duplicate_problem_number_groups(problems)
    _mark_duplicate_problem_number_review_flags(problems, duplicate_problem_number_groups)
    blocking_duplicate_problem_number_groups = [
        group for group in duplicate_problem_number_groups if group.get("blocking") is not False
    ]
    source_problem_overlap_groups = _session_source_problem_overlap_groups(problems)
    _mark_source_problem_overlap_review_flags(problems, source_problem_overlap_groups)
    passage_group_source_reuse_groups = _session_passage_group_source_reuse_groups(problems)
    _mark_passage_group_source_reuse_review_flags(problems, passage_group_source_reuse_groups)
    passage_groups = _session_passage_groups(problems)
    _mark_missing_passage_child_question_review_flags(problems, passage_groups)
    cross_page_passage_group_count = sum(
        1 for group in passage_groups if group.get("continuesAcrossPages")
    )
    passage_review_items = _session_passage_review_items(problems, passage_groups)
    cross_page_passage_review_item_count = sum(
        1 for item in passage_review_items if item.get("continuesAcrossPages")
    )
    layout_diagnostics = _layout_diagnostics_from_problems(problems)

    return {
        "session_name": output_dir.name,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "data_source": "question_export",
        "output_dir": str(output_dir.resolve()),
        "source_mode": "batch" if len(source_paths) > 1 else "single",
        "input_intent": resolved_input_intent,
        "input_notes": (input_notes or "").strip(),
        "input_file_count": len(source_paths),
        "input_files": [str(Path(path).resolve()) for path in source_paths],
        "source_page_count": len(prepared_pages),
        **problem_counts,
        "duplicate_problem_number_groups": duplicate_problem_number_groups,
        "duplicateProblemNumberGroups": duplicate_problem_number_groups,
        "duplicate_problem_number_group_count": len(duplicate_problem_number_groups),
        "duplicateProblemNumberGroupCount": len(duplicate_problem_number_groups),
        "blocking_duplicate_problem_number_groups": blocking_duplicate_problem_number_groups,
        "blockingDuplicateProblemNumberGroups": blocking_duplicate_problem_number_groups,
        "blocking_duplicate_problem_number_group_count": len(blocking_duplicate_problem_number_groups),
        "blockingDuplicateProblemNumberGroupCount": len(blocking_duplicate_problem_number_groups),
        "source_problem_overlap_groups": source_problem_overlap_groups,
        "sourceProblemOverlapGroups": source_problem_overlap_groups,
        "source_problem_overlap_group_count": len(source_problem_overlap_groups),
        "sourceProblemOverlapGroupCount": len(source_problem_overlap_groups),
        "passage_group_source_reuse_groups": passage_group_source_reuse_groups,
        "passageGroupSourceReuseGroups": passage_group_source_reuse_groups,
        "passage_group_source_reuse_group_count": len(passage_group_source_reuse_groups),
        "passageGroupSourceReuseGroupCount": len(passage_group_source_reuse_groups),
        "passage_groups": passage_groups,
        "passageGroups": passage_groups,
        "passage_group_count": len(passage_groups),
        "passageGroupCount": len(passage_groups),
        "passage_problem_count": sum(int(group.get("problemCount") or 0) for group in passage_groups),
        "passageProblemCount": sum(int(group.get("problemCount") or 0) for group in passage_groups),
        "cross_page_passage_group_count": cross_page_passage_group_count,
        "crossPagePassageGroupCount": cross_page_passage_group_count,
        "passage_review_items": passage_review_items,
        "passageReviewItems": passage_review_items,
        "passage_review_item_count": len(passage_review_items),
        "passageReviewItemCount": len(passage_review_items),
        "cross_page_passage_review_item_count": cross_page_passage_review_item_count,
        "crossPagePassageReviewItemCount": cross_page_passage_review_item_count,
        "layout_diagnostics": layout_diagnostics,
        "layoutDiagnostics": layout_diagnostics,
        "export_mode": "question",
        "record_mode": record_mode,
        "board_theme": _resolve_board_theme(board_theme),
        "crop_format": crop_format,
        "pages_json_path": str((output_dir / "pages.json").resolve()),
        "placements_json_path": str((output_dir / "placements.json").resolve()),
        "board_render_dir": str(
            Path(str(placements[0]["board_render_path"])).resolve().parent
            if placements and placements[0].get("board_render_path")
            else (output_dir / "problem_cutouts").resolve()
        ),
        "edb_path": str(edb_path.resolve()) if edb_path else None,
        "edb_file_uri": _to_file_uri(edb_path),
        "rendered_page_paths": [str(path) for path in rendered_page_paths],
        "rendered_page_file_uris": [_to_file_uri(path) for path in rendered_page_paths],
        "template": _template_to_dict(resolved_template),
        "ai_fallback": ai_fallback_config,
        "ai_summary": ai_summary,
        "ocr_summary": ocr_summary,
        "token_usage": token_usage,
        "ai_cost_summary": ai_cost_summary,
        "aiCostSummary": ai_cost_summary,
        "warning_messages": warning_messages,
        "problems": problems,
        "pages": pages_payload,
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


def write_classin_handoff_manifest(
    output_dir: Path,
    *,
    source_paths: Sequence[Path],
    edb_path: Path,
    edb_parts: list[dict[str, Any]] | None = None,
    ui_session: dict[str, Any],
    summary: dict[str, Any],
    template: LayoutTemplate,
) -> tuple[Path, Path]:
    expected_record_count = int(summary.get("record_count") or len(summary.get("placements") or []))
    content_target = _normalize_content_target(
        str(ui_session.get("contentTarget") or ui_session.get("content_target") or "all")
    )
    passage_only = content_target == "shared-passages"
    review_summary = ui_session.get("reviewSummary") or ui_session.get("review_summary") or {}
    duplicate_problem_number_groups = (
        ui_session.get("duplicateProblemNumberGroups")
        or ui_session.get("duplicate_problem_number_groups")
        or []
    )
    if not isinstance(duplicate_problem_number_groups, list):
        duplicate_problem_number_groups = []
    duplicate_problem_number_groups = _duplicate_problem_number_groups_as_nonblocking(duplicate_problem_number_groups)
    blocking_duplicate_problem_number_groups: list[dict[str, Any]] = []
    duplicate_problem_number_note = _duplicate_problem_number_note(duplicate_problem_number_groups)
    classin_preflight = _classin_handoff_preflight(ui_session)
    raw_problems = ui_session.get("problems")
    problems = raw_problems if isinstance(raw_problems, list) else []
    passage_group_source_reuse_groups = (
        ui_session.get("passageGroupSourceReuseGroups")
        or ui_session.get("passage_group_source_reuse_groups")
        or _session_passage_group_source_reuse_groups(problems)
    )
    if not isinstance(passage_group_source_reuse_groups, list):
        passage_group_source_reuse_groups = []
    passage_groups = _session_passage_groups(problems)
    if passage_only:
        passage_groups = [
            {
                **group,
                "missingChildProblemNumbers": [],
                "missingChildProblemCount": 0,
            }
            for group in passage_groups
        ]
    cross_page_passage_group_count = sum(
        1 for group in passage_groups if group.get("continuesAcrossPages")
    )
    passage_review_items = (
        _session_passage_review_items(problems, passage_groups)
        if passage_only
        else (
            ui_session.get("passageReviewItems")
            or ui_session.get("passage_review_items")
            or _session_passage_review_items(problems, passage_groups)
        )
    )
    if not isinstance(passage_review_items, list):
        passage_review_items = []
    cross_page_passage_review_item_count = sum(
        1 for item in passage_review_items if isinstance(item, dict) and item.get("continuesAcrossPages")
    )
    layout_diagnostics = (
        ui_session.get("layoutDiagnostics")
        or ui_session.get("layout_diagnostics")
        or _layout_diagnostics_from_problems(problems)
    )
    if not isinstance(layout_diagnostics, dict):
        layout_diagnostics = _layout_diagnostics_from_problems(problems)
    ready_for_classin = bool(classin_preflight.get("passed")) and not blocking_duplicate_problem_number_groups
    handoff_status = "ready_for_classin_review" if ready_for_classin else "needs_attention_before_classin"
    actual_edb_page_count_hint = max(
        [
            int(part.get("pageCountHint") or part.get("page_count_hint") or 0)
            for part in (edb_parts or [])
            if isinstance(part, dict)
        ],
        default=min(int(template.board_page_count), CLASSIN_MAX_BOARD_PAGE_COUNT),
    )
    payload = {
        "status": handoff_status,
        "readyForClassIn": ready_for_classin,
        "manualReviewRequired": True,
        "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "sourcePaths": [str(path.resolve()) for path in source_paths],
        "outputDir": str(output_dir.resolve()),
        "edbPath": str(edb_path.resolve()),
        "edbFileName": edb_path.name,
        "edbParts": [dict(part) for part in (edb_parts or [])],
        "edbPartCount": len(edb_parts or []) or 1,
        "edbSplit": bool(edb_parts and len(edb_parts) > 1),
        "expectedRecordCount": expected_record_count,
        "expectedCoreProblemCount": int(ui_session.get("core_problem_count") or 0),
        "expectedSupplementalItemCount": int(ui_session.get("supplemental_item_count") or 0),
        "detectedProblemCount": int(ui_session.get("detected_problem_count") or expected_record_count),
        "sourcePageCount": int(ui_session.get("source_page_count") or 0),
        "classinPageCountHint": actual_edb_page_count_hint,
        "globalBoardPageCountEstimate": int(template.board_page_count),
        "recordMode": str(summary.get("record_mode") or ui_session.get("record_mode") or ""),
        "contentTarget": content_target,
        "content_target": content_target,
        "cropFormat": str(summary.get("crop_format") or ui_session.get("crop_format") or ""),
        "boardTheme": str(summary.get("board_theme") or ui_session.get("board_theme") or ""),
        "duplicateProblemNumberGroups": duplicate_problem_number_groups,
        "blockingDuplicateProblemNumberGroups": blocking_duplicate_problem_number_groups,
        "duplicateProblemNumberNote": duplicate_problem_number_note,
        "passageGroupSourceReuseGroups": passage_group_source_reuse_groups,
        "passageGroupSourceReuseGroupCount": len(passage_group_source_reuse_groups),
        "passageGroups": passage_groups,
        "passageGroupCount": len(passage_groups),
        "passageProblemCount": sum(int(group.get("problemCount") or 0) for group in passage_groups),
        "crossPagePassageGroupCount": cross_page_passage_group_count,
        "passageReviewItems": passage_review_items,
        "passageReviewItemCount": len(passage_review_items),
        "crossPagePassageReviewItemCount": cross_page_passage_review_item_count,
        "layoutDiagnostics": layout_diagnostics,
        "layout_diagnostics": layout_diagnostics,
        "classinPreflight": classin_preflight,
        "classin_preflight": classin_preflight,
        "reviewRiskCounts": review_summary.get("riskFlagCounts", {}) if isinstance(review_summary, dict) else {},
        "classinReviewChecklist": [
            "ClassIn에서 EDB 파일 열기",
            "문항 수와 순서가 기대값과 일치하는지 확인",
            "각 문항 이미지가 잘리지 않고 읽히는지 확인",
            "긴 지문/공통 지문 그룹이 하위 문항과 함께 자연스럽게 배치됐는지 확인",
            "긴 이미지 자동 확장 항목이 다음 문항을 침범하지 않는지 확인",
            "보충 자료/이어지는 자료가 문항 뒤에 자연스럽게 배치됐는지 확인",
            "확대/축소와 페이지 이동 시 썸네일/보드가 깨지지 않는지 확인",
        ],
        "manualReviewResult": {
            "classinOpened": None,
            "recordCountOk": None,
            "orderOk": None,
            "readabilityOk": None,
            "supplementalItemsOk": None,
            "notes": "",
        },
    }
    json_path = output_dir / "classin_handoff.json"
    markdown_path = output_dir / "classin_handoff.md"
    payload["classinHandoffMarkdownPath"] = str(markdown_path.resolve())
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    checklist = "\n".join(f"- [ ] {item}" for item in payload["classinReviewChecklist"])
    duplicate_problem_number_lines = (
        ["", f"- {duplicate_problem_number_note}"]
        if duplicate_problem_number_note
        else []
    )
    passage_group_lines: list[str] = []
    if passage_groups:
        passage_group_lines = ["", "## Passage Groups"]
        for group in passage_groups:
            label = str(group.get("numberLabel") or group.get("groupId") or "").strip()
            page_label = ", ".join(str(page_id) for page_id in group.get("sourcePageIds") or [])
            status_parts = [
                f"{int(group.get('problemCount') or 0)} problems",
                f"pages {page_label}" if page_label else "",
                "cross-page" if group.get("continuesAcrossPages") else "single-page",
            ]
            status = " · ".join(part for part in status_parts if part)
            passage_group_lines.append(
                f"- `{group.get('groupId')}` {label}"
                + (f" · {status}" if status else "")
            )
    passage_review_lines: list[str] = []
    if passage_review_items:
        passage_review_lines = ["", "## Passage Review Queue"]
        for item in passage_review_items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("numberLabel") or item.get("groupId") or "").strip()
            reasons = ", ".join(
                formatted
                for formatted in (
                    _format_passage_review_reason(reason)
                    for reason in item.get("reviewReasonCodes") or []
                )
                if formatted
            )
            problem_ids = ", ".join(_ordered_unique_strings(item.get("problemIds") or item.get("problem_ids") or []))
            fragment_ids = ", ".join(
                _ordered_unique_strings(item.get("fragmentProblemIds") or item.get("fragment_problem_ids") or [])
            )
            page_ids = ", ".join(_ordered_unique_strings(item.get("sourcePageIds") or item.get("source_page_ids") or []))
            missing_numbers = ", ".join(
                str(number)
                for number in _ordered_unique_ints(
                    item.get("missingChildProblemNumbers") or item.get("missing_child_problem_numbers") or []
                )
            )
            passage_review_lines.append(
                f"- `{item.get('groupId')}` {label}"
                + (f" · {item.get('message')}" if item.get("message") else "")
                + (f" · problems: {problem_ids}" if problem_ids else "")
                + (f" · fragments: {fragment_ids}" if fragment_ids else "")
                + (f" · pages: {page_ids}" if page_ids else "")
                + (f" · missing: {missing_numbers}" if missing_numbers else "")
                + (f" · reasons: {reasons}" if reasons else "")
            )
    layout_lines: list[str] = ["", "## Layout Diagnostics"]
    layout_label = str(layout_diagnostics.get("label") or "").strip()
    layout_lines.append(f"- Summary: {layout_label or 'no long-image expansion detected'}")
    layout_lines.append(f"- Auto-extended items: {int(layout_diagnostics.get('autoExtendedCount') or layout_diagnostics.get('auto_extended_count') or 0)}")
    layout_lines.append(f"- Overlap risks after reflow: {int(layout_diagnostics.get('overlapRiskCount') or layout_diagnostics.get('overlap_risk_count') or 0)}")
    layout_items = layout_diagnostics.get("items") if isinstance(layout_diagnostics.get("items"), list) else []
    for item in layout_items[:12]:
        if not isinstance(item, dict):
            continue
        problem_id = str(item.get("problemId") or item.get("problem_id") or "").strip()
        title = str(item.get("title") or "").strip()
        rendered_pages = float(item.get("renderedHeightPages") or item.get("rendered_height_pages") or 0.0)
        reserved_pages = float(item.get("reservedSpanPages") or item.get("reserved_span_pages") or 0.0)
        scale_ratio = float(item.get("placementScaleRatio") or item.get("placement_scale_ratio") or 1.0)
        risk_label = " · overlap risk" if item.get("overlapRisk") or item.get("overlap_risk") else ""
        layout_lines.append(
            f"- `{problem_id}` {title} · rendered {rendered_pages:.2f}p"
            f" · reserved {reserved_pages:.2f}p · scale {scale_ratio:.2f}x{risk_label}"
        )
    if classin_preflight["passed"]:
        preflight_lines = ["- OK: no automatic asset issues found."]
    else:
        preflight_lines = [
            f"- {_classin_preflight_issue_label(issue.get('type'))} (`{issue['type']}`, {issue['severity']}): {issue['message']}"
            + (f" [{issue.get('problemId')}]" if issue.get("problemId") else "")
            for issue in classin_preflight["issues"]
        ]
    edb_part_lines = [
        f"- Part {part.get('partIndex') or part.get('part_index')}: `{part.get('edbPath') or part.get('edb_path')}`"
        for part in payload["edbParts"]
    ]
    markdown_path.write_text(
        "\n".join(
            [
                "# ClassIn EDB Handoff",
                "",
                f"- Handoff status: `{payload['status']}`",
                f"- Ready for ClassIn: {'yes' if payload['readyForClassIn'] else 'no'}",
                f"- EDB: `{payload['edbPath']}`",
                *(
                    ["", "## EDB Parts", *edb_part_lines]
                    if len(edb_part_lines) > 1
                    else []
                ),
                f"- Expected records: {payload['expectedRecordCount']}",
                f"- Core problems: {payload['expectedCoreProblemCount']}",
                f"- Supplemental items: {payload['expectedSupplementalItemCount']}",
                f"- ClassIn page hint: {payload['classinPageCountHint']}",
                *duplicate_problem_number_lines,
                *passage_group_lines,
                *passage_review_lines,
                *layout_lines,
                "",
                "## Manual Checklist",
                checklist,
                "",
                "## ClassIn Preflight",
                f"- Status: {classin_preflight['status']}",
                f"- Checked problems: {classin_preflight['checkedProblemCount']}",
                f"- Issues: {classin_preflight['issueCount']}",
                *preflight_lines,
                "",
                "## Notes",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return json_path.resolve(), markdown_path.resolve()


def _classin_handoff_session_fields(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    status = str(payload.get("status") or "").strip()
    ready = (
        bool(payload.get("readyForClassIn"))
        if "readyForClassIn" in payload
        else status == "ready_for_classin_review"
    )
    preflight = payload.get("classinPreflight") if isinstance(payload.get("classinPreflight"), dict) else {}
    issue_count = int(preflight.get("issueCount") or preflight.get("issue_count") or 0)
    layout_diagnostics = (
        payload.get("layoutDiagnostics")
        if isinstance(payload.get("layoutDiagnostics"), dict)
        else payload.get("layout_diagnostics")
        if isinstance(payload.get("layout_diagnostics"), dict)
        else {}
    )
    return {
        "classinHandoffStatus": status,
        "classin_handoff_status": status,
        "readyForClassIn": ready,
        "ready_for_classin": ready,
        "classinPreflight": preflight,
        "classin_preflight": preflight,
        "classinPreflightStatus": str(preflight.get("status") or ""),
        "classin_preflight_status": str(preflight.get("status") or ""),
        "classinPreflightIssueCount": issue_count,
        "classin_preflight_issue_count": issue_count,
        "layoutDiagnostics": layout_diagnostics,
        "layout_diagnostics": layout_diagnostics,
    }


def normalize_text_payload(text: str | None) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.replace("\r", "\n").split("\n")]
    cleaned = [line for line in lines if line]
    return "\n".join(cleaned)


def choose_block_record_mode(block: ContentBlock, *, text_confidence_threshold: float) -> str:
    if block.metadata.get("force_image_record"):
        return "image"
    if block.block_type in IMAGE_ONLY_BLOCK_TYPES:
        return "image"
    text = normalize_text_payload(block.text)
    if not text:
        return "image"
    if block.block_type not in TEXT_ELIGIBLE_BLOCK_TYPES:
        return "image"
    confidence = block.confidence if block.confidence is not None else 0.0
    if confidence < text_confidence_threshold:
        return "image"
    return "text"


def resolve_font_size(block: ContentBlock, scale: float) -> int:
    base_size = block.style.font_size if block.style and block.style.font_size else max(14.0, block.bbox.height * 0.32)
    scaled = base_size * max(scale, 0.25) * 0.9
    return int(max(10, min(40, round(scaled))))


def _validate_problem_entry_ids(problem_entries: Sequence[ProblemEntry]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    missing_indices: list[int] = []
    for index, entry in enumerate(problem_entries):
        problem_id = str(entry.problem_id or "").strip()
        if not problem_id:
            missing_indices.append(index)
            continue
        if problem_id in seen and problem_id not in duplicates:
            duplicates.append(problem_id)
        seen.add(problem_id)
    if missing_indices:
        preview = ", ".join(str(index) for index in missing_indices[:3])
        raise ValueError(f"Problem ID must not be empty (entry index: {preview})")
    if duplicates:
        preview = ", ".join(duplicates[:3])
        raise ValueError(f"Duplicate problem ID is not allowed: {preview}")


def placement_inputs(
    problem_entries: list[ProblemEntry],
    *,
    actual_height_pages_by_problem_id: Mapping[str, float] | None = None,
) -> list[ProblemLayoutInput]:
    return [
        ProblemLayoutInput(
            problem_id=entry.problem_id,
            subject=entry.subject,
            actual_content_height_pages=(
                float(actual_height_pages_by_problem_id[entry.problem_id])
                if actual_height_pages_by_problem_id is not None
                and entry.problem_id in actual_height_pages_by_problem_id
                else entry.actual_height_pages
            ),
            overflow_allowed=entry.overflow_allowed,
            reading_heavy=entry.reading_heavy,
            metadata={
                "title": entry.title,
                "problem_number": entry.problem_number,
                "crop_path": str(entry.crop_path),
                "board_render_path": str(entry.board_render_path),
                "preserve_media_regions": list(entry.preserve_media_regions),
                "source_page_id": entry.source_page_id,
                "source_path": entry.source_path,
                "bbox": {
                    "left": entry.bounds.left,
                    "top": entry.bounds.top,
                    "width": entry.bounds.width,
                    "height": entry.bounds.height,
                },
                "source_segments": [dict(segment) for segment in entry.source_segments],
                "risk_flags": list(entry.risk_flags),
                "processing_step": _normalize_processing_step(entry.processing_step),
                "placement_scale_ratio": _clamp_placement_scale_ratio(
                    entry.placement_scale_ratio,
                    PLACEMENT_FIT_WIDTH_SCALE_MAX
                    if _entry_uses_continuous_page_flow(entry)
                    or _entry_preserves_legacy_placement_scale(entry)
                    else PLACEMENT_SCALE_MAX,
                ) or 1.0,
                "preserve_legacy_placement_scale": _entry_preserves_legacy_placement_scale(entry),
                "reserve_scaled_height": _entry_preserves_legacy_placement_scale(entry),
                "input_intent": entry.input_intent,
                "force_full_page_bounds": entry.force_full_page_bounds,
            },
        )
        for entry in problem_entries
    ]


def _image_only_layout_height_pages(
    entry: ProblemEntry,
    template: LayoutTemplate,
    *,
    crop_format: str,
) -> float:
    """Return the height the EDB image record will occupy before user scaling."""

    if crop_format == CROP_FORMAT_V2:
        rendered_width_px = float(V2_TARGET_IMAGE_WIDTH_PX)
    else:
        rendered_width_px = _v1_default_display_width_px(template)

    try:
        with Image.open(entry.crop_path) as image:
            width_px, height_px = image.size
    except OSError:
        return entry.actual_height_pages

    estimated = rendered_width_px * (float(height_px) / max(float(width_px), 1.0)) / CANVAS_WIDTH
    return max(MIN_HEIGHT_PAGES, min(MAX_HEIGHT_PAGES, estimated))


def _image_only_layout_heights_by_problem_id(
    problem_entries: list[ProblemEntry],
    template: LayoutTemplate,
    *,
    crop_format: str,
) -> dict[str, float]:
    if crop_format == CROP_FORMAT_V1 and template.metadata.get("preserve_source_layout"):
        return {}
    return {
        entry.problem_id: _image_only_layout_height_pages(entry, template, crop_format=crop_format)
        for entry in problem_entries
    }


def _ensure_template_board_capacity(
    template: LayoutTemplate,
    placements: Sequence[Any],
) -> None:
    required_pages = max(
        (
            max(
                float(getattr(placement, "snapped_next_start_y_pages", 0.0) or 0.0),
                float(getattr(placement, "actual_bottom_y_pages", 0.0) or 0.0),
            )
            for placement in placements
        ),
        default=0.0,
    )
    if required_pages <= 0:
        return
    template.board_page_count = max(template.board_page_count, int(math.ceil(required_pages)))


def template_with_board_page_count(template: LayoutTemplate, board_page_count: int) -> LayoutTemplate:
    return LayoutTemplate(
        name=template.name,
        board_page_count=max(1, int(board_page_count)),
        base_slot_height_pages=template.base_slot_height_pages,
        fixed_left_zone_ratio=template.fixed_left_zone_ratio,
        preserve_right_writing_zone=template.preserve_right_writing_zone,
        default_overflow_subjects=set(template.default_overflow_subjects),
        metadata=dict(template.metadata),
    )


def _entries_flow_end_pages(problem_entries: list[ProblemEntry], template: LayoutTemplate) -> float:
    placements = place_problems(placement_inputs(problem_entries), template=template)
    if not placements:
        return 0.0
    return max(
        max(placement.actual_bottom_y_pages, placement.snapped_next_start_y_pages)
        for placement in placements
    )


def _placement_summary_end_pages(placement: dict[str, object]) -> float:
    values: list[float] = []
    for key in ("record_bottom_y_pages", "actual_bottom_y_pages", "snapped_next_start_y_pages"):
        try:
            raw_value = placement.get(key)
            if raw_value is not None:
                values.append(float(raw_value))
        except (TypeError, ValueError):
            continue
    return max(values, default=0.0)


def _placement_summaries_flow_end_pages(placements: list[dict[str, object]]) -> float:
    return max((_placement_summary_end_pages(placement) for placement in placements), default=0.0)


def _validate_record_page_count_hints(
    placements: Sequence[Mapping[str, object]],
    *,
    expected_page_count: int,
) -> None:
    expected = max(1, int(expected_page_count))
    mismatches: list[str] = []
    for placement in placements:
        raw_hint = placement.get("record_page_count_hint") or placement.get("recordPageCountHint")
        if raw_hint is None:
            continue
        try:
            actual = int(raw_hint)
        except (TypeError, ValueError):
            actual = 0
        if actual != expected:
            mismatches.append(str(placement.get("problem_id") or placement.get("problemId") or "unknown"))
    if mismatches:
        preview = ", ".join(mismatches[:3])
        raise ValueError(
            f"EDB image records use a different page scale than the {expected}-page header: {preview}"
        )


def _validate_sequential_record_placements(
    placements: Sequence[Mapping[str, object]],
    *,
    tolerance_pages: float = CLASSIN_PREFLIGHT_PLACEMENT_OVERLAP_TOLERANCE_PAGES,
) -> None:
    """Reject a rendered part whose sequential records overlap vertically."""

    tolerance = max(0.0, float(tolerance_pages))
    overlaps: list[str] = []
    for current, next_placement in zip(placements, placements[1:]):
        try:
            current_bottom = float(current["record_bottom_y_pages"])
            next_top = float(next_placement["record_top_y_pages"])
        except (KeyError, TypeError, ValueError):
            continue
        overlap_pages = current_bottom - next_top
        if overlap_pages <= tolerance:
            continue
        current_id = str(current.get("problem_id") or current.get("problemId") or "unknown")
        next_id = str(next_placement.get("problem_id") or next_placement.get("problemId") or "unknown")
        overlaps.append(f"{current_id}->{next_id} ({overlap_pages:.6f} pages)")

    if overlaps:
        preview = ", ".join(overlaps[:3])
        raise ValueError(f"EDB sequential image records overlap after rendering: {preview}")


def _first_placement_over_page_limit(placements: list[dict[str, object]], max_pages: int) -> int | None:
    for index, placement in enumerate(placements):
        if _placement_summary_end_pages(placement) > max_pages + 1e-6:
            return index
    return None


def split_problem_entries_for_classin_page_limit(
    problem_entries: list[ProblemEntry],
    template: LayoutTemplate,
    *,
    max_page_count: int = CLASSIN_MAX_BOARD_PAGE_COUNT,
) -> list[list[ProblemEntry]]:
    max_pages = max(1, int(max_page_count))
    if not problem_entries:
        return []
    _validate_problem_entry_ids(problem_entries)
    limited_template = template_with_board_page_count(template, max_pages)
    chunks: list[list[ProblemEntry]] = []
    current: list[ProblemEntry] = []
    cursor_pages = 0.0

    # Placement is cursor-based, so each entry can be evaluated once instead
    # of rebuilding every growing candidate prefix (quadratic for long sets).
    for entry, layout_input in zip(problem_entries, placement_inputs(problem_entries)):
        placement = place_problems(
            [layout_input],
            template=limited_template,
            start_y_pages=cursor_pages,
        )[0]
        end_pages = max(placement.actual_bottom_y_pages, placement.snapped_next_start_y_pages)
        if current and end_pages > max_pages + 1e-6:
            chunks.append(current)
            current = []
            placement = place_problems([layout_input], template=limited_template)[0]
            end_pages = max(placement.actual_bottom_y_pages, placement.snapped_next_start_y_pages)
        if end_pages > max_pages + 1e-6:
            raise ValueError(
                f"Problem '{entry.problem_id}' exceeds ClassIn's {max_pages}-page limit "
                f"({end_pages:.3f} pages); split the source before publishing"
            )
        current.append(entry)
        cursor_pages = placement.snapped_next_start_y_pages

    if current:
        chunks.append(current)
    return chunks


def edb_part_file_name(edb_name: str, part_index: int, part_count: int) -> str:
    if part_count <= 1:
        return edb_name
    path = Path(edb_name)
    suffix = path.suffix or ".edb"
    stem = path.stem or "classin"
    width = max(2, len(str(part_count)))
    return f"{stem}_part{part_index + 1:0{width}d}{suffix}"


def _resize_to_target_width(image: Image.Image, target_width_px: int) -> Image.Image:
    if target_width_px <= 0 or image.width == target_width_px:
        return image
    aspect = image.height / max(image.width, 1)
    new_height = max(1, int(round(target_width_px * aspect)))
    return image.resize((target_width_px, new_height), Image.Resampling.LANCZOS)


def _v2_encoded_image_size(
    source_size: tuple[int, int],
    display_size: tuple[float, float],
) -> tuple[int, int]:
    """Choose bitmap pixels independently from ClassIn's logical display hints.

    ClassIn v2 samples use a ~301px logical base width, but encoding the source
    bitmap at that width destroys small Korean/English glyph detail. Keep the
    display geometry unchanged while storing an oversampled bitmap.
    """
    source_width, source_height = source_size
    display_width, _display_height = display_size
    if source_width <= 0 or source_height <= 0:
        return 1, 1
    source_pixels = source_width * source_height
    source_is_export_safe = (
        source_width >= V2_ENCODED_IMAGE_MIN_WIDTH_PX
        and source_width <= V2_ENCODED_IMAGE_MAX_WIDTH_PX
        and max(source_width, source_height) <= V2_ENCODED_IMAGE_MAX_EDGE_PX
        and source_pixels <= V2_ENCODED_IMAGE_MAX_PIXELS
    )
    if source_is_export_safe:
        # Keeping the exact source dimensions avoids an unnecessary Lanczos
        # pass and preserves small glyph strokes for later ClassIn zooming.
        return source_width, source_height

    target_width = int(round(
        max(
            V2_ENCODED_IMAGE_MIN_WIDTH_PX,
            min(V2_ENCODED_IMAGE_MAX_WIDTH_PX, display_width * V2_ENCODED_IMAGE_OVERSAMPLE),
        )
    ))
    aspect = source_height / max(source_width, 1)
    target_height = max(1, int(round(target_width * aspect)))
    pixel_count = target_width * target_height
    edge_scale = V2_ENCODED_IMAGE_MAX_EDGE_PX / max(target_width, target_height, 1)
    pixel_scale = math.sqrt(V2_ENCODED_IMAGE_MAX_PIXELS / float(max(pixel_count, 1)))
    scale = min(1.0, edge_scale, pixel_scale)
    if scale < 1.0:
        target_width = max(1, int(math.floor(target_width * scale)))
        target_height = max(1, int(math.floor(target_height * scale)))
    return target_width, target_height


def _v1_source_layout_transform(problem_entries: list[ProblemEntry]) -> tuple[float, float, float] | None:
    if len(problem_entries) <= 1:
        return None
    bounds = [entry.bounds for entry in problem_entries if entry.bounds.width > 0 and entry.bounds.height > 0]
    if len(bounds) != len(problem_entries):
        return None

    left = min(bound.left for bound in bounds)
    top = min(bound.top for bound in bounds)
    right = max(bound.left + bound.width for bound in bounds)
    bottom = max(bound.top + bound.height for bound in bounds)
    layout_width = right - left
    layout_height = bottom - top
    if layout_width <= 0 or layout_height <= 0:
        return None

    max_width = CANVAS_HEIGHT - V1_LAYOUT_MARGIN_X_PX * 2
    max_height = CANVAS_WIDTH * V1_LAYOUT_MAX_HEIGHT_PAGES - V1_LAYOUT_MARGIN_Y_PX * 2
    scale = min(max_width / layout_width, max_height / layout_height)
    scale = max(0.2, min(1.0, scale))
    return left, top, scale


def _v1_default_display_width_px(template: LayoutTemplate) -> float:
    legacy_width = CANVAS_HEIGHT * template.fixed_left_zone_ratio - LEFT_MARGIN_PX - RIGHT_PADDING_PX
    max_width = CANVAS_HEIGHT - LEFT_MARGIN_PX - RIGHT_PADDING_PX
    return max(legacy_width, min(V1_DEFAULT_DISPLAY_WIDTH_PX, max_width))


def _build_image_only_record_image(
    placement: Any,
    entry: ProblemEntry,
    *,
    dark_board: bool,
    board_theme: str,
    crop_format: str,
    target_image_width_px: float,
    continuous_flow: bool,
    generate_record: bool = True,
) -> _ImageOnlyRecordImage:
    processing_step = _normalize_processing_step(
        entry.processing_step or placement.metadata.get("processing_step")
    )
    text_priority = _problem_prefers_text_preservation(
        entry.subject,
        entry.source_path,
        entry.title,
    )
    chalk_color = _resolve_chalk_color(board_theme)
    chalk_math_media = _stage2_prefers_chalk_math_media(
        entry.subject,
        processing_step,
        entry.source_path,
        entry.source_page_id,
        entry.title,
    )
    crop_path = Path(str(placement.metadata["crop_path"]))
    board_render_path = Path(str(placement.metadata["board_render_path"]))
    reuse_raw_page_bytes = (
        continuous_flow
        and entry.force_full_page_bounds
        and processing_step == PROCESSING_STEP_RAW
        and crop_format == CROP_FORMAT_V1
    )
    with Image.open(crop_path) as loaded_crop:
        source_format = str(loaded_crop.format or "").upper()
        if not generate_record:
            source_width, source_height = loaded_crop.size
            scale_ratio: float | None = None
            display_width = float(source_width)
            display_height = float(source_height)
            encoded_width = source_width
            encoded_height = source_height
            if crop_format == CROP_FORMAT_V2:
                base_display_width = float(target_image_width_px or V2_TARGET_IMAGE_WIDTH_PX)
                base_display_height = base_display_width * source_height / max(source_width, 1)
                scale_ratio = _problem_scale_ratio(
                    entry,
                    placement,
                    base_display_width,
                    base_display_height,
                    ignore_height_limit=continuous_flow,
                )
                display_width = base_display_width * scale_ratio
                display_height = base_display_height * scale_ratio
                encoded_width, encoded_height = _v2_encoded_image_size(
                    (source_width, source_height),
                    (display_width, display_height),
                )
            return _ImageOnlyRecordImage(
                crop_path=crop_path,
                board_render_path=board_render_path,
                image_bytes=b"",
                secondary_bytes=b"",
                width_px=encoded_width,
                height_px=encoded_height,
                scale_ratio=scale_ratio,
                display_width_px=display_width,
                display_height_px=display_height,
                source_width_px=source_width,
                source_height_px=source_height,
                resolution_policy=(
                    "source-preserving"
                    if (encoded_width, encoded_height) == (source_width, source_height)
                    else "adaptive"
                ),
            )
        crop_image = loaded_crop.convert("RGBA" if "A" in loaded_crop.getbands() else "RGB")
        source_width, source_height = loaded_crop.size
    if reuse_raw_page_bytes:
        # "Page as-is" is both a layout and fidelity contract. Reusing the
        # normalized source avoids a redundant background-removal pass and a
        # lossy or CPU-heavy re-encode while keeping every source pixel.
        board_image = crop_image
    elif (
        dark_board
        and processing_step == PROCESSING_STEP_RECONSTRUCT
        and not entry.board_render_preprocessed
    ):
        board_image = _build_transparent_reconstruction_image(
            crop_image,
            board_theme=board_theme,
            text_priority=text_priority,
        )
    elif dark_board and chalk_math_media and not entry.board_render_preprocessed:
        # Older sessions may already contain a Stage-2 board render whose
        # detected figure was restored as an opaque source patch. Rebuild the
        # chalk cutout from the immutable crop so those legacy white boxes do
        # not survive a fresh EDB export.
        board_image = _extract_problem_cutout(
            _enhance_problem_crop(crop_image),
            chalk_color=chalk_color,
        )
        if crop_format == CROP_FORMAT_V1 and board_image.size != crop_image.size:
            board_image = board_image.resize(crop_image.size, Image.Resampling.LANCZOS)
    elif dark_board:
        board_image = _load_board_export_image(
            board_render_path,
            crop_image,
            board_theme=board_theme,
            target_size=crop_image.size if crop_format == CROP_FORMAT_V1 else None,
            text_priority=text_priority,
            trusted_preprocessed_render=entry.board_render_preprocessed,
        )
    else:
        board_image = crop_image

    scale_ratio: float | None = None
    display_width = float(board_image.width)
    display_height = float(board_image.height)
    if crop_format == CROP_FORMAT_V2:
        base_display_width = float(target_image_width_px or V2_TARGET_IMAGE_WIDTH_PX)
        base_display_height = base_display_width * board_image.height / max(board_image.width, 1)
        scale_ratio = _problem_scale_ratio(
            entry,
            placement,
            base_display_width,
            base_display_height,
            ignore_height_limit=continuous_flow,
        )
        display_width = base_display_width * scale_ratio
        display_height = base_display_height * scale_ratio
        encoded_size = _v2_encoded_image_size(board_image.size, (display_width, display_height))
        if board_image.size != encoded_size:
            board_image = board_image.resize(encoded_size, Image.Resampling.LANCZOS)
    elif target_image_width_px > 0:
        board_image = _resize_to_target_width(board_image, int(target_image_width_px))
        display_width = float(board_image.width)
        display_height = float(board_image.height)

    if dark_board and (text_priority or chalk_math_media):
        # Resizing can create fresh subpixel alpha dust. Normalize once at the
        # final encoded size so the EDB itself, not only the preview asset, is
        # guaranteed to be halo-free and single-tone. This also prevents math
        # figure strokes from drifting toward white after legacy-render repair.
        board_image = _finalize_text_cutout(board_image, chalk_color=chalk_color)

    if (
        dark_board
        and processing_step != PROCESSING_STEP_RECONSTRUCT
        and entry.preserve_media_regions
        and not chalk_math_media
    ):
        # Text-priority exports rebuild and normalize the cutout at final size.
        # Restore media last so photos, table shading, legends, and colors can
        # never be collapsed back into the single chalk tone.
        board_image = _apply_selective_media_preservation(
            board_image,
            crop_image,
            entry.preserve_media_regions,
        )

    jpeg_quality = (
        TEXT_PRIORITY_IMAGE_RECORD_JPEG_QUALITY
        if (
            text_priority
            or continuous_flow
            or str(entry.subject or "").strip().lower() in HIGH_FIDELITY_IMAGE_SUBJECTS
        )
        else DEFAULT_IMAGE_RECORD_JPEG_QUALITY
    )
    if (
        reuse_raw_page_bytes
        and source_format in {"PNG", "JPEG", "JPG"}
        and board_image.size == (source_width, source_height)
    ):
        image_bytes = crop_path.read_bytes()
        image_format = "JPEG" if source_format == "JPG" else source_format
    else:
        image_bytes, image_format = _encode_image_bytes(board_image, quality=jpeg_quality)
    if crop_format == CROP_FORMAT_V2:
        secondary_bytes = build_tight_crop_image_bytes(
            image_bytes, format_hint=image_format, quality=88
        )
    else:
        secondary_bytes = build_v1_secondary_image_bytes(
            image_bytes,
            page_as_is=_entry_uses_continuous_page_flow(entry),
            format_hint=image_format,
        )
    return _ImageOnlyRecordImage(
        crop_path=crop_path,
        board_render_path=board_render_path,
        image_bytes=image_bytes,
        secondary_bytes=secondary_bytes,
        width_px=int(board_image.width),
        height_px=int(board_image.height),
        scale_ratio=scale_ratio,
        display_width_px=display_width,
        display_height_px=display_height,
        source_width_px=source_width,
        source_height_px=source_height,
        resolution_policy=(
            "source-preserving"
            if board_image.size == (source_width, source_height)
            else "adaptive"
        ),
    )


def _build_image_only_record_images(
    placements: list[Any],
    entries_by_problem_id: dict[str, ProblemEntry],
    *,
    source_layout: tuple[float, float, float] | None,
    dark_board: bool,
    board_theme: str,
    crop_format: str,
    target_image_width_px: float,
    generate_records: bool = True,
) -> list[_ImageOnlyRecordImage]:
    def _build(placement: Any) -> _ImageOnlyRecordImage:
        entry = entries_by_problem_id[placement.problem_id]
        continuous_flow = _entry_uses_continuous_page_flow(entry) and source_layout is None
        return _build_image_only_record_image(
            placement,
            entry,
            dark_board=dark_board,
            board_theme=board_theme,
            crop_format=crop_format,
            target_image_width_px=target_image_width_px,
            continuous_flow=continuous_flow,
            generate_record=generate_records,
        )

    worker_count = _resolve_image_record_worker_count(len(placements))
    if worker_count <= 1:
        return [_build(placement) for placement in placements]
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        return list(executor.map(_build, placements))


def build_image_only_records(
    problem_entries: list[ProblemEntry],
    template: LayoutTemplate,
    *,
    dark_board: bool = True,
    board_theme: str = DEFAULT_BOARD_THEME,
    crop_format: str = DEFAULT_CROP_FORMAT,
    reserve_rendered_layout_height: bool = True,
    generate_records: bool = True,
    expand_board_capacity: bool = True,
) -> tuple[list[bytes], list[dict[str, object]]]:
    _validate_problem_entry_ids(problem_entries)
    layout_heights = (
        _image_only_layout_heights_by_problem_id(
            problem_entries,
            template,
            crop_format=crop_format,
        )
        if reserve_rendered_layout_height
        else None
    )
    placements = place_problems(
        placement_inputs(problem_entries, actual_height_pages_by_problem_id=layout_heights),
        template=template,
    )
    # Standalone/preview builds may grow the logical board to fit all content.
    # ClassIn-limited part builds must keep this disabled: their EDB header is
    # fixed to CLASSIN_MAX_BOARD_PAGE_COUNT, and image y/height hints must be
    # normalized with that exact same page count or ClassIn distorts images.
    if expand_board_capacity:
        _ensure_template_board_capacity(template, placements)
    entries_by_problem_id = {entry.problem_id: entry for entry in problem_entries}
    if crop_format == CROP_FORMAT_V2:
        # V2_TARGET_IMAGE_WIDTH_PX is a logical ClassIn layout width. The
        # encoded bitmap stays oversampled so small glyph strokes survive;
        # width/height hints remain based on the logical display size.
        target_image_width_px = float(V2_TARGET_IMAGE_WIDTH_PX)
        available_width_px = target_image_width_px
    else:
        target_image_width_px = 0.0  # 0 means "do not resize"
        available_width_px = _v1_default_display_width_px(template)

    records: list[bytes] = []
    placement_summaries: list[dict[str, object]] = []
    next_record_id = 0
    preserve_source_layout = bool(template.metadata.get("preserve_source_layout"))
    source_layout = (
        _v1_source_layout_transform(problem_entries)
        if crop_format == CROP_FORMAT_V1 and preserve_source_layout
        else None
    )
    image_payloads = _build_image_only_record_images(
        placements,
        entries_by_problem_id,
        source_layout=source_layout,
        dark_board=dark_board,
        board_theme=board_theme,
        crop_format=crop_format,
        target_image_width_px=target_image_width_px,
        generate_records=generate_records,
    )
    continuous_cursor_pages: float | None = None

    for placement, image_payload in zip(placements, image_payloads):
        entry = entries_by_problem_id[placement.problem_id]
        continuous_flow = _entry_uses_continuous_page_flow(entry) and source_layout is None
        processing_step = _normalize_processing_step(
            entry.processing_step or placement.metadata.get("processing_step")
        )

        if source_layout is not None:
            layout_left, layout_top, layout_scale = source_layout
            scale_ratio = layout_scale
            rendered_width_px = entry.bounds.width * layout_scale
            rendered_height_px = entry.bounds.height * layout_scale
            x_px = V1_LAYOUT_MARGIN_X_PX + (entry.bounds.left - layout_left) * layout_scale
            y_px = V1_LAYOUT_MARGIN_Y_PX + (entry.bounds.top - layout_top) * layout_scale
            width_hint = normalize_width_px(rendered_width_px)
            height_hint = normalize_height_px(rendered_height_px, page_count_hint=template.board_page_count)
        elif crop_format == CROP_FORMAT_V2:
            scale_ratio = float(image_payload.scale_ratio if image_payload.scale_ratio is not None else 1.0)
            display_width_px = float(image_payload.display_width_px or image_payload.width_px)
            display_height_px = float(image_payload.display_height_px or image_payload.height_px)
            width_hint = normalize_width_px(display_width_px)
            height_hint = normalize_height_px(
                display_height_px, page_count_hint=template.board_page_count
            )
            rendered_width_px = display_width_px
            rendered_height_px = display_height_px
            x_px = _problem_origin_x_px(entry, rendered_width_px)
            y_px = _problem_origin_y_px(entry, placement, rendered_height_px)
        else:
            base_rendered_width_px = available_width_px
            base_rendered_height_px = available_width_px * (
                float(image_payload.height_px) / max(float(image_payload.width_px), 1.0)
            )
            scale_ratio = _problem_scale_ratio(
                entry,
                placement,
                base_rendered_width_px,
                base_rendered_height_px,
                ignore_height_limit=continuous_flow,
            )
            height_px = base_rendered_height_px * scale_ratio
            width_hint = normalize_width_px(available_width_px * scale_ratio)
            height_hint = normalize_height_px(height_px, page_count_hint=template.board_page_count)
            rendered_width_px = available_width_px * scale_ratio
            rendered_height_px = height_px
            x_px = _problem_origin_x_px(entry, rendered_width_px)
            y_px = _problem_origin_y_px(entry, placement, rendered_height_px)

        if continuous_flow:
            start_y_pages = continuous_cursor_pages if continuous_cursor_pages is not None else placement.start_y_pages
            actual_height_pages = rendered_height_px / max(scale_ratio, 0.001) / CANVAS_WIDTH
            rendered_height_pages = rendered_height_px / CANVAS_WIDTH
            actual_bottom_y_pages = start_y_pages + actual_height_pages
            snapped_next_start_y_pages = (
                start_y_pages
                + rendered_height_pages
                + CONTINUOUS_RECORD_GAP_PX / CANVAS_WIDTH
            )
            overflow_amount_pages = max(0.0, rendered_height_pages - template.base_slot_height_pages)
            slot_span_count = max(
                1,
                math.ceil((snapped_next_start_y_pages - start_y_pages - 1e-9) / template.base_slot_height_pages),
            )
            x_px = _problem_origin_x_px(entry, rendered_width_px)
            y_px = start_y_pages * CANVAS_WIDTH + TOP_PADDING_PX
            continuous_cursor_pages = snapped_next_start_y_pages
        else:
            start_y_pages = placement.start_y_pages
            actual_height_pages = placement.actual_content_height_pages
            actual_bottom_y_pages = placement.actual_bottom_y_pages
            snapped_next_start_y_pages = placement.snapped_next_start_y_pages
            overflow_amount_pages = placement.overflow_amount_pages
            slot_span_count = placement.slot_span_count
            continuous_cursor_pages = None

        parent_record_id = next_record_id
        if generate_records:
            records.append(
                build_image_record(
                    ImageRecordSpec(
                        record_id=parent_record_id,
                        image_primary=image_payload.image_bytes,
                        image_secondary=image_payload.secondary_bytes,
                        x=normalize_x_px(x_px),
                        y=normalize_y_px(y_px, page_count_hint=template.board_page_count),
                        width_hint=width_hint,
                        height_hint=height_hint,
                    )
                )
            )
        next_record_id += 1
        image_record_count = 1

        placement_summaries.append(
            {
                "problem_id": placement.problem_id,
                "title": placement.metadata["title"],
                "problem_number": placement.metadata.get("problem_number"),
                "subject": str(placement.subject),
                "crop_path": str(image_payload.crop_path),
                "board_render_path": str(image_payload.board_render_path),
                "preserve_media_regions": list(entry.preserve_media_regions),
                "source_page_id": placement.metadata["source_page_id"],
                "source_path": placement.metadata["source_path"],
                "start_y_pages": round(start_y_pages, 6),
                "actual_content_height_pages": round(actual_height_pages, 6),
                "actual_bottom_y_pages": round(actual_bottom_y_pages, 6),
                "snapped_next_start_y_pages": round(snapped_next_start_y_pages, 6),
                "record_top_y_pages": round(y_px / CANVAS_WIDTH, 6),
                "record_bottom_y_pages": round((y_px + rendered_height_px) / CANVAS_WIDTH, 6),
                "record_gap_px": CONTINUOUS_RECORD_GAP_PX if continuous_flow else 0.0,
                "rendered_height_pages": round(rendered_height_px / CANVAS_WIDTH, 6),
                "overflow_allowed": placement.overflow_allowed,
                "overflow_amount_pages": round(overflow_amount_pages, 6),
                "overflow_violation": overflow_amount_pages > 0 and not placement.overflow_allowed,
                "slot_span_count": slot_span_count,
                "bbox": placement.metadata["bbox"],
                "source_segments": list(placement.metadata.get("source_segments") or []),
                "risk_flags": list(placement.metadata.get("risk_flags") or []),
                "record_mode": "image-only",
                "step": processing_step,
                "processing_step": processing_step,
                "text_record_count": 0,
                "image_record_count": image_record_count,
                "board_theme": _resolve_board_theme(board_theme),
                "crop_format": crop_format,
                "image_pixel_width": int(image_payload.width_px),
                "image_pixel_height": int(image_payload.height_px),
                "source_image_pixel_width": int(image_payload.source_width_px or image_payload.width_px),
                "source_image_pixel_height": int(image_payload.source_height_px or image_payload.height_px),
                "image_resolution_policy": image_payload.resolution_policy,
                "rendered_width_px": float(rendered_width_px),
                "rendered_height_px": float(rendered_height_px),
                "recordPageCountHint": int(template.board_page_count),
                "record_page_count_hint": int(template.board_page_count),
                "placement_x_ratio": float(_clamp_placement_x_ratio(entry.placement_x_ratio) or 0.0),
                "placement_y_ratio": float(_clamp_placement_y_ratio(entry.placement_y_ratio) or 0.0),
                "placement_scale_ratio": float(scale_ratio),
                "preserve_legacy_placement_scale": _entry_preserves_legacy_placement_scale(entry),
            }
        )

    return records, placement_summaries


def build_mixed_records(
    problem_entries: list[ProblemEntry],
    template: LayoutTemplate,
    *,
    output_dir: Path,
    text_confidence_threshold: float,
    dark_board: bool = True,
    board_theme: str = DEFAULT_BOARD_THEME,
) -> tuple[list[bytes], list[dict[str, object]]]:
    _validate_problem_entry_ids(problem_entries)
    placements = place_problems(placement_inputs(problem_entries), template=template)
    _ensure_template_board_capacity(template, placements)
    entries_by_problem_id = {entry.problem_id: entry for entry in problem_entries}
    available_width_px = CANVAS_HEIGHT * template.fixed_left_zone_ratio - LEFT_MARGIN_PX - RIGHT_PADDING_PX
    block_crop_dir = output_dir / "block_crops"
    block_crop_dir.mkdir(parents=True, exist_ok=True)
    chalk_color = _resolve_chalk_color(board_theme)

    records: list[bytes] = []
    placement_summaries: list[dict[str, object]] = []
    next_record_id = 0

    for placement in placements:
        entry = entries_by_problem_id[placement.problem_id]
        scale_ratio = _problem_scale_ratio(
            entry,
            placement,
            available_width_px,
            placement.actual_content_height_pages * CANVAS_WIDTH,
        )
        scaled_available_width_px = available_width_px * scale_ratio
        rendered_problem_height_px = placement.actual_content_height_pages * CANVAS_WIDTH * scale_ratio
        scale = scaled_available_width_px / max(entry.bounds.width, 1.0)
        problem_origin_x_px = _problem_origin_x_px(entry, scaled_available_width_px)
        problem_origin_y_px = _problem_origin_y_px(
            entry,
            placement,
            rendered_problem_height_px,
        )
        block_summaries: list[dict[str, object]] = []
        text_record_count = 0
        image_record_count = 0

        for block in entry.blocks:
            x_px = problem_origin_x_px + max(0.0, block.bbox.left - entry.bounds.left) * scale
            y_px = problem_origin_y_px + max(0.0, block.bbox.top - entry.bounds.top) * scale
            width_px = max(40.0, min(scaled_available_width_px, block.bbox.width * scale))
            height_px = max(22.0, block.bbox.height * scale)
            record_mode = choose_block_record_mode(block, text_confidence_threshold=text_confidence_threshold)

            if record_mode == "text":
                text_payload = normalize_text_payload(block.text)
                records.append(
                    build_text_record(
                        TextRecordSpec(
                            record_id=next_record_id,
                            text=text_payload,
                            x=normalize_x_px(x_px),
                            y=normalize_y_px(y_px, page_count_hint=template.board_page_count),
                            width_hint=normalize_width_px(width_px),
                            font_size=resolve_font_size(block, scale),
                        )
                    )
                )
                text_record_count += 1
            else:
                crop = entry.prepared_page.image.crop(
                    _integer_crop_rect_for_box(
                        block.bbox,
                        image_width=entry.prepared_page.image.width,
                        image_height=entry.prepared_page.image.height,
                    )
                )
                crop_name = f"p{len(placement_summaries) + 1:03d}_b{len(block_summaries) + 1:03d}_{hashlib.sha1((entry.problem_id + block.block_id).encode('utf-8', errors='ignore')).hexdigest()[:8]}.png"
                crop_path = block_crop_dir / crop_name
                crop.save(crop_path)  # Save original for UI/debugging
                board_crop = _extract_problem_cutout(crop, chalk_color=chalk_color) if dark_board else crop.convert("RGB")
                image_bytes, image_format = _encode_image_bytes(board_crop, quality=92)
                preview_bytes = build_preview_image_bytes(image_bytes, max_size=(768, 768), format_hint=image_format, quality=88)
                records.append(
                    build_image_record(
                        ImageRecordSpec(
                            record_id=next_record_id,
                            image_primary=image_bytes,
                            image_secondary=preview_bytes,
                            x=normalize_x_px(x_px),
                            y=normalize_y_px(y_px, page_count_hint=template.board_page_count),
                            width_hint=normalize_width_px(width_px),
                            height_hint=normalize_height_px(height_px, page_count_hint=template.board_page_count),
                        )
                    )
                )
                image_record_count += 1

            block_summaries.append(
                {
                    "block_id": block.block_id,
                    "block_type": str(block.block_type),
                    "record_mode": record_mode,
                    "text_present": bool(normalize_text_payload(block.text)),
                    "confidence": block.confidence,
                    "bbox": {
                        "left": block.bbox.left,
                        "top": block.bbox.top,
                        "width": block.bbox.width,
                        "height": block.bbox.height,
                    },
                }
            )
            next_record_id += 1

        if not block_summaries:
            fallback_image = Image.open(entry.crop_path).convert("RGB")
            board_fallback = Image.open(entry.board_render_path) if dark_board else fallback_image
            image_bytes, image_format = _encode_image_bytes(board_fallback, quality=92)
            preview_bytes = build_preview_image_bytes(image_bytes, max_size=(768, 768), format_hint=image_format, quality=88)
            records.append(
                build_image_record(
                    ImageRecordSpec(
                        record_id=next_record_id,
                        image_primary=image_bytes,
                        image_secondary=preview_bytes,
                        x=normalize_x_px(problem_origin_x_px),
                        y=normalize_y_px(problem_origin_y_px, page_count_hint=template.board_page_count),
                        width_hint=normalize_width_px(scaled_available_width_px),
                        height_hint=normalize_height_px(
                            rendered_problem_height_px,
                            page_count_hint=template.board_page_count,
                        ),
                    )
                )
            )
            image_record_count += 1
            next_record_id += 1

        placement_summaries.append(
            {
                "problem_id": placement.problem_id,
                "title": entry.title,
                "problem_number": entry.problem_number,
                "subject": str(entry.subject),
                "crop_path": str(entry.crop_path),
                "board_render_path": str(entry.board_render_path),
                "preserve_media_regions": list(entry.preserve_media_regions),
                "source_page_id": entry.source_page_id,
                "source_path": entry.source_path,
                "start_y_pages": placement.start_y_pages,
                "actual_content_height_pages": placement.actual_content_height_pages,
                "actual_bottom_y_pages": placement.actual_bottom_y_pages,
                "snapped_next_start_y_pages": placement.snapped_next_start_y_pages,
                "record_top_y_pages": round(problem_origin_y_px / CANVAS_WIDTH, 6),
                "record_bottom_y_pages": round((problem_origin_y_px + rendered_problem_height_px) / CANVAS_WIDTH, 6),
                "rendered_height_pages": round(rendered_problem_height_px / CANVAS_WIDTH, 6),
                "overflow_allowed": placement.overflow_allowed,
                "overflow_amount_pages": placement.overflow_amount_pages,
                "overflow_violation": placement.overflow_violation,
                "slot_span_count": placement.slot_span_count,
                "bbox": {
                    "left": entry.bounds.left,
                    "top": entry.bounds.top,
                    "width": entry.bounds.width,
                    "height": entry.bounds.height,
                },
                "source_segments": [dict(segment) for segment in entry.source_segments],
                "risk_flags": list(entry.risk_flags),
                "record_mode": "mixed",
                "text_record_count": text_record_count,
                "image_record_count": image_record_count,
                "board_theme": _resolve_board_theme(board_theme),
                "placement_x_ratio": float(_clamp_placement_x_ratio(entry.placement_x_ratio) or 0.0),
                "placement_y_ratio": float(_clamp_placement_y_ratio(entry.placement_y_ratio) or 0.0),
                "placement_scale_ratio": float(scale_ratio),
                "preserve_legacy_placement_scale": _entry_preserves_legacy_placement_scale(entry),
                "blocks": block_summaries,
            }
        )

    return records, placement_summaries


def build_records(
    problem_entries: list[ProblemEntry],
    template: LayoutTemplate,
    *,
    record_mode: str,
    output_dir: Path,
    text_confidence_threshold: float,
    dark_board: bool = True,
    board_theme: str = DEFAULT_BOARD_THEME,
    crop_format: str = DEFAULT_CROP_FORMAT,
    reserve_image_layout_height: bool = True,
    generate_records: bool = True,
    expand_board_capacity: bool = True,
) -> tuple[list[bytes], list[dict[str, object]], int]:
    if record_mode == "image-only":
        records, placement_summaries = build_image_only_records(
            problem_entries,
            template,
            dark_board=dark_board,
            board_theme=board_theme,
            crop_format=crop_format,
            reserve_rendered_layout_height=reserve_image_layout_height,
            generate_records=generate_records,
            expand_board_capacity=expand_board_capacity,
        )
        return records, placement_summaries, header_flag_for_crop_format(crop_format, mode="image")

    records, placement_summaries = build_mixed_records(
        problem_entries,
        template,
        output_dir=output_dir,
        text_confidence_threshold=text_confidence_threshold,
        dark_board=dark_board,
        board_theme=board_theme,
    )
    has_images = any(item["image_record_count"] for item in placement_summaries)
    if has_images:
        header_flag = header_flag_for_crop_format(crop_format, mode="image")
    else:
        header_flag = 3
    return records, placement_summaries, header_flag


def write_classin_limited_edb_files(
    problem_entries: list[ProblemEntry],
    template: LayoutTemplate,
    output_dir: Path,
    edb_name: str,
    *,
    record_mode: str,
    text_confidence_threshold: float,
    dark_board: bool,
    board_theme: str,
    crop_format: str,
    existing_records: list[bytes] | None = None,
    existing_placements: list[dict[str, object]] | None = None,
    existing_header_flag: int | None = None,
) -> list[dict[str, Any]]:
    chunks = split_problem_entries_for_classin_page_limit(problem_entries, template)
    if not chunks:
        return []
    rendered_chunks: list[dict[str, Any]] = []

    def build_rendered_chunk(
        chunk_entries: list[ProblemEntry],
        *,
        allow_existing: bool = False,
    ) -> dict[str, Any]:
        render_template = template_with_board_page_count(template, CLASSIN_MAX_BOARD_PAGE_COUNT)
        can_reuse_existing = (
            allow_existing
            and len(chunk_entries) == len(problem_entries)
            and existing_records is not None
            and existing_placements is not None
            and existing_header_flag is not None
            and int(template.board_page_count) == CLASSIN_MAX_BOARD_PAGE_COUNT
        )
        if can_reuse_existing:
            part_records = list(existing_records or [])
            part_placements = [dict(placement) for placement in (existing_placements or [])]
            part_header_flag = int(existing_header_flag or 0)
        else:
            part_records, part_placements, part_header_flag = build_records(
                chunk_entries,
                render_template,
                record_mode=record_mode,
                output_dir=output_dir,
                text_confidence_threshold=text_confidence_threshold,
                dark_board=dark_board,
                board_theme=board_theme,
                crop_format=crop_format,
                # A ClassIn part must reserve the same display height used by
                # its final image record.  In particular, V1 records render at
                # the legacy 540px display width while ProblemEntry estimates
                # may have been computed for the narrower one-third zone.
                reserve_image_layout_height=True,
                expand_board_capacity=False,
            )
        _validate_record_page_count_hints(
            part_placements,
            expected_page_count=CLASSIN_MAX_BOARD_PAGE_COUNT,
        )
        if not render_template.metadata.get("preserve_source_layout"):
            _validate_sequential_record_placements(part_placements)
        return {
            "entries": list(chunk_entries),
            "records": part_records,
            "placements": part_placements,
            "header_flag": part_header_flag,
            "flow_end_pages": _placement_summaries_flow_end_pages(part_placements),
            "page_count_hint": int(render_template.board_page_count),
        }

    def render_chunk(
        chunk_entries: list[ProblemEntry],
        *,
        allow_existing: bool = False,
        from_recursive_split: bool = False,
    ) -> None:
        rendered_chunk = build_rendered_chunk(chunk_entries, allow_existing=allow_existing)
        part_placements = list(rendered_chunk["placements"])
        flow_end_pages = float(rendered_chunk["flow_end_pages"])
        if flow_end_pages > CLASSIN_MAX_BOARD_PAGE_COUNT + 1e-6 and len(chunk_entries) > 1:
            split_index = _first_placement_over_page_limit(part_placements, CLASSIN_MAX_BOARD_PAGE_COUNT)
            if split_index is None or split_index <= 0:
                split_index = 1
            render_chunk(chunk_entries[:split_index], from_recursive_split=True)
            render_chunk(chunk_entries[split_index:], from_recursive_split=True)
            return
        if flow_end_pages > CLASSIN_MAX_BOARD_PAGE_COUNT + 1e-6:
            problem_id = chunk_entries[0].problem_id if chunk_entries else "unknown"
            raise ValueError(
                f"Problem '{problem_id}' exceeds ClassIn's {CLASSIN_MAX_BOARD_PAGE_COUNT}-page limit "
                f"after rendering ({flow_end_pages:.3f} pages); split the source before publishing"
            )

        rendered_chunk["from_recursive_split"] = from_recursive_split
        rendered_chunks.append(rendered_chunk)

    for chunk_entries in chunks:
        render_chunk(chunk_entries, allow_existing=len(chunks) == 1)

    compacted_chunks: list[dict[str, Any]] = []
    for rendered_chunk in rendered_chunks:
        can_compact_with_previous = bool(
            compacted_chunks
            and (
                compacted_chunks[-1].get("from_recursive_split")
                or rendered_chunk.get("from_recursive_split")
            )
        )
        if can_compact_with_previous:
            candidate_entries = [
                *list(compacted_chunks[-1]["entries"]),
                *list(rendered_chunk["entries"]),
            ]
            candidate = build_rendered_chunk(candidate_entries)
            if float(candidate["flow_end_pages"]) <= CLASSIN_MAX_BOARD_PAGE_COUNT + 1e-6:
                candidate["from_recursive_split"] = bool(
                    compacted_chunks[-1].get("from_recursive_split")
                    or rendered_chunk.get("from_recursive_split")
                )
                compacted_chunks[-1] = candidate
                continue
        compacted_chunks.append(rendered_chunk)
    rendered_chunks = compacted_chunks

    part_count = len(rendered_chunks)
    parts: list[dict[str, Any]] = []
    for part_index, rendered_chunk in enumerate(rendered_chunks):
        chunk_entries = list(rendered_chunk["entries"])
        part_records = list(rendered_chunk["records"])
        part_placements = list(rendered_chunk["placements"])
        part_header_flag = int(rendered_chunk["header_flag"])
        part_name = edb_part_file_name(edb_name, part_index, part_count)
        part_path = output_dir / part_name
        write_edb(
            part_path,
            build_edb(
                part_records,
                header_flag=part_header_flag,
                version=version_string_for_crop_format(crop_format),
                page_count_hint=CLASSIN_MAX_BOARD_PAGE_COUNT,
            ),
        )
        problem_ids = [entry.problem_id for entry in chunk_entries]
        parts.append(
            {
                "partIndex": part_index + 1,
                "part_index": part_index + 1,
                "partCount": part_count,
                "part_count": part_count,
                "edbFileName": part_path.name,
                "edb_file_name": part_path.name,
                "edbPath": str(part_path.resolve()),
                "edb_path": str(part_path.resolve()),
                "recordCount": len(part_records),
                "record_count": len(part_records),
                "placementCount": len(part_placements),
                "placement_count": len(part_placements),
                "pageCountHint": CLASSIN_MAX_BOARD_PAGE_COUNT,
                "page_count_hint": CLASSIN_MAX_BOARD_PAGE_COUNT,
                "flowEndPages": float(rendered_chunk.get("flow_end_pages") or 0.0),
                "flow_end_pages": float(rendered_chunk.get("flow_end_pages") or 0.0),
                "problemIds": problem_ids,
                "problem_ids": problem_ids,
                "placements": part_placements,
            }
        )
    return parts


def annotate_ui_session_with_edb_part_metadata(ui_session: dict[str, Any], edb_parts: list[dict[str, Any]]) -> None:
    if not isinstance(ui_session, dict) or not edb_parts:
        return
    part_by_problem_id: dict[str, dict[str, Any]] = {}
    placement_by_problem_id: dict[str, dict[str, Any]] = {}
    for part in edb_parts:
        if not isinstance(part, dict):
            continue
        problem_ids = part.get("problemIds") if isinstance(part.get("problemIds"), list) else part.get("problem_ids")
        for problem_id in problem_ids or []:
            part_by_problem_id[str(problem_id)] = part
        placements = part.get("placements") if isinstance(part.get("placements"), list) else []
        for placement in placements:
            if not isinstance(placement, dict):
                continue
            problem_id = str(placement.get("problem_id") or placement.get("problemId") or "")
            if problem_id:
                placement_by_problem_id[problem_id] = placement

    for problem in ui_session.get("problems", []) or []:
        if not isinstance(problem, dict):
            continue
        problem_id = str(problem.get("id") or problem.get("problem_id") or "")
        part = part_by_problem_id.get(problem_id)
        if not part:
            continue
        part_index = int(part.get("partIndex") or part.get("part_index") or 1)
        problem["edbPartIndex"] = part_index
        problem["edb_part_index"] = part_index
        problem["edbPartFileName"] = part.get("edbFileName") or part.get("edb_file_name") or ""
        problem["edb_part_file_name"] = problem["edbPartFileName"]
        placement = placement_by_problem_id.get(problem_id)
        if placement:
            problem["edbLocalStartYPages"] = placement.get("start_y_pages")
            problem["edb_local_start_y_pages"] = placement.get("start_y_pages")
            problem["edbLocalBottomYPages"] = placement.get("record_bottom_y_pages") or placement.get("actual_bottom_y_pages")
            problem["edb_local_bottom_y_pages"] = placement.get("record_bottom_y_pages") or placement.get("actual_bottom_y_pages")
            problem["edbLocalRecordBottomYPages"] = placement.get("record_bottom_y_pages")
            problem["edb_local_record_bottom_y_pages"] = placement.get("record_bottom_y_pages")


def write_ui_prototype_data(output_path: Path, placements: list[dict[str, object]]) -> None:
    payload = {
        "problems": [
            {
                "id": item["problem_id"],
                "title": item["title"],
                "subject": item["subject"],
                "imagePath": Path(item["crop_path"]).resolve().as_uri(),
                "boardRenderPath": Path(item["board_render_path"]).resolve().as_uri() if item.get("board_render_path") else None,
                "actualHeightPages": item["actual_content_height_pages"],
                "overflowAllowed": item["overflow_allowed"],
                "readingHeavy": item["overflow_allowed"],
                "placementXRatio": float(item.get("placement_x_ratio") or 0.0),
                "placementYRatio": float(item.get("placement_y_ratio") or 0.0),
                "placementScaleRatio": float(item.get("placement_scale_ratio") or 1.0),
                "preserveLegacyPlacementScale": bool(item.get("preserve_legacy_placement_scale")),
            }
            for item in placements
        ]
    }
    output_path.write_text(
        "window.PROTOTYPE_DATA = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )


def resolve_legacy_prototype_data_path(raw_path: str | Path) -> Path:
    output_path = Path(raw_path)
    resolved_output = output_path.expanduser().resolve()
    project_ui_root = (Path(__file__).resolve().parent / "ui_prototype").resolve()
    if resolved_output == project_ui_root or project_ui_root in resolved_output.parents:
        raise ValueError(
            "--prototype-data-out must not write into project ui_prototype; "
            "write legacy prototype data under --output-dir instead"
        )
    return output_path


def build_placement_summary(placements: list[dict[str, object]]) -> dict[str, object]:
    if not placements:
        return {
            "problem_count": 0,
            "overflow_count": 0,
            "overflow_violation_count": 0,
            "max_bottom_y_pages": 0.0,
            "text_record_count": 0,
            "image_record_count": 0,
        }
    return {
        "problem_count": len(placements),
        "overflow_count": sum(1 for item in placements if float(item["overflow_amount_pages"]) > 0),
        "overflow_violation_count": sum(1 for item in placements if bool(item["overflow_violation"])),
        "max_bottom_y_pages": max(float(item["actual_bottom_y_pages"]) for item in placements),
        "text_record_count": sum(int(item.get("text_record_count", 0)) for item in placements),
        "image_record_count": sum(int(item.get("image_record_count", 0)) for item in placements),
    }


def configure_problem_entries_for_export(
    problem_entries: Sequence[ProblemEntry],
    *,
    passage_problem_ids: Iterable[str] = (),
    passages_only: bool = False,
    processing_step: str | None = None,
    full_width: bool = False,
) -> list[ProblemEntry]:
    selected = list(problem_entries)
    if passages_only:
        allowed_ids = {str(problem_id) for problem_id in passage_problem_ids if str(problem_id)}
        selected = [entry for entry in selected if entry.problem_id in allowed_ids]
        if not selected:
            raise ValueError("--passages-only did not find any passage fragments")

    resolved_step: str | None = None
    if processing_step is not None and str(processing_step).strip():
        resolved_step = str(processing_step).strip().lower()
        if resolved_step not in PROCESSING_STEPS:
            raise ValueError(f"Unsupported processing step: {processing_step}")

    # A passage-only export has one clear safe default: preserve the source
    # glyphs as a transparent S2 image and place each range on a continuous,
    # single-column fit-width flow. Requiring two extra flags made it easy to
    # produce a technically valid but narrow/raw passage export.
    effective_full_width = full_width or passages_only
    effective_step = resolved_step or (PROCESSING_STEP_CHALK if passages_only else None)

    for entry in selected:
        if effective_step is not None:
            entry.processing_step = effective_step
        if effective_full_width:
            # The continuous page flow is the established ClassIn fit-width
            # path and permits the 3x V1 width used by existing full-width EDBs.
            # This changes placement only; the extracted passage bounds remain
            # intact and the S2 transparent board render stays the image source.
            entry.input_intent = "page-as-is"
            entry.placement_x_ratio = 0.0
            entry.placement_scale_ratio = PLACEMENT_FIT_WIDTH_SCALE_MAX
    return selected


def resolve_export_record_mode(record_mode: str | None, *, passages_only: bool) -> str:
    """Choose the record strategy without splitting a stitched passage by default."""
    requested = str(record_mode or "").strip().lower()
    if not requested:
        return "image-only" if passages_only else "mixed"
    if requested not in {"mixed", "image-only"}:
        raise ValueError(f"Unsupported record mode: {record_mode}")
    return requested


def run_problem_export(
    source: str | Path | Sequence[str | Path],
    *,
    output_dir: str | Path = "mvp_export_question",
    subject_name: str = "unknown",
    ocr: str = "auto",
    pdf_dpi: int = 200,
    detect_perspective: bool = False,
    skip_deskew: bool = False,
    skip_crop: bool = False,
    max_dimension: int | None = None,
    export_edb: bool = True,
    edb_name: str = "mvp_board.edb",
    record_mode: str = "mixed",
    text_confidence_threshold: float = 0.78,
    dark_board: bool = True,
    board_theme: str = DEFAULT_BOARD_THEME,
    sync_ui: bool = False,
    crop_format: str = DEFAULT_CROP_FORMAT,
    input_intent: str = "auto",
    page_tile_mode: str = "off",
    content_target: str = "all",
    input_notes: str | None = None,
    processing_step: str | None = None,
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
    run_started_at = time.perf_counter()
    timing_ms: dict[str, int] = {}
    if isinstance(source, (str, Path)):
        source_paths = [Path(source).resolve()]
    else:
        source_paths = [Path(path).resolve() for path in source]
    if not source_paths:
        raise ValueError("At least one source path is required")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    subject = resolve_subject(subject_name)
    resolved_input_intent = _normalize_input_intent(input_intent)
    resolved_content_target = _normalize_content_target(content_target)
    resolved_board_theme = _resolve_board_theme(board_theme)
    template = LayoutTemplate(
        name="academy-default",
        base_slot_height_pages=ONE_PROBLEM_SLOT_HEIGHT_PAGES,
        metadata={
            "placement_mode": "continuous-page-as-is"
            if resolved_input_intent == "page-as-is"
            else "one-problem-per-page"
        },
    )
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
    global_ocr_worker_limit = resolve_global_ocr_worker_limit(
        ocr_mode=ocr,
        ai_config=page_ai_config,
    )
    shared_ocr_semaphore = threading.BoundedSemaphore(global_ocr_worker_limit)

    def _build_source_pages(source_path: Path) -> tuple[list[PreparedPage], list[PageModel]]:
        return build_pages(
            source_path,
            subject=subject,
            ocr_mode=ocr,
            ai_fallback_config=ai_fallback_config,
            pdf_dpi=pdf_dpi,
            detect_perspective=detect_perspective,
            deskew=not skip_deskew,
            crop_margins=not skip_crop,
            max_dimension=max_dimension,
            page_tile_mode=page_tile_mode,
            input_intent=resolved_input_intent,
            ocr_semaphore=shared_ocr_semaphore,
            global_ocr_worker_limit=global_ocr_worker_limit,
        )

    source_worker_count = resolve_source_build_worker_count(
        len(source_paths),
        input_intent=resolved_input_intent,
        ocr_mode=ocr,
        ai_fallback_config=ai_fallback_config,
    )
    source_build_started_at = time.perf_counter()
    if source_worker_count <= 1:
        source_results = [_build_source_pages(source_path) for source_path in source_paths]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=source_worker_count) as executor:
            source_results = list(executor.map(_build_source_pages, source_paths))
    timing_ms["source_build"] = _elapsed_ms(source_build_started_at)

    prepared_pages: list[PreparedPage] = []
    pages: list[PageModel] = []
    for prepared, page_models in source_results:
        prepared_pages.extend(prepared)
        pages.extend(page_models)

    if resolved_input_intent == "single-problem":
        pages = _force_single_problem_per_page(pages, input_intent=resolved_input_intent)

    save_pages_json(pages, out_dir / "pages.json")
    ai_summary = _summarize_ai_fallback_usage(pages, ai_fallback_config)
    ocr_summary = _summarize_ocr_usage(pages)
    token_usage = aggregate_token_usage(
        [
            ocr_summary.get("token_usage"),
            ai_summary.get("token_usage") if isinstance(ai_summary, dict) else None,
        ]
    )
    ai_cost_summary = summarize_ai_cost(
        _combined_ai_usage_events(ocr_summary, ai_summary)
    )
    if resolved_input_intent != "page-as-is" and ocr_summary["no_ocr_fallback_active"]:
        print(
            "[run_problem_export] WARNING: OCR resolved to 'none' for every block - "
            "problem-number detection will be disabled and each detected band "
            "will become its own pseudo-problem. Set GEMINI_API_KEY (or pass "
            "ocr='gemini') to enable Gemini OCR.",
            flush=True,
        )
    problem_assets_started_at = time.perf_counter()
    problem_entries = build_problem_entries(
        prepared_pages,
        pages,
        out_dir,
        template,
        board_theme=resolved_board_theme,
        content_target=resolved_content_target,
    )
    if not problem_entries and resolved_content_target == "shared-passages":
        raise ValueError("여러 문항이 함께 쓰는 공통 지문을 찾지 못했습니다")
    if not problem_entries and resolved_content_target == "questions":
        raise ValueError("공통 지문을 제외한 문항을 찾지 못했습니다")
    problem_entries = configure_problem_entries_for_export(
        problem_entries,
        passage_problem_ids=(
            {entry.problem_id for entry in problem_entries}
            if resolved_content_target == "shared-passages"
            else ()
        ),
        passages_only=resolved_content_target == "shared-passages",
        processing_step=processing_step,
    )
    timing_ms["problem_assets"] = _elapsed_ms(problem_assets_started_at)
    save_pages_json(pages, out_dir / "pages.json")
    # Match ClassIn's observed publish behaviour: page_count_hint scales with the
    # number of problems on the board so the logical canvas always covers the
    # actual content height. Real published EDBs use ~2x the record count
    # (e.g. 44 problems -> pages_hint=88); keep 50 as the floor for short boards.
    template.board_page_count = max(50, len(problem_entries) * 2)
    resolved_crop_format = crop_format if crop_format in (CROP_FORMAT_V1, CROP_FORMAT_V2) else DEFAULT_CROP_FORMAT
    records_started_at = time.perf_counter()
    records, placements, header_flag = build_records(
        problem_entries,
        template,
        record_mode=record_mode,
        output_dir=out_dir,
        text_confidence_threshold=text_confidence_threshold,
        dark_board=dark_board,
        board_theme=resolved_board_theme,
        crop_format=resolved_crop_format,
        generate_records=export_edb,
    )
    timing_ms["records"] = _elapsed_ms(records_started_at)
    timing_ms.update(_summarize_page_timing_ms(pages))

    summary = {
        "source_paths": [str(path) for path in source_paths],
        "output_dir": str(out_dir.resolve()),
        "pages_json_path": str((out_dir / "pages.json").resolve()),
        "problem_crop_dir": str((out_dir / "problem_crops").resolve()),
        "board_render_dir": str((out_dir / "board_renders").resolve()),
        "block_crop_dir": str((out_dir / "block_crops").resolve()),
        "record_count": len(records),
        "record_mode": record_mode,
        "dark_board": dark_board,
        "board_theme": resolved_board_theme,
        "crop_format": resolved_crop_format,
        "header_flag": header_flag,
        "text_confidence_threshold": text_confidence_threshold,
        "ai_fallback": ai_fallback_config,
        "ai_summary": ai_summary,
        "placement_summary": build_placement_summary(placements),
        "placements": placements,
        "ocr_backend_requested": ocr,
        "ocr_summary": ocr_summary,
        "token_usage": token_usage,
        "ai_cost_summary": ai_cost_summary,
        "input_intent": resolved_input_intent,
        "processing_step_override": processing_step,
        "page_tile_mode": _normalize_page_tile_mode(page_tile_mode),
        "page_tile_count": sum(
            1 for prepared_page in prepared_pages if prepared_page.metadata.get("page_tile_split")
        ),
        "content_target": resolved_content_target,
        "timing_ms": dict(timing_ms),
    }

    placements_path = out_dir / "placements.json"
    placements_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    edb_path: Path | None = None
    edb_parts: list[dict[str, Any]] = []
    if export_edb:
        edb_write_started_at = time.perf_counter()
        edb_parts = write_classin_limited_edb_files(
            problem_entries,
            template,
            out_dir,
            edb_name,
            record_mode=record_mode,
            text_confidence_threshold=text_confidence_threshold,
            dark_board=dark_board,
            board_theme=resolved_board_theme,
            crop_format=resolved_crop_format,
            existing_records=records,
            existing_placements=placements,
            existing_header_flag=header_flag,
        )
        edb_path = Path(str(edb_parts[0]["edbPath"])) if edb_parts else None
        timing_ms["edb_write"] = _elapsed_ms(edb_write_started_at)
        summary["edb_path"] = str(edb_path.resolve()) if edb_path else None
        summary["edb_paths"] = [str(Path(str(part["edbPath"])).resolve()) for part in edb_parts]
        summary["edb_parts"] = edb_parts
        summary["edb_part_count"] = len(edb_parts)
        summary["edb_split"] = len(edb_parts) > 1
        summary["timing_ms"] = dict(timing_ms)
        placements_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    ui_session_started_at = time.perf_counter()
    ui_session = build_ui_session(
        prepared_pages,
        placements,
        out_dir,
        edb_path if export_edb else None,
        source_paths,
        record_mode=record_mode,
        ai_fallback_config=ai_fallback_config,
        ai_summary=ai_summary,
        ocr_summary=ocr_summary,
        token_usage=token_usage,
        ai_cost_summary=ai_cost_summary,
        pages=pages,
        template=template,
        input_intent=resolved_input_intent,
        input_notes=input_notes,
        board_theme=resolved_board_theme,
        crop_format=resolved_crop_format,
    )
    annotate_ui_session_with_edb_part_metadata(ui_session, edb_parts)
    ui_session["content_target"] = resolved_content_target
    ui_session["contentTarget"] = resolved_content_target
    timing_ms["ui_session"] = _elapsed_ms(ui_session_started_at)
    classin_handoff_path: Path | None = None
    classin_handoff_markdown_path: Path | None = None
    if edb_path is not None and edb_path.exists():
        classin_handoff_path, classin_handoff_markdown_path = write_classin_handoff_manifest(
            out_dir,
            source_paths=source_paths,
            edb_path=edb_path,
            edb_parts=edb_parts,
            ui_session=ui_session,
            summary=summary,
            template=template,
        )
        summary["classin_handoff_path"] = str(classin_handoff_path)
        summary["classin_handoff_markdown_path"] = str(classin_handoff_markdown_path)
        handoff_session_fields = _classin_handoff_session_fields(classin_handoff_path)
        summary.update(handoff_session_fields)
        placements_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        ui_session["classin_handoff_path"] = str(classin_handoff_path)
        ui_session["classinHandoffPath"] = str(classin_handoff_path)
        ui_session["classin_handoff_markdown_path"] = str(classin_handoff_markdown_path)
        ui_session["classinHandoffMarkdownPath"] = str(classin_handoff_markdown_path)
        ui_session["edb_parts"] = edb_parts
        ui_session["edbParts"] = edb_parts
        ui_session["edb_part_count"] = len(edb_parts)
        ui_session["edbPartCount"] = len(edb_parts)
        ui_session["edb_split"] = len(edb_parts) > 1
        ui_session["edbSplit"] = len(edb_parts) > 1
        ui_session.update(handoff_session_fields)
    timing_ms["total"] = _elapsed_ms(run_started_at)
    summary["timing_ms"] = dict(timing_ms)
    ui_session["timing_ms"] = dict(timing_ms)
    placements_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    ui_session_path, synced_ui_path = write_ui_session_bundle(out_dir, ui_session, sync_ui=sync_ui)

    return {
        "output_dir": out_dir.resolve(),
        "edb_path": edb_path.resolve() if edb_path and edb_path.exists() else None,
        "edb_paths": [Path(str(part["edbPath"])).resolve() for part in edb_parts],
        "edb_parts": edb_parts,
        "pages_json_path": (out_dir / "pages.json").resolve(),
        "placements_json_path": placements_path.resolve(),
        "classin_handoff_path": classin_handoff_path,
        "classin_handoff_markdown_path": classin_handoff_markdown_path,
        "ui_session": ui_session,
        "ui_session_path": ui_session_path.resolve(),
        "synced_ui_path": synced_ui_path.resolve() if synced_ui_path else None,
        "summary": summary,
        "ai_fallback": ai_fallback_config,
        "ai_summary": ai_summary,
        "input_intent": resolved_input_intent,
        "content_target": resolved_content_target,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a problem-board EDB from a source image or PDF.")
    parser.add_argument("source", help="Path to a PDF or image source")
    parser.add_argument("--output-dir", default="mvp_export", help="Directory for pipeline artifacts and EDB output")
    parser.add_argument("--subject", default="unknown", help="Subject hint: math, science, korean, english, social, unknown")
    parser.add_argument("--ocr", default="noop", help="OCR backend: noop, auto, paddleocr, tesseract")
    parser.add_argument("--pdf-dpi", type=int, default=200, help="PDF render DPI")
    parser.add_argument("--detect-perspective", action="store_true", help="Try perspective correction for photographed sources")
    parser.add_argument("--skip-deskew", action="store_true", help="Disable deskew")
    parser.add_argument("--skip-crop", action="store_true", help="Disable margin crop")
    parser.add_argument("--max-dimension", type=int, default=None, help="Resize long edge to this many pixels")
    parser.add_argument("--template-name", default="academy-default", help="Layout template name")
    parser.add_argument("--board-pages", type=int, default=50, help="Board page count hint")
    parser.add_argument("--slot-height", type=float, default=ONE_PROBLEM_SLOT_HEIGHT_PAGES, help="Base slot height in board pages")
    parser.add_argument(
        "--record-mode",
        choices=("mixed", "image-only"),
        default=None,
        help=(
            "Record generation strategy; defaults to image-only for --passages-only "
            "so stitched passages stay as one record, otherwise mixed"
        ),
    )
    parser.add_argument("--input-intent", choices=tuple(sorted(INPUT_INTENTS)), default="auto", help="How to treat uploaded source pages")
    parser.add_argument(
        "--passages-only",
        action="store_true",
        help=(
            "Export only passage-fragment records detected from set-problem ranges; "
            "defaults to S2 text-preserving, continuous fit-width placement"
        ),
    )
    parser.add_argument(
        "--processing-step",
        choices=tuple(sorted(PROCESSING_STEPS)),
        default="",
        help="Override exported problem processing step (for example s2 for transparent chalk cutouts)",
    )
    parser.add_argument(
        "--full-width",
        action="store_true",
        help="Use ClassIn continuous fit-width placement for the selected records",
    )
    parser.add_argument(
        "--crop-format",
        choices=(CROP_FORMAT_V1, CROP_FORMAT_V2),
        default=DEFAULT_CROP_FORMAT,
        help=(
            "EDB crop layout version. v2 (default) matches the current ClassIn "
            "crop sample (header_flag=0, ~301px image width, tight-cropped image_secondary, "
            "version 6.0.5.3913). v1 keeps the legacy wide layout (header_flag=4, "
            "wider images, downsampled preview, version 6.0.5.3911)."
        ),
    )
    parser.add_argument("--text-confidence-threshold", type=float, default=0.78, help="Minimum OCR confidence for text records in mixed mode")
    parser.add_argument(
        "--board-theme",
        choices=tuple(BOARD_THEME_PALETTES.keys()),
        default=DEFAULT_BOARD_THEME,
        help="Dark board palette used when converting light-background crops",
    )
    parser.add_argument("--light-board", action="store_true", help="Disable dark-board color conversion (keep original light background in image records)")
    parser.add_argument("--debug-segments", action="store_true", help="Save block overlay images to <output-dir>/debug_segments/ for segmentation inspection")
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
    parser.add_argument(
        "--prototype-data-out",
        default="",
        help="Legacy opt-in path to write old prototype_data.js data outside project ui_prototype",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    subject = resolve_subject(args.subject)
    resolved_input_intent = _normalize_input_intent(args.input_intent)
    resolved_board_theme = _resolve_board_theme(args.board_theme)
    ai_fallback_config = _build_ai_fallback_config(
        enabled=args.ai_fallback_enabled,
        mode=args.ai_fallback,
        provider=args.ai_fallback_provider,
        model=args.ai_fallback_model,
        prompt=args.ai_fallback_prompt,
        max_tokens=args.ai_fallback_max_tokens,
        temperature=args.ai_fallback_temperature,
        threshold=args.ai_fallback_threshold,
        max_regions=args.ai_fallback_max_regions,
        timeout_ms=args.ai_fallback_timeout_ms,
        save_debug=args.ai_fallback_save_debug,
        fail_on_error=args.fail_on_ai_error,
    )
    debug_segments_dir = output_dir / "debug_segments" if args.debug_segments else None
    prepared_pages, pages = build_pages(
        args.source,
        subject=subject,
        ocr_mode=args.ocr,
        ai_fallback_config=ai_fallback_config,
        pdf_dpi=args.pdf_dpi,
        detect_perspective=args.detect_perspective,
        deskew=not args.skip_deskew,
        crop_margins=not args.skip_crop,
        max_dimension=args.max_dimension,
        debug_segments_dir=debug_segments_dir,
        input_intent=resolved_input_intent,
    )
    if resolved_input_intent in {"single-problem", "page-as-is"}:
        pages = _force_single_problem_per_page(pages, input_intent=resolved_input_intent)
    save_pages_json(pages, output_dir / "pages.json")

    template = LayoutTemplate(
        name=args.template_name,
        board_page_count=args.board_pages,
        base_slot_height_pages=args.slot_height,
        metadata={
            "placement_mode": "continuous-page-as-is"
            if resolved_input_intent == "page-as-is"
            else "one-problem-per-page"
        },
    )
    problem_entries = build_problem_entries(
        prepared_pages,
        pages,
        output_dir,
        template,
        board_theme=resolved_board_theme,
    )
    passage_problem_ids = {
        problem.unit_id
        for page in pages
        for problem in page.problems
        if _problem_is_passage_fragment_unit(problem)
    }
    problem_entries = configure_problem_entries_for_export(
        problem_entries,
        passage_problem_ids=passage_problem_ids,
        passages_only=args.passages_only,
        processing_step=args.processing_step or None,
        full_width=args.full_width,
    )
    resolved_record_mode = resolve_export_record_mode(
        args.record_mode,
        passages_only=bool(args.passages_only),
    )
    save_pages_json(pages, output_dir / "pages.json")
    resolved_crop_format = args.crop_format if args.crop_format in (CROP_FORMAT_V1, CROP_FORMAT_V2) else DEFAULT_CROP_FORMAT
    records, placements, header_flag = build_records(
        problem_entries,
        template,
        record_mode=resolved_record_mode,
        output_dir=output_dir,
        text_confidence_threshold=args.text_confidence_threshold,
        dark_board=not args.light_board,
        board_theme=resolved_board_theme,
        crop_format=resolved_crop_format,
    )

    edb_path = output_dir / f"{Path(args.source).stem}.edb"
    write_edb(
        edb_path,
        build_edb(
            records,
            header_flag=header_flag,
            version=version_string_for_crop_format(resolved_crop_format),
            page_count_hint=template.board_page_count,
        ),
    )
    ai_summary = _summarize_ai_fallback_usage(pages, ai_fallback_config)
    ocr_summary = _summarize_ocr_usage(pages)
    token_usage = aggregate_token_usage(
        [
            ocr_summary.get("token_usage"),
            ai_summary.get("token_usage") if isinstance(ai_summary, dict) else None,
        ]
    )
    ai_cost_summary = summarize_ai_cost(
        _combined_ai_usage_events(ocr_summary, ai_summary)
    )

    summary = {
        "source": str(args.source),
        "output_dir": str(output_dir),
        "edb_path": str(edb_path),
        "pages_json_path": str(output_dir / "pages.json"),
        "problem_crop_dir": str(output_dir / "problem_crops"),
        "block_crop_dir": str(output_dir / "block_crops"),
        "record_count": len(records),
        "record_mode": resolved_record_mode,
        "dark_board": not args.light_board,
        "board_theme": resolved_board_theme,
        "crop_format": resolved_crop_format,
        "header_flag": header_flag,
        "text_confidence_threshold": args.text_confidence_threshold,
        "ai_fallback": ai_fallback_config,
        "ai_summary": ai_summary,
        "placement_summary": build_placement_summary(placements),
        "placements": placements,
        "ocr_backend_requested": args.ocr,
        "ocr_summary": ocr_summary,
        "token_usage": token_usage,
        "ai_cost_summary": ai_cost_summary,
        "input_intent": resolved_input_intent,
        "passages_only": bool(args.passages_only),
        "processing_step_override": args.processing_step or None,
        "full_width": bool(args.full_width),
    }
    summary_path = output_dir / "board_run_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    prototype_path: Path | None = None
    if str(args.prototype_data_out or "").strip():
        prototype_path = resolve_legacy_prototype_data_path(args.prototype_data_out)
        prototype_path.parent.mkdir(parents=True, exist_ok=True)
        write_ui_prototype_data(prototype_path, placements)

    ui_session = build_ui_session(
        prepared_pages,
        placements,
        output_dir,
        edb_path,
        [Path(args.source).resolve()],
        record_mode=resolved_record_mode,
        ai_fallback_config=ai_fallback_config,
        ai_summary=ai_summary,
        ocr_summary=ocr_summary,
        token_usage=token_usage,
        ai_cost_summary=ai_cost_summary,
        pages=pages,
        template=template,
        input_intent=resolved_input_intent,
        board_theme=resolved_board_theme,
        crop_format=resolved_crop_format,
    )
    classin_handoff_path, classin_handoff_markdown_path = write_classin_handoff_manifest(
        output_dir,
        source_paths=[Path(args.source).resolve()],
        edb_path=edb_path,
        ui_session=ui_session,
        summary=summary,
        template=template,
    )
    handoff_session_fields = _classin_handoff_session_fields(classin_handoff_path)
    summary["classin_handoff_path"] = str(classin_handoff_path)
    summary["classin_handoff_markdown_path"] = str(classin_handoff_markdown_path)
    summary.update(handoff_session_fields)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    ui_session["classin_handoff_path"] = str(classin_handoff_path)
    ui_session["classinHandoffPath"] = str(classin_handoff_path)
    ui_session["classin_handoff_markdown_path"] = str(classin_handoff_markdown_path)
    ui_session["classinHandoffMarkdownPath"] = str(classin_handoff_markdown_path)
    ui_session.update(handoff_session_fields)
    ui_session_path, _synced_ui_path = write_ui_session_bundle(
        output_dir,
        ui_session,
        sync_ui=False,
    )

    result = {
        "edb_path": str(edb_path),
        "pages_json_path": str(output_dir / "pages.json"),
        "board_run_summary_path": str(summary_path),
        "ui_session_path": str(ui_session_path),
        "problem_count": len(placements),
        "record_mode": resolved_record_mode,
        "text_record_count": summary["placement_summary"]["text_record_count"],
        "image_record_count": summary["placement_summary"]["image_record_count"],
    }
    if prototype_path is not None:
        result["legacy_ui_prototype_data_path"] = str(prototype_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
