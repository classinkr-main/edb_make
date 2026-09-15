"""Parser-only entry point for the web trial.

Runs the same recognition path as the desktop problem export but stops
after problem crops: no board cutouts, placement, EDB, or UI session.
Nothing here imports a web framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz

MIN_TEXT_CHARS_PER_PAGE = 20


class PdfUnreadableError(ValueError):
    """The file is not a PDF PyMuPDF can open without a password."""


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    pages_without_text: int
    max_page_area_pt: float


def inspect_pdf(source: Path, *, max_pages: int) -> PdfInfo:
    """Count pages, text-less pages, and the largest page area without rendering.

    Pages are only scanned when the document is within ``max_pages`` so an
    oversized upload is rejected without extracting its text.
    """
    try:
        doc = fitz.open(source, filetype="pdf")
    except (fitz.FileDataError, RuntimeError, ValueError) as error:
        raise PdfUnreadableError(str(error)) from error
    with doc:
        if not doc.is_pdf or doc.needs_pass:
            raise PdfUnreadableError("not an unencrypted PDF")
        page_count = doc.page_count
        if page_count > max_pages:
            return PdfInfo(page_count=page_count, pages_without_text=0, max_page_area_pt=0.0)
        pages_without_text = 0
        max_page_area_pt = 0.0
        for page in doc:
            text = page.get_text("text")
            if len("".join(text.split())) < MIN_TEXT_CHARS_PER_PAGE:
                pages_without_text += 1
            max_page_area_pt = max(max_page_area_pt, float(page.rect.width * page.rect.height))
    return PdfInfo(
        page_count=page_count,
        pages_without_text=pages_without_text,
        max_page_area_pt=max_page_area_pt,
    )
