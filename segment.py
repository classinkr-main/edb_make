#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable
import unicodedata

from PIL import Image, ImageDraw, ImageOps, ImageStat

from passage_detection import (
    parse_passage_range_candidate,
    parse_shared_passage_range_header,
    passage_header_text_looks_corrupted,
)
from structured_schema import BlockType, Box, ContentBlock, PageModel, Subject

try:
    import cv2  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    cv2 = None

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    np = None


@dataclass(slots=True)
class SegmentOptions:
    min_area_ratio: float = 0.00025
    merge_gap_px: int = 16
    max_merge_gap_y_px: int = 28
    max_merge_gap_x_px: int = 48
    min_fill_ratio: float = 0.01
    ignore_large_border_ratio: float = 0.92
    fallback_row_density: float = 0.0035
    fallback_band_gap_px: int = 10
    fallback_min_band_height_px: int = 18
    fallback_padding_px: int = 10
    fallback_board_margin_ratio: float = 0.04
    fallback_board_min_hit_ratio: float = 0.08
    document_dark_threshold: int = 210
    document_projection_window_px: int = 10
    document_row_density_ratio: float = 0.11
    # Gap below which consecutive row-bands are merged into one block.
    # Smaller value → questions that are close together stay separate.
    document_band_merge_gap_px: int = 12
    # Bands shorter than this are candidates for merging into a neighbor.
    # Reduced so short answer-choice groups are not absorbed into the
    # preceding question stem.
    document_small_band_height_px: int = 80
    # Gap threshold used when deciding whether a small band is "near"
    # a neighbor. Reduced to match the tighter band-merge gap.
    document_near_gap_px: int = 60
    document_min_band_height_px: int = 40
    document_band_padding_px: int = 24
    document_recursive_split_min_height_px: int = 160
    document_recursive_split_max_depth: int = 4
    document_split_search_margin_ratio: float = 0.12
    document_split_valley_ratio: float = 0.35
    document_split_min_gap_run_px: int = 12
    document_split_padding_px: int = 20
    document_split_min_density_ratio: float = 0.0085


SEGMENTATION_MODE_BOARD = "board"
SEGMENTATION_MODE_DOCUMENT = "document"
LARGE_BLOCK_AREA_RATIO = 0.18
PDF_TEXT_MARKER_MIN_HWP_LAYOUT_HEIGHT_PX = 3.0
PDF_TEXT_MARKER_MIN_HWP_LAYOUT_HEIGHT_RATIO = 0.001
PDF_CHOICE_MARKERS = ("①", "②", "③", "④", "⑤")
PDF_PASSAGE_TEXT_EDGE_PADDING_PX = 4.0
PDF_PASSAGE_CENTER_DIVIDER_EXCLUSION_PX = 6.0
PDF_PASSAGE_RANGE_BRACKET_RE = re.compile(
    r"^\s*[\[［（(<]"
    r"(?P<start>[0-9０-９]{1,3})\s*[~\-〜－]\s*(?P<end>[0-9０-９]{1,3})\s*(?:번)?"
    r"[\]］）)>]"
)
PDF_PASSAGE_RANGE_KOREAN_RE = re.compile(
    r"^\s*(?:제\s*)?(?P<start>[0-9０-９]{1,3})\s*(?:번\s*)?"
    r"(?:[~\-〜－]|부터|에서)\s*"
    r"(?:제\s*)?(?P<end>[0-9０-９]{1,3})\s*번(?:까지)?"
)
PDF_PASSAGE_RANGE_COMPACT_RE = re.compile(
    r"^\s*(?:(?:문항|문제|questions?)\s*)?"
    r"(?P<start>[0-9０-９]{1,3})\s*[~\-\u2010-\u2015]\s*(?P<end>[0-9０-９]{1,3})\s*(?:번)?",
    re.IGNORECASE,
)
PDF_PASSAGE_RANGE_CUES = (
    "다음",
    "글",
    "자료",
    "지문",
    "대화",
    "담화",
    "발표",
    "작품",
    "도표",
    "그림",
    "실험",
    "보기",
    "읽고",
    "보고",
    "물음",
    "답하시오",
    "following",
    "read",
    "passage",
    "text",
    "questions",
    "conversation",
    "dialogue",
    "article",
    "chart",
    "graph",
)
PDF_EXAMPLE_MARKER_RE = re.compile(
    r"^\s*(?:(?:ex|example)\b|예제)\s*[\)\.]?",
    re.IGNORECASE,
)
PDF_WORKBOOK_HASH_SECTION_RE = re.compile(r"^\s*#\s*[0-9０-９]{1,3}\s*[\.\)]?")
PDF_WORKBOOK_NUMBER_SECTION_RE = re.compile(
    r"^\s*[0-9０-９]{1,2}\.\s*[가-힣A-Za-z][가-힣A-Za-z0-9\s·ㆍ\-()]{0,32}$"
)


def _pil_to_gray_array(image: Image.Image):
    if np is None:
        raise RuntimeError("numpy is required for segmentation")
    return np.array(image.convert("L"))


def _page_area_px(width: int, height: int) -> float:
    return max(float(width) * float(height), 1.0)


def _unique_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _collect_fallback_reasons(blocks: Iterable[ContentBlock]) -> list[str]:
    reasons: list[str] = []
    for block in blocks:
        raw_reason = block.metadata.get("fallback_reason")
        if isinstance(raw_reason, str):
            reasons.append(raw_reason)
        elif isinstance(raw_reason, (list, tuple, set)):
            reasons.extend(str(item) for item in raw_reason if str(item).strip())
    return _unique_strings(reasons)


def _block_area_ratio(block: ContentBlock, page_area: float) -> float:
    return float(block.bbox.area) / max(page_area, 1.0)


def _enrich_block_segmentation_metadata(
    metadata: dict[str, Any],
    *,
    segmentation_mode: str,
    block_area: float,
    page_area: float,
    large_block_threshold: float,
    page_width: int,
    page_height: int,
) -> dict[str, Any]:
    enriched = dict(metadata)
    block_area_ratio = block_area / max(page_area, 1.0)
    enriched["segmentation_mode"] = segmentation_mode
    enriched["page_area_px"] = int(page_width * page_height)
    enriched["block_area_ratio"] = round(block_area_ratio, 6)
    enriched["large_block"] = block_area_ratio >= large_block_threshold
    return enriched


def _build_segmentation_metadata(
    *,
    page_width: int,
    page_height: int,
    blocks: list[ContentBlock],
    segmentation_mode: str,
    segmenter: str,
    base_metadata: dict[str, Any] | None = None,
    extra_metadata: dict[str, Any] | None = None,
    large_block_threshold: float = LARGE_BLOCK_AREA_RATIO,
) -> dict[str, Any]:
    page_area = _page_area_px(page_width, page_height)
    block_area_ratios = [_block_area_ratio(block, page_area) for block in blocks]
    block_count = len(blocks)
    large_block_count = sum(1 for ratio in block_area_ratios if ratio >= large_block_threshold)
    fallback_reasons = _collect_fallback_reasons(blocks)
    text_block_count = sum(1 for block in blocks if bool(block.text and block.text.strip()))
    image_block_count = sum(1 for block in blocks if block.block_type in {BlockType.IMAGE, BlockType.DIAGRAM, BlockType.TABLE})

    metadata: dict[str, Any] = dict(base_metadata or {})
    if extra_metadata:
        metadata.update(extra_metadata)

    metadata.update(
        {
            "segmenter": segmenter,
            "segmentation_mode": segmentation_mode,
            "page_area_px": int(page_width * page_height),
            "block_count": block_count,
            "text_block_count": text_block_count,
            "image_block_count": image_block_count,
            "large_block_threshold": large_block_threshold,
            "large_block_count": large_block_count,
            "large_block_ratio": round(large_block_count / max(block_count, 1), 6),
            "max_block_area_ratio": round(max(block_area_ratios), 6) if block_area_ratios else 0.0,
            "mean_block_area_ratio": round(sum(block_area_ratios) / max(block_count, 1), 6),
            "fallback_reasons": fallback_reasons,
            "fallback_reason": fallback_reasons[0] if fallback_reasons else metadata.get("fallback_reason"),
            "fallback_reason_count": len(fallback_reasons),
            "has_fallback_reason": bool(fallback_reasons),
        }
    )
    metadata["segmentation_stats"] = {
        "segmenter": segmenter,
        "segmentation_mode": segmentation_mode,
        "page_area_px": int(page_width * page_height),
        "block_count": block_count,
        "text_block_count": text_block_count,
        "image_block_count": image_block_count,
        "large_block_threshold": large_block_threshold,
        "large_block_count": large_block_count,
        "large_block_ratio": round(large_block_count / max(block_count, 1), 6),
        "max_block_area_ratio": round(max(block_area_ratios), 6) if block_area_ratios else 0.0,
        "mean_block_area_ratio": round(sum(block_area_ratios) / max(block_count, 1), 6),
        "fallback_reasons": fallback_reasons,
    }
    return metadata


def _load_image(image_source: Any) -> Image.Image:
    if isinstance(image_source, Image.Image):
        return image_source.convert("RGB")

    for attr in ("image", "normalized_image"):
        image = getattr(image_source, attr, None)
        if isinstance(image, Image.Image):
            return image.convert("RGB")

    for attr in ("normalized_path", "source_path"):
        path = getattr(image_source, attr, None)
        if path:
            return Image.open(path).convert("RGB")

    return Image.open(image_source).convert("RGB")


def _sample_board_like_pixel(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    brightness = (r + g + b) / 3.0
    color_spread = max(r, g, b) - min(r, g, b)
    green_bias = g - max(r, b)
    if brightness < 125.0:
        return True
    if brightness < 235.0 and color_spread > 18 and green_bias >= -12:
        return True
    return False


def _detect_board_region_pil(image: Image.Image, options: SegmentOptions) -> Box:
    width, height = image.size
    step = max(6, min(width, height) // 180)
    sampled_cols = len(range(0, width, step))
    sampled_rows = len(range(0, height, step))
    row_scores = [0] * sampled_rows
    col_scores = [0] * sampled_cols

    pixels = image.convert("RGB").load()
    for row_index, y in enumerate(range(0, height, step)):
        for col_index, x in enumerate(range(0, width, step)):
            if _sample_board_like_pixel(pixels[x, y]):
                row_scores[row_index] += 1
                col_scores[col_index] += 1

    row_threshold = max(2, int(sampled_cols * options.fallback_board_min_hit_ratio))
    col_threshold = max(2, int(sampled_rows * max(0.05, options.fallback_board_min_hit_ratio * 0.75)))
    active_rows = [index for index, score in enumerate(row_scores) if score >= row_threshold]
    active_cols = [index for index, score in enumerate(col_scores) if score >= col_threshold]

    if active_rows and active_cols:
        left = max(0, active_cols[0] * step - step)
        top = max(0, active_rows[0] * step - step)
        right = min(width, (active_cols[-1] + 1) * step + step)
        bottom = min(height, (active_rows[-1] + 1) * step + step)

        margin = max(8, int(min(width, height) * options.fallback_board_margin_ratio))
        left = max(0, left - margin)
        top = max(0, top - margin)
        right = min(width, right + margin)
        bottom = min(height, bottom + margin)

        if right > left and bottom > top:
            region = Box.from_points(float(left), float(top), float(right), float(bottom))
            if region.area >= float(width * height) * 0.08:
                return region

    margin_x = max(12, int(width * 0.08))
    margin_y = max(12, int(height * 0.08))
    return Box.from_points(float(margin_x), float(margin_y), float(width - margin_x), float(height - margin_y))


def _detect_board_region(image: Image.Image, options: SegmentOptions) -> Box:
    if cv2 is None or np is None:
        return _detect_board_region_pil(image, options)

    rgb = np.array(image.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    lower = np.array([35, 10, 20], dtype=np.uint8)
    upper = np.array([120, 255, 220], dtype=np.uint8)
    mask = cv2.inRange(hsv, lower, upper)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_area = 0.0
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = float(w * h)
        if area > best_area and w > image.width * 0.35 and h > image.height * 0.2:
            best = Box(left=float(x), top=float(y), width=float(w), height=float(h))
            best_area = area

    return best or _detect_board_region_pil(image, options)


def _source_metadata(image_source: Any) -> dict[str, Any]:
    metadata = getattr(image_source, "metadata", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


def _is_document_like_page(image_source: Any, image: Image.Image) -> bool:
    metadata = _source_metadata(image_source)
    source_type = str(metadata.get("source_type") or "").lower()
    if source_type == "pdf" or metadata.get("document_like"):
        return True

    source_path = getattr(image_source, "source_path", None)
    if source_path and str(source_path).lower().endswith(".pdf"):
        return True

    gray = ImageOps.grayscale(image)
    stat = ImageStat.Stat(gray)
    mean_intensity = float(stat.mean[0])
    stddev = float(stat.stddev[0])
    if mean_intensity >= 220.0 and stddev <= 55.0:
        return True
    if mean_intensity < 185.0 or stddev > 72.0:
        return False

    histogram = gray.histogram()
    pixel_count = max(1, image.width * image.height)
    ink_ratio = sum(histogram[:170]) / float(pixel_count)
    # Phone photos and screenshots of worksheets often have gray paper or
    # shadows, so their mean brightness drops below the old white-page
    # threshold. Keep this heuristic narrow: enough dark ink to be a document,
    # but not enough to look like a filled board or photo scene.
    return 0.001 <= ink_ratio <= 0.22


def _dark_mask(image: Image.Image, threshold: int) -> Image.Image:
    # Use the raw grayscale image instead of autocontrast. Autocontrast makes
    # light UI highlights or translucent selection fills look like ink, which
    # causes adjacent questions to merge into one dense band.
    gray = ImageOps.grayscale(image)
    return gray.point(lambda px: 255 if px < threshold else 0, mode="L")


def _smooth_projection(values: list[float], window: int) -> list[float]:
    if not values:
        return []
    smoothed: list[float] = []
    for index in range(len(values)):
        start = max(0, index - window)
        end = min(len(values), index + window + 1)
        smoothed.append(sum(values[start:end]) / max(1, end - start))
    return smoothed


def _find_document_content_box(mask: Image.Image, width: int, height: int) -> Box:
    bbox = mask.getbbox()
    if bbox is None:
        return Box(left=0.0, top=0.0, width=float(width), height=float(height))
    left, top, right, bottom = bbox
    return Box.from_points(float(left), float(top), float(right), float(bottom)).expanded(
        12.0,
        max_width=float(width),
        max_height=float(height),
    )


def _detect_document_columns(mask: Image.Image, content_box: Box, options: SegmentOptions) -> list[Box]:
    crop = mask.crop((int(content_box.left), int(content_box.top), int(content_box.right), int(content_box.bottom)))
    if crop.width <= 1 or crop.height <= 1:
        return [content_box]

    # Same per-column dark counts as the inline crop/histogram loop this
    # replaced, but vectorized when NumPy is available.
    column_projection = _column_dark_projection(crop)
    smoothed = _smooth_projection(column_projection, max(12, options.document_projection_window_px * 2))
    if not smoothed:
        return [content_box]

    separator_split = _detect_document_separator_split_x(column_projection, crop.width, crop.height)
    if separator_split is not None:
        return _build_document_split_columns(mask, content_box, separator_split)

    search_start = int(crop.width * 0.3)
    search_end = max(search_start + 1, int(crop.width * 0.7))
    center_slice = smoothed[search_start:search_end]
    if not center_slice:
        return [content_box]

    split_offset = min(range(len(center_slice)), key=lambda idx: center_slice[idx])
    split_x = search_start + split_offset
    valley_score = smoothed[split_x]
    peak_score = max(smoothed)
    if peak_score <= 0 or valley_score > peak_score * 0.22:
        return [content_box]

    return _build_document_split_columns(mask, content_box, split_x)


def _detect_document_separator_split_x(
    column_projection: list[int],
    crop_width: int,
    crop_height: int,
) -> int | None:
    """Detect a narrow vertical rule between two document columns.

    Some Korean workbook pages have a printed or UI-highlighted center rule
    instead of a white gutter. The older valley-only column detector treats
    that rule as dense content and misses the two-column layout.
    """
    if crop_width <= 1 or crop_height <= 1:
        return None

    search_start = int(crop_width * 0.35)
    search_end = max(search_start + 1, int(crop_width * 0.65))
    min_height = max(40, int(crop_height * 0.42))
    max_run_width = max(3, int(crop_width * 0.035))

    runs: list[tuple[int, int, int]] = []
    run_start: int | None = None
    for column_index in range(search_start, search_end):
        if column_projection[column_index] >= min_height:
            if run_start is None:
                run_start = column_index
            continue
        if run_start is not None:
            runs.append((run_start, column_index - 1, max(column_projection[run_start:column_index])))
            run_start = None
    if run_start is not None:
        runs.append((run_start, search_end - 1, max(column_projection[run_start:search_end])))

    narrow_runs = [
        run
        for run in runs
        if (run[1] - run[0] + 1) <= max_run_width
    ]
    if not narrow_runs:
        return None

    best = max(narrow_runs, key=lambda item: item[2])
    split_x = (best[0] + best[1]) // 2
    if split_x < crop_width * 0.25 or split_x > crop_width * 0.75:
        return None
    return split_x


def _build_document_split_columns(mask: Image.Image, content_box: Box, split_x: int) -> list[Box]:
    gutter = max(10, int(content_box.width * 0.025))
    left_box = Box.from_points(
        content_box.left,
        content_box.top,
        content_box.left + float(split_x - gutter),
        content_box.bottom,
    )
    right_box = Box.from_points(
        content_box.left + float(split_x + gutter),
        content_box.top,
        content_box.right,
        content_box.bottom,
    )
    if left_box.width < mask.width * 0.2 or right_box.width < mask.width * 0.2:
        return [content_box]
    return [left_box, right_box]


def _band_ink_width_ratio(mask: Image.Image, column_box: Box, band: tuple[int, int]) -> float:
    band_top, band_bottom = band
    crop = mask.crop(
        (
            int(column_box.left),
            int(column_box.top + band_top),
            int(column_box.right),
            int(column_box.top + band_bottom + 1),
        )
    )
    if crop.width <= 0 or crop.height <= 0:
        return 0.0
    bbox = crop.getbbox()
    if bbox is None:
        return 0.0
    return float(bbox[2] - bbox[0]) / max(1.0, float(crop.width))


def _find_document_row_bands(mask: Image.Image, column_box: Box, options: SegmentOptions) -> list[tuple[int, int]]:
    crop = mask.crop((int(column_box.left), int(column_box.top), int(column_box.right), int(column_box.bottom)))
    if crop.width <= 1 or crop.height <= 1:
        return []

    row_projection = _row_dark_projection(crop)
    smoothed = _smooth_projection(row_projection, options.document_projection_window_px)
    if not smoothed:
        return []

    threshold = max(8.0, max(smoothed) * options.document_row_density_ratio)
    bands: list[tuple[int, int]] = []
    band_start: int | None = None
    last_active: int | None = None
    for row_index, score in enumerate(smoothed):
        if score >= threshold:
            if band_start is None:
                band_start = row_index
            last_active = row_index
            continue
        if band_start is not None and last_active is not None and row_index - last_active <= 16:
            continue
        if band_start is not None and last_active is not None:
            bands.append((band_start, last_active))
        band_start = None
        last_active = None
    if band_start is not None and last_active is not None:
        bands.append((band_start, last_active))

    merged: list[list[int]] = []
    for band_top, band_bottom in bands:
        if not merged or band_top - merged[-1][1] > options.document_band_merge_gap_px:
            merged.append([band_top, band_bottom])
        else:
            merged[-1][1] = band_bottom

    retained: list[list[int]] = []
    min_tail_height = max(8, int(options.document_min_band_height_px * 0.25))
    for index, (band_top, band_bottom) in enumerate((int(top), int(bottom)) for top, bottom in merged):
        band_height = band_bottom - band_top
        if band_height >= options.document_min_band_height_px:
            retained.append([band_top, band_bottom])
            continue
        if band_height < min_tail_height:
            continue

        previous_gap = band_top - retained[-1][1] if retained else 10**9
        next_gap = merged[index + 1][0] - band_bottom if index + 1 < len(merged) else 10**9
        width_ratio = _band_ink_width_ratio(mask, column_box, (band_top, band_bottom))
        if retained and previous_gap <= options.document_near_gap_px:
            retained[-1][1] = max(retained[-1][1], band_bottom)
            continue

        terminal_tail_gap = max(float(options.document_near_gap_px), float(column_box.height) * 0.14)
        if (
            retained
            and index == len(merged) - 1
            and previous_gap <= terminal_tail_gap
            and width_ratio >= 0.18
        ):
            retained[-1][1] = max(retained[-1][1], band_bottom)
            continue

        if next_gap <= options.document_near_gap_px and width_ratio >= 0.12:
            retained.append([band_top, band_bottom])

    if retained and column_box.width >= float(mask.width) * 0.72:
        weak_threshold = max(2.0, threshold * 0.45)
        weak_bands: list[tuple[int, int]] = []
        band_start = None
        last_active = None
        for row_index, score in enumerate(smoothed):
            if score >= weak_threshold or row_projection[row_index] > 0:
                if band_start is None:
                    band_start = row_index
                last_active = row_index
                continue
            if band_start is not None and last_active is not None and row_index - last_active <= 16:
                continue
            if band_start is not None and last_active is not None:
                weak_bands.append((band_start, last_active))
            band_start = None
            last_active = None
        if band_start is not None and last_active is not None:
            weak_bands.append((band_start, last_active))

        tail_gap_limit = max(float(options.document_near_gap_px), float(column_box.height) * 0.14)
        for weak_top, weak_bottom in weak_bands:
            weak_height = weak_bottom - weak_top
            if weak_height < min_tail_height:
                continue
            width_ratio = _band_ink_width_ratio(mask, column_box, (weak_top, weak_bottom))
            if width_ratio < 0.12:
                continue
            for retained_index, retained_band in enumerate(retained):
                retained_top, retained_bottom = retained_band
                if weak_top <= retained_bottom:
                    continue
                next_retained_top = retained[retained_index + 1][0] if retained_index + 1 < len(retained) else 10**9
                if weak_top >= next_retained_top:
                    continue
                if weak_top - retained_bottom <= tail_gap_limit:
                    retained_band[1] = max(retained_band[1], weak_bottom)
                    break

    return [(band_top, band_bottom) for band_top, band_bottom in retained]


def _merge_small_document_bands(
    bands: list[tuple[int, int]],
    options: SegmentOptions,
) -> list[tuple[int, int]]:
    if len(bands) <= 1:
        return bands

    merged = [[top, bottom] for top, bottom in bands]
    changed = True
    while changed and len(merged) > 1:
        changed = False
        index = 0
        while index < len(merged):
            band_top, band_bottom = merged[index]
            height = band_bottom - band_top
            if height > options.document_small_band_height_px:
                index += 1
                continue

            prev_gap = band_top - merged[index - 1][1] if index > 0 else 10**9
            next_gap = merged[index + 1][0] - band_bottom if index + 1 < len(merged) else 10**9
            if min(prev_gap, next_gap) > options.document_near_gap_px:
                index += 1
                continue

            if next_gap < prev_gap and index + 1 < len(merged):
                merged[index][1] = max(merged[index][1], merged[index + 1][1])
                merged.pop(index + 1)
            elif index > 0:
                merged[index - 1][1] = max(merged[index - 1][1], merged[index][1])
                merged.pop(index)
            elif index + 1 < len(merged):
                merged[index][1] = max(merged[index][1], merged[index + 1][1])
                merged.pop(index + 1)
            changed = True
            index = 0
    return [(top, bottom) for top, bottom in merged]


def _document_band_box(mask: Image.Image, column_box: Box, band: tuple[int, int], options: SegmentOptions) -> Box:
    band_top, band_bottom = band
    crop = mask.crop((int(column_box.left), int(column_box.top + band_top), int(column_box.right), int(column_box.top + band_bottom + 1)))
    bbox = crop.getbbox()
    if bbox is None:
        return Box.from_points(column_box.left, column_box.top + band_top, column_box.right, column_box.top + band_bottom)

    padding = float(options.document_band_padding_px)
    left = column_box.left + max(0.0, float(bbox[0]) - padding)
    right = column_box.left + min(column_box.width, float(bbox[2]) + padding)
    top = column_box.top + max(0.0, float(band_top) - padding)
    bottom = column_box.top + min(column_box.height, float(band_bottom) + padding)
    # minimum_width = column_box.width * 0.72
    # if right - left < minimum_width:
    #     left = column_box.left
    #     right = column_box.right

    return Box.from_points(left, top, right, bottom).expanded(
        8.0,
        max_width=float(mask.width),
        max_height=float(mask.height),
    )


def _marker_bbox(marker: dict[str, Any]) -> Box | None:
    bbox = marker.get("bbox")
    if not isinstance(bbox, dict):
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
    return Box.from_points(left, top, right, bottom)


def _is_tiny_hwp_layout_marker(marker: dict[str, Any], marker_box: Box, image_height: int) -> bool:
    if str(marker.get("marker_kind") or "") != "hwp_layout_number":
        return False
    min_height = max(
        PDF_TEXT_MARKER_MIN_HWP_LAYOUT_HEIGHT_PX,
        float(image_height) * PDF_TEXT_MARKER_MIN_HWP_LAYOUT_HEIGHT_RATIO,
    )
    visible_top = max(0.0, marker_box.top)
    visible_bottom = min(float(image_height), marker_box.bottom)
    if visible_bottom <= visible_top:
        return True
    return visible_bottom - visible_top < min_height


def _cluster_pdf_marker_columns(markers: list[dict[str, Any]], page_width: int) -> list[list[dict[str, Any]]]:
    marker_pairs: list[tuple[float, dict[str, Any]]] = []
    for marker in markers:
        box = _marker_bbox(marker)
        if box is None:
            continue
        marker_pairs.append(((box.left + box.right) / 2.0, marker))
    if not marker_pairs:
        return []
    if len(marker_pairs) == 1:
        return [[marker_pairs[0][1]]]

    sorted_pairs = sorted(marker_pairs, key=lambda item: item[0])
    gaps = [
        (sorted_pairs[index + 1][0] - sorted_pairs[index][0], index)
        for index in range(len(sorted_pairs) - 1)
    ]
    largest_gap, split_index = max(gaps, key=lambda item: item[0])
    if largest_gap < max(80.0, float(page_width) * 0.18):
        return [[marker for _, marker in sorted_pairs]]

    return [
        [marker for _, marker in sorted_pairs[: split_index + 1]],
        [marker for _, marker in sorted_pairs[split_index + 1 :]],
    ]


def _column_bounds_from_marker_columns(
    columns: list[list[dict[str, Any]]],
    page_width: int,
) -> list[tuple[float, float]]:
    if len(columns) != 2:
        return [(0.0, float(page_width)) for _ in columns]

    left_centers = [
        (_marker_bbox(marker).left + _marker_bbox(marker).right) / 2.0
        for marker in columns[0]
        if _marker_bbox(marker) is not None
    ]
    right_centers = [
        (_marker_bbox(marker).left + _marker_bbox(marker).right) / 2.0
        for marker in columns[1]
        if _marker_bbox(marker) is not None
    ]
    if not left_centers or not right_centers:
        return [(0.0, float(page_width)) for _ in columns]

    # The marker x positions are near the problem-number glyphs, not the true
    # column edges. For rendered exam PDFs the physical columns are balanced,
    # so the page midpoint is a better crop boundary than the midpoint between
    # left and right marker numbers.
    split_x = float(page_width) / 2.0
    return [(0.0, split_x), (split_x, float(page_width))]


def _marker_center_x(marker: dict[str, Any]) -> float | None:
    box = _marker_bbox(marker)
    if box is None:
        return None
    return (box.left + box.right) / 2.0


def _detect_pdf_visual_column_boxes(image: Image.Image) -> list[Box]:
    mask = _dark_mask(image, threshold=220)
    content_box = _find_document_content_box(mask, image.width, image.height)
    columns = _detect_document_columns(mask, content_box, SegmentOptions())
    if len(columns) != 2:
        full_page_split_x = _detect_document_separator_split_x(
            _column_dark_projection(mask),
            image.width,
            image.height,
        )
        if full_page_split_x is None:
            return []
        return [
            Box.from_points(0.0, 0.0, float(full_page_split_x), float(image.height)),
            Box.from_points(float(full_page_split_x), 0.0, float(image.width), float(image.height)),
        ]
    if any(column.width < float(image.width) * 0.2 for column in columns):
        return []
    return sorted(columns, key=lambda column: column.left)


def detect_pdf_visual_column_divider_x(image: Image.Image) -> float | None:
    """Return the visual gutter/divider between two PDF body columns.

    Official Korean exam pages are not always symmetric around the physical
    page midpoint. Reusing the segmentation boundary prevents later crop
    recovery from expanding into the neighbouring column.
    """
    columns = _detect_pdf_visual_column_boxes(image)
    if len(columns) != 2:
        return None
    left_column, right_column = sorted(columns, key=lambda box: box.left)
    if left_column.right >= right_column.left:
        return None
    return (float(left_column.right) + float(right_column.left)) * 0.5


def _assign_pdf_marker_columns(
    image: Image.Image,
    markers: list[dict[str, Any]],
) -> tuple[list[tuple[int, list[dict[str, Any]], tuple[float, float]]], int, bool]:
    visual_columns = _detect_pdf_visual_column_boxes(image)
    if len(visual_columns) == 2:
        boundary_x = (visual_columns[0].right + visual_columns[1].left) / 2.0
        grouped: list[list[dict[str, Any]]] = [[], []]
        for marker in markers:
            center_x = _marker_center_x(marker)
            if center_x is None:
                continue
            column_index = 0 if center_x < boundary_x else 1
            grouped[column_index].append(marker)
        entries = [
            (
                index + 1,
                column_markers,
                (visual_columns[index].left, visual_columns[index].right),
            )
            for index, column_markers in enumerate(grouped)
        ]
        if entries:
            return entries, len(visual_columns), True

    marker_columns = _cluster_pdf_marker_columns(markers, image.width)
    column_bounds = _column_bounds_from_marker_columns(marker_columns, image.width)
    return [
        (index + 1, column_markers, column_bounds[index])
        for index, column_markers in enumerate(marker_columns)
    ], len(marker_columns), False


def _trim_isolated_pdf_footer_bottom(
    image: Image.Image,
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> float:
    if bottom < float(image.height) - 1.0:
        return bottom
    crop_box = (
        max(0, int(left)),
        max(0, int(top)),
        min(image.width, int(right)),
        min(image.height, int(bottom)),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return bottom

    mask = _dark_mask(image.crop(crop_box), threshold=220)
    row_projection = _row_dark_projection(mask)
    row_threshold = max(2, int(mask.width * 0.004))
    runs = [
        run
        for run in _find_active_runs(row_projection, threshold=row_threshold)
        if run[1] - run[0] + 1 >= 2
    ]
    if len(runs) < 2:
        return bottom

    last_run = runs[-1]
    min_footer_gap = max(44, int(float(image.height) * 0.055))
    if len(runs) >= 2 and last_run[0] - runs[-2][1] < min_footer_gap:
        return bottom

    last_run_top = crop_box[1] + last_run[0]
    if last_run_top < float(image.height) * 0.86:
        return bottom
    if last_run[1] - last_run[0] + 1 > max(30, int(float(image.height) * 0.04)):
        return bottom

    last_run_mask = mask.crop((0, last_run[0], mask.width, last_run[1] + 1))
    last_bbox = last_run_mask.getbbox()
    if last_bbox is None:
        return bottom
    footer_width_ratio = float(last_bbox[2] - last_bbox[0]) / max(1.0, float(mask.width))
    if footer_width_ratio > 0.45:
        return bottom
    footer_center_ratio = ((last_bbox[0] + last_bbox[2]) / 2.0) / max(1.0, float(mask.width))
    if not 0.35 <= footer_center_ratio <= 0.65:
        return bottom

    for previous_run in reversed(runs[:-1]):
        if last_run[0] - previous_run[1] < min_footer_gap:
            continue
        padding = max(14.0, float(image.height) * 0.012)
        trimmed_bottom = min(bottom, float(crop_box[1] + previous_run[1]) + padding)
        if trimmed_bottom - top >= 40.0:
            return trimmed_bottom
        return bottom

    return bottom


def _text_contains_choice_marker(text: Any) -> bool:
    return any(marker in str(text or "") for marker in PDF_CHOICE_MARKERS)


def _pdf_text_lines_in_region(
    text_lines: list[dict[str, Any]],
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> list[tuple[Box, dict[str, Any]]]:
    lines: list[tuple[Box, dict[str, Any]]] = []
    for line in text_lines:
        if not isinstance(line, dict):
            continue
        line_box = _marker_bbox(line)
        if line_box is None:
            continue
        center_x = (line_box.left + line_box.right) / 2.0
        if center_x < left - 6.0 or center_x > right + 6.0:
            continue
        if line_box.bottom < top - 2.0 or line_box.top > bottom + 2.0:
            continue
        lines.append((line_box, line))
    return sorted(lines, key=lambda item: (item[0].top, item[0].left))


def _mask_without_tall_vertical_rules(mask: Image.Image) -> Image.Image:
    if mask.width <= 1 or mask.height <= 1:
        return mask

    column_projection = _column_dark_projection(mask)
    threshold = max(60, int(mask.height * 0.65))
    rule_runs = _find_active_runs(column_projection, threshold=threshold)
    if not rule_runs:
        return mask

    max_rule_width = max(5, int(mask.width * 0.018))
    cleaned = mask.copy()
    draw = ImageDraw.Draw(cleaned)
    changed = False
    for run_left, run_right in rule_runs:
        if run_right - run_left + 1 > max_rule_width:
            continue
        draw.rectangle((run_left, 0, run_right, mask.height), fill=0)
        changed = True

    return cleaned if changed else mask


def _pdf_problem_ink_bottom(
    image: Image.Image,
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> tuple[float, bool]:
    crop_box = (
        max(0, int(left)),
        max(0, int(top)),
        min(image.width, int(right)),
        min(image.height, int(bottom)),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return bottom, False

    mask = _dark_mask(image.crop(crop_box), threshold=220)
    mask = _mask_without_tall_vertical_rules(mask)
    row_projection = _row_dark_projection(mask)
    row_threshold = max(2, int(mask.width * 0.003))
    runs = [
        run
        for run in _find_active_runs(row_projection, threshold=row_threshold)
        if run[1] - run[0] + 1 >= 2
    ]
    if not runs:
        return bottom, False

    cluster_gap = max(80, int(float(image.height) * 0.04))
    clusters: list[list[int]] = []
    for run_top, run_bottom in runs:
        if not clusters or run_top - clusters[-1][1] > cluster_gap:
            clusters.append([run_top, run_bottom])
        else:
            clusters[-1][1] = max(clusters[-1][1], run_bottom)
    if not clusters:
        return bottom, False

    selected_index = len(clusters) - 1
    detached_gap = max(260.0, float(image.height) * 0.12)
    lower_tail_start = float(crop_box[1]) + (float(crop_box[3] - crop_box[1]) * 0.45)
    while selected_index > 0:
        current = clusters[selected_index]
        previous = clusters[selected_index - 1]
        gap = float(current[0] - previous[1])
        current_abs_top = float(crop_box[1] + current[0])
        if gap <= detached_gap or current_abs_top < lower_tail_start:
            break
        selected_index -= 1

    selected_cluster = clusters[selected_index]
    padding = max(18.0, float(image.height) * 0.012)
    trimmed_bottom = min(bottom, float(crop_box[1] + selected_cluster[1]) + padding)
    if trimmed_bottom - top < 40.0:
        return bottom, False
    if bottom - trimmed_bottom < max(24.0, float(image.height) * 0.012):
        return bottom, False
    return trimmed_bottom, True


def _pdf_tail_has_thin_math_figure_signal(
    mask: Image.Image,
    tail_bbox: tuple[int, int, int, int],
    *,
    crop_width: float,
) -> bool:
    left, top, right, bottom = tail_bbox
    tail_width = right - left
    tail_height = bottom - top
    if tail_width < max(54, int(float(crop_width) * 0.12)) or tail_height < 8:
        return False

    tail_mask = mask.crop(tail_bbox)
    row_projection = _row_dark_projection(tail_mask)
    column_projection = _column_dark_projection(tail_mask)
    if not row_projection or not column_projection:
        return False

    horizontal_signal = max(row_projection) >= max(36, int(float(tail_width) * 0.42))
    vertical_signal = max(column_projection) >= max(10, int(float(tail_height) * 0.52))
    return horizontal_signal and vertical_signal


def _pdf_choice_visual_tail_bottom(
    image: Image.Image,
    text_lines: list[dict[str, Any]],
    *,
    left: float,
    right: float,
    choice_bottom: float,
    bottom: float,
) -> float | None:
    tail_start = choice_bottom + max(8.0, float(image.height) * 0.006)
    crop_box = (
        max(0, int(left)),
        max(0, int(tail_start)),
        min(image.width, int(right)),
        min(image.height, int(bottom)),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return None

    mask = _dark_mask(image.crop(crop_box), threshold=220)
    mask = _mask_without_tall_vertical_rules(mask)
    tail_bbox = mask.getbbox()
    if tail_bbox is None:
        return None

    row_projection = _row_dark_projection(mask)
    row_threshold = max(2, int(mask.width * 0.003))
    runs = [
        run
        for run in _find_active_runs(row_projection, threshold=row_threshold)
        if run[1] - run[0] + 1 >= 2
    ]
    if runs:
        cluster_gap = max(80, int(float(image.height) * 0.04))
        clusters: list[list[int]] = []
        for run_top, run_bottom in runs:
            if not clusters or run_top - clusters[-1][1] > cluster_gap:
                clusters.append([run_top, run_bottom])
            else:
                clusters[-1][1] = max(clusters[-1][1], run_bottom)
        if clusters:
            selected_end_index = 0
            detached_gap = max(260.0, float(image.height) * 0.12)
            for index in range(1, len(clusters)):
                if float(clusters[index][0] - clusters[index - 1][1]) > detached_gap:
                    break
                selected_end_index = index
            selected_top = clusters[0][0]
            selected_bottom = clusters[selected_end_index][1]
            selected_mask = mask.crop((0, selected_top, mask.width, selected_bottom + 1))
            selected_bbox = selected_mask.getbbox()
            if selected_bbox is not None:
                tail_bbox = (
                    selected_bbox[0],
                    selected_top + selected_bbox[1],
                    selected_bbox[2],
                    selected_top + selected_bbox[3],
                )

    tail_abs_top = float(crop_box[1] + tail_bbox[1])
    tail_abs_bottom = float(crop_box[1] + tail_bbox[3])
    tail_gap = tail_abs_top - choice_bottom
    max_tail_gap = max(120.0, min(260.0, float(image.height) * 0.12))
    if tail_gap > max_tail_gap:
        return None

    crop_width = max(1.0, float(crop_box[2] - crop_box[0]))
    tail_width_ratio = float(tail_bbox[2] - tail_bbox[0]) / crop_width
    tail_height = tail_abs_bottom - tail_abs_top
    regular_tail_min_height = max(42.0, float(image.height) * 0.025)
    thin_math_figure_tail = False
    if tail_width_ratio >= 0.12 and (
        tail_width_ratio < 0.16 or tail_height < regular_tail_min_height
    ):
        thin_math_figure_tail = _pdf_tail_has_thin_math_figure_signal(
            mask,
            tail_bbox,
            crop_width=crop_width,
        )
    min_width_ratio = 0.12 if thin_math_figure_tail else 0.16
    if tail_width_ratio < min_width_ratio or (
        tail_height < regular_tail_min_height
        and not thin_math_figure_tail
    ):
        return None

    tail_lines = _pdf_text_lines_in_region(
        text_lines,
        left=left,
        right=right,
        top=choice_bottom + 2.0,
        bottom=bottom,
    )
    if tail_lines:
        first_line_box = tail_lines[0][0]
        column_width = max(1.0, right - left)
        first_line_width_ratio = first_line_box.width / column_width
        first_line_aligned_with_tail = abs(tail_abs_top - first_line_box.top) <= max(10.0, float(image.height) * 0.005)
        if first_line_aligned_with_tail and first_line_width_ratio >= 0.28:
            return None

        text_span_top = min(line_box.top for line_box, _line in tail_lines)
        text_span_bottom = max(line_box.bottom for line_box, _line in tail_lines)
        max_text_width_ratio = max(line_box.width / column_width for line_box, _line in tail_lines)
        text_span_height = max(1.0, text_span_bottom - text_span_top)
        if max_text_width_ratio >= 0.24 and tail_height <= text_span_height * 1.6:
            return None

    padding = max(18.0, float(image.height) * 0.012)
    return min(bottom, tail_abs_bottom + padding)


def _trim_pdf_problem_bottom_to_last_choice(
    image: Image.Image,
    text_lines: list[dict[str, Any]],
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> tuple[float, bool, bool]:
    region_lines = _pdf_text_lines_in_region(
        text_lines,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
    )
    if not region_lines:
        return bottom, False, False

    choice_indexes = [
        index
        for index, (_line_box, line) in enumerate(region_lines)
        if _text_contains_choice_marker(line.get("text"))
    ]
    if not choice_indexes:
        return bottom, False, False

    last_choice_index = choice_indexes[-1]
    last_choice_bottom = region_lines[last_choice_index][0].bottom
    line_heights = [line_box.height for line_box, _line in region_lines if line_box.height > 0]
    median_line_height = sorted(line_heights)[len(line_heights) // 2] if line_heights else 18.0
    continuation_gap = max(18.0, min(72.0, max(median_line_height * 2.2, float(image.height) * 0.018)))
    for next_line_box, _next_line in region_lines[last_choice_index + 1 :]:
        gap = next_line_box.top - last_choice_bottom
        if gap > continuation_gap:
            break
        last_choice_bottom = max(last_choice_bottom, next_line_box.bottom)

    padding = max(18.0, float(image.height) * 0.012)
    trimmed_bottom = min(bottom, max(top + 40.0, last_choice_bottom + padding))
    visual_tail_bottom = _pdf_choice_visual_tail_bottom(
        image,
        text_lines,
        left=left,
        right=right,
        choice_bottom=last_choice_bottom,
        bottom=bottom,
    )
    visual_tail_attached = False
    if visual_tail_bottom is not None and visual_tail_bottom > trimmed_bottom:
        trimmed_bottom = visual_tail_bottom
        visual_tail_attached = True

    if bottom - trimmed_bottom < max(24.0, float(image.height) * 0.012):
        return bottom, False, False
    return trimmed_bottom, True, visual_tail_attached


def _trim_pdf_problem_bottom_to_ink(
    image: Image.Image,
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> tuple[float, bool]:
    return _pdf_problem_ink_bottom(
        image,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
    )


def _extract_pdf_passage_range(text: Any) -> tuple[int, int] | None:
    header = parse_shared_passage_range_header(text)
    if header is not None:
        return header.start, header.end
    candidate = parse_passage_range_candidate(text)
    if candidate is not None and passage_header_text_looks_corrupted(text):
        return candidate[0], candidate[1]
    return None


def _pdf_column_bounds_for_x(
    column_entries: list[tuple[int, list[dict[str, Any]], tuple[float, float]]],
    center_x: float,
    image: Image.Image,
) -> tuple[int, float, float]:
    for column_index, _markers, (left_bound, right_bound) in column_entries:
        if left_bound - 8.0 <= center_x <= right_bound + 8.0:
            return column_index, left_bound, right_bound
    visual_columns = _detect_pdf_visual_column_boxes(image)
    if len(visual_columns) == 2:
        for column_index, column_box in enumerate(visual_columns, start=1):
            if column_box.left - 8.0 <= center_x <= column_box.right + 8.0:
                return column_index, column_box.left, column_box.right
    return 1, 0.0, float(image.width)


def _looks_like_pdf_footer_text_line(text: Any, box: Box, image_height: int) -> bool:
    compact = unicodedata.normalize("NFKC", str(text or ""))
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact or box.top < float(image_height) * 0.82:
        return False
    if re.fullmatch(r"[-–—]?\s*[0-9]{1,3}\s*[-–—]?", compact):
        return True
    lower = compact.lower()
    return any(token in lower for token in ("copyright", "저작권", "확인 사항", "답안지"))


def _pdf_same_column_text_bottom_after_header(
    text_lines: list[dict[str, Any]],
    header_box: Box,
    *,
    left_bound: float,
    right_bound: float,
    image_height: int,
) -> float | None:
    bottoms: list[float] = []
    for line in text_lines:
        line_box = _marker_bbox(line)
        if line_box is None or line_box.top <= header_box.top:
            continue
        line_center_x = (line_box.left + line_box.right) / 2.0
        if not left_bound - 8.0 <= line_center_x <= right_bound + 8.0:
            continue
        if _looks_like_pdf_footer_text_line(line.get("text"), line_box, image_height):
            continue
        bottoms.append(line_box.bottom)
    if not bottoms:
        return None
    return min(float(image_height), max(bottoms) + max(18.0, float(image_height) * 0.012))


def _looks_like_pdf_page_header_text_line(text: Any, box: Box, image_height: int) -> bool:
    if box.top > float(image_height) * 0.12:
        return False
    compact = unicodedata.normalize("NFKC", str(text or ""))
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact:
        return True
    no_space = re.sub(r"\s+", "", compact)
    header_label = re.sub(r"[^0-9A-Za-z가-힣]", "", no_space)
    if header_label in {
        "국어",
        "영어",
        "화법과작문",
        "언어와매체",
        "홀수형",
        "짝수형",
    }:
        return True
    if compact in {
        "고 1",
        "고 2",
        "고 3",
        "국어 영역",
        "영어 영역",
    }:
        return True
    collapsed = re.sub(r"\s+", "", compact)
    if collapsed in {
        "고1",
        "고2",
        "고3",
        "국어영역",
        "수학영역",
        "영어영역",
        "영역",
    }:
        return True
    if re.fullmatch(r"고[123]\d{1,2}", collapsed):
        return True
    if re.fullmatch(r"[━─—\-_=·•\s]+", compact):
        return True
    if re.fullmatch(r"\d{1,2}\s+\d{1,2}", compact):
        return True
    if re.fullmatch(r"\d{1,2}", compact):
        return True
    return False


def _pdf_pre_question_text_regions(
    image: Image.Image,
    text_lines: list[dict[str, Any]],
    column_entries: list[tuple[int, list[dict[str, Any]], tuple[float, float]]],
    numbered_markers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Summarize substantive PDF text before the page's first question."""
    candidates = [
        marker
        for marker in numbered_markers
        if isinstance(marker.get("number"), int) and _marker_bbox(marker) is not None
    ]
    if not candidates:
        return []
    first_marker = min(
        candidates,
        key=lambda marker: (int(marker["number"]), _marker_bbox(marker).top),
    )
    first_box = _marker_bbox(first_marker)
    if first_box is None:
        return []
    first_column, _first_left, _first_right = _pdf_column_bounds_for_x(
        column_entries,
        (first_box.left + first_box.right) / 2.0,
        image,
    )
    padding = max(8.0, float(image.height) * 0.004)
    regions: list[dict[str, Any]] = []
    for column_index, _markers, (left_bound, right_bound) in sorted(
        column_entries,
        key=lambda entry: entry[0],
    ):
        if column_index > first_column:
            continue
        boundary = first_box.top if column_index == first_column else float(image.height)
        region_lines: list[tuple[Box, str]] = []
        for line in text_lines:
            line_box = _marker_bbox(line)
            if line_box is None or line_box.top >= boundary:
                continue
            center_x = (line_box.left + line_box.right) / 2.0
            if not left_bound - 8.0 <= center_x <= right_bound + 8.0:
                continue
            if _looks_like_pdf_page_header_text_line(line.get("text"), line_box, image.height):
                continue
            if _looks_like_pdf_footer_text_line(line.get("text"), line_box, image.height):
                continue
            text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(line.get("text") or "")))
            if not text:
                continue
            region_lines.append((line_box, text))
        char_count = sum(len(text) for _box, text in region_lines)
        if len(region_lines) < 2 or char_count < 16:
            continue
        top = max(0.0, min(box.top for box, _text in region_lines) - padding)
        if column_index == first_column:
            # Leave a visible gap before the first question marker, but do not
            # pull the crop boundary so far upward that the last passage line
            # loses its antialiased baseline on 200-DPI pages.
            bottom = max(
                top + 1.0,
                first_box.top - max(6.0, float(image.height) * 0.002),
            )
        else:
            bottom = min(
                float(image.height),
                max(box.bottom for box, _text in region_lines) + max(18.0, float(image.height) * 0.012),
            )
        if bottom - top < max(36.0, float(image.height) * 0.025):
            continue
        regions.append(
            {
                "column_index": column_index,
                "before_problem_number": int(first_marker["number"]),
                "line_count": len(region_lines),
                "char_count": char_count,
                "bbox": {
                    "left": left_bound,
                    "top": top,
                    "right": right_bound,
                    "bottom": bottom,
                },
            }
        )
    return regions


def _pdf_passage_column_text_top(
    text_lines: list[dict[str, Any]],
    *,
    left_bound: float,
    right_bound: float,
    bottom: float,
    image_height: int,
) -> float | None:
    tops: list[float] = []
    for line in text_lines:
        line_box = _marker_bbox(line)
        if line_box is None or line_box.top >= bottom:
            continue
        line_center_x = (line_box.left + line_box.right) / 2.0
        if not left_bound - 8.0 <= line_center_x <= right_bound + 8.0:
            continue
        if _looks_like_pdf_page_header_text_line(line.get("text"), line_box, image_height):
            continue
        if _looks_like_pdf_footer_text_line(line.get("text"), line_box, image_height):
            continue
        tops.append(line_box.top)
    if not tops:
        return None
    return max(0.0, min(tops) - max(8.0, float(image_height) * 0.004))


def _pdf_passage_column_text_bottom(
    text_lines: list[dict[str, Any]],
    *,
    left_bound: float,
    right_bound: float,
    top: float,
    bottom: float,
    image_height: int,
) -> float | None:
    bottoms: list[float] = []
    for line in text_lines:
        line_box = _marker_bbox(line)
        if line_box is None or line_box.bottom <= top or line_box.top >= bottom:
            continue
        line_center_x = (line_box.left + line_box.right) / 2.0
        if not left_bound - 8.0 <= line_center_x <= right_bound + 8.0:
            continue
        if _looks_like_pdf_footer_text_line(line.get("text"), line_box, image_height):
            continue
        bottoms.append(line_box.bottom)
    if not bottoms:
        return None
    # Text-layer boxes already include the glyph body. Keep only a small
    # rasterization allowance here: the previous 1.2%-of-page outset could
    # cross a lower frame/separator on high-resolution exam pages.
    return min(
        bottom,
        max(bottoms) + max(PDF_PASSAGE_TEXT_EDGE_PADDING_PX, float(image_height) * 0.002),
    )


def _trim_pdf_passage_column_bottom_to_ink(
    image: Image.Image,
    *,
    left: float,
    right: float,
    top: float,
    bottom: float,
) -> float:
    crop_box = (
        max(0, int(left)),
        max(0, int(top)),
        min(image.width, int(right)),
        min(image.height, int(bottom)),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return bottom

    mask = _dark_mask(image.crop(crop_box), threshold=220)
    row_projection = _row_dark_projection(mask)
    row_threshold = max(2, int(mask.width * 0.004))
    runs = [
        run
        for run in _find_active_runs(row_projection, threshold=row_threshold)
        if run[1] - run[0] + 1 >= 2
    ]
    if not runs:
        return bottom

    meaningful_runs: list[tuple[int, int]] = []
    for run in runs:
        abs_top = crop_box[1] + run[0]
        run_projection = row_projection[run[0] : run[1] + 1]
        max_dark_pixels = max(run_projection) if run_projection else 0
        width_ratio = float(max_dark_pixels) / max(1.0, float(mask.width))
        if abs_top >= float(image.height) * 0.86 and width_ratio < 0.12:
            continue
        meaningful_runs.append(run)

    if not meaningful_runs:
        return bottom
    last_run = meaningful_runs[-1]
    # Stop close to the last real ink run. A large fixed tail is especially
    # harmful when the next question begins immediately below a passage.
    padding = max(PDF_PASSAGE_TEXT_EDGE_PADDING_PX, float(image.height) * 0.002)
    trimmed_bottom = min(bottom, float(crop_box[1] + last_run[1]) + padding)
    return trimmed_bottom if trimmed_bottom - top >= 40.0 else bottom


def _is_pdf_example_marker_text(text: Any) -> bool:
    return bool(PDF_EXAMPLE_MARKER_RE.match(unicodedata.normalize("NFKC", str(text or "")).strip()))


def _is_pdf_workbook_section_heading_text(text: Any) -> bool:
    compact = unicodedata.normalize("NFKC", str(text or ""))
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact:
        return False
    return bool(
        PDF_WORKBOOK_HASH_SECTION_RE.match(compact)
        or PDF_WORKBOOK_NUMBER_SECTION_RE.match(compact)
    )


def _looks_like_pdf_workbook_footer_line(text: Any, box: Box, image_height: int) -> bool:
    compact = unicodedata.normalize("NFKC", str(text or ""))
    compact = re.sub(r"\s+", " ", compact).strip()
    if not compact or box.top < float(image_height) * 0.84:
        return False
    lower = compact.lower()
    if re.fullmatch(r"[-–—]?\s*[0-9]{1,3}\s*[-–—]?", compact):
        return True
    return any(token in lower for token in ("youtube", "중3", "중학교", "친절한"))


def _pdf_workbook_footer_top(text_lines: list[dict[str, Any]], image_height: int) -> float | None:
    footer_tops: list[float] = []
    for line in text_lines:
        line_box = _marker_bbox(line)
        if line_box is None:
            continue
        if _looks_like_pdf_workbook_footer_line(line.get("text"), line_box, image_height):
            footer_tops.append(line_box.top)
    return min(footer_tops) if footer_tops else None


def _pdf_example_boundary_lines(text_lines: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    boundaries: list[tuple[float, dict[str, Any]]] = []
    for line in text_lines:
        line_box = _marker_bbox(line)
        if line_box is None:
            continue
        text = line.get("text")
        if _is_pdf_example_marker_text(text) or _is_pdf_workbook_section_heading_text(text):
            boundaries.append((line_box.top, line))
    boundaries.sort(key=lambda item: item[0])
    return boundaries


def _pdf_example_meaningful_bottom(crop_mask: Image.Image, *, page_height: int, crop_top: int) -> int | None:
    row_projection = _row_dark_projection(crop_mask)
    if not row_projection:
        return None
    row_threshold = max(2, int(crop_mask.width * 0.004))
    runs = [
        run
        for run in _find_active_runs(row_projection, threshold=row_threshold)
        if run[1] - run[0] + 1 >= 2
    ]
    meaningful_runs: list[tuple[int, int]] = []
    for run in runs:
        run_projection = row_projection[run[0] : run[1] + 1]
        max_dark_pixels = max(run_projection) if run_projection else 0
        width_ratio = float(max_dark_pixels) / max(1.0, float(crop_mask.width))
        run_height = run[1] - run[0] + 1
        abs_top = crop_top + run[0]
        if abs_top >= float(page_height) * 0.8 and run_height <= 6 and width_ratio > 0.72:
            continue
        meaningful_runs.append(run)
    if not meaningful_runs:
        return None
    return meaningful_runs[-1][1] + 1


def _should_use_pdf_example_marker_segmentation(
    markers: list[dict[str, Any]],
    text_lines: list[dict[str, Any]],
) -> bool:
    # A set-problem range is stronger evidence of an exam page than an
    # incidental line beginning with "ex".  Prefer numbered-problem/range
    # segmentation so strings such as "EXW" cannot consume the whole page as
    # a workbook example.
    if any(_extract_pdf_passage_range(line.get("text")) for line in text_lines):
        return False
    example_count = sum(1 for line in text_lines if _is_pdf_example_marker_text(line.get("text")))
    if example_count <= 0:
        return False
    if not markers:
        return True
    if len(markers) <= 1:
        return True
    section_like_count = sum(
        1 for marker in markers if _is_pdf_workbook_section_heading_text(marker.get("text"))
    )
    return section_like_count >= len(markers)


def _segment_pdf_example_markers(
    image: Image.Image,
    page_id: str,
    text_lines: list[dict[str, Any]],
) -> tuple[list[ContentBlock], dict[str, Any]] | None:
    if not text_lines:
        return None

    sorted_lines = sorted(
        (line for line in text_lines if isinstance(line, dict)),
        key=lambda line: (
            (_marker_bbox(line).top if _marker_bbox(line) else 0.0),
            (_marker_bbox(line).left if _marker_bbox(line) else 0.0),
        ),
    )
    example_lines = [
        line for line in sorted_lines if _is_pdf_example_marker_text(line.get("text"))
    ]
    if not example_lines:
        return None

    mask = _dark_mask(image, threshold=220)
    content_box = _find_document_content_box(mask, image.width, image.height)
    footer_top = _pdf_workbook_footer_top(sorted_lines, image.height)
    boundaries = _pdf_example_boundary_lines(sorted_lines)
    blocks: list[ContentBlock] = []
    page_area = _page_area_px(image.width, image.height)
    padding = max(16.0, float(image.height) * 0.012)

    for example_index, line in enumerate(example_lines, start=1):
        marker_box = _marker_bbox(line)
        if marker_box is None:
            continue
        next_boundaries = [
            top
            for top, boundary_line in boundaries
            if top > marker_box.top + max(28.0, marker_box.height * 0.8)
            and boundary_line is not line
        ]
        boundary_bottom = min(next_boundaries) - padding if next_boundaries else float(image.height)
        if footer_top is not None:
            boundary_bottom = min(boundary_bottom, footer_top - padding)

        top = max(0.0, marker_box.top - padding)
        rough_bottom = min(float(image.height), boundary_bottom)
        if rough_bottom - top < max(44.0, float(image.height) * 0.025):
            continue

        horizontal_padding = max(10.0, padding * 0.5)
        crop_left = max(content_box.left, marker_box.left - horizontal_padding)
        crop_right = min(content_box.right, float(image.width) - horizontal_padding)
        crop_box = (
            max(0, int(crop_left)),
            max(0, int(top)),
            min(image.width, int(crop_right)),
            min(image.height, int(rough_bottom)),
        )
        if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
            continue
        crop_mask = mask.crop(crop_box)
        meaningful_bottom = _pdf_example_meaningful_bottom(
            crop_mask,
            page_height=image.height,
            crop_top=crop_box[1],
        )
        if meaningful_bottom is not None:
            crop_mask = crop_mask.crop((0, 0, crop_mask.width, meaningful_bottom))
        ink_bbox = crop_mask.getbbox()
        if ink_bbox is None:
            continue

        left = max(0.0, float(crop_box[0] + ink_bbox[0]) - padding)
        right = min(float(image.width), float(crop_box[0] + ink_bbox[2]) + padding)
        bottom = min(float(image.height), float(crop_box[1] + ink_bbox[3]) + padding)
        if right - left < max(80.0, float(image.width) * 0.08):
            left = content_box.left
            right = content_box.right
        if bottom - top < max(60.0, float(image.height) * 0.04):
            continue

        box = Box.from_points(left, top, right, bottom)
        text = str(line.get("text") or "").strip()
        metadata = _enrich_block_segmentation_metadata(
            {
                "segmenter": "pdf-example-markers",
                "marker_kind": "example",
                "example_index": example_index,
                "question_band_index": example_index,
                "source_band_index": example_index,
                "force_problem_start": True,
                "force_image_record": True,
                "display_title": text[:120] or f"ex {example_index}",
            },
            segmentation_mode=SEGMENTATION_MODE_DOCUMENT,
            block_area=box.area,
            page_area=page_area,
            large_block_threshold=LARGE_BLOCK_AREA_RATIO,
            page_width=image.width,
            page_height=image.height,
        )
        blocks.append(
            ContentBlock(
                block_id=f"{page_id}-block-{len(blocks) + 1:03d}",
                block_type=BlockType.IMAGE,
                bbox=box,
                reading_order=len(blocks),
                text=None,
                confidence=1.0,
                metadata=metadata,
            )
        )

    if not blocks:
        return None
    return blocks, {
        "segmenter": "pdf-example-markers",
        "pdf_example_marker_count": len(example_lines),
        "document_split_block_count": len(blocks),
        "document_split_applied": True,
        "content_box_area_ratio": content_box.area / max(1.0, page_area),
    }


def _build_pdf_passage_range_blocks(
    image: Image.Image,
    page_id: str,
    text_lines: list[dict[str, Any]],
    column_entries: list[tuple[int, list[dict[str, Any]], tuple[float, float]]],
    *,
    page_area: float,
    start_index: int,
) -> list[ContentBlock]:
    blocks: list[ContentBlock] = []
    if not text_lines or not column_entries:
        return blocks

    seen_ranges: set[tuple[int, int, int]] = set()
    sorted_lines = sorted(
        (line for line in text_lines if isinstance(line, dict)),
        key=lambda line: (
            (_marker_bbox(line).top if _marker_bbox(line) else 0.0),
            (_marker_bbox(line).left if _marker_bbox(line) else 0.0),
        ),
    )
    for line_index, line in enumerate(sorted_lines):
        text = str(line.get("text") or "").strip()
        range_header = parse_shared_passage_range_header(text)
        header_box = _marker_bbox(line)
        raw_candidate = parse_passage_range_candidate(text)
        if range_header is None and raw_candidate is not None and header_box is not None:
            header_center_x = (header_box.left + header_box.right) / 2.0
            _column_index, left_bound, right_bound = _pdf_column_bounds_for_x(
                column_entries,
                header_center_x,
                image,
            )
            joined_parts = [text]
            previous_box = header_box
            same_column_line_count = 0
            for following_line in sorted_lines[line_index + 1:]:
                following_box = _marker_bbox(following_line)
                following_text = str(following_line.get("text") or "").strip()
                if following_box is None or not following_text:
                    continue
                following_center_x = (following_box.left + following_box.right) / 2.0
                max_gap = max(
                    24.0,
                    previous_box.height * 1.6,
                    following_box.height * 1.6,
                    float(image.height) * 0.012,
                )
                if following_box.top - previous_box.bottom > max_gap:
                    break
                if not left_bound - 8.0 <= following_center_x <= right_bound + 8.0:
                    continue
                if following_box.top < previous_box.top:
                    continue
                joined_parts.append(following_text)
                same_column_line_count += 1
                joined_text = " ".join(joined_parts)
                range_header = parse_shared_passage_range_header(joined_text)
                if range_header is not None:
                    text = joined_text
                    break
                previous_box = following_box
                if same_column_line_count >= 2:
                    break
        corrupted_header = range_header is None and passage_header_text_looks_corrupted(text)
        candidate = parse_passage_range_candidate(text) if corrupted_header else None
        if range_header is None and candidate is None:
            continue
        if header_box is None:
            continue
        start, end = (
            (range_header.start, range_header.end)
            if range_header is not None
            else (candidate[0], candidate[1])
        )
        seen_key = (start, end, int(round(header_box.top / 8.0)))
        if seen_key in seen_ranges:
            continue

        header_center_x = (header_box.left + header_box.right) / 2.0
        column_index, left_bound, right_bound = _pdf_column_bounds_for_x(
            column_entries,
            header_center_x,
            image,
        )
        # Passage reading order is column-major: finish the physical left
        # column, then continue at the top of the physical right column.  Do
        # not trust caller-provided indices here; recovered columns can arrive
        # in detection order on rotated/noisy pages.
        ordered_columns = sorted(
            column_entries,
            key=lambda entry: (float(entry[2][0]), float(entry[2][1]), entry[0]),
        )
        passage_divider_x: float | None = None
        if len(ordered_columns) == 2:
            passage_divider_x = (
                float(ordered_columns[0][2][1]) + float(ordered_columns[1][2][0])
            ) * 0.5
        current_column_markers = next(
            (
                markers
                for entry_column_index, markers, _bounds in ordered_columns
                if entry_column_index == column_index
            ),
            [],
        )
        preceding_numbers = [
            int(marker["number"])
            for marker in current_column_markers
            if isinstance(marker.get("number"), int)
            and (marker_box := _marker_bbox(marker)) is not None
            and marker_box.top < header_box.top
        ]
        # A standard shared-passage header precedes its first child question.
        # If the claimed range has already started in the same column, this
        # header is content inside that question rather than a new common
        # passage. This is the main guard against in-question ranges being
        # exported by the passage-only action.
        if preceding_numbers and preceding_numbers[-1] >= start:
            continue

        child_marker_numbers = sorted(
            {
                int(marker["number"])
                for _entry_column_index, markers, _bounds in ordered_columns
                for marker in markers
                if isinstance(marker.get("number"), int)
                and start <= int(marker["number"]) <= end
            }
        )
        if corrupted_header and len(child_marker_numbers) < 2 and header_box.top > image.height * 0.25:
            continue
        if corrupted_header:
            detection_confidence = 0.93 if len(child_marker_numbers) >= 2 else 0.82
        elif len(child_marker_numbers) >= 2:
            detection_confidence = range_header.confidence if range_header is not None else 0.98
        elif len(child_marker_numbers) == 1:
            detection_confidence = 0.94
        else:
            detection_confidence = 0.9
        header_column_position = next(
            (
                position
                for position, (entry_column_index, _markers, _bounds) in enumerate(ordered_columns)
                if entry_column_index == column_index
            ),
            0,
        )
        range_blocks_start = len(blocks)
        stopped_at_problem = False
        for fragment_index, (
            fragment_column_index,
            column_markers,
            (fragment_left, fragment_right),
        ) in enumerate(ordered_columns[header_column_position:], start=1):
            fragment_column_position = next(
                (
                    position
                    for position, (entry_column_index, _markers, _bounds) in enumerate(ordered_columns)
                    if entry_column_index == fragment_column_index
                ),
                0,
            )
            marker_boundaries: list[Box] = []
            for marker in column_markers:
                marker_box = _marker_bbox(marker)
                if marker_box is None:
                    continue
                if fragment_column_index == column_index and marker_box.top <= header_box.top:
                    continue
                marker_boundaries.append(marker_box)

            first_marker_top = min((box.top for box in marker_boundaries), default=float(image.height))
            if fragment_column_index == column_index:
                fragment_top = max(0.0, header_box.top - max(8.0, float(image.height) * 0.004))
            else:
                detected_top = _pdf_passage_column_text_top(
                    sorted_lines,
                    left_bound=fragment_left,
                    right_bound=fragment_right,
                    bottom=first_marker_top,
                    image_height=image.height,
                )
                if detected_top is None:
                    if marker_boundaries:
                        stopped_at_problem = True
                        break
                    continue
                fragment_top = detected_top

            if marker_boundaries:
                fragment_bottom = min(
                    float(image.height),
                    first_marker_top - max(14.0, float(image.height) * 0.007),
                )
                stopped_at_problem = True
            else:
                text_bottom = _pdf_passage_column_text_bottom(
                    sorted_lines,
                    left_bound=fragment_left,
                    right_bound=fragment_right,
                    top=fragment_top,
                    bottom=float(image.height),
                    image_height=image.height,
                )
                ink_bottom = _trim_pdf_passage_column_bottom_to_ink(
                    image,
                    left=fragment_left,
                    right=fragment_right,
                    top=fragment_top,
                    bottom=float(image.height),
                )
                fragment_bottom = max(text_bottom, ink_bottom) if text_bottom is not None else ink_bottom
                fragment_footer_tops = [
                    line_box.top
                    for candidate in sorted_lines
                    if (line_box := _marker_bbox(candidate)) is not None
                    and fragment_left - max(44.0, float(image.width) * 0.02)
                    <= (line_box.left + line_box.right) / 2.0
                    <= fragment_right + max(44.0, float(image.width) * 0.02)
                    and _looks_like_pdf_footer_text_line(
                        candidate.get("text"),
                        line_box,
                        image.height,
                    )
                ]
                if fragment_footer_tops:
                    fragment_bottom = min(
                        fragment_bottom,
                        min(fragment_footer_tops) - max(12.0, float(image.height) * 0.006),
                    )

            if fragment_bottom - fragment_top < max(40.0, float(image.height) * 0.02):
                if stopped_at_problem:
                    break
                continue

            fragment_text_lines = [
                candidate
                for candidate in sorted_lines
                if (candidate_box := _marker_bbox(candidate)) is not None
                and fragment_left <= (candidate_box.left + candidate_box.right) / 2.0 <= fragment_right
                and fragment_top <= (candidate_box.top + candidate_box.bottom) / 2.0 <= fragment_bottom
                and str(candidate.get("text") or "").strip()
                and not _looks_like_pdf_page_header_text_line(
                    candidate.get("text"),
                    candidate_box,
                    image.height,
                )
                and not _looks_like_pdf_footer_text_line(
                    candidate.get("text"),
                    candidate_box,
                    image.height,
                )
            ]
            fragment_text_boxes = [
                candidate_box
                for candidate in fragment_text_lines
                if (candidate_box := _marker_bbox(candidate)) is not None
            ]
            # Visual column bounds describe the body flow, but a shared-passage
            # heading can legitimately span beyond that column.  Keep every
            # text-layer line assigned to this fragment inside the source box;
            # otherwise a crisp crop can still lose the right end of a long
            # instruction line while receiving a misleadingly high image score.
            box_left = min(
                fragment_left,
                *(candidate_box.left for candidate_box in fragment_text_boxes),
            ) if fragment_text_boxes else fragment_left
            box_right = max(
                fragment_right,
                *(candidate_box.right for candidate_box in fragment_text_boxes),
            ) if fragment_text_boxes else fragment_right
            if passage_divider_x is not None:
                if fragment_column_position == 0:
                    box_right = min(
                        box_right,
                        passage_divider_x - PDF_PASSAGE_CENTER_DIVIDER_EXCLUSION_PX,
                    )
                elif fragment_column_position == 1:
                    box_left = max(
                        box_left,
                        passage_divider_x + PDF_PASSAGE_CENTER_DIVIDER_EXCLUSION_PX,
                    )
            box_bottom = max(
                fragment_bottom,
                *(
                    candidate_box.bottom + PDF_PASSAGE_TEXT_EDGE_PADDING_PX
                    for candidate_box in fragment_text_boxes
                ),
            ) if fragment_text_boxes else fragment_bottom
            box = Box.from_points(
                max(0.0, box_left),
                fragment_top,
                min(float(image.width), box_right),
                min(float(image.height), box_bottom),
            )
            fragment_texts = [
                str(candidate.get("text") or "").strip()
                for candidate in fragment_text_lines
            ]
            text_bounds_scores = [
                (
                    max(0.0, min(box.right, candidate_box.right) - max(box.left, candidate_box.left))
                    * max(0.0, min(box.bottom, candidate_box.bottom) - max(box.top, candidate_box.top))
                )
                / max(1.0, candidate_box.area)
                for candidate_box in fragment_text_boxes
            ]
            text_bounds_score = min(text_bounds_scores, default=1.0)
            metadata = _enrich_block_segmentation_metadata(
                {
                    "segmenter": "pdf-passage-range",
                    "column_index": fragment_column_index,
                    "question_band_index": 0,
                    "source_band_index": 0,
                    "marker_kind": "passage_range" if fragment_index == 1 else "passage_continuation",
                    "passage_range_start": start,
                    "passage_range_end": end,
                    "passage_range": {"start": start, "end": end},
                    "passage_fragment_index": fragment_index,
                    "passage_detection_confidence": detection_confidence,
                    "passage_detection_cue_language": (
                        range_header.cue_language if range_header is not None else "corrupted-text-layer"
                    ),
                    "passage_child_marker_numbers": child_marker_numbers,
                    "passage_text_line_count": len(fragment_texts),
                    "passage_text_character_count": sum(
                        len(re.sub(r"\s+", "", fragment_text))
                        for fragment_text in fragment_texts
                    ),
                    "passage_text_bounds_score": text_bounds_score,
                    "passage_center_divider_x": passage_divider_x,
                    "passage_center_divider_exclusion_px": (
                        PDF_PASSAGE_CENTER_DIVIDER_EXCLUSION_PX
                        if passage_divider_x is not None
                        else 0.0
                    ),
                    "display_title": text[:120],
                    "force_image_record": True,
                    "shared_passage": True,
                },
                segmentation_mode=SEGMENTATION_MODE_DOCUMENT,
                block_area=box.area,
                page_area=page_area,
                large_block_threshold=LARGE_BLOCK_AREA_RATIO,
                page_width=image.width,
                page_height=image.height,
            )
            blocks.append(
                ContentBlock(
                    block_id=f"{page_id}-block-{start_index + len(blocks):03d}",
                    block_type=BlockType.IMAGE,
                    bbox=box,
                    reading_order=0,
                    text=text[:180] if fragment_index == 1 else None,
                    confidence=1.0,
                    metadata=metadata,
                )
            )
            if stopped_at_problem:
                break

        range_fragment_count = len(blocks) - range_blocks_start
        range_blocks = blocks[range_blocks_start:]
        total_text_line_count = sum(
            int(block.metadata.get("passage_text_line_count") or 0)
            for block in range_blocks
        )
        total_text_character_count = sum(
            int(block.metadata.get("passage_text_character_count") or 0)
            for block in range_blocks
        )
        for block in range_blocks:
            block.metadata["passage_fragment_count"] = range_fragment_count
            block.metadata["passage_total_text_line_count"] = total_text_line_count
            block.metadata["passage_total_text_character_count"] = total_text_character_count
        seen_ranges.add(seen_key)

    return blocks


def _segment_pdf_passage_ranges_only(
    image: Image.Image,
    page_id: str,
    text_lines: list[dict[str, Any]],
) -> tuple[list[ContentBlock], dict[str, Any]] | None:
    if not any(_extract_pdf_passage_range(line.get("text")) for line in text_lines):
        return None
    visual_columns = _detect_pdf_visual_column_boxes(image)
    if len(visual_columns) == 2:
        column_entries = [
            (index + 1, [], (column.left, column.right))
            for index, column in enumerate(visual_columns)
        ]
    else:
        column_entries = [(1, [], (0.0, float(image.width)))]
    page_area = _page_area_px(image.width, image.height)
    blocks = _build_pdf_passage_range_blocks(
        image,
        page_id,
        text_lines,
        column_entries,
        page_area=page_area,
        start_index=1,
    )
    if not blocks:
        return None
    for index, block in enumerate(blocks):
        block.reading_order = index
    return blocks, {
        "segmenter": "pdf-passage-ranges",
        "column_count": len(column_entries),
        "visual_column_bounds_used": len(visual_columns) == 2,
        "pdf_passage_range_block_count": len(blocks),
        "document_split_block_count": len(blocks),
        "document_split_applied": True,
        "content_box_area_ratio": 1.0,
    }


def _segment_pdf_problem_markers(
    image: Image.Image,
    page_id: str,
    markers: list[dict[str, Any]],
    *,
    text_lines: list[dict[str, Any]] | None = None,
) -> tuple[list[ContentBlock], dict[str, Any]] | None:
    cleaned_markers: list[dict[str, Any]] = []
    ignored_tiny_markers: list[dict[str, Any]] = []
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        if not (
            isinstance(marker.get("number"), int)
            or marker.get("marker_kind") == "text_stem"
        ):
            continue
        marker_box = _marker_bbox(marker)
        if marker_box is None:
            continue
        if _is_tiny_hwp_layout_marker(marker, marker_box, image.height):
            ignored_tiny_markers.append(marker)
            continue
        cleaned_markers.append(marker)
    if not cleaned_markers:
        return None

    ignored_tiny_marker_ids = {id(marker) for marker in ignored_tiny_markers}
    column_reference_markers = cleaned_markers + ignored_tiny_markers
    column_entries, detected_column_count, visual_column_bounds_used = _assign_pdf_marker_columns(
        image,
        column_reference_markers,
    )
    if not column_entries:
        return None

    # A passage may contain a short numbered procedure (1., 2.) inside a
    # higher-numbered question. If the exam sequence resumes later on the
    # same page, treat that low-number reset as nested content.
    ordered_numbered_markers = [
        marker
        for _column_index, column_markers, _bounds in sorted(column_entries, key=lambda entry: entry[0])
        for marker in sorted(
            column_markers,
            key=lambda item: (_marker_bbox(item).top if _marker_bbox(item) else 0.0),
        )
        if isinstance(marker.get("number"), int)
    ]
    leading_main_sequence_ids: set[int] = set()
    expected_leading_number = 1
    for marker in ordered_numbered_markers:
        number = int(marker["number"])
        if number != expected_leading_number:
            break
        leading_main_sequence_ids.add(id(marker))
        expected_leading_number += 1
    # A real exam commonly starts at question 1 and reaches double digits on
    # the same page. The old nested-list guard discarded 1-3 as soon as it saw
    # question 10 later in the page. Only protect a sufficiently long leading
    # 1,2,3,4... run; short 1,2 procedure lists before a higher question remain
    # eligible for the nested-enumeration filter below.
    if expected_leading_number <= 4:
        leading_main_sequence_ids.clear()

    nested_enumeration_marker_ids: set[int] = set()
    highest_number = 0
    for marker_index, marker in enumerate(ordered_numbered_markers):
        number = int(marker["number"])
        if number <= 3 and id(marker) not in leading_main_sequence_ids:
            later_numbers = [
                int(candidate["number"])
                for candidate in ordered_numbered_markers[marker_index + 1 :]
                if isinstance(candidate.get("number"), int)
            ]
            high_sequence_follows = any(candidate >= 10 for candidate in later_numbers)
            resumed_sequence_follows = highest_number >= 4 and any(
                candidate > highest_number for candidate in later_numbers
            )
            if high_sequence_follows or resumed_sequence_follows:
                nested_enumeration_marker_ids.add(id(marker))
                continue
        highest_number = max(highest_number, number)

    page_area = _page_area_px(image.width, image.height)
    usable_text_lines = text_lines if isinstance(text_lines, list) else []
    blocks: list[ContentBlock] = []
    choice_bottom_trim_count = 0
    content_bottom_trim_count = 0
    choice_visual_tail_count = 0

    for column_index, column_markers, (left_bound, right_bound) in column_entries:
        usable_column_markers = [
            marker
            for marker in column_markers
            if id(marker) not in ignored_tiny_marker_ids
            and id(marker) not in nested_enumeration_marker_ids
        ]
        if not usable_column_markers:
            continue
        sorted_markers = sorted(
            usable_column_markers,
            key=lambda marker: (_marker_bbox(marker).top if _marker_bbox(marker) else 0.0),
        )
        for marker_index, marker in enumerate(sorted_markers, start=1):
            marker_box = _marker_bbox(marker)
            if marker_box is None:
                continue
            next_marker_box = (
                _marker_bbox(sorted_markers[marker_index])
                if marker_index < len(sorted_markers)
                else None
            )
            marker_padding_y = max(10.0, image.height * 0.006)
            top = max(0.0, marker_box.top - marker_padding_y)
            bottom = (
                max(top + 40.0, next_marker_box.top - max(8.0, image.height * 0.004))
                if next_marker_box is not None
                else float(image.height)
            )
            bottom = _trim_isolated_pdf_footer_bottom(
                image,
                left=left_bound,
                right=right_bound,
                top=top,
                bottom=bottom,
            )
            choice_trimmed = False
            choice_visual_tail_attached = False
            if usable_text_lines:
                bottom, choice_trimmed, choice_visual_tail_attached = _trim_pdf_problem_bottom_to_last_choice(
                    image,
                    usable_text_lines,
                    left=left_bound,
                    right=right_bound,
                    top=top,
                    bottom=bottom,
                )
                if choice_trimmed:
                    choice_bottom_trim_count += 1
                if choice_visual_tail_attached:
                    choice_visual_tail_count += 1
            if not choice_trimmed:
                bottom, content_trimmed = _trim_pdf_problem_bottom_to_ink(
                    image,
                    left=left_bound,
                    right=right_bound,
                    top=top,
                    bottom=bottom,
                )
                if content_trimmed:
                    content_bottom_trim_count += 1
            box = Box.from_points(left_bound, top, right_bound, min(float(image.height), bottom))
            raw_number = marker.get("number")
            number = int(raw_number) if isinstance(raw_number, int) else None
            marker_text = str(marker.get("text") or "")[:120]
            metadata = _enrich_block_segmentation_metadata(
                {
                    "segmenter": "pdf-text-markers",
                    "column_index": column_index,
                    "question_band_index": marker_index,
                    "source_band_index": marker_index,
                    **(
                        {
                            "pdf_problem_number": number,
                            "problem_number": number,
                            "problem_number_source": "pdf_text_marker",
                            "display_title": f"{number}.",
                        }
                        if number is not None
                        else {
                            "problem_number_source": "text_stem",
                            "display_title": marker_text,
                        }
                    ),
                    "force_problem_start": True,
                    "force_image_record": True,
                    "marker_kind": str(marker.get("marker_kind") or "number"),
                    "marker_text": marker_text,
                    "visual_column_bounds_used": visual_column_bounds_used,
                    "choice_bottom_trimmed": choice_trimmed,
                    "choice_visual_tail_attached": choice_visual_tail_attached,
                },
                segmentation_mode=SEGMENTATION_MODE_DOCUMENT,
                block_area=box.area,
                page_area=page_area,
                large_block_threshold=LARGE_BLOCK_AREA_RATIO,
                page_width=image.width,
                page_height=image.height,
            )
            blocks.append(
                ContentBlock(
                    block_id=f"{page_id}-block-{len(blocks) + 1:03d}",
                    block_type=BlockType.TITLE,
                    bbox=box,
                    reading_order=len(blocks),
                    text=f"{number}." if number is not None else marker_text,
                    confidence=1.0,
                    metadata=metadata,
                )
            )

    if not blocks:
        return None

    passage_range_blocks = _build_pdf_passage_range_blocks(
        image,
        page_id,
        usable_text_lines,
        column_entries,
        page_area=page_area,
        start_index=len(blocks) + 1,
    )
    blocks.extend(passage_range_blocks)

    blocks = sorted(
        blocks,
        key=lambda block: (
            int(block.metadata.get("column_index") or 0),
            int(block.metadata.get("question_band_index") or 0),
            block.bbox.top,
        ),
    )
    for index, block in enumerate(blocks):
        block.reading_order = index

    pre_question_text_regions = _pdf_pre_question_text_regions(
        image,
        usable_text_lines,
        column_entries,
        [
            marker
            for marker in ordered_numbered_markers
            if id(marker) not in nested_enumeration_marker_ids
        ],
    )

    metadata: dict[str, Any] = {
        "segmenter": "pdf-text-markers",
        "pdf_text_marker_count": len(cleaned_markers),
        "column_count": detected_column_count,
        "visual_column_bounds_used": visual_column_bounds_used,
        "pdf_choice_bottom_trim_count": choice_bottom_trim_count,
        "pdf_content_bottom_trim_count": content_bottom_trim_count,
        "pdf_choice_visual_tail_count": choice_visual_tail_count,
        "pdf_nested_enumeration_marker_count": len(nested_enumeration_marker_ids),
        "pdf_passage_range_block_count": len(passage_range_blocks),
        "pdf_pre_question_text_regions": pre_question_text_regions,
        "document_split_block_count": len(blocks),
        "document_split_applied": True,
        "content_box_area_ratio": 1.0,
    }
    if ignored_tiny_markers:
        ignored_numbers = [
            int(marker["number"])
            for marker in ignored_tiny_markers
            if isinstance(marker.get("number"), int)
        ]
        metadata["ignored_tiny_pdf_marker_count"] = len(ignored_tiny_markers)
        if ignored_numbers:
            metadata["ignored_tiny_pdf_marker_numbers"] = ignored_numbers

    return blocks, metadata


def _row_dark_projection(mask: Image.Image) -> list[int]:
    if np is not None:
        arr = np.asarray(mask, dtype=np.uint8)
        if arr.ndim == 2:
            return np.count_nonzero(arr == 255, axis=1).astype(int).tolist()
    return [
        int(mask.crop((0, row_index, mask.width, row_index + 1)).histogram()[255])
        for row_index in range(mask.height)
    ]


def _column_dark_projection(mask: Image.Image) -> list[int]:
    if np is not None:
        arr = np.asarray(mask, dtype=np.uint8)
        if arr.ndim == 2:
            return np.count_nonzero(arr == 255, axis=0).astype(int).tolist()
    return [
        int(mask.crop((column_index, 0, column_index + 1, mask.height)).histogram()[255])
        for column_index in range(mask.width)
    ]


def _find_active_runs(values: list[int], *, threshold: float) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index, value in enumerate(values):
        if value >= threshold:
            if run_start is None:
                run_start = index
            continue
        if run_start is not None:
            runs.append((run_start, index - 1))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(values) - 1))
    return runs


def _fit_document_slice_box(
    mask: Image.Image,
    parent_box: Box,
    slice_top: int,
    slice_bottom: int,
    options: SegmentOptions,
) -> Box | None:
    slice_top = max(0, int(slice_top))
    slice_bottom = min(int(parent_box.height), int(slice_bottom))
    if slice_bottom - slice_top < options.document_min_band_height_px:
        return None

    crop = mask.crop(
        (
            int(parent_box.left),
            int(parent_box.top + slice_top),
            int(parent_box.right),
            int(parent_box.top + slice_bottom),
        )
    )
    bbox = crop.getbbox()
    if bbox is None:
        return None

    padding = float(options.document_split_padding_px)
    left = parent_box.left + max(0.0, float(bbox[0]) - padding)
    top = parent_box.top + float(slice_top) + max(0.0, float(bbox[1]) - padding)
    right = parent_box.left + min(parent_box.width, float(bbox[2]) + padding)
    bottom = parent_box.top + float(slice_top) + min(float(slice_bottom - slice_top), float(bbox[3]) + padding)

    # minimum_width = parent_box.width * 0.7
    # if right - left < minimum_width:
    #     left = parent_box.left
    #     right = parent_box.right

    return Box.from_points(left, top, right, bottom).expanded(
        6.0,
        max_width=float(mask.width),
        max_height=float(mask.height),
    )


def _looks_like_question_start(mask: Image.Image, band_box: Box) -> bool:
    crop = mask.crop((int(band_box.left), int(band_box.top), int(band_box.right), int(band_box.bottom)))
    if crop.width <= 1 or crop.height <= 1:
        return False

    sample_height = min(crop.height, max(88, min(148, int(crop.height * 0.12))))
    top_crop = crop.crop((0, 0, crop.width, sample_height))
    if top_crop.getbbox() is None:
        return False

    column_projection = _column_dark_projection(top_crop)
    if not column_projection:
        return False

    max_score = max(column_projection)
    if max_score <= 0:
        return False

    active_runs = _find_active_runs(column_projection, threshold=max(2.0, max_score * 0.22))
    if len(active_runs) < 2:
        return False

    first_run = active_runs[0]
    second_run = active_runs[1]
    first_width = first_run[1] - first_run[0] + 1
    second_width = second_run[1] - second_run[0] + 1
    gap = second_run[0] - first_run[1] - 1

    if first_run[0] > crop.width * 0.05:
        return False
    if first_width > crop.width * 0.12:
        return False
    if gap < max(8, int(crop.width * 0.01)):
        return False
    if second_width < max(42, int(crop.width * 0.12)):
        return False

    # Reject the classic choice-row pattern. A "① 강자성체 ② 상자성체 ③ … ⑤ …"
    # row produces several narrow, regularly spaced ink runs that pass the
    # naïve first-run / second-run check above. Real problem starts ("1.",
    # "2.") are followed by ONE long stem run, not 4+ short runs. Counting
    # the short runs in the tail is a cheap, language-agnostic signal that
    # the band is a choices line and must not become a problem boundary.
    short_run_width_limit = max(24, int(crop.width * 0.14))
    short_tail_runs = sum(
        1
        for run in active_runs[1:]
        if (run[1] - run[0] + 1) <= short_run_width_limit
    )
    if short_tail_runs >= 4:
        return False

    right_side_density = sum(column_projection[second_run[0] :]) / max(1.0, float((crop.width - second_run[0]) * sample_height))
    return right_side_density >= 0.012


def _find_question_anchor_rows(
    mask: Image.Image,
    band_box: Box,
    row_projection: list[int],
    smoothed: list[float],
    options: SegmentOptions,
) -> list[int]:
    crop_height = int(band_box.height)
    crop_width = int(band_box.width)
    min_segment_height = max(options.document_min_band_height_px, int(crop_height * options.document_split_search_margin_ratio))
    if crop_height - (min_segment_height * 2) <= 32:
        return []

    step = 8
    window_height = min(crop_height, max(96, min(180, int(crop_height * 0.16))))
    anchors: list[int] = []
    for row_index in range(min_segment_height, crop_height - min_segment_height, step):
        anchor_box = Box.from_points(
            band_box.left,
            band_box.top + float(row_index),
            band_box.right,
            min(band_box.bottom, band_box.top + float(row_index + window_height)),
        )
        if not _looks_like_question_start(mask, anchor_box):
            continue

        gap_top = max(0, row_index - max(30, int(crop_height * 0.045)))
        gap_bottom = row_index
        if gap_bottom - gap_top < 6:
            continue
        gap_density = sum(row_projection[gap_top:gap_bottom]) / max(1.0, float((gap_bottom - gap_top) * crop_width))
        if gap_density > options.document_split_min_density_ratio * 1.55:
            continue

        local_start = max(min_segment_height, row_index - 90)
        local_end = max(local_start + 1, row_index - 8)
        if local_end <= local_start:
            continue
        local_min = min(smoothed[local_start:local_end])
        if local_min > max(smoothed) * 0.42:
            continue
        anchors.append(row_index)
    return anchors


def _find_closed_box_vertical_ranges(crop: Image.Image) -> list[tuple[int, int]]:
    """Detect vertical coordinate ranges of large rectangular closed contours in the crop.
    
    These ranges correspond to <보기> or reading passage boxes and should be protected
    from being split horizontally.
    """
    if cv2 is None or np is None:
        return []
    try:
        arr = np.asarray(crop, dtype=np.uint8)
        # Find external contours (outermost boxes)
        contours, _ = cv2.findContours(arr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        ranges: list[tuple[int, int]] = []
        crop_width, crop_height = crop.width, crop.height
        min_box_width = crop_width * 0.35  # Box spans at least 35% of the column width
        min_box_height = 42                # Box height is at least 42 pixels
        max_box_height = crop_height * 0.90 # Avoid page-chrome borders
        
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w >= min_box_width and h >= min_box_height and h <= max_box_height:
                ranges.append((y, y + h))
        return ranges
    except Exception:
        return []


def _find_document_split_row(mask: Image.Image, band_box: Box, options: SegmentOptions) -> int | None:
    crop = mask.crop((int(band_box.left), int(band_box.top), int(band_box.right), int(band_box.bottom)))
    if crop.width <= 1 or crop.height < options.document_recursive_split_min_height_px:
        return None

    # Detect vertical coordinate ranges of closed contours (<보기> boxes, reading passages)
    box_ranges = _find_closed_box_vertical_ranges(crop)

    row_projection = _row_dark_projection(crop)
    if not row_projection:
        return None

    smoothed = _smooth_projection(row_projection, max(4, options.document_projection_window_px // 2))
    if not smoothed:
        return None

    max_score = max(smoothed)
    if max_score <= 0:
        return None

    min_segment_height = max(options.document_min_band_height_px, int(crop.height * options.document_split_search_margin_ratio))
    if crop.height - (min_segment_height * 2) <= options.document_split_min_gap_run_px:
        return None

    anchor_rows = _find_question_anchor_rows(mask, band_box, row_projection, smoothed, options)
    if anchor_rows:
        anchor_row = anchor_rows[0]
        local_start = max(min_segment_height, anchor_row - 90)
        local_end = max(local_start + 1, anchor_row - 8)
        if local_end > local_start:
            local_offset = min(range(local_end - local_start), key=lambda idx: smoothed[local_start + idx])
            refined_row = local_start + local_offset
            if refined_row >= min_segment_height and crop.height - refined_row >= min_segment_height:
                # Closed-contour check for anchor-refined split row
                is_inside_box = False
                for box_top, box_bottom in box_ranges:
                    if (box_top - 4) < refined_row < (box_bottom + 4):
                        is_inside_box = True
                        break
                if not is_inside_box:
                    return refined_row

    valley_threshold = max(4.0, max_score * options.document_split_valley_ratio)
    search_start = min_segment_height
    search_end = crop.height - min_segment_height

    low_runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for row_index in range(search_start, search_end):
        if smoothed[row_index] <= valley_threshold:
            if run_start is None:
                run_start = row_index
            continue
        if run_start is not None:
            low_runs.append((run_start, row_index - 1))
            run_start = None
    if run_start is not None:
        low_runs.append((run_start, search_end - 1))

    candidates: list[tuple[float, int]] = []
    min_gap = options.document_split_min_gap_run_px
    for run_top, run_bottom in low_runs:
        run_length = run_bottom - run_top + 1
        if run_length < min_gap:
            continue

        split_row = (run_top + run_bottom) // 2
        if split_row < min_segment_height or crop.height - split_row < min_segment_height:
            continue

        # Closed-contour check for candidate split row
        is_inside_box = False
        for box_top, box_bottom in box_ranges:
            if (box_top - 4) < split_row < (box_bottom + 4):
                is_inside_box = True
                break
        if is_inside_box:
            continue

        top_density = sum(row_projection[:split_row]) / max(1.0, float(split_row * crop.width))
        bottom_density = sum(row_projection[split_row:]) / max(1.0, float((crop.height - split_row) * crop.width))
        if min(top_density, bottom_density) < options.document_split_min_density_ratio:
            continue

        valley_score = sum(smoothed[run_top : run_bottom + 1]) / max(1, run_length)
        valley_depth = 1.0 - min(1.0, valley_score / max_score)
        centrality = abs(split_row - (crop.height / 2.0)) / max(1.0, crop.height / 2.0)
        score = valley_depth * 2.2 + min(1.0, run_length / max(1, min_gap * 1.4)) - centrality * 0.45
        candidates.append((score, split_row))

    if not candidates:
        return None

    return max(candidates, key=lambda item: item[0])[1]


def _split_document_band_box(
    mask: Image.Image,
    band_box: Box,
    options: SegmentOptions,
    *,
    depth: int = 0,
) -> list[Box]:
    if depth >= options.document_recursive_split_max_depth:
        return [band_box]
    if band_box.height < options.document_recursive_split_min_height_px:
        return [band_box]

    split_row = _find_document_split_row(mask, band_box, options)
    if split_row is None:
        return [band_box]

    top_box = _fit_document_slice_box(mask, band_box, 0, split_row, options)
    bottom_box = _fit_document_slice_box(mask, band_box, split_row, int(band_box.height), options)
    if top_box is None or bottom_box is None:
        return [band_box]

    if top_box.area < band_box.area * 0.12 or bottom_box.area < band_box.area * 0.12:
        return [band_box]
    if not (_looks_like_question_start(mask, top_box) and _looks_like_question_start(mask, bottom_box)):
        return [band_box]

    return _split_document_band_box(mask, top_box, options, depth=depth + 1) + _split_document_band_box(
        mask,
        bottom_box,
        options,
        depth=depth + 1,
    )


DocumentColumnEntry = tuple[Box, int, int, int, bool]


def _split_grid_balance_candidate(
    mask: Image.Image,
    entry: DocumentColumnEntry,
    options: SegmentOptions,
) -> list[DocumentColumnEntry] | None:
    box, source_band_index, _split_index, _split_count, _grid_balance_split = entry
    if box.height < max(260.0, float(mask.height) * 0.24):
        return None

    split_row = _find_document_split_row(mask, box, options)
    if split_row is None:
        return None

    top_box = _fit_document_slice_box(mask, box, 0, split_row, options)
    bottom_box = _fit_document_slice_box(mask, box, split_row, int(box.height), options)
    if top_box is None or bottom_box is None:
        return None
    if top_box.height < 90.0 or bottom_box.height < 90.0:
        return None
    if top_box.area < box.area * 0.18 or bottom_box.area < box.area * 0.18:
        return None

    return [
        (top_box, source_band_index, 1, 2, True),
        (bottom_box, source_band_index, 2, 2, True),
    ]


def _balance_document_grid_columns(
    mask: Image.Image,
    columns: list[list[DocumentColumnEntry]],
    options: SegmentOptions,
) -> tuple[list[list[DocumentColumnEntry]], int]:
    """Balance obvious two-column worksheet grids.

    When one column is split into three question regions and the other into
    two, the missing region is usually a large merged block caused by light
    highlights, diagrams, or horizontal rules. Split the tallest candidate
    until column counts match, but keep the heuristic narrow so long single
    questions are not cut apart.
    """
    if len(columns) < 2:
        return columns, 0

    target_count = max(len(entries) for entries in columns)
    min_count = min(len(entries) for entries in columns)
    if target_count <= 1 or target_count > 4 or target_count - min_count > 2:
        return columns, 0

    balanced = [list(entries) for entries in columns]
    split_count = 0
    for column_index, entries in enumerate(balanced):
        while len(entries) < target_count:
            candidate_indexes = sorted(
                range(len(entries)),
                key=lambda idx: entries[idx][0].height,
                reverse=True,
            )
            replacement: list[DocumentColumnEntry] | None = None
            replacement_index: int | None = None
            for candidate_index in candidate_indexes:
                replacement = _split_grid_balance_candidate(mask, entries[candidate_index], options)
                if replacement is not None:
                    replacement_index = candidate_index
                    break

            if replacement is None or replacement_index is None:
                break

            entries[replacement_index : replacement_index + 1] = replacement
            split_count += 1

    return balanced, split_count


def _align_balanced_document_grid_rows(
    columns: list[list[DocumentColumnEntry]],
    *,
    content_box: Box,
    page_width: int,
    page_height: int,
) -> tuple[list[list[DocumentColumnEntry]], int]:
    if len(columns) < 2:
        return columns, 0
    if not columns or any(len(entries) != len(columns[0]) for entries in columns):
        return columns, 0

    row_count = len(columns[0])
    if row_count < 2 or row_count > 4:
        return columns, 0

    sorted_columns = [sorted(entries, key=lambda entry: (entry[0].top, entry[0].left)) for entries in columns]
    row_starts: list[float] = [content_box.top]
    spread_threshold = max(48.0, float(page_height) * 0.08)
    for row_index in range(1, row_count):
        starts = [entries[row_index][0].top for entries in sorted_columns]
        if max(starts) - min(starts) > spread_threshold:
            # A much earlier "start" in one column is usually the previous
            # question's answer choices leaking into the next row. Use the
            # later boundary so the next question crop does not inherit them.
            row_starts.append(max(starts))
        else:
            row_starts.append(min(starts))
    row_starts.append(content_box.bottom)

    aligned_columns: list[list[DocumentColumnEntry]] = []
    adjusted_count = 0
    for entries in sorted_columns:
        if not entries:
            aligned_columns.append(entries)
            continue

        column_left = max(0.0, min(entry[0].left for entry in entries))
        column_right = min(float(page_width), max(entry[0].right for entry in entries))
        aligned_entries: list[DocumentColumnEntry] = []
        for row_index, entry in enumerate(entries):
            box, source_band_index, split_index, split_count, grid_balance_split = entry
            top = row_starts[row_index]
            bottom = row_starts[row_index + 1]
            if bottom - top < 80.0:
                aligned_entries.append(entry)
                continue
            aligned_box = Box.from_points(column_left, top, column_right, bottom)
            if (
                abs(aligned_box.top - box.top) > 1.0
                or abs(aligned_box.bottom - box.bottom) > 1.0
                or abs(aligned_box.left - box.left) > 1.0
                or abs(aligned_box.right - box.right) > 1.0
            ):
                adjusted_count += 1
            aligned_entries.append((aligned_box, source_band_index, split_index, split_count, grid_balance_split))
        aligned_columns.append(aligned_entries)

    return aligned_columns, adjusted_count


def _colored_problem_marker_rows(image: Image.Image, column_box: Box, box: Box) -> list[int]:
    """Find teal/blue-green problem-number ink rows near the left edge.

    Many workbook scans use colored problem numbers while answer choices remain
    black. When projection-based bands split a question into "stem" and
    "choices", this gives us a cheap way to keep real starts separate while
    merging continuation fragments.
    """
    left = int(max(0.0, column_box.left))
    top = int(max(0.0, box.top))
    right = int(min(float(image.width), column_box.left + max(58.0, min(82.0, column_box.width * 0.22))))
    bottom = int(min(float(image.height), box.bottom))
    if right <= left or bottom <= top:
        return []

    data = image.crop((left, top, right, bottom)).convert("RGB").tobytes()
    width = right - left
    rows: list[int] = []
    for y in range(bottom - top):
        row_offset = y * width * 3
        hit = False
        for x in range(width):
            index = row_offset + (x * 3)
            r = data[index]
            g = data[index + 1]
            b = data[index + 2]
            if g >= 70 and b >= 70 and g > r + 22 and b > r + 10 and max(g, b) - r > 30:
                hit = True
                break
        if hit:
            rows.append(y)
    return rows


def _colored_problem_marker_component_offsets(image: Image.Image, column_box: Box, box: Box) -> list[int]:
    if cv2 is None or np is None:
        return []

    left = int(max(0.0, column_box.left))
    top = int(max(0.0, box.top))
    right = int(min(float(image.width), column_box.left + max(58.0, min(86.0, column_box.width * 0.24))))
    bottom = int(min(float(image.height), box.bottom))
    if right <= left or bottom <= top:
        return []

    rgb = np.array(image.crop((left, top, right, bottom)).convert("RGB"))
    red = rgb[:, :, 0].astype(np.int16)
    green = rgb[:, :, 1].astype(np.int16)
    blue = rgb[:, :, 2].astype(np.int16)
    marker_mask = (
        (green >= 70)
        & (blue >= 70)
        & (green > red + 22)
        & (blue > red + 10)
        & ((np.maximum(green, blue) - red) > 30)
    ).astype(np.uint8)
    if not marker_mask.any():
        return []

    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(marker_mask, 8)
    offsets: list[int] = []
    for component_index in range(1, component_count):
        x, y, width, height, area = (int(value) for value in stats[component_index])
        if area < 35 or area > 180:
            continue
        if width < 3 or height < 10:
            continue
        if x > 48 or width > 28 or height > 36:
            continue
        offsets.append(y)

    if not offsets:
        return []

    clustered: list[int] = []
    for offset in sorted(offsets):
        if not clustered or offset - clustered[-1] > 10:
            clustered.append(offset)
        else:
            clustered[-1] = min(clustered[-1], offset)
    return clustered


def _colored_problem_marker_offsets(image: Image.Image, column_box: Box, box: Box) -> list[int]:
    component_offsets = _colored_problem_marker_component_offsets(image, column_box, box)
    if component_offsets or (cv2 is not None and np is not None):
        return component_offsets

    # Fallback for environments without cv2/numpy component analysis.
    rows = _colored_problem_marker_rows(image, column_box, box)
    if len(rows) < 8:
        return []
    return [min(rows)]


def _dark_numeric_problem_marker_component_offsets(image: Image.Image, column_box: Box, box: Box) -> list[int]:
    if cv2 is None or np is None:
        return []

    left = int(max(0.0, column_box.left))
    top = int(max(0.0, box.top))
    right = int(min(float(image.width), column_box.left + max(58.0, min(92.0, column_box.width * 0.24))))
    bottom = int(min(float(image.height), box.bottom))
    if right <= left or bottom <= top:
        return []

    gray = np.array(image.crop((left, top, right, bottom)).convert("L"))
    marker_mask = (gray <= 115).astype(np.uint8)
    if not marker_mask.any():
        return []

    component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(marker_mask, 8)
    components: list[dict[str, int]] = []
    for component_index in range(1, component_count):
        x, y, width, height, area = (int(value) for value in stats[component_index])
        if area <= 0:
            continue
        components.append(
            {
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "area": area,
                "right": x + width,
                "bottom": y + height,
            }
        )

    digit_like = [
        component
        for component in components
        if component["x"] <= 50
        and 2 <= component["width"] <= 28
        and 6 <= component["height"] <= 46
        and 8 <= component["area"] <= 320
    ]
    dot_like = [
        component
        for component in components
        if component["x"] <= 66
        and 1 <= component["width"] <= 9
        and 1 <= component["height"] <= 9
        and 1 <= component["area"] <= 55
    ]
    if not digit_like or not dot_like:
        return []

    offsets: list[int] = []
    for dot in dot_like:
        candidate_digits = [
            digit
            for digit in digit_like
            if digit["right"] <= dot["x"] + 2
            and 1 <= dot["x"] - digit["right"] <= 18
            and dot["y"] >= digit["y"] + max(2, int(digit["height"] * 0.45))
            and dot["y"] <= digit["bottom"] + 8
        ]
        if not candidate_digits:
            continue
        digit = max(candidate_digits, key=lambda item: item["right"])
        offsets.append(min(digit["y"], dot["y"]))

    if not offsets:
        return []

    clustered: list[int] = []
    for offset in sorted(offsets):
        if not clustered or offset - clustered[-1] > 14:
            clustered.append(offset)
        else:
            clustered[-1] = min(clustered[-1], offset)
    return clustered


def _visual_problem_marker_offsets(image: Image.Image, column_box: Box, box: Box) -> list[int]:
    offsets = [
        *_colored_problem_marker_offsets(image, column_box, box),
        *_dark_numeric_problem_marker_component_offsets(image, column_box, box),
    ]
    if not offsets:
        return []
    clustered: list[int] = []
    for offset in sorted(offsets):
        if not clustered or offset - clustered[-1] > 14:
            clustered.append(offset)
        else:
            clustered[-1] = min(clustered[-1], offset)
    return clustered


def _problem_marker_start_limit(box: Box) -> float:
    return max(96.0, min(118.0, box.height * 0.58))


def _select_colored_problem_marker_offset(offsets: list[int], box: Box) -> float | None:
    start_offsets = [offset for offset in sorted(offsets) if offset <= _problem_marker_start_limit(box)]
    if not start_offsets:
        return None

    for index, offset in enumerate(start_offsets):
        if (
            offset < 40
            and index + 1 < len(start_offsets)
            and start_offsets[index + 1] - offset >= 18
        ):
            continue
        return float(offset)
    return float(start_offsets[-1])


def _entry_has_colored_problem_marker(image: Image.Image, column_box: Box, entry: DocumentColumnEntry) -> bool:
    box = entry[0]
    offsets = _visual_problem_marker_offsets(image, column_box, box)
    return _select_colored_problem_marker_offset(offsets, box) is not None


def _merge_box_pair(first: Box, second: Box) -> Box:
    return Box.from_points(
        min(first.left, second.left),
        min(first.top, second.top),
        max(first.right, second.right),
        max(first.bottom, second.bottom),
    )


def _merge_document_continuation_entries(
    image: Image.Image,
    columns: list[list[DocumentColumnEntry]],
    column_boxes: list[Box],
) -> tuple[list[list[DocumentColumnEntry]], int]:
    if not columns or len(columns) != len(column_boxes):
        return columns, 0

    adjusted_columns: list[list[DocumentColumnEntry]] = []
    embedded_marker_move_count = 0
    for column_box, entries in zip(column_boxes, columns):
        sorted_entries = sorted(entries, key=lambda entry: (entry[0].top, entry[0].left))
        adjusted_entries: list[DocumentColumnEntry] = []
        index = 0
        while index < len(sorted_entries):
            entry = sorted_entries[index]
            box, source_band_index, split_index, split_count, grid_balance_split = entry
            offsets = _visual_problem_marker_offsets(image, column_box, box)
            start_marker_limit = _problem_marker_start_limit(box)
            embedded_offsets = [offset for offset in offsets if offset > start_marker_limit]
            marker_offset = min(embedded_offsets) if embedded_offsets else None
            if (
                marker_offset is not None
                and marker_offset > start_marker_limit
                and index + 1 < len(sorted_entries)
            ):
                split_y = box.top + max(0.0, float(marker_offset) - 8.0)
                next_entry = sorted_entries[index + 1]
                next_box = next_entry[0]
                if split_y - box.top >= 44.0 and box.bottom - split_y >= 10.0:
                    upper_box = Box.from_points(box.left, box.top, box.right, split_y)
                    lower_box = Box.from_points(
                        min(box.left, next_box.left),
                        split_y,
                        max(box.right, next_box.right),
                        max(box.bottom, next_box.bottom),
                    )
                    adjusted_entries.append(
                        (upper_box, source_band_index, split_index, split_count, grid_balance_split)
                    )
                    sorted_entries[index + 1] = (
                        lower_box,
                        source_band_index,
                        1,
                        split_count + next_entry[3],
                        grid_balance_split or next_entry[4],
                    )
                    embedded_marker_move_count += 1
                    index += 1
                    continue
            adjusted_entries.append(entry)
            index += 1
        adjusted_columns.append(adjusted_entries)

    marker_offsets_by_column = [
        [_visual_problem_marker_offsets(image, column_box, entry[0]) for entry in entries]
        for column_box, entries in zip(column_boxes, adjusted_columns)
    ]
    marker_flags_by_column = []
    for entries, offsets_for_entries in zip(adjusted_columns, marker_offsets_by_column):
        marker_flags: list[bool] = []
        for entry, offsets in zip(entries, offsets_for_entries):
            marker_flags.append(_select_colored_problem_marker_offset(offsets, entry[0]) is not None)
        marker_flags_by_column.append(marker_flags)
    marker_count = sum(1 for flags in marker_flags_by_column for has_marker in flags if has_marker)
    if marker_count < 2:
        return columns, 0

    gap_threshold = max(34.0, float(image.height) * 0.04)
    max_merged_height = max(220.0, float(image.height) * 0.36)
    merged_columns: list[list[DocumentColumnEntry]] = []
    merge_count = embedded_marker_move_count

    for entries, marker_flags, marker_offsets in zip(
        adjusted_columns,
        marker_flags_by_column,
        marker_offsets_by_column,
    ):
        sorted_pairs = sorted(
            zip(entries, marker_flags, marker_offsets),
            key=lambda pair: (pair[0][0].top, pair[0][0].left),
        )
        normalized_pairs: list[tuple[DocumentColumnEntry, bool, float | None]] = []
        for entry, has_marker, offsets in sorted_pairs:
            box, source_band_index, split_index, split_count, grid_balance_split = entry
            selected_marker_offset = _select_colored_problem_marker_offset(offsets, box) if has_marker else None
            marker_top = box.top + selected_marker_offset if selected_marker_offset is not None else None
            if marker_top is not None:
                trimmed_top = max(box.top, marker_top)
                if trimmed_top - box.top >= 6.0 and box.bottom - trimmed_top >= 44.0:
                    box = Box.from_points(box.left, trimmed_top, box.right, box.bottom)
                    entry = (box, source_band_index, split_index, split_count, grid_balance_split)
            normalized_pairs.append((entry, has_marker, marker_top))

        merged_entries: list[DocumentColumnEntry] = []
        for pair_index, (entry, has_marker, _marker_top) in enumerate(normalized_pairs):
            box, source_band_index, split_index, split_count, grid_balance_split = entry
            if merged_entries and not has_marker:
                previous = merged_entries[-1]
                previous_box = previous[0]
                next_marker_top = (
                    normalized_pairs[pair_index + 1][2]
                    if pair_index + 1 < len(normalized_pairs)
                    else None
                )
                merge_box = box
                if next_marker_top is not None:
                    # Problem-level crops add their own padding later; stop
                    # the continuation far enough above the next marker that
                    # the final padded crop does not include the next number.
                    boundary = next_marker_top - 30.0
                    if boundary < merge_box.bottom and boundary - merge_box.top >= 24.0:
                        merge_box = Box.from_points(merge_box.left, merge_box.top, merge_box.right, boundary)
                gap = box.top - previous_box.bottom
                combined_height = max(previous_box.bottom, merge_box.bottom) - min(previous_box.top, merge_box.top)
                if gap <= gap_threshold and combined_height <= max_merged_height:
                    merged_entries[-1] = (
                        _merge_box_pair(previous_box, merge_box),
                        previous[1],
                        previous[2],
                        max(previous[3], previous[3] + split_count),
                        previous[4] or grid_balance_split,
                    )
                    merge_count += 1
                    continue
            merged_entries.append(entry)
        merged_columns.append(merged_entries)

    return merged_columns, merge_count


def _extend_terminal_document_entries_to_content_tail(
    mask: Image.Image,
    columns: list[list[DocumentColumnEntry]],
    column_boxes: list[Box],
    *,
    page_height: int,
) -> tuple[list[list[DocumentColumnEntry]], int]:
    if len(columns) != len(column_boxes):
        return columns, 0

    adjusted_columns: list[list[DocumentColumnEntry]] = []
    extension_count = 0
    for entries, column_box in zip(columns, column_boxes):
        if not entries:
            adjusted_columns.append(entries)
            continue

        adjusted_entries = list(entries)
        last_index = max(range(len(adjusted_entries)), key=lambda idx: (adjusted_entries[idx][0].top, adjusted_entries[idx][0].left))
        box, source_band_index, split_index, split_count, grid_balance_split = adjusted_entries[last_index]
        tail_top = int(min(column_box.bottom, max(box.bottom, box.top)))
        tail_bottom = int(column_box.bottom)
        if tail_bottom <= tail_top:
            adjusted_columns.append(adjusted_entries)
            continue

        crop = mask.crop((int(column_box.left), tail_top, int(column_box.right), tail_bottom))
        tail_bbox = crop.getbbox()
        if tail_bbox is None:
            adjusted_columns.append(adjusted_entries)
            continue

        tail_abs_top = float(tail_top + tail_bbox[1])
        tail_abs_bottom = float(tail_top + tail_bbox[3])
        tail_gap = tail_abs_top - box.bottom
        max_tail_gap = max(56.0, float(page_height) * 0.1)
        if tail_gap > max_tail_gap:
            adjusted_columns.append(adjusted_entries)
            continue

        tail_width_ratio = float(tail_bbox[2] - tail_bbox[0]) / max(1.0, float(crop.width))
        tail_height = float(tail_bbox[3] - tail_bbox[1])
        footer_like_tail = tail_width_ratio < 0.12 and tail_height <= max(24.0, float(page_height) * 0.035)
        if tail_abs_top >= float(page_height) * 0.86 and footer_like_tail:
            adjusted_columns.append(adjusted_entries)
            continue

        padding = max(16.0, float(page_height) * 0.018)
        left = min(box.left, float(column_box.left + tail_bbox[0]) - padding)
        right = max(box.right, float(column_box.left + tail_bbox[2]) + padding)
        new_bottom = min(column_box.bottom, tail_abs_bottom + padding)
        if new_bottom - box.bottom < 3.0:
            adjusted_columns.append(adjusted_entries)
            continue

        adjusted_entries[last_index] = (
            Box.from_points(
                max(0.0, left),
                box.top,
                min(float(mask.width), right),
                new_bottom,
            ),
            source_band_index,
            split_index,
            split_count,
            grid_balance_split,
        )
        extension_count += 1
        adjusted_columns.append(adjusted_entries)

    return adjusted_columns, extension_count


def _looks_like_two_visual_columns(
    entries: list[DocumentColumnEntry],
    content_box: Box,
    page_height: int,
) -> tuple[float, list[DocumentColumnEntry], list[DocumentColumnEntry]] | None:
    if len(entries) < 4:
        return None

    has_top_inversion = any(
        entries[index + 1][0].top + 4.0 < entries[index][0].top
        for index in range(len(entries) - 1)
    )
    if not has_top_inversion:
        return None

    centers = sorted(
        ((entry[0].left + entry[0].right) / 2.0, index, entry)
        for index, entry in enumerate(entries)
    )
    gaps = [
        (centers[index + 1][0] - centers[index][0], index)
        for index in range(len(centers) - 1)
    ]
    if not gaps:
        return None

    largest_gap, split_index = max(gaps, key=lambda item: item[0])
    if largest_gap < max(90.0, content_box.width * 0.18):
        return None

    left_entries = [entry for _center, _index, entry in centers[: split_index + 1]]
    right_entries = [entry for _center, _index, entry in centers[split_index + 1 :]]
    if len(left_entries) < 2 or len(right_entries) < 2:
        return None

    left_sorted = sorted(left_entries, key=lambda entry: (entry[0].top, entry[0].left))
    right_sorted = sorted(right_entries, key=lambda entry: (entry[0].top, entry[0].left))
    pair_count = min(len(left_sorted), len(right_sorted))
    row_tolerance = max(48.0, float(page_height) * 0.08)
    aligned_pairs = sum(
        1
        for index in range(pair_count)
        if abs(left_sorted[index][0].top - right_sorted[index][0].top) <= row_tolerance
    )
    if aligned_pairs < max(2, pair_count - 1):
        return None

    split_x = (centers[split_index][0] + centers[split_index + 1][0]) / 2.0
    if (
        split_x < content_box.left + content_box.width * 0.28
        or split_x > content_box.right - content_box.width * 0.28
    ):
        return None

    return split_x, left_sorted, right_sorted


def _reassign_single_column_entries_by_visual_columns(
    column_entry_groups: list[list[DocumentColumnEntry]],
    column_boxes: list[Box],
    *,
    content_box: Box,
    page_height: int,
) -> tuple[list[list[DocumentColumnEntry]], list[Box], bool]:
    """Recover two visual columns when the gutter detector missed a tight grid."""
    if len(column_entry_groups) != 1 or len(column_boxes) != 1:
        return column_entry_groups, column_boxes, False

    visual_columns = _looks_like_two_visual_columns(column_entry_groups[0], content_box, page_height)
    if visual_columns is None:
        return column_entry_groups, column_boxes, False

    split_x, left_entries, right_entries = visual_columns
    left_box = Box.from_points(content_box.left, content_box.top, split_x, content_box.bottom)
    right_box = Box.from_points(split_x, content_box.top, content_box.right, content_box.bottom)
    return [left_entries, right_entries], [left_box, right_box], True


def _is_image_upload_document_source(image_source: Any, metadata: dict[str, Any]) -> bool:
    source_type = str(metadata.get("source_type") or "").strip().lower()
    if source_type == "image":
        return True
    if metadata:
        return False
    source_path = getattr(image_source, "source_path", None) or getattr(image_source, "normalized_path", None)
    if not source_path:
        return False
    return str(source_path).lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"))


def _visual_marker_column_boxes(image: Image.Image, options: SegmentOptions) -> tuple[Image.Image, Box, list[Box]]:
    marker_mask = _dark_mask(image, min(options.document_dark_threshold, 190))
    content_box = _find_document_content_box(marker_mask, image.width, image.height)
    columns = _detect_document_columns(marker_mask, content_box, options)
    if not columns:
        columns = [content_box]
    return marker_mask, content_box, columns


def _visual_problem_marker_tops(image: Image.Image, column_box: Box) -> list[float]:
    offsets = _visual_problem_marker_offsets(image, column_box, column_box)
    tops: list[float] = []
    for offset in offsets:
        top = column_box.top + float(offset)
        if top < column_box.top - 1.0 or top > column_box.bottom + 1.0:
            continue
        if tops and top - tops[-1] <= 22.0:
            continue
        tops.append(top)
    return tops


def _fit_visual_problem_box(
    mask: Image.Image,
    column_box: Box,
    *,
    marker_top: float,
    boundary_bottom: float,
    page_width: int,
    page_height: int,
) -> Box | None:
    padding = max(16.0, float(page_height) * 0.015)
    top = max(0.0, marker_top - padding)
    bottom_limit = min(float(page_height), max(top + 1.0, boundary_bottom))
    crop_box = (
        max(0, int(column_box.left)),
        max(0, int(top)),
        min(page_width, int(column_box.right)),
        min(page_height, int(bottom_limit)),
    )
    if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
        return None

    crop = mask.crop(crop_box)
    ink_bbox = crop.getbbox()
    if ink_bbox is None:
        return None

    left = max(0.0, float(crop_box[0] + ink_bbox[0]) - padding)
    right = min(float(page_width), float(crop_box[0] + ink_bbox[2]) + padding)
    bottom = min(bottom_limit, float(crop_box[1] + ink_bbox[3]) + padding)
    if right - left < max(80.0, float(column_box.width) * 0.24):
        left = max(0.0, column_box.left)
        right = min(float(page_width), column_box.right)
    if bottom - top < max(52.0, float(page_height) * 0.035):
        return None
    return Box.from_points(left, top, right, bottom)


def _segment_visual_problem_markers(
    image: Image.Image,
    page_id: str,
    options: SegmentOptions,
) -> tuple[list[ContentBlock], dict[str, Any]] | None:
    marker_mask, content_box, columns = _visual_marker_column_boxes(image, options)
    marker_tops_by_column = [_visual_problem_marker_tops(image, column_box) for column_box in columns]
    marker_count = sum(len(tops) for tops in marker_tops_by_column)
    if marker_count < 2:
        return None

    blocks: list[ContentBlock] = []
    page_area = _page_area_px(image.width, image.height)
    boundary_padding = max(18.0, float(image.height) * 0.018)
    for column_index, (column_box, marker_tops) in enumerate(zip(columns, marker_tops_by_column), start=1):
        if not marker_tops:
            continue
        sorted_tops = sorted(marker_tops)

        valid_tops: list[float] = []
        for marker_index, marker_top in enumerate(sorted_tops):
            next_marker_top = sorted_tops[marker_index + 1] if marker_index + 1 < len(sorted_tops) else None
            probe_boundary_bottom = (
                next_marker_top - boundary_padding
                if next_marker_top is not None
                else column_box.bottom
            )
            if _fit_visual_problem_box(
                marker_mask,
                column_box,
                marker_top=marker_top,
                boundary_bottom=probe_boundary_bottom,
                page_width=image.width,
                page_height=image.height,
            ) is not None:
                valid_tops.append(marker_top)
        if not valid_tops:
            continue

        for marker_index, marker_top in enumerate(valid_tops, start=1):
            next_marker_top = valid_tops[marker_index] if marker_index < len(valid_tops) else None
            boundary_bottom = (
                next_marker_top - boundary_padding
                if next_marker_top is not None
                else column_box.bottom
            )
            box = _fit_visual_problem_box(
                marker_mask,
                column_box,
                marker_top=marker_top,
                boundary_bottom=boundary_bottom,
                page_width=image.width,
                page_height=image.height,
            )
            if box is None:
                continue
            metadata = _enrich_block_segmentation_metadata(
                {
                    "segmenter": "visual-problem-markers",
                    "column_index": column_index,
                    "question_band_index": marker_index,
                    "source_band_index": marker_index,
                    "visual_problem_marker": True,
                    "visual_problem_marker_top": round(marker_top, 2),
                    "force_problem_start": True,
                    "force_image_record": True,
                },
                segmentation_mode=SEGMENTATION_MODE_DOCUMENT,
                block_area=box.area,
                page_area=page_area,
                large_block_threshold=LARGE_BLOCK_AREA_RATIO,
                page_width=image.width,
                page_height=image.height,
            )
            blocks.append(
                ContentBlock(
                    block_id=f"{page_id}-block-{len(blocks) + 1:03d}",
                    block_type=BlockType.IMAGE,
                    bbox=box,
                    reading_order=len(blocks),
                    confidence=1.0,
                    metadata=metadata,
                )
            )

    if len(blocks) < 2:
        return None

    return blocks, {
        "segmenter": "visual-problem-markers",
        "visual_problem_marker_count": marker_count,
        "document_split_block_count": len(blocks),
        "document_split_applied": True,
        "column_count": len(columns),
        "content_box": {
            "left": content_box.left,
            "top": content_box.top,
            "width": content_box.width,
            "height": content_box.height,
        },
        "content_box_area_ratio": content_box.area / max(1.0, page_area),
    }


def _segment_document_page(image: Image.Image, page_id: str, options: SegmentOptions) -> tuple[list[ContentBlock], dict[str, Any]]:
    mask = _dark_mask(image, options.document_dark_threshold)
    content_box = _find_document_content_box(mask, image.width, image.height)
    columns = _detect_document_columns(mask, content_box, options)
    blocks: list[ContentBlock] = []
    page_area = _page_area_px(image.width, image.height)

    total_split_count = 0
    row_band_count = 0
    column_entry_groups: list[list[DocumentColumnEntry]] = []
    for column_index, column_box in enumerate(columns, start=1):
        row_bands = _find_document_row_bands(mask, column_box, options)
        row_bands = _merge_small_document_bands(row_bands, options)
        row_band_count += len(row_bands)
        column_entries: list[DocumentColumnEntry] = []
        for source_band_index, band in enumerate(row_bands, start=1):
            box = _document_band_box(mask, column_box, band, options)
            split_boxes = _split_document_band_box(mask, box, options)
            total_split_count += max(0, len(split_boxes) - 1)
            for local_split_index, split_box in enumerate(split_boxes, start=1):
                column_entries.append((split_box, source_band_index, local_split_index, len(split_boxes), False))

        column_entry_groups.append(column_entries)

    column_entry_groups, continuation_merge_count = _merge_document_continuation_entries(
        image,
        column_entry_groups,
        columns,
    )
    column_entry_groups, balance_split_count = _balance_document_grid_columns(mask, column_entry_groups, options)
    total_split_count += balance_split_count
    if continuation_merge_count:
        row_alignment_count = 0
    else:
        column_entry_groups, row_alignment_count = _align_balanced_document_grid_rows(
            column_entry_groups,
            content_box=content_box,
            page_width=image.width,
            page_height=image.height,
        )
    column_entry_groups, terminal_tail_extension_count = _extend_terminal_document_entries_to_content_tail(
        mask,
        column_entry_groups,
        columns,
        page_height=image.height,
    )
    column_entry_groups, columns, visual_column_reassign_applied = _reassign_single_column_entries_by_visual_columns(
        column_entry_groups,
        columns,
        content_box=content_box,
        page_height=image.height,
    )

    for column_index, column_entries in enumerate(column_entry_groups, start=1):
        for band_index, (box, source_band_index, split_index, split_count, grid_balance_split) in enumerate(column_entries, start=1):
            metadata = _enrich_block_segmentation_metadata(
                {
                    "segmenter": "document-bands",
                    "column_index": column_index,
                    "question_band_index": band_index,
                    "source_band_index": source_band_index,
                    "split_from_band": split_count > 1,
                    "band_split_index": split_index,
                    "band_split_count": split_count,
                    "grid_balance_split": grid_balance_split,
                },
                segmentation_mode=SEGMENTATION_MODE_DOCUMENT,
                block_area=box.area,
                page_area=page_area,
                large_block_threshold=LARGE_BLOCK_AREA_RATIO,
                page_width=image.width,
                page_height=image.height,
            )
            blocks.append(
                ContentBlock(
                    block_id=f"{page_id}-block-{len(blocks) + 1:03d}",
                    block_type=BlockType.STEM,
                    bbox=box,
                    reading_order=len(blocks),
                    metadata=metadata,
                )
            )

    if not blocks:
        fallback_metadata = _enrich_block_segmentation_metadata(
            {
                "segmenter": "document-bands",
                "fallback_reason": "empty_document_segmentation",
            },
            segmentation_mode=SEGMENTATION_MODE_DOCUMENT,
            block_area=content_box.area,
            page_area=page_area,
            large_block_threshold=LARGE_BLOCK_AREA_RATIO,
            page_width=image.width,
            page_height=image.height,
        )
        blocks = [
            ContentBlock(
                block_id=f"{page_id}-block-001",
                block_type=BlockType.IMAGE,
                bbox=content_box,
                reading_order=0,
                metadata=fallback_metadata,
            )
        ]

    return blocks, {
        "segmenter": "document-bands",
        "content_box": {
            "left": content_box.left,
            "top": content_box.top,
            "width": content_box.width,
            "height": content_box.height,
        },
        "column_count": len(columns),
        "document_band_split_count": total_split_count,
        "document_column_balance_split_count": balance_split_count,
        "document_continuation_merge_count": continuation_merge_count,
        "document_grid_row_alignment_count": row_alignment_count,
        "document_terminal_tail_extension_count": terminal_tail_extension_count,
        "document_visual_column_reassign_applied": visual_column_reassign_applied,
        "document_row_band_count": row_band_count,
        "document_split_block_count": len(blocks),
        "document_split_applied": total_split_count > 0,
        "content_box_area_ratio": round(content_box.area / page_area, 6),
    }


def _find_candidate_boxes_cv2(image: Image.Image, region: Box, options: SegmentOptions) -> list[tuple[Box, float]]:
    if cv2 is None or np is None:
        return []

    region_image = image.crop((int(region.left), int(region.top), int(region.right), int(region.bottom)))
    region_width, region_height = region_image.size
    if region_width <= 1 or region_height <= 1:
        return []

    gray = _pil_to_gray_array(region_image)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(15, region_width // 30), max(3, region_height // 220)))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, region_width // 220), max(15, region_height // 30)))
    grouped = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, horizontal_kernel)
    grouped = cv2.bitwise_or(grouped, cv2.morphologyEx(binary, cv2.MORPH_CLOSE, vertical_kernel))

    contours, _ = cv2.findContours(grouped, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = region_width * region_height * options.min_area_ratio
    candidates: list[tuple[Box, float]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if area < min_area:
            continue
        if w >= region_width * options.ignore_large_border_ratio and h >= region_height * options.ignore_large_border_ratio:
            continue
        crop = binary[y : y + h, x : x + w]
        fill_ratio = float(np.count_nonzero(crop)) / float(crop.size) if crop.size else 0.0
        if fill_ratio < options.min_fill_ratio:
            continue
        candidates.append((Box(left=region.left + float(x), top=region.top + float(y), width=float(w), height=float(h)), fill_ratio))
    return candidates


def _find_candidate_boxes_pil(image: Image.Image, region: Box, options: SegmentOptions) -> list[tuple[Box, float]]:
    region_image = image.crop((int(region.left), int(region.top), int(region.right), int(region.bottom)))
    region_width, region_height = region_image.size
    if region_width <= 1 or region_height <= 1:
        return []

    gray = ImageOps.autocontrast(ImageOps.grayscale(region_image))
    stat = ImageStat.Stat(gray)
    threshold = int(max(120.0, min(235.0, stat.mean[0] + stat.stddev[0] * 0.85)))
    bright_mask = gray.point(lambda px: 255 if px >= threshold else 0, mode="L")

    def build_bands(min_row_pixels: int) -> list[tuple[int, int]]:
        row_counts: list[int] = []
        for row_index in range(region_height):
            row = bright_mask.crop((0, row_index, region_width, row_index + 1))
            row_counts.append(int(row.histogram()[255]))

        bands: list[tuple[int, int]] = []
        band_start: int | None = None
        last_active: int | None = None
        for row_index, count in enumerate(row_counts):
            if count >= min_row_pixels:
                if band_start is None:
                    band_start = row_index
                last_active = row_index
                continue
            if band_start is not None and last_active is not None and row_index - last_active <= options.fallback_band_gap_px:
                continue
            if band_start is not None and last_active is not None:
                bands.append((band_start, last_active))
            band_start = None
            last_active = None
        if band_start is not None and last_active is not None:
            bands.append((band_start, last_active))
        return bands

    bands = build_bands(max(2, int(region_width * options.fallback_row_density)))
    if not bands:
        bands = build_bands(max(2, int(region_width * options.fallback_row_density * 0.5)))

    candidates: list[tuple[Box, float]] = []
    min_area = region_width * region_height * max(options.min_area_ratio, 0.001)
    for band_top, band_bottom in bands:
        if band_bottom - band_top + 1 < options.fallback_min_band_height_px:
            continue
        band_mask = bright_mask.crop((0, band_top, region_width, band_bottom + 1))
        bbox = band_mask.getbbox()
        if bbox is None:
            continue
        left, top, right, bottom = bbox
        left = max(0, left - options.fallback_padding_px)
        top = max(0, top - options.fallback_padding_px)
        right = min(region_width, right + options.fallback_padding_px)
        bottom = min(region_height, bottom + options.fallback_padding_px)
        box = Box(left=region.left + float(left), top=region.top + float(top), width=float(max(0, right - left)), height=float(max(0, bottom - top)))
        if box.area < min_area:
            continue
        crop = bright_mask.crop((left, top, right, bottom))
        fill_ratio = float(crop.histogram()[255]) / float(crop.width * crop.height) if crop.width and crop.height else 0.0
        if fill_ratio < options.min_fill_ratio / 3.0:
            continue
        candidates.append((box, fill_ratio))

    if len(candidates) > 1:
        filtered = [
            item
            for item in candidates
            if item[0].area < region_width * region_height * 0.75 and item[1] >= 0.22
        ]
        if filtered:
            candidates = filtered

    if len(candidates) > 1:
        region_area = region_width * region_height
        contained_large_candidates = []
        for index, (box, fill_ratio) in enumerate(candidates):
            contains_other = False
            for other_index, (other_box, _) in enumerate(candidates):
                if index == other_index:
                    continue
                if (
                    box.left <= other_box.left
                    and box.top <= other_box.top
                    and box.right >= other_box.right
                    and box.bottom >= other_box.bottom
                ):
                    contains_other = True
                    break
            if contains_other and box.area >= region_area * 0.08:
                contained_large_candidates.append((box, fill_ratio))
        if contained_large_candidates and len(candidates) - len(contained_large_candidates) >= 1:
            candidates = [item for item in candidates if item not in contained_large_candidates]

    return candidates


def _find_candidate_boxes(image: Image.Image, region: Box, options: SegmentOptions) -> list[tuple[Box, float]]:
    cv2_candidates = _find_candidate_boxes_cv2(image, region, options)
    pil_candidates = _find_candidate_boxes_pil(image, region, options)

    if not cv2_candidates:
        return pil_candidates or [(Box(left=region.left, top=region.top, width=region.width, height=region.height), 1.0)]

    if len(cv2_candidates) == 1:
        candidate_box, _ = cv2_candidates[0]
        if candidate_box.area >= region.area * 0.7 and pil_candidates:
            pil_area = max(box.area for box, _ in pil_candidates)
            if pil_area < candidate_box.area * 0.95:
                return pil_candidates

    if len(pil_candidates) > len(cv2_candidates) and pil_candidates:
        return pil_candidates

    return cv2_candidates


def _split_large_candidate_box(image: Image.Image, box: Box, options: SegmentOptions) -> list[Box]:
    if box.height < max(260.0, image.height * 0.14):
        return [box]

    crop = image.crop((int(box.left), int(box.top), int(box.right), int(box.bottom)))
    gray = ImageOps.autocontrast(ImageOps.grayscale(crop))
    stat = ImageStat.Stat(gray)
    threshold = int(max(120.0, min(235.0, stat.mean[0] + stat.stddev[0] * 0.8)))
    mask = gray.point(lambda px: 255 if px >= threshold else 0, mode="L")

    row_counts: list[int] = []
    for row_index in range(mask.height):
        row = mask.crop((0, row_index, mask.width, row_index + 1))
        row_counts.append(int(row.histogram()[255]))

    if not row_counts:
        return [box]

    window = 7
    smooth_counts: list[float] = []
    for index in range(len(row_counts)):
        start = max(0, index - window)
        end = min(len(row_counts), index + window + 1)
        smooth_counts.append(sum(row_counts[start:end]) / max(1, end - start))

    search_start = int(len(smooth_counts) * 0.15)
    search_end = max(search_start + 1, int(len(smooth_counts) * 0.85))
    segment = smooth_counts[search_start:search_end]
    if not segment:
        return [box]

    split_offset = min(range(len(segment)), key=lambda idx: segment[idx])
    split_row = search_start + split_offset
    max_count = max(smooth_counts)
    min_count = smooth_counts[split_row]
    # A valley at 82 % of the peak is already a meaningful gap between two
    # content regions (e.g. question stem and its diagram or next question).
    if max_count <= 0 or min_count > max_count * 0.82:
        return [box]
    if split_row < options.fallback_min_band_height_px or len(smooth_counts) - split_row < options.fallback_min_band_height_px:
        return [box]

    top_box = Box.from_points(box.left, box.top, box.right, box.top + split_row)
    bottom_box = Box.from_points(box.left, box.top + split_row, box.right, box.bottom)
    if top_box.area < box.area * 0.1 or bottom_box.area < box.area * 0.1:
        return [box]
    return [top_box, bottom_box]


def _merge_boxes(boxes: list[Box], options: SegmentOptions) -> list[Box]:
    merged = sorted(boxes, key=lambda item: (item.top, item.left))
    changed = True
    while changed:
        changed = False
        next_boxes: list[Box] = []
        while merged:
            current = merged.pop(0)
            i = 0
            while i < len(merged):
                other = merged[i]
                current_contains_other = (
                    current.left <= other.left
                    and current.top <= other.top
                    and current.right >= other.right
                    and current.bottom >= other.bottom
                )
                other_contains_current = (
                    other.left <= current.left
                    and other.top <= current.top
                    and other.right >= current.right
                    and other.bottom >= current.bottom
                )
                if (current_contains_other or other_contains_current) and max(current.area, other.area) >= min(current.area, other.area) * 1.4:
                    i += 1
                    continue
                vertical_overlap = min(current.bottom, other.bottom) - max(current.top, other.top)
                horizontal_overlap = min(current.right, other.right) - max(current.left, other.left)
                near_same_line = abs(other.top - current.top) <= options.max_merge_gap_y_px
                stacked = 0 <= other.top - current.bottom <= options.max_merge_gap_y_px and horizontal_overlap > -options.max_merge_gap_x_px
                side_by_side = 0 <= other.left - current.right <= options.max_merge_gap_x_px and vertical_overlap > -options.max_merge_gap_y_px
                overlapping = vertical_overlap > 0 or horizontal_overlap > 0
                if overlapping or near_same_line or stacked or side_by_side:
                    current = Box.from_points(
                        min(current.left, other.left),
                        min(current.top, other.top),
                        max(current.right, other.right),
                        max(current.bottom, other.bottom),
                    )
                    merged.pop(i)
                    changed = True
                    continue
                i += 1
            next_boxes.append(current)
        merged = sorted(next_boxes, key=lambda item: (item.top, item.left))
    return merged


def _classify_geometry(image: Image.Image, box: Box, board_region: Box, fill_ratio: float) -> tuple[BlockType, dict[str, float]]:
    crop = image.crop((int(box.left), int(box.top), int(box.right), int(box.bottom))).convert("L")
    stat = ImageStat.Stat(crop)
    mean_intensity = float(stat.mean[0])
    stddev = float(stat.stddev[0])
    aspect_ratio = box.width / max(box.height, 1.0)
    board_area = max(board_region.area, 1.0)
    area_ratio = box.area / board_area

    metadata = {
        "fill_ratio": round(fill_ratio, 4),
        "mean_intensity": round(mean_intensity, 2),
        "stddev": round(stddev, 2),
        "aspect_ratio": round(aspect_ratio, 4),
        "area_ratio": round(area_ratio, 6),
    }

    relative_top = (box.top - board_region.top) / max(board_region.height, 1.0)
    if relative_top < 0.12 and aspect_ratio > 2.0:
        return BlockType.TITLE, metadata
    if area_ratio > 0.18 and stddev > 35:
        return BlockType.IMAGE, metadata
    if aspect_ratio > 4.5 and box.height < board_region.height * 0.08:
        return BlockType.FORMULA, metadata
    if area_ratio < 0.01 and box.height < board_region.height * 0.09:
        return BlockType.NOTE, metadata
    return BlockType.STEM, metadata


def segment_page(
    image_path: str | Path | Image.Image | Any,
    *,
    page_id: str,
    subject: Subject = Subject.UNKNOWN,
    options: SegmentOptions | None = None,
) -> PageModel:
    resolved_options = options or SegmentOptions()
    image = _load_image(image_path)
    page_area = _page_area_px(image.width, image.height)
    if _is_document_like_page(image_path, image):
        source_metadata = _source_metadata(image_path)
        marker_result = None
        raw_text_lines = source_metadata.get("pdf_text_lines")
        usable_text_lines = raw_text_lines if isinstance(raw_text_lines, list) else []
        raw_markers = source_metadata.get("pdf_problem_markers")
        if isinstance(raw_markers, list):
            if _should_use_pdf_example_marker_segmentation(raw_markers, usable_text_lines):
                marker_result = _segment_pdf_example_markers(
                    image,
                    page_id,
                    usable_text_lines,
                )
            if marker_result is None:
                marker_result = _segment_pdf_problem_markers(
                    image,
                    page_id,
                    raw_markers,
                    text_lines=usable_text_lines if usable_text_lines else None,
                )
        if marker_result is None and usable_text_lines:
            marker_result = _segment_pdf_passage_ranges_only(
                image,
                page_id,
                usable_text_lines,
            )
        if marker_result is None and _is_image_upload_document_source(image_path, source_metadata):
            marker_result = _segment_visual_problem_markers(image, page_id, resolved_options)
        if marker_result is not None:
            blocks, metadata = marker_result
        else:
            blocks, metadata = _segment_document_page(image, page_id, resolved_options)
        source_path = getattr(image_path, "normalized_path", None) or getattr(image_path, "source_path", None)
        if source_path is None and not isinstance(image_path, Image.Image):
            source_path = str(image_path)
        metadata = _build_segmentation_metadata(
            page_width=image.width,
            page_height=image.height,
            blocks=blocks,
            segmentation_mode=SEGMENTATION_MODE_DOCUMENT,
            segmenter=str(metadata.get("segmenter") or "document-bands"),
            base_metadata=metadata,
        )
        return PageModel(
            page_id=page_id,
            width_px=image.width,
            height_px=image.height,
            subject=subject,
            source_path=source_path,
            blocks=blocks,
            metadata=metadata,
        )

    board_region = _detect_board_region(image, resolved_options)
    candidates = _find_candidate_boxes(image, board_region, resolved_options)
    candidate_fallback_full_region = (
        len(candidates) == 1
        and abs(candidates[0][0].left - board_region.left) < 1.0
        and abs(candidates[0][0].top - board_region.top) < 1.0
        and abs(candidates[0][0].width - board_region.width) < 1.0
        and abs(candidates[0][0].height - board_region.height) < 1.0
        and float(candidates[0][1]) >= 0.999
    )
    candidate_count = len(candidates)
    expanded_candidates: list[tuple[Box, float]] = []
    split_applied = False
    for box, fill_ratio in candidates:
        split_boxes = _split_large_candidate_box(image, box, resolved_options)
        if len(split_boxes) > 1:
            split_applied = True
            for split_box in split_boxes:
                expanded_candidates.append((split_box, fill_ratio))
        else:
            expanded_candidates.append((box, fill_ratio))
    candidates = expanded_candidates
    merged_boxes = [box for box, _ in candidates] if split_applied else _merge_boxes([box for box, _ in candidates], resolved_options)
    expanded_candidate_count = len(candidates)
    merged_candidate_count = len(merged_boxes)

    blocks: list[ContentBlock] = []
    for index, box in enumerate(sorted(merged_boxes, key=lambda item: (item.top, item.left))):
        fill_ratio = next(
            (
                candidate_fill_ratio
                for candidate_box, candidate_fill_ratio in candidates
                if abs(candidate_box.left - box.left) < 2 and abs(candidate_box.top - box.top) < 2
            ),
            0.25,
        )
        block_type, metadata = _classify_geometry(image, box, board_region, fill_ratio)
        block_metadata = {
            **metadata,
            "segmenter": "rule-based",
        }
        if candidate_fallback_full_region:
            block_metadata["fallback_reason"] = "candidate_detection_failed"
        metadata = _enrich_block_segmentation_metadata(
            block_metadata,
            segmentation_mode=SEGMENTATION_MODE_BOARD,
            block_area=box.area,
            page_area=page_area,
            large_block_threshold=LARGE_BLOCK_AREA_RATIO,
            page_width=image.width,
            page_height=image.height,
        )
        blocks.append(
            ContentBlock(
                block_id=f"{page_id}-block-{index + 1:03d}",
                block_type=block_type,
                bbox=box,
                reading_order=index,
                metadata=metadata,
            )
        )

    if not blocks:
        fallback_metadata = _enrich_block_segmentation_metadata(
            {
                "fill_ratio": 1.0,
                "fallback_reason": "empty_segmentation",
                "segmenter": "rule-based",
            },
            segmentation_mode=SEGMENTATION_MODE_BOARD,
            block_area=board_region.area,
            page_area=page_area,
            large_block_threshold=LARGE_BLOCK_AREA_RATIO,
            page_width=image.width,
            page_height=image.height,
        )
        blocks = [
            ContentBlock(
                block_id=f"{page_id}-block-001",
                block_type=BlockType.IMAGE,
                bbox=board_region,
                reading_order=0,
                metadata=fallback_metadata,
            )
        ]

    source_path = getattr(image_path, "normalized_path", None) or getattr(image_path, "source_path", None)
    if source_path is None and not isinstance(image_path, Image.Image):
        source_path = str(image_path)

    metadata = _build_segmentation_metadata(
        page_width=image.width,
        page_height=image.height,
        blocks=blocks,
        segmentation_mode=SEGMENTATION_MODE_BOARD,
        segmenter="rule-based",
        base_metadata={
            "board_region": {
                "left": board_region.left,
                "top": board_region.top,
                "width": board_region.width,
                "height": board_region.height,
            },
            "board_region_area_ratio": round(board_region.area / page_area, 6),
            "candidate_count": candidate_count,
            "expanded_candidate_count": expanded_candidate_count,
            "merged_candidate_count": merged_candidate_count,
            "split_applied": split_applied,
            **(
                {"fallback_reason": "candidate_detection_failed"}
                if candidate_fallback_full_region
                else {}
            ),
        },
    )

    return PageModel(
        page_id=page_id,
        width_px=image.width,
        height_px=image.height,
        subject=subject,
        source_path=source_path,
        blocks=blocks,
        metadata=metadata,
    )


def crop_block_images(image_path: str | Path | Image.Image | Any, blocks: Iterable[ContentBlock], output_dir: str | Path) -> dict[str, str]:
    image = _load_image(image_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for block in blocks:
        crop = image.crop((int(block.bbox.left), int(block.bbox.top), int(block.bbox.right), int(block.bbox.bottom)))
        path = out_dir / f"{block.block_id}.png"
        crop.save(path)
        written[block.block_id] = str(path)
    return written


def blocks_from_page(prepared_page, config: SegmentOptions | None = None) -> list[ContentBlock]:
    page = segment_page(prepared_page, page_id=prepared_page.page_id, options=config)
    return page.blocks


def crop_block_image(prepared_page, block: ContentBlock) -> Image.Image:
    image = _load_image(prepared_page)
    return image.crop((int(block.bbox.left), int(block.bbox.top), int(block.bbox.right), int(block.bbox.bottom)))


_DEBUG_PALETTE = [
    (220, 50, 50),   # red
    (50, 100, 220),  # blue
    (50, 180, 50),   # green
    (220, 140, 0),   # orange
    (160, 50, 200),  # purple
    (0, 180, 180),   # cyan
    (180, 160, 0),   # yellow
]


def draw_segment_debug(
    image_source: Any,
    blocks: Iterable[ContentBlock],
    output_path: "str | Path",
) -> None:
    """Save a copy of the image with detected block bounding boxes overlaid.

    Useful for diagnosing segmentation quality: each block gets a uniquely
    colored rectangle and a short label with its index and block type.
    """
    from PIL import ImageDraw

    image = _load_image(image_source).copy()
    draw = ImageDraw.Draw(image)
    for index, block in enumerate(list(blocks)):
        color = _DEBUG_PALETTE[index % len(_DEBUG_PALETTE)]
        left = int(block.bbox.left)
        top = int(block.bbox.top)
        right = int(block.bbox.right)
        bottom = int(block.bbox.bottom)
        draw.rectangle((left, top, right, bottom), outline=color, width=3)
        label = f"{index + 1} {block.block_type.value}"
        label_w = len(label) * 7 + 6
        draw.rectangle((left + 2, top + 2, left + 2 + label_w, top + 20), fill=color)
        draw.text((left + 5, top + 4), label, fill=(255, 255, 255))
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
