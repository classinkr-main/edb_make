"""Upload checks and the rejection catalog for the web trial.

Pure functions: no web framework, no network. The server maps a raised
TrialRejected to its HTTP status and JSON payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from problem_parser import PdfInfo

A3_AREA_PT = 841.89 * 1190.55
MAGIC_SCAN_BYTES = 1024

# Named here so InputLimits and trial_config.from_env cannot drift apart.
DEFAULT_MAX_PAGES = 4
DEFAULT_MAX_WORDS_PER_PAGE = 8000
DEFAULT_MAX_DRAWINGS_PER_PAGE = 10000


@dataclass(frozen=True)
class Rejection:
    status: int
    code: str
    message: str
    feature: str | None = None

    def payload(self, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message, "feature": self.feature}}
        body.update(extra)
        return body


class TrialRejected(Exception):
    def __init__(self, rejection: Rejection, detail: str | None = None) -> None:
        super().__init__(rejection.code)
        self.rejection = rejection
        # Why a shared code was refused (busy: not_ready | turnstile | quota_store | global_limit | slot_wait).
        self.detail = detail


REJECTIONS: dict[str, Rejection] = {
    rejection.code: rejection
    for rejection in (
        Rejection(400, "bot_check_failed", "확인에 실패했어요. 새로고침 후 다시 시도해 주세요."),
        Rejection(413, "too_large", "무료 체험은 4MB까지 올릴 수 있어요.", "limit_size"),
        Rejection(415, "image_not_supported", "무료 체험은 글자가 들어 있는 PDF만 나눠 드려요.", "scan"),
        Rejection(415, "bad_type", "PDF 파일만 올릴 수 있어요."),
        Rejection(422, "unreadable_pdf", "이 PDF는 열 수 없어요. 암호가 걸려 있거나 손상된 파일인지 확인해 주세요."),
        Rejection(422, "too_many_pages", "페이지가 너무 많은 파일이에요.", "limit_pages"),
        Rejection(422, "page_too_large", "페이지 크기가 너무 커요."),
        Rejection(422, "page_too_complex", "이 파일은 무료 체험에서 처리하기에 너무 복잡해요.", "ai"),
        Rejection(422, "no_text_layer", "무료 체험은 글자가 들어 있는 PDF만 나눠 드려요.", "scan"),
        Rejection(429, "daily_limit", "오늘의 무료 체험을 모두 사용했어요.", "limit_daily"),
        Rejection(500, "parse_failed", "이 파일은 처리하지 못했어요.", "ai"),
        Rejection(503, "busy", "지금은 체험이 어려워요. 잠시 후 다시 시도해 주세요."),
        Rejection(401, "demo_auth_required", "시연 비밀번호를 확인해 주세요."),
        Rejection(403, "demo_unavailable", "지금은 박람회 시연을 이용할 수 없어요."),
    )
}


@dataclass(frozen=True)
class InputLimits:
    max_bytes: int
    max_pages: int
    max_source_pages: int
    max_page_area_pt: float
    # Pathological PDFs could run past Vercel's 60 s limit. "Words" are the whitespace-separated
    # tokens of the page's text layer and "drawings" are its vector paths, both as inspect_pdf
    # counts them; the corpus maximum is 674 words and 613 drawings per page.
    max_words_per_page: int = DEFAULT_MAX_WORDS_PER_PAGE
    max_drawings_per_page: int = DEFAULT_MAX_DRAWINGS_PER_PAGE


def reject(code: str, detail: str | None = None) -> TrialRejected:
    return TrialRejected(REJECTIONS[code], detail)


def sniff_kind(head: bytes) -> str:
    if b"%PDF-" in head[:MAGIC_SCAN_BYTES]:
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return "unknown"


def check_upload_head(head: bytes) -> None:
    kind = sniff_kind(head)
    if kind == "pdf":
        return
    if kind in {"png", "jpeg"}:
        raise reject("image_not_supported")
    raise reject("bad_type")


def check_pdf_info(info: PdfInfo, limits: InputLimits) -> None:
    if info.page_count > limits.max_source_pages:
        raise reject("too_many_pages")
    if info.scanned_pages == 0:
        raise reject("no_text_layer")
    if info.max_page_area_pt > limits.max_page_area_pt:
        raise reject("page_too_large")
    if info.pages_without_text > 0:
        raise reject("no_text_layer")
    if info.max_words_per_page > limits.max_words_per_page or info.max_drawings_per_page > limits.max_drawings_per_page:
        raise reject("page_too_complex")
