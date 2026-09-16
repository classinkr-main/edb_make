# Web Trial Board Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 웹 체험판 응답에 문항의 칠판용(배경 제거) 미리보기를 원본과 함께 싣고, 결과 화면 토글로 전환할 수 있게 한다.

**Architecture:** `problem_parser.parse_problems`에 `render_board_assets` 스위치를 열어 데스크톱과 같은 컷아웃(분필색 잉크 + 투명 배경)을 `ParsedProblem.board_image`로 받는다. `trial_preview`가 컷아웃을 charcoal 배경에 합성한 뒤 다른 미리보기와 함께 WebP(q78, method 2)로 인코딩해 `problems[].board`와 `board_previews`를 응답에 넣고, 예산 폴백의 마지막 단계에서만 칠판용을 뺀다. 화면은 `trial_logic`의 순수 함수로 카드 이미지를 고르고 `app.js` 토글이 모든 카드를 한 번에 바꾼다. 설정 `TRIAL_BOARD_PREVIEWS`(기본 켬)로 전체를 끌 수 있다.

**Tech Stack:** Python 3.12 (Vercel) / 3.14 (로컬 venv), Pillow 12.2 (WebP), numpy, FastAPI, 순수 JS(`public/`), node 기반 JS 테스트, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-09-16-web-trial-board-preview-design.md`

**작업 규칙:** 같은 워크트리(`.claude/worktrees/web-trial`)를 다른 세션이 쓴다. 커밋 전 `git status --short`로 남의 변경(예: `page_repair.py`, `scripts/trial_bench/oracle.py`)을 확인하고 **파일 지정 `git add`만** 쓴다. `git add -A`, `git stash`, `git reset` 금지. 파이썬은 항상 `.venv/bin/python`, 파서를 실행하는 테스트·스크립트는 `GEMINI_API_KEY=` 빈 값으로.

---

## 파일 구조

| 파일 | 역할 | 변경 |
|---|---|---|
| `problem_parser.py` | 트라이얼 파서 진입점 | `render_board_assets` 인자, `ParsedProblem.board_image`, `_load_detached_rgba` |
| `test_problem_parser.py` | 파서 테스트 | 컷아웃 스위치 테스트 2개 |
| `trial_preview.py` | 응답 인코더 | WebP 인코더 `encode_preview_data_uri`, `compose_board_preview`, `PreviewStep.board_long_side`, `board`·`board_previews` |
| `test_trial_preview.py` | 인코더 테스트 | 이름 갱신 + `TestBoardPreviews` |
| `trial_config.py` | 환경변수 → 설정 | `_flag`, `board_previews` |
| `test_trial_config.py` | 설정 테스트 | 플래그 테스트 |
| `trial_server.py` | Vercel 함수 | 파서 인자 전달, `/api/config`·`/api/demo/config` 노출, 이벤트 `timing.preview_step`·`timing.board` |
| `test_trial_api.py` | API 테스트 | `FakeParser` 인자, config 기대값, 칠판용 응답·이벤트 테스트 |
| `public/trial_logic.js` | 화면 순수 논리 | `isSafeImageSource` webp, `hasBoardPreviews`, `cardImageSource` |
| `test_trial_web_logic.py` | node 테스트 + 마크업 검사 | 헬퍼 테스트, HTML 문구·토글 검사 |
| `public/app.js` | 화면 동작 | `previewMode`, 토글, 카드 이미지 전환 |
| `public/index.html`, `public/demo/index.html` | 마크업 | 토글 행, "15~30초" |
| `public/style.css` | 스타일 | `.problems-panel`, `.preview-toggle`, `.problem-card--board` |
| `scripts/trial_bench/complexity.py`, `memory.py` | 측정 | `--board` |
| `trial_input.py` | 입력 상한 | `DEFAULT_MAX_DRAWINGS_PER_PAGE` 재측정값 |
| `docs/web-trial-load.md`, `docs/web-trial-operations.md` | 문서 | §7 컷아웃 비용, 환경변수·점검·SQL·경보 기준 |

---

### Task 1: 파서 스위치와 `board_image`

**Files:**
- Modify: `problem_parser.py` (`ParsedProblem`, `_load_detached_rgb` 옆, `parse_problems`)
- Test: `test_problem_parser.py` (`TestParseProblems`)

- [ ] **Step 1: 실패하는 테스트 작성**

`test_problem_parser.py`의 `class TestParseProblems` 안, `test_images_survive_work_dir_removal` 바로 뒤에 추가:

```python
    def test_render_board_assets_adds_detached_rgba_cutouts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3]])
            result = parse_problems(path, work_dir=root / "work", render_board_assets=True)
            self.assertTrue((root / "work" / "problem_cutouts").is_dir())

        # The work dir is gone now; the cutouts must have been loaded into memory.
        self.assertEqual([1, 2, 3], [problem.number for problem in result.problems])
        for problem in result.problems:
            self.assertEqual("RGBA", problem.board_image.mode)
            self.assertGreater(problem.board_image.width, 0)
            alpha_min, alpha_max = problem.board_image.getchannel("A").getextrema()
            self.assertLess(alpha_min, 255)  # paper background became transparent
            self.assertGreater(alpha_max, 0)  # ink survived as chalk
            problem.board_image.load()

    def test_board_image_is_none_unless_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2]])
            result = parse_problems(path, work_dir=root / "work")
            self.assertFalse((root / "work" / "problem_cutouts").exists())
        self.assertEqual([None, None], [problem.board_image for problem in result.problems])
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest test_problem_parser.py -q -k "board_image or render_board_assets_adds"`
Expected: FAIL — `TypeError: parse_problems() got an unexpected keyword argument 'render_board_assets'` 와 `AttributeError: 'ParsedProblem' object has no attribute 'board_image'`

- [ ] **Step 3: 구현**

`problem_parser.py`에서 `ParsedProblem`을 바꾼다:

```python
@dataclass(frozen=True)
class ParsedProblem:
    problem_id: str
    number: int | None
    title: str
    regions: list[ParsedRegion]
    risk_flags: list[str]
    image: Image.Image
    # Chalk-on-transparent cutout (RGBA) when parse_problems(render_board_assets=True); else None.
    board_image: Image.Image | None = None
```

`_load_detached_rgb` 바로 아래에 추가:

```python
def _load_detached_rgba(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGBA")
```

`parse_problems` 시그니처와 본문:

```python
def parse_problems(
    source: Path,
    *,
    work_dir: Path,
    max_pages: int | None = None,
    subject: str = "unknown",
    ocr_mode: str = "none",
    ai_fallback_config: dict[str, Any] | None = None,
    render_board_assets: bool = False,
) -> ParseResult:
```

docstring 끝에 한 문단 추가:

```python
    With ``render_board_assets`` the desktop's chalk cutouts are rendered too and
    returned as ``ParsedProblem.board_image`` (RGBA); the trial's board preview
    is composited from them. Off by default: it costs about +60% of the asset
    stage and +0.1-0.35 GB RSS.
```

`build_problem_entries(...)` 호출에서 `render_board_assets=False`를 `render_board_assets=render_board_assets`로 바꾸고, `problems = [...]` 컴프리헨션의 `image=_load_detached_rgb(entry.crop_path),` 다음 줄에 추가:

```python
            board_image=_load_detached_rgba(entry.board_render_path) if render_board_assets else None,
```

모듈 docstring 4행 `after problem crops: no board cutouts, placement, EDB, or UI session.`을 `after problem crops: no placement, EDB, or UI session, and board cutouts only on request.`로 바꾼다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest test_problem_parser.py -q`
Expected: 전부 PASS (기존 `test_text_pdf_yields_numbered_problems_inside_their_pages`의 "cutouts 폴더 없음" 단언 포함)

- [ ] **Step 5: 커밋**

```bash
git status --short   # 남의 변경이 있으면 건드리지 않는다
git add problem_parser.py test_problem_parser.py
git commit -m "feat: let the trial parser return chalk cutouts on request

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: WebP 인코더, 단계 표, 칠판용 필드

**Files:**
- Modify: `trial_preview.py`
- Test: `test_trial_preview.py`

- [ ] **Step 1: 기존 테스트를 새 이름·형식에 맞추고 실패하는 테스트 추가**

`test_trial_preview.py` 상단 import를 바꾼다:

```python
import base64
import io
import json
import random
import unittest
from dataclasses import replace
from unittest import mock

from PIL import Image, features

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from structured_schema import Box
from trial_preview import (
    BOARD_BACKGROUND_RGB,
    PREVIEW_FORMAT,
    PREVIEW_MIME,
    PREVIEW_STEPS,
    PreviewBudgetExceeded,
    _payload_for_step,
    build_parse_body,
    build_parse_payload,
    compose_board_preview,
    encode_preview_data_uri,
)
```

`_decode`를 형식에 따라가게 바꾸고, 칠판용 픽스처를 추가한다:

```python
def _decode(data_uri: str) -> Image.Image:
    prefix = f"data:{PREVIEW_MIME};base64,"
    assert data_uri.startswith(prefix), data_uri[:40]
    return Image.open(io.BytesIO(base64.b64decode(data_uri[len(prefix):])))


def _board(width: int, height: int, seed: int) -> Image.Image:
    # A chalk cutout: uniform chalk RGB, ink only in the alpha channel.
    board = Image.new("RGBA", (width, height), (248, 249, 246, 0))
    board.putalpha(Image.frombytes("L", (width, height), random.Random(seed).randbytes(width * height)))
    return board


def _with_boards(result: ParseResult) -> ParseResult:
    result.problems[:] = [
        replace(problem, board_image=_board(*problem.image.size, seed=index + 40))
        for index, problem in enumerate(result.problems)
    ]
    return result
```

`class TestEncodeJpegDataUri`를 `class TestEncodePreviewDataUri`로, 그 안의 `encode_jpeg_data_uri` 호출 4곳을 `encode_preview_data_uri`로 바꾸고 메서드 두 개를 추가한다:

```python
    def test_uses_webp_in_this_runtime(self):
        self.assertTrue(features.check("webp"))
        self.assertEqual(("WEBP", "image/webp"), (PREVIEW_FORMAT, PREVIEW_MIME))
        self.assertTrue(encode_preview_data_uri(_noise(30, 20, 1), long_side=800, quality=70).startswith("data:image/webp;base64,"))

    def test_is_deterministic_for_the_same_pixels(self):
        first = encode_preview_data_uri(_noise(300, 200, 3), long_side=800, quality=70)
        second = encode_preview_data_uri(_noise(300, 200, 3), long_side=800, quality=70)
        self.assertEqual(first, second)
```

`class TestBuildParsePayload`의 `test_shape_and_coordinates` 끝에 두 줄 추가:

```python
        self.assertIsNone(problem["board"])
        self.assertFalse(payload["board_previews"])
```

파일 끝(`if __name__` 앞)에 새 클래스:

```python
class TestBoardPreviews(unittest.TestCase):
    def test_board_is_composited_and_encoded_at_the_step_size(self):
        payload = build_parse_payload(_with_boards(_result()), remaining_today=2, elapsed_ms=1, processed_page_limit=4)
        self.assertEqual(0, payload["preview_step"])
        self.assertTrue(payload["board_previews"])
        for problem in payload["problems"]:
            board = _decode(problem["board"])
            self.assertEqual(PREVIEW_STEPS[0].board_long_side, max(board.size))
            self.assertEqual("RGB", board.mode)

    def test_step_table_keeps_boards_until_the_last_step(self):
        self.assertEqual([800, 600, 600, None], [step.board_long_side for step in PREVIEW_STEPS])
        self.assertEqual([1000, 1000, 900, 700], [step.page_long_side for step in PREVIEW_STEPS])
        self.assertEqual([800, 600, 600, 450], [step.problem_long_side for step in PREVIEW_STEPS])

    def test_compose_flattens_onto_the_charcoal_board(self):
        transparent = Image.new("RGBA", (4, 4), (248, 249, 246, 0))
        self.assertEqual(BOARD_BACKGROUND_RGB, compose_board_preview(transparent).getpixel((0, 0)))
        opaque = Image.new("RGBA", (4, 4), (248, 249, 246, 255))
        self.assertEqual((248, 249, 246), compose_board_preview(opaque).getpixel((0, 0)))
        self.assertEqual("RGB", compose_board_preview(opaque).mode)

    def test_board_color_matches_the_desktop_default_theme(self):
        from build_problem_board_edb import BOARD_THEME_PALETTES, DEFAULT_BOARD_THEME

        self.assertEqual(BOARD_THEME_PALETTES[DEFAULT_BOARD_THEME]["background"], BOARD_BACKGROUND_RGB)

    def test_problems_without_a_cutout_get_null_board(self):
        result = _with_boards(_result(problem_count=3))
        result.problems[1] = replace(result.problems[1], board_image=None)
        payload = build_parse_payload(result, remaining_today=2, elapsed_ms=1, processed_page_limit=4)
        self.assertEqual([True, False, True], [problem["board"] is not None for problem in payload["problems"]])
        self.assertTrue(payload["board_previews"])

    def test_boards_are_dropped_on_the_last_step_before_rejecting(self):
        result = _with_boards(_result(problem_count=4))
        sizes = []
        for step_index in range(len(PREVIEW_STEPS)):
            payload = _payload_for_step(result, step_index, remaining_today=1, elapsed_ms=1, processed_page_limit=4)
            sizes.append(len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")))
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        payload, body = build_parse_body(result, remaining_today=1, elapsed_ms=1, processed_page_limit=4, budget_bytes=sizes[2] - 1)
        self.assertEqual(3, payload["preview_step"])
        self.assertFalse(payload["board_previews"])
        self.assertTrue(all(problem["board"] is None for problem in payload["problems"]))
        self.assertLessEqual(len(body), sizes[2] - 1)
        with self.assertRaises(PreviewBudgetExceeded):
            build_parse_body(result, remaining_today=1, elapsed_ms=1, processed_page_limit=4, budget_bytes=sizes[3] - 1)
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_preview.py -q`
Expected: FAIL — `ImportError: cannot import name 'BOARD_BACKGROUND_RGB' from 'trial_preview'`

- [ ] **Step 3: 구현**

`trial_preview.py` 전체를 다음 내용으로 바꾼다 (기존 `needs_review`, `_finite_bbox`, `build_parse_body`, `build_parse_payload`는 그대로):

```python
"""Encode a ParseResult into the /api/parse JSON body within Vercel's body limit."""

from __future__ import annotations

import base64
import io
import json
import math
from dataclasses import dataclass
from typing import Any

from PIL import Image, features

from problem_parser import ParseResult

# Vercel caps function response bodies at 4.5 MB; leave room for headers and slack.
RESPONSE_BUDGET_BYTES = 3_500_000

# WebP lossy is 25-40% smaller than JPEG at the same visual quality (spec §2-3). The
# pinned Pillow wheel ships libwebp; the JPEG fallback only guards a build without it.
PREVIEW_FORMAT = "WEBP" if features.check("webp") else "JPEG"
PREVIEW_MIME = "image/webp" if PREVIEW_FORMAT == "WEBP" else "image/jpeg"
# method 4 is ~1.7x slower for ~3% smaller output; method 6 takes minutes per request.
WEBP_METHOD = 2

# BOARD_THEME_PALETTES["charcoal"]["background"] in build_problem_board_edb. Kept as a
# constant so this module never imports the pipeline (rejected requests must not load
# OpenCV); test_trial_preview asserts the two stay equal.
BOARD_BACKGROUND_RGB = (24, 28, 32)


class PreviewBudgetExceeded(ValueError):
    """Even the smallest preview would exceed the function response budget."""

# Flags that mean the problem boundary itself is uncertain. Desktop review
# hints such as passage_cross_page_merge_check tag half of a normal Korean
# exam, so the trial does not badge them.
REVIEW_WORTHY_FLAGS = frozenset({"fallback_grouping", "merged_problem_block", "marker_conflicts", "hwp_oversegmentation"})


def needs_review(risk_flags: list[str]) -> bool:
    return any(flag in REVIEW_WORTHY_FLAGS for flag in risk_flags)


def _finite_bbox(region: Any) -> dict[str, float] | None:
    values = {
        "left": float(region.bbox.left),
        "top": float(region.bbox.top),
        "width": float(region.bbox.width),
        "height": float(region.bbox.height),
    }
    # Strict JSON has no NaN or Infinity; a region we cannot draw is better dropped.
    return values if all(math.isfinite(value) for value in values.values()) else None


@dataclass(frozen=True)
class PreviewStep:
    page_long_side: int
    problem_long_side: int
    quality: int
    # None drops the board previews at this step (last resort before rejecting).
    board_long_side: int | None = None


PREVIEW_STEPS = (
    PreviewStep(page_long_side=1000, problem_long_side=800, quality=78, board_long_side=800),
    PreviewStep(page_long_side=1000, problem_long_side=600, quality=65, board_long_side=600),
    PreviewStep(page_long_side=900, problem_long_side=600, quality=65, board_long_side=600),
    PreviewStep(page_long_side=700, problem_long_side=450, quality=55, board_long_side=None),
)


def _downscale(image: Image.Image, long_side: int) -> Image.Image:
    scale = long_side / max(image.size)
    if scale >= 1:
        return image
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    # HAMMING instead of LANCZOS: same preview size, roughly half the resize cost (LANCZOS
    # resize x25 cost about 0.17 s per request locally, ~0.7 s on Vercel) for no visible gain
    # at preview sizes. reducing_gap=2.0 additionally lets Pillow run an integer reduce()
    # first, which only engages on the >4x fallback steps.
    return image.resize(size, Image.Resampling.HAMMING, reducing_gap=2.0)


def encode_preview_data_uri(image: Image.Image, *, long_side: int, quality: int) -> str:
    preview = _downscale(image.convert("RGB"), long_side)
    buffer = io.BytesIO()
    if PREVIEW_FORMAT == "WEBP":
        preview.save(buffer, format="WEBP", quality=quality, method=WEBP_METHOD)
    else:
        preview.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=False)
    return f"data:{PREVIEW_MIME};base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def compose_board_preview(board_image: Image.Image) -> Image.Image:
    """Flatten a chalk-on-transparent cutout onto the charcoal board color."""
    rgba = board_image.convert("RGBA")
    flat = Image.new("RGBA", rgba.size, BOARD_BACKGROUND_RGB + (255,))
    flat.alpha_composite(rgba)
    return flat.convert("RGB")


def _payload_for_step(
    result: ParseResult,
    step_index: int,
    *,
    remaining_today: int | None,
    elapsed_ms: int,
    processed_page_limit: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step = PREVIEW_STEPS[step_index]
    problems = []
    for problem in result.problems:
        board = None
        if step.board_long_side is not None and problem.board_image is not None:
            board = encode_preview_data_uri(
                compose_board_preview(problem.board_image), long_side=step.board_long_side, quality=step.quality
            )
        problems.append(
            {
                "problem_id": problem.problem_id,
                "number": problem.number,
                "title": problem.title,
                "regions": [
                    {"page_id": region.page_id, "bbox": bbox}
                    for region in problem.regions
                    if (bbox := _finite_bbox(region)) is not None
                ],
                "risk_flags": list(problem.risk_flags),
                "needs_review": needs_review(problem.risk_flags),
                "preview": encode_preview_data_uri(problem.image, long_side=step.problem_long_side, quality=step.quality),
                "board": board,
            }
        )
    payload: dict[str, Any] = {
        "parser_version": result.parser_version,
        "elapsed_ms": elapsed_ms,
        "source_page_count": result.source_page_count,
        "processed_page_count": len(result.pages),
        "processed_page_limit": processed_page_limit,
        "remaining_today": remaining_today,
        "preview_step": step_index,
        "board_previews": any(problem["board"] is not None for problem in problems),
        "pages": [
            {
                "page_id": page.page_id,
                "index": page.index,
                "width": page.width,
                "height": page.height,
                "preview": encode_preview_data_uri(page.image, long_side=step.page_long_side, quality=step.quality),
            }
            for page in result.pages
        ],
        "problems": problems,
    }
    if extra:
        payload.update(extra)
    return payload
```

(`build_parse_body`·`build_parse_payload`는 기존 그대로 둔다.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_preview.py -q`
Expected: 전부 PASS. `test_many_noisy_crops_cannot_escape_the_real_response_budget`가 여전히 `PreviewBudgetExceeded`를 내야 한다(노이즈 crop 60개는 WebP로도 3.5 MB를 넘는다).

- [ ] **Step 5: 다른 사용처 확인**

Run: `grep -rn "encode_jpeg_data_uri" --include="*.py" . | grep -v "\.venv/"`
Expected: 출력 없음. 남아 있으면 그 파일도 `encode_preview_data_uri`로 바꾼다.

- [ ] **Step 6: 커밋**

```bash
git add trial_preview.py test_trial_preview.py
git commit -m "feat: encode trial previews as WebP and add the board preview

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: 설정 플래그, 서버 연결, 이벤트

**Files:**
- Modify: `trial_config.py`, `trial_server.py`
- Test: `test_trial_config.py`, `test_trial_api.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`test_trial_config.py`의 `test_defaults_without_environment` 안 `self.assertEqual(20.0, config.parse_wait_seconds)` 다음 줄에:

```python
        self.assertTrue(config.board_previews)
```

같은 클래스 끝에:

```python
    def test_board_previews_flag(self):
        for value in ("0", "false", "No", "OFF"):
            self.assertFalse(TrialConfig.from_env({"TRIAL_BOARD_PREVIEWS": value}).board_previews, value)
        for value in ("1", "true", "Yes", "ON"):
            self.assertTrue(TrialConfig.from_env({"TRIAL_BOARD_PREVIEWS": value}).board_previews, value)
        with self.assertRaises(ValueError):
            TrialConfig.from_env({"TRIAL_BOARD_PREVIEWS": "maybe"})
```

`test_trial_api.py`:

1. `FakeParser.__call__`을 바꾼다:

```python
    def __call__(self, source: Path, *, work_dir: Path, max_pages: int, render_board_assets: bool = False):
        self.calls.append(
            {
                "source": source,
                "work_dir": work_dir,
                "max_pages": max_pages,
                "render_board_assets": render_board_assets,
                "bytes": source.read_bytes(),
            }
        )
        if self.delay:
            time.sleep(self.delay)
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return self.result
```

2. `_result()` 아래에 픽스처 추가 (파일 상단 `from dataclasses import replace` import 추가):

```python
def _result_with_boards(problem_count: int = 2) -> ParseResult:
    result = _result(problem_count)
    result.problems[:] = [
        replace(problem, board_image=Image.new("RGBA", problem.image.size, (248, 249, 246, 128)))
        for problem in result.problems
    ]
    return result
```

3. `test_config_exposes_public_settings_only`의 기대 dict에 `"board_previews": True,`를 `"daily_limit": 3,` 다음에 추가.

4. `test_parses_first_pages_and_consumes_one_use`에서 `self.assertEqual(4, self.parser.calls[0]["max_pages"])` 다음 줄에:

```python
        self.assertTrue(self.parser.calls[0]["render_board_assets"])
        self.assertFalse(body["board_previews"])
        self.assertEqual([None, None], [problem["board"] for problem in body["problems"]])
        self.assertEqual((0, 0), (event["timing"]["preview_step"], event["timing"]["board"]))
```

(`event = self.store.events[-1]`는 그 테스트에서 이미 뒤에 정의되므로, 위 마지막 줄은 `event = ...` 정의 **뒤**에 둔다.)

5. `class TestParseSuccess`에 테스트 두 개 추가:

```python
    def test_board_previews_are_returned_and_counted_in_the_event(self):
        client = self.make_client(parser=FakeParser(result=_result_with_boards()))
        body = self.post_pdf(client).json()
        self.assertTrue(body["board_previews"])
        for problem in body["problems"]:
            self.assertTrue(problem["board"].startswith("data:image/"))
        self.assertEqual(1, self.store.events[-1]["timing"]["board"])
        self.assertEqual(0, self.store.events[-1]["timing"]["preview_step"])

    def test_board_previews_can_be_switched_off(self):
        config = TrialConfig(ip_salt="salt", cron_secret="cron-secret", board_previews=False)
        client = self.make_client(config=config, parser=FakeParser(result=_result_with_boards()))
        self.assertFalse(client.get("/api/config").json()["board_previews"])
        body = self.post_pdf(client).json()
        self.assertFalse(self.parser.calls[0]["render_board_assets"])
        # A parser that still returned cutouts would be a bug elsewhere; the response follows the parser.
        self.assertTrue(body["board_previews"])
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_config.py test_trial_api.py -q -x`
Expected: FAIL — `AttributeError: 'TrialConfig' object has no attribute 'board_previews'`

- [ ] **Step 3: 설정 구현**

`trial_config.py`의 `_positive_float` 아래에:

```python
_FLAG_TRUE = frozenset({"1", "true", "yes", "on"})
_FLAG_FALSE = frozenset({"0", "false", "no", "off"})


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _text(env, name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _FLAG_TRUE:
        return True
    if lowered in _FLAG_FALSE:
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off")
```

`TrialConfig`에 필드 추가 (`parse_wait_seconds: float = 20.0` 다음):

```python
    # Chalk-cutout previews next to the raw crops: +4-6 s and +0.1-0.35 GB per parse on
    # Vercel (spec 2026-09-16 §2-2). Off turns the cutouts, the response field and the
    # page toggle off together.
    board_previews: bool = True
```

`from_env`의 `parse_wait_seconds=...` 다음에:

```python
            board_previews=_flag(env, "TRIAL_BOARD_PREVIEWS", True),
```

- [ ] **Step 4: 서버 구현**

`trial_server.py`의 `parse_and_encode`에서:

```python
            result = parser(source, work_dir=work_dir, max_pages=config.limits.max_pages)
```
을
```python
            result = parser(
                source,
                work_dir=work_dir,
                max_pages=config.limits.max_pages,
                render_board_assets=config.board_previews,
            )
```
로 바꾸고, `timing["encode"] = _ms(encode_started_at)` 앞에 두 줄을 넣는다:

```python
            # Budget fallbacks are invisible in elapsed_ms; record which step answered and
            # whether the board previews survived it (ops doc §4-5).
            timing["preview_step"] = payload["preview_step"]
            timing["board"] = 1 if payload["board_previews"] else 0
```

`/api/config`와 `/api/demo/config`의 JSON에 `"daily_limit": ...` 다음 줄로 `"board_previews": config.board_previews,`를 추가한다.

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_config.py test_trial_api.py test_trial_demo_api.py test_trial_server.py -q`
Expected: 전부 PASS. `test_trial_demo_api.py`가 `/api/demo/config`를 dict 전체로 비교한다면 그 기대값에도 `"board_previews": True`를 넣는다.

- [ ] **Step 6: 커밋**

```bash
git add trial_config.py trial_server.py test_trial_config.py test_trial_api.py
git commit -m "feat: wire the board preview switch through config, server, and events

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 화면 순수 논리 (`trial_logic.js`)

**Files:**
- Modify: `public/trial_logic.js`
- Test: `test_trial_web_logic.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`test_trial_web_logic.py`의 `TestTrialWebLogic` 클래스에 추가:

```python
    def test_board_preview_helpers(self) -> None:
        run_node(
            """
            const logic = require('./public/trial_logic.js');
            assert.equal(logic.isSafeImageSource('data:image/webp;base64,AAAA'), true);
            assert.equal(logic.isSafeImageSource('data:image/jpeg;base64,AAAA'), true);
            assert.equal(logic.isSafeImageSource('data:image/svg+xml;base64,AAAA'), false);
            assert.equal(logic.isSafeImageSource('https://example.com/x.png'), false);
            assert.equal(logic.isSafeImageSource(null), false);
            const raw = 'data:image/webp;base64,RAW';
            const board = 'data:image/webp;base64,BOARD';
            assert.equal(logic.hasBoardPreviews({ board_previews: true, problems: [{ preview: raw, board }] }), true);
            assert.equal(logic.hasBoardPreviews({ board_previews: true, problems: [{ preview: raw, board: null }] }), false);
            assert.equal(logic.hasBoardPreviews({ board_previews: false, problems: [{ preview: raw, board }] }), false);
            assert.equal(logic.hasBoardPreviews({ problems: [] }), false);
            assert.equal(logic.hasBoardPreviews(null), false);
            assert.equal(logic.cardImageSource({ preview: raw, board }, 'board'), board);
            assert.equal(logic.cardImageSource({ preview: raw, board }, 'raw'), raw);
            assert.equal(logic.cardImageSource({ preview: raw, board: null }, 'board'), raw);
            assert.equal(logic.cardImageSource({ preview: 'javascript:alert(1)', board: null }, 'raw'), null);
            assert.equal(logic.cardImageSource(null, 'board'), null);
            """
        )
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_web_logic.py -q -k board_preview_helpers`
Expected: FAIL — node가 `logic.hasBoardPreviews is not a function`으로 종료 코드 1

- [ ] **Step 3: 구현**

`public/trial_logic.js`에서 `isSafeImageSource`를 다음으로 바꾸고 두 함수를 추가한다:

```js
  const SAFE_IMAGE_PREFIXES = ["data:image/webp;base64,", "data:image/jpeg;base64,"];

  function isSafeImageSource(value) {
    return typeof value === "string" && SAFE_IMAGE_PREFIXES.some(prefix => value.startsWith(prefix));
  }

  // The server sets board_previews only when at least one problem carries a board image;
  // the page still checks each source so a malformed payload never reaches an <img>.
  function hasBoardPreviews(payload) {
    if (!payload || payload.board_previews !== true || !Array.isArray(payload.problems)) {
      return false;
    }
    return payload.problems.some(problem => problem && isSafeImageSource(problem.board));
  }

  // Which image a card shows: the board cutout in "board" mode when it exists, else the raw crop.
  function cardImageSource(problem, mode) {
    if (!problem) {
      return null;
    }
    if (mode === "board" && isSafeImageSource(problem.board)) {
      return problem.board;
    }
    return isSafeImageSource(problem.preview) ? problem.preview : null;
  }
```

반환 객체에 `cardImageSource,`(`FEATURES,` 다음)와 `hasBoardPreviews,`(`interpretError,` 다음)를 알파벳 순서로 넣는다.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_web_logic.py -q`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add public/trial_logic.js test_trial_web_logic.py
git commit -m "feat: add board preview helpers to the trial page logic

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: 토글 화면 — `app.js`, 두 HTML, CSS, 문구

**Files:**
- Modify: `public/app.js`, `public/index.html`, `public/demo/index.html`, `public/style.css`
- Test: `test_trial_web_logic.py` (마크업 검사)

- [ ] **Step 1: 실패하는 마크업 테스트 작성**

`test_trial_web_logic.py` 끝(`if __name__` 앞)에:

```python
class TestTrialPageMarkup(unittest.TestCase):
    PAGES = ("public/index.html", "public/demo/index.html")

    def test_both_pages_have_the_preview_toggle_and_the_new_wait_copy(self) -> None:
        for relative in self.PAGES:
            html = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(page=relative):
                self.assertIn('id="preview-toggle"', html)
                self.assertIn('data-mode="raw" aria-pressed="true"', html)
                self.assertIn('data-mode="board" aria-pressed="false"', html)
                self.assertIn('class="problems-panel"', html)
                self.assertIn("보통 15~30초 걸려요", html)
                self.assertNotIn("10~20초", html)

    def test_app_script_switches_cards_through_the_shared_logic(self) -> None:
        script = (PROJECT_ROOT / "public/app.js").read_text(encoding="utf-8")
        self.assertIn("logic.cardImageSource(", script)
        self.assertIn("logic.hasBoardPreviews(", script)
        self.assertIn('"problem-card--board"', script)
        css = (PROJECT_ROOT / "public/style.css").read_text(encoding="utf-8")
        self.assertIn(".problem-card--board img", css)
        self.assertIn(".preview-toggle button[aria-pressed=\"true\"]", css)
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_web_logic.py -q -k Markup`
Expected: FAIL — `'id="preview-toggle"' not found`

- [ ] **Step 3: HTML**

`public/index.html`과 `public/demo/index.html` 둘 다에서:

(a) `<p class="muted">보통 10~20초 걸려요<span aria-hidden="true">` → `<p class="muted">보통 15~30초 걸려요<span aria-hidden="true">`

(b) 결과 본문을 바꾼다:

```html
      <div class="result-body" id="result-body">
        <div class="pages" id="pages" aria-label="페이지 미리보기"></div>
        <div class="problems-panel">
          <div class="problems-head">
            <span class="problems-head__label">찾은 문항</span>
            <div class="preview-toggle" id="preview-toggle" role="group" aria-label="문항 보기 방식" hidden>
              <button type="button" data-mode="raw" aria-pressed="true">원본</button>
              <button type="button" data-mode="board" aria-pressed="false">칠판용</button>
            </div>
          </div>
          <ol class="problems" id="problems" aria-label="찾은 문항"></ol>
        </div>
      </div>
```

- [ ] **Step 4: CSS**

`public/style.css`에서 `.problems { ... }` 블록(`position: sticky;`로 시작하는 것)을 다음으로 바꾼다:

```css
.problems-panel {
  position: sticky;
  top: 16px;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  gap: 10px;
  max-height: calc(100vh - 32px);
}

.problems-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 32px; }

.problems-head__label { color: var(--ink-2); font-size: var(--text-sm); font-weight: 700; }

.preview-toggle {
  display: inline-flex;
  padding: 2px;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius);
  background: var(--surface);
}

.preview-toggle button {
  min-height: 30px;
  padding: 4px 12px;
  border: 0;
  border-radius: calc(var(--radius) - 2px);
  background: transparent;
  color: var(--ink-2);
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
  transition: background var(--motion-base), color var(--motion-base);
}

.preview-toggle button[aria-pressed="true"] { background: var(--accent); color: var(--on-ink); }

.problems {
  display: grid;
  gap: 10px;
  min-height: 0;
  margin: 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}
```

`.problem-card__ai { ... }` 앞에 추가:

```css
/* Board mode: the chalk cutout is already flattened onto charcoal; match the frame to it. */
.problem-card--board img { background: #181c20; border-color: #181c20; }
```

`@media (max-width: 760px)` 블록 안의 `.problems { position: static; max-height: none; overflow: visible; }`를 다음으로 바꾼다:

```css
  .problems-panel { position: static; max-height: none; }
  .problems { overflow: visible; }
```

- [ ] **Step 5: `app.js`**

(a) `state`에 `previewMode: "raw",`를 `popupContext: {},` 다음에 추가.

(b) `renderProblems` 안의 이미지 생성 블록

```js
      if (logic.isSafeImageSource(problem.preview)) {
        const image = document.createElement("img");
        image.src = problem.preview;
        image.alt = `${logic.problemLabel(problem)} 미리보기`;
        image.loading = "lazy";
        item.appendChild(image);
      }
```
을
```js
      const source = logic.cardImageSource(problem, state.previewMode);
      if (source) {
        const image = document.createElement("img");
        image.src = source;
        image.alt = `${logic.problemLabel(problem)} 미리보기`;
        image.loading = "lazy";
        item.appendChild(image);
      }
      item.classList.toggle("problem-card--board", isBoardSource(problem, source));
```
로 바꾼다.

(c) `renderProblems` 앞에 세 함수를 추가:

```js
  function isBoardSource(problem, source) {
    return state.previewMode === "board" && source !== null && source === problem.board;
  }

  function syncPreviewToggle(payload) {
    const toggle = $("preview-toggle");
    const available = logic.hasBoardPreviews(payload);
    if (!available) {
      state.previewMode = "raw";
    }
    toggle.hidden = !available;
    for (const button of toggle.querySelectorAll("[data-mode]")) {
      button.setAttribute("aria-pressed", String(button.dataset.mode === state.previewMode));
    }
  }

  function setPreviewMode(mode) {
    if ((mode !== "raw" && mode !== "board") || mode === state.previewMode) {
      return;
    }
    state.previewMode = mode;
    const payload = state.lastPayload;
    if (payload) {
      const byId = new Map(payload.problems.map(problem => [problem.problem_id, problem]));
      for (const item of $("problems").querySelectorAll(".problem-card")) {
        const problem = byId.get(item.dataset.problemId);
        const image = item.querySelector("img");
        if (!problem || !image) {
          continue;
        }
        const source = logic.cardImageSource(problem, state.previewMode);
        if (source) {
          image.src = source;
        }
        item.classList.toggle("problem-card--board", isBoardSource(problem, source));
      }
    }
    syncPreviewToggle(payload);
  }
```

(d) `renderResult`에서 `renderPages(payload);` 앞에 `syncPreviewToggle(payload);`를 넣는다(토글 상태를 먼저 맞춰야 카드가 그 모드로 그려진다).

(e) `loadConfig`의 데모 초기화 콜백 안 `$("problems").replaceChildren();` 다음에 `$("preview-toggle").hidden = true;`를 추가.

(f) `bindEvents`에서 `$("pages-banner").addEventListener(...)` 앞에:

```js
    $("preview-toggle").addEventListener("click", event => {
      const button = event.target.closest("[data-mode]");
      if (button) {
        setPreviewMode(button.dataset.mode);
      }
    });
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_web_logic.py test_trial_demo_web.py -q`
Expected: 전부 PASS (`test_demo_page_uses_shared_renderer_and_keeps_premium_gates`가 `하루 3회`·`limit_daily` 부재를 계속 확인한다)

- [ ] **Step 7: 브라우저 확인**

`.claude/launch.json`에 다음 항목이 없으면 추가한다:

```json
{
  "version": "0.0.1",
  "configurations": [
    {
      "name": "trial-local",
      "runtimeExecutable": ".venv/bin/python",
      "runtimeArgs": ["scripts/run_trial_local.py", "--turnstile-test", "--daily-limit", "50"],
      "port": 8790
    }
  ]
}
```

앱 내 브라우저로 `http://127.0.0.1:8790/`을 열고 `/Users/clmagi/Downloads/파일/문제 모음집/01 물리학Ⅰ_문제지.pdf`를 올린다. 확인: 결과에 [원본 | 칠판용] 토글이 보이고, 칠판용을 누르면 20개 카드가 어두운 배경에 분필색 글자로 바뀌며 5번 문항의 도르래 그림은 흰 상자로 남는다. 다시 원본을 누르면 되돌아온다. 페이지 오버레이 박스는 두 모드에서 그대로다. 콘솔에 CSP 오류가 없다. 스크린샷을 `docs/`가 아닌 스크래치패드에 남긴다.

- [ ] **Step 8: 커밋**

```bash
git add public/app.js public/index.html public/demo/index.html public/style.css test_trial_web_logic.py
git commit -m "feat: show board previews behind a raw/board toggle on the trial pages

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

(`.claude/launch.json`은 커밋하지 않는다.)

---

### Task 6: 벤치 `--board`, 재측정, 드로잉 상한 확정

**Files:**
- Modify: `scripts/trial_bench/complexity.py`, `scripts/trial_bench/memory.py`, `trial_input.py`, `test_trial_config.py`, `docs/web-trial-load.md`

- [ ] **Step 1: `complexity.py`에 플래그**

`main()`의 `parser.add_argument("--pages", ...)` 다음에:

```python
    parser.add_argument(
        "--board",
        action="store_true",
        help="also render the chalk cutouts, as the trial does with TRIAL_BOARD_PREVIEWS on",
    )
```

`result = parse_in_scratch(pdf, parse_problems, max_pages=args.pages)`를
`result = parse_in_scratch(pdf, parse_problems, max_pages=args.pages, render_board_assets=args.board)`로 바꾼다. 모듈 docstring의 Usage에 한 줄 추가:

```
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 4500 --drawings 1500 2000 2500 --pages 4 --board
```

- [ ] **Step 2: `memory.py`에 플래그**

`run_two_overlapping_parses`와 `run_single_parse`에 `board: bool = False` 키워드 인자를 더하고 `parse_in_scratch(pdf, parse_problems)` 호출 두 곳을 `parse_in_scratch(pdf, parse_problems, render_board_assets=board)`로 바꾼다. `measure_case(pdf, *, concurrency: int = 2, board: bool = False)`로 바꾸고 워커 명령을:

```python
    command = [sys.executable, str(SCRIPT_PATH), "--concurrency", str(concurrency), "--worker", str(pdf)]
    if board:
        command.append("--board")
```

`main()`에 `parser.add_argument("--board", action="store_true", help="render chalk cutouts too (TRIAL_BOARD_PREVIEWS on)")`를 `--worker` 앞에 추가하고, 워커 분기를:

```python
    if args.worker is not None:
        payload = (
            run_single_parse(args.worker, board=args.board)
            if args.concurrency == 1
            else run_two_overlapping_parses(args.worker, board=args.board)
        )
        print(json.dumps(payload))
        return 0
```

`main()` 안의 모든 `measure_case(pdf, concurrency=args.concurrency)` 호출을 `measure_case(pdf, concurrency=args.concurrency, board=args.board)`로 바꾼다.

- [ ] **Step 3: 스크립트가 여전히 도는지 확인**

Run: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 500 --drawings 0 --pages 2 --board`
Expected: 표 한 행, `assets_ms`가 `--board` 없이 돌린 값보다 크다.

Run: `.venv/bin/python -m pytest test_trial_bench.py -q`
Expected: 전부 PASS

- [ ] **Step 4: 복잡도 재측정과 상한 결정**

Run: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 4500 --drawings 1500 2000 2500 --pages 4 --board`

규칙: `vercel_est_s`가 30 이하인 가장 큰 drawings 값을 `trial_input.DEFAULT_MAX_DRAWINGS_PER_PAGE`로 쓴다(예상 2000). 값이 2500에서도 30 이하면 2500을 유지하고 그 사실을 기록한다. `test_trial_config.py`의 `self.assertEqual(2500, defaults.limits.max_drawings_per_page)`를 새 값으로 바꾼다.

- [ ] **Step 5: 메모리 재측정**

Run:
```bash
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py ~/edb-trial-bench/inputs/*.pdf --concurrency 1 --board
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py ~/edb-trial-bench/inputs/2026학년도-9월-모평-국어-언어와매체.pdf ~/edb-trial-bench/inputs/earth_input.pdf ~/edb-trial-bench/inputs/social_saengwoon_2025suneung_9wolmopyeong_20240904.pdf --concurrency 2 --board
```
Expected: 단일 파싱 전부 2 GB의 60%(1,228.8 MB) 아래. 겹침 2건은 넘을 수 있으며, 그것이 인스턴스당 1건을 유지하는 근거다.

- [ ] **Step 6: `docs/web-trial-load.md` §7 작성**

파일 끝에 추가:

```markdown
## 7. 칠판용 컷아웃 비용 (2026-09-17)

설계: `docs/superpowers/specs/2026-09-16-web-trial-board-preview-design.md`. 로컬 M4·스레드 1·새 폴더 복사, Vercel 시간 ≈ ×4, 메모리 ≈ ×1.

### 7-1. 컷아웃 끔 → 켬 (설계 §2-2 재수록)

| 시험지 | 쪽 | 문항 | 문항 자산 단계 | 최대 RSS 1건 |
|---|---|---|---|---|
| 2026 9월 모평 국어 | 4 | 15 | 0.65 → 1.73초 | 569 → 869 MB |
| 2026 9월 모평 국어 | 5 | 20 | 0.92 → 2.27초 | 592 → 894 MB |
| 2025 수능 국어 | 4 | 15 | 0.66 → 1.70초 | 617 → 966 MB |
| 지구과학 | 4 | 20 | 0.66 → 1.93초 | 574 → 684 MB |
| 물리학Ⅰ | 4 | 20 | 0.64 → 1.89초 | 575 → 676 MB |
| 전자기 교재 | 5 | 20 | 0.58 → 2.07초 | 415 → 698 MB |

5쪽 국어 2건 겹침: 984 → 1,471 MB. 인스턴스당 1건이 필수다.

### 7-2. 복잡도 상한 재측정 (`complexity.py --words 4500 --drawings 1500 2000 2500 --pages 4 --board`)

(Step 4의 표를 그대로 붙인다)

결정: `TRIAL_MAX_DRAWINGS_PER_PAGE` 기본값 = (선택한 값). 근거: 두 상한을 채운 4쪽 입력의 Vercel 추정이 30초 이하.

### 7-3. 메모리 (`memory.py ... --board`)

(Step 5의 표 두 개를 붙인다)
```

- [ ] **Step 7: 테스트와 커밋**

Run: `.venv/bin/python -m pytest test_trial_config.py test_trial_input.py test_trial_bench.py -q`
Expected: 전부 PASS

```bash
git add scripts/trial_bench/complexity.py scripts/trial_bench/memory.py trial_input.py test_trial_config.py docs/web-trial-load.md
git commit -m "chore: measure the board preview cost and lower the drawing cap

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: 운영 문서, 전체 테스트

**Files:**
- Modify: `docs/web-trial-operations.md`

- [ ] **Step 1: 환경변수 표**

§2-3 표의 `TRIAL_MAX_WORDS_PER_PAGE` 행에서 `기본 4500 / 2500`을 Task 6의 값으로 고치고, 그 행 다음에 추가:

```markdown
| `TRIAL_BOARD_PREVIEWS` | 기본 `1`(켬). 문항마다 칠판용(분필색·어두운 배경) 미리보기를 원본과 함께 보내고 화면에 [원본 / 칠판용] 토글을 연다. `0`이면 컷아웃을 만들지도 보내지도 않는다. 켜면 4쪽 한 건이 Vercel 기준 +4~6초, RSS +0.1~0.35 GB (`docs/web-trial-load.md` §7). `1/0`, `true/false`, `yes/no`, `on/off`만 받는다 | 아니오 |
```

- [ ] **Step 2: 점검 항목**

§3 브라우저 점검 1번 문장 끝에 붙인다: ` 문항 목록 위 [원본 / 칠판용]을 눌러 카드가 어두운 배경에 분필색 글자로 바뀌고 그림은 원본 그대로 남는지, 다시 원본으로 돌아오는지 본다.`

- [ ] **Step 3: 경보 기준과 SQL**

§4-3의 `p95가 20초를 넘으면 \`TRIAL_MAX_PAGES\`를 줄이거나 Function CPU를 Performance로 올린다.`를
`p95가 30초를 넘으면 먼저 \`TRIAL_BOARD_PREVIEWS=0\`으로 칠판용을 끄고(한 건 4~6초 절약), 그래도 넘으면 \`TRIAL_MAX_PAGES\`를 줄이거나 Function CPU를 Performance로 올린다.`로 바꾼다.

§4-5 SQL 블록 끝(`-- words나 drawings가 ...` 주석 뒤)에 추가:

```sql
-- 미리보기 예산 폴백: preview_step > 0 이면 축소 단계로 답했고, board = 0 이면 칠판용이 빠진 응답이다.
select (timing->>'preview_step')::int as preview_step, (timing->>'board')::int as board, count(*)
from public.trial_events
where kind = 'parse' and status = 200 and created_at > now() - interval '7 days'
group by 1, 2 order by 1, 2;
```

§8의 `- 4페이지·4MB·PDF 복잡도·응답 크기·인스턴스당 작업 제한은 동일하다.` 문장 끝에 ` 칠판용 미리보기 토글도 같다.`를 붙인다.

- [ ] **Step 4: 전체 테스트**

Run: `LC_ALL=en_US.UTF-8 GEMINI_API_KEY= .venv/bin/python -m pytest -q -x`
Expected: 전부 PASS. `test_trial_sql`이 로케일로 실패하면 `LC_ALL=C TRIAL_PG_BIN=/opt/homebrew/opt/postgresql@17/bin`으로 다시 돌린다(둘 다 기록된 우회법).

- [ ] **Step 5: 커밋**

```bash
git add docs/web-trial-operations.md
git commit -m "docs: describe the board preview switch, checks, and fallback query

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## 완료 기준

- 스펙 §1 표의 결정 9개가 모두 코드·문서에 반영됐다(Task 1~7).
- 전체 pytest 통과, node 테스트 통과, 브라우저에서 토글 동작 확인.
- 푸시는 하지 않는다. `web-trial` 푸시는 공개 배포이므로 사용자 승인이 필요하다.
