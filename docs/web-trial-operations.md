# 웹 체험판 운영 문서

- 대상: `web-trial` 브랜치의 공개 문제 파서 체험판
- 설계: `docs/superpowers/specs/2026-09-15-problem-parser-web-trial-design.md`
- 최종 확인일: 2026-09-15 (외부 서비스 설정 화면 이름은 바뀔 수 있다)

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
   ```
4. Project Settings > API Keys에서 **secret key**(`sb_secret_...`)를 만든다. 이 키는 Vercel 환경변수에만 넣고 채팅·저장소·`.env.local`에 넣지 않는다. publishable key는 이 체험판에서 쓰지 않는다.

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
| Ignored Build Step (Custom) | `if [ "$VERCEL_GIT_COMMIT_REF" = "web-trial" ]; then exit 1; else exit 0; fi` |
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
| `TRIAL_MAX_BYTES` / `TRIAL_MAX_PAGES` / `TRIAL_MAX_SOURCE_PAGES` | 기본 4000000 / 3 / 100 | 아니오 |
| `TRIAL_PARSE_CONCURRENCY` / `TRIAL_PARSE_WAIT_SECONDS` | 기본 2 / 20 (인스턴스당) | 아니오 |

**필수 6개 중 하나라도 비어 있으면 운영의 `/api/parse`는 503만 돌려준다.** 설정이 덜 된 채 배포돼도 파싱은 열리지 않는다. 환경변수를 바꾸면 재배포해야 반영된다.

### 2-4. 스파이크 정리

Vercel 측정(`docs/web-trial-spike-results.md`)이 끝나면 `TRIAL_SPIKE_TOKEN` 환경변수를 지우고 `/api/spike` 코드를 제거한다.

## 3. 배포 후 점검

```bash
DOMAIN=https://trial.example.com

curl -s $DOMAIN/api/health
# {"status":"ok","commit":"<7자리>","ready":true}   ← ready가 false면 §2-3 필수 환경변수 확인

curl -s $DOMAIN/api/config
# turnstile_site_key가 운영 키인지, inquiry_url이 맞는지

curl -sI $DOMAIN/ | grep -i content-security-policy
# vercel.json의 CSP가 붙어 있는지

curl -s -H "Authorization: Bearer $CRON_SECRET" $DOMAIN/api/cron/daily
# {"ok":true}
```

브라우저에서:
1. 텍스트 PDF(모의고사 원본)를 올려 결과·박스·카드가 보이는지, 3쪽 넘는 파일이면 "앞 3쪽까지" 배너가 뜨는지 본다.
2. 사진(JPG)을 골라 "스캔본·사진은 프리미엄 AI 인식으로" 팝업이 뜨는지 본다.
3. 팝업의 [프리미엄 도입 문의]가 `classin.co.kr/contact`로 열리는지 본다.
4. Supabase에서 이벤트가 쌓였는지 본다:
   ```sql
   select kind, status, reject_code, feature, action, created_at from public.trial_events order by id desc limit 10;
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

p95가 20초를 넘으면 `TRIAL_MAX_PAGES`를 줄이거나 Function CPU를 Performance로 올린다.

### 4-4. 보존

매일 00:10 KST 크론이 `trial_quota` 7일, `trial_events` 180일 지난 행을 지운다. 크론은 재시도하지 않으므로 하루 빠져도 다음 날 함께 정리된다.

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
