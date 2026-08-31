# ClassIn EDB report collector

`reports.classin.cloud`에서 EDB 메이크의 개인정보 제거된 버그 리포트를 받는
Cloudflare Worker입니다. 신고 메타데이터는 D1 `bug_reports` 테이블에 저장합니다.
원본 시험지, 세션 JSON, API 키, 전체 로컬 경로는 앱에서 전송하지 않습니다.
회신 연락처는 사용자가 직접 입력하고 연락 동의한 경우에만 저장합니다.

## Endpoints

- `GET /health`
- `POST /v1/edb-reports`

`/health`는 `REPORTS_DB`, `REPORT_RATE_LIMITER`뿐 아니라 storage schema v3의
17개 필수 컬럼과 `payload_hash` partial unique index를 읽기 전용으로 검사합니다.
구버전 스키마에 새 Worker가 연결되면 ready를 반환하지 않습니다.

## 안전한 배포

의존성은 lockfile과 Wrangler `4.125.0`으로 고정되어 있습니다.

```bash
npm ci
npm run check
npm run verify:pre-migration:remote
npm run deploy
```

`npm run deploy` 하나만 실행해도 `predeploy`에서 `npm run check`를 먼저 수행합니다.
그 뒤 다음 순서를 중간 단계마다 fail-closed로 실행합니다.

1. D1 migration ledger와 실제 스키마가 같은 단계인지 읽기 전용으로 확인
2. Wrangler 설정의 D1 UUID가 실제 대상과 일치하고 Time Travel bookmark를 발급할 수 있는지 확인
3. 변경 직전 Time Travel bookmark 확보 및 출력
4. 현재 Worker version ID 확보 및 출력
5. `0002`, `0003`을 Wrangler migration ledger 순서로 적용
6. 최종 D1 schema v3 검증
7. Worker 배포
8. 요청별 5초 timeout과 재시도 간격을 포함해 최대 약 65초 동안 `/health`의 binding·schema·report contract 검증

검증 또는 마이그레이션이 실패하면 다음 단계로 진행하지 않습니다. 기존
`predeploy`처럼 최종 스키마가 먼저 있어야만 deploy 명령 자체가 시작되는 순환
조건은 없습니다. `predeploy`는 로컬 테스트와 빌드만 담당하고, 원격 preflight와
최종 schema gate는 배포 오케스트레이터가 올바른 순서에 실행합니다.

Worker는 배포하지 않고 D1만 안전한 절차로 갱신하려면 다음을 실행합니다.

```bash
npm run deploy:migrations-only
```

직접 `wrangler deploy` 또는 직접 `wrangler d1 migrations apply --remote`를 실행하면
bookmark, 단계별 gate, rollback target 확보를 우회하므로 운영에서는 사용하지
않습니다.

## 마이그레이션 재실행과 drift

Cloudflare D1은 migration 파일 하나가 실패하면 그 파일을 rollback하고, 앞서
성공한 migration은 ledger에 남깁니다. 따라서 다음 실행은 성공한 파일을 건너뛰고
실패한 파일부터 재시도합니다. `0002` 성공 후 `0003` 실패 상태도 정상 prefix로
간주합니다.

반대로 다음 상태는 자동 복구하지 않고 preflight에서 차단합니다.

- ledger에는 `0001`만 있는데 `0002` 컬럼 일부가 수동으로 존재함
- ledger에는 적용됐다고 기록됐지만 대응 컬럼·인덱스가 없음
- migration 순서가 prefix가 아니거나 모르는 migration이 존재함
- `bug_reports`가 있지만 `d1_migrations` ledger가 없음
- 이미 배포된 SQL 파일의 내용이 `migrations/manifest.json` SHA-256과 다름

이 상태에서 `d1_migrations`를 직접 수정하거나 기존 SQL을 고치지 않습니다. 먼저
원인을 확인하고, 변경 전 bookmark로 복원하거나 별도의 검토된 복구 migration을
추가합니다. 기존 migration 파일은 불변입니다.

로컬 통합 테스트는 실제 임시 D1에서 `0001 → 0002/0003 → 재실행 no-op`을 수행하고
기존 신고 행 보존 및 health schema probe의 `0 → 1` 전환을 검증합니다.

```bash
npm test
npm run test:migrations
npm run build:check
```

## 장애 시 rollback / Time Travel

배포 로그의 다음 두 값을 반드시 보관합니다.

- `Pre-migration D1 bookmark`
- `Previous Worker version`

Worker 코드나 binding만 문제이고 D1 v3가 정상이라면 D1은 그대로 두고 Worker만
되돌립니다. `0002/0003`은 기존 Worker에 additive이므로 이 경로가 데이터 손실이
없는 우선 rollback입니다.

```bash
npx wrangler rollback <PREVIOUS_WORKER_VERSION> --message "rollback failed reports deployment"
```

D1 자체를 복원해야 한다면 먼저 Worker를 이전 version으로 되돌립니다. 그 다음
bookmark 이후 접수된 신고를 조회·보존할지 판단한 뒤에만 다음 명령을 실행합니다.
Time Travel restore는 DB를 제자리에서 덮어쓰고 진행 중 쿼리를 취소하므로
destructive operation입니다.

```bash
npx wrangler d1 time-travel restore classin-edb-reports --bookmark=<PRE_MIGRATION_BOOKMARK>
```

restore 출력의 `previous bookmark`도 기록합니다. 잘못 복원했을 때 이 bookmark로
restore를 되돌릴 수 있습니다. Time Travel 보존 기간은 요금제에 따라 7일 또는
30일이므로 장애 판단을 미루지 않습니다.

참고: [D1 migrations](https://developers.cloudflare.com/d1/reference/migrations/),
[Wrangler D1 commands](https://developers.cloudflare.com/workers/wrangler/commands/d1/),
[D1 Time Travel](https://developers.cloudflare.com/d1/reference/time-travel/),
[Workers rollback](https://developers.cloudflare.com/workers/wrangler/commands/workers/#rollback)

## 운영 정보

- Worker: `classin-edb-reports`
- Custom Domain: `https://reports.classin.cloud`
- D1 database: `classin-edb-reports`
- D1 binding: `REPORTS_DB`
- Rate Limit binding: `REPORT_RATE_LIMITER` (IP별 분당 20회)

운영 상태를 읽기 전용으로 확인합니다.

```bash
npm run verify:deploy:remote
```

리포트 확인 쿼리:

```sql
SELECT id, created_at, app_version, platform, description, error_code,
       failed_operation, reporter_contact, status, resolution_note, resolved_at
FROM bug_reports
ORDER BY created_at DESC
LIMIT 100;
```

동일 payload는 key 순서를 정규화한 SHA-256 hash와 D1 unique index로 한 번만
저장됩니다. 같은 payload 재전송은 기존 접수번호와 `duplicate: true`를 반환합니다.
Rate Limit binding이 없거나 실패하면 D1에 쓰지 않고
`503 rate_limiter_unavailable`로 fail-closed합니다.
