# 웹 체험판 칠판용(배경 제거) 미리보기 설계

- 작성일: 2026-09-16
- 브랜치: `web-trial` (기준 커밋 2ece93a, 4쪽 체험판·박람회 시연 포함)
- 상태: 설계 검토 대기
- 선행 문서: `docs/superpowers/specs/2026-09-15-problem-parser-web-trial-design.md`, `docs/web-trial-load.md`

## 1. 결정 사항

| 항목 | 결정 |
|---|---|
| 제공 형태 | 한 응답에 **원본과 칠판용을 모두** 싣는다. 결과 화면의 문항 목록 위에 토글(원본 / 칠판용)을 두고 모든 카드를 한 번에 바꾼다. 저장·다운로드는 지금처럼 프리미엄 팝업 |
| 칠판용 생성 | 데스크톱과 같은 `_render_problem_board_asset()`(분필색 잉크 + 투명 배경, 그림 영역은 원본 픽셀 보존)을 켠다. 서버가 **charcoal 테마 배경에 합성**해 보내고 알파는 보내지 않는다 |
| 인코딩 | 페이지·문항 원본·칠판용 미리보기 **모두 WebP lossy, 품질 78, method 2**. JPEG보다 25~40% 작고 화질은 같다(§2-3) |
| 미리보기 단계 | 페이지 1000px / 문항 800px / 칠판용 800px · q78 → 1000/600/600 · q65 → 900/600/600 · q65 → 700/450/**없음** · q55. 마지막 단계는 칠판용을 빼서 예산을 지킨다 |
| 설정 | `TRIAL_BOARD_PREVIEWS` 기본 켬(`1`). `0`이면 컷아웃 생성·전송·토글이 모두 사라진다 |
| 동시성 | 인스턴스당 파싱 1건 유지(현재 기본). 칠판용을 켜면 5쪽 국어 2건 겹침이 1.47 GB라 2건은 안 된다 |
| 복잡도 상한 | 컷아웃이 4쪽 기준 Vercel +4~6초를 더하므로 `TRIAL_MAX_DRAWINGS_PER_PAGE` 기본을 2500에서 **2000**으로 내린다(단어 4500 유지). 계획에서 `complexity.py --board`로 다시 재고 "두 상한을 채운 4쪽 입력의 Vercel 추정 ≤ 30초"로 확정한다 |
| 안내 문구 | 처리 중 화면 "보통 10~20초 걸려요" → **"보통 15~30초 걸려요"** (공개·시연 둘 다) |
| 기본 보기 | 원본. 방문자가 인식 결과(박스·번호)를 먼저 확인하고, 토글로 칠판용을 본다 |

## 2. 확인한 사실 (2026-09-16, 로컬 M4 · 스레드 1 · 새 폴더 복사, Vercel 시간 ≈ ×4, 메모리 ≈ ×1)

### 2-1. 의존성과 코드 경로

- 컷아웃 경로(`_enhance_problem_crop` → `_extract_problem_cutout` → `_apply_selective_media_preservation` → `_finalize_text_cutout`)는 numpy와 Pillow(`ImageOps`, `ImageFilter`)만 쓴다. 둘 다 `pyproject.toml`에 고정된 Vercel 번들 안이다.
- `build_problem_entries(render_board_assets=True)`면 `entry.board_render_path`에 RGBA PNG가 생기고, 페이지를 넘는 지문 병합(`_coalesce_cross_page_passage_drafts`)도 컷아웃을 함께 이어 붙인다. 국어 3종·5쪽까지 돌려 문제없이 동작했다.
- 트라이얼 파서 `problem_parser.parse_problems()`는 지금 `render_board_assets=False`로 고정돼 있다. 스위치와 결과 필드만 더하면 된다.

### 2-2. 시간·메모리 (컷아웃 끔 → 켬)

| 시험지 | 쪽 | 문항 | 문항 자산 단계 | 최대 RSS 1건 |
|---|---|---|---|---|
| 2026 9월 모평 국어 | 4 | 15 | 0.65 → 1.73초 | 569 → 869 MB |
| 2026 9월 모평 국어 | 5 | 20 | 0.92 → 2.27초 | 592 → 894 MB |
| 2025 수능 국어 | 4 | 15 | 0.66 → 1.70초 | 617 → 966 MB |
| 지구과학 | 4 | 20 | 0.66 → 1.93초 | 574 → 684 MB |
| 물리학Ⅰ | 4 | 20 | 0.64 → 1.89초 | 575 → 676 MB |
| 전자기 교재 | 5 | 20 | 0.58 → 2.07초 | 415 → 698 MB |

- 추가 시간은 문항 수·크롭 면적에 비례하고 드로잉 수와는 무관하다. 로컬 +1.05~1.5초 = Vercel +4~6초. 4쪽 한 건 8.5~10초 → 12~14초.
- 5쪽 국어 2건 겹침: 984 → 1,471 MB, wall 3.95 → 5.4초. 인스턴스당 1건이면 문제없다.

### 2-3. 인코딩 (물리학Ⅰ 4쪽 20문항, 긴 변 800px)

| 인코딩 | 20문항 합계 | 인코딩 시간 |
|---|---|---|
| 원본 JPEG q78 (현재) | 1,325 KB | 0.09초 |
| 원본 WebP q78 m2 | 792 KB | 0.25초 |
| 칠판 합성 JPEG q78 | 1,436 KB | 0.09초 |
| 칠판 합성 WebP q78 m2 | 872 KB | 0.26초 |
| 칠판 투명 WebP q78 m2 (알파 무손실) | 1,306 KB | 0.46초 |
| 칠판 투명 PNG | 3,398 KB | 0.23초 |
| WebP method 6 | 1,239 KB | 71초 — 쓰지 않는다 |

- 페이지 4장, 긴 변 1000px: JPEG 501 KB, WebP m2 320 KB (0.10초).
- 샘플(물리 20번, 국어 지문)을 눈으로 비교: WebP q78 m2는 원본·칠판용 모두 JPEG q78과 구분되지 않는다. 그림 영역은 원본 픽셀이 흰 상자로 남고 글자는 분필색이다.
- 알파를 보내면 브라우저에서 칠판 색을 바꿀 수 있지만 크기가 1.5배고, 이 체험판은 테마 선택이 없다. 합성본으로 간다.

## 3. 범위

### 포함

1. `problem_parser.parse_problems(render_board_assets=)` 스위치와 `ParsedProblem.board_image`
2. `trial_preview`: WebP 인코더, 단계 표에 칠판용 크기, 응답에 `board`·`board_previews`
3. `trial_config.board_previews` ← `TRIAL_BOARD_PREVIEWS`, `/api/config` 노출, `trial_server` 연결과 이벤트 기록
4. 화면: `trial_logic` 순수 함수, `app.js` 토글, `public/index.html`·`public/demo/index.html` 마크업과 문구, `style.css`
5. 복잡도 상한 재측정과 기본값, 벤치 스크립트 `--board` 플래그
6. 문서: 운영 문서 환경변수·점검·SQL, `docs/web-trial-load.md` §7 컷아웃 비용

### 제외 (YAGNI)

- 칠판 테마 선택, 알파 전송, 문항별 개별 탭, 비교 보기
- 저장·다운로드(프리미엄 팝업 유지), 토글 클릭 이벤트 기록
- 데스크톱 앱 기본값·공용 파이프라인 알고리즘 변경

## 4. 데이터 흐름

```text
PDF ─ parse_problems(render_board_assets=config.board_previews)
      ├ build_pages()                          (그대로)
      └ build_problem_entries(render_board_assets=True)
            → entry.crop_path (원본)  → ParsedProblem.image (RGB)
            → entry.board_render_path (RGBA) → ParsedProblem.board_image (RGBA | None)
    ─ build_parse_body(): 단계별로
            pages[].preview   = WebP(page, 1000px)
            problems[].preview = WebP(image, 800px)
            problems[].board   = WebP(charcoal 배경에 합성(board_image), 800px) | null
            board_previews     = 이 단계에 칠판용이 있고 결과에 board_image가 하나라도 있으면 true
    ─ 화면: 토글 "원본 | 칠판용" → 카드 <img src>를 preview / board로 바꾸고 카드에 어두운 배경
```

## 5. 구성 요소

### 5-1. `problem_parser.py`

- `parse_problems(..., render_board_assets: bool = False)`. `True`면 `build_problem_entries(render_board_assets=True)`.
- `ParsedProblem.board_image: Image.Image | None`. `_load_detached_rgba(entry.board_render_path)`로 메모리에 올려 `work_dir` 삭제와 무관하게 만든다. 스위치가 꺼지면 `None`이고 `problem_cutouts/` 폴더도 생기지 않는다(기존 테스트 유지).
- `timing_ms`는 그대로다. 컷아웃 비용은 이미 `assets`에 들어간다.

### 5-2. `trial_config.py`

- `TrialConfig.board_previews: bool = True`. `TRIAL_BOARD_PREVIEWS`는 `1/0`, `true/false`, `yes/no`, `on/off`(대소문자 무시)만 받고 그 밖은 `ValueError`(다른 설정과 같은 실패 방식).
- `/api/config`에 `board_previews`를 넣는다. 화면은 응답 본문의 `board_previews`로 토글을 판단하므로 이 값은 점검용이다.

### 5-3. `trial_preview.py`

- `encode_preview_data_uri(image, *, long_side, quality)`가 `encode_jpeg_data_uri`를 대체한다. 축소 규칙(HAMMING, `reducing_gap=2.0`, 확대 없음)은 그대로이고 저장만 `WEBP, quality, method=2`다. 접두사 `data:image/webp;base64,`.
- Pillow에 WebP가 없으면(`features.check("webp")` 거짓) 모듈 로드 시 JPEG로 내려간다(`PREVIEW_FORMAT`). 고정된 Pillow 12.2 휠은 WebP를 포함하므로 실제로는 WebP다. 테스트가 이 환경에서 WebP가 켜져 있음을 단언해 휠이 바뀌면 드러나게 한다.
- `PreviewStep`에 `board_long_side: int | None` 추가. 단계 표는 §1과 같다.
- 칠판용은 `_compose_board(board_image)`가 charcoal 배경 `(24, 28, 32)`에 `alpha_composite`한 RGB를 인코딩한다. `build_problem_board_edb`를 여기서 import하면 거절된 요청까지 OpenCV를 싣게 되므로 색은 상수로 두고, 테스트가 `BOARD_THEME_PALETTES[DEFAULT_BOARD_THEME]["background"]`와 같은지 확인한다.
- 응답: `problems[].board`(data URI 또는 `null`), 최상위 `board_previews: bool`. 단계에 칠판용이 없거나 `board_image`가 없는 문항은 `null`.
- 예산 초과 순서: 문항 축소 → 페이지 축소 → 칠판용 제거 → 그래도 넘으면 지금처럼 `PreviewBudgetExceeded`.

### 5-4. `trial_server.py`

- `parser(source, work_dir=..., max_pages=..., render_board_assets=config.board_previews)`. 테스트의 가짜 파서는 인자를 기록한다.
- 이벤트 `timing`에 `preview_step`과 `board`(0/1)를 더해 예산 폴백과 칠판용 탈락 빈도를 SQL로 볼 수 있게 한다. 열 추가는 없다(`timing`은 jsonb).

### 5-5. 화면

- `public/trial_logic.js`
  - `isSafeImageSource`가 `data:image/webp;base64,`와 `data:image/jpeg;base64,`를 받는다.
  - `hasBoardPreviews(payload)`: `payload.board_previews`가 참이고 `board`가 안전한 문항이 하나라도 있으면 참.
  - `cardImageSource(problem, mode)`: `mode === "board"`이고 `board`가 안전하면 `board`, 아니면 `preview`.
- `public/app.js`
  - `state.previewMode = "raw"`. 결과 렌더 시 `hasBoardPreviews`가 거짓이면 토글을 숨기고 모드를 `raw`로 되돌린다.
  - 토글 클릭: `aria-pressed` 갱신, 모든 `.problem-card img`의 `src`를 `cardImageSource`로 바꾸고 `problem-card--board` 클래스를 토글한다. 카드는 `data-problem-id`로 `state.lastPayload.problems`를 찾는다. 재업로드해도 모드는 유지된다.
- `public/index.html`, `public/demo/index.html`
  - 문항 목록 위에 토글 행: `<div class="problems-head"><span class="problems-head__label">문항 보기</span><div class="preview-toggle" id="preview-toggle" role="group" aria-label="문항 보기 방식" hidden><button type="button" data-mode="raw" aria-pressed="true">원본</button><button type="button" data-mode="board" aria-pressed="false">칠판용</button></div></div>`
  - 처리 중 문구 "보통 15~30초 걸려요".
- `public/style.css`
  - 오른쪽 열을 `.problems-panel`(sticky, 목록 스크롤)로 묶어 토글이 목록과 함께 고정된다. `.preview-toggle` 버튼은 `.secondary-button` 모양에 `[aria-pressed="true"]`를 강조색으로.
  - `.problem-card--board img { background: #181c20; border-color: #181c20; }`.

### 5-6. 복잡도 상한

- `trial_input.DEFAULT_MAX_DRAWINGS_PER_PAGE` 2500 → 2000(잠정). 계획에서 `scripts/trial_bench/complexity.py --board`를 4쪽·단어 4500 조건으로 돌려 Vercel 추정 ≤ 30초인 가장 큰 드로잉 값으로 확정하고 `docs/web-trial-load.md` §3-2에 기록한다. 실제 시험지 최대는 드로잉 613/쪽이라 정상 파일에는 영향이 없다.

### 5-7. 벤치 스크립트

- `scripts/trial_bench/complexity.py`, `memory.py`에 `--board` 플래그(→ `render_board_assets=True`). `common.parse_in_scratch`는 이미 `**kwargs`를 넘긴다. 코퍼스 관측(`observe.py`)은 문항 번호·박스만 채점하므로 바꾸지 않는다.

### 5-8. 문서

- `docs/web-trial-operations.md`: §2-3 환경변수 표에 `TRIAL_BOARD_PREVIEWS` 행, §3 점검에 "칠판용 토글" 항목, §4-3 경보 기준 "p95 20초"를 30초로, §4-5 SQL에 `timing->>'board'` 폴백 집계, §8 시연에도 동일 적용 한 줄.
- `docs/web-trial-load.md`: §7 "칠판용 컷아웃 비용" — §2-2·2-3 표와 `--board` 재측정 결과.

## 6. 응답 예산 계산 (base64 ×1.34 포함)

| 입력 | 단계 0 예상 | 예산 3.5 MB |
|---|---|---|
| 4쪽 과학 20문항 | 페이지 0.32 + 원본 0.79 + 칠판용 0.87 = 1.98 MB → 2.65 MB | 통과 |
| 5쪽 국어 20문항 | 페이지 0.40 + 원본 0.95 + 칠판용 0.95 ≈ 2.3 MB → 3.1 MB | 통과 |
| 4쪽 국어 15문항 | ≈ 1.6 MB → 2.2 MB | 통과 |

- 지금(JPEG, 페이지 1200px) 4쪽 과학이 2.7 MB였으니 두 버전을 실어도 응답은 같은 크기다.
- 계획 단계에서 실제 코퍼스 파일로 `preview_step` 분포를 다시 재고 표를 갱신한다.

## 7. 오류·실패 처리

| 상황 | 처리 |
|---|---|
| 컷아웃 렌더 예외 | 데스크톱과 같이 파싱 실패(`parse_failed` 500, 차감 유지). 따로 삼키지 않는다. 코퍼스 실행에서 발생하면 그때 원인을 고친다 |
| 예산 초과 | 단계 3에서 칠판용을 빼고, 그래도 넘으면 기존 `page_too_complex`(`response_budget`) |
| Pillow WebP 없음 | JPEG로 자동 폴백, 화면은 두 접두사를 모두 받는다 |
| `TRIAL_BOARD_PREVIEWS=0` | 파서가 컷아웃을 만들지 않고(`board_image=None`), 응답 `board`는 모두 `null`, `board_previews=false`, 토글 숨김 |
| 문항 일부만 `board_image` 없음(텍스트 폴백 카드 등) | 그 카드는 칠판용 모드에서도 원본을 보여준다(`cardImageSource` 폴백) |

## 8. 운영 영향 요약

| 항목 | 지금 | 이 설계 |
|---|---|---|
| Vercel 처리 시간, 4쪽 20문항 | 8.5~10초 | 13~16초 (컷아웃 +4~6초, WebP 인코딩 +1.5초) |
| 최대 RSS 1건 | 0.42~0.62 GB | 0.68~0.97 GB |
| 인스턴스당 동시 파싱 | 1 | 1 (필수) |
| 응답 크기, 4쪽 20문항 | 2.7 MB | 2.65 MB (두 버전) |
| 비용, 하루 500건 상한 | 월 $5~8 | +$3 |
| 복잡도 상한 (드로잉/쪽) | 2500 | 2000 (재측정으로 확정) |

## 9. 테스트

| 대상 | 방법 |
|---|---|
| 파서 스위치 | 합성 PDF로 `render_board_assets=True` → 모든 문항 `board_image`가 RGBA이고 알파 최솟값 < 255, `problem_cutouts/` 존재. 기본값 → `None`, 폴더 없음(기존 테스트). 페이지 넘김 지문 케이스에서도 `board_image`가 있다 |
| 인코더 | WebP data URI 접두사, 긴 변 축소·확대 없음(기존 테스트 이름만 갱신), 결정성. 이 환경에서 `features.check("webp")`가 참 |
| 단계 표·응답 | 단계별 `board` 유무, `board_previews`, 예산 초과 시 칠판용이 마지막에 빠지는 순서, `board_image=None`인 문항은 `null`, 합성 배경색이 팔레트와 같음 |
| 설정 | 기본 참, `0`·`false` → 거짓, 잘못된 값 → `ValueError` |
| API | 가짜 파서가 `render_board_assets` 인자를 받음(기본 참, 설정 끄면 거짓). 결과에 `board_image`가 있으면 응답 `board`·`board_previews`, 이벤트 `timing.board`·`preview_step` |
| 화면 논리 (node) | `isSafeImageSource` webp·jpeg, `hasBoardPreviews`, `cardImageSource` 폴백 |
| 마크업 | 두 HTML에 토글 마크업과 "15~30초" 문구가 있음 |
| 브라우저 | `scripts/run_trial_local.py`로 물리학Ⅰ을 올려 토글 전환·카드 배경·페이지 오버레이 확인(앱 내 브라우저) |
| 회귀 | 전체 `pytest` (macOS는 `LC_ALL=en_US.UTF-8`) |

## 10. 구현 순서

1. 파서 스위치·`board_image` (테스트 먼저)
2. 인코더 WebP 전환·단계 표·`board` 필드
3. 설정·서버 연결·이벤트
4. `trial_logic` 함수 → `app.js` 토글 → HTML·CSS·문구
5. 벤치 `--board`, 복잡도·메모리 재측정, 드로잉 상한 확정, `web-trial-load.md` §7
6. 운영 문서, 브라우저 확인, 전체 테스트

같은 워크트리를 다른 세션이 쓴다. 작업마다 작은 커밋, 파일 지정 `git add`, 커밋 전 `git status` 확인.
