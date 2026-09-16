# 웹 체험판 정확성 코퍼스 결과

- 설계: `docs/superpowers/specs/2026-09-15-web-trial-quality-speed-load-design.md` §5-1
- 코퍼스 위치: 저장소 밖 `~/edb-trial-bench/` (시험지·관측 JSON·라벨은 커밋하지 않는다)
- 갱신: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/score.py --doc docs/web-trial-quality.md`

## 지표

| 열 | 뜻 |
|---|---|
| `q_recall` / `q_prec` | 정답 문항 번호 중 체험판이 찾은 비율 / 체험판 문항 중 정답에 있는 비율 |
| `p_recall` / `p_prec` | 지문 범위(예: 1~3) 기준 같은 비율 |
| `mean_iou` / `low_iou` | 짝지은 문항의 박스 IoU 평균 / 0.8 미만 개수 (짝지은 것이 하나도 없으면 빈 칸) |
| `missing` / `extra` | 정답에는 있는데 체험판에 없는 키 / 체험판에는 있는데 정답에 없는 키. 번호도 지문 범위도 아닌 체험판 항목(`t:<제목>` 형태의 미분류 단위)은 항상 위양성으로 `extra`에 포함되고, 집계 행에는 케이스별 개수의 합으로 나온다 |
| `review` | "확인 필요" 배지 비율 |
| `status` | `approved`는 Fable이 판정한 라벨 기준, `pending`은 오라클을 임시 정답으로 |
| `ai_evidence` | 이 케이스의 오라클 실행에서 AI 페이지 보정 결과가 로컬 기준선과 실제로 달라진 쪽수/전체 쪽수(`oracle.page_repair.pages_changed`). 블록 분류, 문항 묶음, 제목(`display_title`), 크롭 박스(`bbox_px`), 확인 필요 플래그(`review_flags`) 중 하나라도 달라지면 그 쪽은 바뀐 것으로 센다. 0쪽이면 `(NO EVIDENCE)`가 붙고, 오라클 관측값 자체가 없으면 `no records`, 그보다 오래된(열이 없는) 관측값이면 `?`로 표시된다 -- 이 셋 중 어느 쪽도 "체험판이 AI급 인식과 일치했다"는 증거가 아니다 |

분모가 0인 지표(예: 정답에 지문이 하나도 없을 때의 `p_recall`)는 1.00이 아니라 빈 칸으로 나온다. 집계(`합계`) 행의 비율 열은 빈 칸을 제외한 케이스들의 평균이다.

## 결과

아래는 **앞 3쪽 설정**으로 측정한 과거 기준값이다. 현재 체험판은 앞 4쪽이며, 네 번째 쪽까지 포함한 품질 보증으로 해석하지 않는다. 재측정할 때 기존 라벨·관측값과 섞이지 않도록 별도 `TRIAL_BENCH_ROOT`를 사용한다.

> 참고: 아래 표는 오라클에 `ai_evidence` 열(AI 페이지 보정 결과가 로컬 기준선과 실제로 달라진 쪽수)이 추가되기 전에 측정한 스냅샷이라 그 열이 비어 있다. `scripts/trial_bench/score.py`를 다시 돌리면 각 행에 `ai_evidence` 값이 채워지고, 근거가 없는 케이스가 있으면 표 아래에 각주가 자동으로 붙는다.

<!-- corpus-table -->
측정일 2026-09-16 · 라벨 없는 케이스는 오라클을 임시 정답으로 채점(pending)

| case | status | q_recall | q_prec | p_recall | p_prec | mean_iou | low_iou | review | missing | extra | trial_ms | oracle_ms | ai_evidence |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01_물리학Ⅰ_문제지 | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1218 | 7971 | 0/3 (NO EVIDENCE) |
| 2025학년도-수능-국어-언어와매체-홀수형 | pending | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1405 | 12856 | 0/3 (NO EVIDENCE) |
| 2026학년도-9월-모평-국어-언어와매체 | pending | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1267 | 8267 | 0/3 (NO EVIDENCE) |
| 2026학년도-수능-국어-언어와매체-홀수형 | pending | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 |  |  | 1369 | 8386 | 0/3 (NO EVIDENCE) |
| earth_input | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1219 | 6711 | 0/3 (NO EVIDENCE) |
| math_2026suneung_9wolmopyeong_20250903 | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 739 | 11783 | 0/4 (NO EVIDENCE) |
| math_go3_hakpyeong_20240328 | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 636 | 10122 | 0/4 (NO EVIDENCE) |
| physics_input | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1170 | 7385 | 0/3 (NO EVIDENCE) |
| social_saengwoon_2020suneung_20191015 | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1335 | 16030 | 0/4 (NO EVIDENCE) |
| social_saengwoon_2025suneung_9wolmopyeong_20240904 | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 1507 | 16190 | 0/4 (NO EVIDENCE) |
| 전자기_교재문제 | pending | 1.00 | 1.00 |  |  | 1.00 | 0 | 0.00 |  |  | 865 | 10065 | 0/3 (NO EVIDENCE) |
| 합계 | 0/11 approved | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0 | 0.00 | 0 | 0 |  |  |  |

> **AI page repair produced no evidence of a real change for 11 of 11 case(s): `01_물리학Ⅰ_문제지`, `2025학년도-수능-국어-언어와매체-홀수형`, `2026학년도-9월-모평-국어-언어와매체`, `2026학년도-수능-국어-언어와매체-홀수형`, `earth_input`, `math_2026suneung_9wolmopyeong_20250903`, `math_go3_hakpyeong_20240328`, `physics_input`, `social_saengwoon_2020suneung_20191015`, `social_saengwoon_2025suneung_9wolmopyeong_20240904`, `전자기_교재문제`.** On those cases the forced-AI oracle's block types, problem grouping, titles, crop boxes and review flags all came out identical to what the local baseline produced on its own, so those rows' scores show agreement with the trial's own local baseline, not confirmation by AI-grade recognition -- see `ai_evidence` and rerun scripts/trial_bench/oracle.py to refresh.
<!-- /corpus-table -->

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

  영어 2개 케이스는 이 표를 처음 만들었을 때는 오라클이 페이지 하나도 처리하지 못하고 실패했다(`Gemini response JSON decode failed: Unterminated string`, 각각 1회 재시도에도 동일하게 재현되어 지속 실패로 기록). 원인을 진단하고 고친 뒤 다시 돌린 결과는 아래 "오라클 영어 지원 수정" 항목에 있다 -- 이 표의 두 행과 위 판정·해석 문장(11개 케이스 기준)은 그 진단 이전 스냅샷이며, `score.py`/`adjudicate.py`는 영어 케이스에 대해 이 문서 갱신 시점까지 아직 다시 돌리지 않았다.
- **판정(adjudicate) 결과**: 오라클이 성공한 11개 케이스 모두 disagreement 0건, 생성된 조정용 이미지도 0장(`~/edb-trial-bench/adjudication/` 디렉터리 자체가 만들어지지 않았다).
- **이 숫자가 증명하는 것과 증명하지 않는 것**: 11개 케이스에서 체험판과 오라클이 완전히 일치한 것은, 오라클의 강제 AI 보정이 로컬 기준선을 단 한 쪽도 바꾸지 않았기 때문이다(`ai_evidence` 열, 위 표의 `repair_changed`). 즉 이 일치는 "체험판이 AI급 인식과 같다"는 근거가 아니라 "이번 코퍼스에서는 AI가 로컬 파서와 다른 답을 내지 않았다"는 근거일 뿐이다. `low_iou`/`review`/`missing`/`extra`가 전부 0인 것, adjudication 이미지가 0장인 것도 같은 이유로 독립적인 정답과의 일치를 뜻하지 않는다. 영어 과목은 (아래 수정 전까지는) 오라클 자체가 실패했으므로 일치·불일치 어느 쪽도 말할 수 없었고, 계획서(§13)가 요구한 "같은 케이스를 두 번 돌려 문항 집합이 다른지" 재현성 점검도 이번 실행 범위 밖이다(1회씩만 실행).

## 2026-09-16 추가: 오라클 영어 지원 수정

- **원인**: `page_repair.py`의 `_repair_output_token_budget`은 강제 오라클 경로에서도 블록당 24토큰짜리 어림값(`512 + 24*block_count`)으로 실제 Gemini 호출의 `maxOutputTokens`를 계산한다. `english_2020suneung_go3_20191107`의 실패를 계측해 다시 돌려 보니(`page_repair.GeminiRepairTruncatedError`가 새로 기록하는 진단 정보) `block_count=11`, `include_problem_units=False`일 때 어림값이 776까지만 나왔다 -- `force_config`가 설정한 4096은커녕 2048 하드 캡보다도 훨씬 작다. 응답은 `finishReason=MAX_TOKENS`로 `display_titles` 배열 도중 블록 id 문자열이 끊긴 채로 잘렸다(`~/edb-trial-bench/oracle_failures/english_2020suneung_go3_20191107.json`에 그 진단 기록이 남아 있다). 24토큰/블록 어림값은 `"block-1"` 같은 짧은 테스트용 id를 기준으로 잡힌 값이라, 실제 페이지 id(`english_2020suneung_go3_20191107-page-001-block-011`, 50자 이상, 블록마다 `problem_start_block_ids`와 `display_titles` 두 곳에 등장)의 길이를 전혀 반영하지 못한다.
- **수정**: `page_repair.AIFallbackConfig`에 `max_output_token_cap`(기본값 `None`)을 추가해, 값이 주어지면 블록당 어림값·2048/3072 하드 캡을 모두 건너뛰고 `min(configured_max_tokens, max_output_token_cap)`만 쓴다. 데스크톱 쪽 `ai_fallback_config` 딕셔너리(`build_problem_board_edb.py`의 `_build_ai_fallback_config`)는 이 키를 절대 만들지 않으므로 데스크톱 동작은 그대로다. `scripts/trial_bench/oracle.py`의 `force_config`만 `max_tokens=8192`·`max_output_token_cap=8192`를 설정한다. 또한 JSON 디코드가 실패했을 때 `finishReason`이 `MAX_TOKENS`/`LENGTH`이면 평범한 `RuntimeError` 대신 `page_repair.GeminiRepairTruncatedError`(모델명·finishReason·유효/설정 토큰 한도·프롬프트·응답 바이트 수·응답 앞뒤 200자를 담은 `diagnostics`)를 던지도록 바꿨다. `_request_ai_repair_with_retry`는 이 오류를 **`config.max_output_token_cap`이 설정된 호출(즉 오라클의 `force_config`)에서만** 같은 모델로 재시도하지 않는다 -- 근거는 `temperature=0.0`이라서가 아니다(`FALLBACK_GEMINI_REPAIR_MODEL`인 `gemini-3.6-flash`는 `_request_gemini_repair`가 실제로 `temperature`를 지우므로 동일 요청이라도 결정적이지 않다). 수정 전 오라클 실패 기록(`~/edb-trial-bench/oracle_failures/english_go2_hakpyeong_20260324.json`)이 "AI repair failed after retries: ... Unterminated string"로 남아 있어, 같은 모델로 한 번 재시도한 결과가 실제로 똑같이 잘렸다는 것을 직접 보여준다. 데스크톱 쪽 호출은 `max_output_token_cap`을 절대 설정하지 않으므로 이 재시도 건너뛰기는 적용되지 않고 -- 절단을 일으키는 블록당 어림값 자체는 데스크톱에도 그대로 남아 있으므로 -- 데스크톱은 절단이 나면 여전히 재시도한다(수정 전과 동일하게 시도 2회).
- **재현 명령과 결과**: `.venv/bin/python scripts/trial_bench/oracle.py --runtime-dir /Users/clmagi/Desktop/Projects/edb_mak/.app_runtime english_2020suneung_go3_20191107`와 같은 방식으로 `english_go2_hakpyeong_20260324`도 실행. 두 케이스 모두 이제 4쪽 전부 오라클 관측값을 만든다:

  | case | oracle 쪽수 | repair_attempted | repair_changed | repair_applied | repair_model |
  |---|---|---|---|---|---|
  | english_2020suneung_go3_20191107 | 4 | 4/4 | 0/4 (NO AI EVIDENCE) | 4/4 | gemini-3.1-pro-preview |
  | english_go2_hakpyeong_20260324 | 4 | 4/4 | 0/4 (NO AI EVIDENCE) | 4/4 | gemini-3.1-pro-preview |

  두 케이스 모두 기본(primary) 모델(`gemini-3.1-pro-preview`)에서 바로 성공했고 `gemini-3.6-flash`로 폴백하지 않았으며, 오류는 0건이다. `repair_changed=0/4`는 위 11개 케이스와 같은 기존 이슈(오라클의 강제 AI 보정이 로컬 기준선과 실제로 다른 답을 낸 쪽이 없음)이지 이번 수정이 만든 새 문제가 아니다 -- 이번 수정의 범위는 "오라클이 영어 페이지를 끝까지 처리하는가"였지 "AI가 실제로 무언가를 고치는가"가 아니다. `score.py`/`adjudicate.py`로 영어 케이스를 위 표에 반영하는 작업은 아직 하지 않았다.
