# 웹 체험판 Plan 1 — 파서 코어와 Vercel 배포 스파이크

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 텍스트 PDF를 받아 문항 박스와 문항 이미지를 돌려주는 웹 무관 파서(`problem_parser.py`)를 만들고, 그 파서가 Vercel 함수에서 실제로 도는지 크기·속도·메모리를 잰다.

**Architecture:** 기존 `build_pages()` → `build_problem_entries()` 경로를 그대로 쓰되 칠판 cutout 생성만 끄는 스위치를 넣는다. `problem_parser.py`가 그 위에 PDF 사전 검사(`inspect_pdf`)와 결과 데이터클래스를 얹는다. `trial_server.py`는 이번 Plan에서 헬스 체크와 토큰으로 막은 스파이크 엔드포인트만 가진다.

**Tech Stack:** Python 3.12(Vercel)/3.14(로컬 venv), PyMuPDF, Pillow, OpenCV, FastAPI 0.141.1, Vercel Python runtime, unittest/pytest

**Spec:** `docs/superpowers/specs/2026-09-15-problem-parser-web-trial-design.md`

**스펙과 다른 순서:** 스펙 §14는 스파이크를 가장 먼저 두었다. 스파이크가 실제 체험판 경로를 재도록 파서 코어(Task 1~3)를 먼저 만들고 스파이크(Task 4~6)를 뒤에 둔다. 파서 코어는 로컬에서만 끝나는 작은 작업이라 스파이크 결과로 버려질 위험이 없다.

**Plan 2 (스파이크 결과를 본 뒤 작성):** 입력 검사·Turnstile·Supabase 한도·응답 예산·`/api/parse`·체험판 페이지·Cron·출시 준비.

---

## 파일 구조

| 파일 | 상태 | 책임 |
|---|---|---|
| `build_problem_board_edb.py` | 수정 | `render_board_assets` 스위치 (cutout 생성·복사·이어 붙이기 건너뛰기) |
| `problem_parser.py` | 신규 | `inspect_pdf`, `parse_problems`, 결과 데이터클래스. FastAPI를 import하지 않는다 |
| `test_problem_parser.py` | 신규 | 스위치·`inspect_pdf`·`parse_problems` 테스트 |
| `trial_server.py` | 신규 | FastAPI 앱: `/api/health`, `/api/spike`(토큰 필요). `app_server`를 import하지 않는다 |
| `test_trial_server.py` | 신규 | 헬스·스파이크 토큰 분기·import 격리 테스트 |
| `pyproject.toml` | 신규 | Vercel 엔트리포인트와 체험판 런타임 의존성만 |
| `.python-version` | 신규 | Vercel 파이썬 버전 `3.12` |
| `vercel.json` | 신규 | 리전·메모리·시간·번들 제외 |
| `.vercelignore` | 신규 | 업로드에서 뺄 폴더 |
| `scripts/trial_spike_probe.py` | 신규 | 배포된 스파이크 엔드포인트를 반복 호출해 콜드·웜 시간 출력 |
| `docs/web-trial-spike-results.md` | 신규 | 스파이크 측정 결과와 호스팅 판단 |

모든 명령은 워크트리 루트 `/Users/clmagi/Desktop/Projects/edb_mak/.claude/worktrees/web-trial`에서, 워크트리 venv(`.venv`, Python 3.14, CI 락 설치 완료)로 실행한다. 테스트는 Gemini 호출을 막기 위해 `env -u GEMINI_API_KEY`를 붙인다.

---

### Task 1: 칠판 cutout 스위치

**Files:**
- Modify: `build_problem_board_edb.py` (`_ProblemAssetTask` :1887, `_render_problem_asset` :2281, `_render_problem_assets` :2477, `_coalesce_cross_page_passage_drafts` :5779, `build_problem_entries` :6010)
- Test: `test_problem_parser.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`test_problem_parser.py`를 새로 만든다. 페이지를 넘는 지문 합치기 경로를 타도록 `test_passage_group_quality_score.py`의 국어 교차 페이지 픽스처를 옮겨 쓴다.

```python
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from assemble_page import group_problem_units
from build_problem_board_edb import build_problem_entries
from layout_template_schema import LayoutTemplate
from preprocess import PreparedPage
from structured_schema import BlockType, Box, ContentBlock, PageModel, ProblemUnit, Subject


def _block(block_id: str, block_type: BlockType, top: float, text: str | None, *, height: float = 80.0) -> ContentBlock:
    return ContentBlock(
        block_id=block_id,
        block_type=block_type,
        bbox=Box(left=40.0, top=top, width=760.0, height=height),
        reading_order=int(top),
        text=text,
    )


def _cross_page_passage_fixture(root: Path) -> tuple[list[PreparedPage], list[PageModel]]:
    root.mkdir(parents=True, exist_ok=True)
    page_1_path = root / "page-1.png"
    page_2_path = root / "page-2.png"
    for path in (page_1_path, page_2_path):
        Image.new("RGB", (900, 1400), "white").save(path)
    prepared_pages = [
        PreparedPage(
            page_id="korean-cross-001",
            source_path=str(page_1_path),
            page_number=1,
            image=Image.open(page_1_path).convert("RGB"),
            original_size=(900, 1400),
        ),
        PreparedPage(
            page_id="korean-cross-002",
            source_path=str(page_2_path),
            page_number=2,
            image=Image.open(page_2_path).convert("RGB"),
            original_size=(900, 1400),
        ),
    ]
    page_1 = PageModel(
        page_id="korean-cross-001",
        width_px=900,
        height_px=1400,
        subject=Subject.KOREAN,
        source_path=str(page_1_path),
        blocks=[
            _block("range-18-21", BlockType.STEM, 40, "[18~21] 다음 글을 읽고 물음에 답하시오."),
            _block("shared-passage-a", BlockType.STEM, 140, "긴 지문의 첫 페이지 내용이다.", height=520),
        ],
        problems=[
            ProblemUnit(
                unit_id="korean-cross-001-passage-fragment",
                subject=Subject.KOREAN,
                title="지문 18~21",
                stem_block_ids=["range-18-21", "shared-passage-a"],
                metadata={
                    "passage_group_id": "korean-cross-001-passage-18-21",
                    "passage_range": {"start": 18, "end": 21},
                    "passage_role": "passage_fragment",
                    "passage_child_problem_numbers": [18, 19, 20, 21],
                    "supplemental_item": True,
                },
            )
        ],
    )
    page_2 = group_problem_units(
        PageModel(
            page_id="korean-cross-002",
            width_px=900,
            height_px=1400,
            subject=Subject.KOREAN,
            source_path=str(page_2_path),
            blocks=[
                _block("shared-passage-b", BlockType.STEM, 40, "앞 페이지에서 이어지는 긴 지문 내용이다.", height=420),
                _block("q18", BlockType.STEM, 520, "18. 윗글의 내용으로 적절한 것은?"),
                _block("q19", BlockType.STEM, 680, "19. 윗글의 서술 방식으로 적절한 것은?"),
                _block("q20", BlockType.STEM, 840, "20. 윗글을 바탕으로 추론한 내용은?"),
                _block("q21", BlockType.STEM, 1000, "21. 윗글의 핵심 내용은?"),
            ],
        )
    )
    return prepared_pages, [page_1, page_2]


class TestRenderBoardAssetsSwitch(unittest.TestCase):
    def test_disabling_board_assets_keeps_crops_and_skips_cutouts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with_board_pages, with_board_models = _cross_page_passage_fixture(root / "with")
            without_board_pages, without_board_models = _cross_page_passage_fixture(root / "without")

            with_board = build_problem_entries(
                with_board_pages,
                with_board_models,
                root / "with" / "out",
                LayoutTemplate(name="academy-default"),
            )
            without_board = build_problem_entries(
                without_board_pages,
                without_board_models,
                root / "without" / "out",
                LayoutTemplate(name="academy-default"),
                render_board_assets=False,
            )

            self.assertEqual(
                [entry.problem_id for entry in with_board],
                [entry.problem_id for entry in without_board],
            )
            self.assertTrue((root / "with" / "out" / "problem_cutouts").is_dir())
            self.assertFalse((root / "without" / "out" / "problem_cutouts").exists())
            for kept, lean in zip(with_board, without_board):
                self.assertTrue(lean.crop_path.is_file())
                self.assertFalse(lean.board_render_path.exists())
                self.assertEqual(kept.crop_path.read_bytes(), lean.crop_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_problem_parser.py::TestRenderBoardAssetsSwitch`
Expected: FAIL — `TypeError: build_problem_entries() got an unexpected keyword argument 'render_board_assets'`

- [ ] **Step 3: `_ProblemAssetTask`에 필드 추가** (`build_problem_board_edb.py:1905` 근처)

```python
    source_media_regions: tuple[dict[str, Any], ...] = ()
    render_board_asset: bool = True
    rendered_media_regions: list[dict[str, Any]] = field(default_factory=list)
```

- [ ] **Step 4: `_render_problem_asset`의 cutout 호출 두 곳을 막는다** (:2291, :2428)

두 곳 모두 아래처럼 바꾼다.

```python
    if task.render_board_asset:
        _render_problem_board_asset(crop, task)
    return crop.size
```

첫 번째(텍스트 대체 카드) 분기는 들여쓰기가 한 단계 더 깊다:

```python
        if task.render_board_asset:
            _render_problem_board_asset(crop, task)
        return crop.size
```

- [ ] **Step 5: 중복 작업 복사에서 cutout 복사를 막는다** (:2504~2505)

```python
        _copy_problem_asset(canonical.crop_path, task.crop_path)
        if task.render_board_asset:
            _copy_problem_asset(canonical.board_render_path, task.board_render_path)
```

- [ ] **Step 6: 지문 합치기에서 cutout 이어 붙이기를 막는다** (:5779, :5895~5899)

시그니처:

```python
def _coalesce_cross_page_passage_drafts(
    drafts: list[_ProblemEntryDraft],
    crop_sizes: list[tuple[int, int]],
    pages: Sequence[PageModel],
    *,
    render_board_assets: bool = True,
) -> tuple[list[_ProblemEntryDraft], list[tuple[int, int]]]:
```

이어 붙이기:

```python
        if primary.asset_task is not None:
            primary.asset_task.rendered_media_regions = list(stitched_regions)
        if render_board_assets:
            _stitch_passage_image_files(
                [drafts[index].board_render_path for index in ordered_indices],
                primary.board_render_path,
                transparent=True,
            )
```

- [ ] **Step 7: `build_problem_entries`에 인자를 추가하고 전달한다** (:6010~6022, :6311, :6364)

```python
def build_problem_entries(
    prepared_pages: list[PreparedPage],
    pages: list[PageModel],
    output_dir: Path,
    template: LayoutTemplate,
    *,
    board_theme: str = DEFAULT_BOARD_THEME,
    content_target: str = "all",
    render_board_assets: bool = True,
) -> list[ProblemEntry]:
    crop_dir = output_dir / "problem_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    cutout_dir = output_dir / "problem_cutouts"
    if render_board_assets:
        cutout_dir.mkdir(parents=True, exist_ok=True)
```

`_ProblemAssetTask(` 호출(:6311)의 마지막 인자 `source_media_regions=(...)` 다음 줄에 추가:

```python
                        render_board_asset=render_board_assets,
```

합치기 호출(:6364):

```python
    drafts, crop_sizes = _coalesce_cross_page_passage_drafts(
        drafts,
        crop_sizes,
        pages,
        render_board_assets=render_board_assets,
    )
```

- [ ] **Step 8: 통과 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_problem_parser.py::TestRenderBoardAssetsSwitch`
Expected: PASS

- [ ] **Step 9: 기존 문항 정리 테스트 회귀 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_passage_group_quality_score.py test_pdf_text_marker_segmentation.py test_edb_publish_flow.py`
Expected: 모두 PASS (기본값 `True`라 동작 변화 없음)

- [ ] **Step 10: 커밋**

```bash
git add build_problem_board_edb.py test_problem_parser.py
git commit -m "feat: allow building problem entries without board cutouts"
```

---

### Task 2: `inspect_pdf` — 렌더 없는 PDF 사전 검사

**Files:**
- Create: `problem_parser.py`
- Test: `test_problem_parser.py`

- [ ] **Step 1: 실패하는 테스트 추가** (`test_problem_parser.py` 상단 import와 클래스 추가)

import 블록에 추가:

```python
import io

import fitz

from problem_parser import PdfUnreadableError, inspect_pdf
```

파일에 헬퍼와 클래스 추가:

```python
def _write_text_exam_pdf(path: Path, pages: list[list[int]], *, width: float = 600, height: float = 800) -> Path:
    doc = fitz.open()
    for numbers in pages:
        page = doc.new_page(width=width, height=height)
        slots = ((60, 120), (60, 430), (330, 120), (330, 430))
        for number, (x, y) in zip(numbers, slots):
            page.insert_text((x, y), f"{number}. problem stem", fontsize=14)
            page.draw_rect(fitz.Rect(x + 35, y + 50, x + 180, y + 140), color=(0, 0, 0), width=1)
            page.insert_text((x, y + 210), "① a   ② b   ③ c", fontsize=12)
    doc.save(path)
    doc.close()
    return path


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 500), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class TestInspectPdf(unittest.TestCase):
    def test_text_pdf_reports_pages_and_no_textless_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "exam.pdf", [[1, 2, 3, 4], [5, 6]])
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(2, info.page_count)
        self.assertEqual(0, info.pages_without_text)
        self.assertAlmostEqual(600 * 800, info.max_page_area_pt)

    def test_image_only_page_counts_as_textless(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "mixed.pdf", [[1, 2, 3, 4]])
            doc = fitz.open(path)
            scanned = doc.new_page(width=600, height=800)
            scanned.insert_image(scanned.rect, stream=_png_bytes())
            doc.saveIncr()
            doc.close()
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(2, info.page_count)
        self.assertEqual(1, info.pages_without_text)

    def test_over_limit_skips_page_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "long.pdf"
            doc = fitz.open()
            for _ in range(5):
                doc.new_page(width=600, height=800)
            doc.save(path)
            doc.close()
            info = inspect_pdf(path, max_pages=3)
        self.assertEqual(5, info.page_count)
        self.assertEqual(0, info.pages_without_text)
        self.assertEqual(0.0, info.max_page_area_pt)

    def test_reports_largest_page_area(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "big.pdf", [[1, 2, 3, 4]], width=1684, height=2384)
            info = inspect_pdf(path, max_pages=3)
        self.assertAlmostEqual(1684 * 2384, info.max_page_area_pt)

    def test_non_pdf_bytes_raise_unreadable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "fake.pdf"
            path.write_bytes(_png_bytes())
            with self.assertRaises(PdfUnreadableError):
                inspect_pdf(path, max_pages=3)

    def test_encrypted_pdf_raises_unreadable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "locked.pdf"
            doc = fitz.open()
            doc.new_page()
            doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
            doc.close()
            with self.assertRaises(PdfUnreadableError):
                inspect_pdf(path, max_pages=3)
```

- [ ] **Step 2: 실패 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_problem_parser.py::TestInspectPdf`
Expected: FAIL — `ModuleNotFoundError: No module named 'problem_parser'`

- [ ] **Step 3: 구현** (`problem_parser.py` 신규)

```python
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
```

- [ ] **Step 4: 통과 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_problem_parser.py::TestInspectPdf`
Expected: 6 passed. PyMuPDF가 PNG 바이트를 `filetype="pdf"`로 열 때 다른 예외 타입을 던지면 그 타입을 `except` 튜플에 더하고 다시 돌린다.

- [ ] **Step 5: 커밋**

```bash
git add problem_parser.py test_problem_parser.py
git commit -m "feat: inspect trial PDFs for page count and text layer"
```

---

### Task 3: `parse_problems` — 문항 박스와 이미지 반환

**Files:**
- Modify: `problem_parser.py`
- Test: `test_problem_parser.py`

- [ ] **Step 1: 실패하는 테스트 추가**

import 줄을 바꾼다:

```python
from problem_parser import PdfUnreadableError, inspect_pdf, parse_problems
```

클래스 추가:

```python
class TestParseProblems(unittest.TestCase):
    def test_text_pdf_yields_numbered_problems_inside_their_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2, 3, 4], [5, 6]])
            result = parse_problems(path, work_dir=root / "work")

            self.assertEqual(2, len(result.pages))
            self.assertEqual([1, 2, 3, 4, 5, 6], [problem.number for problem in result.problems])
            pages_by_id = {page.page_id: page for page in result.pages}
            for problem in result.problems:
                self.assertTrue(problem.regions)
                for region in problem.regions:
                    page = pages_by_id[region.page_id]
                    self.assertGreaterEqual(region.bbox.left, 0.0)
                    self.assertGreaterEqual(region.bbox.top, 0.0)
                    self.assertLessEqual(region.bbox.left + region.bbox.width, page.width + 1)
                    self.assertLessEqual(region.bbox.top + region.bbox.height, page.height + 1)
                self.assertEqual("RGB", problem.image.mode)
                self.assertGreater(problem.image.width, 0)
            for page in result.pages:
                self.assertEqual((page.width, page.height), page.image.size)
            self.assertFalse((root / "work" / "problem_cutouts").exists())
            self.assertIn("recognize", result.timing_ms)
            self.assertIn("crops", result.timing_ms)

    def test_images_survive_work_dir_removal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2]])
            with tempfile.TemporaryDirectory() as work_dir:
                result = parse_problems(path, work_dir=Path(work_dir))
            self.assertEqual(result.problems[0].image.size, result.problems[0].image.copy().size)
            self.assertIsNotNone(result.pages[0].image.getpixel((0, 0)))
```

- [ ] **Step 2: 실패 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_problem_parser.py::TestParseProblems`
Expected: FAIL — `ImportError: cannot import name 'parse_problems'`

- [ ] **Step 3: 구현** (`problem_parser.py`에 추가)

import 블록을 바꾼다:

```python
import os
import time
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image

from structured_schema import Box
```

상수와 데이터클래스, 함수 추가(`inspect_pdf` 아래):

```python
PDF_RENDER_DPI = 200


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


def parse_problems(source: Path, *, work_dir: Path, subject: str = "unknown") -> ParseResult:
    """Recognize problems in a text-layer PDF without OCR, AI, or board rendering.

    Returned images are fully loaded copies, so ``work_dir`` may be deleted
    as soon as this returns.
    """
    from build_problem_board_edb import build_pages, build_problem_entries, resolve_subject
    from layout_template_schema import LayoutTemplate

    timing_ms: dict[str, int] = {}
    total_started_at = time.perf_counter()
    work_dir.mkdir(parents=True, exist_ok=True)

    recognize_started_at = time.perf_counter()
    prepared_pages, page_models = build_pages(
        source,
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
    return ParseResult(pages=pages, problems=problems, parser_version=parser_version(), timing_ms=timing_ms)
```

`build_problem_board_edb` import를 함수 안에 두는 이유: `inspect_pdf`만 쓰는 요청 경로(거절되는 업로드)가 OpenCV·파이프라인 전체를 불러오지 않게 하려는 것이다. `app_server.py`의 지연 import와 같은 방식이다.

- [ ] **Step 4: 통과 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_problem_parser.py`
Expected: 모두 PASS. 번호가 `[1, 2, 3, 4, 5, 6]`이 아니면 실제 값을 출력해 보고, 두 번째 페이지 번호가 이어지지 않는 파서 동작이면 테스트 기대값을 실제 파서 동작(앱과 동일)에 맞추되 그 사실을 커밋 메시지에 적는다. 스위치나 `parse_problems`의 버그면 코드를 고친다.

- [ ] **Step 5: 커밋**

```bash
git add problem_parser.py test_problem_parser.py
git commit -m "feat: parse text PDFs into problem regions and crops"
```

---

### Task 4: 스파이크용 `trial_server.py`

**Files:**
- Create: `trial_server.py`
- Test: `test_trial_server.py`

- [ ] **Step 1: 워크트리 venv에 FastAPI 설치**

`web-migration`의 해시 잠금에서 체험판에 필요한 부분만 쓴다.

```bash
git show 3e935da:requirements-web.lock > "$CLAUDE_JOB_DIR/tmp/requirements-web.lock"
.venv/bin/python -m pip install -q --disable-pip-version-check --require-hashes -r "$CLAUDE_JOB_DIR/tmp/requirements-web.lock"
.venv/bin/python -c "import fastapi, httpx" 2>&1 || .venv/bin/python -m pip install -q httpx
```

`TestClient`는 `httpx`가 필요하다. 잠금에 없으면 테스트 전용으로만 설치하고, Plan 2에서 CI 락에 올린다. 이번 Plan에서는 CI를 건드리지 않으므로 `test_trial_server.py`는 FastAPI가 없으면 모듈 전체를 건너뛴다.

- [ ] **Step 2: 실패하는 테스트 작성** (`test_trial_server.py`)

```python
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

try:
    from fastapi.testclient import TestClient

    import trial_server
except ModuleNotFoundError as error:  # the CI lock has no web dependencies until Plan 2
    raise unittest.SkipTest(f"trial web dependencies missing: {error}")


def _text_pdf_bytes() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    for number, (x, y) in zip((1, 2), ((60, 120), (330, 120))):
        page.insert_text((x, y), f"{number}. problem stem", fontsize=14)
        page.insert_text((x, y + 210), "① a   ② b   ③ c", fontsize=12)
    payload = doc.tobytes()
    doc.close()
    return payload


class TestTrialServer(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(trial_server.app)

    def test_health_reports_commit(self):
        with mock.patch.dict(os.environ, {"VERCEL_GIT_COMMIT_SHA": "abcdef1234"}):
            response = self.client.get("/api/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "commit": "abcdef1"}, response.json())

    def test_spike_hidden_without_configured_token(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRIAL_SPIKE_TOKEN", None)
            response = self.client.post("/api/spike", content=b"%PDF-1.7", headers={"x-spike-token": ""})
        self.assertEqual(404, response.status_code)

    def test_spike_rejects_wrong_token(self):
        with mock.patch.dict(os.environ, {"TRIAL_SPIKE_TOKEN": "right"}):
            response = self.client.post("/api/spike", content=b"%PDF-1.7", headers={"x-spike-token": "wrong"})
        self.assertEqual(404, response.status_code)

    def test_spike_rejects_oversized_body(self):
        with mock.patch.dict(os.environ, {"TRIAL_SPIKE_TOKEN": "right"}):
            response = self.client.post(
                "/api/spike",
                content=b"0" * (trial_server.SPIKE_MAX_BYTES + 1),
                headers={"x-spike-token": "right"},
            )
        self.assertEqual(413, response.status_code)

    def test_spike_parses_pdf_and_reports_measurements(self):
        with mock.patch.dict(os.environ, {"TRIAL_SPIKE_TOKEN": "right"}):
            response = self.client.post(
                "/api/spike",
                content=_text_pdf_bytes(),
                headers={"x-spike-token": "right", "content-type": "application/pdf"},
            )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(1, body["page_count"])
        self.assertEqual(0, body["pages_without_text"])
        self.assertEqual([1, 2], body["problem_numbers"])
        for key in ("import_ms", "parse_ms", "timing_ms", "max_rss_mb", "instance_age_s", "request_index"):
            self.assertIn(key, body)

    def test_trial_modules_do_not_import_desktop_server(self):
        code = "import sys, trial_server; sys.exit(1 if 'app_server' in sys.modules else 0)"
        completed = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent, capture_output=True)
        self.assertEqual(0, completed.returncode, completed.stderr.decode())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 실패 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_trial_server.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'trial_server'`

- [ ] **Step 4: 구현** (`trial_server.py`)

```python
"""Web trial server (Vercel Python function).

Plan 1 only exposes a health check and a token-guarded spike endpoint that
measures the parser on Vercel. Plan 2 replaces the spike with /api/parse.
"""

from __future__ import annotations

import time

IMPORT_STARTED_AT = time.perf_counter()

import hmac
import os
import resource
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from problem_parser import PdfUnreadableError, inspect_pdf, parse_problems, parser_version

SPIKE_MAX_BYTES = 4_000_000
SPIKE_MAX_PAGES = 20

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

_instance_started_at = time.time()
_request_count = 0
_import_ms = int(round((time.perf_counter() - IMPORT_STARTED_AT) * 1000))


def _max_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS reports bytes.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(usage / divisor, 1)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "commit": parser_version()}


@app.post("/api/spike")
async def spike(request: Request) -> JSONResponse:
    global _request_count
    expected = os.environ.get("TRIAL_SPIKE_TOKEN", "")
    provided = request.headers.get("x-spike-token", "")
    if not expected or not hmac.compare_digest(provided.encode(), expected.encode()):
        return JSONResponse({"error": "not_found"}, status_code=404)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > SPIKE_MAX_BYTES:
            return JSONResponse({"error": "too_large"}, status_code=413)

    _request_count += 1
    request_index = _request_count
    with tempfile.TemporaryDirectory(prefix="trial-spike-") as temp_dir:
        source = Path(temp_dir) / "input.pdf"
        source.write_bytes(bytes(body))
        try:
            info = inspect_pdf(source, max_pages=SPIKE_MAX_PAGES)
        except PdfUnreadableError:
            return JSONResponse({"error": "bad_pdf"}, status_code=415)
        parse_started_at = time.perf_counter()
        result = await run_in_threadpool(parse_problems, source, work_dir=Path(temp_dir) / "work")
        parse_ms = int(round((time.perf_counter() - parse_started_at) * 1000))

    return JSONResponse(
        {
            "commit": result.parser_version,
            "bytes": len(body),
            "page_count": info.page_count,
            "pages_without_text": info.pages_without_text,
            "problem_numbers": [problem.number for problem in result.problems],
            "import_ms": _import_ms,
            "parse_ms": parse_ms,
            "timing_ms": result.timing_ms,
            "max_rss_mb": _max_rss_mb(),
            "instance_age_s": round(time.time() - _instance_started_at, 1),
            "request_index": request_index,
            "python": sys.version.split()[0],
        }
    )
```

- [ ] **Step 5: 통과 확인**

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q test_trial_server.py test_problem_parser.py`
Expected: 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add trial_server.py test_trial_server.py
git commit -m "feat: add trial server with a token-guarded parser spike"
```

---

### Task 5: Vercel 배포 설정과 의존성 격리 확인

**Files:**
- Create: `pyproject.toml`, `.python-version`, `vercel.json`, `.vercelignore`

- [ ] **Step 1: `pyproject.toml`**

```toml
# Vercel deploy manifest for the web trial only (trial_server.py).
# The desktop app keeps using requirements-*.lock; nothing else reads this file.
[project]
name = "edb-parser-trial"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi==0.141.1",
    "numpy==2.4.6",
    "opencv-python-headless==4.13.0.92",
    "Pillow==12.2.0",
    "PyMuPDF==1.27.2.3",
]

[tool.vercel]
entrypoint = "trial_server:app"
```

- [ ] **Step 2: `.python-version`**

```text
3.12
```

- [ ] **Step 3: `vercel.json`**

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "regions": ["icn1"],
  "functions": {
    "trial_server.py": {
      "memory": 2048,
      "maxDuration": 60,
      "excludeFiles": "{test_*.py,docs/**,scripts/**,ui_prototype/**,assets/**,quality/**,reports_worker/**,release/**,resources/**,.audit/**,.github/**,**/*.md,**/*.ipynb}"
    }
  }
}
```

- [ ] **Step 4: `.vercelignore`**

```text
.venv/
.app_runtime/
.pipeline_cache/
docs/
reports_worker/
ui_prototype/vendor/
release/
.audit/
```

- [ ] **Step 5: 체험판 의존성만으로 import되는지 확인** (Vercel 번들과 같은 조건)

```bash
python3.14 -m venv "$CLAUDE_JOB_DIR/tmp/trial-venv"
"$CLAUDE_JOB_DIR/tmp/trial-venv/bin/python" -m pip install -q --disable-pip-version-check fastapi==0.141.1 numpy==2.4.6 opencv-python-headless==4.13.0.92 Pillow==12.2.0 PyMuPDF==1.27.2.3 httpx
env -u GEMINI_API_KEY "$CLAUDE_JOB_DIR/tmp/trial-venv/bin/python" -m pytest --version >/dev/null 2>&1 || "$CLAUDE_JOB_DIR/tmp/trial-venv/bin/python" -m pip install -q pytest
env -u GEMINI_API_KEY "$CLAUDE_JOB_DIR/tmp/trial-venv/bin/python" -m pytest -q test_trial_server.py test_problem_parser.py
```

Expected: 모두 PASS. `ModuleNotFoundError`가 나면 그 패키지가 체험판 경로에서 실제로 필요한지 확인한다 — 파이프라인 import 경로에서 필요하면 `pyproject.toml` `dependencies`에 `requirements-release.lock`과 같은 버전으로 추가하고 이 단계를 반복한다.

- [ ] **Step 6: 전체 테스트 회귀 확인** (`pyproject.toml`이 pytest 수집에 영향이 없는지 포함)

Run: `env -u GEMINI_API_KEY .venv/bin/python -m pytest -q`
Expected: 새 테스트를 포함해 모두 PASS. 실패가 이번 변경과 무관하면 `git stash` 없이 `up3_mac`(5ff735b)에서 같은 테스트를 따로 돌려 기존 실패인지 확인하고 결과에 적는다.

- [ ] **Step 7: 커밋**

```bash
git add pyproject.toml .python-version vercel.json .vercelignore
git commit -m "build: configure Vercel deployment for the trial server"
```

---

### Task 6: 배포하고 스파이크 측정

**Files:**
- Create: `scripts/trial_spike_probe.py`, `docs/web-trial-spike-results.md`

- [ ] **Step 1: 측정 스크립트** (`scripts/trial_spike_probe.py`)

```python
"""Call the deployed trial spike endpoint and print cold/warm timings.

Usage:
  TRIAL_SPIKE_TOKEN=... python scripts/trial_spike_probe.py https://<deployment> exam.pdf [--repeat 5]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def call(base_url: str, payload: bytes, token: str) -> tuple[int, float, dict]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/spike",
        data=payload,
        method="POST",
        headers={"content-type": "application/pdf", "x-spike-token": token},
    )
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    wall_ms = (time.perf_counter() - started_at) * 1000
    try:
        body = json.loads(raw)
    except ValueError:
        body = {"raw": raw[:200].decode("utf-8", "replace")}
    return status, wall_ms, body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()
    token = os.environ.get("TRIAL_SPIKE_TOKEN", "")
    if not token:
        print("TRIAL_SPIKE_TOKEN is not set", file=sys.stderr)
        return 2
    payload = args.pdf.read_bytes()
    print(f"file={args.pdf.name} bytes={len(payload)}")
    for attempt in range(1, args.repeat + 1):
        status, wall_ms, body = call(args.base_url, payload, token)
        print(
            json.dumps(
                {
                    "attempt": attempt,
                    "status": status,
                    "wall_ms": round(wall_ms),
                    "request_index": body.get("request_index"),
                    "import_ms": body.get("import_ms"),
                    "parse_ms": body.get("parse_ms"),
                    "max_rss_mb": body.get("max_rss_mb"),
                    "pages": body.get("page_count"),
                    "problems": len(body.get("problem_numbers") or []),
                    "python": body.get("python"),
                    "error": body.get("error") or body.get("raw"),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`request_index`가 1이면 그 호출은 새 인스턴스(콜드)다.

- [ ] **Step 2: 커밋하고 배포 저장소에 브랜치 올리기**

```bash
git add scripts/trial_spike_probe.py
git commit -m "chore: add trial spike probe script"
git remote get-url classinkr 2>/dev/null || git remote add classinkr https://github.com/classinkr-main/edb_make.git
git push -u classinkr web-trial
```

`classinkr-main/edb_make`는 공개 저장소다. 키·토큰을 커밋하지 않았는지 푸시 전에 `git log -p up3_mac..web-trial | grep -iE "sb_secret|service_role|TRIAL_SPIKE_TOKEN=" ` 결과가 비어 있는지 확인한다.

- [ ] **Step 3: (사용자) Vercel 프로젝트 연결**

Vercel 대시보드에서:
1. Add New → Project → `classinkr-main/edb_make` Import
2. Framework Preset: FastAPI(자동 감지), Root Directory: `./`
3. Settings → Environments → Production Branch: `web-trial`
4. Settings → Git → Ignored Build Step: Custom, 명령
   `if [ "$VERCEL_GIT_COMMIT_REF" = "web-trial" ]; then exit 1; else exit 0; fi`
   (다른 브랜치 푸시는 빌드하지 않는다)
5. Settings → Environment Variables (Production): `TRIAL_SPIKE_TOKEN` = 임의의 긴 문자열
6. Settings → Billing → Spend Management: 월 한도 설정
7. Deployments → web-trial 최신 커밋 Redeploy

- [ ] **Step 4: 빌드 로그 확인**

빌드 로그에서 확인해 기록한다: 설치된 파이썬 버전, 함수 번들 크기(500 MB 이하), 경고. 빌드가 번들 크기로 실패하면 `vercel.json`의 `excludeFiles`에 로그에 나온 큰 경로를 추가하고 다시 커밋·푸시한다.

- [ ] **Step 5: 헬스 체크**

```bash
curl -s https://<production-domain>/api/health
```

Expected: `{"status":"ok","commit":"<7자리>"}`

- [ ] **Step 6: 측정**

합성 파일과 실제 텍스트 PDF(3쪽 이하로 자른 모의고사) 각각:

```bash
TRIAL_SPIKE_TOKEN=... .venv/bin/python scripts/trial_spike_probe.py https://<production-domain> <pdf> --repeat 5
```

콜드 스타트를 다시 보려면 10분 이상 기다렸다가 한 번 더 부른다. 3쪽 넘는 PDF를 자르는 법:

```bash
.venv/bin/python -c "import fitz,sys; d=fitz.open(sys.argv[1]); d.select(range(min(3,d.page_count))); d.save(sys.argv[2])" in.pdf out-3p.pdf
```

- [ ] **Step 7: 결과 문서** (`docs/web-trial-spike-results.md`)

아래 표를 실제 값으로 채운다.

```markdown
# 웹 체험판 Vercel 스파이크 결과

- 측정일: YYYY-MM-DD
- 배포 커밋: <sha>
- 함수 설정: icn1, 2 GB, maxDuration 60초, Python <버전>
- 번들 크기: <빌드 로그 값>

| 입력 | 쪽수 | 크기 | 콜드 wall | 콜드 import | 웜 wall p50 | 웜 parse p50 | 최대 RSS | 문항 수 |
|---|---|---|---|---|---|---|---|---|
| 합성 2문항 | 1 | | | | | | | |
| <실제 PDF 1> | 3 | | | | | | | |
| <실제 PDF 2> | 3 | | | | | | | |

## 판단

- 스펙 기준: 웜 p95 20초 이하, 최대 RSS 1.6 GB 이하, 번들 500 MB 이하
- 결과: 통과 / 미통과 (항목별)
- 다음: Plan 2 작성 / 호스팅 재검토 (이유)
```

- [ ] **Step 8: 커밋·푸시**

```bash
git add docs/web-trial-spike-results.md
git commit -m "docs: record Vercel spike measurements for the trial"
git push classinkr web-trial
```

---

## 완료 기준

- `test_problem_parser.py`, `test_trial_server.py` 통과, 기존 전체 테스트 회귀 없음
- 체험판 의존성만 설치한 venv에서 두 테스트 파일 통과
- Vercel 배포에서 `/api/health` 응답, `/api/spike`로 실제 PDF 파싱 성공
- `docs/web-trial-spike-results.md`에 콜드·웜 시간, 메모리, 번들 크기와 판단 기록
