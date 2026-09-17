# 웹 체험판 정확성 코퍼스 결과

- 설계: `docs/superpowers/specs/2026-09-15-web-trial-quality-speed-load-design.md` §5-1
- 코퍼스 위치: 저장소 밖 `~/edb-trial-bench/` (시험지·관측 JSON·라벨은 커밋하지 않는다)
- 갱신: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/score.py --doc docs/web-trial-quality.md`

## 지표

| 열 | 뜻 |
|---|---|
| `q_recall` / `q_prec` | 정답 문항 번호 중 체험판이 찾은 비율 / 체험판 문항 중 정답에 있는 비율 |
| `p_recall` / `p_prec` | 지문 범위(예: 1~3) 기준 같은 비율 |
| `mean_iou` / `low_iou` | 짝지은 문항의 박스 IoU 평균 / 0.8 미만 개수 (짝지은 것이 하나도 없으면 빈 칸). **`status`가 `truth`인 케이스도 이 박스는 `ground_truth`가 담고 있는 값이 아니라 항상 오라클의 박스다** -- `ground_truth`는 문항 번호·지문 범위 목록일 뿐 박스를 담지 않으므로, IoU는 오라클도 그 문항을 찾았을 때만 오라클 박스 대 체험판 박스로 계산한다. 오라클이 그 문항을 아예 못 찾았으면(`ground_truth` 목록에만 있는 문항) 비교할 박스가 없으므로 그 문항은 `mean_iou`/`low_iou` 계산에서 빠진다 -- 0으로 세지 않는다 |
| `missing` / `extra` | 정답에는 있는데 체험판에 없는 키 / 체험판에는 있는데 정답에 없는 키. 번호도 지문 범위도 아닌 체험판 항목(`t:<제목>` 형태의 미분류 단위)은 항상 위양성으로 `extra`에 포함되고, 집계 행에는 케이스별 개수의 합으로 나온다 |
| `review` | "확인 필요" 배지 비율 |
| `status` | `truth`는 독립적으로 읽은 `ground_truth`(아래) 기준, `approved`는 Fable(오라클 판정을 수행하는 에이전트 -- 사람이 아니다)이 오라클을 기준으로 판정한 라벨, `pending`은 오라클을 임시 정답으로 -- `approved`와 `pending`은 둘 다 결국 오라클에서 나온 값이므로 독립적인 정답이 아니라 잠정치("provisional")다. 집계(`합계`) 행에 이 케이스들 중 몇 개가 `truth`이고 몇 개가 잠정치인지가 `N/M truth-backed, N/M provisional (K approved)` 형태로 나온다 |
| `verified_by` | `truth` 케이스의 `ground_truth`를 누가/무엇이 만들었는지 -- `ground_truth.verified_by`의 값을 그대로 옮긴 것이다(필드가 없으면 `model`). `truth`가 아닌 행은 `ground_truth`를 아예 쓰지 않으므로 빈 칸이다. 지금까지 만들어진 13개 라벨은 전부 `model`이다: 전부 Claude 에이전트가 트리밍된 입력을 시각적으로 읽어 만든 것이고, 사람이 서명(sign-off)한 케이스는 아직 하나도 없다 |
| `ai_evidence` | 이 케이스의 오라클 실행에서 AI 페이지 보정 결과가 로컬 기준선과 실제로 달라진 쪽수/전체 쪽수(`oracle.page_repair.pages_changed`). 블록 분류, 문항 묶음, 제목(`display_title`), 크롭 박스(`bbox_px`), 확인 필요 플래그(`review_flags`) 중 하나라도 달라지면 그 쪽은 바뀐 것으로 센다. 0쪽이면 `(NO EVIDENCE)`가 붙고, 오라클 관측값 자체가 없으면 `no records`, 그보다 오래된(열이 없는) 관측값이면 `?`로 표시된다 -- 이 셋 중 어느 쪽도 "체험판이 AI급 인식과 일치했다"는 증거가 아니다 |

분모가 0인 지표(예: 정답에 지문이 하나도 없을 때의 `p_recall`)는 1.00이 아니라 빈 칸으로 나온다. 집계(`합계`) 행의 비율 열은 빈 칸을 제외한 케이스들의 평균이다.

## 라벨 형식과 `truth` 상태

`labels/<case>.json`은 기존 필드(`case`, `status`: `pending` | `approved`, 문항별 `items[].truth`: `trial` | `oracle` | `both` | `neither`)에 더해 선택적 `ground_truth` 객체를 가질 수 있다:

```json
{
  "case": "...",
  "status": "approved",
  "ground_truth": {
    "pages": 4,
    "question_numbers": [1, 2, 3, "..."],
    "passage_ranges": [[1, 3], [4, 9]],
    "verified_by": "model",
    "source": "누가/무엇으로 만들었는지",
    "note": "..."
  },
  "items": ["..."]
}
```

- `question_numbers`는 **트리밍된 체험판 입력** `~/edb-trial-bench/inputs/<case>.pdf`(`make_inputs.py`가 원본 시험지 앞 `MAX_PAGES`쪽만 잘라낸 파일)에 실제로 존재하는 문항 번호 전체 목록(Claude 에이전트가 그 트리밍된 PDF를 페이지 이미지로 렌더링해 직접 읽고 센 것 -- 일부 라벨(13개 중 6개)은 150 DPI PNG 렌더링을 노트에 기록했고, 나머지 7개는 렌더 해상도나 렌더링 방법을 기록하지 않았다. 날짜는 13개 라벨 전부 `source` 필드에 남아 있다)이다. **원본 시험지 전체를 기준으로 세면 안 된다** -- 그러면 트리밍으로 잘려나간 뒤쪽 문항까지 포함되어, 체험판이 놓친 적도 없는 문항들이 순전히 인위적인 대량 recall 미스로 보고된다. `passage_ranges`는 같은 트리밍된 입력 기준 지문 범위 목록이다. 둘 다 박스 좌표는 담지 않는다.
- `verified_by`는 그 `ground_truth`를 누가/무엇이 만들었는지 구분하는 필드다: `model`(기본값 -- 필드를 생략하면 이 값으로 취급된다) 또는 `human`. 지금까지 라벨을 채운 13개 케이스는 전부 이 필드를 생략했고, 즉 전부 Claude 에이전트가 만든 것이지 사람이 서명(sign-off)한 것이 아니다 -- 리포트의 `verified_by` 열이 케이스별로 이 구분을 그대로 보여준다(위 지표 표 참고). 두 값 외의 것은 `case`를 명시한 `ValueError`로 즉시 멈춘다.
- `pages`는 그 트리밍된 입력의 페이지 수(관측값의 `pages`/`source_page_count`와 같은 값)다. 이 값은 실제로 검사된다: `score.py`가 라벨의 `pages`를 오라클 관측값의 `pages`와 비교해, 다르면(전형적으로 원본 시험지 전체를 기준으로 세었을 때) `case`와 두 값을 모두 명시한 경고를 stderr와 리포트 실행 로그에 남긴다 -- 스코어링을 막지는 않지만, 문항 번호를 잘못된 PDF에서 세었을 가능성을 알려준다.
- 라벨 파일의 `status`가 `approved`이거나 `truth`(리포트가 실제로 찍는 값이자, 라벨을 채우는 쪽이 그대로 복사해 넣기 쉬운 값 -- 둘 다 동일하게 받아들여진다)이고 `ground_truth`에 `question_numbers`나 `passage_ranges` 중 하나라도 실제 값이 있으면 `score.py`는 오라클의 `problems` 목록을 아예 참고하지 않고 `ground_truth`만으로 정답 키 집합(`q<번호>`, `p<시작>-<끝>`)을 만든다 -- 오라클이 어떤 문항을 찾았는지·놓쳤는지와 무관하게 `ground_truth`에 적힌 목록이 그대로 정답이 된다. 이때 보고서의 `status` 열은 `truth`로 나와, 오라클을 임시 정답으로 쓴 행과 한눈에 구분된다. `ground_truth`가 있으면 `items[].truth`(trial/oracle/both/neither) 조정은 적용되지 않는다 -- `ground_truth`가 이미 최종 정답이기 때문이다.
- `ground_truth`가 truthy인데 `status`가 `approved`/`truth`가 아니면(예: 라벨을 아직 `pending`에 둔 채 `ground_truth`만 먼저 채워 넣은 경우) `score.py`는 그 값을 조용히 무시하지 않는다 -- `case`와 실제 `status`를 명시한 경고를 내고 기존 오라클 채점 경로로 넘어간다(그 라벨은 `pending`/`approved`로 그대로 채점됨). 마찬가지로 `ground_truth`는 있지만 `question_numbers`도 `passage_ranges`도 비어 있는 반쯤 채운 상태(예: `{"source": "...", "note": "WIP"}`만 있는 경우)도 `status: truth`로 격상시키지 않는다 -- 경고를 내고 같은 오라클 경로로 넘어간다.
- `ground_truth`가 객체가 아니거나(예: 배열), `passage_ranges`의 원소가 `[시작, 끝]` 두 값짜리 쌍이 아니거나, `verified_by`가 `model`/`human` 둘 다 아니면 `score.py`는 `case`를 명시한 `ValueError`로 즉시 멈춘다 -- `items[]`의 알 수 없는 `truth` 값과 같은 처리다.
- `ground_truth`가 없거나 `null`인 라벨은 기존대로 오라클 + `items[].truth` 보정 경로를 그대로 타고 `status`도 `approved`/`pending`으로 남는다(이 문서와 코드 양쪽에서 하위 호환).
- 박스 IoU(`mean_iou`/`low_iou`)는 `truth` 케이스에서도 여전히 오라클의 박스를 쓴다: 위 지표 표의 `mean_iou` 설명 참고.
- 이 문서에 어떤 케이스의 `ground_truth` 값(문항 번호 목록·지문 범위 목록) 자체는 적지 않는다 -- 시험지 내용이므로 라벨 파일에만 두고, 이 문서에는 그 결과인 점수와 어긋난 키만 싣는다. 읽는 작업은 2026-09-17에 13개 케이스 전부 끝났고(아래 "결과"), 현재 `~/edb-trial-bench/labels/*.json` 13개가 모두 `status: "approved"` + `ground_truth`(전부 `verified_by` 생략 = `model`)를 갖는다.

## 결과

아래 표는 13개 케이스 **전부**를 독립적으로 읽은 정답(`ground_truth`) 기준으로 채점한 것이다 -- `status` 열이 13행 모두 `truth`이고, 집계 행이 `13/13 truth-backed, 0/13 provisional`이다. 오라클을 임시 정답으로 쓴 행은 이제 하나도 없다. **이 정답을 만든 것은 Claude 에이전트(모델)다: 트리밍된 입력을 페이지 이미지로 렌더링해 시각적으로 읽고 문항 번호·지문 범위를 센 것이지, 파서(체험판·오라클) 출력을 베낀 것이 아니다.** `verified_by` 열(과 각 라벨의 `source`/`note` 필드)이 그 근거이고, 13개 전부 `model`이다 -- **사람이 이 정답을 검토하고 서명(sign-off)한 적은 아직 한 번도 없다.** 재측정할 때 기존 라벨·관측값과 섞이지 않도록 별도 `TRIAL_BENCH_ROOT`(`scripts/trial_bench/common.py:21`)를 쓴다.

- **코퍼스 규모**: 13개 케이스 / 5개 과목 -- 과학 4(물리학Ⅰ×2, 지구과학, 전자기) · 국어 3 · 영어 2 · 수학 2 · 사회(생활과윤리) 2. Claude가 트리밍된 입력에서 직접 읽고 센 정답은 문항 207개와 지문 범위 9개이고, 체험판이 내놓은 단위는 216개다(이 절을 수정하기 전 측정에서는 219개였다 -- 아래 "발견된 오류 유형" 2번이 기록한 위양성 4개가 그 사이에 고쳐지고, `p16-17` 누락이 고쳐지며 1개가 늘었다: 219 − 4 + 1 = 216. `~/edb-trial-bench/labels/*.json` 13개 전부의 `ground_truth`(문항 번호 + 지문 범위)를 합산하거나 `~/edb-trial-bench/trial/*.json` 13개의 `problems` 개수를 직접 세면 둘 다 216이 나온다).
- **트리밍 쪽수는 케이스마다 다르다 -- 코퍼스 전체가 앞 3쪽인 것도, 전체가 앞 4쪽인 것도 아니다.** 영어·수학·사회 6개 케이스는 현재 캡(`trial_input.py`의 `DEFAULT_MAX_PAGES = 4`)대로 앞 4쪽이고, 나머지 7개(국어 3 · 과학 4)는 캡이 3이던 시절에 만들어진 앞 3쪽 입력 그대로다. 이 7개는 `~/edb-trial-bench/sources/`에 원본 PDF가 남아 있지 않아(현재 `sources/`에는 2026-09-16에 추가한 6개뿐) 다시 4쪽으로 자르려면 원본을 다시 받아야 한다. 특히 `01_물리학Ⅰ_문제지`·`earth_input`·`physics_input`은 원본 자체가 4쪽이므로, 오늘의 체험판이라면 읽었을 네 번째 쪽이 이 측정에는 아예 들어 있지 않다.
- 표의 `ai_evidence` 열은 이제 13행 모두 채워져 있고, 13행 모두 `(NO EVIDENCE)`다. 그 뜻은 아래 "오라클이 기여한 것과 하지 않은 것" 항목에 있다.

<!-- corpus-table -->
측정일 2026-09-17 · 라벨 없는 케이스는 오라클을 임시 정답으로 채점(pending)

| case | status | q_recall | q_prec | p_recall | p_prec | mean_iou | low_iou | review | missing | extra | trial_ms | oracle_ms | ai_evidence | verified_by |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01_물리학Ⅰ_문제지 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 931 | 7971 | 0/3 (NO EVIDENCE) | model |
| 2025학년도-수능-국어-언어와매체-홀수형 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1078 | 12856 | 0/3 (NO EVIDENCE) | model |
| 2026학년도-9월-모평-국어-언어와매체 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 943 | 8267 | 0/3 (NO EVIDENCE) | model |
| 2026학년도-수능-국어-언어와매체-홀수형 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1045 | 8386 | 0/3 (NO EVIDENCE) | model |
| chemistry_2025suneung | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1147 | 13197 | 0/4 (NO EVIDENCE) | model |
| earth_input | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 933 | 6711 | 0/3 (NO EVIDENCE) | model |
| em_textbook_edited_quartz | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 876 | 12033 | 0/4 (NO EVIDENCE) | model |
| english_2020suneung_go3_20191107 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 928 | 43832 | 0/4 (NO EVIDENCE) | model |
| english_go2_hakpyeong_20260324 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 913 | 15796 | 0/4 (NO EVIDENCE) | model |
| korean_2025suneung_distiller | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1314 | 11612 | 0/4 (NO EVIDENCE) | model |
| korean_go2_hakpyeong_20260326_hwp | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1087 | 12014 | 0/4 (NO EVIDENCE) | model |
| math_2026suneung_9wolmopyeong_20250903 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 458 | 11783 | 0/4 (NO EVIDENCE) | model |
| math_go3_hakpyeong_20240328 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 359 | 10122 | 0/4 (NO EVIDENCE) | model |
| physics_input | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 884 | 7385 | 0/3 (NO EVIDENCE) | model |
| social_saengwoon_2020suneung_20191015 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 918 | 16030 | 0/4 (NO EVIDENCE) | model |
| social_saengwoon_2025suneung_9wolmopyeong_20240904 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1098 | 16190 | 0/4 (NO EVIDENCE) | model |
| 전자기_교재문제 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 700 | 10065 | 0/3 (NO EVIDENCE) | model |
| 합계 | 17/17 truth-backed, 0/17 provisional (0 approved) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 | 0 | 0 |  |  |  |  |

> **`01_물리학Ⅰ_문제지`, `2025학년도-수능-국어-언어와매체-홀수형`, `2026학년도-9월-모평-국어-언어와매체`, `2026학년도-수능-국어-언어와매체-홀수형`, `chemistry_2025suneung`, `earth_input`, `em_textbook_edited_quartz`, `english_2020suneung_go3_20191107`, `english_go2_hakpyeong_20260324`, `korean_2025suneung_distiller`, `korean_go2_hakpyeong_20260326_hwp`, `math_2026suneung_9wolmopyeong_20250903`, `math_go3_hakpyeong_20240328`, `physics_input`, `social_saengwoon_2020suneung_20191015`, `social_saengwoon_2025suneung_9wolmopyeong_20240904`, `전자기_교재문제`: 17 truth-backed case(s) above.** `q_recall`/`q_prec`/`p_recall`/`p_prec` come from an independently read question/passage list (the label's `ground_truth`), but `mean_iou`/`low_iou` are still scored against the oracle's own boxes -- see `ground_truth`, `verified_by`, and the `docs/web-trial-quality.md` 라벨 형식 section.

> **AI page repair produced no evidence of a real change for 17 of 17 case(s): `01_물리학Ⅰ_문제지`, `2025학년도-수능-국어-언어와매체-홀수형`, `2026학년도-9월-모평-국어-언어와매체`, `2026학년도-수능-국어-언어와매체-홀수형`, `chemistry_2025suneung`, `earth_input`, `em_textbook_edited_quartz`, `english_2020suneung_go3_20191107`, `english_go2_hakpyeong_20260324`, `korean_2025suneung_distiller`, `korean_go2_hakpyeong_20260326_hwp`, `math_2026suneung_9wolmopyeong_20250903`, `math_go3_hakpyeong_20240328`, `physics_input`, `social_saengwoon_2020suneung_20191015`, `social_saengwoon_2025suneung_9wolmopyeong_20240904`, `전자기_교재문제`.** On those cases the forced-AI oracle's block types, problem grouping, titles, crop boxes and review flags all came out identical to what the local baseline produced on its own, so those rows' scores show agreement with the trial's own local baseline, not confirmation by AI-grade recognition -- see `ai_evidence` and rerun scripts/trial_bench/oracle.py to refresh.
<!-- /corpus-table -->

### 게이트 판정 (설계 §3 "정확성")

설계 §3의 기준은 "확정 라벨 기준 문항 recall·precision 0.95 이상, 지문 범위 recall 0.9 이상, 문항 박스 IoU 0.8 미만 비율 0.1 이하, 확인 필요 비율 0.2 이하"다. 집계 행은 케이스별 값의 평균(빈 칸 제외)이므로, 문항·범위 단위로 합산한 값도 같이 적는다. 두 방식이 갈리는 지표는 없다. 아래 숫자는 두 결함 수정과 그 후속 보정(`eee279b`)이 모두 들어간 2026-09-17 HEAD에서 13개 케이스를 **전부** 다시 관측해 다시 채점한 결과다(재현 명령은 아래 "발견된 오류 유형" 절 첫 문단).

| §3 기준 | 집계 행(케이스 평균) | 단위 합산 | 판정 |
|---|---|---|---|
| 문항 recall 0.95 이상 | 1.00 | 207/207 = 1.000 | **통과** |
| 문항 precision 0.95 이상 | 1.00 | 207/207 = 1.000 | **통과** |
| 지문 범위 recall 0.9 이상 | 1.00 | 9/9 = 1.000 | **통과** |
| 박스 IoU 0.8 미만 비율 0.1 이하 | 0/215 = 0.00 | 같음 | **측정된 적 없음**(아래) |
| 확인 필요 비율 0.2 이하 | 0.00 | 0/216 = 0.000 | 통과, 단 아래 단서 |

- **문항 precision은 수정 전에도 게이트를 넘었지만 위양성이 남아 있었고, 이제 완전하다** (케이스 평균 0.99 → 1.00, 문항 단위 207/211 = 0.981 → 207/207 = 1.000). 0.95 기준은 수정 전에도 통과였다 -- 달라진 것은 `english_go2_hakpyeong_20260324`의 위양성 4개(`q1#2`~`q4#2`, 아래 오류 유형 2)가 사라졌다는 점이다. 문항 recall은 수정 전후 모두 1.00(207/207)으로 바뀌지 않았다.
- **지문 범위 recall은 이제 통과다** (케이스 평균 1.00, 범위 단위 9/9 = 1.000). 이전에는 케이스 평균 0.80·범위 단위 8/9(0.889)로 두 방식 모두 0.9에 못 미쳤다. 원인은 단 한 건 -- `english_2020suneung_go3_20191107`의 `p16-17` 누락(아래 오류 유형 1) -- 이었고, 커밋 `7bcf1bb`("Keep a bodyless bracketed range header as its own passage")로 고쳐졌다.
- **당시 판단이 필요했던 지점: `p16-17`을 지문 범위로 셀지 여부.** 수정 전 이 유일한 원인 건의 판단 근거는 라벨 파일(`~/edb-trial-bench/labels/english_2020suneung_go3_20191107.json`)의 `ground_truth.note`에만 적혀 있었고, 이 문서에는 한동안 옮겨진 적이 없었다: 문제의 `[16~17] 다음을 듣고, 물음에 답하시오.`는 대괄호로 범위를 표시한 헤더이지만, 그것이 다스리는 것은 듣기 스크립트이고 트리밍된 지면에는 본문이 인쇄되어 있지 않다. 그 노트는 "본문 없는 대괄호 헤더에 대해 지문 단위를 아예 만들지 않는 파서도 defensible하다"고 명시했다 -- 실제로 그 대안을 택해 라벨에서 `[16, 17]` 항목을 뺐다면, 수정 전에도 지문 범위 recall이 0.889 → 1.00으로 올라 표의 미달이 사라졌을 것이다. **이 프로젝트는 그 대안을 채택하지 않았다.** 대괄호 범위 헤더는 본문 유무와 무관하게 항상 지문 단위로 기록한다는 기존 관례를 그대로 유지했다. 이유는 둘이다: **(1) 같은 헤더 형식이 이미 정확히 처리된 선례가 코퍼스 안에 있었다** -- `english_go2_hakpyeong_20260324`에서 체험판 스스로 같은 `[16~17]` 헤더를 `p16-17`로 정확히 잡아냈으므로, 같은 형식을 한 케이스에서는 필수로 요구하고 다른 케이스에서는 선택으로 두면 recall 기준 자체가 케이스마다 달라져 버렸을 것이다. **(2) 관찰된 실패 양상이 그 defensible한 대안과 달랐다** -- 아래 "발견된 오류 유형" 절 1번이 기록하듯, 체험판이 이 헤더를 놓친 원인은 헤더가 앞 문항 단위로 흡수된 버그였지, "본문 없는 헤더는 지문 단위를 만들지 않는다"는 의도된 설계가 아니었다. 의도된 설계가 아닌 결과를 defensible한 설계 선택으로 취급해 라벨을 고쳤다면, 게이트를 통과시키기 위해 정답을 맞추는 것과 다르지 않았을 것이다. 이 라벨링 관례는 그대로 유지한 채, 커밋 `7bcf1bb`가 답지-연속 스캔(`_trim_pdf_problem_bottom_to_last_choice`)에 지문 범위 헤더를 만나면 끊는 조건을 추가해 버그 자체를 고쳤다 -- 그래서 위 표의 지문 범위 recall 행은 라벨을 바꾸지 않고도 **통과**로 올라섰다. 이 관례를 뒤집었다면(본문 없는 `[16~17]` 헤더를 두 영어 케이스의 라벨에서 모두 제외했다면) 정답 지문 범위는 9개가 아니라 7개가 되고, 체험판이 내놓는 9개 중 2개가 위양성이 되어 지문 범위 precision이 7/9 = 0.778로 떨어진다. §3에는 지문 범위 *precision* 기준이 없으므로 게이트 판정 자체는 어느 관례로도 통과지만, 위 표의 `p_prec` 1.00은 이 관례를 유지했을 때의 값이다.
- **박스 IoU 두 열은 통과가 아니라 미측정이다.** `mean_iou`/`low_iou`는 독립적으로 읽은 정답(`ground_truth`)이 아니라 **오라클의 박스**와 비교한 값인데(`ground_truth`는 번호·범위 목록일 뿐 박스가 없다), 이번 코퍼스에서는 오라클 관측값의 키 집합이 거의 전부 체험판과 동일하고 짝지은 키의 `regions`가 대부분 **바이트 단위로 같다**. IoU 분모가 216(단위 수)이 아니라 215인 것은 어긋난 게 아니다: `english_2020suneung_go3_20191107`의 `p16-17`은 `ground_truth`(진실 라벨)에는 있지만 그 케이스의 오라클 관측값에는 없는 유일한 키다 -- 위 "오라클이 기여한 것과 하지 않은 것" 절대로, 이 케이스의 오라클은 `p16-17` 누락을 고친 커밋 `7bcf1bb` 이후 재실행되지 않았다(재실행한 것은 `english_go2_hakpyeong_20260324`뿐이다). `_expected_from_ground_truth`(score.py:191, `passage_ranges` 루프)는 `ground_truth`에 있는 키를 오라클의 `problems`에서 찾지 못하면 빈 `regions`로 채우고, `score_case`(score.py:337)는 그런 키를 IoU 비교에서 아예 제외한다 -- 그래서 216개 단위 중 215쌍만 채점 대상이다. **같은 이유로 이 215쌍이 전부 정확히 `1.0`인 것도 아니다**: 같은 케이스의 `q15`가 `0.8955`다 -- 앞 문항(15번)의 답지 스캔이 헤더를 흡수했던 수정 전 상태 그대로인 오라클의 낡은 박스와, 수정 후 그 헤더를 흡수하지 않는 체험판의 새 박스를 비교한 결과다(`LOW_IOU=0.8` 미만은 아니라서 `low_iou` 카운트에는 안 잡히고, 그 케이스의 `mean_iou`도 소수점 둘째 자리에서는 `1.00`으로 반올림된다 -- 위 표의 값은 바뀌지 않는다). 나머지 214쌍은 여전히 오라클과 체험판이 바이트 단위로 같은 박스를 낸 경우다. 즉 이 두 열은 "박스가 정확하다"는 증거가 아니라 "박스를 검증할 독립적인 기준이 (한 쌍을 빼면) 아직 없다"는 표시다. 확인 명령:
  ```
  GEMINI_API_KEY= .venv/bin/python -c "
  import sys, json, pathlib; sys.path.insert(0,'.')
  from scripts.trial_bench.score import expected_from, regions_iou
  B=pathlib.Path.home()/'edb-trial-bench'
  n=exact=0
  for c in sorted(json.load(open(B/'cases.json'))):
      tr=json.load(open(B/'trial'/f'{c}.json')); orc=json.load(open(B/'oracle'/f'{c}.json')); lab=json.load(open(B/'labels'/f'{c}.json'))
      exp,_=expected_from(orc,tr,lab); trb={p['key']:p for p in tr['problems']}
      for k in set(trb)&set(exp):
          if not exp[k].get('regions'): continue
          n+=1; exact += regions_iou(trb[k]['regions'], exp[k]['regions'])==1.0
  print(n, exact)"
  ```
  → `215 214`(2026-09-17 기준 -- `n`은 IoU가 채점된 쌍 수, `exact`는 그중 정확히 `1.0`인 쌍 수. 위에서 설명한 `english_2020suneung_go3_20191107`의 `q15` 한 쌍만 `1.0`이 아니다).
- **확인 필요 비율 0.00은 통과이되 좋은 신호가 아니다.** 체험판은 216개 단위 중 단 하나에도 "확인 필요" 배지를 달지 않았다. 아래 "발견된 오류 유형"이 기록한 결함들(모두 고쳐짐)도 당시엔 그 배지 없는 216건 안에 있었다. 즉 이 게이트는 "오탐이 적다"는 뜻일 뿐이고, 배지가 그 결함들을 잡지 못했다는 사실은 이 숫자에 드러나지 않는다.

### 발견된 오류 유형

**2026-09-17 갱신: 아래 "없는 문항 생성"과 "지문 범위 누락" 결함이 모두 고쳐졌다.** 이 절을 처음 쓴 시점의 측정은 체험판이 `ground_truth`(Claude가 독립적으로 읽은 정답)와 어긋난 곳 5건(전부 영어 2개 케이스)을 기록했다. 그중 4건(오류 유형 2번, "없는 문항 생성")은 같은 날 `segment.py`의 `_indented_nested_enumeration_marker_ids`에 세 번째 신호 -- 답지 마커(①-⑤)가 지워진 마커 묶음과 다음 문항 사이(또는 단 끝까지)에 실제로 나타나는지 -- 를 요구하도록 고쳐서 해소했다(들여쓰기 + 역행하는 번호라는 기존 두 신호만으로는, 들여쓴 목록이 아니라 진짜로 번호를 다시 시작하는 절이어도 지워질 수 있었다). 이 신호는 커밋 `1ccece1`·`25a06e4`·`54d58c0`에 걸쳐 도입·보강됐고, 코퍼스에서 위양성 4개가 사라진 것은 `25a06e4` 시점의 표부터다(`git show 25a06e4 -- docs/web-trial-quality.md`의 diff에서 집계 행 `extra` 열이 4에서 0으로 내려간다). 남은 1건(오류 유형 1번, "지문 범위 누락")도 같은 날 커밋 `7bcf1bb`("Keep a bodyless bracketed range header as its own passage")가 `_trim_pdf_problem_bottom_to_last_choice`의 답지-연속 스캔에 지문 범위 헤더를 만나면 끊는 조건을 추가해 고쳤고, 그 뒤 `eee279b`("Require an in-question guard on the passage-header choice break")가 그 끊김 조건에 "주장된 범위가 현재 문항 번호보다 뒤에서 시작할 때만"이라는 가드를 덧붙였다.

**이 문서의 표·본문 숫자는 두 결함 수정과 그 후속 보정(`eee279b`)이 모두 들어간 HEAD에서 13개 케이스를 전부 다시 관측해 얻은 것이다**(측정일 2026-09-17):

```
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/observe.py          # 인자 없음 = 13개 전부
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/score.py --doc docs/web-trial-quality.md
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/adjudicate.py
```

**오라클 관측값은 이번에 다시 만들지 않았다** -- 13개 중 12개는 두 수정 이전(2026-09-16)에 만들어진 그대로이고, `english_go2_hakpyeong_20260324` 하나만 "없는 문항 생성"을 고친 직후 재실행된 상태다(`scripts/trial_bench/oracle.py`). 그것이 `ai_evidence` 열과 박스 IoU에 대해 뜻하는 바는 아래 "오라클이 기여한 것과 하지 않은 것" 절에 있다.

`eee279b`는 실제 오탐 형태(앞선 문항 번호보다 앞에서 시작하는, 문항 안의 범위 표기)를 막는 가드라 이 코퍼스의 어떤 케이스도 건드리지 않는다. 그 사실 자체를 측정했다 -- `eee279b` 직전 파서로 같은 13개 입력을 별도 루트에 다시 관측하면 `timing_ms`와 크롭 경로를 뺀 관측값 전체(키·`number`·`title`·`passage_range`·`regions`·`risk_flags`)가 13개 케이스 216단위 모두 HEAD와 동일하다:

```
export WORK="${TMPDIR:-/tmp}/pre-eee279b"
rm -rf "$WORK" && mkdir -p "$WORK/snapshot" "$WORK/root"
git archive eee279b^ | tar -x -C "$WORK/snapshot"
cp -R ~/edb-trial-bench/inputs "$WORK/root/inputs"
cat > "$WORK/snapshot/_observe_all.py" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scripts.trial_bench.observe import main
raise SystemExit(main([]))
PYEOF
TRIAL_BENCH_ROOT="$WORK/root" GEMINI_API_KEY= .venv/bin/python "$WORK/snapshot/_observe_all.py"
.venv/bin/python -c "
import json, os, pathlib
def strip(o):
    o = dict(o); o.pop('timing_ms', None)
    o['problems'] = [{k: v for k, v in p.items() if k != 'crop'} for p in o['problems']]
    return o
new = pathlib.Path.home()/'edb-trial-bench'/'trial'; old = pathlib.Path(os.environ['WORK'])/'root'/'trial'
files = sorted(new.glob('*.json'))
bad = [f.stem for f in files if strip(json.load(open(f))) != strip(json.load(open(old/f.name)))]
print('cases compared:', len(files), 'differing:', len(bad), bad)"
```

→ `cases compared: 13 differing: 0 []`(2026-09-17 기준). 스냅샷은 자기 자신의 `sys.path`에서만 돌고 관측값은 별도 `TRIAL_BENCH_ROOT`에만 쓰므로 현재 작업 트리와 코퍼스는 건드리지 않는다.

남은 결함은 없다: 13개 케이스(국어 3 · 과학 4 · 영어 2 · 수학 2 · 사회 2) 전부 문항·범위가 정답과 완전히 일치한다.

| 오류 유형 | 건수 | 어디서 | 어느 쪽이 옳은가 |
|---|---|---|---|
| 지문 범위 누락 | 0 (수정 전 1건 -- 아래 1번) | -- | **고쳐짐** |
| 없는 문항 생성 | 0 (수정 전 4건 -- 아래 2번) | -- | **고쳐짐** |
| 문항 누락 | 0 | -- | -- |
| 박스 경계 오류 | 0건 관측, 단 위 항목대로 **측정된 적 없음** | -- | -- |
| 쪽 넘김 병합 | 0 | -- | -- |

1. **지문 범위 누락 (수정 전 1건, 이제 0건)** -- `english_2020suneung_go3_20191107` 2쪽 왼쪽 단, 15번과 16번 사이에 `[16~17] 다음을 듣고, 물음에 답하시오.`가 분명히 인쇄돼 있다(트리밍된 입력 `inputs/english_2020suneung_go3_20191107.pdf` 2쪽을 렌더링해 직접 확인). 수정 전 체험판은 이 케이스에서 지문 단위를 **하나도** 만들지 않았다(관측값의 키 28개가 전부 `q1`~`q28`) -- 같은 시험 형식인 `english_go2_hakpyeong_20260324`에서는 같은 `[16~17]` 헤더를 `p16-17`로 정확히 잡았으므로, 형식을 못 읽는 문제가 아니라 이 한 쪽에서 헤더가 앞 문항(15번)의 답지 목록 연속으로 흡수된 것이었다. 그 결과 수정 전 이 케이스의 `p_recall`은 0.00이었고, `p_prec`는 체험판 지문 단위가 0개라 분모가 없어 빈 칸이었다. 커밋 `7bcf1bb`가 `_trim_pdf_problem_bottom_to_last_choice`의 답지-연속 스캔에 지문 범위 헤더를 만나면 끊는 조건을 추가해 고쳤고, 이제 이 케이스도 `p_recall`/`p_prec` 1.00/1.00이다.
2. **없는 문항 생성 (수정 전 4건, 이제 0건)** -- `english_go2_hakpyeong_20260324` 4쪽 28번 문항의 `Library of Things` 안내문 상자 안에 `How It Works` 번호 목록 `1.` `2.` `3.` `4.`가 들어 있는데, 체험판이 이 네 줄을 최상위 문항으로 승격시켰다(4쪽 실제 문항은 25~28번뿐인데 1~4번 키가 다시 생겨 `q1#2`~`q4#2`로 충돌 회피됐었다). 오류 지점은 **한 곳**(안내문 목록 하나)이지만 위양성 키는 4개였다. 수정 전 이 케이스의 `q_prec`는 28/32 = 0.88이었고 `q_recall`은 1.00이었다 -- 놓친 문항은 없고 없는 문항을 만든 쪽이었다. 위 갱신 안내대로 답지 마커 신호를 추가한 뒤에는 이 네 줄이 28번 문항의 크롭으로 흡수되어 더는 별도 문항이 되지 않고, 이 케이스의 `q_prec`도 1.00이다.
3. **쪽 넘김 병합 0건**은 구조적으로 확인했다: 216개 단위 중 `regions`가 두 쪽 이상에 걸친 것이 하나도 없다(`{r['page_index'] for r in p['regions']}`의 크기가 전부 1).

### "확인 필요" 배지가 실제 오류를 예측하는가

위 절이 기록한 5건(수정 전)이 이 질문의 유일한 실제 데이터다: **당시 체험판은 5건 모두에 "확인 필요" 배지를 달지 않았다.** `scripts/trial_bench/risk_flag_predictivity.py`가 이 질문을 반복 측정 가능하게 만든다 -- 코퍼스의 모든 트리밍 단위(`~/edb-trial-bench/trial/*.json`)에 대해 그 단위의 `risk_flags`와 그 단위가 라벨 정답 기준 실제 오류인지(`score.py`의 `extra`와 같은 정의)를 짝지어, `trial_preview.REVIEW_WORTHY_FLAGS`가 지금 무시하는 플래그(특히 `passage_cross_page_merge_check`)를 포함한 플래그별 오탐/누락표를 만든다. 정답 목록에는 있지만 체험판이 아예 단위를 만들지 않은 키(`score.py`의 `missing`)는 따로 센다 -- 존재하지 않는 단위에는 어떤 플래그도 붙을 수 없기 때문이다.

**현재 코퍼스(13개 케이스, 216단위)에는 알려진 실제 오류가 0건이다** -- 위 절의 5건이 모두 고쳐졌기 때문이다. 그래서 아래 표의 모든 `recall`은 빈 칸이다(잡아야 할 실제 오류가 없음). 그래도 두 가지는 지금 측정할 수 있다: `REVIEW_WORTHY_FLAGS`에 들어 있는 4개 플래그 중 `fallback_grouping`/`merged_problem_block`/`marker_conflicts` 3개는 이 코퍼스 216단위 중 단 하나에도 붙지 않았다(발생 0건 -- 코퍼스를 더 모으면 언젠가는 나타날 수 있는, 아직 관측되지 않았을 뿐인 값이다). **나머지 하나인 `hwp_oversegmentation`은 이것과 다르다.** 이 플래그는 `build_problem_board_edb.py`의 `build_ui_session`(`_collect_page_risk_flags`/`_collect_hwp_problem_count_mismatches`, 데스크톱 세션 전용 *페이지* 레벨 플래그, `build_ui_session` 안에서만 만들어진다) 쪽에만 있는데, 체험판이 부르는 `problem_parser.parse_problems`는 `build_pages`+`build_problem_entries`만 호출하고 `build_ui_session`은 아예 호출하지 않는다 -- 그리고 체험판이 실제로 쓰는 단위별 플래그 수집기 `_collect_problem_risk_flags`(`build_problem_board_edb.py:4746`)에는 이 이름을 만드는 분기 자체가 없다. 그래서 아래 표의 `hwp_oversegmentation` 행이 보여주는 0/0/0/216은 "이 코퍼스에서 발생 0건"이 아니라 **"이 경로로는 코퍼스를 아무리 늘려도 절대 발생할 수 없음"**이다(표에서 `†`로 표시하고 그 아래 각주에 설명을 반복한다) -- 코퍼스를 키우는 것이 이 행의 숫자를 바꿀 수 있는지 여부가 이 셋과 이 하나 사이의 실질적인 차이다. `REVIEW_WORTHY_FLAGS`가 무시하는 `passage_cross_page_merge_check`는 216단위 중 21개(국어 3개 케이스에서만, 케이스당 7개)에 붙어 있는데 그 21개는 지금 전부 정답과 일치하는 정상 단위다 -- 즉 지금 이 플래그를 배지에 추가하면 리뷰율이 0.00에서 21/216 = 0.10으로 오르고, 그 10%는 전부 오탐이 된다.

갱신: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/risk_flag_predictivity.py --doc docs/web-trial-quality.md`

<!-- risk-flag-table -->
측정일 2026-09-17

| predictor | in badge today | tp | fp | fn | tn | precision | recall |
|---|---|---|---|---|---|---|---|
| fallback_grouping | yes | 0 | 0 | 0 | 216 |  |  |
| hwp_oversegmentation † | yes | 0 | 0 | 0 | 216 |  |  |
| marker_conflicts | yes | 0 | 0 | 0 | 216 |  |  |
| merged_problem_block | yes | 0 | 0 | 0 | 216 |  |  |
| passage_cross_page_merge_check | no | 0 | 21 | 0 | 195 | 0.00 |  |
| 현재 배지 (needs_review) | -- | 0 | 0 | 0 | 216 |  |  |
| 아무 risk_flag나 (any) | -- | 0 | 21 | 0 | 195 | 0.00 |  |

> **단위 216개 중 실제 오류 0개, 플래그를 달 단위 자체가 없는 오류(정답에는 있으나 체험판이 아예 만들지 않은 키) 0개 -- 합쳐서 이 코퍼스가 아는 실제 오류는 총 0건이다.** `fn`은 단위가 존재하는 오류만 센다: 없는 단위에는 어떤 risk_flag도(현재도, 가상의 어떤 조합도) 붙을 수 없기 때문이다.

> **† 표시된 플래그는 이 코퍼스 크기와 무관하게 체험판 경로에서 절대 나타날 수 없다(구조적으로 도달 불가 -- 관측된 0건이 아니다): `hwp_oversegmentation`.** `problem_parser.parse_problems`(체험판이 부르는 경로)는 `build_pages`+`build_problem_entries`만 호출하고 `build_ui_session`은 절대 호출하지 않는데, 이 플래그는 `build_ui_session` 전용 페이지 레벨 플래그(`_collect_page_risk_flags`/`_collect_hwp_problem_count_mismatches`)라서 체험판 쪽 `_collect_problem_risk_flags`에는 이를 만드는 분기 자체가 없다 -- 코퍼스를 아무리 늘려도 이 표의 위 행은 0/0/0/N에서 움직이지 않는다.
<!-- /risk-flag-table -->

#### 수정 전 5건에서 재구성한 측정 (역사적, 재현 가능)

현재 코퍼스에 실제 오류가 0건이라는 사실만으로는 "배지가 오류를 잡을 수 있는가"에 답할 수 없다 -- 이 코퍼스가 지금까지 찾아낸 실제 오류는 위 5건이 전부이고, 그 5건은 이미 고쳐졌다. 그 5건 당시 각 단위의 `risk_flags`가 무엇이었는지는 지금의 `~/edb-trial-bench/trial/*.json`에는 남아 있지 않다(고쳐진 뒤 다시 관측했으므로). 그래서 수정 전 커밋으로 파서만 되돌려 그 5건을 다시 만들어냈다 -- 코드를 되돌린 것이 아니라(현재 작업 트리는 전혀 건드리지 않는다), 별도 디렉터리에 그 커밋의 코드를 풀어 완전히 격리된 `sys.path`로 실행한 것이다. 아래 명령은 오늘도 그대로 다시 실행하면 같은 결과를 낸다:

```
# 0) 준비.
mkdir -p /tmp/old-snapshot /tmp/hist-root/trial /tmp/hist-root/oracle /tmp/hist-root/labels

# 1) 두 수정(1ccece1, 25a06e4, 54d58c0, 7bcf1bb) 바로 전 커밋 -- segment.py는
#    9c7e171(이 5건을 처음 기록한 커밋)과 바이트 단위로 동일하다.
git archive aeba954 | tar -x -C /tmp/old-snapshot

# 2) 그 스냅샷 자신의 sys.path 안에서만 파서를 실행하는 드라이버를 그 스냅샷
#    안에 써넣는다 -- 현재 작업 트리 경로를 섞으면
#    problem_parser.parse_problems의 지연 임포트(`from build_problem_board_edb
#    import ...`)가 고쳐진 현재 코드를 끌어와 버그가 재현되지 않는다.
#    observation_from_result도 같은 스냅샷의 scripts/trial_bench/common.py에서
#    가져온다.
cat > /tmp/old-snapshot/_build_historical_trial.py <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from problem_parser import parse_problems
from scripts.trial_bench.common import BENCH_ROOT, bench_dir, save_json, observation_from_result
REAL_BENCH_INPUTS = Path.home() / "edb-trial-bench" / "inputs"
for case in ["english_go2_hakpyeong_20260324", "english_2020suneung_go3_20191107"]:
    pdf = REAL_BENCH_INPUTS / f"{case}.pdf"
    work_dir = BENCH_ROOT / "_work" / case
    work_dir.mkdir(parents=True, exist_ok=True)
    result = parse_problems(pdf, work_dir=work_dir, max_pages=4)
    observation = observation_from_result(case, result)
    save_json(bench_dir("trial", BENCH_ROOT) / f"{case}.json", observation)
    print(case, "problems:", len(observation["problems"]), "keys:", [p["key"] for p in observation["problems"]])
PYEOF
TRIAL_BENCH_ROOT=/tmp/hist-root GEMINI_API_KEY= .venv/bin/python /tmp/old-snapshot/_build_historical_trial.py

# 3) oracle·labels는 현재(수정 후) 파일을 그대로 재사용한다 -- expected_from은
#    이 값들을 truth-목록 키의 박스 보강과 쪽수 경고에만 쓰고, 어느 쪽도 이
#    측정(플래그 vs 실제 오류)에 영향을 주지 않는다.
cp ~/edb-trial-bench/oracle/english_go2_hakpyeong_20260324.json ~/edb-trial-bench/oracle/english_2020suneung_go3_20191107.json /tmp/hist-root/oracle/
cp ~/edb-trial-bench/labels/english_go2_hakpyeong_20260324.json ~/edb-trial-bench/labels/english_2020suneung_go3_20191107.json /tmp/hist-root/labels/

# 4) 이 저장소에 커밋된 스크립트로 채점한다 -- 임시 코드가 아니라
#    scripts/trial_bench/risk_flag_predictivity.py 그 자체가 만든 표다.
TRIAL_BENCH_ROOT=/tmp/hist-root GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/risk_flag_predictivity.py \
    english_go2_hakpyeong_20260324 english_2020suneung_go3_20191107
```

결과 (2026-09-17 측정, `score.py`를 같은 `TRIAL_BENCH_ROOT`로 돌려 `missing p16-17` / `extra q1#2 q2#2 q3#2 q4#2`가 여전히 재현됨을 먼저 확인했다):

| predictor | in badge today | tp | fp | fn | tn | precision | recall |
|---|---|---|---|---|---|---|---|
| fallback_grouping | yes | 0 | 0 | 4 | 57 |  | 0.00 |
| hwp_oversegmentation † | yes | 0 | 0 | 4 | 57 |  | 0.00 |
| marker_conflicts | yes | 0 | 0 | 4 | 57 |  | 0.00 |
| merged_problem_block | yes | 0 | 0 | 4 | 57 |  | 0.00 |
| 현재 배지 (needs_review) | -- | 0 | 0 | 4 | 57 |  | 0.00 |
| 아무 risk_flag나 (any) | -- | 0 | 0 | 4 | 57 |  | 0.00 |

> **단위 61개 중 실제 오류 4개, 플래그를 달 단위 자체가 없는 오류(정답에는 있으나 체험판이 아예 만들지 않은 키) 1개 -- 합쳐서 이 코퍼스가 아는 실제 오류는 총 5건이다.** `fn`은 단위가 존재하는 오류만 센다: 없는 단위에는 어떤 risk_flag도(현재도, 가상의 어떤 조합도) 붙을 수 없기 때문이다.

> **† 표시된 플래그는 이 코퍼스 크기와 무관하게 체험판 경로에서 절대 나타날 수 없다(구조적으로 도달 불가 -- 관측된 0건이 아니다): `hwp_oversegmentation`.** `problem_parser.parse_problems`(체험판이 부르는 경로)는 `build_pages`+`build_problem_entries`만 호출하고 `build_ui_session`은 절대 호출하지 않는데, 이 플래그는 `build_ui_session` 전용 페이지 레벨 플래그(`_collect_page_risk_flags`/`_collect_hwp_problem_count_mismatches`)라서 체험판 쪽 `_collect_problem_risk_flags`에는 이를 만드는 분기 자체가 없다 -- 코퍼스를 아무리 늘려도 이 표의 위 행은 0/0/0/N에서 움직이지 않는다.

> **이 표는 크롭 정확도(IoU) 오류를 오류로 세지 않는다.** score.py 자신의 리포트는 이 코퍼스에서 짝지은 단위인데 IoU가 `LOW_IOU`(0.8) 미만인 단위(`low_iou`)를 총 1개 기록한다: `english_go2_hakpyeong_20260324` (1). 그런 단위는 `extra`/`missing` 어디에도 없으므로 위 표의 `is_error`는 그것을 정답(true negative)으로 센다 -- 박스가 심하게 잘못 잘렸어도 이 표는 잡지 못한다. 어느 단위인지는 `scripts/trial_bench/score.py`의 리포트(`low_iou` 열)를 보라.

위는 `scripts/trial_bench/risk_flag_predictivity.py`가 그대로 출력한 표와 각주다(위 재현 명령을 오늘 다시 돌려도 동일하다). `passage_cross_page_merge_check`는 이 두 영어 케이스 어디에도 한 번도 나타나지 않아(0/61) 관측된 플래그 목록에 없으므로 위 표에도 없다 -- 위 코퍼스 전체 표의 21건은 전부 국어 케이스에서 나온 것이다. **표에 나온 "실제 오류 총 5건"은 score.py의 `extra`/`missing` 정의만 쓴 것이라, score.py 자신의 리포트가 아는 결함과 어긋난다**: 같은 `TRIAL_BENCH_ROOT`로 `score.py`를 돌리면 `english_go2_hakpyeong_20260324` 행에 `low_iou 1`이 찍힌다 -- 그 단위는 `q28`이고, `expected_from`/`regions_iou`로 직접 계산하면 IoU `0.261`, `risk_flags == []`다. 이 단위는 위 표에서 `tn`(정답, 손대지 않음)으로 들어가 있다 -- 잘못 잘린 박스를 위 predictor 표는 오류로 세지 않기 때문이다. 그러므로 이 재구성 실행에서 score.py가 아는 결함은 5건이 아니라 **6건**이고, 위 predictor 표(그리고 recall 0/5라는 결론)는 그중 `extra`/`missing`으로 정의되는 5건에 대해서만 말한다.

**결론: 지금 계산되는 *risk_flag* 중 어느 것도(배지 안에 있든, `passage_cross_page_merge_check`처럼 배지 밖에 있든) 이 5건 중 단 하나도 잡지 못했을 것이다 -- recall 0/5, 모든 predictor에서 동일.** 이 사실 자체는 검증됐고 그대로 유지된다. `passage_cross_page_merge_check`를 배지에 추가해도 이 5건에 대해서는 recall이 그대로 0이고, 대신 위 "현재 코퍼스" 표대로 코퍼스 리뷰율만 0.00 → 0.10으로 오른다(오탐 21건, 실제로 잡는 오류 0건) -- 그래서 `REVIEW_WORTHY_FLAGS`는 바꾸지 않는다.

**다만 "신호 자체가 존재하지 않는다"는 말은 두 결함 클래스 모두에 참은 아니다.** **본문 없는 대괄호 지문 헤더 흡수(수정 전 1건, `p16-17`)**는 기존 네 risk_flag의 의미 영역(페이지 헤더 중복, 마커 충돌, HWP 과분할, 페이지 넘김 지문 병합)과 실제로 무관하다 -- 이 결함에 대응하는 신호는 없다. 그러나 **안내문 상자 안 들여쓴 번호 목록이 최상위 문항으로 승격된 것(수정 전 4건, `q1#2`~`q4#2`)**은 다르다: 이 결함은 정확히 "같은 문항 번호가 두 번 나타난다"는 모양이고, 데스크톱 세션 경로의 `_session_duplicate_problem_number_groups`(`build_problem_board_edb.py:7436`)가 정확히 그 모양을 잡아내도록 이미 구현돼 있다. 그 5건 당시의 번호열(1~24, 그다음 1,2,3,4, 그다음 25~28)을 그대로 넣으면 `{'numberLabel': '1-4', 'occurrencesPerNumber': 2, 'classification': 'duplicate_number_reuse'}`가 정확히 나온다 -- 이 결함에 대한 직격 신호다. 그런데도 이것이 risk_flag가 되지 않는 이유는 셋이다: (1) `blocking`이 `build_problem_board_edb.py:7517`에서 언제나 `False`로 하드코딩돼 있고, (2) `_mark_duplicate_problem_number_review_flags`(`build_problem_board_edb.py:7589`)는 `blocking`이 아닌 그룹을 전부 건너뛴다(위 입력을 그대로 넣어 직접 확인: 32개 문항 중 어디에도 플래그가 붙지 않는다), (3) 무엇보다 체험판은 이 로직이 사는 `build_ui_session`을 애초에 호출하지 않는다(`problem_parser.parse_problems`는 `build_pages`+`build_problem_entries`만 호출한다). 정직한 문장은 "기존 신호의 의미 영역이 이 결함과 아예 무관하다"가 아니라 **"이 결함과 정확히 일치하는 신호가 데스크톱 세션 경로에 이미 계산돼 있지만, 억제돼 있고(non-blocking) 체험판에는 애초에 도달하지 않는다"**이다. `passage_cross_page_merge_check`를 포함해 지금 계산되는 *risk_flag*가 이 5건을 하나도 잡지 못했을 것이라는 위 문단의 검증된 사실은 바뀌지 않는다 -- 바뀌는 것은 "그래서 다음에 뭘 해야 하는가"뿐이다: 아래 사례 수 논의와 별개로, 이 결함 클래스에 대해서는 코퍼스를 더 모으는 대신 **`_session_duplicate_problem_number_groups`를 체험판 경로에 연결하고 216단위 코퍼스에서 오탐률을 측정**하는 쪽이 오늘 바로 실행 가능한 다음 단계다.

**5건(그리고 지금의 0건)은 어떤 방향으로도 결론을 낼 만한 표본이 아니다 -- 그리고 "5건"이라는 표본 크기 자체도 조심해서 읽어야 한다.** 위 "발견된 오류 유형" 2번이 이미 적었듯 그 4개 키(`q1#2`~`q4#2`)는 서로 독립된 관측이 아니다: "오류 지점은 한 곳(안내문 목록 하나)이지만 위양성 키는 4개였다." 그래서 이 5개의 오류 *키*는 서로 독립된 결함 **2건**(그 안내문 목록 하나, 그리고 본문 없는 `[16~17]` 헤더 하나)에서 나온 것이지, 5개의 독립된 관측이 아니다. 신뢰구간을 어느 쪽 표본으로 계산하느냐에 따라 결론의 폭이 크게 달라진다 -- 오류 *키* 기준 0/5의 Wilson 95% 신뢰구간은 [0%, 43%]지만, 독립 *결함* 기준 0/2는 [0%, 66%]로 훨씬 넓다(둘 다 아래 명령으로 계산, 재현 가능):
```
.venv/bin/python -c "
import math
def wilson(successes, n, z=1.96):
    phat = successes / n
    denom = 1 + z**2/n
    center = (phat + z**2/(2*n)) / denom
    halfwidth = (z * math.sqrt(phat*(1-phat)/n + z**2/(4*n**2))) / denom
    return center - halfwidth, center + halfwidth
print(wilson(0, 5))       # 오류 키 기준 0/5 -> (약 0%, 약 43%)
print(wilson(0, 2))       # 독립 결함 기준 0/2 -> (약 0%, 약 66%) -- 이쪽이 진짜 표본 크기다
print(wilson(10, 20))     # recall 절반짜리 신호와 구분하려면 대략 몇 건이 필요한지
print(wilson(50, 100))
"
```
아래 표본 크기 산정은 **독립 결함** 기준(0/2)을 쓴다 -- 배지가 실제로 마주치는 것은 서로 다른 결함이지, 그 결함이 우연히 몇 개의 키로 갈라지는지가 아니기 때문이다. '전혀 안 잡음'과 '절반은 잡음'을 구분할 수 있는 정도(반너비 ±20%p 안팎)만 되려도 확인된 독립 결함이 최소 20~25건은 있어야 하고(`n=20`일 때 반너비 0.20), 배지 교체를 정당화할 만큼 좁은 추정(반너비 ±10%p)을 얻으려면 대략 100건이 필요하다(`n=100`일 때 반너비 0.096). 이 코퍼스가 13개 케이스·219개 단위(수정 전 기준 -- 지금은 216개)를 만드는 동안 실제로 찾아낸 독립 결함은 **2건**뿐이고(오류 키로 잘못 세면 5건으로 보인다) 그나마 5개 과목 중 1개(영어, 2케이스)에 몰려 있었다 -- 같은 비율(케이스 13개당 결함 2건)로 20~25건을 유기적으로 모으려면 케이스 수를 `20*13/2 = 130`~`25*13/2 = 162.5`, 대략 **130~160케이스**로 늘려야 한다(오류 *키* 비율로 계산하면 60~70케이스가 나오는데, 그 결함이 우연히 4개 키로 갈라졌던 것을 매번 재현 가능한 비율로 착각한 결과라 약 2.5배 과소평가다). 100건을 모으려면 그보다 한 자릿수 더 큰 코퍼스이거나 오류를 의도적으로 주입한 별도 픽스처가 필요하다. **그래서 이 절의 정직한 답은 "배지를 이렇게 바꿔야 한다"가 아니라 "지금 가진 독립 결함 2건으로는 어느 플래그 조합도 정당화도 반박도 할 수 없다"이고, `REVIEW_WORTHY_FLAGS`는 바꾸지 않았다.** (위 결론 문단의 "recall 0/5, 모든 predictor에서 동일"이라는 검증된 사실 자체는 오류 *키* 5개를 그대로 세는 것이라 바뀌지 않는다 -- 바뀌는 것은 그 표본으로 "그래서 몇 건을 더 모아야 하는가"를 계산할 때뿐이다.)

### 오라클이 기여한 것과 하지 않은 것

- **기여한 것: 없다.** 두 결함을 고친 뒤의 2026-09-17 재실행에서 `adjudicate.py`가 보고한 disagreement는 13개 케이스를 통틀어 **1건**인데, 그 1건조차 오라클이 무언가를 잡아낸 것이 아니라 **오라클이 낡았다는 표시**다: `english_2020suneung_go3_20191107`의 `p16-17`이 `trial only`로 뜬다 -- 체험판은 `7bcf1bb` 이후 이 지문 단위를 만들지만 이 케이스의 오라클 관측값은 그 수정 이전(2026-09-16)의 것이라 아직 키가 28개뿐이기 때문이다. 이 1건 때문에 이번에는 `~/edb-trial-bench/adjudication/english_2020suneung_go3_20191107/p16-17.png` 한 장이 생성됐다(그 전까지는 `adjudication/` 디렉터리 자체가 만들어지지 않았다). 나머지 12개 케이스는 여전히 disagreement 0건이고, 그 12개는 오라클 관측값의 키 집합도 박스도 체험판과 동일하다. 같은 영어 케이스의 `q15`는 박스가 달라졌지만(IoU `0.8955`) `LOW_IOU=0.8` 위라 disagreement로는 잡히지 않는다 -- 이것도 같은 원인(수정 전 상태로 남아 있는 오라클 박스)이다.
- 그래서 오라클은 위 오류 유형 절이 기록한 5건 중 **단 하나도** 잡아내지 못했다. (수정되기 전에는) `p16-17`도, `q1#2`~`q4#2`도 오라클이 똑같이 놓치거나 만들어냈다. 5건을 찾아낸 것은 전적으로 Claude가 트리밍된 입력을 직접 읽어 만든 `ground_truth`다.
- **`ai_evidence` 열은 이번 재측정의 산물이 아니다.** 그 값(`oracle.page_repair.pages_changed`)은 오라클을 돌린 시점에 그 실행의 강제 AI 보정을 그때의 로컬 기준선과 비교해 센 것이므로, 13행 중 12행은 두 결함 수정 **이전** 파서로 잰 값이고 나머지 한 행(`english_go2_hakpyeong_20260324`)만 "없는 문항 생성" 수정 직후에 잰 값이다. 즉 `0/N (NO EVIDENCE)`가 13행 전부에 찍혀 있어도 그것은 "오늘의 파서에서 AI 보정이 아무것도 바꾸지 않는다"가 아니라 "그 오라클 실행 시점의 파서에서 아무것도 바꾸지 않았다"는 뜻이다. 다시 재려면 `scripts/trial_bench/oracle.py`를 13개 케이스 전부에 다시 돌려야 한다(유료 Gemini 호출).
- 근본 원인은 이전 절들이 기록한 그대로다: 강제 Gemini 페이지 보정이 13개 케이스 35쪽 전부에서 `repair_applied`는 됐지만 `repair_changed = 0`이었다(표의 `ai_evidence` 열이 13행 모두 `(NO EVIDENCE)`). 보정이 아무것도 바꾸지 않으니 오라클 출력은 정의상 체험판 출력과 같아지고, 오라클을 정답으로 쓰는 한 점수는 항상 1.00이 된다. 이번 재채점 이전의 `pending` 표가 전부 1.00이었던 것은 품질의 증거가 아니라 그 항등식이었다.

### 남아 있는 공백

- **영어**: 한때 유일하게 오류가 남아 있던 과목이었다(지문 누락 1건, `english_2020suneung_go3_20191107`). 그 건과 문항 위양성이던 다른 한 건(`english_go2_hakpyeong_20260324`)이 위 "발견된 오류 유형" 1·2번대로 모두 고쳐져, 이제 두 케이스 모두 `q_prec`/`p_recall` 1.00이다. 케이스도 2개뿐이라 과목 단위 수치의 표본이 가장 얇다는 점은 여전하다.
- **지문 범위를 가진 과목이 국어·영어뿐이다.** 지문 범위 9개는 국어 7개 + 영어 2개이고, 과학·수학·사회 8개 케이스에는 지문 범위가 하나도 없어 `p_recall`/`p_prec`가 빈 칸이다. 즉 §3의 지문 범위 게이트는 13개 케이스가 아니라 5개 케이스·9개 범위 위에서만 판정된다.
- **박스 정확도는 어느 과목에서도 검증되지 않았다**(위 게이트 판정의 IoU 항목). `ground_truth`에 박스가 없고, 채점된 215쌍 중 214쌍은 오라클 박스가 체험판 박스와 바이트 단위로 같다. 나머지 한 쌍(`english_2020suneung_go3_20191107`의 `q15`, IoU `0.8955`)도 독립적인 검증이 아니라 수정 전 상태로 남아 있는 오라클 박스와의 차이일 뿐이다 -- 박스에 대한 독립적인 기준은 코퍼스 전체에 여전히 0개다.
- **트리밍 쪽수 불일치**: 7개 케이스가 아직 앞 3쪽이고 원본이 `sources/`에 없다(위 결과 절). 그중 과학 3개는 원본 4쪽 중 한 쪽이 측정에서 통째로 빠져 있다.
- **재현성 점검은 여전히 범위 밖이다**: 계획서 §13이 요구한 "같은 케이스를 두 번 돌려 문항 집합이 다른지"는 이번에도 하지 않았다(HEAD에서 각 케이스 1회 관측). 위 "발견된 오류 유형" 절의 `eee279b^` 비교는 13개 케이스를 두 번 관측하긴 하지만 두 실행의 파서 버전이 다르므로, 같은 코드의 재현성 점검이 아니라 "그 커밋이 이 코퍼스를 바꾸지 않았다"는 확인이다.

## 2026-09-16: 코퍼스 확장과 오라클 수리 횟수 (헤드라인: 수리 0건)

- **코퍼스 규모와 과목 분포**: 13개 케이스(기존 7개 유지 + 신규 6개). 국어 3 · 과학 4(물리학Ⅰ×2, 지구과학, 전자기) · 영어 2 · 수학 2 · 사회(생활과윤리) 2. 영어·수학·사회는 이번에 처음 추가되어, 계획서가 요구한 다섯 과목이 모두 최소 2개 케이스로 채워졌다. 신규 6개는 전부 `ebsi.co.kr`(`wdown.ebsi.co.kr`) 공식 아카이브에서 내려받은 텍스트 레이어 PDF다.
- **케이스별 오라클 수리 횟수(헤드라인)**: 오라클이 끝까지 돈 11개 케이스 전부 `repair_changed = 0`이었다 -- Gemini 강제 보정이 로컬 기준선과 실제로 다른 답을 낸 쪽이 단 한 쪽도 없었다.
  | case | subject | oracle 쪽수 | repair_changed | repair_applied |
  |---|---|---|---|---|
  | 01_물리학Ⅰ_문제지 | science | 3 | 0/3 | 3/3 |
  | 2025학년도-수능-국어-언어와매체-홀수형 | korean | 3 | 0/3 | 3/3 |
  | 2026학년도-9월-모평-국어-언어와매체 | korean | 3 | 0/3 | 3/3 |
  | 2026학년도-수능-국어-언어와매체-홀수형 | korean | 3 | 0/3 | 3/3 |
  | earth_input | science | 3 | 0/3 | 3/3 |
  | math_2026suneung_9wolmopyeong_20250903 | math | 4 | 0/4 | 4/4 |
  | math_go3_hakpyeong_20240328 | math | 4 | 0/4 | 4/4 |
  | physics_input | science | 3 | 0/3 | 3/3 |
  | social_saengwoon_2020suneung_20191015 | social | 4 | 0/4 | 4/4 |
  | social_saengwoon_2025suneung_9wolmopyeong_20240904 | social | 4 | 0/4 | 4/4 |
  | 전자기_교재문제 | science | 3 | 0/3 | 3/3 |
  | english_2020suneung_go3_20191107 | english | 0 (오라클 실패, 이후 수정됨 -- 아래 2026-09-16 추가 항목 참고) | - | - |
  | english_go2_hakpyeong_20260324 | english | 0 (오라클 실패, 이후 수정됨 -- 아래 2026-09-16 추가 항목 참고) | - | - |

  영어 2개 케이스는 이 표를 처음 만들었을 때는 오라클이 페이지 하나도 처리하지 못하고 실패했다(`Gemini response JSON decode failed: Unterminated string`, 각각 1회 재시도에도 동일하게 재현되어 지속 실패로 기록). 원인을 진단하고 고친 뒤 다시 돌린 결과는 아래 "오라클 영어 지원 수정" 항목에 있다 -- 이 표의 두 행과 이 절의 판정·해석 문장(11개 케이스 기준)은 그 진단 이전 스냅샷이다. **영어 2개를 포함한 13개 케이스 전부를 다시 채점한 현재 결과는 위 "결과" 절에 있다**(2026-09-17). `repair_changed = 0`이라는 이 절의 헤드라인은 13개 케이스 35쪽 전부에서 그대로 유지됐다.
- **판정(adjudicate) 결과**: 오라클이 성공한 11개 케이스 모두 disagreement 0건, 생성된 조정용 이미지도 0장(`~/edb-trial-bench/adjudication/` 디렉터리 자체가 만들어지지 않았다). 영어 2개를 고쳐 넣은 뒤인 2026-09-17 재실행에서도 13개 케이스 전부 disagreement 0건으로 같았다. 파서 결함 2건을 고친 뒤의 같은 날 재실행에서는 1건이 되었는데, 그것은 오라클이 잡아낸 것이 아니라 오라클 관측값이 낡았다는 표시다(위 "오라클이 기여한 것과 하지 않은 것" 절).
- **이 숫자가 증명하는 것과 증명하지 않는 것**: 11개 케이스에서 체험판과 오라클이 완전히 일치한 것은, 오라클의 강제 AI 보정이 로컬 기준선을 단 한 쪽도 바꾸지 않았기 때문이다(`ai_evidence` 열, 위 표의 `repair_changed`). 즉 이 일치는 "체험판이 AI급 인식과 같다"는 근거가 아니라 "이번 코퍼스에서는 AI가 로컬 파서와 다른 답을 내지 않았다"는 근거일 뿐이다. `low_iou`/`review`/`missing`/`extra`가 전부 0인 것, adjudication 이미지가 0장인 것도 같은 이유로 독립적인 정답과의 일치를 뜻하지 않는다. **이 예상은 2026-09-17에 확인됐다**: 독립적으로 읽은 정답(`ground_truth`)으로 다시 채점하자 같은 관측값에서 `missing` 1건과 `extra` 4건이 나왔다(위 "결과" 절). `low_iou`가 0인 것만은 그때도 그대로였는데, 그것 역시 품질이 아니라 오라클 박스와 체험판 박스가 동일해서였다. 영어 과목은 (아래 수정 전까지는) 오라클 자체가 실패했으므로 일치·불일치 어느 쪽도 말할 수 없었고, 계획서(§13)가 요구한 "같은 케이스를 두 번 돌려 문항 집합이 다른지" 재현성 점검도 이번 실행 범위 밖이다(1회씩만 실행 -- 2026-09-17 재채점에서도 마찬가지다).

## 2026-09-16 추가: 오라클 영어 지원 수정

- **원인**: `page_repair.py`의 `_repair_output_token_budget`은 강제 오라클 경로에서도 블록당 24토큰짜리 어림값(`512 + 24*block_count`)으로 실제 Gemini 호출의 `maxOutputTokens`를 계산한다. `english_2020suneung_go3_20191107`의 실패를 계측해 다시 돌려 보니(`page_repair.GeminiRepairTruncatedError`가 새로 기록하는 진단 정보) `block_count=11`, `include_problem_units=False`일 때 어림값이 776까지만 나왔다 -- `force_config`가 설정한 4096은커녕 2048 하드 캡보다도 훨씬 작다. 응답은 `finishReason=MAX_TOKENS`로 `display_titles` 배열 도중 블록 id 문자열이 끊긴 채로 잘렸다(`~/edb-trial-bench/oracle_failures/english_2020suneung_go3_20191107.json`에 그 진단 기록이 남아 있다). 24토큰/블록 어림값은 `"block-1"` 같은 짧은 테스트용 id를 기준으로 잡힌 값이라, 실제 페이지 id(`english_2020suneung_go3_20191107-page-001-block-011`, 50자 이상, 블록마다 `problem_start_block_ids`와 `display_titles` 두 곳에 등장)의 길이를 전혀 반영하지 못한다.
- **수정**: `page_repair.AIFallbackConfig`에 `max_output_token_cap`(기본값 `None`)을 추가해, 값이 주어지면 블록당 어림값·2048/3072 하드 캡을 모두 건너뛰고 `min(configured_max_tokens, max_output_token_cap)`만 쓴다. 데스크톱 쪽 `ai_fallback_config` 딕셔너리(`build_problem_board_edb.py`의 `_build_ai_fallback_config`)는 이 키를 절대 만들지 않으므로 데스크톱 동작은 그대로다. `scripts/trial_bench/oracle.py`의 `force_config`만 `max_tokens=8192`·`max_output_token_cap=8192`를 설정한다. 또한 JSON 디코드가 실패했을 때 `finishReason`이 `MAX_TOKENS`/`LENGTH`이면 평범한 `RuntimeError` 대신 `page_repair.GeminiRepairTruncatedError`(모델명·finishReason·유효/설정 토큰 한도·프롬프트·응답 바이트 수·응답 앞뒤 200자를 담은 `diagnostics`)를 던지도록 바꿨다. `_request_ai_repair_with_retry`는 이 오류를 **`config.max_output_token_cap`이 설정된 호출(즉 오라클의 `force_config`)에서만** 같은 모델로 재시도하지 않는다 -- 근거는 `temperature=0.0`이라서가 아니다(`FALLBACK_GEMINI_REPAIR_MODEL`인 `gemini-3.6-flash`는 `_request_gemini_repair`가 실제로 `temperature`를 지우므로 동일 요청이라도 결정적이지 않다). 수정 전 오라클 실패 기록(`~/edb-trial-bench/oracle_failures/english_go2_hakpyeong_20260324.json`)이 "AI repair failed after retries: ... Unterminated string"로 남아 있어, 같은 모델로 한 번 재시도한 결과가 실제로 똑같이 잘렸다는 것을 직접 보여준다. 데스크톱 쪽 호출은 `max_output_token_cap`을 절대 설정하지 않으므로 이 재시도 건너뛰기는 적용되지 않고 -- 절단을 일으키는 블록당 어림값 자체는 데스크톱에도 그대로 남아 있으므로 -- 데스크톱은 절단이 나면 여전히 재시도한다(수정 전과 동일하게 시도 2회).
- **재현 명령과 결과**: `.venv/bin/python scripts/trial_bench/oracle.py --runtime-dir /Users/clmagi/Desktop/Projects/edb_mak/.app_runtime english_2020suneung_go3_20191107`와 같은 방식으로 `english_go2_hakpyeong_20260324`도 실행. 두 케이스 모두 이제 4쪽 전부 오라클 관측값을 만든다:

  | case | oracle 쪽수 | repair_attempted | repair_changed | repair_applied | repair_model |
  |---|---|---|---|---|---|
  | english_2020suneung_go3_20191107 | 4 | 4/4 | 0/4 (NO AI EVIDENCE) | 4/4 | gemini-3.1-pro-preview |
  | english_go2_hakpyeong_20260324 | 4 | 4/4 | 0/4 (NO AI EVIDENCE) | 4/4 | gemini-3.1-pro-preview |

  두 케이스 모두 기본(primary) 모델(`gemini-3.1-pro-preview`)에서 바로 성공했고 `gemini-3.6-flash`로 폴백하지 않았으며, 오류는 0건이다. `repair_changed=0/4`는 위 11개 케이스와 같은 기존 이슈(오라클의 강제 AI 보정이 로컬 기준선과 실제로 다른 답을 낸 쪽이 없음)이지 이번 수정이 만든 새 문제가 아니다 -- 이번 수정의 범위는 "오라클이 영어 페이지를 끝까지 처리하는가"였지 "AI가 실제로 무언가를 고치는가"가 아니다. 영어 2개 케이스를 `score.py`/`adjudicate.py`로 표에 반영하는 작업은 2026-09-17에 끝났고, 결과는 위 "결과" 절에 있다 -- 코퍼스에서 실제 오류가 나온 유일한 두 케이스가 공교롭게도 이 두 개였다.

## 2026-09-17 추가: 수리-변경 감지기의 라이브 포지티브 컨트롤

위 두 절 모두 오라클이 돈 케이스 전부(11개, 이후 영어 2개 포함 13개) `repair_changed = 0`이었다고 기록한다. `page_repair.py`의 `_repair_change_counters`(`blocks_changed`/`problems_regrouped`/`titles_changed`/`boxes_overridden`/`problem_metadata_changed`와 그 합집합 `changed`)는 유닛 테스트로는 합성 입력에서 발동하는 것이 증명돼 있지만, **실제 Gemini 응답에서 발동하는 모습은 이 코퍼스만으로는 한 번도 관측된 적이 없었다** -- "AI가 동의했다"와 "diff가 실제 AI 답변을 못 알아본다"를 이 데이터만으로는 구분할 수 없었다는 뜻이다.

- **컨트롤 설계**: `scripts/trial_bench/control.py`(신규)가 코퍼스에 이미 있는 시험지 한 쪽을 골라 **이미지 전용 PDF로 재발행**한다(`build_control_pdf`: PyMuPDF로 그 쪽을 ~200dpi PNG로 렌더링한 뒤 텍스트 레이어·벡터 드로잉이 전혀 없는 새 1쪽짜리 PDF로 감싼다 -- `page.get_text()`가 빈 문자열을 반환하는 것으로 확인). 텍스트 레이어가 없으니 체험판의 텍스트-마커 세그멘터는 읽을 게 없다. **이 컨트롤은 AI 없는 파이프라인을 테스트하지 않는다**: `ocr_mode="auto"`(기본값)에 `GEMINI_API_KEY`가 있으면 `ocr_backend.build_ocr_backend("auto")`는 `GeminiOCRBackend`로 귀결되고(`preferred_ocr_backend_name("auto") == "gemini"`), `control.py run`은 애초에 그 키 없이는 시작조차 하지 않으므로(`main()`의 가드), 이 컨트롤이 diff의 "이전" 쪽으로 쓰는 로컬 기준선 자체가 항상 같은 페이지 이미지를 본 Gemini 비전 OCR의 산출물이다. 이 컨트롤이 실제로 격리하는 것은 그 다음 단계, **문항 경계 보정(page repair)** 하나뿐이다: 같은 Gemini-OCR 기준선에 강제 Gemini 수리를 한 번 더 돌려서 결과가 실제로 달라지는지만 본다(진짜 AI-프리 기준선은 `--ocr-mode none`으로 별도로 얻을 수 있다). 컨트롤 입력·오라클 관측값은 전부 `~/edb-trial-bench/control/`(코퍼스의 `inputs/`·`cases.json`·`oracle/`와 별도 루트) 아래에만 쓰고, 저장소에는 `scripts/trial_bench/control.py`와 `test_trial_bench_control.py`만 커밋한다. `build_control_pdf`/`run_control`은 `root`를 기본값 없는 필수 키워드 인자로 받아, 호출자가 실수로 코퍼스 루트(`BENCH_ROOT`)를 넘길 길 자체를 없앴다 -- `test_trial_bench_control.py`의 `test_root_is_required_with_no_default`/`test_run_control_root_is_required_with_no_default`, 그리고 `main()`을 직접 호출해 `<root>/control/inputs/`에만 쓰고 `<root>/inputs/`는 절대 만들어지지 않는 것을 확인하는 `TestMain`이 이를 고정한다.
- **재현 명령**:
  ```
  .venv/bin/python scripts/trial_bench/control.py build --source ~/edb-trial-bench/inputs/math_go3_hakpyeong_20240328.pdf --page 0
  .venv/bin/python scripts/trial_bench/control.py run --runtime-dir /Users/clmagi/Desktop/Projects/edb_mak/.app_runtime --source ~/edb-trial-bench/inputs/math_go3_hakpyeong_20240328.pdf --page 0 --subject math --attempts 5
  ```
  (`run`이 내부에서 `build`를 다시 수행하므로 `build`는 입력을 눈으로 확인하고 싶을 때만 따로 실행하면 된다.) **`--attempts` 없이 한 번만 돌리면 이 명령은 절반 가량 `invalid_response`로 끝난다** -- 응답이 `_validate_repair_payload`를 통과해야 diff가 돌기 때문이며, 아래 "재현성" 항목에 실제 뒤집힘 비율과 그래서 `--attempts`를 넣은 경위가 있다.
- **측정일 2026-09-17, 세 번의 시도**:

  | 컨트롤 입력 | ocr_mode | status | repair_changed | 실패/성공 사유 |
  |---|---|---|---|---|
  | social_saengwoon_2020suneung_20191015 1쪽 | auto | invalid_response | 0/1 | `problem start and choice block ids overlap` |
  | 전자기_교재문제 1쪽 (auto, 이어서 none으로 재시도) | auto / none | invalid_response | 0/1 (둘 다) | `problem_start_block_ids must be in reading order` |
  | **math_go3_hakpyeong_20240328 1쪽** | auto | **applied** | **1/1** | (검증 통과, 아래 상세 -- 단 이 행은 **재실행하면 절반 가량 `invalid_response`로 뒤집힌다**, 아래 재현성 항목) |

  앞의 두 시도는 `page_repair._validate_repair_payload`의 스키마 검증에서 걸렸다 -- Gemini는 확실히 응답했고(`repair_attempted=1/1`, 요청·파싱 자체는 성공) 로컬 기준선에도 실제로 동의하지 않았지만(사회 케이스는 원본 우측 칼럼 전체가 기준선에서 문항 하나로 뭉쳐 있었고, 전자기 케이스는 로컬 블록의 bbox 자체가 서로 겹쳐 있었다 -- `~/edb-trial-bench/control/oracle/*.json`의 `problems[].regions[].bbox` 참고), 반환한 `problem_start_block_ids`/`choice_block_ids`가 로컬이 준 블록 파티션 안에서 스키마 제약(겹치지 않음/읽기 순서)을 만족시키지 못해 `_apply_repair_payload`가 아예 호출되지 않았다 -- 즉 `changed`는 "AI가 동의해서"가 아니라 "diff 자체가 실행되지 못해서" False로 남았다. 이 두 실패는 그 자체로 유효한 관측이다: 강제 오라클 파이프라인이 실제 Gemini 응답을 놓고 실제로 검증을 수행한다는 것, 그리고 `summarize_page_repair`의 `statuses`/`errors`가 "AI가 답했지만 거부됐다"를 "AI가 동의했다"와 구분해 기록한다는 것을 살아있는 트래픽으로 확인해 준다.
- **세 번째 시도(양성 결과) 상세**: `math_go3_hakpyeong_20240328` 1쪽에서 `ocr_mode="auto"` 기준선(위에서 설명했듯, `GEMINI_API_KEY`가 있으므로 실제로는 Gemini 비전 OCR의 산출물이지 AI 없는 로컬 인식이 아니다)은 페이지 우측 상단의 시험지 머리말 블록을 "문항 3"으로 잘못 인식했다(그 블록의 텍스트가 그대로 `"국(전국)연합학력평가 문제지\n...\n영역"`이고, 문항 번호로는 `3`이 배정돼 있었다) -- 그러면서 실제 세 번째 문항(우측 칼럼의 $\cos\theta$/$\tan\theta$ 문제)은 내부 번호 `5`로 밀려났다. `control.py run`이 이제 이 기준선 크기를 직접 출력·저장한다(`_page_rows`의 `baseline_block_count`/`baseline_problem_count` 열, 그리고 저장된 `control/oracle/<case>.json`의 `oracle.page_repair_pages[0]` -- 이전에는 터미널에만 찍히고 어디에도 저장되지 않았다): `baseline_block_count=6`, `baseline_problem_count=4`(문항 4에 해당하는 블록은 아예 없다). 강제 오라클을 돌리면 `repair_attempted=1/1`, `status=applied`, 모델은 `gemini-3.1-pro-preview`(폴백 없음), 그리고 (`control/oracle/math_go3_hakpyeong_20240328-p1-image-only.json`에 그대로 저장된) 페이지별 원시 카운터는:

  | blocks_changed | problems_regrouped | titles_changed | boxes_overridden | problem_metadata_changed | changed |
  |---|---|---|---|---|---|
  | 0 | False | **5** | 0 | 0 | **True** |

  즉 `_repair_change_counters`가 `titles_changed=5`를 통해 `changed=True`를 반환했다. 다만 이 관측값만으로는 "제목이 정확히 어떻게 바뀌었는지"는 말할 수 없다: `scripts/trial_bench/common.py`의 `_safe_title`은 지문 마커나 짧은 번호 마커(`"3."` 같은 형태)가 아닌 제목은 전부 `null`로 지워 버리므로(이번 관측값에서도 문항 4개 전부 `"title": null`), 머리말 텍스트가 그대로 남은 제목과 AI가 새로 붙인 설명형 제목이 이 저장된 JSON 안에서는 구분되지 않는다 -- `_apply_repair_payload`(page_repair.py:1443-1456)는 페이로드가 값을 준 블록에 대해서만 `display_title`을 *쓸* 뿐 지우는 코드 경로가 아예 없으므로, "머리말 제목이 사라졌다"는 식의 서술은 이 관측값으로는 뒷받침되지 않는다. 이 컨트롤이 실제로 보여주는 것은 카운터 자체뿐이다: 스키마를 통과한 실제 Gemini 응답이 `display_titles`를 실어 보냈고, `titles_changed`가 5가 되면서 그 합집합인 `changed`가 `True`가 됐다는 것 -- **"기준선과 다른 제목"이었다는 뜻은 아니다. 바로 아래 항목이 그 이유다.**
- **이 결과가 증명하는 것과 증명하지 않는 것**: 이번 컨트롤이 살아있는 트래픽으로 확인한 것은 `changed`와 `titles_changed` 두 카운터가 **실제 모델 출력에서 실제로 움직인다**는 것뿐이다 -- 스키마 검증을 통과한 진짜 Gemini 응답이 display title을 하나라도 실어 보내면 `titles_changed`가 0보다 커지고 그 합집합인 `changed`가 `True`가 된다는 것은 이제 실제 응답으로 직접 관측됐다. 감지기가 구조적으로 죽어 있지는 않다는 뜻이다. **그러나 그 응답이 기준선과 달랐다는 증거는 아니다.** `_classification_state`(page_repair.py:297)가 읽는 `block.metadata["display_title"]`을 쓰는 코드는 `_apply_repair_payload`(page_repair.py:1456)와 segment.py의 **PDF 텍스트 레이어 세그멘터 세 곳**(`_segment_pdf_example_markers` segment.py:1732, `_build_pdf_passage_range_blocks` segment.py:2102, `_segment_pdf_problem_markers` segment.py:2351·2356)뿐인데, 이 컨트롤의 입력은 텍스트 레이어를 일부러 지운 이미지 전용 PDF라 그 세 곳이 애초에 돌 수 없다. 그래서 이 페이지의 기준선 쪽 `display_title` 슬롯은 블록 6개 전부 `None`이고, `_count_changed_blocks`는 응답이 비어 있지 않은 제목을 준 블록을 **전부** "바뀜"으로 센다 -- 그 제목이 기준선이 실제로 그 블록에 쓰고 있던 값(`assemble_page.py:212` `_problem_title_source`의 폴백, 즉 블록 자신의 텍스트)과 같든 다르든 상관없이. `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_page_repair.py`의 `test_titles_changed_counts_a_display_title_that_only_echoes_the_block_text`가 이 구멍을 고정한다: 그룹핑이 기준선과 완전히 같고 `display_titles`가 각 블록의 텍스트를 글자 그대로 되돌려주기만 하는 페이로드에서도 `titles_changed=2`/`changed=True`가 나오고, 그러면서 결과 `ProblemUnit.title`은 기준선(`['문제','문제']`)과 바이트 단위로 동일하다. 오늘 저장한 원시 페이로드가 이 설명을 실제 응답으로도 확인해 준다(`~/edb-trial-bench/control/oracle/math_go3_hakpyeong_20240328-p1-attempts.json`의 `oracle.repair_payloads`, `response_id P8GqatzMC6fL2roPvYniyQQ`): 모델은 `display_titles`를 **딱 두 블록**(`-block-001`, `-block-004`)에 대해서만 보냈고 -- 둘 다 같은 응답의 `problem_start_block_ids`에 들어 있지 않으며 붙은 제목도 문항 제목이 아니라 시험지 머리말/배너 텍스트다 -- 그 실행의 `titles_changed`는 정확히 **2**였다. 기준선의 해당 슬롯 두 개가 `None`이었으므로 이 2는 "모델이 제목 두 개를 실어 보냈다"만으로 전부 설명되고, "모델이 기준선과 다르게 판단했다"는 가정은 전혀 필요하지 않다. **여기서 코퍼스 쪽으로 한 단계 더 좁힐 수 있는데, 리뷰가 제안한 형태 그대로는 성립하지 않는다**: 코퍼스에서 `applied`로 처리된 모든 쪽(위 두 절의 13개 케이스)의 `changed=0`을 "Gemini가 어느 쪽에도 `display_titles` 항목을 하나도 반환하지 않았다"로 읽으려면 코퍼스 기준선의 `display_title` 슬롯도 전부 `None`이어야 하는데, 코퍼스 입력은 전부 텍스트 레이어가 살아 있는 진짜 시험지 PDF이고 `_segment_pdf_problem_markers`가 마커 블록마다 `display_title`을 **미리 채워 넣는다**(segment.py:2351의 `f"{number}."`, 즉 `"1."`·`"2."` 같은 값; 번호가 없으면 segment.py:2356의 마커 텍스트). 그래서 코퍼스에서 `titles_changed=0`이 뜻하는 것은 "모델이 제목을 안 보냈다"가 아니라 **"모델이 각 블록에 대해 아무것도 안 보냈거나, 보낸 제목이 그 블록의 기준선 값과 글자까지 똑같았거나 둘 중 하나"**다 -- `test_titles_changed_stays_zero_when_the_baseline_already_carries_that_title`이 그 대칭을 고정한다(같은 `"1."`·`"2."` 응답이 이미지 전용 컨트롤에서는 변경으로 세어지고 텍스트 레이어 페이지에서는 0으로 세어진다). 둘을 가르려면 코퍼스 오라클도 검증 통과 페이로드를 남겨야 하는데, `oracle.force_config`는 (시험지 원문이 그대로 들어가는 아티팩트라) 일부러 그렇게 하지 않는다 -- **다음에 확인할 것은 바로 이것이다.** `blocks_changed`, `problems_regrouped`, `boxes_overridden`, `problem_metadata_changed` 네 카운터는 이 컨트롤로도 **여전히 실제 모델 출력에서 한 번도 발동한 적이 없다** -- 위 세 번의 시도도, 오늘의 `--attempts` 재실행도 블록 재분류, 문항 재그룹, `bbox_px` override, 문항 메타데이터 변경을 하나도 만들어내지 못했다(앞의 두 실패는 스키마 검증에서 걸려 diff 자체가 실행되지 않았고, math 케이스는 `applied`가 난 세 번 모두 제목 카운터만 움직였다). 이 네 경로 중 하나에 blindness 버그가 있어도 이번 컨트롤의 관측과 완전히 들어맞으므로, "감지기가 실제 모델 출력을 못 알아보는 것은 아니다"라는 결론은 `changed`/`titles_changed` 경로에 대해서만 성립한다. 위 두 절과 상단 코퍼스 표의 `repair_changed = 0/N`(`NO AI EVIDENCE`)이 13개 케이스 전부에서 나온 것에 대해 이번 컨트롤이 배제하는 것은 딱 하나, "`titles_changed` 경로 자체가 죽어 있어서 아무것도 셀 수 없었다"는 가능성뿐이다 -- 나머지 네 경로(블록 재분류, 재그룹, bbox override, 메타데이터)가 죽어 있을 가능성은 이번 컨트롤로 배제되지 않는다. 그 전제 위에서, 2026-09-16 절의 설명("이 13개 케이스에서는 강제 Gemini 답변이 매번 로컬 기준선과 실제로 일치했다")은 유지된다. 코퍼스 표의 1.00 점수들이 "체험판이 AI급 인식과 같다"는 근거가 아니라는 결론 자체도 바뀌지 않는다. 나머지 네 카운터를 살아있는 트래픽으로 검증하려면, 모델이 블록을 재분류하거나 문항을 재그룹하거나 `bbox_px`를 바꿔 답하는 페이지를 담은 새 컨트롤이 필요하다.
- **재현성 -- 이 컨트롤은 한 번 돌려서 재현되는 실험이 아니다**: 기본 모델 호출이 `temperature=0.0`으로 고정돼 있다는 것(`page_repair.py`의 `_request_gemini_repair`)은 응답이 결정적이라는 보장이 아니다. 이 케이스(math 1쪽)의 **알려진 실행은 2026-09-17 기준 6회**이고, 그중 **3회가 `applied`, 3회가 `invalid_response`**(3회 모두 같은 사유 `problem start and choice block ids overlap`)다. `applied`가 난 3회조차 `titles_changed`가 각각 **6 → 5 → 2**로 매번 달랐다 -- 그 외 `blocks_changed=0`/`problems_regrouped=False`/`boxes_overridden=0`/`problem_metadata_changed=0`/`changed=True`/`baseline_block_count=6`/`baseline_problem_count=4`/모델(`gemini-3.1-pro-preview`, 폴백 없음)은 3회 모두 동일했다. 즉 위 명령을 지금 그대로 돌리면 `invalid_response`가 나올 수 있고, 그래도 이 절의 주장이 틀린 것이 아니라 그것이 이 실험의 성질이다.
- **그래서 이 절의 1차 증거는 명령이 아니라 저장된 관측값이다**: `~/edb-trial-bench/control/oracle/math_go3_hakpyeong_20240328-p1-image-only.json`(위 카운터 표 그대로 -- `status=applied`, `titles_changed=5`, `response_id p7qqau3tNtCM1e8P_OHykAU`, 실제 `token_usage`)와 `~/edb-trial-bench/control/oracle/math_go3_hakpyeong_20240328-p1-attempts.json`(오늘 `--attempts 5`로 돌린 재검증 -- 1번째 시도에서 검증 통과, `titles_changed=2`, `response_id P8GqatzMC6fL2roPvYniyQQ`, **원시 페이로드 포함**). 뒤집힌 쪽도 같은 루트에 남아 있다: `~/edb-trial-bench/control/oracle/review-verify-math-p1.json`은 같은 원본 쪽·같은 명령이 `invalid_response`(`problem start and choice block ids overlap`)로 끝난 실행의 관측값이고, `baseline_block_count=6`/`baseline_problem_count=4`는 `applied` 실행들과 동일하다 -- 즉 뒤집히는 것은 입력이 아니라 모델 응답이다. 이 파일들은 전부 `~/edb-trial-bench/control/` 아래에만 있고 저장소에는 없다.
- **그래서 `control.py`에 두 가지를 넣었다**:
  - `--attempts N` -- 응답 하나가 `_validate_repair_payload`를 통과할 때까지 다시 시도하고, 매 시도의 status를 출력하면서 전부 관측값(`oracle.attempts`)에 남긴다. `run_control`은 검증에 성공한 **첫** 시도에서 멈추고 그 시도의 관측값을 저장하며, 예산을 다 써도 하나도 통과하지 못하면 마지막 시도의 관측값을 저장한다(`test_retries_until_a_response_validates_and_saves_that_attempt` / `test_spends_the_whole_budget_then_saves_the_last_attempt`). 시도마다 Gemini 호출이 새로 나가므로 유료다. 예외(=`fail_on_error=True`가 올리는 전송·파싱 실패)는 재시도하지 않고 기존대로 `oracle_failures/`에 기록하고 그대로 올린다.
  - **검증을 통과한 원시 페이로드 저장** -- `control_force_config`가 `oracle.force_config` 위에 `save_debug=True` 하나만 더 켜서 page_repair의 `_maybe_write_debug_artifacts`가 `{summary, repair_payload}`를 쓰게 하고, `_parse_control`이 임시 파스 디렉터리가 지워지기 **전에** 그것을 거둬 `oracle.repair_payloads`에 넣는다(`test_saves_the_raw_validated_payload_before_the_scratch_dir_is_deleted`가 그 수명 관계를 고정한다). 이제 "모델이 어떤 제목을 어느 블록에 보냈나"를 새 유료 호출 없이 확인할 수 있다 -- 위 항목의 payload 분석이 그 첫 사례다. `invalid_response`로 거부된 응답은 `_maybe_write_debug_artifacts`에 닿기 전에 반환되므로 페이로드가 남지 않는다(남는 것은 그 페이지의 `status`/`error`뿐). `save_debug`는 코퍼스 오라클(`oracle.force_config`)에는 넣지 않았다: 그 아티팩트에는 시험지 원문이 그대로 들어간다.
- **이 저장 기능보다 앞선 관측값에는 페이로드가 없다**: 2026-09-17 00:50의 `math_go3_hakpyeong_20240328-p1-image-only.json`에는 `oracle.repair_payloads`가 없고, 그 실행의 `titles_changed=5`가 어떤 제목에서 나왔는지는 되살릴 수 없다. 위 카운터 표는 그 관측값 그대로이고, 지금 다시 만들려면 실제 `GEMINI_API_KEY`가 등록된 `--runtime-dir`로 위 재현 명령을 다시 돌려야 하며 카운터는 또 달라질 수 있다.

## 2026-09-17 추가: 코퍼스 17건으로 확장 (제작 도구·재출력 경로 다양화)

- **추가한 4건**(전부 이 기계에 이미 있던 실제 파일이며 `~/edb-trial-bench/sources/`에 깨끗한 이름으로 복사한 뒤 `make_inputs.py`로 앞 4쪽을 잘랐다):

  | case | subject | 원본 | 왜 넣었나 |
  |---|---|---|---|
  | `korean_go2_hakpyeong_20260326_hwp` | korean | 2026학년도 3월 고2 학평 국어, 16쪽 | 코퍼스에 없던 **HWP 제작 PDF**(producer `PDF 2022 12.0.0.535`). 4쪽 끝에서 `[11~13]` 지문 묶음이 잘려 12·13번이 5쪽에 있는 케이스 |
  | `korean_2025suneung_distiller` | korean | 2025학년도 수능 국어 홀수형, Acrobat Distiller 원본 20쪽 | 기존 케이스 `2025학년도-수능-국어-언어와매체-홀수형`(pypdf 재저장본, 3쪽 컷)과 같은 시험지의 **다른 제작 경로**, 4쪽 컷 |
  | `chemistry_2025suneung` | science | 2026학년도 수능 화학 I 4쪽 발췌 (파일명은 25수능) | 과학탐구 과목 추가(물리·지구과학 외 화학), 표·오비탈 그림이 많은 2단 |
  | `em_textbook_edited_quartz` | science | 전자기 교재 연습 문제 4쪽, macOS Quartz PDFContext 재출력 | 기존 `전자기_교재문제`(Hancom PDF 21쪽)와 같은 책의 **Quartz 재출력본** — 재출력이 텍스트 층 순서를 바꾸는지 확인 |

- **정답(`ground_truth`)**: 4건 모두 잘린 입력을 110 DPI PNG로 렌더링해 Claude(Fable 5.1)가 직접 읽었다(`verified_by: model`, 사람 서명 없음). 문항 번호·지문 범위는 라벨 파일에만 둔다.
- **결과**: 위 표대로 4건 모두 `q_recall`·`q_prec` 1.00, 국어 2건은 `p_recall`·`p_prec` 1.00, 확인 필요 0, `adjudicate.py` 불일치 0건. 합계 행은 **17/17 truth-backed**. 오라클은 4건 16쪽 전부 `repair_changed = 0`(기존 13건과 같은 "NO EVIDENCE")이라 이번에도 박스 IoU는 미측정 상태 그대로다.
- **이 확장이 말해 주는 것**: HWP 제작본·Distiller 원본·Quartz 재출력본·화학 시험지에서도 파서가 깨지지 않았다. 수능형 시험지 안에서는 더 이상 실패 입력을 찾기 어렵다는 뜻이며, 다음 실패는 학원 자체 제작(한글·워드) 시험지나 OCR 텍스트 층이 얹힌 스캔본에서 찾아야 한다.
- **같은 날 체험판에 붙은 "이어짐" 표시**(`docs/superpowers/specs/2026-09-17-web-trial-page-cut-continuation-design.md`): 위 국어 2건처럼 4쪽 끝에서 잘린 지문 묶음은 응답 `problems[].continuation`으로 표시되며, 채점에는 영향이 없다(관측 JSON은 `parse_problems` 결과만 담는다).
