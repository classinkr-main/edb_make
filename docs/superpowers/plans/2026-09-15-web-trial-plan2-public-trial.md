# 웹 체험판 Plan 2 — 공개 체험판 (파싱 API · 한도 · 화면)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 누구나 텍스트 PDF를 올려 앞 3쪽의 문항 박스·미리보기를 보고, 막힌 기능은 프리미엄 추천 팝업을 거쳐 `classin.co.kr/contact`로 가는 공개 체험판을 Vercel에 올릴 수 있는 상태로 만든다.

**Architecture:** `trial_server.py`는 `create_app(config, deps)` 팩토리로 바뀌고, 입력 검사(`trial_input.py`)·봇 확인(`trial_turnstile.py`)·한도와 통계(`trial_quota.py` → Supabase PostgREST)·응답 인코딩(`trial_preview.py`)을 조립한다. 각 모듈은 표준 라이브러리 `urllib`만 쓰고 네트워크 호출은 주입 가능한 `opener`로 감싸 테스트에서 가짜로 바꾼다. 정적 화면은 `public/`에 두고 설정은 `/api/config`로 받는다.

**Tech Stack:** Python 3.12(Vercel), FastAPI, PyMuPDF, Pillow, Supabase Postgres(PostgREST RPC), Cloudflare Turnstile, 순수 HTML/CSS/JS, node(`node -e`) 헬퍼 테스트, 로컬 PostgreSQL 17(SQL 테스트)

**Spec:** `docs/superpowers/specs/2026-09-15-problem-parser-web-trial-design.md`
**선행:** Plan 1 (`docs/superpowers/plans/2026-09-15-web-trial-plan1-parser-and-spike.md`) Task 1~5 완료. Task 6(Vercel 스파이크)은 다른 세션이 진행 중이며 이 Plan은 그 결과를 기다리지 않는다. 스파이크가 호스팅 부적합으로 끝나면 Task 9·10(Vercel 설정)만 다시 쓴다.

**같은 워크트리를 쓰는 다른 세션과의 규칙:** `git add <파일>`로만 커밋, `git add -A`·stash·reset·force push 금지. `docs/web-trial-spike-results.md`는 수정하지 않는다. `/api/spike`는 Task 8에서 제거하되, 스파이크 측정 완료 알림을 받은 뒤에 한다.

**테스트 환경 주의:** `build_structured_page_json.py`는 import 시 워크트리 `.env.local`을 `os.environ.setdefault`로 읽는다(`SUPABASE_URL`, `TRIAL_SPIKE_TOKEN` 등이 들어 있음). 모든 새 테스트는 환경변수를 읽지 않고 `TrialConfig`를 직접 만들어 넣으며, 네트워크 opener는 항상 가짜를 쓴다.

---

## 파일 구조

| 파일 | 상태 | 책임 |
|---|---|---|
| `problem_parser.py` | 수정 | `inspect_pdf`가 앞 `max_pages`쪽만 검사, `parse_problems(max_pages=)`가 앞쪽만 잘라 파싱 |
| `trial_input.py` | 신규 | 거절 코드 목록, 매직 넘버 판정, `PdfInfo` → 거절 판단. 네트워크·웹 없음 |
| `trial_preview.py` | 신규 | `ParseResult` → 응답 JSON(미리보기 data URI), 3.5 MB 예산 단계 |
| `trial_turnstile.py` | 신규 | Turnstile siteverify 호출과 결과 해석 |
| `trial_quota.py` | 신규 | KST 날짜·IP 해시, Supabase PostgREST 클라이언트, `QuotaStore` |
| `trial_config.py` | 신규 | `TrialConfig` 데이터클래스와 `from_env()` |
| `trial_server.py` | 수정 | `create_app()` 팩토리: `/api/config`, `/api/parse`, `/api/event`, `/api/health`, `/api/cron/daily` |
| `supabase/migrations/20260915000000_web_trial.sql` | 신규 | 테이블·함수·권한·RLS·퍼널 뷰 |
| `public/index.html`, `public/style.css`, `public/app.js` | 신규 | 체험판 화면 |
| `public/trial_logic.js` | 신규 | 화면에서 쓰는 순수 함수(팝업 문구, 오류→상태, 좌표 변환). node로 테스트 |
| `vercel.json` | 수정 | cron, 보안 헤더 |
| `test_trial_input.py`, `test_trial_preview.py`, `test_trial_turnstile.py`, `test_trial_quota.py`, `test_trial_sql.py`, `test_trial_web_logic.py` | 신규 | 모듈별 테스트 |
| `test_problem_parser.py`, `test_trial_server.py` | 수정 | 바뀐 동작 테스트 |
| `docs/web-trial-operations.md` | 신규 | 환경변수·Supabase·Turnstile 설정, 출시 점검표, 퍼널 SQL |

모든 명령은 워크트리 루트 `/Users/clmagi/Desktop/Projects/edb_mak/.claude/worktrees/web-trial`에서 `.venv`로 실행하고 `GEMINI_API_KEY=`를 앞에 붙인다.

---

### Task 1: 앞 3쪽만 검사·파싱

**Files:**
- Modify: `problem_parser.py`
- Modify: `test_problem_parser.py`

- [ ] **Step 1: 테스트 수정·추가**

`TestInspectPdf.test_over_limit_skips_page_scan`을 지우고 아래 두 테스트로 바꾼다.

```python
    def test_scans_only_leading_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "long.pdf", [[1, 2], [3, 4], [5, 6]])
            doc = fitz.open(path)
            for _ in range(2):
                scanned = doc.new_page(width=1684, height=2384)
                scanned.insert_image(scanned.rect, stream=_png_bytes())
            doc.saveIncr()
            doc.close()
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(5, info.page_count)
        self.assertEqual(3, info.scanned_pages)
        self.assertEqual(0, info.pages_without_text)
        self.assertAlmostEqual(600 * 800, info.max_page_area_pt)

    def test_short_document_scans_every_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "short.pdf", [[1, 2]])
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(1, info.page_count)
        self.assertEqual(1, info.scanned_pages)
```

`TestParseProblems`에 추가:

```python
    def test_max_pages_parses_only_leading_pages_from_a_compacted_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]])
            result = parse_problems(path, work_dir=root / "work", max_pages=3)
            trimmed = root / "work" / "leading-pages.pdf"

            self.assertEqual(5, result.source_page_count)
            self.assertEqual(3, len(result.pages))
            self.assertEqual([1, 2, 3, 4, 5, 6], [problem.number for problem in result.problems])
            self.assertTrue(trimmed.is_file())
            self.assertLess(trimmed.stat().st_size, path.stat().st_size)

    def test_without_max_pages_parses_whole_document(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4]])
            result = parse_problems(path, work_dir=root / "work")
            self.assertEqual(2, result.source_page_count)
            self.assertEqual(2, len(result.pages))
            self.assertFalse((root / "work" / "leading-pages.pdf").exists())
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_problem_parser.py`
Expected: FAIL — `AttributeError: 'PdfInfo' object has no attribute 'scanned_pages'`, `TypeError: parse_problems() got an unexpected keyword argument 'max_pages'`

- [ ] **Step 3: `inspect_pdf` 수정**

```python
@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    scanned_pages: int
    pages_without_text: int
    max_page_area_pt: float


def inspect_pdf(source: Path, *, max_pages: int) -> PdfInfo:
    """Count all pages, then check text layer and page size on the first ``max_pages`` only.

    The trial processes only the leading pages, so later scanned pages or
    oversized pages do not matter and are never extracted.
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
```

- [ ] **Step 4: `parse_problems` 수정**

`ParseResult`에 `source_page_count: int` 필드를 `problems` 다음에 추가한다.

```python
@dataclass(frozen=True)
class ParseResult:
    pages: list[ParsedPage]
    problems: list[ParsedProblem]
    source_page_count: int
    parser_version: str
    timing_ms: dict[str, int]
```

`parse_problems` 시그니처와 앞부분:

```python
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
```

(이하 기존 본문 그대로.) 마지막 `return`:

```python
    return ParseResult(
        pages=pages,
        problems=problems,
        source_page_count=source_page_count,
        parser_version=parser_version(),
        timing_ms=timing_ms,
    )
```

- [ ] **Step 5: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_problem_parser.py test_trial_server.py`
Expected: 모두 PASS (`/api/spike`는 `max_pages`를 넘기지 않으므로 기존 동작 유지)

- [ ] **Step 6: 커밋**

```bash
git add problem_parser.py test_problem_parser.py
git commit -m "feat: parse only the leading pages of long trial PDFs"
```

---

### Task 2: 입력 검사와 거절 코드 — `trial_input.py`

**Files:**
- Create: `trial_input.py`
- Test: `test_trial_input.py`

- [ ] **Step 1: 실패하는 테스트** (`test_trial_input.py`)

```python
import unittest

from problem_parser import PdfInfo
from trial_input import (
    A3_AREA_PT,
    InputLimits,
    REJECTIONS,
    TrialRejected,
    check_pdf_info,
    check_upload_head,
    sniff_kind,
)

LIMITS = InputLimits(max_bytes=4_000_000, max_pages=3, max_source_pages=100, max_page_area_pt=2 * A3_AREA_PT)


def _info(**overrides):
    values = {"page_count": 3, "scanned_pages": 3, "pages_without_text": 0, "max_page_area_pt": 595.0 * 842.0}
    values.update(overrides)
    return PdfInfo(**values)


class TestSniffKind(unittest.TestCase):
    def test_pdf_header_at_start(self):
        self.assertEqual("pdf", sniff_kind(b"%PDF-1.7\n..."))

    def test_pdf_header_after_leading_junk_within_1024_bytes(self):
        self.assertEqual("pdf", sniff_kind(b"\x00" * 500 + b"%PDF-1.4"))

    def test_pdf_header_beyond_1024_bytes_is_unknown(self):
        self.assertEqual("unknown", sniff_kind(b"\x00" * 1100 + b"%PDF-1.4"))

    def test_png_and_jpeg(self):
        self.assertEqual("png", sniff_kind(b"\x89PNG\r\n\x1a\n rest"))
        self.assertEqual("jpeg", sniff_kind(b"\xff\xd8\xff\xe0 rest"))

    def test_other_bytes(self):
        self.assertEqual("unknown", sniff_kind(b"PK\x03\x04 zip"))
        self.assertEqual("unknown", sniff_kind(b""))


class TestCheckUploadHead(unittest.TestCase):
    def test_images_are_redirected_to_scan_popup(self):
        with self.assertRaises(TrialRejected) as caught:
            check_upload_head(b"\x89PNG\r\n\x1a\n")
        self.assertEqual("image_not_supported", caught.exception.rejection.code)
        self.assertEqual(415, caught.exception.rejection.status)
        self.assertEqual("scan", caught.exception.rejection.feature)

    def test_unknown_is_bad_type_without_popup(self):
        with self.assertRaises(TrialRejected) as caught:
            check_upload_head(b"hello")
        self.assertEqual("bad_type", caught.exception.rejection.code)
        self.assertIsNone(caught.exception.rejection.feature)

    def test_pdf_passes(self):
        check_upload_head(b"%PDF-1.7")


class TestCheckPdfInfo(unittest.TestCase):
    def assertRejected(self, info, code):
        with self.assertRaises(TrialRejected) as caught:
            check_pdf_info(info, LIMITS)
        self.assertEqual(code, caught.exception.rejection.code)
        return caught.exception.rejection

    def test_normal_exam_passes(self):
        check_pdf_info(_info(page_count=16), LIMITS)

    def test_source_over_hundred_pages(self):
        rejection = self.assertRejected(_info(page_count=101), "too_many_pages")
        self.assertEqual(422, rejection.status)
        self.assertEqual("limit_pages", rejection.feature)

    def test_exactly_hundred_pages_passes(self):
        check_pdf_info(_info(page_count=100), LIMITS)

    def test_empty_document(self):
        self.assertRejected(_info(page_count=0, scanned_pages=0), "no_text_layer")

    def test_oversized_leading_page(self):
        self.assertRejected(_info(max_page_area_pt=2 * A3_AREA_PT + 1), "page_too_large")

    def test_textless_leading_page(self):
        rejection = self.assertRejected(_info(pages_without_text=1), "no_text_layer")
        self.assertEqual("scan", rejection.feature)

    def test_page_size_checked_before_text_layer(self):
        self.assertRejected(_info(pages_without_text=1, max_page_area_pt=10 * A3_AREA_PT), "page_too_large")


class TestRejectionCatalog(unittest.TestCase):
    def test_codes_are_unique_and_statuses_are_client_or_server_errors(self):
        codes = [rejection.code for rejection in REJECTIONS.values()]
        self.assertEqual(len(codes), len(set(codes)))
        for rejection in REJECTIONS.values():
            self.assertIn(rejection.status, {400, 413, 415, 422, 429, 500, 503})
            self.assertTrue(rejection.message)

    def test_payload_shape(self):
        payload = REJECTIONS["daily_limit"].payload(remaining_today=0)
        self.assertEqual(
            {"error": {"code": "daily_limit", "message": REJECTIONS["daily_limit"].message, "feature": "limit_daily"}, "remaining_today": 0},
            payload,
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_input.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'trial_input'`

- [ ] **Step 3: 구현** (`trial_input.py`)

```python
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
    def __init__(self, rejection: Rejection) -> None:
        super().__init__(rejection.code)
        self.rejection = rejection


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
        Rejection(422, "no_text_layer", "무료 체험은 글자가 들어 있는 PDF만 나눠 드려요.", "scan"),
        Rejection(429, "daily_limit", "오늘의 무료 체험을 모두 사용했어요.", "limit_daily"),
        Rejection(500, "parse_failed", "이 파일은 처리하지 못했어요.", "ai"),
        Rejection(503, "busy", "지금은 체험이 어려워요. 잠시 후 다시 시도해 주세요."),
    )
}


@dataclass(frozen=True)
class InputLimits:
    max_bytes: int
    max_pages: int
    max_source_pages: int
    max_page_area_pt: float


def reject(code: str) -> TrialRejected:
    return TrialRejected(REJECTIONS[code])


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
```

- [ ] **Step 4: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_input.py`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add trial_input.py test_trial_input.py
git commit -m "feat: add trial upload checks and rejection catalog"
```

---

### Task 3: 응답 인코딩과 크기 예산 — `trial_preview.py`

**Files:**
- Create: `trial_preview.py`
- Test: `test_trial_preview.py`

- [ ] **Step 1: 실패하는 테스트** (`test_trial_preview.py`)

```python
import base64
import io
import json
import unittest

from PIL import Image

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from structured_schema import Box
from trial_preview import PREVIEW_STEPS, build_parse_payload, encode_jpeg_data_uri


def _noise(width: int, height: int, seed: int) -> Image.Image:
    # Deterministic high-entropy pixels so JPEG sizes are realistic.
    data = bytes((index * 7919 + seed * 104729) % 251 for index in range(width * height * 3))
    return Image.frombytes("RGB", (width, height), data)


def _result(problem_count: int = 2, page_size=(1915, 2811), problem_size=(1600, 900)) -> ParseResult:
    pages = [ParsedPage(page_id="p1", index=0, width=page_size[0], height=page_size[1], image=_noise(*page_size, seed=1))]
    problems = [
        ParsedProblem(
            problem_id=f"q{index}",
            number=index,
            title=f"{index}.",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.5, top=20.25, width=300.0, height=200.0))],
            risk_flags=["low_confidence"] if index == 1 else [],
            image=_noise(*problem_size, seed=index + 10),
        )
        for index in range(1, problem_count + 1)
    ]
    return ParseResult(pages=pages, problems=problems, source_page_count=16, parser_version="abc1234", timing_ms={"total": 1500})


def _decode(data_uri: str) -> Image.Image:
    prefix = "data:image/jpeg;base64,"
    assert data_uri.startswith(prefix)
    return Image.open(io.BytesIO(base64.b64decode(data_uri[len(prefix):])))


class TestEncodeJpegDataUri(unittest.TestCase):
    def test_downscales_long_side_and_never_upscales(self):
        big = _decode(encode_jpeg_data_uri(_noise(2000, 1000, 1), long_side=800, quality=70))
        self.assertEqual((800, 400), big.size)
        small = _decode(encode_jpeg_data_uri(_noise(300, 200, 1), long_side=800, quality=70))
        self.assertEqual((300, 200), small.size)


class TestBuildParsePayload(unittest.TestCase):
    def test_shape_and_coordinates(self):
        payload = build_parse_payload(_result(), remaining_today=2, elapsed_ms=1234, processed_page_limit=3)
        self.assertEqual("abc1234", payload["parser_version"])
        self.assertEqual(1234, payload["elapsed_ms"])
        self.assertEqual(16, payload["source_page_count"])
        self.assertEqual(1, payload["processed_page_count"])
        self.assertEqual(3, payload["processed_page_limit"])
        self.assertEqual(2, payload["remaining_today"])
        page = payload["pages"][0]
        self.assertEqual({"page_id": "p1", "index": 0, "width": 1915, "height": 2811}, {k: page[k] for k in ("page_id", "index", "width", "height")})
        problem = payload["problems"][0]
        self.assertEqual(
            {"problem_id": "q1", "number": 1, "title": "1.", "risk_flags": ["low_confidence"]},
            {k: problem[k] for k in ("problem_id", "number", "title", "risk_flags")},
        )
        self.assertEqual([{"page_id": "p1", "bbox": {"left": 10.5, "top": 20.25, "width": 300.0, "height": 200.0}}], problem["regions"])
        self.assertEqual(0, payload["preview_step"])
        self.assertEqual(PREVIEW_STEPS[0].page_long_side, max(_decode(page["preview"]).size))
        self.assertEqual(PREVIEW_STEPS[0].problem_long_side, max(_decode(problem["preview"]).size))

    def test_falls_back_to_smaller_step_when_over_budget(self):
        roomy = build_parse_payload(_result(problem_count=4), remaining_today=1, elapsed_ms=1, processed_page_limit=3)
        roomy_size = len(json.dumps(roomy, ensure_ascii=False).encode("utf-8"))
        tight = build_parse_payload(
            _result(problem_count=4),
            remaining_today=1,
            elapsed_ms=1,
            processed_page_limit=3,
            budget_bytes=roomy_size - 1,
        )
        self.assertGreater(tight["preview_step"], 0)
        self.assertLessEqual(len(json.dumps(tight, ensure_ascii=False).encode("utf-8")), roomy_size - 1)

    def test_uses_last_step_when_nothing_fits(self):
        payload = build_parse_payload(_result(), remaining_today=1, elapsed_ms=1, processed_page_limit=3, budget_bytes=10)
        self.assertEqual(len(PREVIEW_STEPS) - 1, payload["preview_step"])

    def test_is_deterministic(self):
        first = build_parse_payload(_result(), remaining_today=1, elapsed_ms=1, processed_page_limit=3)
        second = build_parse_payload(_result(), remaining_today=1, elapsed_ms=1, processed_page_limit=3)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_preview.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'trial_preview'`

- [ ] **Step 3: 구현** (`trial_preview.py`)

```python
"""Encode a ParseResult into the /api/parse JSON body within Vercel's body limit."""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from typing import Any

from PIL import Image

from problem_parser import ParseResult

# Vercel caps function response bodies at 4.5 MB; leave room for headers and slack.
RESPONSE_BUDGET_BYTES = 3_500_000


@dataclass(frozen=True)
class PreviewStep:
    page_long_side: int
    problem_long_side: int
    quality: int


PREVIEW_STEPS = (
    PreviewStep(page_long_side=1200, problem_long_side=800, quality=78),
    PreviewStep(page_long_side=1200, problem_long_side=600, quality=65),
    PreviewStep(page_long_side=900, problem_long_side=600, quality=65),
    PreviewStep(page_long_side=700, problem_long_side=450, quality=55),
)


def encode_jpeg_data_uri(image: Image.Image, *, long_side: int, quality: int) -> str:
    preview = image.convert("RGB")
    scale = long_side / max(preview.size)
    if scale < 1:
        size = (max(1, round(preview.width * scale)), max(1, round(preview.height * scale)))
        preview = preview.resize(size, Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    preview.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=False)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _payload_for_step(
    result: ParseResult,
    step_index: int,
    *,
    remaining_today: int,
    elapsed_ms: int,
    processed_page_limit: int,
) -> dict[str, Any]:
    step = PREVIEW_STEPS[step_index]
    return {
        "parser_version": result.parser_version,
        "elapsed_ms": elapsed_ms,
        "source_page_count": result.source_page_count,
        "processed_page_count": len(result.pages),
        "processed_page_limit": processed_page_limit,
        "remaining_today": remaining_today,
        "preview_step": step_index,
        "pages": [
            {
                "page_id": page.page_id,
                "index": page.index,
                "width": page.width,
                "height": page.height,
                "preview": encode_jpeg_data_uri(page.image, long_side=step.page_long_side, quality=step.quality),
            }
            for page in result.pages
        ],
        "problems": [
            {
                "problem_id": problem.problem_id,
                "number": problem.number,
                "title": problem.title,
                "regions": [
                    {
                        "page_id": region.page_id,
                        "bbox": {
                            "left": float(region.bbox.left),
                            "top": float(region.bbox.top),
                            "width": float(region.bbox.width),
                            "height": float(region.bbox.height),
                        },
                    }
                    for region in problem.regions
                ],
                "risk_flags": list(problem.risk_flags),
                "preview": encode_jpeg_data_uri(problem.image, long_side=step.problem_long_side, quality=step.quality),
            }
            for problem in result.problems
        ],
    }


def build_parse_payload(
    result: ParseResult,
    *,
    remaining_today: int,
    elapsed_ms: int,
    processed_page_limit: int,
    budget_bytes: int = RESPONSE_BUDGET_BYTES,
) -> dict[str, Any]:
    """Return the first preview step whose JSON fits the budget, else the smallest step."""
    payload: dict[str, Any] = {}
    for step_index in range(len(PREVIEW_STEPS)):
        payload = _payload_for_step(
            result,
            step_index,
            remaining_today=remaining_today,
            elapsed_ms=elapsed_ms,
            processed_page_limit=processed_page_limit,
        )
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= budget_bytes:
            return payload
    return payload
```

- [ ] **Step 4: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_preview.py`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add trial_preview.py test_trial_preview.py
git commit -m "feat: encode trial parse results within the response budget"
```

---

## 구현 기록 (2026-09-15)

Task 1~3은 위 단계 그대로 옮겨 구현했다. Task 4 이후는 외부 계약 조사(Turnstile·Supabase·Vercel 공식 문서를 조사 에이전트 4개가 읽고 검증 에이전트 4개가 반박 검토, 145개 사실 중 15개 정정)를 반영해야 해서 계획 본문을 먼저 쓰지 않고 테스트 우선으로 바로 구현했다. 아래가 실제 결과다.

| Task | 내용 | 파일 | 커밋 | 검증 |
|---|---|---|---|---|
| 1 | 앞 3쪽만 검사·파싱 (`garbage=4` 사본) | `problem_parser.py` | `53e49be` | 5쪽 PDF → 3쪽 6문항, 사본이 원본보다 작음 |
| 2 | 입력 검사·거절 코드 | `trial_input.py` | `68f1211` | 매직 넘버·쪽수·크기·텍스트 층 |
| 3 | 응답 인코딩·3.5 MB 예산 | `trial_preview.py` | `1bea0ca`, `b380a11` | 예산 초과 시 단계 하강, 결정성. `needs_review`는 경계 불확실 플래그만 |
| 4 | Supabase 스키마 | `supabase/migrations/20260915000000_web_trial.sql` | `0af7fcd`, `be0038f` | 로컬 PostgreSQL 17 + pgbench 16세션 동시성. 행 잠금·revoke·0 하한·service_role grant를 뺀 변이 4종이 모두 테스트에 걸림 |
| 5 | 한도 저장소 | `trial_quota.py` | `845b57c` | `apikey` 헤더만, 오류·비JSON·540 → `QuotaUnavailable` |
| 6 | Turnstile 검증 | `trial_turnstile.py` | `44cf9cd` | 2048자 선검사, 잘못된 secret·internal-error·네트워크 → 503 |
| 7 | 설정 | `trial_config.py` | `8ad8368` | 운영에서 필수 비밀값 6개 누락 목록 |
| 8 | API | `trial_server.py` | `0bad43e` | 30개 시나리오: 성공, 거절 11종, 한도, 동시 슬롯 초과 503·환불, 이벤트, 크론 인증 |
| 9 | 화면 로직 | `public/trial_logic.js` | `7d78a16` | node 테스트: 팝업 8종 문구, 사전 검사, Vercel 플랫폼 오류 해석, 좌표 |
| 10 | 화면 | `public/index.html`·`style.css`·`app.js`, `scripts/run_trial_local.py` | `cfaae59` | 헤드리스 Chromium 1280·400px, 실제 16쪽 수능 국어 PDF: 1.6초, 9문항+지문 3개, 가로 스크롤 없음, 4번째 업로드에 한도 팝업 |
| 11 | Vercel 설정 | `vercel.json` | `4d7f290` | memory 제거, 크론 00:10 KST, CSP. 로컬 러너가 같은 헤더를 적용한 상태로 브라우저 확인(콘솔 오류 0) |
| 12 | 운영 문서 | `docs/web-trial-operations.md` | `ca2ff3f` | — |

### 계획과 달라진 점

- **`/api/spike`는 아직 남아 있다.** 다른 세션이 배포된 `2c58021`로 측정 중이라 측정 완료 후 제거한다(운영 문서 §2-4).
- **Turnstile 컨테이너 id.** `id="turnstile"`은 `window.turnstile` 전역을 가려 위젯이 뜨지 않았다(브라우저 확인에서 발견). `turnstile-widget`으로 바꿨다.
- **동시 처리.** Fluid compute는 한 프로세스가 여러 요청을 받으므로 인스턴스당 파싱 슬롯 2개(`TRIAL_PARSE_CONCURRENCY`)와 20초 대기를 두었다. 대기 초과는 503이며 차감을 되돌린다.
- **"확인 필요" 표시.** 실제 수능 국어에서 `passage_cross_page_merge_check`가 12문항 중 7개에 붙어 이 플래그는 표시하지 않는다.
- **CI.** `test_trial_api.py`·`test_trial_server.py`는 FastAPI가 없는 CI 락에서 건너뛴다. `test_trial_sql.py`는 PostgreSQL 바이너리가 있으면(GitHub ubuntu 러너 포함) 돈다. 웹 의존성을 CI 락에 올리는 일은 남아 있다.

### 전체 테스트

`GEMINI_API_KEY= .venv/bin/python -m pytest -q` → 1364 passed (Plan 1 종료 시 1253).

### 코드 리뷰 반영 (2026-09-15)

리뷰 관점 5개(보안, 동시성·한도, 화면, SQL·Supabase, 배포)마다 에이전트가 찾고, 발견마다 반박 에이전트 2개가 검증했다.
26건 중 18건은 두 검증이 모두 인정, 2건은 의견이 갈렸고, 6건은 반박되었다.

| 커밋 | 반영한 발견 |
|---|---|
| `5e8f90f` | 커밋 후 시간 초과된 차감이 환불되지 않음, 환불·차감 잠금 순서 교착(2건), 잠금 대기 중 늦은 커밋, `IncompleteRead`·잘못된 URL 예외 누락, `smallint` 이벤트 열 넘침 |
| `5fefc1e` | 슬롯 대기 중 차감(공유 IP의 거짓 429), 대기 스레드가 anyio 스레드 풀 고갈, 거절 외 예외의 텍스트 500·이벤트 누락, NaN 좌표 인코딩 실패, (의견 갈림) 파서 크래시 환불로 한도 우회 → 파싱 시작 후 차감 유지 |
| `9bf287c` | 팝업 `{rest}` 노출, 대기 중 Turnstile 위젯이 가려짐, 설정 로드 전 업로드, 실패 메시지 없이 팝업만, 드롭존 밖 드롭 시 PDF로 이동, 카드 안 버튼 키보드 조작 불가, 같은 파일 재선택 불가, 경과 초 과다 낭독 |
| `927b8b0` | 잠금 파일 없는 Vercel 빌드의 전이 의존성 변동 → 14개 전부 고정 |

의견이 갈린 나머지 1건(이벤트 기록을 응답 전에 기다림)은 이벤트 insert 시간 제한을 2초로 줄이는 것으로만 반영했다.
반박된 6건 중 `/api/spike`가 운영에 남는 문제는 스파이크 측정 뒤 제거한다(운영 문서 §2-4).

