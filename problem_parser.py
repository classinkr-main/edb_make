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

import fitz
from PIL import Image

from structured_schema import Box

MIN_TEXT_CHARS_PER_PAGE = 20
PDF_RENDER_DPI = 200


class PdfUnreadableError(ValueError):
    """The file is not a PDF PyMuPDF can open without a password."""


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    scanned_pages: int
    pages_without_text: int
    max_page_area_pt: float


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
        for index in range(scanned_pages):
            page = doc[index]
            text = page.get_text("text")
            if len("".join(text.split())) < MIN_TEXT_CHARS_PER_PAGE:
                pages_without_text += 1
            max_page_area_pt = max(max_page_area_pt, float(page.rect.width * page.rect.height))
    return PdfInfo(
        page_count=page_count,
        scanned_pages=scanned_pages,
        pages_without_text=pages_without_text,
        max_page_area_pt=max_page_area_pt,
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
) -> ParseResult:
    """Recognize problems in a text-layer PDF without OCR, AI, or board rendering.

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
        ocr_mode="none",
        ai_fallback_config=None,
        pdf_dpi=PDF_RENDER_DPI,
        detect_perspective=False,
        deskew=True,
        crop_margins=True,
        max_dimension=None,
    )
    timing_ms["recognize"] = _elapsed_ms(recognize_started_at)

    crops_started_at = time.perf_counter()
    entries = build_problem_entries(
        prepared_pages,
        page_models,
        work_dir,
        LayoutTemplate(name="academy-default"),
        render_board_assets=False,
    )
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
