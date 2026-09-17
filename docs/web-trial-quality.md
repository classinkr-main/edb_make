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

- **코퍼스 규모**: 13개 케이스 / 5개 과목 -- 과학 4(물리학Ⅰ×2, 지구과학, 전자기) · 국어 3 · 영어 2 · 수학 2 · 사회(생활과윤리) 2. Claude가 트리밍된 입력에서 직접 읽고 센 정답은 문항 207개와 지문 범위 9개이고, 체험판이 내놓은 단위는 215개다(이 절을 수정하기 전 측정에서는 219개였다 -- 아래 "발견된 오류 유형" 2번이 기록한 위양성 4개가 그 사이에 고쳐졌다).
- **트리밍 쪽수는 케이스마다 다르다 -- 코퍼스 전체가 앞 3쪽인 것도, 전체가 앞 4쪽인 것도 아니다.** 영어·수학·사회 6개 케이스는 현재 캡(`trial_input.py`의 `DEFAULT_MAX_PAGES = 4`)대로 앞 4쪽이고, 나머지 7개(국어 3 · 과학 4)는 캡이 3이던 시절에 만들어진 앞 3쪽 입력 그대로다. 이 7개는 `~/edb-trial-bench/sources/`에 원본 PDF가 남아 있지 않아(현재 `sources/`에는 2026-09-16에 추가한 6개뿐) 다시 4쪽으로 자르려면 원본을 다시 받아야 한다. 특히 `01_물리학Ⅰ_문제지`·`earth_input`·`physics_input`은 원본 자체가 4쪽이므로, 오늘의 체험판이라면 읽었을 네 번째 쪽이 이 측정에는 아예 들어 있지 않다.
- 표의 `ai_evidence` 열은 이제 13행 모두 채워져 있고, 13행 모두 `(NO EVIDENCE)`다. 그 뜻은 아래 "오라클이 기여한 것과 하지 않은 것" 항목에 있다.

<!-- corpus-table -->
측정일 2026-09-17 · 라벨 없는 케이스는 오라클을 임시 정답으로 채점(pending)

| case | status | q_recall | q_prec | p_recall | p_prec | mean_iou | low_iou | review | missing | extra | trial_ms | oracle_ms | ai_evidence | verified_by |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01_물리학Ⅰ_문제지 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1227 | 7971 | 0/3 (NO EVIDENCE) | model |
| 2025학년도-수능-국어-언어와매체-홀수형 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1400 | 12856 | 0/3 (NO EVIDENCE) | model |
| 2026학년도-9월-모평-국어-언어와매체 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1262 | 8267 | 0/3 (NO EVIDENCE) | model |
| 2026학년도-수능-국어-언어와매체-홀수형 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1360 | 8386 | 0/3 (NO EVIDENCE) | model |
| earth_input | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1213 | 6711 | 0/3 (NO EVIDENCE) | model |
| english_2020suneung_go3_20191107 | truth | 1.00 | 1.00 | 0.00 |  | 1.00 | 0 | 0.00 | p16-17 |  | 1307 | 43832 | 0/4 (NO EVIDENCE) | model |
| english_go2_hakpyeong_20260324 | truth | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1302 | 15796 | 0/4 (NO EVIDENCE) | model |
| math_2026suneung_9wolmopyeong_20250903 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 740 | 11783 | 0/4 (NO EVIDENCE) | model |
| math_go3_hakpyeong_20240328 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 633 | 10122 | 0/4 (NO EVIDENCE) | model |
| physics_input | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1165 | 7385 | 0/3 (NO EVIDENCE) | model |
| social_saengwoon_2020suneung_20191015 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1330 | 16030 | 0/4 (NO EVIDENCE) | model |
| social_saengwoon_2025suneung_9wolmopyeong_20240904 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1508 | 16190 | 0/4 (NO EVIDENCE) | model |
| 전자기_교재문제 | truth | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 858 | 10065 | 0/3 (NO EVIDENCE) | model |
| 합계 | 13/13 truth-backed, 0/13 provisional (0 approved) | 1.00 | 1.00 | 0.80 | 1.00 | 1.00 | 0 | 0.00 | 1 | 0 |  |  |  |  |

> **`01_물리학Ⅰ_문제지`, `2025학년도-수능-국어-언어와매체-홀수형`, `2026학년도-9월-모평-국어-언어와매체`, `2026학년도-수능-국어-언어와매체-홀수형`, `earth_input`, `english_2020suneung_go3_20191107`, `english_go2_hakpyeong_20260324`, `math_2026suneung_9wolmopyeong_20250903`, `math_go3_hakpyeong_20240328`, `physics_input`, `social_saengwoon_2020suneung_20191015`, `social_saengwoon_2025suneung_9wolmopyeong_20240904`, `전자기_교재문제`: 13 truth-backed case(s) above.** `q_recall`/`q_prec`/`p_recall`/`p_prec` come from an independently read question/passage list (the label's `ground_truth`), but `mean_iou`/`low_iou` are still scored against the oracle's own boxes -- see `ground_truth`, `verified_by`, and the `docs/web-trial-quality.md` 라벨 형식 section.

> **AI page repair produced no evidence of a real change for 13 of 13 case(s): `01_물리학Ⅰ_문제지`, `2025학년도-수능-국어-언어와매체-홀수형`, `2026학년도-9월-모평-국어-언어와매체`, `2026학년도-수능-국어-언어와매체-홀수형`, `earth_input`, `english_2020suneung_go3_20191107`, `english_go2_hakpyeong_20260324`, `math_2026suneung_9wolmopyeong_20250903`, `math_go3_hakpyeong_20240328`, `physics_input`, `social_saengwoon_2020suneung_20191015`, `social_saengwoon_2025suneung_9wolmopyeong_20240904`, `전자기_교재문제`.** On those cases the forced-AI oracle's block types, problem grouping, titles, crop boxes and review flags all came out identical to what the local baseline produced on its own, so those rows' scores show agreement with the trial's own local baseline, not confirmation by AI-grade recognition -- see `ai_evidence` and rerun scripts/trial_bench/oracle.py to refresh.
<!-- /corpus-table -->

### 게이트 판정 (설계 §3 "정확성")

설계 §3의 기준은 "확정 라벨 기준 문항 recall·precision 0.95 이상, 지문 범위 recall 0.9 이상, 문항 박스 IoU 0.8 미만 비율 0.1 이하, 확인 필요 비율 0.2 이하"다. 집계 행은 케이스별 값의 평균(빈 칸 제외)이므로, 문항·범위 단위로 합산한 값도 같이 적는다. 두 방식이 갈리는 지표는 없다.

| §3 기준 | 집계 행(케이스 평균) | 단위 합산 | 판정 |
|---|---|---|---|
| 문항 recall 0.95 이상 | 1.00 | 207/207 = 1.000 | **통과** |
| 문항 precision 0.95 이상 | 1.00 | 207/207 = 1.000 | **통과** |
| 지문 범위 recall 0.9 이상 | 0.80 | 8/9 = 0.889 | **미달** |
| 박스 IoU 0.8 미만 비율 0.1 이하 | 0/215 = 0.00 | 같음 | **측정된 적 없음**(아래) |
| 확인 필요 비율 0.2 이하 | 0.00 | 0/215 = 0.000 | 통과, 단 아래 단서 |

- **지문 범위 recall이 유일한 실제 미달**이다. 케이스 평균 0.80, 범위 단위로도 9개 중 8개(0.889)로 두 방식 모두 0.9에 못 미친다. 원인은 단 한 건 -- `english_2020suneung_go3_20191107`의 `p16-17` 누락(아래 오류 유형 1).
- **판단이 필요한 지점: `p16-17`을 지문 범위로 셀지 여부.** 이 원인 유일 건의 판단 근거는 라벨 파일(`~/edb-trial-bench/labels/english_2020suneung_go3_20191107.json`)의 `ground_truth.note`에만 적혀 있었고, 이 문서에는 지금까지 옮겨진 적이 없었다: 문제의 `[16~17] 다음을 듣고, 물음에 답하시오.`는 대괄호로 범위를 표시한 헤더이지만, 그것이 다스리는 것은 듣기 스크립트이고 트리밍된 지면에는 본문이 인쇄되어 있지 않다. 그 노트는 "본문 없는 대괄호 헤더에 대해 지문 단위를 아예 만들지 않는 파서도 defensible하다"고 명시하며, 실제로 이 라벨에서 `[16, 17]` 항목 하나만 빼면 지문 범위 recall이 0.889 → 1.00으로 올라 위 표에서 수치 미달로 남는 행이 하나도 없어진다 -- 다만 박스 IoU 행은 그래도 **측정된 적 없음**으로 남고(아래), 확인 필요 행에도 아래 단서(0.00이 좋은 신호는 아니라는 것)가 그대로 붙는다. **이 프로젝트는 그 대안을 채택하지 않는다.** 대괄호 범위 헤더는 본문 유무와 무관하게 항상 지문 단위로 기록한다는 기존 관례를 그대로 유지하며, 그래서 위 표의 지문 범위 recall 행은 **미달**로 남는다. 이유는 둘이다: **(1) 같은 헤더 형식이 이미 정확히 처리된 선례가 코퍼스 안에 있다** -- `english_go2_hakpyeong_20260324`에서 체험판 스스로 같은 `[16~17]` 헤더를 `p16-17`로 정확히 잡아냈으므로, 같은 형식을 한 케이스에서는 필수로 요구하고 다른 케이스에서는 선택으로 두면 recall 기준 자체가 케이스마다 달라져 버린다. **(2) 관찰된 실패 양상이 그 defensible한 대안과 다르다** -- 아래 "발견된 오류 유형" 절 1번이 기록하듯, 체험판이 이 헤더를 놓친 원인은 헤더가 앞 문항 단위로 흡수된 버그이지, "본문 없는 헤더는 지문 단위를 만들지 않는다"는 의도된 설계가 아니다. 의도된 설계가 아닌 결과를 defensible한 설계 선택으로 취급해 라벨을 고치는 것은 게이트를 통과시키기 위해 정답을 맞추는 것과 다르지 않다. 이 판단이 나중에 바뀌면(예: 본문 없는 대괄호 헤더를 지문 단위로 세지 않기로 프로젝트가 명시적으로 결정하면) 라벨의 `ground_truth.passage_ranges`에서 `[16, 17]`을 빼고 이 문서 맨 위의 "갱신" 명령으로 이 문서를 다시 생성해야 하며, 그 전까지 이 미달은 실제 결함이지 라벨링 관례의 부작용이 아니다.
- **박스 IoU 두 열은 통과가 아니라 미측정이다.** `mean_iou`/`low_iou`는 독립적으로 읽은 정답(`ground_truth`)이 아니라 **오라클의 박스**와 비교한 값인데(`ground_truth`는 번호·범위 목록일 뿐 박스가 없다), 이번 코퍼스에서는 오라클 관측값의 키 집합이 13개 케이스 전부 체험판과 동일하고 짝지은 키의 `regions`가 **바이트 단위로 같다**. 그래서 채점된 215쌍의 IoU가 전부 정확히 `1.0`이다 -- 근사적으로 1에 가까운 것이 아니라 같은 값끼리 비교한 결과다. 즉 이 두 열은 "박스가 정확하다"는 증거가 아니라 "박스를 검증할 독립적인 기준이 아직 없다"는 표시다. 확인 명령:
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
  → `215 215`.
- **확인 필요 비율 0.00은 통과이되 좋은 신호가 아니다.** 체험판은 215개 단위 중 단 하나에도 "확인 필요" 배지를 달지 않았다. 아래 "발견된 오류 유형"이 기록하는 남은 결함 1건도 그 안에 있다. 즉 이 게이트는 "오탐이 적다"는 뜻일 뿐이고, 배지가 그 결함을 잡지 못했다는 사실은 이 숫자에 드러나지 않는다.

### 발견된 오류 유형

**2026-09-17 갱신: 아래 "없는 문항 생성" 결함은 고쳐졌다.** 이 절을 처음 쓴 시점의 측정은 체험판이 `ground_truth`(Claude가 독립적으로 읽은 정답)와 어긋난 곳 5건(전부 영어 2개 케이스)을 기록했다. 그중 4건(오류 유형 2번, "없는 문항 생성")은 같은 날 `segment.py`의 `_indented_nested_enumeration_marker_ids`에 세 번째 신호 -- 답지 마커(①-⑤)가 지워진 마커 묶음과 다음 문항 사이(또는 단 끝까지)에 실제로 나타나는지 -- 를 요구하도록 고쳐서 해소했다(들여쓰기 + 역행하는 번호라는 기존 두 신호만으로는, 들여쓴 목록이 아니라 진짜로 번호를 다시 시작하는 절이어도 지워질 수 있었다). 이 문서의 표·본문 숫자는 그 수정 이후 다시 실행한 결과다: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/observe.py`, `scripts/trial_bench/oracle.py`(`english_go2_hakpyeong_20260324`만 재실행 -- 오라클 관측값이 수정 전 값으로 남아 있으면 박스가 바뀌었다는 허위 신호가 나온다), 그다음 `scripts/trial_bench/score.py --doc`. 남은 것은 1건(지문 범위 누락)이고, 나머지 11개 케이스(국어 3 · 과학 4 · 수학 2 · 사회 2)는 여전히 문항·범위가 정답과 완전히 일치한다.

| 오류 유형 | 건수 | 어디서 | 어느 쪽이 옳은가 |
|---|---|---|---|
| 지문 범위 누락 | 1 | `english_2020suneung_go3_20191107` `p16-17` | **정답이 옳고 체험판이 틀렸다** |
| 없는 문항 생성 | 0 (수정 전 4건 -- 아래 2번) | -- | **고쳐짐** |
| 문항 누락 | 0 | -- | -- |
| 박스 경계 오류 | 0건 관측, 단 위 항목대로 **측정된 적 없음** | -- | -- |
| 쪽 넘김 병합 | 0 | -- | -- |

1. **지문 범위 누락 (1건, 현재도 남아 있음)** -- `english_2020suneung_go3_20191107` 2쪽 왼쪽 단, 15번과 16번 사이에 `[16~17] 다음을 듣고, 물음에 답하시오.`가 분명히 인쇄돼 있다(트리밍된 입력 `inputs/english_2020suneung_go3_20191107.pdf` 2쪽을 렌더링해 직접 확인). 체험판은 이 케이스에서 지문 단위를 **하나도** 만들지 않았다(관측값의 키 28개가 전부 `q1`~`q28`). 같은 시험 형식인 `english_go2_hakpyeong_20260324`에서는 같은 `[16~17]` 헤더를 `p16-17`로 정확히 잡았으므로, 형식을 못 읽는 것이 아니라 이 한 쪽에서 헤더가 앞 문항 단위로 흡수된 것이다. 그 결과 이 케이스의 `p_recall`은 0.00이고, `p_prec`는 체험판 지문 단위가 0개라 분모가 없어 빈 칸이다.
2. **없는 문항 생성 (수정 전 4건, 이제 0건)** -- `english_go2_hakpyeong_20260324` 4쪽 28번 문항의 `Library of Things` 안내문 상자 안에 `How It Works` 번호 목록 `1.` `2.` `3.` `4.`가 들어 있는데, 체험판이 이 네 줄을 최상위 문항으로 승격시켰다(4쪽 실제 문항은 25~28번뿐인데 1~4번 키가 다시 생겨 `q1#2`~`q4#2`로 충돌 회피됐었다). 오류 지점은 **한 곳**(안내문 목록 하나)이지만 위양성 키는 4개였다. 수정 전 이 케이스의 `q_prec`는 28/32 = 0.88이었고 `q_recall`은 1.00이었다 -- 놓친 문항은 없고 없는 문항을 만든 쪽이었다. 위 갱신 안내대로 답지 마커 신호를 추가한 뒤에는 이 네 줄이 28번 문항의 크롭으로 흡수되어 더는 별도 문항이 되지 않고, 이 케이스의 `q_prec`도 1.00이다.
3. **쪽 넘김 병합 0건**은 구조적으로 확인했다: 215개 단위 중 `regions`가 두 쪽 이상에 걸친 것이 하나도 없다(`{r['page_index'] for r in p['regions']}`의 크기가 전부 1).

### 오라클이 기여한 것과 하지 않은 것

- **기여한 것: 없다.** `adjudicate.py`는 13개 케이스 전부 disagreement **0건**을 보고했고(`~/edb-trial-bench/adjudication/` 디렉터리 자체가 만들어지지 않는다), 따라서 확인용 판정 이미지도 0장이다. 그럴 수밖에 없는 것이, 오라클 관측값은 13개 케이스 전부 키 집합도 박스도 체험판과 동일하기 때문이다.
- 그래서 오라클은 위 오류 유형 절이 기록한 5건 중 **단 하나도** 잡아내지 못했다. `p16-17`은 오라클도 똑같이 놓쳤고, (수정되기 전에는) `q1#2`~`q4#2`도 오라클이 똑같이 만들어냈다. 5건을 찾아낸 것은 전적으로 Claude가 트리밍된 입력을 직접 읽어 만든 `ground_truth`다.
- 근본 원인은 이전 절들이 기록한 그대로다: 강제 Gemini 페이지 보정이 13개 케이스 35쪽 전부에서 `repair_applied`는 됐지만 `repair_changed = 0`이었다(표의 `ai_evidence` 열이 13행 모두 `(NO EVIDENCE)`). 보정이 아무것도 바꾸지 않으니 오라클 출력은 정의상 체험판 출력과 같아지고, 오라클을 정답으로 쓰는 한 점수는 항상 1.00이 된다. 이번 재채점 이전의 `pending` 표가 전부 1.00이었던 것은 품질의 증거가 아니라 그 항등식이었다.

### 남아 있는 공백

- **영어**: 유일하게 오류가 남아 있는 과목이다(지문 누락 1건, `english_2020suneung_go3_20191107`). 문항 위양성이던 다른 한 건(`english_go2_hakpyeong_20260324`)은 위 "발견된 오류 유형" 2번대로 고쳐져 `q_prec`가 두 케이스 모두 1.00이다. 케이스도 2개뿐이라 과목 단위 수치(`q_prec` 1.00, `p_recall` 0.500)의 표본이 가장 얇다.
- **지문 범위를 가진 과목이 국어·영어뿐이다.** 지문 범위 9개는 국어 7개 + 영어 2개이고, 과학·수학·사회 8개 케이스에는 지문 범위가 하나도 없어 `p_recall`/`p_prec`가 빈 칸이다. 즉 §3의 지문 범위 게이트는 13개 케이스가 아니라 5개 케이스·9개 범위 위에서만 판정된다.
- **박스 정확도는 어느 과목에서도 검증되지 않았다**(위 게이트 판정의 IoU 항목). `ground_truth`에 박스가 없고 오라클 박스는 체험판 박스와 동일하므로, 박스에 대한 독립적인 기준은 코퍼스 전체에 0개다.
- **트리밍 쪽수 불일치**: 7개 케이스가 아직 앞 3쪽이고 원본이 `sources/`에 없다(위 결과 절). 그중 과학 3개는 원본 4쪽 중 한 쪽이 측정에서 통째로 빠져 있다.
- **재현성 점검은 여전히 범위 밖이다**: 계획서 §13이 요구한 "같은 케이스를 두 번 돌려 문항 집합이 다른지"는 이번에도 하지 않았다(각 케이스 1회 관측).

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
- **판정(adjudicate) 결과**: 오라클이 성공한 11개 케이스 모두 disagreement 0건, 생성된 조정용 이미지도 0장(`~/edb-trial-bench/adjudication/` 디렉터리 자체가 만들어지지 않았다). 영어 2개를 고쳐 넣은 뒤인 2026-09-17 재실행에서도 13개 케이스 전부 disagreement 0건으로 같았다.
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
