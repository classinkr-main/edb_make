# 문제 파서 웹 체험판 설계

- 작성일: 2026-09-15
- 브랜치: `web-trial` (베이스 `up3_mac` @ 5ff735b)
- 상태: 초안 — 사용자 검토 대기
- 선행 문서: `docs/problem-parser-web-trial.md` (2026-09-14, 방향·용량 검토)

## 1. 결정 사항

| 항목 | 결정 |
|---|---|
| 제품 역할 | **웹 = 체험판, 설치형 앱 = 본품.** 웹은 앱 도입 문의로 유도하는 창구다 |
| 대상 | 누구나. **가입 없음** |
| 체험 범위 | 내 파일 업로드 → 문항 인식 → 페이지 위 문항 박스와 문항 미리보기 |
| 막는 기능 | EDB 내보내기, 이미지 다운로드, 문항 수정, AI 정밀 인식, 페이지·횟수 제한 초과 → **"프리미엄 기능" 팝업** |
| 팝업 버튼 | **도입 문의(상담 신청).** 설정값 `TRIAL_INQUIRY_URL`의 외부 링크(카카오 채널·폼)를 연다 |
| AI | **쓰지 않는다.** 로컬 인식 경로만 쓴다. AI는 팝업 광고 문구로만 등장한다 |
| 라이선스 | 비상업 체험판. 사용자 판단으로 이번 설계의 제약에서 뺀다 |
| 기존 `web-migration` 브랜치 | **보류.** 삭제하지 않고 Plan 1 결과를 보존한다 (§11) |

## 2. 코드에서 확인한 사실

| 항목 | 현재 상태 | 설계 함의 |
|---|---|---|
| 기본 파싱 진입점 | 앱의 기본 export 모드는 `run_problem_export()` (`build_problem_board_edb.py:11444`) | 체험판도 이 경로의 앞부분을 쓴다. 인식 품질이 앱과 같아야 체험이 의미 있다 |
| 인식 단계 | `build_pages()` (`:2604`) → 페이지별 `PageModel` | 그대로 쓴다 |
| 문항 정리 | `build_problem_entries()` (`:6010`)가 ① 문항 박스 계산(drafts) ② `_render_problem_assets()`로 이미지 생성 ③ 페이지를 넘는 지문 합치기 순서로 진행 | 박스 계산과 이미지 생성이 **이미 단계로 나뉘어 있다** |
| 이미지 생성 | `_render_problem_asset()` (`:2281`)이 문항 crop을 저장한 뒤 `_render_problem_board_asset()`로 칠판용 cutout을 **따로** 만든다 | cutout만 끄는 스위치를 넣으면 된다. 큰 추출 작업이 필요 없다 |
| 지문 합치기 | `_coalesce_cross_page_passage_drafts()`가 crop 크기를 쓰고, crop과 `board_render_path`를 **둘 다** 이어 붙인다 (`:5877~5898`) | crop은 끌 수 없다(합치기 품질에 필요). cutout 이어 붙이기만 같은 스위치로 건너뛴다 |
| 후처리 | 이후 `build_records` → 배치 요약 → EDB 쓰기 → handoff → `ui_session` | 체험판은 **전부 호출하지 않는다** |
| 웹 의존성 | `web-migration`의 `3e935da`에 FastAPI·uvicorn 해시 잠금(`requirements-web.lock`)이 있다. `python-multipart`는 없다 | 이 커밋만 가져온다. 업로드는 multipart 대신 **원본 바이트 본문**으로 받아 의존성을 늘리지 않는다 |
| 디자인 토큰 | `ui_prototype/board.css`의 `:root` 변수 (`672c218` 통합 디자인 시스템) | 체험판 페이지가 같은 값을 복사해 앱과 인상을 맞춘다 |

## 3. 범위

### 포함

1. 파서 코어: 칠판 cutout을 끄는 스위치 + `problem_parser.py` (웹과 무관한 순수 라이브러리)
2. 체험판 서버 `trial_server.py`: 업로드 1개 엔드포인트, **상태를 저장하지 않는다**
3. 입력 제한, 파싱 격리(별도 프로세스·시간·메모리 제한), IP별 일일 횟수, 봇 방어
4. 체험판 페이지 `trial_web/`: 업로드 → 처리 중 → 결과, 프리미엄 팝업
5. 컨테이너 이미지와 운영 지표(로그)

### 제외 (YAGNI)

- 계정, DB, 결과 저장·공유 링크, 대기열 서비스
- AI 인식, HWP 입력, EDB·ZIP 생성, 문항 수정
- 샘플 시험지 갤러리 (필요하면 나중에 정적 페이지로 추가)
- 다중 인스턴스 (횟수 제한이 프로세스 메모리에 있으므로 인스턴스 1개로 운영)
- `app.jsx` 재사용

## 4. 아키텍처

```
브라우저 ── HTTPS ──▶ Cloudflare (프록시 + Turnstile)
                          │
                 [trial_server.py  FastAPI/uvicorn, 인스턴스 1개]
                   ├── GET  /              trial_web/ 정적 페이지
                   ├── POST /api/parse     업로드 → 결과 JSON (동기)
                   ├── POST /api/event     프리미엄 버튼 클릭 기록
                   └── GET  /api/health
                          │  (동시 2건, 초과 시 최대 20초 대기)
                 [파싱 프로세스 — forkserver, 요청마다 1개]
                   problem_parser.parse_problems()
                     build_pages → build_problem_entries(render_board_assets=False)
                     → 미리보기 축소 → 임시 폴더 삭제
```

**동기 처리로 가는 이유.** 3쪽·AI 없음이면 한 건이 수 초다(§9 실측 참고). 잡 ID·폴링·결과 저장이 없으면
DB도 디스크 보존 정책도 필요 없다. 업로드 원본을 **저장하지 않는다**는 점은 시험지를 올리기 꺼리는
학원에 그대로 안내 문구가 된다.

**호스팅에 묶이지 않는다.** 상태가 없으므로 컨테이너 하나로 어디서든 돈다. 호스팅 선택은 §12의 결정 항목이다.

## 5. 구성 요소

### 5-1. 파서 코어 변경 — `build_problem_board_edb.py`

- `build_problem_entries(..., render_board_assets: bool = True)` 인자를 추가한다.
- `False`면 `_ProblemAssetTask`에 표시해 `_render_problem_asset()`이 `_render_problem_board_asset()`을 건너뛰고,
  `_coalesce_cross_page_passage_drafts()`가 `board_render_path` 이어 붙이기를 건너뛴다. cutout 폴더도 만들지 않는다.
- 기본값이 `True`이므로 앱 동작은 바뀌지 않는다.

### 5-2. `problem_parser.py` (신규, 웹 의존성 없음)

```python
@dataclass(frozen=True)
class ParsedPage:     page_id: str; index: int; width: int; height: int; preview_jpeg: bytes
@dataclass(frozen=True)
class ParsedRegion:   page_id: str; bbox: Box            # 원본 페이지 픽셀 좌표
@dataclass(frozen=True)
class ParsedProblem:  problem_id: str; number: int | None; title: str
                      regions: list[ParsedRegion]        # 페이지를 넘는 지문이면 2개 이상
                      risk_flags: list[str]; preview_jpeg: bytes
@dataclass(frozen=True)
class ParseResult:    pages: list[ParsedPage]; problems: list[ParsedProblem]
                      parser_version: str; timing_ms: dict[str, int]

def parse_problems(source: Path, *, work_dir: Path, subject: str = "unknown") -> ParseResult
```

- 순서: `build_pages(ocr_mode="local", ai_fallback_config=None, pdf_dpi=200, ...)` →
  `build_problem_entries(..., render_board_assets=False)` → crop과 페이지 이미지를 긴 변 기준으로 축소해 JPEG로 변환.
- 축소 기준: 페이지 미리보기 긴 변 1400px, 문항 미리보기 긴 변 900px, JPEG 품질 80.
- `regions`는 `ProblemEntry.source_segments`가 있으면 그것에서, 없으면 `source_page_id` + `bounds` 하나로 만든다.
- `parser_version`은 앱 버전 문자열과 git 커밋(빌드 시 환경변수 `EDB_BUILD_COMMIT`)을 합친다.
- 나중에 설치형 앱이 "문항 데이터셋"을 쓰게 되면 이 모듈이 공통 코어의 출발점이 된다. 이번에는 체험판만 쓴다.

### 5-3. `trial_server.py` (신규)

**`POST /api/parse`**
- 본문: 파일 바이트 그대로. 헤더 `Content-Type`(`application/pdf` | `image/png` | `image/jpeg`),
  `X-File-Name`(표시용), `X-Turnstile-Token`.
- 처리 순서: Turnstile 확인 → IP 일일 횟수 확인 → 본문을 10MB 상한까지만 스트리밍으로 읽기 →
  **파일 앞 바이트(매직 넘버)로 형식 판정**(헤더 값은 믿지 않는다) → 페이지 수·픽셀 검사 →
  파싱 세마포어 획득(최대 20초 대기) → 파싱 프로세스 실행 → 성공 시 횟수 차감 → 응답.
- 응답:

```json
{
  "parser_version": "<앱 버전>+5ff735b",
  "elapsed_ms": 4210,
  "pages": [{"page_id": "p1", "index": 0, "width": 2339, "height": 3308,
             "preview": "data:image/jpeg;base64,..."}],
  "problems": [{"problem_id": "q1", "number": 1, "title": "1번",
                "regions": [{"page_id": "p1", "bbox": {"left": 120, "top": 340, "width": 980, "height": 610}}],
                "risk_flags": [], "preview": "data:image/jpeg;base64,..."}],
  "remaining_today": 2
}
```

**`POST /api/event`** — `{"type": "premium_click", "feature": "edb" | "zip" | "edit" | "ai" | "more_pages" | "daily_limit"}`.
허용 목록 밖의 값은 버린다. 로그 한 줄만 남기고 저장하지 않는다.

**`GET /api/health`** — 버전과 현재 실행 중 파싱 수.

**설정 (환경변수)**

| 이름 | 기본값 | 의미 |
|---|---|---|
| `TRIAL_INQUIRY_URL` | (필수) | 팝업 "도입 문의" 링크. 페이지에 주입한다 |
| `TRIAL_TURNSTILE_SITE_KEY` / `TRIAL_TURNSTILE_SECRET` | 없음 | 없으면 봇 확인을 건너뛴다(로컬 개발용). 운영에서 비어 있으면 서버가 시작을 거부한다 |
| `TRIAL_TRUST_CF_HEADERS` | `0` | `1`이면 `CF-Connecting-IP`를 클라이언트 IP로 쓴다. Cloudflare 뒤에서만 켠다 |
| `TRIAL_MAX_BYTES` | `10485760` | 업로드 상한 10MB |
| `TRIAL_MAX_PAGES` | `3` | PDF 페이지 상한. 이미지는 1장 = 1쪽 |
| `TRIAL_DAILY_LIMIT` | `3` | IP당 하루 성공 횟수 (KST 자정 초기화) |
| `TRIAL_MAX_CONCURRENCY` | `2` | 동시 파싱 수 |
| `TRIAL_PARSE_TIMEOUT_S` | `60` | 파싱 프로세스 강제 종료 시간 |
| `TRIAL_WORKER_MEMORY_MB` | `3000` | 파싱 프로세스 가상 메모리 상한(Linux). 실측 후 조정 |

## 6. 입력 제한과 격리

| 위험 | 방어 |
|---|---|
| 큰 파일 | 10MB 넘으면 읽기를 멈추고 413 |
| 확장자 위장 | 매직 넘버로 PDF·PNG·JPEG만 허용, 나머지 415 |
| 페이지 폭탄 | 렌더 전에 PyMuPDF로 `page_count` 확인, 3쪽 초과 422 |
| 거대 페이지·이미지 폭탄 | PDF 페이지 크기가 A3의 2배(포인트 기준 면적)를 넘거나 이미지가 4천만 픽셀을 넘으면 422. `PIL.Image.MAX_IMAGE_PIXELS`도 같은 값으로 설정 |
| 무한 루프·악성 PDF | 파싱은 **별도 프로세스**(forkserver, 파서 모듈 미리 로드)에서 돌린다. 60초 넘으면 프로세스를 죽이고 504. 메모리는 Linux에서 `RLIMIT_AS`로 제한한다 — 가상 메모리 기준이라 실제 사용량(RSS)보다 크게 잡아야 하므로 값은 §9 실측으로 정하고, macOS 개발 환경에서는 적용하지 않는다. 컨테이너 메모리 한도가 마지막 방어선이다 |
| 몰림 | 동시 2건. 20초 안에 자리가 안 나면 503 "잠시 후 다시" |
| 반복 사용 | IP당 하루 3회. 서버 사정으로 실패한 요청(5xx)은 차감하지 않는다 |
| 봇 | Cloudflare Turnstile 토큰을 서버에서 확인 |
| 임시 파일 | 요청마다 `tempfile.TemporaryDirectory`. 성공·실패·타임아웃 모두 삭제. 서버 시작 시 남은 임시 폴더 정리 |

IP 횟수는 프로세스 메모리에 둔다. 재시작하면 초기화되지만 체험판에는 충분하다. 인스턴스가 1개라는 전제가 깨지면
이 부분부터 바꾼다.

## 7. 화면 흐름 — `trial_web/`

정적 파일 3개(`index.html`, `app.js`, `style.css`), 빌드 도구 없음, 문구는 한국어.

```
① 업로드                     ② 처리 중                    ③ 결과
┌────────────────────┐      ┌────────────────────┐      ┌─────────────────────────────┐
│ 시험지를 올리면      │      │ 문항을 찾는 중…     │      │ 문항 16개를 찾았어요  4.2초   │
│ 문항을 나눠드려요    │      │ (보통 5~10초)       │      │ [EDB 내보내기🔒][이미지 받기🔒]│
│                    │  →   │                    │  →   │ ┌ 페이지 ─────┐ ┌ 문항 ─────┐ │
│ [ PDF·이미지 끌어놓기]│      │                    │      │ │ 박스 오버레이 │ │ 1번 카드   │ │
│ 3쪽·10MB · 하루 3회  │      │                    │      │ │ (클릭→카드)  │ │ 2번 카드⚠ │ │
│ 파일은 저장 안 함    │      │                    │      │ └────────────┘ └──────────┘ │
└────────────────────┘      └────────────────────┘      │ [다른 파일 해보기] 오늘 2회 남음 │
                                                        └─────────────────────────────┘
```

- 결과 화면: 왼쪽 페이지 미리보기 위에 문항 박스를 그리고, 오른쪽에 문항 카드 목록을 둔다.
  박스를 누르면 해당 카드로, 카드를 누르면 해당 박스로 이동한다. 좁은 화면에서는 위아래로 쌓는다.
- `risk_flags`가 있는 문항은 "확인 필요" 표시를 단다. 이 표시가 "AI 정밀 인식" 팝업으로 가는 자연스러운 입구다.
- 🔒 버튼(EDB 내보내기, 이미지 받기, 문항 수정, AI로 더 정확하게)과 제한 초과 응답(413 제외 422·429)은 모두 같은
  **프리미엄 팝업**을 연다. 팝업은 기능별 제목과 공통 버튼을 쓴다.

| 계기 | 팝업 제목 (초안) |
|---|---|
| EDB 내보내기 | 클래스인 칠판에 바로 올리고 싶다면 |
| 이미지 받기·문항 수정 | 문항을 내 수업 자료로 쓰고 싶다면 |
| AI로 더 정확하게 / 확인 필요 문항 | 더 정확한 인식이 필요하다면 |
| 3쪽 초과·하루 횟수 초과 | 더 많이 사용하고 싶다면 |

  본문 공통: "설치형 앱에서는 페이지 제한 없이 AI 정밀 인식, 문항 수정, EDB 내보내기까지 할 수 있어요."
  버튼: **[도입 문의하기]** (`TRIAL_INQUIRY_URL`, 새 탭) / [닫기]. 버튼을 누를 때 `/api/event`를 보낸다.
- 문항이 0개면 빈 결과 대신 "문항을 찾지 못했어요" + AI 팝업 문구를 보여준다.
- 색·글꼴 변수는 `ui_prototype/board.css`의 `:root` 값을 복사한다.

## 8. 오류 응답

| 상태 | 사용자 문구 | 팝업 |
|---|---|---|
| 400 | 봇 확인에 실패했어요. 새로고침 후 다시 시도해 주세요 | — |
| 413 | 10MB 이하 파일만 올릴 수 있어요 | — |
| 415 | PDF, PNG, JPG 파일만 올릴 수 있어요 | — |
| 422 | 체험판은 3쪽까지 처리해요 (또는: 페이지가 너무 커요) | 더 많이 사용하고 싶다면 |
| 429 | 오늘 체험 횟수를 모두 썼어요 | 더 많이 사용하고 싶다면 |
| 503 | 지금 사용자가 많아요. 잠시 후 다시 시도해 주세요 | — |
| 504 / 500 | 이 파일은 처리하지 못했어요 | 더 정확한 인식이 필요하다면 |

응답 본문은 `{"error": {"code": "too_many_pages", "message": "..."}}` 형식이다. 서버 로그에는 스택을 남기고
응답에는 넣지 않는다.

## 9. 성능 근거와 운영 지표

- 실측(M4, AI 끔, `docs/web-hosting-options.md` §4): 16쪽 A3 7.3초·최대 1.19GB, 4쪽 2.1초·0.62GB.
  기존 로컬 미리보기 기록: 4쪽 16~20문항 1.4~1.9초. 체험판은 cutout·배치·EDB를 빼므로 이보다 짧거나 같다.
- 클라우드 vCPU를 M4 코어의 1/2~1/3로 잡으면 3쪽 한 건이 수 초~10초 안팎이다. 동시 2건 × 3쪽 한 건 약 0.6~1.2GB면
  **2 vCPU / 4GB** 한 대로 충분하다.
- **출시 전 필수:** 실제 시험지 묶음(스캔본·사진 포함)으로 3쪽 기준 p50/p95 시간, 최대 메모리, 실패율을 서버 사양에서 잰다.
  p95가 20초를 넘으면 페이지 상한이나 동시 처리 수를 조정한다.

운영 로그(한 줄 JSON, 개인정보 없음):

| 이벤트 | 필드 |
|---|---|
| `parse` | 결과 상태 코드, 바이트 수, 페이지 수, 문항 수, 소요 ms, `risk_flags` 개수, IP 해시(일 단위 솔트) |
| `premium_click` | `feature` |
| `reject` | 사유 코드 |

이 로그로 주간 퍼널(방문 → 업로드 → 결과 → 팝업 → 문의 클릭)과 **어떤 프리미엄 기능이 가장 많이 눌리는지**를 본다.
방문 수는 Cloudflare 통계를 쓴다.

## 10. 테스트

| 대상 | 방법 |
|---|---|
| cutout 스위치 | 합성 페이지로 `build_problem_entries`를 두 번 돌려 crop 파일 바이트가 같고, `False`에서 cutout 파일이 없음을 확인. 페이지를 넘는 지문 합치기 경로 포함 |
| 기존 동작 | 전체 `pytest` 통과 (기본값 `True`) |
| `problem_parser` | 합성 PDF(PyMuPDF로 생성)에서 페이지·문항 수, `regions` 좌표가 페이지 안에 있음, 미리보기 긴 변 상한 |
| 입력 검사 | 매직 넘버 위장, 10MB+1바이트, 4쪽 PDF, 거대 페이지, 4천만 픽셀 초과 이미지 |
| 격리 | 잠드는 가짜 파서로 타임아웃 → 504와 프로세스 종료·임시 폴더 삭제 확인. 메모리 초과 → 500 |
| 횟수·동시성 | 가짜 시계로 KST 자정 초기화, 5xx 미차감, 세마포어 대기 초과 503 |
| API | FastAPI `TestClient` + 가짜 파서로 상태 코드·응답 형식·Turnstile 확인 분기 |
| 페이지 | 브라우저로 업로드 → 결과 → 팝업 → 문의 링크까지 수동 확인, 400px 폭 확인 |

## 11. `web-migration` 브랜치 처리

- 브랜치와 워크트리를 그대로 둔다. `docs/superpowers/specs/2026-09-10-web-migration-phase1-design.md` 맨 위에
  "2026-09-15 보류 — 웹은 체험판으로 방향 전환, 본 문서 참고" 한 줄을 커밋한다.
- `web-trial`은 `3e935da`(웹 의존성 잠금)만 cherry-pick한다. 소켓 없는 세션 분리 등 나머지 Plan 1 커밋은 가져오지 않는다.
- 데스크톱 "동결" 전제는 해제된다. 본품은 계속 `up3_mac`에서 개발한다.

## 12. 결정이 필요한 것 (구현을 막지 않음)

| 항목 | 필요한 시점 | 추천 |
|---|---|---|
| 호스팅 | 배포 단계 | **Google Cloud Run** (인스턴스 최소 0·최대 1, 요청 동시성 2, 2 vCPU/4GB): 쓰지 않을 때 비용이 거의 0이고 회수 정책이 없다. 첫 요청은 컨테이너 기동으로 느리다. 대안은 Oracle Always Free VM(항상 켜짐, 유휴 회수 정책 확인 필요) |
| 도메인과 Cloudflare 계정 | 배포 단계 | Turnstile·프록시에 필요 |
| 도입 문의 링크 | 페이지 완성 전 | 카카오톡 채널 또는 구글 폼 URL 하나 |
| 팝업·안내 문구 최종본 | 페이지 완성 전 | §7 초안에서 수정 |
| 개인정보 안내 문구 | 배포 전 | "업로드한 파일은 처리 후 즉시 삭제되며 저장하지 않습니다" + IP는 횟수 제한에만 쓰인다는 한 줄 |

## 13. 알려진 위험

- **첫인상 = 로컬 인식 품질.** AI를 끄므로 스캔본·휴대폰 사진에서는 인식이 약할 수 있다. §9의 실측 묶음에 스캔본을 꼭 넣고,
  실패율이 높으면 첫 화면 안내를 "PDF 권장"으로 조정한다.
- **동기 요청 시간.** Cloudflare 프록시는 응답을 100초까지 기다린다. 파싱 60초 + 대기 20초로 그 안에 들어온다.
- **횟수 초기화.** IP 횟수가 메모리에 있어서, Cloud Run이 쉬는 동안 인스턴스를 0개로 줄이면 초기화된다.
  남용이 보이면 Cloudflare 요청 제한 규칙을 더하거나 최소 인스턴스를 1로 둔다.
- **IP 공유.** 학원·학교가 같은 공인 IP를 쓰면 하루 3회를 여러 명이 나눠 쓴다. 문의가 들어오면 한도를 올리거나
  브라우저 쿠키 기준을 더한다.
- **서버 코드 노출 면적.** 체험판 서버는 `app_server.py`를 import하지 않는다. 테스트로 이를 고정한다.

## 14. 구현 순서

Plan 하나, 네 부분으로 나눈다.

1. **파서 코어** — cutout 스위치, `problem_parser.py`, 테스트
2. **체험판 서버** — `3e935da` cherry-pick, 입력 검사, 파싱 프로세스 격리, 횟수·동시성, API, 테스트
3. **체험판 페이지** — 업로드·결과·팝업, 이벤트 전송
4. **배포 준비** — `Dockerfile.trial`, 실측 스크립트(p50/p95·메모리), 운영 문서. 실제 배포는 §12 결정 후
