# 문제 파서 웹 체험판 설계

- 작성일: 2026-09-15
- 브랜치: `web-trial` (베이스 `up3_mac` @ 5ff735b)
- 상태: 구현됨 (Plan 1·2, 2026-09-15). 운영 설정은 `docs/web-trial-operations.md`
- 선행 문서: `docs/problem-parser-web-trial.md` (2026-09-14, 방향·용량 검토)

## 1. 결정 사항

| 항목 | 결정 |
|---|---|
| 제품 역할 | **웹 = 무료 체험판, 설치형 앱 = 프리미엄 본품.** 웹은 도입 문의로 이어지는 창구다 |
| 대상 | 누구나. **가입 없음** |
| 체험 범위 | **글자가 들어 있는 PDF**(모의고사 원본 PDF 등) 업로드 → 문항 인식 → 페이지 위 문항 박스와 문항 미리보기 |
| 3쪽 넘는 PDF | **앞 3쪽만 처리한다.** 결과 위에 "무료 체험은 앞 3쪽까지예요 · 나머지 N쪽은 프리미엄으로" 배너 (2026-09-15 결정) |
| 사진·스캔본 | 파싱하지 않는다. "스캔본·사진은 프리미엄 AI 인식으로" 추천 팝업을 띄우고 횟수를 차감하지 않는다 (§2-1 실험) |
| 막는 기능 | EDB 내보내기, 이미지 저장, 문항 수정, AI 정밀 인식, 쪽수·용량·횟수 초과 → **프리미엄 추천 팝업** |
| 팝업 버튼 | **프리미엄 도입 문의** → `https://classin.co.kr/contact` (설정값 `TRIAL_INQUIRY_URL`) |
| 팝업 톤 | 막았다는 안내가 아니라 추천: "무료 체험은 여기까지예요 · 프리미엄으로 더 누려보세요!" (§7-2) |
| AI | **쓰지 않는다.** 로컬 인식 경로만 쓴다. AI는 팝업 추천 문구로만 등장한다 |
| 호스팅 | **Vercel Pro + Supabase(무료).** 서버는 Vercel 함수, 횟수 제한·이벤트 통계는 Supabase Postgres |
| 도메인·봇 방어 | 보유한 도메인과 Cloudflare 계정. 봇 방어는 Cloudflare Turnstile |
| 라이선스 | 사용자 판단으로 이번 설계의 제약에서 뺀다 |
| 기존 `web-migration` 브랜치 | **보류.** 삭제하지 않고 Plan 1 결과를 보존한다 (§11) |

## 2. 확인한 사실

### 2-1. 코드

| 항목 | 현재 상태 | 설계 함의 |
|---|---|---|
| 기본 파싱 진입점 | 앱의 기본 export 모드는 `run_problem_export()` (`build_problem_board_edb.py:11444`) | 체험판도 이 경로의 앞부분을 쓴다. 인식 품질이 앱과 같아야 체험이 의미 있다 |
| 인식 단계 | `build_pages()` (`:2604`) → 페이지별 `PageModel` | 그대로 쓴다 |
| 문항 정리 | `build_problem_entries()` (`:6010`)가 ① 문항 박스 계산(drafts) ② `_render_problem_assets()`로 이미지 생성 ③ 페이지를 넘는 지문 합치기 순서로 진행 | 박스 계산과 이미지 생성이 **이미 단계로 나뉘어 있다** |
| 이미지 생성 | `_render_problem_asset()` (`:2281`)이 문항 crop을 저장한 뒤 `_render_problem_board_asset()`로 칠판용 cutout을 **따로** 만든다 | cutout만 끄는 스위치를 넣으면 된다. 큰 추출 작업이 필요 없다 |
| 지문 합치기 | `_coalesce_cross_page_passage_drafts()`가 crop 크기를 쓰고, crop과 `board_render_path`를 **둘 다** 이어 붙인다 (`:5877~5898`) | crop은 끌 수 없다(합치기 품질에 필요). cutout 이어 붙이기만 같은 스위치로 건너뛴다 |
| 후처리 | 이후 `build_records` → 배치 요약 → EDB 쓰기 → handoff → `ui_session` | 체험판은 **전부 호출하지 않는다** |
| **AI 없는 인식 범위** | `ocr_mode="local"`은 PaddleOCR·Tesseract가 없으면 `none`으로 떨어진다 (`ocr_backend.py:1229~1245`). Vercel에는 둘 다 없다. 2026-09-15 실험: 4문항 합성 페이지를 PDF로 넣으면 `pdf-text-markers`로 **4문항·번호 1~4 정확**, 같은 페이지를 PNG로 넣으면 `visual-problem-markers`로 **2문항·번호 없음** | 체험판은 **텍스트 층이 있는 PDF만** 받는다. 이미지와 텍스트 없는 PDF는 파싱 전에 거른다 |
| 파이프라인 캐시 | `default_pipeline_cache_dir()`가 **입력 파일 옆** `.pipeline_cache`에 쓴다 (`pipeline_cache.py:47`) | 입력을 `/tmp` 작업 폴더에 두면 캐시도 `/tmp`에 쓰인다. Vercel의 읽기 전용 코드 폴더를 건드리지 않는다 |
| HWP 라이브러리 | `preprocess.py`의 `hwp_hwpx_parser`·`rhwp` import는 하위 프로세스용 스크립트 문자열 안에 있다 | 체험판 번들에서 HWP 패키지를 빼도 import 오류가 나지 않는다 (스파이크에서 확인) |
| 웹 의존성 | `web-migration`의 `3e935da`에 FastAPI·uvicorn 해시 잠금이 있다. `python-multipart`는 없다 | 버전 선택만 참고한다. 업로드는 multipart 대신 **원본 바이트 본문**으로 받아 의존성을 늘리지 않는다. HTTP 호출(Supabase·Turnstile)은 표준 라이브러리 `urllib`를 쓴다 (`ocr_backend.py`와 같은 방식) |
| 디자인 토큰 | `ui_prototype/board.css`의 `:root` 변수 (`672c218` 통합 디자인 시스템) | 체험판 페이지가 같은 값을 복사해 앱과 인상을 맞춘다 |

### 2-2. Vercel · Supabase (공식 문서, 2026-09-15 확인)

| 항목 | 내용 | 설계 함의 |
|---|---|---|
| 상업 이용 | Hobby는 비상업 개인용만. "제품·서비스 판매 광고"도 상업 이용 ([Fair Use](https://vercel.com/docs/limits/fair-use-guidelines)) | 도입 문의로 유도하므로 **Pro(월 $20, 사용 크레딧 $20 포함)** |
| 본문 크기 | 요청·응답 본문 **4.5 MB** 상한 ([Limits](https://vercel.com/docs/functions/limitations)) | 업로드 상한 **4 MB**, 응답은 **3.5 MB 예산** 안에서 미리보기 화질을 맞춘다 |
| 메모리·시간 | Pro 기본 2 GB · 1 vCPU (최대 4 GB · 2 vCPU), 기본·최대 실행 300초/800초 | 3쪽 한 건은 0.6~1.2 GB(§9). 2 GB로 시작, `maxDuration` 60초 |
| 번들 | 파이썬 500 MB (압축 해제 기준). 코드 트리 전체가 들어가므로 `excludeFiles`로 제외 ([Python runtime](https://vercel.com/docs/functions/runtimes/python)) | 필요 패키지 합계 약 234 MB(OpenCV 119, PyMuPDF 56, NumPy 33, Pillow 14, FastAPI 계열 12 — macOS 휠 실측). 저장소의 문서·테스트·리소스를 반드시 제외 |
| 파이썬 버전 | 3.12(기본)·3.13·3.14 | CI와 같은 계열로 고정한다 (스파이크에서 3.12 확인) |
| 확장 | 요청에 따라 인스턴스가 자동으로 늘어난다 | 프로세스 메모리에 횟수를 둘 수 없다 → Supabase. 동시 처리 대기열은 두지 않는다 |
| 비용 폭주 | Pro 지출 한도와 한도 도달 시 프로젝트 자동 일시정지 옵션 ([Pricing](https://vercel.com/pricing)) | 지출 한도를 걸고, 서버에도 **전체 하루 한도**를 둔다 |
| 리전 | 기본 `iad1`, 변경 가능 | 함수 리전 `icn1`(서울), Supabase도 서울 리전 |
| Supabase 무료 | DB 500 MB, **1주 동안 활동이 없으면 프로젝트 일시정지**, 프로젝트 2개 ([Pricing](https://supabase.com/pricing)) | 멈추면 횟수 확인이 실패한다 → §6 실패 처리와 §9 상태 점검 |

## 3. 범위

### 포함

1. 파서 코어: 칠판 cutout을 끄는 스위치 + `problem_parser.py` (웹과 무관한 순수 라이브러리)
2. 체험판 서버 `trial_server.py`: Vercel 함수로 도는 FastAPI 앱, 업로드 파일을 저장하지 않는다
3. 입력 제한, IP별·전체 하루 한도(Supabase), 봇 방어(Turnstile)
4. 체험판 페이지 `trial_web/`: 업로드 → 처리 중 → 결과, 프리미엄 추천 팝업
5. Vercel 배포 설정, Supabase 스키마, 운영 통계

### 제외 (YAGNI)

- 계정, 결과 저장·공유 링크, 대기열
- AI 인식, 이미지(PNG·JPG)·스캔 PDF 파싱, HWP 입력, EDB·ZIP 생성, 문항 수정
- 4 MB 넘는 파일을 위한 Storage 직접 업로드
- 샘플 시험지 갤러리
- `app.jsx` 재사용

## 4. 아키텍처

```
브라우저 ── HTTPS ──▶ Vercel (도메인은 Cloudflare DNS, 프록시 끔)
   │  Turnstile 위젯                 │
   │                       [trial_server.py  FastAPI · Vercel 함수 · icn1 · 2GB · 60초]
   │                         ├── GET  /              trial_web/ 정적 파일
   │                         ├── POST /api/parse     업로드 → 결과 JSON (동기)
   │                         ├── POST /api/event     추천 팝업 클릭 기록
   │                         ├── GET  /api/health    배포 점검용 (ready 여부)
   │                         └── GET  /api/cron/daily Vercel Cron 00:10 KST, Bearer CRON_SECRET
   │                                  │
   │                     /tmp/<요청별 폴더>          [Supabase Postgres · 서울]
   │                     problem_parser               trial_quota (IP·날짜별 횟수)
   │                     → 미리보기 → 폴더 삭제        trial_events (통계)
```

**동기 처리로 가는 이유.** 3쪽·AI 없음이면 한 건이 수 초다(§9). 잡 ID·폴링·결과 저장이 없으면 대기열도
보존 정책도 필요 없다. 업로드 원본을 **저장하지 않는다**는 점은 시험지를 올리기 꺼리는 학원에 그대로 안내 문구가 된다.

**Cloudflare는 DNS와 Turnstile만 쓴다.** Vercel 앞에 Cloudflare 프록시를 겹치면 캐시·인증서·IP 헤더가 꼬이므로
DNS 레코드는 프록시를 끈다(회색 구름). 클라이언트 IP는 Vercel이 넣어주는 `x-real-ip`를 쓴다.

## 5. 구성 요소

### 5-1. 파서 코어 변경 — `build_problem_board_edb.py`

- `build_problem_entries(..., render_board_assets: bool = True)` 인자를 추가한다.
- `False`면 `_ProblemAssetTask`에 표시해 `_render_problem_asset()`이 `_render_problem_board_asset()`을 건너뛰고,
  `_coalesce_cross_page_passage_drafts()`가 `board_render_path` 이어 붙이기를 건너뛴다. cutout 폴더도 만들지 않는다.
- 기본값이 `True`이므로 앱 동작은 바뀌지 않는다.

### 5-2. `problem_parser.py` (신규, 웹 의존성 없음)

```python
@dataclass(frozen=True)
class ParsedPage:     page_id: str; index: int; width: int; height: int; image: Image.Image
@dataclass(frozen=True)
class ParsedRegion:   page_id: str; bbox: Box            # 원본 페이지 픽셀 좌표
@dataclass(frozen=True)
class ParsedProblem:  problem_id: str; number: int | None; title: str
                      regions: list[ParsedRegion]        # 페이지를 넘는 지문이면 2개 이상
                      risk_flags: list[str]; image: Image.Image
@dataclass(frozen=True)
class ParseResult:    pages: list[ParsedPage]; problems: list[ParsedProblem]
                      parser_version: str; timing_ms: dict[str, int]

@dataclass(frozen=True)
class PdfInfo:        page_count: int; scanned_pages: int
                      pages_without_text: int; max_page_area_pt: float   # 앞 scanned_pages쪽 기준

def inspect_pdf(source: Path, *, max_pages: int) -> PdfInfo   # 렌더 없이 전체 쪽수와 앞 max_pages쪽의 텍스트 층·크기
def parse_problems(source: Path, *, work_dir: Path, max_pages: int, subject: str = "unknown") -> ParseResult
```

- 텍스트 층 판정: 앞 `max_pages`쪽 각각의 `page.get_text("text")`에서 공백을 뺀 글자가 20자 미만이면 "텍스트 없는 페이지"다. 한 페이지라도 있으면 거절한다.
- 쪽 자르기: 원본이 `max_pages`쪽을 넘으면 `parse_problems`가 `work_dir`에 앞 `max_pages`쪽만 `select()` 후 **`garbage=4, deflate=True`**로 저장해 그 파일을 파싱한다(`garbage` 없이 저장하면 지운 쪽의 글꼴이 남는다). `ParseResult`에 `source_page_count`를 더한다.
- 순서: `build_pages(ocr_mode="none", ai_fallback_config=None, pdf_dpi=200, ...)` →
  `build_problem_entries(..., render_board_assets=False)` → 페이지 이미지와 crop을 메모리로 읽어 반환.
- `regions`는 `ProblemEntry.source_segments`가 있으면 그것에서, 없으면 `source_page_id` + `bounds` 하나로 만든다.
- `parser_version`은 앱 버전 문자열과 커밋(Vercel이 넣어주는 `VERCEL_GIT_COMMIT_SHA` 앞 7자리)을 합친다.
- 미리보기 인코딩은 서버가 맡는다(응답 예산이 웹 사정이므로). 나중에 설치형 앱이 "문항 데이터셋"을 쓰게 되면
  이 모듈이 공통 코어의 출발점이 된다.

### 5-3. `trial_server.py` (신규)

**`POST /api/parse`**
- 본문: 파일 바이트 그대로. 헤더 `Content-Type: application/pdf`,
  `X-File-Name`(표시용), `X-Turnstile-Token`.
- 처리 순서:
  1. 본문을 4 MB 상한까지만 읽는다 (넘으면 413)
  2. Turnstile 토큰 확인 (실패 400)
  3. **파일 앞 바이트(매직 넘버)로 형식 판정**, 헤더 값은 믿지 않는다. PNG·JPEG면 415 `image_not_supported`, 그 밖은 415 `bad_type`
  4. `inspect_pdf(max_pages=3)`로 전체 쪽수(100쪽 초과 422)·앞 3쪽의 페이지 크기(422)·텍스트 층(422 `no_text_layer`) 검사
  5. 인스턴스당 파싱 슬롯을 **차감 전에** 잡는다. 20초 안에 못 잡으면 503이며 차감하지 않는다
  6. 요청 id(UUID)와 함께 Supabase `trial_consume()`로 IP 하루 한도와 전체 하루 한도를 **원자적으로 차감** (429)
  7. `/tmp` 요청 폴더에서 파싱하고 미리보기를 인코딩한다 (§5-4)
  8. 환불은 차감 결과를 모르는 경우(Supabase 응답 유실·시간 초과)에만 요청 id로 한 번 한다. **파싱이 시작된 뒤의 실패는 차감을 유지한다** — 파서를 죽이는 PDF를 반복해 올려 두 한도를 우회하지 못하게 하려는 것이다(2026-09-15 리뷰 결정)
  9. `trial_events`에 결과 한 줄 기록 (실패해도 응답은 보낸다). 예상하지 못한 예외도 JSON `parse_failed` 500으로 답한다
- 응답:

```json
{
  "parser_version": "<앱 버전>+5ff735b",
  "elapsed_ms": 4210,
  "source_page_count": 16,
  "processed_page_count": 3,
  "pages": [{"page_id": "p1", "index": 0, "width": 2339, "height": 3308,
             "preview": "data:image/jpeg;base64,..."}],
  "problems": [{"problem_id": "q1", "number": 1, "title": "1번",
                "regions": [{"page_id": "p1", "bbox": {"left": 120, "top": 340, "width": 980, "height": 610}}],
                "risk_flags": [], "preview": "data:image/jpeg;base64,..."}],
  "remaining_today": 2
}
```

**`POST /api/event`** — `{"feature": "edb" | "image" | "edit" | "ai" | "scan" | "limit_pages" | "limit_size" | "limit_daily", "action": "open" | "inquiry"}`.
허용 목록 밖의 값은 버린다. `trial_events`에 기록한다. IP당 분당 20회를 넘으면 조용히 버린다(서버 메모리 기준, 인스턴스별 근사치로 충분).

**`GET /api/health`** — 버전과 `ready`(필수 비밀값·한도 저장소 준비 여부). 비밀값 이름은 내보내지 않는다.

**`GET /api/cron/daily`** — Vercel Cron이 매일 00:10 KST에 `Authorization: Bearer $CRON_SECRET`으로 호출한다. Supabase 연결 확인(`trial_quota` 1행 조회) 후 `trial_cleanup()`을 부르고 실패하면 503을 돌려 함수 로그에 남긴다. `CRON_SECRET`이 없으면 404.

**설정 (Vercel 환경변수)**

| 이름 | 기본값 | 의미 |
|---|---|---|
| `TRIAL_INQUIRY_URL` | `https://classin.co.kr/contact` | 팝업 버튼 링크. 페이지에 주입한다 |
| `TRIAL_TURNSTILE_SITE_KEY` / `TRIAL_TURNSTILE_SECRET` | 없음 | 없으면 봇 확인을 건너뛴다(로컬 개발용). `VERCEL_ENV=production`에서 비어 있으면 모든 파싱 요청을 503으로 거부한다 |
| `SUPABASE_URL` / `SUPABASE_SECRET_KEY` | 없음 | 서버 전용 secret key(`sb_secret_...`, `apikey` 헤더). 브라우저에 절대 내보내지 않는다 |
| `TRIAL_IP_SALT` | 없음 | IP 해시용 비밀값 |
| `TRIAL_MAX_BYTES` | `4000000` | 업로드 상한 |
| `TRIAL_MAX_PAGES` | `3` | 처리할 앞쪽 수 |
| `TRIAL_MAX_SOURCE_PAGES` | `100` | 받아들이는 원본 전체 쪽수 상한 |
| `TRIAL_DAILY_LIMIT` | `3` | IP당 하루 성공 횟수 (KST 기준 날짜) |
| `TRIAL_GLOBAL_DAILY_LIMIT` | `500` | 전체 하루 파싱 상한. 비용 방어선 |

### 5-4. 응답 크기 예산

- 목표 3.5 MB 이하(Vercel 4.5 MB 상한과 base64 팽창을 고려).
- 기본값: 페이지 미리보기 긴 변 1200px, 문항 미리보기 긴 변 800px, JPEG 품질 78.
- 인코딩 후 합계가 예산을 넘으면 문항 미리보기를 긴 변 600px·품질 65로 다시 인코딩한다. 그래도 넘으면 페이지 미리보기를
  900px로 줄인다. 이 단계는 결정적이며 테스트로 고정한다.

### 5-5. Supabase 스키마

```sql
create table trial_quota (
  day        date   not null,           -- KST 날짜
  subject    text   not null,           -- IP 해시 또는 '__global__'
  used       int    not null default 0,
  primary key (day, subject)
);

create table trial_events (
  id          bigint generated always as identity primary key,
  created_at  timestamptz not null default now(),
  kind        text not null,            -- 'parse' | 'popup'
  status      int,                      -- parse: HTTP 상태
  reject_code text,                     -- 'too_large' | 'bad_type' | 'too_many_pages' | 'daily_limit' | ...
  feature     text,                     -- popup: §5-3 feature
  action      text,                     -- popup: 'open' | 'inquiry'
  source_pages int, pages int, problems int, risk_problems int,
  bytes       int, elapsed_ms int,
  ip_hash     text                      -- sha256(salt || 날짜 || ip) 앞 16자
);

create table trial_charges (             -- 차감 1건 = 1행, 환불을 정확히 한 번만 하게 한다
  request_id uuid primary key, day date, subject text, refunded boolean, created_at timestamptz
);

-- 원자적 차감: '__global__' 행 → subject 행 순서로 잠그고 두 한도를 확인·증가, 같은 id는 'duplicate'
create function trial_consume(p_request_id uuid, p_day date, p_subject text, p_limit int, p_global_limit int)
  returns table (allowed boolean, remaining int, reason text) ... set lock_timeout = '3s';
-- 그 요청의 차감만 한 번 되돌린다. 잠금 순서는 consume과 같다
create function trial_refund(p_request_id uuid) returns boolean ... set lock_timeout = '3s';
```

- `lock_timeout` 3초는 HTTP 클라이언트 시간 제한(5초)보다 짧다. 잠금을 오래 기다리면 커밋하지 않고 실패한다.
- 실제 파일은 `supabase/migrations/20260915000000_web_trial.sql`이고 로컬 PostgreSQL 17 + pgbench로 동시성·교착을 테스트한다.

- 두 테이블 모두 RLS를 켜고 정책을 두지 않는다. 서버만 service role 키로 접근한다.
- 보존: `trial_quota`는 7일, `trial_events`는 180일이 지나면 Vercel Cron이 매일 지운다.
- IP 해시에 날짜를 섞어 날짜가 바뀌면 같은 사람을 이어 추적할 수 없게 한다.

## 6. 입력 제한과 실패 처리

| 위험 | 방어 |
|---|---|
| 큰 파일 | 4 MB 넘으면 읽기를 멈추고 413. 브라우저에서 먼저 확인해 올리기 전에 안내한다 |
| 사진·스캔본 | 브라우저가 파일 선택 시 PDF가 아니면 올리지 않고 `scan` 팝업. 서버도 매직 넘버와 텍스트 층으로 다시 거른다 |
| 확장자 위장 | 매직 넘버로 PDF만 허용, 나머지 415 |
| 쪽수 폭탄 | 렌더 전에 `inspect_pdf()`로 전체 쪽수 확인, 100쪽 초과 422. 100쪽 이하는 앞 3쪽만 검사·처리 |
| 거대 페이지 | PDF 페이지 면적이 A3의 2배를 넘으면 422. 렌더된 페이지 이미지 대비 `PIL.Image.MAX_IMAGE_PIXELS`를 4천만으로 둔다 |
| 오래 걸리는 파일 | `maxDuration` 60초. 넘으면 Vercel이 504를 돌려준다(본문은 JSON이 아닐 수 있어 프론트가 일반 오류로 처리). 차감은 이미 됐으므로 되돌리지 못한다 — 드문 경우로 받아들인다 |
| 메모리 초과 | 함수 인스턴스가 죽고 500. 위와 같이 처리 |
| 반복 사용 | IP당 하루 3회. 파싱이 시작되면 실패해도 차감 유지, 슬롯 대기 초과는 차감하지 않음, 차감 결과를 모르면 요청 id로 환불 |
| 비용 폭주 | 전체 하루 500회 + Vercel 지출 한도 |
| 봇 | Turnstile 토큰을 서버에서 확인 |
| **Supabase 연결 실패·일시정지** | 파싱을 **거부한다(503, fail-closed).** 한도 없이 열어두면 비용이 무방비가 된다. `/api/health` 점검으로 알아챈다 |
| 임시 파일 | 요청마다 `/tmp` 아래 `TemporaryDirectory`. 성공·실패 모두 삭제 |
| 서버 코드 노출 면적 | `trial_server.py`는 `app_server.py`를 import하지 않는다. 테스트로 고정한다 |

## 7. 화면 — `trial_web/`

정적 파일 3개(`index.html`, `app.js`, `style.css`), 빌드 도구 없음, 문구는 한국어.

### 7-1. 흐름

```
① 업로드                     ② 처리 중                    ③ 결과
┌────────────────────┐      ┌────────────────────┐      ┌─────────────────────────────────┐
│ 시험지를 올리면      │      │ 문항을 찾는 중…     │      │ 문항 16개를 찾았어요 · 4.2초      │
│ 문항을 나눠드려요    │      │ (보통 10~20초)      │      │ ✦ 앞 3쪽까지 체험 · 나머지 13쪽 → │
│                    │  →   │                    │  →   │ [EDB 내보내기 ✦][이미지 저장 ✦]   │
│ [ PDF 끌어놓기 ]     │      │                    │      │ ┌ 페이지 ─────┐ ┌ 문항 ──────┐  │
│ 글자가 있는 PDF      │      │                    │      │ │ 박스 오버레이 │ │ 1번 카드    │  │
│ 앞 3쪽·4MB·하루 3회  │      │                    │      │ │ (클릭→카드)  │ │ 2번 확인필요 │  │
│ 파일은 저장하지 않아요│      │                    │      │ └────────────┘ └───────────┘  │
└────────────────────┘      └────────────────────┘      │ [다른 파일 해보기]                 │
                                                        │ ✦ 프리미엄으로 더 누려보세요 →      │
                                                        └─────────────────────────────────┘
```

- 결과 화면: 왼쪽 페이지 미리보기 위에 문항 박스, 오른쪽에 문항 카드. 박스와 카드를 서로 누르면 이동한다.
  좁은 화면(400px)에서는 위아래로 쌓는다.
- 원본이 3쪽을 넘으면 결과 맨 위에 `limit_pages` 배너를 둔다: "✦ 무료 체험은 앞 3쪽까지예요 · 나머지 {N}쪽은 프리미엄으로 →". 누르면 팝업.
- "오늘 {n}회 남음"은 결과 하단 "다른 파일 해보기" 옆에 둔다.
- `risk_flags`가 있는 문항에는 "확인 필요" 표시와 작은 "AI로 더 정확하게 ✦" 링크를 단다.
- ✦ 표시는 자물쇠 대신 "프리미엄" 배지다. 누르면 추천 팝업이 열린다.
- 결과 하단에는 늘 작은 추천 줄 "✦ 프리미엄으로 더 누려보세요 →"를 둔다.

### 7-2. 프리미엄 추천 팝업 문구

구조: 배지 "✦ 프리미엄" / 제목 / 본문 한두 줄 / **[프리미엄 도입 문의]** (새 탭, `TRIAL_INQUIRY_URL`) / [계속 체험하기]

| 계기 (`feature`) | 제목 | 본문 |
|---|---|---|
| 하루 횟수 초과 (`limit_daily`) | 오늘의 무료 체험을 모두 사용했어요 | 프리미엄으로 더 누려보세요! 설치형 앱에서는 횟수 걱정 없이 시험지를 처리할 수 있어요. |
| 3쪽 초과 배너 (`limit_pages`) | 무료 체험은 앞 3쪽까지예요 | 나머지 {N}쪽도 프리미엄에서 한 번에 나눠 보세요! 시험지 한 권을 통째로 처리할 수 있어요. |
| 4MB 초과 (`limit_size`) | 무료 체험은 4MB까지 올릴 수 있어요 | 스캔본·고화질 시험지도 프리미엄에서 그대로 처리해 보세요. |
| EDB 내보내기 (`edb`) | 클래스인 칠판으로 바로 보내보세요 | 문항을 칠판에 자동 배치해 EDB 파일로 만들어 드려요. 프리미엄 기능이에요. |
| 이미지 저장 (`image`) | 잘라낸 문항을 수업 자료로 써보세요 | 문항 이미지를 한 번에 저장하는 건 프리미엄 기능이에요. |
| 문항 수정 (`edit`) | 문항 경계를 직접 다듬어 보세요 | 합치기·나누기·영역 조정은 프리미엄에서 할 수 있어요. |
| 사진·스캔본 (`scan`) | 스캔본·사진은 프리미엄 AI 인식으로 | 무료 체험은 글자가 들어 있는 PDF(예: 모의고사 원본 PDF)만 나눠 드려요. 프리미엄 AI 정밀 인식으로 스캔본·사진도 문항을 나눠 보세요! |
| AI·확인 필요·0문항·처리 실패 (`ai`) | 더 정확한 인식이 필요하신가요? | 프리미엄 AI 정밀 인식으로 스캔본·사진도 더 깔끔하게 나눠 드려요. |

- 문구 속 기능 약속(쪽수 제한 없음, AI 정밀 인식, 합치기·나누기 등)은 설치형 앱의 실제 기능과 맞는지 출시 전에 확인한다.
- 팝업이 열릴 때 `action: "open"`, 문의 버튼을 누를 때 `action: "inquiry"`를 `/api/event`로 보낸다.
- 색·글꼴 변수는 `ui_prototype/board.css`의 `:root` 값을 복사한다. ✦ 배지는 `--accent` 계열.

## 8. 오류 응답

응답 본문은 `{"error": {"code": "...", "message": "..."}}`. 서버 로그에는 스택을 남기고 응답에는 넣지 않는다.

| 상태 | code | 사용자 문구 | 팝업 |
|---|---|---|---|
| 400 | `bot_check_failed` | 확인에 실패했어요. 새로고침 후 다시 시도해 주세요 | — |
| 413 | `too_large` | — | `limit_size` |
| 415 | `image_not_supported` | — | `scan` |
| 415 | `bad_type` | PDF 파일만 올릴 수 있어요 | — |
| 422 | `no_text_layer` | — | `scan` |
| 422 | `too_many_pages` (원본 100쪽 초과) | 페이지가 너무 많은 파일이에요 | `limit_pages` |
| 422 | `page_too_large` | 페이지 크기가 너무 커요 | — |
| 429 | `daily_limit` | — | `limit_daily` |
| 503 | `busy` (전체 한도·Supabase 실패·Turnstile 미설정) | 지금은 체험이 어려워요. 잠시 후 다시 시도해 주세요 | — |
| 500·504 | `parse_failed` / (Vercel 기본) | 이 파일은 처리하지 못했어요 | `ai` |
| 200, 문항 0개 | — | 문항을 찾지 못했어요 | `ai` |

## 9. 성능 근거와 운영

- 실측(M4, AI 끔, `docs/web-hosting-options.md` §4): 16쪽 A3 7.3초·최대 1.19 GB, 4쪽 2.1초·0.62 GB.
  기존 로컬 미리보기 기록: 4쪽 16~20문항 1.4~1.9초. 체험판은 cutout·배치·EDB를 빼므로 이보다 짧거나 같다.
- Vercel 1 vCPU를 M4 코어의 1/2~1/3로 잡으면 3쪽 한 건이 수 초~10초 안팎이다. **콜드 스타트는 문서에 수치가 없다.**
  OpenCV·PyMuPDF import가 더해지므로 스파이크에서 잰다.
- **출시 전 필수:** 여러 출처의 실제 텍스트 PDF 시험지 묶음(모의고사·학교·학원 자체 제작)으로 Vercel 프리뷰 배포에서 3쪽 기준 p50/p95 시간(콜드·웜 따로), 텍스트 층 판정에서 `scan`으로 빠지는 비율,
  최대 메모리, 실패율, 응답 크기를 잰다. p95가 20초를 넘거나 메모리가 1.6 GB를 넘으면 쪽수 상한이나 함수 메모리를 조정한다.
- **비용 감:** Pro 크레딧 $20에 Active CPU 시간당 $0.128, 메모리 GB·시간당 $0.0106. 한 건 10초·2 GB로 잡으면
  한 건에 약 $0.0004(CPU) + $0.00006(메모리)라 **하루 500건 상한을 매일 채워도 월 $7 안팎**이다(추정, 정적 파일 전송 별도).
- 통계: Supabase SQL로 주간 퍼널(업로드 → 결과 → 팝업 열림 → 문의 클릭)과 **어떤 프리미엄 기능이 가장 많이 눌리는지**를 본다.
  방문 수는 Vercel Web Analytics를 쓴다. 조회용 SQL 뷰 `trial_weekly_funnel`을 스키마에 포함한다.
- 배포 흐름: Vercel 프로젝트를 저장소에 연결하고 **운영 브랜치를 `web-trial`로 지정**한다. 같은 저장소의 `up3_mac` 등
  다른 브랜치 푸시가 프리뷰 빌드를 만들지 않도록 Ignored Build Step으로 `web-trial`만 빌드한다.

## 10. 테스트

| 대상 | 방법 |
|---|---|
| cutout 스위치 | 합성 페이지로 `build_problem_entries`를 두 번 돌려 crop 파일 바이트가 같고, `False`에서 cutout 파일이 없음을 확인. 페이지를 넘는 지문 합치기 경로 포함 |
| 기존 동작 | 전체 `pytest` 통과 (기본값 `True`) |
| `problem_parser` | 합성 PDF(PyMuPDF로 생성)에서 페이지·문항 수·번호, `regions` 좌표가 페이지 안. `inspect_pdf`: 텍스트 PDF, 그림만 있는 PDF(`pages_without_text`), 섞인 PDF, 앞쪽만 검사(4쪽째가 그림이어도 통과), 쪽수, 페이지 크기. `parse_problems(max_pages=3)`: 5쪽 PDF에서 앞 3쪽 문항만, 잘린 파일이 원본보다 작음 |
| 응답 예산 | 문항이 많은 합성 결과로 3.5 MB 이하로 줄어드는 단계와 결정성 |
| 입력 검사 | 매직 넘버 위장, PNG·JPEG 거절 코드, 4 MB+1바이트, 4쪽 PDF, 거대 페이지, 텍스트 없는 PDF |
| 한도 | 가짜 Supabase 클라이언트로 차감·되돌림·KST 날짜 경계·전체 한도·연결 실패 시 503 |
| SQL | `trial_consume`의 동시 호출 원자성은 Supabase 로컬 또는 프리뷰 DB에서 한 번 수동 확인 |
| API | FastAPI `TestClient` + 가짜 파서·가짜 Supabase로 상태 코드·응답 형식·Turnstile 분기, `app_server` 미import |
| 페이지 | 브라우저로 업로드 → 결과 → 팝업 7종 → 문의 링크, 400px 폭 |

## 11. `web-migration` 브랜치 처리

- 브랜치와 워크트리를 그대로 둔다. `docs/superpowers/specs/2026-09-10-web-migration-phase1-design.md` 맨 위에
  "2026-09-15 보류 — 웹은 체험판으로 방향 전환, `web-trial` 브랜치 설계 참고" 한 줄을 커밋한다.
- `web-trial`은 `web-migration` 커밋을 가져오지 않는다. FastAPI 버전 선택만 `3e935da`의 잠금을 참고한다.
- 데스크톱 "동결" 전제는 해제된다. 본품은 계속 `up3_mac`에서 개발한다.

## 12. 필요한 준비물

| 항목 | 필요한 시점 | 상태 |
|---|---|---|
| Vercel Pro 팀과 저장소 연결 | 스파이크 | 준비 필요 |
| Supabase 프로젝트(서울 리전) | 서버 구현 | 준비 필요 |
| Cloudflare Turnstile 사이트 키·비밀 키 | 페이지 완성 전 | 계정 있음 |
| 체험판 도메인(예: `trial.` 서브도메인) DNS 레코드 | 배포 | 도메인 있음 |
| 도입 문의 링크 | — | `https://classin.co.kr/contact` 확정 |
| 팝업 문구 | — | §7-2 추천 톤 초안 확정, 기능 약속만 출시 전 확인 |
| 개인정보 안내 문구 | 배포 전 | 초안: "올린 파일은 처리 후 바로 삭제하며 저장하지 않아요. 접속 IP는 하루 이용 횟수 확인에만 쓰이고, 되돌릴 수 없는 형태로 바꿔 보관해요." |

## 13. 알려진 위험

- **체험 가능한 파일이 좁다.** 학교 시험지는 스캔본이 많아 상당수가 `scan` 팝업으로 빠질 수 있다. 이것도 문의로 이어지는 입구지만,
  `scan` 비율이 너무 높아 체험 자체가 안 되면 사진·스캔본에 한해 AI를 제한적으로 여는 방안을 다시 검토한다.
- **텍스트 PDF에서도 품질 편차.** 글자가 윤곽선으로 변환된 PDF나 특이한 레이아웃은 약할 수 있다. §9 실측 묶음에 여러 출처의 PDF를 넣는다.
  약한 결과는 "AI로 더 정확하게 ✦" 추천으로 이어진다.
- **4 MB 상한.** 실측(2026-09-15): 16쪽 수능·모평 국어 원본이 2.76~3.71 MB로 상한 안에 들어간다(`docs/web-trial-spike-results.md` §1-1). 그림이 많은 과목은 넘을 수 있으므로 `limit_size` 팝업 비율을 지켜본다.
- **콜드 스타트.** 한동안 요청이 없으면 첫 사용자가 몇 초 더 기다린다. 스파이크 수치가 나쁘면 처리 중 화면 안내 문구로 흡수하거나
  Vercel 인스턴스 예열 옵션을 검토한다.
- **Supabase 일시정지.** 1주 동안 활동이 없으면 멈추고, 멈추면 체험판이 503을 낸다. 하루 1회 `/api/health` 점검으로 알아채고
  대시보드에서 재개한다. 운영이 길어지면 Supabase Pro($25/월)로 옮긴다.
- **IP 공유.** 학원·학교가 같은 공인 IP를 쓰면 하루 3회를 여러 명이 나눠 쓴다. 이 경우도 `limit_daily` 팝업 → 도입 문의로 이어진다.

## 14. 구현 순서

Plan 하나, 다섯 부분으로 나눈다.

0. **배포 스파이크 (가장 먼저)** — 최소 FastAPI 함수에 파서 의존성을 넣어 Vercel 프리뷰에 올리고 합성 3쪽 PDF를 파싱한다.
   확인: 번들 크기 500 MB 이하, `excludeFiles` 설정, `/tmp` 쓰기, 콜드·웜 시간, 메모리, 파이썬 버전.
   여기서 막히면 호스팅 결정을 다시 연다
1. **파서 코어** — cutout 스위치, `problem_parser.py`, 테스트
2. **체험판 서버** — 입력 검사, Turnstile, Supabase 한도·이벤트, 응답 예산, API, 테스트
3. **체험판 페이지** — 업로드·결과·추천 팝업, 이벤트 전송
4. **출시 준비** — Supabase 마이그레이션 SQL, Vercel Cron(점검·정리), 실측 스크립트, 운영 문서, `web-migration` 보류 표시
