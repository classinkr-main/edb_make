# 웹 체험판 운영 문서

- 대상: `web-trial` 브랜치의 공개 문제 파서 체험판
- 설계: `docs/superpowers/specs/2026-09-15-problem-parser-web-trial-design.md`
- 최종 확인일: 2026-09-16 (외부 서비스 설정 화면 이름은 바뀔 수 있다)

## 1. 무엇이 어디서 도는가

| 구성 | 위치 | 역할 |
|---|---|---|
| 체험판 화면 `public/` | Vercel CDN | 업로드·결과·프리미엄 팝업. 함수를 거치지 않는다 |
| API `trial_server.py` | Vercel Python 함수 (icn1, 60초) | `/api/config` `/api/parse` `/api/event` `/api/health` `/api/cron/daily` |
| 한도·통계 | Supabase Postgres (서울) | `trial_quota`, `trial_events`, `trial_weekly_funnel` |
| 봇 확인 | Cloudflare Turnstile | 위젯 + 서버 검증 |
| 매일 정리 | Vercel Cron `10 15 * * *` (00:10 KST) | 오래된 한도·이벤트 삭제, Supabase 연결 확인 |

업로드 파일은 요청이 끝나면 `/tmp`에서 지워지고 어디에도 저장하지 않는다.

## 2. 처음 한 번 설정

### 2-1. Supabase

1. 서울 리전(Northeast Asia, Seoul) 프로젝트를 쓴다. 일반 "APAC" 지역은 싱가포르로 잡힐 수 있으니 서울을 직접 고른다.
2. SQL Editor에서 `supabase/migrations/20260915000000_web_trial.sql` 전체를 실행한다. 다시 실행해도 되며, 파일이 바뀌면(예: 2026-09-15 리뷰 수정으로 `trial_charges` 추가) 다시 실행한다.
3. 확인:
   ```sql
   select relname, relrowsecurity from pg_class where relname in ('trial_quota', 'trial_charges', 'trial_events');
   -- 세 행 모두 true

   select column_name from information_schema.columns
   where table_schema = 'public' and table_name = 'trial_events'
     and column_name in ('timing', 'instance_id', 'reject_detail', 'complexity');
   -- 네 행 모두 나와야 한다. 하나라도 빠지면 마이그레이션이 코드보다 늦게 적용된 것이다
   ```
4. Project Settings > API Keys에서 **secret key**(`sb_secret_...`)를 만든다. 이 키는 Vercel 환경변수에만 넣고 채팅·저장소·`.env.local`에 넣지 않는다. publishable key는 이 체험판에서 쓰지 않는다.

**배포 순서: 마이그레이션 실행 → 코드 배포.** 새 열이 없는 상태로 코드가 먼저 나가면 PostgREST가 그 열을 포함한 insert를 조용히 거부해 이벤트가 사라진다(품질·속도·과부하 설계 §4-2). 마이그레이션 파일이 바뀔 때마다(새 열 추가 등) 이 순서를 다시 지킨다.

주의: 2026-05-30 이후 만든 프로젝트는 새 테이블·함수를 API 역할에 자동 공개하지 않는다. 마이그레이션이 `service_role`에만 직접 권한을 주므로 추가 작업은 없다.

### 2-2. Cloudflare Turnstile

1. Turnstile에서 위젯을 만든다. 모드는 Managed.
2. Hostname에 체험판 도메인(예: `trial.example.com`)을 넣는다. 와일드카드는 안 되고, 넣은 도메인의 하위 도메인은 자동 포함된다. `*.vercel.app` 전체를 넣지 않는다.
3. Site key와 Secret key를 Vercel 환경변수에 넣는다.

로컬 개발은 Cloudflare 공개 테스트 키를 쓰므로 운영 위젯이 필요 없다(§5).

### 2-3. Vercel

| 설정 | 값 |
|---|---|
| Git 저장소 | `classinkr-main/edb_make` |
| Production Branch | `web-trial` |
| Ignored Build Step (Custom) | `if [ "$VERCEL_GIT_COMMIT_REF" = "web-trial" ]; then exit 1; else exit 0; fi` (측정 기간에는 §2-5의 case 문으로 바꾼다) |
| Functions > Function CPU | Standard (2 GB / 1 vCPU). `vercel.json`으로는 바꿀 수 없다 |
| Spend Management | 월 한도를 걸고 **Pause production deployment**를 켠다. 켜지 않으면 알림만 온다. 멈춘 프로젝트는 대시보드에서 직접 재개해야 한다 |
| Deployment Protection | Standard. 운영 도메인만 공개, 미리보기 배포는 로그인 필요 |
| Domains | 체험판 도메인 추가 → Cloudflare DNS에 Vercel이 보여주는 레코드를 **프록시 끔(DNS only)**으로 등록 |

환경변수 (Production):

| 이름 | 값 | 필수 |
|---|---|---|
| `TRIAL_TURNSTILE_SITE_KEY` | Turnstile site key | 예 |
| `TRIAL_TURNSTILE_SECRET` | Turnstile secret key | 예 |
| `SUPABASE_URL` | `https://<project-ref>.supabase.co` | 예 |
| `SUPABASE_SECRET_KEY` | `sb_secret_...` | 예 |
| `TRIAL_IP_SALT` | `openssl rand -hex 32` 결과 | 예 |
| `CRON_SECRET` | `openssl rand -hex 32` 결과 | 예 |
| `TRIAL_HOSTNAMES` | 체험판 도메인, 쉼표로 여러 개 | 권장 (Turnstile 결과의 hostname 확인) |
| `TRIAL_INQUIRY_URL` | 기본 `https://classin.co.kr/contact` | 아니오 |
| `TRIAL_DAILY_LIMIT` / `TRIAL_GLOBAL_DAILY_LIMIT` | 기본 3 / 500 | 아니오 |
| `TRIAL_MAX_BYTES` / `TRIAL_MAX_PAGES` / `TRIAL_MAX_SOURCE_PAGES` | 기본 4000000 / 4 / 100 | 아니오 |
| `TRIAL_PARSE_CONCURRENCY` / `TRIAL_PARSE_WAIT_SECONDS` | 기본 1 / 20 (인스턴스당). 4쪽 초기 운영은 1로 시작하고 클라우드 RSS·분산 실측 후 조정 | 아니오 |
| `TRIAL_MAX_WORDS_PER_PAGE` / `TRIAL_MAX_DRAWINGS_PER_PAGE` | 기본 4500 / 2000 (2026-09-16 복잡도 실측으로 4500/2500, 2026-09-17 칠판용 컷아웃 도입으로 드로잉 2000, `docs/web-trial-load.md` §3·§7-2). 앞 4쪽 중 한 쪽이라도 넘으면 422 `page_too_complex` | 아니오 |
| `TRIAL_BOARD_PREVIEWS` | 기본 `1`(켬). 문항마다 칠판용(분필색·어두운 배경) 미리보기를 원본과 함께 보내고 화면에 [원본 / 칠판용] 토글을 연다. `0`이면 컷아웃을 만들지도 보내지도 않는다. 켜면 4쪽 한 건이 Vercel 기준 +4~6초, RSS +0.1~0.35 GB (`docs/web-trial-load.md` §7). `1/0`, `true/false`, `yes/no`, `on/off`만 받는다 | 아니오 |
| `EDB_PREPROCESS_PAGE_WORKERS` | 렌더(`render`) 단계 중 디스큐·마진 크롭·리사이즈 풀만의 스레드 상한(`_normalize_pdf_rendered_pages`, `preprocess.py:2753`). 래스터라이즈·PNG 저장(`render_pdf_pages`, `preprocess.py:442-478`)은 `doc.page_count`를 직렬 for 루프로 도는 코드라 이 변수의 영향을 받지 않는다. 미설정 시 기본값 `min(4, 페이지 수, CPU 코어 수)`이고, 값을 설정해도 `max(1, min(기본 상한, 설정값))`이라 이 상한보다 올릴 수는 없고 낮출 수만 있다(4쪽에서 8이나 16을 넣어도 4). 정수가 아닌 값은 조용히 기본값으로 되돌아가지만, 0 이하의 값은 기본값이 아니라 1(완전 직렬)로 고정된다(`preprocess.py:2719-2722`). 0을 "자동/해제" 뜻으로 넣으면 안 되고, 기본값으로 되돌리려면 변수를 지운다. "CPU 코어 수"는 함수 컨테이너 안에서 본 `os.cpu_count()`이며 Vercel이 실제로 할당한 1 vCPU와 다를 수 있고, 코어가 1로 보이면 이 변수는 어떤 값을 넣어도 완전한 no-op이 된다 — 실제 적용된 값은 각 페이지 메타데이터의 `pdf_preprocess_page_worker_count`(`preprocess.py:2760`)로 확인한다. render 단계를 풀 부분과 직렬 래스터라이즈 부분으로 나눠 재는 스크립트는 커밋된 코드에 없다 — 이전 판이 인용한 "render 단계 총 0.93초 중 이 풀이 0.09초, 직렬 래스터라이즈가 0.73초"라는 단계-내부 분해는 어떤 커밋된 명령으로도 재생산할 수 없었다. 대신 `scripts/trial_bench/complexity.py`가 이미 재는 render 단계 전체(end-to-end) 시간으로, 이 변수를 켰을 때와 껐을 때를 비교한다: 이 상한 조합(단어 4500·드로잉 2500·4쪽, §4-6의 비용 최악치와 같은 입력)에서 미설정(기본값)은 render 5826 ms, `EDB_PREPROCESS_PAGE_WORKERS=1`은 render 6027 ms로 약 201 ms(3.4%) 차이가 난다(2026-09-17, `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 4500 --drawings 2500 --pages 4`을 환경변수 미설정/`EDB_PREPROCESS_PAGE_WORKERS=1` 두 번 실행). Vercel 웜 파싱에서 실제로 파싱 시간을 가장 크게 좌우하는 단계는 이 변수와 무관한 인식(3.7~4.1초)·crop(2.7~3.1초)이다(품질·속도·과부하 설계 §2-2, 국어 3쪽 실측) | 아니오 |
| `EDB_PROBLEM_ASSET_WORKERS` | crop·asset 렌더(`assets`) 단계의 스레드 풀 상한(`build_problem_board_edb.py`). 미설정 시 기본값 `min(8, 작업 수, CPU 코어 수)`이고, `EDB_PREPROCESS_PAGE_WORKERS`와 같은 방식으로 값을 올려도 이 기본 상한 위로는 못 올라가며(낮추기만 가능) 정수가 아닌 값은 조용히 기본값으로 되돌아가지만 0 이하의 값은 기본값이 아니라 1(완전 직렬)로 고정된다(`build_problem_board_edb.py:1966-1969`). 즉 0은 "자동/해제"가 아니라 2.7~3.1초짜리 crop 단계를 통째로 직렬화하는 값이며, 기본값으로 되돌리려면 변수를 지운다. 여기서도 "CPU 코어 수"는 함수 컨테이너 안 `os.cpu_count()`이지 Vercel이 할당한 1 vCPU가 아니며, 코어가 1로 보이면 이 변수도 no-op이 된다. 렌더 단계에는 영향을 주지 않는다. Task 15의 A/B 결과로 정한다 | 아니오 |

**필수 6개 중 하나라도 비어 있으면 운영의 `/api/parse`는 503만 돌려준다.** 설정이 덜 된 채 배포돼도 파싱은 열리지 않는다. 환경변수를 바꾸면 재배포해야 반영된다. 4쪽 전환 시 기존 `TRIAL_MAX_PAGES=3`이 있으면 `4`로 수정하거나 삭제한다. 배포 뒤 `/api/config`의 `max_pages: 4`와 4쪽 시험지의 전체 결과를 확인한다. 20문항은 대표 사용 예시이며 문항 수 상한이나 인식 보장이 아니다. 기존 `TRIAL_PARSE_CONCURRENCY=2`도 초기 운영에서는 `1`로 수정하거나 삭제한다. 이는 인스턴스당 제한이며 서비스 전체 동시 사용자 수가 아니다.

### 2-4. 스파이크 정리

Vercel 측정은 `docs/web-trial-spike-results.md`에 기록했고 `/api/spike` 코드는 제거했다. Vercel에 남아 있는 `TRIAL_SPIKE_TOKEN` 환경변수는 지운다(코드가 더는 읽지 않아 남아 있어도 동작에는 영향이 없다).

### 2-5. 프리뷰 배포 (측정용)

| 설정 | 값 |
|---|---|
| Ignored Build Step | `case "$VERCEL_GIT_COMMIT_REF" in web-trial\|web-trial-bench) exit 1;; *) exit 0;; esac` |
| Deployment Protection > Protection Bypass for Automation | 켜고 비밀값을 `VERCEL_AUTOMATION_BYPASS_SECRET`으로 로컬 셸에만 둔다 |
| Preview 환경변수 | `TRIAL_DAILY_LIMIT=10000`, `TRIAL_GLOBAL_DAILY_LIMIT=10000` (프리뷰 한도는 인스턴스 메모리). Turnstile·Supabase 비밀은 넣지 않는다 → 봇 확인 생략, 메모리 한도 |

`web-trial-bench` 브랜치를 푸시하면 프리뷰가 뜬다. 프로브는 `scripts/trial_bench/probe.py`, 동시 요청은 `scripts/trial_bench/load.py`이며 둘 다 `x-vercel-protection-bypass` 헤더를 붙인다. 운영 환경에는 우회 경로가 없다.

## 3. 배포 후 점검

**마이그레이션 → 코드 배포 순서를 지켰는지 먼저 확인한다.** 순서가 바뀌면 PostgREST가 새 열을 포함한 insert를 조용히 거부해 이벤트가 사라진다(품질·속도·과부하 설계 §4-2):

```sql
select column_name from information_schema.columns
where table_schema = 'public' and table_name = 'trial_events'
  and column_name in ('timing', 'instance_id', 'reject_detail', 'complexity');
-- 네 행 모두 나와야 한다
```

이 쿼리는 DDL이 적용됐다는 것만 증명하고, insert가 실제로 성공한다는 증거는 아니다. PostgREST는 스키마를 캐시하므로 캐시가 오래되면 위 쿼리는 네 행을 그대로 보여주는데도 새 열을 포함한 insert는 `PGRST204`로 계속 실패할 수 있다. `trial_server.py`는 이벤트 기록 실패를 `logger.warning`으로만 남기고 방문자에게는 그대로 200을 돌려주므로(`record_event`), 이 화면은 초록인데 이벤트는 전부 유실되는 상태가 가능하다 — 진짜 게이트는 아래 4번에서 새 열이 실제로 채워졌는지 보는 것이다. 캐시가 오래된 것으로 보이면 `notify pgrst, 'reload schema';`를 실행하고, Vercel 함수 로그에서 `trial event not recorded` 경고가 찍히는지 확인한다.

```bash
DOMAIN=https://trial.example.com

curl -s $DOMAIN/api/health
# {"status":"ok","commit":"<7자리>","ready":true,"instance_id":"...","instance_age_s":...}   ← ready가 false면 §2-3 필수 환경변수 확인

curl -s $DOMAIN/api/config
# turnstile_site_key가 운영 키인지, inquiry_url이 맞는지

curl -sI $DOMAIN/ | grep -i content-security-policy
# vercel.json의 CSP가 붙어 있는지

curl -s -H "Authorization: Bearer $CRON_SECRET" $DOMAIN/api/cron/daily
# {"ok":true}
```

브라우저에서:
1. 텍스트 PDF(모의고사 원본)를 올려 결과·박스·카드가 보이는지, 4쪽 넘는 파일이면 "앞 4쪽까지" 배너가 뜨는지 본다. 문항 목록 위 [원본 / 칠판용]을 눌러 카드가 어두운 배경에 분필색 글자로 바뀌고 그림은 원본 그대로 남는지, 다시 원본으로 돌아오는지 본다.
2. 사진(JPG)을 골라 "스캔본·사진은 프리미엄 AI 인식으로" 팝업이 뜨는지 본다.
3. 팝업의 [프리미엄 도입 문의]가 `classin.co.kr/contact`로 열리는지 본다.
4. Supabase에서 이벤트가 쌓였는지, 그리고 새 열이 실제로 채워지는지 본다 (배포 순서가 지켜졌다는 진짜 증거는 이것이다):
   ```sql
   select kind, status, reject_code, feature, action, created_at from public.trial_events order by id desc limit 10;

   select count(*) from public.trial_events
   where timing is not null and instance_id is not null
     and created_at > now() - interval '1 hour';
   -- 위 1번 스모크 파싱 이후라면 1 이상이어야 한다. 0이면 PostgREST 스키마 캐시가 오래된 것이니
   -- notify pgrst, 'reload schema'; 실행 후 다시 파싱한다
   ```

## 4. 운영

### 4-1. 주간 퍼널

```sql
select * from public.trial_weekly_funnel order by week desc limit 8;
```

| 열 | 뜻 |
|---|---|
| `parses` / `parses_ok` | 파싱 요청 / 성공 |
| `rejected_scan` | 텍스트 없는 PDF로 거절 — 높으면 스캔본 수요가 크다는 뜻 |
| `rejected_daily` | 하루 한도 초과 — 높으면 한도 조정 또는 문의 유도가 잘 되는 중 |
| `popup_opens` / `inquiries` | 프리미엄 팝업 열림 / 문의 버튼 클릭 |

### 4-2. 어떤 프리미엄 기능이 가장 끌리는가

```sql
select feature,
       count(*) filter (where action = 'open') as opens,
       count(*) filter (where action = 'inquiry') as inquiries
from public.trial_events
where kind = 'popup' and created_at > now() - interval '30 days'
group by feature
order by inquiries desc, opens desc;
```

### 4-3. 속도·실패

```sql
select date_trunc('day', created_at) as day,
       percentile_cont(0.5) within group (order by elapsed_ms) filter (where status = 200) as p50_ms,
       percentile_cont(0.95) within group (order by elapsed_ms) filter (where status = 200) as p95_ms,
       count(*) filter (where status >= 500) as server_errors
from public.trial_events
where kind = 'parse' and created_at > now() - interval '14 days'
group by 1 order by 1 desc;
```

p95가 30초를 넘으면 먼저 `TRIAL_BOARD_PREVIEWS=0`으로 칠판용을 끄고(한 건 4~6초 절약), 그래도 넘으면 `TRIAL_MAX_PAGES`를 줄이거나 Function CPU를 Performance로 올린다.

### 4-4. 보존

매일 00:10 KST 크론이 `trial_quota` 7일, `trial_events` 180일 지난 행을 지운다. 크론은 재시도하지 않으므로 하루 빠져도 다음 날 함께 정리된다.

### 4-5. 단계별 시간 · 인스턴스 · busy 사유

```sql
-- 단계별 p50/p95 (최근 7일, 성공만)
select
  percentile_cont(0.5) within group (order by (timing->>'render')::int) as render_p50,
  percentile_cont(0.95) within group (order by (timing->>'render')::int) as render_p95,
  percentile_cont(0.5) within group (order by (timing->>'segment')::int) as segment_p50,
  percentile_cont(0.95) within group (order by (timing->>'segment')::int) as segment_p95,
  percentile_cont(0.5) within group (order by (timing->>'assets')::int) as assets_p50,
  percentile_cont(0.95) within group (order by (timing->>'assets')::int) as assets_p95,
  percentile_cont(0.5) within group (order by (timing->>'encode')::int) as encode_p50,
  percentile_cont(0.95) within group (order by (timing->>'encode')::int) as encode_p95,
  percentile_cont(0.95) within group (order by elapsed_ms) as total_p95
from public.trial_events
where kind = 'parse' and status = 200 and created_at > now() - interval '7 days';

-- 페이지 복잡도 분포. 병리적 콘텐츠 스트림이라 inspect_pdf가 곧장 거절한 페이지
-- (problem_parser.PATHOLOGICAL_COUNT_SENTINEL = 1,000,000,000)는 실제 밀도가 아니므로 제외한다.
select
  percentile_cont(0.5) within group (order by (complexity->>'words')::bigint) as words_p50,
  percentile_cont(0.95) within group (order by (complexity->>'words')::bigint) as words_p95,
  percentile_cont(0.5) within group (order by (complexity->>'drawings')::bigint) as drawings_p50,
  percentile_cont(0.95) within group (order by (complexity->>'drawings')::bigint) as drawings_p95
from public.trial_events
where kind = 'parse' and complexity is not null
  and (complexity->>'words')::bigint < 1000000000
  and (complexity->>'drawings')::bigint < 1000000000
  and created_at > now() - interval '7 days';
-- words나 drawings가 1,000,000,000이면 그 요청은 페이지가 그만큼 밀도 높았던 게 아니라
-- 콘텐츠 스트림이 너무 복잡해 inspect_pdf가 세는 대신 거절했다는 뜻이다.

-- 미리보기 예산 폴백: preview_step > 0 이면 축소 단계로 답했고, board = 0 이면 칠판용이 빠진 응답이다.
select (timing->>'preview_step')::int as preview_step, (timing->>'board')::int as board, count(*)
from public.trial_events
where kind = 'parse' and status = 200 and created_at > now() - interval '7 days'
group by 1, 2 order by 1, 2;

-- 쪽수 상한에 잘린 지문 묶음: timing.continued 가 1 이상이면 "N쪽에 이어짐" 칩이 붙은 응답이다.
select count(*) filter (where (timing->>'continued')::int > 0) as with_cut_passages, count(*) as parses
from public.trial_events
where kind = 'parse' and status = 200 and created_at > now() - interval '7 days';

-- 인스턴스별 건수: 1건짜리 인스턴스가 많으면 콜드 스타트가 잦다는 뜻
select instance_id, count(*) as requests, min(created_at) as first_seen, max(created_at) as last_seen
from public.trial_events
where kind = 'parse' and created_at > now() - interval '7 days'
group by instance_id order by requests desc;

-- busy 사유별 건수
select reject_detail, count(*)
from public.trial_events
where kind = 'parse' and reject_code = 'busy' and created_at > now() - interval '7 days'
group by reject_detail order by 2 desc;
```

### 4-6. 비용 최악치

하루 상한(`TRIAL_GLOBAL_DAILY_LIMIT` 기본 500건)을 매번 **28.3초**·2 GB로 다 채운다고 가정한, **과금 트래픽 기준** 최악치(스펙 §9 단가: Active CPU 시간당 $0.128, 메모리 GB·시간당 $0.0106). 28.3초는 이전 판이 쓰던 15초짜리 임의 가정이 아니라, 현재 배포가 실제로 받아들이는 가장 무거운 입력 — `TRIAL_MAX_WORDS_PER_PAGE=4500`·`TRIAL_MAX_DRAWINGS_PER_PAGE=2500`를 4쪽 모두 채운 조합 — 의 Vercel 환산 실측값이다(2026-09-17, `docs/web-trial-load.md` §3-2, `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 4500 --drawings 2500 --pages 4`). `vercel.json`의 `maxDuration`은 60초이므로 이 28.3초 최악치는 그 안에 들고도 약 32초의 여유를 남긴다 — 60초 자체가 최악치는 아니다.

- 건당: 28.3 × (0.128 ÷ 3600) + 2 × (28.3 ÷ 3600) × 0.0106 ≈ $0.001006(CPU) + $0.000167(메모리) ≈ **$0.00117**
- 하루: 500 × $0.00117 ≈ **$0.59**
- 월(30일): $0.59 × 30 ≈ **$17.59** (Vercel 함수 컴퓨트만. 정적 파일 전송·Supabase·Vercel 기본 무료 사용량은 별도)

이 500건은 `charge_and_parse`로 과금·차감되는 일반 체험판 요청만 센다. §8 박람회 시연은 `charge_and_parse`가 아니라 `parse_and_encode`를 직접 불러 이 500회·Turnstile을 건너뛰므로(§8 "시연은 IP별 3회·일반 전체 500회 차감과 Turnstile을 건너뛴다") 시연 트래픽 전체가 이 수치 밖에 있고, `unreadable_pdf`·`page_too_complex`·`busy` 같은 과금 전 거절 요청도 500회를 소모하지 않는다 — 그중 `busy`(`slot_wait`) 거절은 과금 없이도 `TRIAL_PARSE_WAIT_SECONDS`(기본 20초) 만큼 함수 실행 시간을 붙잡아 둔다. 즉 위 수치는 과금된 트래픽만의 추정치이며 프로젝트가 실제로 쓸 수 있는 컴퓨트의 상한이 아니다. §2-3의 Spend Management 월 한도는 이 최악치에 여유를 두고 그보다 높게 잡는다. 실제 요금은 대부분 이 28.3초 상한 조합보다 가벼운 웜 케이스(§4-3)와 3~4쪽 실측(품질·속도·과부하 설계 §2-2)에 가까우므로 이 수치보다 낮게 나온다.

## 5. 로컬 실행

```bash
.venv/bin/python scripts/run_trial_local.py --turnstile-test
# http://127.0.0.1:8790 — public/ + API, vercel.json 헤더 적용, 한도는 메모리, Cloudflare 테스트 키
```

`--daily-limit 50`으로 한도를 늘릴 수 있다. Supabase에는 연결하지 않는다.

## 6. 장애 대응

| 증상 | 원인 | 조치 |
|---|---|---|
| 모든 업로드가 "지금은 체험이 어려워요" | 필수 환경변수 누락(`/api/health`의 `ready: false`) | §2-3 환경변수 확인 후 재배포 |
| 〃, `ready: true` | Supabase 일시정지(HTTP 540) 또는 장애 | Supabase 대시보드에서 프로젝트 재개. 1주 무활동 시 멈춘다 |
| 〃, 특정 시간대만 | 전체 하루 한도(`TRIAL_GLOBAL_DAILY_LIMIT`) 소진 | 한도 상향 여부 결정. 비용은 §4-3 건수로 추정 |
| "확인에 실패했어요" 반복 | Turnstile hostname 미등록, 잘못된 site key | Turnstile 위젯 Hostname에 도메인 추가, `TRIAL_TURNSTILE_SITE_KEY` 확인 |
| 확인 상자가 안 보임 | CSP 차단 또는 challenges.cloudflare.com 접속 차단 | 브라우저 콘솔의 CSP 오류 확인, `vercel.json` `script-src`·`frame-src` 확인 |
| 사이트 전체 503 `DEPLOYMENT_PAUSED` | Spend Management 한도 도달 | 사용량 확인 후 대시보드에서 재개 |
| 크론 `{"ok":false}` | Supabase 연결 실패 | 위 Supabase 항목과 같음 |
| 특정 PDF만 "처리하지 못했어요" | 파서 예외(`parse_failed`). 이 경우 사용 횟수는 차감된 채 남는다(반복 업로드로 한도를 우회하지 못하게) | Vercel 함수 로그의 `trial parse failed` 스택 확인. 파일은 저장하지 않으므로 사용자에게 받아 로컬(§5)에서 재현 |

## 7. 안내 문구

- 업로드 화면: "올린 파일은 처리 후 바로 삭제하고 저장하지 않아요"
- 푸터: "접속 IP는 하루 이용 횟수 확인에만 쓰이고, 되돌릴 수 없는 형태로 바꿔 보관해요." (IP는 비밀 salt와 날짜를 섞은 SHA-256 앞 16자로만 저장한다)
- 프리미엄 팝업 문구의 기능 약속(쪽수 제한 없음, AI 정밀 인식, 문항 수정 등)이 설치형 앱 실제 기능과 맞는지 출시 전에 확인한다.

## 8. 박람회 전용 시연

- 주소: `/demo` (일반 체험판과 같은 배포·파서 사용). `/demo`·`/demo/` 요청은 `vercel.json`의 rewrite(`/demo/index.html`)로 정적 파일을 돌려준다. §1의 `public/`과 같은 경로라 함수를 거치지 않으며, 실제 인증·파싱은 `/api/demo/*`가 맡는다.
- 이번 행사: **2026-09-17 00:00 ~ 2026-09-19 18:00, 한국 시간**, 종료 시각은 포함하지 않는다.
- 비밀번호로 인증한 브라우저만 `/api/demo/parse`를 호출할 수 있다. IP가 바뀌어도 인증 쿠키는 유지된다.
- 시연은 IP별 3회·일반 전체 500회 차감과 Turnstile을 건너뛴다. 일반 `/api/parse`는 시연 쿠키가 있어도 기존 정책을 지킨다.
- 4페이지·4MB·PDF 복잡도·응답 크기·인스턴스당 작업 제한은 동일하다. 칠판용 미리보기 토글도 같다. 시연도 일반 트래픽과 컴퓨팅 자원을 공유하므로 전용 처리 용량을 보장하지 않는다.
- 비밀번호 확인 요청은 IP별 분당 5회·인스턴스 전체 분당 30회로 제한한다. 이미 인증한 뒤 반복 파싱하는 횟수에는 이 제한이 적용되지 않는다.
- Supabase 한도 저장소 장애로 시연 파싱을 막지 않는다. 이벤트는 기존처럼 최선 노력으로 기록하며 `timing.demo = true`로 구분한다.

Production 환경변수. **다섯 값이 모두 올바른 형식으로 설정돼야 시연이 열린다** — `TRIAL_DEMO_ENABLED=1`이어도 나머지 넷 중 하나라도 비어 있거나 형식이 틀리면 시연은 꺼진 상태로 남고 일반 체험판 `/api/parse`는 그대로 동작한다:

| 이름 | 역할 | 이번 행사 설정 |
|---|---|---|
| `TRIAL_DEMO_ENABLED` | 시연 기능 전체 스위치 | `1` (기본은 꺼짐) |
| `TRIAL_DEMO_STARTS_AT` | 시연 시작 시각(ISO 8601, 타임존 포함) | `2026-09-17T00:00:00+09:00` |
| `TRIAL_DEMO_ENDS_AT` | 시연 종료 시각(ISO 8601, 타임존 포함). 시작~종료 간격이 72시간을 넘으면 설정 오류로 처리한다 | `2026-09-19T18:00:00+09:00` |
| `TRIAL_DEMO_PASSWORD_HASH` | 시연 비밀번호의 PBKDF2-SHA256 해시(원문은 어디에도 저장하지 않는다) | `.venv/bin/python scripts/hash_trial_demo_password.py`로 비밀번호를 비공개 입력해 만든 해시 |
| `TRIAL_DEMO_SESSION_SECRET` | 로그인 세션 쿠키 서명용 비밀값(최소 32자 미만이면 설정 오류) | 충분히 긴 임의 비밀값, 서버 환경변수에만 보관 |

비밀번호 원문·해시·세션 비밀값은 저장소, URL, 브라우저 저장소에 넣지 않는다. 브라우저에는 서명된 HttpOnly·SameSite=Strict 쿠키만 저장되며 HTTPS에서는 Secure도 적용한다. 시작 전과 종료 시각 이후에는 새 로그인과 파싱 모두 거절한다. 기간은 최대 72시간이며 설정 누락·잘못된 시간창은 시연만 닫고 일반 체험판을 유지한다.

환경변수 변경은 **재배포 후** 운영 주소에 반영된다. `TRIAL_DEMO_ENABLED=0`으로 바꾸고 재배포하면 운영 주소의 시연이 닫힌다. 비밀번호 해시·시간창·세션 비밀값을 바꾸고 재배포하면 기존 인증 쿠키가 무효화된다. 이전 배포 고유 주소가 외부에 공유됐다면 그 배포도 보호/삭제해야 한다.

검증:
1. `/api/demo/config`의 `active`·`authenticated`·`ends_at` 확인(비밀값은 반환하지 않음).
2. 행사 전에는 로그인 폼이 닫히고, 기간 중에는 비밀번호 입력 후 업로드가 보이는지 확인.
3. 같은 브라우저에서 4회 이상 처리·새로고침·로그아웃 확인. 일반 체험판의 제한은 그대로인지 확인.
4. 종료 시각에는 쿠키가 남아 있어도 API가 401을 반환하는지 확인. 종료 전에 시작한 처리 작업은 완료될 수 있으나 새 파싱 작업은 시작하지 않는다.

로컬 실행 스크립트도 `TRIAL_DEMO_*` 환경변수를 읽지만 Supabase와 운영 Turnstile은 연결하지 않는다. 실제 운영 시간창을 바꿔 사전 테스트하지 말고, 테스트 서버의 주입 가능한 시계를 이용한다.
