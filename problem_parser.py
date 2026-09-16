"""Parser-only entry point for the web trial.

Runs the same recognition path as the desktop problem export but stops
after problem crops: no board cutouts, placement, EDB, or UI session.
Nothing here imports a web framework.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz
from PIL import Image

from structured_schema import Box

MIN_TEXT_CHARS_PER_PAGE = 20
PDF_RENDER_DPI = 200

# Pillow only warns between this value and twice it, and raises DecompressionBombError
# above twice it, so this setting is a 40M-pixel hard ceiling with a 20M-pixel warning
# threshold. 2×A3 at 200 DPI is about 15.5M pixels, which stays below the warning threshold;
# anything that warns is already larger than this trial renders. Assigning the attribute is
# process-global, so every importer of problem_parser inherits the limit -- which is what the
# trial server wants, and why it lives next to the render settings rather than in a caller.
Image.MAX_IMAGE_PIXELS = 20_000_000

# inspect_pdf runs after the Turnstile check but before the parse slot and the quota charge,
# so everything it spends is free to a caller and endlessly repeatable. Its per-page work is
# therefore ordered cheapest-first, and a page that trips either gate below is reported with
# a sentinel count that exceeds any configured word/drawing limit, so check_pdf_info rejects
# it as page_too_complex without the page ever being interpreted.
#
# Measured locally on a one-page PDF whose content stream is "100 100 1 1 re S" repeated two
# million times (34 MB decompressed, 83 KB once flate-compressed -- an 83 KB upload):
#     xref_stream_raw()   (compressed length)        0.0 ms
#     xref_stream()       (decompressed length)     21.8 ms
#     page.get_text("text")                        340.5 ms
#     page.get_drawings()                       10_329.7 ms
#
# The raw gate is free but cannot see a flate bomb, so it cannot replace the decompressed
# gate; its job is to bound how much the decompressed gate is ever willing to inflate. Deflate
# tops out near 1030:1 (measured), so capping the compressed stream at 500 KB caps one page's
# transient inflation at roughly 500 MB. A synthetic page at the configured 4500-word /
# 2500-drawing limits measures about 131 KB compressed and 152 KB decompressed (2026-09-16),
# so these thresholds leave about 4x and 13x headroom over anything the trial accepts.
MAX_CONTENT_STREAM_RAW_BYTES_PER_PAGE = 500_000
MAX_CONTENT_STREAM_BYTES_PER_PAGE = 2_000_000
PATHOLOGICAL_COUNT_SENTINEL = 1_000_000_000


class PdfUnreadableError(ValueError):
    """The file is not a PDF PyMuPDF can open without a password."""


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    scanned_pages: int
    pages_without_text: int
    max_page_area_pt: float
    max_words_per_page: int = 0
    max_drawings_per_page: int = 0


def _content_stream_is_pathological(doc: fitz.Document, page: fitz.Page) -> bool:
    """True when a page's content stream is too big to be worth interpreting at all.

    Both checks run before get_text() or get_drawings() ever touch the page, cheapest first:
    the compressed length is free, and the decompressed length costs a single inflate that the
    raw gate has already bounded.
    """
    xrefs = page.get_contents()
    raw_bytes = sum(len(doc.xref_stream_raw(xref)) for xref in xrefs)
    if raw_bytes > MAX_CONTENT_STREAM_RAW_BYTES_PER_PAGE:
        return True
    return sum(len(doc.xref_stream(xref)) for xref in xrefs) > MAX_CONTENT_STREAM_BYTES_PER_PAGE


def inspect_pdf(source: Path, *, max_pages: int) -> PdfInfo:
    """Count all pages, then check text layer and page size on the first ``max_pages`` only.

    The trial processes only the leading pages, so later scanned or oversized
    pages do not matter and are never extracted.
    """
    try:
        doc = fitz.open(source, filetype="pdf")
    except (fitz.FileDataError, RuntimeError, ValueError) as error:
        raise PdfUnreadableError(str(error)) from error
    with doc:
        if not doc.is_pdf or doc.needs_pass:
            raise PdfUnreadableError("not an unencrypted PDF")
        page_count = doc.page_count
        scanned_pages = min(page_count, max_pages)
        pages_without_text = 0
        max_page_area_pt = 0.0
        max_words_per_page = 0
        max_drawings_per_page = 0
        for index in range(scanned_pages):
            page = doc[index]
            # page.rect is metadata, not content, so page size stays measurable for every page.
            max_page_area_pt = max(max_page_area_pt, float(page.rect.width * page.rect.height))
            if _content_stream_is_pathological(doc, page):
                # Skipped pages are deliberately left out of pages_without_text: check_pdf_info
                # raises no_text_layer before page_too_complex, so counting a page we refused to
                # read as textless would report the wrong reason for refusing it.
                max_words_per_page = max(max_words_per_page, PATHOLOGICAL_COUNT_SENTINEL)
                max_drawings_per_page = max(max_drawings_per_page, PATHOLOGICAL_COUNT_SENTINEL)
                continue
            text = page.get_text("text")
            if len("".join(text.split())) < MIN_TEXT_CHARS_PER_PAGE:
                pages_without_text += 1
            max_words_per_page = max(max_words_per_page, len(text.split()))
            max_drawings_per_page = max(max_drawings_per_page, len(page.get_drawings()))
    return PdfInfo(
        page_count=page_count,
        scanned_pages=scanned_pages,
        pages_without_text=pages_without_text,
        max_page_area_pt=max_page_area_pt,
        max_words_per_page=max_words_per_page,
        max_drawings_per_page=max_drawings_per_page,
    )


@dataclass(frozen=True)
class ParsedPage:
    page_id: str
    index: int
    width: int
    height: int
    image: Image.Image


@dataclass(frozen=True)
class ParsedRegion:
    page_id: str
    bbox: Box


@dataclass(frozen=True)
class ParsedProblem:
    problem_id: str
    number: int | None
    title: str
    regions: list[ParsedRegion]
    risk_flags: list[str]
    image: Image.Image


@dataclass(frozen=True)
class ParseResult:
    pages: list[ParsedPage]
    problems: list[ParsedProblem]
    source_page_count: int
    parser_version: str
    timing_ms: dict[str, int]


def parser_version() -> str:
    return (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "dev")[:7]


def _elapsed_ms(started_at: float) -> int:
    return int(round((time.perf_counter() - started_at) * 1000))


def _load_detached_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def _problem_regions(entry) -> list[ParsedRegion]:
    regions: list[ParsedRegion] = []
    for segment in entry.source_segments:
        bbox = segment.get("bbox") if isinstance(segment, dict) else None
        page_id = segment.get("source_page_id") if isinstance(segment, dict) else None
        if not isinstance(bbox, dict) or not page_id:
            continue
        regions.append(
            ParsedRegion(
                page_id=str(page_id),
                bbox=Box(
                    left=float(bbox["left"]),
                    top=float(bbox["top"]),
                    width=float(bbox["width"]),
                    height=float(bbox["height"]),
                ),
            )
        )
    return regions or [ParsedRegion(page_id=entry.source_page_id, bbox=entry.bounds)]


LEADING_PAGES_FILENAME = "leading-pages.pdf"


def _leading_pages_copy(source: Path, work_dir: Path, max_pages: int | None) -> tuple[Path, int]:
    """Return the file to parse and the source page count.

    ``garbage=4`` matters: without it PyMuPDF keeps the dropped pages' fonts
    and the copy stays as large as the original.
    """
    with fitz.open(source, filetype="pdf") as doc:
        page_count = doc.page_count
        if max_pages is None or page_count <= max_pages:
            return source, page_count
        doc.select(range(max_pages))
        target = work_dir / LEADING_PAGES_FILENAME
        doc.save(target, garbage=4, deflate=True)
    return target, page_count


def parse_problems(
    source: Path,
    *,
    work_dir: Path,
    max_pages: int | None = None,
    subject: str = "unknown",
    ocr_mode: str = "none",
    ai_fallback_config: dict[str, Any] | None = None,
) -> ParseResult:
    """Recognize problems in a text-layer PDF.

    The trial calls this with the defaults: no OCR, no AI, no board rendering.
    The bench oracle passes ``ocr_mode="auto"`` and a forced AI repair config
    so both sides share every downstream step and coordinate frame.

    With ``max_pages`` only the leading pages are parsed. Returned images are
    fully loaded copies, so ``work_dir`` may be deleted as soon as this returns.
    """
    # Deferred so requests rejected by inspect_pdf never load OpenCV and the pipeline.
    from build_problem_board_edb import build_pages, build_problem_entries, resolve_subject
    from layout_template_schema import LayoutTemplate

    timing_ms: dict[str, int] = {}
    total_started_at = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)
    parse_source, source_page_count = _leading_pages_copy(source, work_dir, max_pages)

    recognize_started_at = time.perf_counter()
    prepared_pages, page_models = build_pages(
        parse_source,
        subject=resolve_subject(subject),
        ocr_mode=ocr_mode,
        ai_fallback_config=ai_fallback_config,
        pdf_dpi=PDF_RENDER_DPI,
        detect_perspective=False,
        deskew=True,
        crop_margins=True,
        max_dimension=None,
        timings=timing_ms,
    )
    timing_ms["recognize"] = _elapsed_ms(recognize_started_at)

    crops_started_at = time.perf_counter()
    entries = build_problem_entries(
        prepared_pages,
        page_models,
        work_dir,
        LayoutTemplate(name="academy-default"),
        render_board_assets=False,
        timings=timing_ms,
    )
    load_started_at = time.perf_counter()
    problems = [
        ParsedProblem(
            problem_id=entry.problem_id,
            number=entry.problem_number,
            title=entry.title,
            regions=_problem_regions(entry),
            risk_flags=list(entry.risk_flags),
            image=_load_detached_rgb(entry.crop_path),
        )
        for entry in entries
    ]
    timing_ms["load"] = _elapsed_ms(load_started_at)
    timing_ms["crops"] = _elapsed_ms(crops_started_at)

    pages = [
        ParsedPage(
            page_id=prepared.page_id,
            index=index,
            width=prepared.image.width,
            height=prepared.image.height,
            image=prepared.image.convert("RGB"),
        )
        for index, prepared in enumerate(prepared_pages)
    ]
    timing_ms["total"] = _elapsed_ms(total_started_at)
    return ParseResult(
        pages=pages,
        problems=problems,
        source_page_count=source_page_count,
        parser_version=parser_version(),
        timing_ms=timing_ms,
    )
