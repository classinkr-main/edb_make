# 웹 체험판 쪽수 상한 경계의 "이어짐" 표시 설계

- 작성일: 2026-09-17
- 브랜치: `new_web1` (web-trial aca1966에서 분기)
- 상태: 설계 검토 대기
- 선행 문서: `docs/superpowers/specs/2026-09-15-web-trial-quality-speed-load-design.md` §8-2 ("3쪽 끝에서 잘린 문항·지문 처리"), `docs/superpowers/specs/2026-09-16-web-trial-board-preview-design.md`

## 1. 문제

체험판은 원본의 앞 4쪽만 처리한다. 국어처럼 지문 하나에 문항 여러 개가 묶인 시험지는 4쪽 끝에서 지문 묶음이 잘린다. 실측(2026-09-17, 원본 6종):

| 원본 | 4쪽까지 찾은 문항 | 4쪽의 마지막 지문 | 원본에서 그 지문의 나머지 문항 |
|---|---|---|---|
| 2025 수능 국어 (16쪽, pypdf·Distiller 두 판 동일) | 1~12 | 지문 10~13 | 13번 (5쪽) |
| 2026 9월 모평 국어 (16쪽) | 1~12 | 지문 10~13 | 13번 (5쪽) |
| 2026 수능 국어 (16쪽) | 1~13 | 지문 10~13 | 없음 (묶음이 4쪽에서 끝남) |
| 2026 고2 3월 학평 국어 (16쪽, HWP 제작) | 1~11 | 지문 11~13 | 12·13번 (5쪽) |
| 전자기 교재 (21쪽) | 1~16 | 지문 없음 | — |

방문자 화면에는 "지문 10~13" 카드와 10·11·12번만 보인다. 13번이 인식에서 빠진 것처럼 보이지만 실제로는 체험 범위 밖(5쪽)에 있다. 지금은 이 차이를 설명하는 표시가 없다.

## 2. 확인한 사실

- **쓸 수 있는 신호는 지문 범위뿐이다.** 지문 단위 제목은 파이프라인이 항상 `지문 {start}~{end}` 형식으로 만든다(`build_problem_board_edb.py`의 두 생성 지점). 범위 안 번호 중 찾은 최대 번호보다 큰 번호가 있고 원본 쪽수가 처리 쪽수보다 많으면, 그 번호들은 다음 쪽에 있다.
- **기하 신호(마지막 단위의 바닥이 쪽 끝에 닿는지)는 쓸 수 없다.** 페이지 이미지는 여백을 잘라내므로 완결된 4쪽 시험지(지구과학·물리·화학·전자기 편집본)도 마지막 문항 바닥이 쪽 높이의 0.98~1.00에 닿는다. 잘린 문서(0.976~1.000)와 구분되지 않는다.
- 문항 본문이 4쪽/5쪽에 걸쳐 잘리는 경우는 감지할 수 없다. 수능형 2단 시험지에서는 문항이 쪽을 넘지 않으므로 이번 범위에서 제외한다.
- 체험판 응답에는 이미 `source_page_count`, `processed_page_count`, 문항 `number`·`title`이 있어 서버가 이 판단을 할 수 있다. 공용 파이프라인(`segment.py`, `build_problem_board_edb.py`)은 건드리지 않는다.

## 3. 결정 사항

| 항목 | 결정 |
|---|---|
| 판단 위치 | 서버. 새 순수 모듈 `trial_continuation.py`가 `ParseResult`에서 지문 단위별 이어짐을 계산하고 `trial_preview`가 응답에 싣는다 |
| 규칙 | 지문 단위 제목 `지문 a~b`에 대해 `missing = {n ∈ [a, b] : n > max(찾은 문항 번호 전체)}`. `source_page_count > processed_page_count`이고 `missing`이 비어 있지 않으면 이어짐. 찾은 최대 번호보다 작은 누락은 인식 누락이지 잘림이 아니므로 표시하지 않는다 |
| 응답 | `problems[].continuation`: `null` 또는 `{"numbers": [13], "page": 5}` (`page`는 처리 쪽수 + 1) |
| 카드 | 이어지는 지문 카드에 정보 칩 "5쪽에 이어짐". 칩의 `title`은 "13번은 5쪽부터예요"(번호가 여럿이면 "12·13번은 5쪽부터예요") |
| 배너 | 기존 "무료 체험은 앞 4쪽까지예요 · 나머지 12쪽은 프리미엄으로"에 찾은 최대 번호가 있으면 "12번까지 찾았어요"를 끼운다: "✦ 무료 체험은 앞 4쪽까지예요 · 12번까지 찾았어요 · 나머지 12쪽은 프리미엄으로". 팝업 문구·`context`는 그대로 |
| 이벤트 | `timing.continued` = 이어짐 표시된 단위 수 (`preview_step`·`board`와 같은 자리) |
| 원본이 잘리지 않은 경우 | `continuation`은 모두 `null`, 칩 없음, 배너 없음(지금과 같음) |

## 4. 구성 요소

### 4-1. `trial_continuation.py` (신규, 웹 의존성 없음)

```python
PASSAGE_RANGE = re.compile(r"지문\s*(\d+)\s*[~∼～\-–]\s*(\d+)")

def passage_range(title: str) -> tuple[int, int] | None
def continuations(result: ParseResult) -> dict[str, dict[str, Any]]
    # {problem_id: {"numbers": [12, 13], "page": 5}} — 규칙 §3. 원본이 잘리지 않았으면 {}.
```

- 찾은 번호가 하나도 없으면(문항 0개) `{}`.
- `a > b`처럼 뒤집힌 범위는 무시한다.

### 4-2. `trial_preview.py`

- `build_parse_body`가 단계 반복 전에 `continuations(result)`를 한 번 계산해 `_payload_for_step`에 넘긴다(단계와 무관한 값이므로 매 단계 재계산하지 않는다).
- `problems[].continuation`에 그 값 또는 `null`.

### 4-3. `trial_server.py`

- `timing["continued"] = sum(1 for p in payload["problems"] if p["continuation"])`.

### 4-4. 화면

- `public/trial_logic.js`
  - `continuationNote(problem)` → `"12·13번은 5쪽부터예요"` 또는 `null`.
  - `pagesBanner(payload)`의 `text`에 찾은 최대 번호(`problems[].number`의 최댓값)가 있으면 ` · {n}번까지 찾았어요` 구간을 넣는다. `feature`·`context`는 변경 없음.
- `public/app.js`: 카드 머리(`problem-card__head`)에 `continuation`이 있으면 `<span class="continue-chip" title="…">5쪽에 이어짐</span>`. 확인 필요 칩과 함께 있을 수 있다.
- `public/style.css`: `.continue-chip`은 `.review-chip`과 같은 모양에 정보색(`--accent-soft` 배경, `--accent-ink` 글자).
- 시연 페이지는 `app.js`를 공유하므로 추가 마크업이 없다.

## 5. 테스트

| 대상 | 방법 |
|---|---|
| `passage_range` | `지문 10~13`, `지문 4∼9`, `지문 1-3` → 튜플, `지문`·`3.`·`지문 9~4` → `None` |
| `continuations` | 합성 `ParseResult`: (a) 원본 16쪽·처리 4쪽, 문항 10·11·12, 지문 10~13 → `{passage_id: {"numbers": [13], "page": 5}}`; (b) 같은 조건에 문항 11·12만(10 누락) → `numbers`는 여전히 `[13]`; (c) 원본 4쪽·처리 4쪽 → `{}`; (d) 문항 0개 → `{}` |
| 응답 | `problems[].continuation`이 단계와 무관하게 같고, 이어짐 없는 문항은 `null` |
| API | 가짜 파서 결과에 지문·잘림을 넣어 응답 필드와 `timing.continued` 확인 |
| 화면 논리 (node) | `continuationNote` 두 형태, `pagesBanner`의 "N번까지 찾았어요" 유무(문항 없음·잘림 없음) |
| 마크업·스타일 | `app.js`에 `continue-chip`, `style.css`에 `.continue-chip` |
| 브라우저 | 로컬 서버에 2025 수능 국어 원본을 올려 "지문 10~13" 카드에 "5쪽에 이어짐" 칩과 배너 문구 확인 |
| 회귀 | 전체 `pytest` |

## 6. 이후 (같은 브랜치, 별도 계획)

코퍼스 확장: 로컬 후보 4개(HWP 제작 고2 3월 학평 국어, 25수능 화학, Quartz 재출력 전자기 편집본, Distiller 원본 2025 수능 국어)를 `make_inputs.py`로 넣고 정답 라벨을 만들어 채점한다. 파서 수정은 예상하지 않는다.
