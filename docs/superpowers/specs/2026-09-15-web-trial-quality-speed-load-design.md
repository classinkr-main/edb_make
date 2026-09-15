# 웹 체험판 품질·속도·과부하 개선 설계

- 작성일: 2026-09-15
- 브랜치: `web-trial` (기준 커밋 cf8bdec, Plan 1·2 구현 완료 상태)
- 상태: 설계 승인됨 (2026-09-15). 다음은 구현 계획 작성
- 선행 문서: `docs/superpowers/specs/2026-09-15-problem-parser-web-trial-design.md`, `docs/web-trial-spike-results.md`, `docs/web-trial-operations.md`

## 1. 결정 사항

| 항목 | 결정 |
|---|---|
| 접근 | **측정 우선.** 정확성·속도 기준값을 먼저 만들고, 모든 변경은 그 숫자로 전후를 확인한다 |
| 1급 목표 | **과부하·리소스 분배.** 사용자 결정(2026-09-15): 부하 상황에서 자원 분배가 괜찮으면 한 건 15초까지 허용 |
| 속도 목표 | 웜 p50 10초 이하(국어 3쪽), 동시 20건 부하에서 p95 15초 이하 |
| 정확성 기준값 | **데스크톱 앱 AI 인식 결과를 오라클**로 쓰고, 어긋난 문항만 Fable이 crop 이미지를 보고 판정해 정답을 확정한다 |
| 속도 레버 | 체험판 전용 모듈만 바꾸는 L3·L5·L6은 기본 포함. 공용 파이프라인을 건드리는 L1·L2와 실험 L4는 **부하 p95가 15초를 넘을 때만** |
| 서브에이전트 | 높은 판단(설계·불일치 판정·공용 코드 리뷰)은 Fable, 공용 파이프라인 변경은 Opus, 체험판 모듈·스크립트·테스트는 Sonnet, 벤치마크 실행·수치 정리·문서는 Haiku |
| 제외 | 스트리밍 응답, Performance CPU 기본 채택, AI·스캔·이미지 입력, 계정·저장 |

## 2. 확인한 사실 (2026-09-15)

### 2-1. 배포가 막힌 원인 두 가지

- 공개 주소 `https://edb-parser-trial.vercel.app`의 `/api/health`가 `ready: false`. `/api/config`의 `turnstile_site_key`가 `null`. 운영 환경변수 6개가 비어 있어 서버가 설계대로 모든 파싱을 503 `busy`로 막는다.
- Supabase 프로젝트는 살아 있으나 `trial_quota`·`trial_events` 테이블과 `trial_consume` 함수가 없다(PostgREST `PGRST205`/`PGRST202`, 존재하지 않는 이름과 같은 응답). 마이그레이션이 실행되지 않았다.
- 사용자가 테스트한 `…46s20dd4n…` 주소는 배포별 주소라 Vercel 로그인 보호(302) 뒤에 있다. 방문자 테스트는 공개 주소에서 한다.
- 둘 다 대시보드 작업이다. 코드 문제는 없다(배포 커밋은 최신).

### 2-2. 속도 실측

- Vercel(`docs/web-trial-spike-results.md` §2): 국어 3쪽 웜 6.6~7.4초(인식 3.7~4.1, crop 2.7~3.1), 과학 4쪽 9.1~9.6초. 새 인스턴스는 +5초. 한 인스턴스에 2건이 겹치면 12.5~17.3초. 최대 RSS 3쪽 515 MB, 4쪽 801 MB, 2건 겹침 790 MB.
- 로컬 M4 프로파일(국어 3쪽, 총 1.65초). 큰 항목:

| 항목 | 로컬 | 내용 |
|---|---|---|
| 페이지 렌더 PNG 왕복 | 약 0.5초 | `render_pdf_pages`가 픽스맵을 PNG로 저장(`fz_save_pixmap_as_png` 0.32초)하고 PIL이 다시 읽는다(`ImagingDecoder.decode`) |
| 표 탐지 | 0.34초 | `_extract_pdf_media_regions` → `page.find_tables()` 쪽당 0.11초 |
| crop PNG 저장·재로딩 | 약 0.3초 | `_render_problem_asset`의 `PIL.Image.save` 0.24초, `problem_parser`가 `crop_path`를 다시 연다 |
| 미리보기 축소 | 0.17초 | `encode_jpeg_data_uri`의 LANCZOS `resize` 25회 |

- Vercel은 로컬의 약 4.3배 느리므로 위 넷은 Vercel에서 약 3초에 해당한다.

### 2-3. 정확성 단서

- 과학·물리 4개 파일(앞 3쪽): 문항 16·16·16·12개, 번호 연속, 확인 필요 0건.
- 국어 3개 파일(2025 수능, 2026 9월 모평, 2026 수능, 앞 3쪽): 문항 1~9 + 지문 2~3개("지문 1~3", "지문 4~9", "지문 10~13"), 페이지 넘김 지문 병합 1건, `passage_cross_page_merge_check` 플래그 파일당 7건, 배지 기준 확인 필요 0건. 정답이 없어 맞는지 알 수 없다.
- **과목 지정 가설 기각.** 체험판은 과목을 `unknown`으로 넘기지만 `korean`으로 넘겨도 국어 3개 파일 결과가 바이트 동일했다. `segment.py`가 페이지 텍스트의 "국어 영역" 표기로 과목을 이미 추론한다.
- 기존 품질 측정 틀: `quality/corpus.schema.json`과 `scripts/evaluate_quality_corpus.py`가 문항 recall·precision, 지문 범위 recall·precision, 확인 필요 비율, 처리 시간을 잰다. 합성 케이스 2개뿐이고 박스 정확도 지표는 없다.
- 시험지 파일 위치(저장소 밖): 국어 3개 `/Users/clmagi/Desktop/Projects/omr_maker/output/pdf/`, 지구과학·물리 `tmp_test_inputs/`, `~/Downloads/파일/문제 모음집/01 물리학Ⅰ_문제지.pdf`, `~/Downloads/클래스인 다운로드 위컴/전자기 교재문제.pdf`.
- Gemini 키는 `edb_mak/.app_runtime/user_settings.json`에 있다(`user_settings.load_user_settings`가 환경변수로 동기화). 오라클 실행은 이 로더를 써서 키를 출력하지 않는다.

### 2-4. 관측·과부하 현황

- `trial_events`는 총 소요시간(`elapsed_ms`)만 기록한다. 단계별 시간, 인스턴스 구분, `busy` 사유가 없다.
- `/api/spike`는 제거됐다(cf8bdec). 운영에서 파싱 시간을 잴 경로가 없다.
- 스펙 §6의 `PIL.Image.MAX_IMAGE_PIXELS` 상한이 코드에 없다.
- 동시 요청은 10건까지만 쟀다. 인스턴스 유휴 만료 시간은 재지 않았다.
- Vercel Ignored Build Step이 `web-trial`만 빌드하므로 다른 브랜치는 프리뷰 배포가 안 된다.

## 3. 목표와 합격 기준

| 축 | 기준선 | 목표 | 재는 방법 |
|---|---|---|---|
| 과부하·분배 | 2건 겹침 12~17초, 20건 미측정 | 동시 20건에서 p95 15초 이하, 504와 JSON 아닌 500이 0건, RSS 2 GB의 60% 이하 | §6 부하 프로브·고장 주입 |
| 속도 | 국어 3쪽 웜 6.8초 | 웜 p50 10초 이하 (여유 확보용, 이미 통과) | §5 프로브 |
| 정확성 | 정답 없음 | 확정 라벨 기준 문항 recall·precision 0.95 이상, 지문 범위 recall 0.9 이상, 문항 박스 IoU 0.8 미만 비율 0.1 이하, 확인 필요 비율 0.2 이하 | §5 코퍼스 |
| 변경 안전성 | — | 속도 레버 L1·L2·L5·L6은 코퍼스에서 문항 번호·박스가 바이트 동일. L3은 미리보기 픽셀만 변경. L4는 지표 게이트 | §5 코퍼스 |
| 화면 품질 | JPEG 78, 응답 1.3~2.2 MB | 미리보기 선명도 유지 이상, 응답 3.5 MB 이하, 박스 오버레이 좌표 오차 없음 | §8 |

"웜"은 같은 인스턴스의 2회차 이후 호출, "부하 p95"는 동시 20건 한 묶음의 95번째 백분위다.

## 4. 0단계 — 배포 복구와 관측

### 4-1. 사용자 작업 (대시보드)

1. Supabase SQL Editor에서 `supabase/migrations/20260915000000_web_trial.sql` 실행. **이 설계로 파일이 바뀌므로(§4-2 열 추가) 코드 배포 전에 다시 실행한다.**
2. Vercel Production 환경변수 6개(`TRIAL_TURNSTILE_SITE_KEY`, `TRIAL_TURNSTILE_SECRET`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `TRIAL_IP_SALT`, `CRON_SECRET`)와 `TRIAL_HOSTNAMES=edb-parser-trial.vercel.app`.
3. Turnstile 위젯 Hostname에 `edb-parser-trial.vercel.app` 추가(도메인 연결 시 둘 다).
4. 재배포. 환경변수는 재배포해야 반영된다.
5. Ignored Build Step을 `web-trial`과 `web-trial-bench` 둘 다 빌드하게 바꾼다: `case "$VERCEL_GIT_COMMIT_REF" in web-trial|web-trial-bench) exit 1;; *) exit 0;; esac`
6. Deployment Protection에서 자동화 우회(Protection Bypass for Automation)를 켜고 비밀값을 받아 둔다. Preview 환경변수에 `TRIAL_DAILY_LIMIT=10000`, `TRIAL_GLOBAL_DAILY_LIMIT=10000`을 넣는다(프리뷰는 한도가 인스턴스 메모리라 프로브가 3회 만에 막히지 않게).

### 4-2. 코드

- 마이그레이션에 열 추가: `trial_events.timing jsonb`(단계별 ms), `instance_id text`(프로세스 시작 시 만든 UUID 앞 8자), `reject_detail text`(`busy`의 사유: `not_ready`·`turnstile`·`quota_store`·`global_limit`·`slot_wait`). `add column if not exists`로 재실행 가능하게.
- `problem_parser.timing_ms`에 `render`(페이지 렌더)와 `entries`(박스 계산)를 분리해 기록하고, 서버가 `encode`(미리보기 인코딩)와 `total`을 더한다.
- `/api/parse` 응답 JSON에 `timing_ms`와 `instance_id`를 넣는다. 화면은 쓰지 않고 프로브가 읽는다.
- `problem_parser` 모듈 로드 시 `PIL.Image.MAX_IMAGE_PIXELS = 40_000_000`을 설정한다(2×A3 200 DPI ≈ 15.5M px가 상한 안). 스펙 §6에 있었으나 구현되지 않은 항목이다.
- 운영 문서 §4에 SQL 추가: 단계별 p50/p95, 인스턴스별 건수(콜드 스타트 빈도), `busy` 사유별 건수.
- **배포 순서**: 마이그레이션 → 코드 푸시. 열이 없으면 PostgREST가 insert를 거부해 이벤트가 조용히 사라진다.

### 4-3. 검증

- `/api/health`가 `ready: true`, `/api/config`에 운영 site key.
- 앱 내 브라우저로 공개 주소에 실제 시험지(국어 원본 16쪽)를 올려 결과·"앞 3쪽" 배너·박스·카드 확인.
- Supabase `trial_events`에 `timing`·`instance_id`가 채워진 행 확인.

## 5. 1단계 — 측정 틀

### 5-1. 정확성 코퍼스 (저장소 밖 `~/edb-trial-bench/`)

```
~/edb-trial-bench/
  inputs/<case>.pdf            체험판이 실제로 받는 형태: 앞 3쪽을 select 후 garbage=4, deflate로 저장
  oracle/<case>.json           데스크톱 AI 인식 관측 JSON
  trial/<case>.json            체험판 parse_problems 관측 JSON
  labels/<case>.json           확정 라벨 (status: pending | approved), 문항별 truth: trial | oracle | neither
  adjudication/<case>/<n>.png  불일치 문항의 좌우 비교 이미지
  report.md                    채점 결과
```

- 케이스: 국어 3, 지구과학·물리 2, 물리학Ⅰ, 전자기 교재 = 7개로 시작. 영어·수학·사회는 평가원 공개 시험지를 사용자가 내려받아 `inputs/`에 넣거나 내려받기를 허락하면 추가한다. 학원·학교 자체 제작 PDF(HWP 내보내기)가 있으면 2~3개 추가한다. 목표 12개 이상, 과목 4개 이상.
- 관측 JSON(양쪽 공통): `{"case", "pages": n, "problems": [{"number": int|null, "title", "regions": [{"page_index", "bbox": {left, top, width, height}}], "risk_flags": [...]}], "passage_ranges": [[1,3], ...]}`. 좌표는 200 DPI 페이지 픽셀. 지문 범위는 제목 "지문 1~3"에서 뽑는다. 텍스트는 넣지 않는다.
- 오라클: `build_pages(ocr_mode="auto", ai_fallback_config=<force>, subject=<과목>)` → `build_problem_entries(render_board_assets=False)`. 체험판과 같은 하류 코드에 인식 입력만 다르다. 데스크톱 앱의 "AI 정밀 인식"과 같은 설정(`app_server.py`의 `ai_fallback="force"`)을 쓴다. 키는 `load_user_settings(<edb_mak>/.app_runtime)`로 읽는다.
- 체험판: `problem_parser.parse_problems(max_pages=3)`.
- 채점: 문항은 번호로, 지문은 범위로 짝짓는다. 기존 `evaluate_quality_corpus`의 recall·precision·확인 필요 비율에 **박스 IoU**(짝지은 문항의 페이지별 합집합 박스)를 더한다. 라벨이 `approved`인 케이스만 합격 판정에 쓰고, `pending`은 참고치로 표시한다.
- 실행: `scripts/trial_bench/make_inputs.py`, `oracle.py`, `observe.py`, `score.py`, `adjudicate.py`(비교 이미지 생성). 결과 표는 `docs/web-trial-quality.md`에 커밋한다. 시험지·관측 JSON은 커밋하지 않는다.

### 5-2. 속도 프로브 (프리뷰 배포)

- 브랜치 `web-trial-bench`를 `web-trial`에서 만들어 푸시하면 프리뷰가 뜬다. 프리뷰는 `VERCEL_ENV=preview`라 Turnstile 비밀이 없으면 봇 확인을 건너뛰고 한도는 인스턴스 메모리다. 운영에 우회 경로를 두지 않는다.
- 로그인 보호는 자동화 우회 비밀을 `x-vercel-protection-bypass` 헤더로 보내 통과한다. 헤더 이름과 설정 위치는 첫 사용 때 Vercel 문서로 확인한다.
- `scripts/trial_bench/probe.py <url> <pdf...> --repeat 5`: 파일별 5회, 2회차부터 웜. wall, 서버 `elapsed_ms`, `timing_ms` 단계, `instance_id`, 응답 크기를 표로 낸다. 결과는 `docs/web-trial-spike-results.md` §4에 붙인다.
- Performance CPU 실험(30분): 프로젝트 설정이라 운영도 바뀌므로 한가한 시간에 켜고 프로브 후 되돌린다. 결과는 기록만 하고 채택은 코드 개선 뒤 다시 판단한다.
- 콜드 스타트: 30분·2시간 유휴 뒤 첫 호출 wall을 기록한다. 첫 방문자가 12초를 넘기면 10분 간격 `/api/health` 크론 핑을 검토한다.

## 6. 3단계 — 과부하·리소스 분배 점검 (1급 목표)

| 점검 | 방법 | 기준 |
|---|---|---|
| 동시 요청 분배 | `scripts/trial_bench/load.py`로 프리뷰에 국어 3쪽을 5·10·20건 동시 업로드. 요청별 wall·status·`instance_id`·`timing_ms` 기록. `TRIAL_PARSE_CONCURRENCY` 1과 2를 각각 측정 | p95 15초 이하인 값을 운영 설정으로 채택. 1일 때 2번째 요청이 슬롯 대기 대신 새 인스턴스로 가는지 확인 |
| 대기·타임아웃 예산 | 슬롯 대기 20초 + 파싱 ≤ 10초 = 최악 30초가 `maxDuration` 60초 안인지. 대기 초과가 차감 없이 `busy`로 끝나는지 | 504 0건, 대기 초과 미차감 |
| 메모리 | 2×A3 3쪽 PDF와 과학 4쪽을 2건 겹쳐 돌려 최대 RSS. `MAX_IMAGE_PIXELS` 설정 | 2 GB의 60%(1.2 GB) 이하 |
| 고장 주입 (로컬) | `create_app`에 가짜 저장소·검증기·파서를 꽂아 Supabase 5초 지연·540, Turnstile 장애, 파서 예외, 손상 PDF, 101쪽 PDF, 슬롯 고갈 재현 | 모두 JSON 오류. 차감 규칙 유지(대기 초과·결과 불명 미차감, 파싱 시작 후 차감). `reject_detail` 기록 |
| 병리적 입력 | `inspect_pdf`가 앞 3쪽의 텍스트 스팬·드로잉 수를 세어 상한 초과면 422 `page_too_complex`(팝업 `ai`). 상한은 코퍼스의 "복잡도 대 인식 시간" 관계에서 60초를 넘길 수 없는 값으로 정한다 | 60초 초과 차단 |

- 비용 최악치: 500건 × 15초 × 2 GB를 스펙 §9 단가로 계산해 운영 문서에 적는다.
- 결과는 `docs/web-trial-load.md`에 표로 남기고 운영 설정(`TRIAL_PARSE_CONCURRENCY`, `TRIAL_PARSE_WAIT_SECONDS`)을 확정한다.

## 7. 2단계 — 속도 레버

모두 체험판 전용 스위치 뒤에 두고 데스크톱 기본값은 바꾸지 않는다. 레버 하나가 작업 하나이며 매번 코퍼스와 프로브로 전후를 잰다.

| 레버 | 내용 | 로컬 절감 | 포함 조건 | 담당 |
|---|---|---|---|---|
| L3 미리보기 생성 | 200 DPI 이미지를 LANCZOS로 줄이는 대신 PyMuPDF로 100 DPI를 한 번 더 렌더해 페이지 미리보기로 쓴다. 문항 미리보기는 crop을 `Image.reduce` 뒤 BILINEAR로 줄인다 | 0.15초 | 기본 | Sonnet |
| L5 워커 1개 | `TRIAL_WORKERS=1`이면 렌더·crop 스레드 풀을 1로. 1 vCPU 경합만 줄인다 | 소폭 | 기본 | Sonnet |
| L6 JSON 1회 직렬화 | `build_parse_payload`가 예산 확인에 쓴 직렬화 결과를 응답에 재사용 | 소폭 | 기본 | Sonnet |
| L1 페이지 렌더 메모리 경로 | `render_pdf_pages`가 픽스맵을 `Image.frombytes`로 유지. `NormalizedPageImage`에 선택적 이미지 필드, 경로를 읽는 3곳 대응. 캐시·디버그 소비자만 PNG | 0.5초 | 부하 p95 > 15초 | Opus |
| L2 crop 메모리 경로 | `_render_problem_asset`이 crop을 엔트리에 보관, `Image.open(crop_path)` 4곳이 메모리 이미지를 우선. PNG 쓰기 생략 | 0.25초 | 부하 p95 > 15초 | Opus |
| L4 표 탐지 스위치 | 텍스트 PDF 체험판에서 `find_tables` 생략 | 0.34초 | 부하 p95 > 15초이고 코퍼스 IoU 무손실 | Sonnet + Haiku |

- 합격: L1·L2·L5·L6은 코퍼스 문항 번호·박스 바이트 동일. L3은 미리보기 픽셀만 변경(응답 크기 3.5 MB 이하 유지). L4는 지표 게이트.
- 부하 p95가 15초를 넘지 않으면 L1·L2·L4는 하지 않는다. 필요해지면 그때 별도 계획으로 연다.

## 8. 4단계 — 정확성·품질 트랙

1. **불일치 판정**: `adjudicate.py`가 오라클·체험판이 어긋난 문항·지문마다 두 crop과 페이지 박스를 나란히 그린 PNG를 만든다. Fable(이 세션)이 이미지를 보고 `labels/<case>.json`에 truth를 적어 `approved`로 올린다. 오라클도 틀릴 수 있으므로 오라클을 그대로 정답으로 쓰지 않는다.
2. **오류 유형 → 수정**: 판정 결과를 유형별로 센다. 후보는 국어의 페이지 넘김 지문 병합(`passage_cross_page_merge_check` 파일당 7건), 3쪽 끝에서 잘린 문항·지문 처리, 지문 범위 표기, 번호 없는 문항. 빈도순으로만 고치고 코퍼스로 전후를 확인한다. 수정이 공용 파이프라인이면 Opus가 구현하고 Fable이 데스크톱 동일성을 검토한다.
3. **확인 필요 배지 보정**: 플래그별로 "실제 오류였던 비율"을 라벨로 재서 `REVIEW_WORTHY_FLAGS`를 데이터로 정한다.
4. **화면 품질**: 코퍼스 파일을 브라우저로 올려 박스 오버레이가 페이지 이미지와 맞는지(여백 자르기 뒤 좌표) 확인하고, 어긋나면 `regions` 좌표 변환을 고친다. 미리보기 선명도는 L3로 올린다.
5. **과목 폭**: 영어·수학·사회·자체 제작 PDF를 코퍼스에 넣고 같은 절차를 돌린다.

## 9. 실행 방식

- 계획의 작업 하나마다 Agent 도구로 모델을 지정해 보낸다(superpowers:subagent-driven-development). 클라우드 다중 에이전트(Workflow)는 쓰지 않는다.

| 모델 | 맡는 일 |
|---|---|
| Fable (이 세션) | 설계, 불일치 판정(이미지 확인), 공용 파이프라인 변경 리뷰, 부하 결과 판단, 최종 승인 |
| Opus | 공용 파이프라인 변경(조건부 L1·L2, 정확성 수정)과 그 코드 리뷰 |
| Sonnet | 체험판 모듈·마이그레이션·프로브·부하·고장 주입·오라클 어댑터·테스트 |
| Haiku | 벤치마크·프로브 실행, 수치 표 정리, 문서 갱신 |

- 독립 작업은 동시에 보낸다: 프로브 제작 ∥ 고장 주입 테스트 ∥ 오라클 어댑터.
- 같은 워크트리를 다른 세션이 쓴다. 작은 커밋, 파일 지정 `git add`, `git add -A`·stash·reset 금지. 커밋 전 `git status`로 남의 변경을 확인한다.
- 시험지 원본·관측 JSON·라벨은 저장소 밖에 둔다. 커밋하는 것은 스크립트, 테스트, 결과 표뿐이다.

## 10. 테스트

| 대상 | 방법 |
|---|---|
| 이벤트 열 | TestClient + `MemoryQuotaStore`로 `timing`·`instance_id`·`reject_detail`이 기록되는지, `busy` 사유별로 값이 다른지 |
| 응답 | `timing_ms`·`instance_id` 필드, 기존 필드 불변 |
| 복잡도 거절 | 스팬을 대량 넣은 합성 PDF가 422 `page_too_complex`, 정상 PDF는 통과 |
| `MAX_IMAGE_PIXELS` | 모듈 로드 후 값 확인 |
| L3 | 페이지 미리보기 크기·비율, 응답 예산 단계 결정성 유지 |
| L5·L6 | 결과 바이트 동일 |
| 고장 주입 | §6의 7가지 케이스를 `test_trial_api.py`에 추가 |
| 코퍼스 회귀 | `scripts/trial_bench/score.py`를 로컬에서 실행, `approved` 케이스가 §3 기준을 넘는지. CI에는 넣지 않는다(입력이 사적) |
| 기존 | 전체 `pytest` 통과(데스크톱 기본값 불변) |

## 11. 산출물

| 파일 | 상태 | 책임 |
|---|---|---|
| `supabase/migrations/20260915000000_web_trial.sql` | 수정 | `timing`·`instance_id`·`reject_detail` 열 |
| `trial_server.py` | 수정 | 단계 시간·인스턴스·사유 기록, `MAX_IMAGE_PIXELS`, 복잡도 검사 호출, 응답 필드 |
| `problem_parser.py` | 수정 | `render`·`entries` 시간 분리, `inspect_pdf` 복잡도, 100 DPI 미리보기 렌더 |
| `trial_preview.py` | 수정 | L3, L6 |
| `trial_config.py`, `trial_input.py`, `trial_quota.py` | 수정 | `TRIAL_WORKERS`, `page_too_complex`, 이벤트 열 |
| `scripts/trial_bench/{make_inputs,oracle,observe,score,adjudicate,probe,load}.py` | 신규 | 측정 틀 |
| `docs/web-trial-quality.md`, `docs/web-trial-load.md` | 신규 | 결과 표 |
| `docs/web-trial-operations.md`, `docs/web-trial-spike-results.md` | 수정 | 프리뷰·우회 비밀·새 환경변수·SQL, 프로브 결과 §4 |
| `test_trial_*.py`, `test_problem_parser.py` | 수정 | §10 |
| `preprocess.py`, `build_problem_board_edb.py` | 조건부 | L1·L2·L4, 정확성 수정 |

## 12. 계획 분할

- **Plan 3 (측정)**: §4 0단계 코드·검증, §5 코퍼스 7개와 프로브, §6 부하·고장 주입, §7의 L3·L5·L6. 끝나면 부하 p95와 코퍼스 기준값이 나온다.
- **Plan 4 (개선)**: §8 판정 결과에 따른 정확성 수정, 배지 보정, 과목 확장, 조건부 L1·L2·L4. Plan 3의 숫자를 보고 쓴다.

## 13. 위험

- 오라클 비용·비결정성: Gemini 호출은 케이스당 3쪽이라 작다. 결과가 흔들리면 같은 케이스를 2회 돌려 일치하는 것만 쓴다.
- 프리뷰 우회 비밀·Ignored Build Step 변경은 사용자 대시보드 작업이라 그 전까지 프로브·부하 측정이 막힌다. 그동안 로컬 고장 주입과 코퍼스 작업을 먼저 한다.
- 프리뷰 인스턴스는 운영과 같은 함수 설정(Standard 1 vCPU)이라 수치는 옮겨 쓸 수 있지만, 콜드 스타트 예방 대기 인스턴스 수는 다를 수 있다. 콜드 수치는 운영 `trial_events`의 `instance_id`로 다시 본다.
- 공용 파이프라인 변경(L1·L2·정확성 수정)은 데스크톱에 영향을 줄 수 있다. 기본값 유지 + 전체 테스트 + Fable 리뷰로 막는다.
- 코퍼스가 작다(7개). 과목 확장 전까지 결론을 일반화하지 않는다.
