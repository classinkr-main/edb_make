# Web Trial Page-Cut Continuation Marker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 체험판이 앞 4쪽만 처리할 때 지문 묶음의 남은 문항이 다음 쪽에 있음을 카드 칩과 배너로 알린다.

**Architecture:** 새 순수 모듈 `trial_continuation.py`가 `ParseResult`에서 "지문 a~b" 단위별로 찾은 최대 번호보다 큰 남은 번호를 계산한다(원본이 처리 쪽수보다 길 때만). `trial_preview.build_parse_body`가 이를 한 번 계산해 `problems[].continuation`으로 싣고, 서버는 `timing.continued`를 기록한다. 화면은 `trial_logic.continuationNote`로 칩 문구를 만들고 배너에 "N번까지 찾았어요"를 끼운다. 공용 파이프라인은 건드리지 않는다.

**Tech Stack:** Python 3.12/3.14, unittest/pytest, 순수 JS + node 테스트.

**Spec:** `docs/superpowers/specs/2026-09-17-web-trial-page-cut-continuation-design.md`

**작업 규칙:** 브랜치 `new_web1`, 워크트리 `.claude/worktrees/new_web1`. 파이썬은 `.venv/bin/python`, 파서를 실행하는 테스트는 `GEMINI_API_KEY=` 빈 값. 파일 지정 `git add`만 쓴다.

---

## 파일 구조

| 파일 | 역할 | 변경 |
|---|---|---|
| `trial_continuation.py` | 이어짐 규칙 (신규, 웹 의존성 없음) | `passage_range`, `continuations` |
| `test_trial_continuation.py` | 규칙 테스트 (신규) | |
| `trial_preview.py` | 응답 인코더 | `problems[].continuation` |
| `test_trial_preview.py` | | `TestContinuation` |
| `trial_server.py` | Vercel 함수 | `timing.continued` |
| `test_trial_api.py` | | 지문 잘림 픽스처·응답·이벤트 |
| `public/trial_logic.js` | 화면 순수 논리 | `continuationNote`, `pagesBanner` 문구 |
| `public/app.js` | 카드 렌더 | 이어짐 칩 |
| `public/style.css` | | `.continue-chip` |
| `test_trial_web_logic.py` | node·마크업 테스트 | |

---

### Task 1: 이어짐 규칙 모듈

**Files:**
- Create: `trial_continuation.py`
- Test: `test_trial_continuation.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`test_trial_continuation.py`:

```python
import unittest

from PIL import Image

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from structured_schema import Box
from trial_continuation import continuations, passage_range


def _problem(problem_id: str, number: int | None, title: str) -> ParsedProblem:
    return ParsedProblem(
        problem_id=problem_id,
        number=number,
        title=title,
        regions=[ParsedRegion(page_id="p1", bbox=Box(left=0.0, top=0.0, width=10.0, height=10.0))],
        risk_flags=[],
        image=Image.new("RGB", (10, 10), "white"),
    )


def _result(problems: list[ParsedProblem], *, source_pages: int, processed_pages: int = 1) -> ParseResult:
    pages = [
        ParsedPage(page_id=f"p{index + 1}", index=index, width=10, height=10, image=Image.new("RGB", (10, 10), "white"))
        for index in range(processed_pages)
    ]
    return ParseResult(pages=pages, problems=problems, source_page_count=source_pages, parser_version="dev", timing_ms={})


class TestPassageRange(unittest.TestCase):
    def test_parses_the_pipeline_title_and_common_dash_variants(self):
        self.assertEqual((10, 13), passage_range("지문 10~13"))
        self.assertEqual((4, 9), passage_range("지문 4∼9"))
        self.assertEqual((1, 3), passage_range("지문 1-3"))
        self.assertEqual((16, 17), passage_range("지문 16～17"))

    def test_rejects_non_ranges(self):
        for title in ("지문", "3.", "지문 9~4", "", "지문 a~b"):
            self.assertIsNone(passage_range(title), title)


class TestContinuations(unittest.TestCase):
    def test_trailing_numbers_of_a_passage_continue_on_the_next_page(self):
        result = _result(
            [_problem("pass", None, "지문 10~13"), _problem("q10", 10, "10."), _problem("q11", 11, "11."), _problem("q12", 12, "12.")],
            source_pages=16,
            processed_pages=4,
        )
        self.assertEqual({"pass": {"numbers": [13], "page": 5}}, continuations(result))

    def test_gaps_below_the_last_found_number_are_recognition_misses_not_cuts(self):
        result = _result(
            [_problem("pass", None, "지문 10~13"), _problem("q11", 11, "11."), _problem("q12", 12, "12.")],
            source_pages=16,
            processed_pages=4,
        )
        self.assertEqual({"pass": {"numbers": [13], "page": 5}}, continuations(result))

    def test_multiple_trailing_numbers(self):
        result = _result([_problem("pass", None, "지문 11~13"), _problem("q11", 11, "11.")], source_pages=16, processed_pages=4)
        self.assertEqual({"pass": {"numbers": [12, 13], "page": 5}}, continuations(result))

    def test_complete_documents_have_no_continuations(self):
        result = _result([_problem("pass", None, "지문 10~13"), _problem("q10", 10, "10.")], source_pages=4, processed_pages=4)
        self.assertEqual({}, continuations(result))

    def test_no_numbered_problems_means_no_continuations(self):
        result = _result([_problem("pass", None, "지문 10~13")], source_pages=16, processed_pages=4)
        self.assertEqual({}, continuations(result))

    def test_passages_that_end_before_the_last_number_are_not_marked(self):
        result = _result(
            [_problem("early", None, "지문 1~3"), _problem("q3", 3, "3."), _problem("q13", 13, "13.")],
            source_pages=16,
            processed_pages=4,
        )
        self.assertEqual({}, continuations(result))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_continuation.py -q`
Expected: 수집 오류 `ModuleNotFoundError: No module named 'trial_continuation'`

- [ ] **Step 3: 구현**

`trial_continuation.py`:

```python
"""Which passage units continue past the trial's page cap.

Pure functions on ParseResult (spec 2026-09-17 page-cut continuation §3). The
only reliable signal is the passage range in the unit title: numbers in
"지문 a~b" above the highest number found anywhere must sit on later pages.
Geometry is useless here because pages are margin-cropped, so complete exams
also end flush with the page bottom.
"""

from __future__ import annotations

import re
from typing import Any

from problem_parser import ParseResult

PASSAGE_RANGE = re.compile(r"지문\s*(\d+)\s*[~∼～\-–]\s*(\d+)")


def passage_range(title: str) -> tuple[int, int] | None:
    match = PASSAGE_RANGE.search(title or "")
    if match is None:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return (start, end) if start <= end else None


def continuations(result: ParseResult) -> dict[str, dict[str, Any]]:
    """{problem_id: {"numbers": [...], "page": next_page}} for passages cut by the page cap."""
    processed = len(result.pages)
    if result.source_page_count <= processed:
        return {}
    found = [problem.number for problem in result.problems if problem.number is not None]
    if not found:
        return {}
    last_found = max(found)
    continued: dict[str, dict[str, Any]] = {}
    for problem in result.problems:
        if problem.number is not None:
            continue
        bounds = passage_range(problem.title)
        if bounds is None:
            continue
        missing = [number for number in range(bounds[0], bounds[1] + 1) if number > last_found]
        if missing:
            continued[problem.problem_id] = {"numbers": missing, "page": processed + 1}
    return continued
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_continuation.py -q`
Expected: 8 passed

- [ ] **Step 5: 커밋**

```bash
git add trial_continuation.py test_trial_continuation.py
git commit -m "feat: compute which passages continue past the trial page cap

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: 응답 필드

**Files:**
- Modify: `trial_preview.py`
- Test: `test_trial_preview.py`

- [ ] **Step 1: 실패하는 테스트**

`test_trial_preview.py`의 `test_shape_and_coordinates` 끝에:

```python
        self.assertIsNone(problem["continuation"])
```

파일 끝(`if __name__` 앞)에:

```python
class TestContinuation(unittest.TestCase):
    def test_cut_passage_carries_its_remaining_numbers_on_every_step(self):
        result = _result(problem_count=3)  # numbers 1..3, source_page_count=16, one processed page
        result.problems[:] = [
            replace(result.problems[0], problem_id="q10", number=10, title="10."),
            replace(result.problems[1], problem_id="q11", number=11, title="11."),
            replace(result.problems[2], problem_id="pass", number=None, title="지문 10~13"),
        ]
        for step_index in range(len(PREVIEW_STEPS)):
            payload = _payload_for_step(
                result, step_index, remaining_today=1, elapsed_ms=1, processed_page_limit=4,
                continuation_by_id=continuations(result),
            )
            by_id = {problem["problem_id"]: problem["continuation"] for problem in payload["problems"]}
            self.assertEqual({"q10": None, "q11": None, "pass": {"numbers": [12, 13], "page": 2}}, by_id)
        payload = build_parse_payload(result, remaining_today=1, elapsed_ms=1, processed_page_limit=4)
        self.assertEqual({"numbers": [12, 13], "page": 2}, payload["problems"][2]["continuation"])
```

상단 import에 `from trial_continuation import continuations`를 추가한다.

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_preview.py -q -k "continuation or shape"`
Expected: FAIL — `KeyError: 'continuation'` 또는 `TypeError: unexpected keyword argument 'continuation_by_id'`

- [ ] **Step 3: 구현**

`trial_preview.py`:
- import: `from trial_continuation import continuations`
- `_payload_for_step(...)` 시그니처에 `continuation_by_id: dict[str, dict[str, Any]] | None = None` 키워드 추가. 본문 첫 줄에 `continuation_by_id = continuation_by_id or {}`. 문항 dict에 `"board": board,` 다음으로 `"continuation": continuation_by_id.get(problem.problem_id),`.
- `build_parse_body`: 루프 전에 `continuation_by_id = continuations(result)`를 두고 `_payload_for_step(..., continuation_by_id=continuation_by_id)`로 넘긴다.

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_preview.py -q`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add trial_preview.py test_trial_preview.py
git commit -m "feat: mark cut passages in the trial response

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: 서버 이벤트

**Files:**
- Modify: `trial_server.py`
- Test: `test_trial_api.py`

- [ ] **Step 1: 실패하는 테스트**

`test_trial_api.py`의 `_result_with_boards` 다음에 픽스처:

```python
def _result_with_cut_passage() -> ParseResult:
    result = _result(problem_count=3)
    result.problems[:] = [
        replace(result.problems[0], problem_id="q10", number=10, title="10."),
        replace(result.problems[1], problem_id="q11", number=11, title="11."),
        replace(result.problems[2], problem_id="pass", number=None, title="지문 10~13"),
    ]
    return result
```

`test_parses_first_pages_and_consumes_one_use`의 `self.assertEqual((0, 0), (event["timing"]["preview_step"], event["timing"]["board"]))` 다음에:

```python
        self.assertEqual(0, event["timing"]["continued"])
        self.assertTrue(all(problem["continuation"] is None for problem in body["problems"]))
```

`class TestParseSuccess`에 추가:

```python
    def test_cut_passage_is_marked_and_counted(self):
        client = self.make_client(parser=FakeParser(result=_result_with_cut_passage()))
        body = self.post_pdf(client).json()
        by_id = {problem["problem_id"]: problem["continuation"] for problem in body["problems"]}
        self.assertEqual({"q10": None, "q11": None, "pass": {"numbers": [12, 13], "page": 2}}, by_id)
        self.assertEqual(1, self.store.events[-1]["timing"]["continued"])
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_api.py -q -k "cut_passage or consumes_one_use"`
Expected: FAIL — `KeyError: 'continued'`

- [ ] **Step 3: 구현**

`trial_server.py`의 `timing["board"] = ...` 다음 줄에:

```python
            timing["continued"] = sum(1 for problem in payload["problems"] if problem["continuation"])
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_api.py test_trial_demo_api.py -q`
Expected: 전부 PASS

- [ ] **Step 5: 커밋**

```bash
git add trial_server.py test_trial_api.py
git commit -m "feat: count cut passages in trial parse events

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 화면 논리

**Files:**
- Modify: `public/trial_logic.js`
- Test: `test_trial_web_logic.py`

- [ ] **Step 1: 실패하는 테스트**

`TestTrialWebLogic`에 추가:

```python
    def test_continuation_note_and_banner_count(self) -> None:
        run_node(
            """
            const logic = require('./public/trial_logic.js');
            assert.equal(logic.continuationNote({ continuation: { numbers: [13], page: 5 } }), '13번은 5쪽부터예요');
            assert.equal(logic.continuationNote({ continuation: { numbers: [12, 13], page: 5 } }), '12·13번은 5쪽부터예요');
            assert.equal(logic.continuationNote({ continuation: null }), null);
            assert.equal(logic.continuationNote({ continuation: { numbers: [], page: 5 } }), null);
            assert.equal(logic.continuationNote(null), null);
            const cut = { source_page_count: 16, processed_page_count: 4, processed_page_limit: 4,
                          problems: [{ number: 10 }, { number: null }, { number: 12 }] };
            assert.equal(logic.pagesBanner(cut).text, '✦ 무료 체험은 앞 4쪽까지예요 · 12번까지 찾았어요 · 나머지 12쪽은 프리미엄으로');
            assert.deepEqual(logic.pagesBanner(cut).context, { max: 4, rest: 12 });
            const noNumbers = { source_page_count: 16, processed_page_count: 4, processed_page_limit: 4, problems: [{ number: null }] };
            assert.equal(logic.pagesBanner(noNumbers).text, '✦ 무료 체험은 앞 4쪽까지예요 · 나머지 12쪽은 프리미엄으로');
            assert.equal(logic.pagesBanner({ source_page_count: 4, processed_page_count: 4, problems: [{ number: 20 }] }), null);
            """
        )
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_web_logic.py -q -k continuation_note`
Expected: FAIL — `logic.continuationNote is not a function`

- [ ] **Step 3: 구현**

`public/trial_logic.js`의 `pagesBanner`를 바꾸고 `continuationNote`를 추가한다:

```js
  function highestNumber(payload) {
    const problems = Array.isArray(payload.problems) ? payload.problems : [];
    const numbers = problems.map(problem => Number(problem && problem.number)).filter(n => Number.isFinite(n) && n > 0);
    return numbers.length ? Math.max(...numbers) : null;
  }

  function pagesBanner(payload) {
    const source = Number(payload.source_page_count) || 0;
    const processed = Number(payload.processed_page_count) || 0;
    const max = Number(payload.processed_page_limit) || processed;
    if (source <= processed) {
      return null;
    }
    const rest = source - processed;
    const highest = highestNumber(payload);
    const found = highest === null ? "" : ` · ${highest}번까지 찾았어요`;
    return {
      text: `✦ 무료 체험은 앞 ${max}쪽까지예요${found} · 나머지 ${rest}쪽은 프리미엄으로`,
      feature: "limit_pages",
      context: { max, rest },
    };
  }

  // "13번은 5쪽부터예요": the passage's remaining questions sit past the page cap.
  function continuationNote(problem) {
    const info = problem && problem.continuation;
    if (!info || !Array.isArray(info.numbers) || info.numbers.length === 0) {
      return null;
    }
    const page = Number(info.page);
    if (!Number.isFinite(page) || page <= 0) {
      return null;
    }
    return `${info.numbers.join("·")}번은 ${page}쪽부터예요`;
  }
```

반환 객체에 `continuationNote,`를 `cardImageSource,` 다음에 넣는다.

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_web_logic.py -q`
Expected: 전부 PASS (기존 배너 테스트가 `problems` 없이 호출하면 문구는 그대로다)

- [ ] **Step 5: 커밋**

```bash
git add public/trial_logic.js test_trial_web_logic.py
git commit -m "feat: add continuation note and found-count banner copy

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: 카드 칩과 스타일

**Files:**
- Modify: `public/app.js`, `public/style.css`
- Test: `test_trial_web_logic.py` (`TestTrialPageMarkup`)

- [ ] **Step 1: 실패하는 테스트**

`TestTrialPageMarkup.test_app_script_switches_cards_through_the_shared_logic` 끝에:

```python
        self.assertIn("logic.continuationNote(", script)
        self.assertIn('"continue-chip"', script)
        self.assertIn(".continue-chip", css)
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest test_trial_web_logic.py -q -k Markup`
Expected: FAIL — `'logic.continuationNote(' not found`

- [ ] **Step 3: 구현**

`public/app.js`의 `renderProblems` 안, 확인 필요 칩 블록(`if (problem.needs_review) { ... head.appendChild(chip); }`) 다음에:

```js
      const note = logic.continuationNote(problem);
      if (note) {
        const chip = document.createElement("span");
        chip.className = "continue-chip";
        chip.textContent = `${Number(problem.continuation.page)}쪽에 이어짐`;
        chip.title = note;
        head.appendChild(chip);
      }
```

`public/style.css`의 `.review-chip { ... }` 블록 다음에:

```css
/* A passage whose remaining questions sit past the page cap; informational, not a warning. */
.continue-chip {
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent-ink);
  font-size: var(--text-xs);
  font-weight: 600;
  white-space: nowrap;
}
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest test_trial_web_logic.py test_trial_demo_web.py -q`
Expected: 전부 PASS

- [ ] **Step 5: 브라우저 확인**

메인 체크아웃의 `.claude/launch.json`(`trial-local`)은 `web-trial` 워크트리를 가리키므로, 이 브랜치용 항목 `trial-local-new-web1`을 같은 형식으로 추가한다(경로만 `.claude/worktrees/new_web1`). 2025 수능 국어 원본(`/Users/clmagi/Desktop/Projects/omr_maker/output/pdf/2025학년도-수능-국어-언어와매체-홀수형.pdf`)을 `public/_bench_sample.pdf`로 복사해 페이지 스크립트로 올리고 확인한다: "지문 10~13" 카드에 "5쪽에 이어짐" 칩(제목 "13번은 5쪽부터예요"), 배너 "✦ 무료 체험은 앞 4쪽까지예요 · 12번까지 찾았어요 · 나머지 12쪽은 프리미엄으로". 확인 뒤 `public/_bench_sample.pdf`를 지운다.

- [ ] **Step 6: 커밋**

```bash
git add public/app.js public/style.css test_trial_web_logic.py
git commit -m "feat: show a continuation chip on passages cut by the page cap

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: 전체 테스트와 문서 한 줄

**Files:**
- Modify: `docs/web-trial-operations.md` (§4-5 SQL 주석 한 줄)

- [ ] **Step 1: 운영 문서**

§4-5의 `-- 미리보기 예산 폴백: ...` 쿼리 다음에:

```sql
-- 쪽수 상한에 잘린 지문 묶음: timing.continued 가 1 이상이면 "N쪽에 이어짐" 칩이 붙은 응답이다.
select count(*) filter (where (timing->>'continued')::int > 0) as with_cut_passages, count(*) as parses
from public.trial_events
where kind = 'parse' and status = 200 and created_at > now() - interval '7 days';
```

- [ ] **Step 2: 전체 테스트**

Run: `LC_ALL=en_US.UTF-8 GEMINI_API_KEY= .venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: 전부 PASS

- [ ] **Step 3: 커밋**

```bash
git add docs/web-trial-operations.md
git commit -m "docs: add the cut-passage count query

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
