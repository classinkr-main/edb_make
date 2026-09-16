# 웹 체험판 Plan 3 — 측정 (관측 · 코퍼스 · 프로브 · 부하 · 체험판 전용 속도 레버)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 배포가 막힌 체험판을 복구하고, 운영·프리뷰·로컬에서 "어디가 느린지, 부하에서 어떻게 분배되는지, 문항 인식이 얼마나 맞는지"를 숫자로 낼 수 있는 측정 틀을 만든다. 끝나면 부하 분배 결과, 코퍼스 기준값, 판정 대기 목록이 나오고 Plan 4(개선)를 그 숫자로 쓴다.

**Architecture:** 서버는 단계별 시간·인스턴스·거절 사유·복잡도를 `trial_events`와 응답 JSON에 남긴다. 파이프라인 두 함수에 선택적 `timings` dict를 넘겨 단계 시간을 받는다(기본 `None`, 데스크톱 동작 불변). 측정 스크립트는 `scripts/trial_bench/`에 두고 시험지·관측 JSON·라벨은 저장소 밖 `~/edb-trial-bench/`에 둔다. 오라클은 체험판과 같은 `parse_problems`에 `ocr_mode="auto"`, `ai_fallback_config={"mode": "force", ...}`만 다르게 넣어 좌표계와 어댑터를 공유한다. 프로브·부하는 Vercel 프리뷰 배포(`web-trial-bench` 브랜치)에 자동화 우회 헤더로 붙는다.

**Tech Stack:** Python 3.12(Vercel)/3.14(로컬), FastAPI TestClient, PyMuPDF, Pillow, Supabase Postgres(PostgREST), 로컬 PostgreSQL 17(SQL 테스트), Cloudflare Turnstile, Vercel Pro(프리뷰·자동화 우회), Gemini(오라클 전용)

**Spec:** `docs/superpowers/specs/2026-09-15-web-trial-quality-speed-load-design.md`
**선행:** Plan 1·2 완료(`web-trial` @ 3a82cb8). 다른 세션이 같은 워크트리를 쓴다.

**같은 워크트리를 쓰는 다른 세션과의 규칙:** 커밋 전 `git status`로 남의 변경을 확인하고 `git add <파일>`로만 스테이지한다. `git add -A`·stash·reset·force push 금지. 푸시는 사용자가 한다(푸시 = 운영 배포).

**실행 환경:** 모든 명령은 워크트리 루트 `/Users/clmagi/Desktop/Projects/edb_mak/.claude/worktrees/web-trial`에서 `.venv`로 실행하고 `GEMINI_API_KEY=`를 앞에 붙인다(테스트가 네트워크·AI를 쓰지 않게). 오라클 스크립트(Task 9)만 예외다.

**모델 배분:** Task 1~8·10~13은 Sonnet, Task 4(공용 파이프라인 훅)와 Task 9(파서 인자 추가)는 Sonnet 구현 + Fable 리뷰, Task 14는 사용자, Task 15·16 실행은 Haiku, 결과 판단은 Fable.

---

## 파일 구조

| 파일 | 상태 | 책임 |
|---|---|---|
| `supabase/migrations/20260915000000_web_trial.sql` | 수정 | `trial_events`에 `timing jsonb`, `instance_id text`, `reject_detail text`, `complexity jsonb` |
| `trial_input.py` | 수정 | `TrialRejected.detail`, `reject(code, detail)`, `page_too_complex`, `InputLimits` 복잡도 상한 |
| `trial_config.py` | 수정 | `TRIAL_MAX_WORDS_PER_PAGE`, `TRIAL_MAX_DRAWINGS_PER_PAGE` |
| `problem_parser.py` | 수정 | `MAX_IMAGE_PIXELS`, `PdfInfo` 복잡도, 세부 timing 키, `ocr_mode`·`ai_fallback_config` 인자 |
| `build_problem_board_edb.py` | 수정 | `build_pages(timings=)`, `build_problem_entries(timings=)`, `_record_ms` |
| `trial_preview.py` | 수정 | `extra` 병합, `build_parse_body`(직렬화 1회), `reducing_gap` 축소 |
| `trial_server.py` | 수정 | 인스턴스 id, 단계 시간, 거절 사유, 복잡도 기록, 응답 필드 |
| `scripts/trial_bench/__init__.py`, `common.py` | 신규 | 경로, 관측 JSON, 백분위·표 헬퍼 |
| `scripts/trial_bench/make_inputs.py`, `observe.py`, `oracle.py` | 신규 | 입력 압축본, 체험판 관측, 오라클 관측 |
| `scripts/trial_bench/score.py`, `adjudicate.py` | 신규 | 채점·보고, 불일치 비교 이미지·라벨 뼈대 |
| `scripts/trial_bench/probe.py`, `load.py`, `complexity.py`, `memory.py` | 신규 | 프리뷰 프로브, 동시 요청, 복잡도 대 시간, 메모리 |
| `test_trial_sql.py`, `test_trial_input.py`, `test_trial_config.py`, `test_problem_parser.py`, `test_trial_preview.py`, `test_trial_api.py` | 수정 | 위 변경의 테스트 |
| `test_trial_bench.py` | 신규 | 관측 어댑터·채점·라벨 뼈대·백분위 테스트 |
| `docs/web-trial-operations.md` | 수정 | 프리뷰·우회 비밀·새 환경변수·SQL |
| `docs/web-trial-load.md`, `docs/web-trial-quality.md` | 신규 | 부하·복잡도·메모리 표, 코퍼스 표 |
| `docs/web-trial-spike-results.md` | 수정 | §4 프로브 결과 |

---

### Task 1: `trial_events` 열 추가 (마이그레이션)

**Files:**
- Modify: `supabase/migrations/20260915000000_web_trial.sql:42-47`
- Test: `test_trial_sql.py`

- [ ] **Step 1: 실패하는 테스트 추가**

`test_trial_sql.py`의 테스트 클래스 끝에 추가한다(클래스 이름은 파일의 `class ... (unittest.TestCase)`를 따른다):

```python
    def test_events_accept_timing_instance_detail_and_complexity(self):
        self.psql(
            "insert into public.trial_events (kind, status, reject_code, reject_detail, instance_id, timing, complexity) "
            "values ('parse', 503, 'busy', 'slot_wait', 'abcd1234', "
            "'{\"render\": 120, \"segment\": 300}'::jsonb, '{\"words\": 674, \"drawings\": 191}'::jsonb);",
            role="service_role",
        )
        out = self.psql(
            "select reject_detail, instance_id, timing->>'render', complexity->>'words' from public.trial_events;",
            role="service_role",
        ).stdout.strip()
        self.assertEqual("slot_wait|abcd1234|120|674", out)

    def test_migration_is_rerunnable_with_new_columns(self):
        self.psql(MIGRATION.read_text(encoding="utf-8"))
        out = self.psql(
            "select column_name from information_schema.columns "
            "where table_name = 'trial_events' and column_name in ('timing', 'instance_id', 'reject_detail', 'complexity');"
        ).stdout
        for column in ("timing", "instance_id", "reject_detail", "complexity"):
            self.assertIn(column, out)
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_sql.py -k "timing_instance or rerunnable"`
Expected: FAIL — `column "reject_detail" of relation "trial_events" does not exist` (PostgreSQL 바이너리가 없으면 skip 되므로 `TRIAL_PG_BIN`을 확인한다)

- [ ] **Step 3: 마이그레이션에 열 추가**

`alter table public.trial_events alter column ... type integer;` 블록 바로 뒤에 넣는다:

```sql
-- 2026-09-15 quality/load design: stage timings, instance id, busy reason, page complexity.
alter table public.trial_events
  add column if not exists timing jsonb,
  add column if not exists instance_id text,
  add column if not exists reject_detail text,
  add column if not exists complexity jsonb;
```

- [ ] **Step 4: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_sql.py`
Expected: 모두 PASS (기존 18개 + 2개)

- [ ] **Step 5: 커밋**

```bash
git add supabase/migrations/20260915000000_web_trial.sql test_trial_sql.py
git commit -m "feat: record stage timing, instance, busy reason, and complexity in trial events"
```

---

### Task 2: `busy` 사유 기록 (`reject_detail`)

**Files:**
- Modify: `trial_input.py:24-33`
- Modify: `trial_server.py` (`parse` 핸들러, `rejection_response`)
- Test: `test_trial_input.py`, `test_trial_api.py`

- [ ] **Step 1: 실패하는 테스트 추가**

`test_trial_input.py`에 추가:

```python
    def test_reject_carries_optional_detail(self):
        rejected = reject("busy", "slot_wait")
        self.assertEqual("busy", rejected.rejection.code)
        self.assertEqual("slot_wait", rejected.detail)
        self.assertIsNone(reject("busy").detail)
```

`test_trial_api.py`의 `TrialApiCase` 하위 테스트 클래스에 추가(파일의 기존 클래스 중 `make_client`를 쓰는 클래스에 넣는다):

```python
    def test_not_ready_records_reason(self):
        client = self.make_client(config=TrialConfig(production=True, ip_salt="salt", cron_secret="cron-secret"))
        response = self.post_pdf(client)
        self.assertEqual(503, response.status_code)
        self.assertEqual("not_ready", self.store.events[-1]["reject_detail"])

    def test_turnstile_outage_records_reason_and_does_not_charge(self):
        client = self.make_client(verifier=FakeVerifier(error=TurnstileUnavailable("down")))
        response = self.post_pdf(client)
        self.assertEqual(503, response.status_code)
        self.assertEqual("turnstile", self.store.events[-1]["reject_detail"])
        self.assertEqual(0, self.used())

    def test_quota_store_outage_records_reason(self):
        client = self.make_client(store=CommitThenTimeoutStore())
        response = self.post_pdf(client)
        self.assertEqual(503, response.status_code)
        self.assertEqual("quota_store", self.store.events[-1]["reject_detail"])

    def test_global_limit_records_reason(self):
        client = self.make_client(config=TrialConfig(ip_salt="salt", cron_secret="cron-secret", global_daily_limit=1))
        self.assertEqual(200, self.post_pdf(client, ip="203.0.113.1").status_code)
        response = self.post_pdf(client, ip="203.0.113.2")
        self.assertEqual(503, response.status_code)
        self.assertEqual("global_limit", self.store.events[-1]["reject_detail"])

    def test_slot_wait_timeout_records_reason_and_does_not_charge(self):
        gate = threading.Event()
        client = self.make_client(
            config=TrialConfig(ip_salt="salt", cron_secret="cron-secret", parse_concurrency=1, parse_wait_seconds=0.3),
            parser=FakeParser(gate=gate),
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.post_pdf, client, ip="203.0.113.1")
            time.sleep(0.2)  # let the first request take the only slot
            second = self.post_pdf(client, ip="203.0.113.2")
            # Check right away: the still-blocked first request only finishes (and
            # records its own event) after gate.set() below, which would otherwise
            # shadow the busy event we're checking for here.
            self.assertEqual(503, second.status_code)
            self.assertEqual("slot_wait", self.store.events[-1]["reject_detail"])
            self.assertEqual(0, self.used("203.0.113.2"))
            gate.set()
            self.assertEqual(200, first.result().status_code)

    def test_parser_crash_stays_charged_and_has_no_detail(self):
        client = self.make_client(parser=FakeParser(error=RuntimeError("boom")))
        response = self.post_pdf(client)
        self.assertEqual(500, response.status_code)
        self.assertEqual("parse_failed", response.json()["error"]["code"])
        self.assertEqual(1, self.used())  # parsing started, so the use stays charged (spec §6)
        self.assertIsNone(self.store.events[-1]["reject_detail"])

    def test_success_and_ordinary_rejections_have_no_detail(self):
        client = self.make_client()
        self.post_pdf(client)
        self.assertIsNone(self.store.events[-1]["reject_detail"])
        self.post_pdf(client, body=b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        self.assertEqual("image_not_supported", self.store.events[-1]["reject_code"])
        self.assertIsNone(self.store.events[-1]["reject_detail"])
```

`FakeParser(gate=...)`는 파일의 기존 클래스다: `gate`가 주어지면 `__call__`이 `gate.wait()`로 막힌다. 동작이 다르면 그 클래스의 구현을 따라 테스트를 맞춘다.

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_input.py test_trial_api.py -k "detail or reason"`
Expected: FAIL — `TypeError: reject() takes 1 positional argument but 2 were given`, `KeyError: 'reject_detail'`

- [ ] **Step 3: `trial_input.py` 수정**

```python
class TrialRejected(Exception):
    def __init__(self, rejection: Rejection, detail: str | None = None) -> None:
        super().__init__(rejection.code)
        self.rejection = rejection
        # Why a shared code was refused (busy: not_ready | turnstile | quota_store | global_limit | slot_wait).
        self.detail = detail


def reject(code: str, detail: str | None = None) -> TrialRejected:
    return TrialRejected(REJECTIONS[code], detail)
```

(`reject`는 파일 아래쪽에 있다. 위치는 그대로 두고 시그니처만 바꾼다.)

- [ ] **Step 4: `trial_server.py` 수정**

`event` 초기 dict에 `"reject_detail": None,`을 넣고, `rejection_response`를 바꾼다:

```python
    def rejection_response(rejection: Any, event: dict[str, Any], detail: str | None = None) -> JSONResponse:
        event.update(status=rejection.status, reject_code=rejection.code, reject_detail=detail)
        extra = {"remaining_today": 0} if rejection.code == "daily_limit" else {}
        return JSONResponse(rejection.payload(**extra), status_code=rejection.status, headers=NO_STORE)
```

`parse` 핸들러에서 `busy`를 올리는 다섯 곳에 사유를 넣는다:

```python
            if not ready():
                raise reject("busy", "not_ready")
```
```python
                except TurnstileUnavailable as error:
                    logger.warning("turnstile unavailable: %s", error)
                    raise reject("busy", "turnstile") from error
```
```python
                if not await acquire_parse_slot():
                    raise reject("busy", "slot_wait")
```
```python
                    except QuotaUnavailable as error:
                        logger.warning("quota unavailable: %s", error)
                        await run_in_threadpool(refund_quietly, request_id)
                        raise reject("busy", "quota_store") from error
                    if not decision.allowed:
                        if decision.reason == "ip":
                            raise reject("daily_limit")
                        raise reject("busy", "global_limit")
```

`except TrialRejected as rejected:` 분기는 `rejection_response(rejected.rejection, event, rejected.detail)`로 바꾼다. 예상 밖 예외 분기(`REJECTIONS["parse_failed"]`)는 그대로 둔다.

- [ ] **Step 5: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_input.py test_trial_api.py`
Expected: 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add trial_input.py trial_server.py test_trial_input.py test_trial_api.py
git commit -m "feat: record why the trial answered busy"
```

---

### Task 3: 인스턴스 id · 단계 시간 · 응답 필드

**Files:**
- Modify: `trial_preview.py:66-110` (`_payload_for_step`, `build_parse_payload`)
- Modify: `trial_server.py` (모듈 상수, `parse_and_encode`, `parse`)
- Test: `test_trial_preview.py`, `test_trial_api.py`

- [ ] **Step 1: 실패하는 테스트 추가**

`test_trial_preview.py`:

```python
    def test_extra_fields_are_merged_into_the_payload(self):
        payload = build_parse_payload(
            _result(),
            remaining_today=2,
            elapsed_ms=10,
            processed_page_limit=3,
            extra={"timing_ms": {"render": 1}, "instance_id": "abcd1234", "instance_age_s": 12.5},
        )
        self.assertEqual({"render": 1}, payload["timing_ms"])
        self.assertEqual("abcd1234", payload["instance_id"])
        self.assertEqual(12.5, payload["instance_age_s"])
        self.assertNotIn("timing_ms", build_parse_payload(_result(), remaining_today=2, elapsed_ms=10, processed_page_limit=3))
```

`test_trial_api.py`:

```python
    def test_response_and_event_carry_timing_and_instance(self):
        client = self.make_client()
        response = self.post_pdf(client)
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual({"total": 5}, payload["timing_ms"])
        self.assertRegex(payload["instance_id"], r"^[0-9a-f]{8}$")
        self.assertGreaterEqual(payload["instance_age_s"], 0)
        event = self.store.events[-1]
        self.assertEqual(payload["instance_id"], event["instance_id"])
        self.assertEqual(5, event["timing"]["total"])
        for key in ("encode", "parse_total"):
            self.assertIsInstance(event["timing"][key], int)
            self.assertGreaterEqual(event["timing"][key], 0)

    def test_rejections_still_carry_instance_id(self):
        client = self.make_client(config=TrialConfig(production=True, ip_salt="salt", cron_secret="cron-secret"))
        self.post_pdf(client)
        self.assertRegex(self.store.events[-1]["instance_id"], r"^[0-9a-f]{8}$")
        self.assertIsNone(self.store.events[-1]["timing"])
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_preview.py test_trial_api.py -k "extra or timing or instance"`
Expected: FAIL — `TypeError: build_parse_payload() got an unexpected keyword argument 'extra'`, `KeyError: 'timing_ms'`

- [ ] **Step 3: `trial_preview.py` 수정**

`_payload_for_step`에 `extra` 인자를 더하고 마지막에 병합한다:

```python
def _payload_for_step(
    result: ParseResult,
    step_index: int,
    *,
    remaining_today: int,
    elapsed_ms: int,
    processed_page_limit: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step = PREVIEW_STEPS[step_index]
    payload: dict[str, Any] = {
        "parser_version": result.parser_version,
        "elapsed_ms": elapsed_ms,
        "source_page_count": result.source_page_count,
        "processed_page_count": len(result.pages),
        "processed_page_limit": processed_page_limit,
        "remaining_today": remaining_today,
        "preview_step": step_index,
        "pages": [
            {
                "page_id": page.page_id,
                "index": page.index,
                "width": page.width,
                "height": page.height,
                "preview": encode_jpeg_data_uri(page.image, long_side=step.page_long_side, quality=step.quality),
            }
            for page in result.pages
        ],
        "problems": [
            {
                "problem_id": problem.problem_id,
                "number": problem.number,
                "title": problem.title,
                "regions": [
                    {"page_id": region.page_id, "bbox": bbox}
                    for region in problem.regions
                    if (bbox := _finite_bbox(region)) is not None
                ],
                "risk_flags": list(problem.risk_flags),
                "needs_review": needs_review(problem.risk_flags),
                "preview": encode_jpeg_data_uri(problem.image, long_side=step.problem_long_side, quality=step.quality),
            }
            for problem in result.problems
        ],
    }
    if extra:
        payload.update(extra)
    return payload
```

(`pages`·`problems` 부분은 지금 코드와 같다. 바뀌는 것은 `return {`가 `payload: dict[str, Any] = {`가 되고 끝에 `extra` 병합이 붙는 것뿐이다.)

`build_parse_payload`에도 `extra: dict[str, Any] | None = None`을 더해 `_payload_for_step(..., extra=extra)`로 넘긴다. 시그니처:

```python
def build_parse_payload(
    result: ParseResult,
    *,
    remaining_today: int,
    elapsed_ms: int,
    processed_page_limit: int,
    budget_bytes: int = RESPONSE_BUDGET_BYTES,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
```

- [ ] **Step 4: `trial_server.py` 수정**

모듈 상수(`logger` 아래):

```python
# One id per Python process: shows instance churn and cold starts in trial_events.
INSTANCE_ID = uuid.uuid4().hex[:8]
INSTANCE_STARTED_AT = time.time()


def _ms(started_at: float) -> int:
    return int(round((time.perf_counter() - started_at) * 1000))
```

`parse_and_encode`:

```python
    def parse_and_encode(
        source: Path, work_dir: Path, remaining_today: int, started_at: float
    ) -> tuple[bytes, dict[str, int], dict[str, int]]:
        parse_started_at = time.perf_counter()
        try:
            result = parser(source, work_dir=work_dir, max_pages=config.limits.max_pages)
            timing: dict[str, int] = dict(result.timing_ms)
            encode_started_at = time.perf_counter()
            payload = build_parse_payload(
                result,
                remaining_today=remaining_today,
                elapsed_ms=_ms(started_at),
                processed_page_limit=config.limits.max_pages,
                extra={
                    "timing_ms": dict(timing),
                    "instance_id": INSTANCE_ID,
                    "instance_age_s": round(time.time() - INSTANCE_STARTED_AT, 1),
                },
            )
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            # encode is measured after the body exists, so the response cannot include it; the event does.
            timing["encode"] = _ms(encode_started_at)
            timing["parse_total"] = _ms(parse_started_at)
        except Exception as error:
            logger.exception("trial parse failed")
            raise reject("parse_failed") from error
        counts = {
            "pages": len(payload["pages"]),
            "problems": len(payload["problems"]),
            "risk_problems": sum(1 for problem in payload["problems"] if problem["needs_review"]),
        }
        return body, counts, timing
```

`parse` 핸들러: `event` 초기 dict에 `"timing": None, "instance_id": INSTANCE_ID, "complexity": None,`을 넣고, 호출부를 바꾼다:

```python
                    body, counts, timing = await run_in_threadpool(
                        parse_and_encode, source, Path(temp_dir) / "work", decision.remaining, started_at
                    )
                finally:
                    parse_slots.release()
            event.update(status=200, timing=timing, **counts)
```

`/api/health`에 인스턴스 정보를 더한다(비밀 아님, 운영 점검용):

```python
        return JSONResponse(
            {
                "status": "ok",
                "commit": parser_version(),
                "ready": ready(),
                "instance_id": INSTANCE_ID,
                "instance_age_s": round(time.time() - INSTANCE_STARTED_AT, 1),
            },
            headers=NO_STORE,
        )
```

- [ ] **Step 5: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_preview.py test_trial_api.py test_trial_server.py`
Expected: 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add trial_preview.py trial_server.py test_trial_preview.py test_trial_api.py
git commit -m "feat: expose stage timing and instance id in trial responses and events"
```

---

### Task 4: 파이프라인 단계 시간 훅 (`timings` dict)

**Files:**
- Modify: `build_problem_board_edb.py:2608-2655` (`build_pages`), `:6017-6382` (`build_problem_entries`)
- Modify: `problem_parser.py:154-220` (`parse_problems`)
- Test: `test_problem_parser.py`

Fable 리뷰 대상: 공용 함수에 인자를 더한다. 기본값 `None`이면 어떤 경로도 바뀌지 않아야 한다.

- [ ] **Step 1: 실패하는 테스트 추가**

`test_problem_parser.py`의 `TestParseProblems`에 추가:

```python
    def test_timing_has_stage_breakdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4]])
            result = parse_problems(path, work_dir=root / "work")
        expected_keys = {"render", "segment", "recognize", "entries", "assets", "coalesce", "finish", "load", "crops", "total"}
        self.assertTrue(expected_keys <= set(result.timing_ms), result.timing_ms)
        for key in expected_keys:
            self.assertIsInstance(result.timing_ms[key], int)
            self.assertGreaterEqual(result.timing_ms[key], 0)
        self.assertLessEqual(result.timing_ms["render"] + result.timing_ms["segment"], result.timing_ms["recognize"] + 50)
        self.assertLessEqual(
            result.timing_ms["entries"] + result.timing_ms["assets"] + result.timing_ms["coalesce"] + result.timing_ms["finish"],
            result.timing_ms["crops"] + 50,
        )
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_problem_parser.py -k breakdown`
Expected: FAIL — `AssertionError: {'recognize': ..., 'crops': ..., 'total': ...}`

- [ ] **Step 3: `build_problem_board_edb.py`에 헬퍼와 훅 추가**

`_resolve_problem_asset_worker_count` 정의 위(모듈 어디든 함수 정의 전)에 헬퍼를 둔다:

```python
def _record_ms(timings: dict[str, int] | None, key: str, started_at: float) -> None:
    """Store a stage duration when the caller asked for timings; no-op otherwise."""
    if timings is not None:
        timings[key] = int(round((time.perf_counter() - started_at) * 1000))
```

`build_pages` 시그니처 끝에 `timings: dict[str, int] | None = None,`을 더하고 본문을 바꾼다:

```python
    render_started_at = time.perf_counter()
    prepared_pages = prepare_source_pages(
        source,
        pdf_dpi=pdf_dpi,
        detect_perspective=detect_perspective,
        deskew=deskew,
        crop_margins=crop_margins,
        max_dimension=max_dimension,
    )
    _record_ms(timings, "render", render_started_at)
    if _normalize_input_intent(input_intent) == "page-as-is":
        prepared_pages = _tile_page_as_is_prepared_pages(
            prepared_pages,
            page_tile_mode=page_tile_mode,
        )
        return prepared_pages, _build_page_as_is_models(prepared_pages, subject=subject)

    page_ai_config = _to_page_ai_config(ai_fallback_config)
    segment_started_at = time.perf_counter()
    page_models = build_page_models_for_prepared_pages(
        prepared_pages,
        subject=subject,
        ocr_mode=ocr_mode,
        ai_config=page_ai_config,
        ocr_semaphore=ocr_semaphore,
        global_ocr_worker_limit=global_ocr_worker_limit,
    )
    _record_ms(timings, "segment", segment_started_at)
```

`build_problem_entries` 시그니처 끝(`render_board_assets: bool = True,` 뒤)에 `timings: dict[str, int] | None = None,`을 더하고, 본문 첫 줄에 `entries_started_at = time.perf_counter()`를 둔다. `rendered_crop_sizes = iter(...)` 부분을 다음으로 바꾼다:

```python
    _record_ms(timings, "entries", entries_started_at)
    assets_started_at = time.perf_counter()
    rendered_crop_sizes = iter(
        _render_problem_assets([draft.asset_task for draft in drafts if draft.asset_task is not None])
    )
    _record_ms(timings, "assets", assets_started_at)
    coalesce_started_at = time.perf_counter()
    crop_sizes = [
        draft.prepared_page.image.size if draft.asset_task is None else next(rendered_crop_sizes)
        for draft in drafts
    ]
    for draft in drafts:
        if draft.asset_task is not None:
            draft.preserve_media_regions = list(draft.asset_task.rendered_media_regions)
    drafts, crop_sizes = _coalesce_cross_page_passage_drafts(
        drafts,
        crop_sizes,
        pages,
        render_board_assets=render_board_assets,
    )
    _annotate_passage_crop_quality(drafts, pages)
    _record_ms(timings, "coalesce", coalesce_started_at)
    finish_started_at = time.perf_counter()
    entries: list[ProblemEntry] = []
```

함수의 `return entries` 직전에 `_record_ms(timings, "finish", finish_started_at)`를 넣는다(`return`이 여러 개면 모두).

- [ ] **Step 4: `problem_parser.parse_problems` 수정**

```python
    recognize_started_at = time.perf_counter()
    prepared_pages, page_models = build_pages(
        parse_source,
        subject=resolve_subject(subject),
        ocr_mode="none",
        ai_fallback_config=None,
        pdf_dpi=PDF_RENDER_DPI,
        detect_perspective=False,
        deskew=True,
        crop_margins=True,
        max_dimension=None,
        timings=timing_ms,
    )
    timing_ms["recognize"] = _elapsed_ms(recognize_started_at)

    crops_started_at = time.perf_counter()
    entries = build_problem_entries(
        prepared_pages,
        page_models,
        work_dir,
        LayoutTemplate(name="academy-default"),
        render_board_assets=False,
        timings=timing_ms,
    )
    load_started_at = time.perf_counter()
    problems = [
        ParsedProblem(
            problem_id=entry.problem_id,
            number=entry.problem_number,
            title=entry.title,
            regions=_problem_regions(entry),
            risk_flags=list(entry.risk_flags),
            image=_load_detached_rgb(entry.crop_path),
        )
        for entry in entries
    ]
    timing_ms["load"] = _elapsed_ms(load_started_at)
    timing_ms["crops"] = _elapsed_ms(crops_started_at)
```

- [ ] **Step 5: 통과 확인 + 전체 회귀**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_problem_parser.py`
Expected: PASS

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q -x`
Expected: 전체 PASS (Plan 2 기준 1382개 + 이번 추가분). 실패하면 훅이 기본 경로를 바꾼 것이므로 되돌린다.

- [ ] **Step 6: 커밋**

```bash
git add build_problem_board_edb.py problem_parser.py test_problem_parser.py
git commit -m "feat: report render, segment, entry, asset, and load timings from the parser"
```

---

### Task 5: 복잡도 계수·상한, `MAX_IMAGE_PIXELS`

**Files:**
- Modify: `problem_parser.py` (`PdfInfo`, `inspect_pdf`, 모듈 상단)
- Modify: `trial_input.py` (`REJECTIONS`, `InputLimits`, `check_pdf_info`)
- Modify: `trial_config.py` (`InputLimits` 생성 두 곳, `from_env`)
- Modify: `trial_server.py` (`event["complexity"]`)
- Test: `test_problem_parser.py`, `test_trial_input.py`, `test_trial_config.py`, `test_trial_api.py`

- [ ] **Step 1: 실패하는 테스트 추가**

`test_problem_parser.py`:

```python
class TestImageLimits(unittest.TestCase):
    def test_decompression_bomb_limit_is_set_on_import(self):
        from PIL import Image

        import problem_parser  # noqa: F401  (import side effect under test)

        self.assertEqual(20_000_000, Image.MAX_IMAGE_PIXELS)
```

`TestInspectPdf`에 추가:

```python
    def test_counts_words_and_drawings_per_page(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_text_exam_pdf(Path(temp_dir) / "exam.pdf", [[1, 2], [3]])
            doc = fitz.open(path)
            doc[0].draw_rect(fitz.Rect(10, 10, 100, 100), color=(0, 0, 0))
            doc.saveIncr()
            doc.close()
            info = inspect_pdf(path, max_pages=3)
        self.assertGreaterEqual(info.max_words_per_page, 6)  # "1. problem stem" + choices on the fuller page
        self.assertGreaterEqual(info.max_drawings_per_page, 1)
```

`test_trial_input.py`(상단 import에 `A3_AREA_PT`, `InputLimits`, `REJECTIONS`, `TrialRejected`, `check_pdf_info`가 없으면 더한다):

```python
    def test_rejects_pages_with_too_many_words_or_drawings(self):
        limits = InputLimits(max_bytes=4_000_000, max_pages=3, max_source_pages=100, max_page_area_pt=2 * A3_AREA_PT, max_words_per_page=100, max_drawings_per_page=50)
        base = dict(page_count=3, scanned_pages=3, pages_without_text=0, max_page_area_pt=500_000.0)
        with self.assertRaises(TrialRejected) as too_many_words:
            check_pdf_info(PdfInfo(**base, max_words_per_page=101, max_drawings_per_page=0), limits)
        self.assertEqual("page_too_complex", too_many_words.exception.rejection.code)
        with self.assertRaises(TrialRejected) as too_many_drawings:
            check_pdf_info(PdfInfo(**base, max_words_per_page=0, max_drawings_per_page=51), limits)
        self.assertEqual("page_too_complex", too_many_drawings.exception.rejection.code)
        self.assertEqual("ai", REJECTIONS["page_too_complex"].feature)
        check_pdf_info(PdfInfo(**base, max_words_per_page=100, max_drawings_per_page=50), limits)  # at the limit passes

    def test_scan_rejection_wins_over_complexity(self):
        limits = InputLimits(max_bytes=4_000_000, max_pages=3, max_source_pages=100, max_page_area_pt=2 * A3_AREA_PT, max_words_per_page=1, max_drawings_per_page=1)
        with self.assertRaises(TrialRejected) as rejected:
            check_pdf_info(PdfInfo(page_count=3, scanned_pages=3, pages_without_text=1, max_page_area_pt=500_000.0, max_words_per_page=9, max_drawings_per_page=9), limits)
        self.assertEqual("no_text_layer", rejected.exception.rejection.code)
```

`test_trial_config.py`:

```python
    def test_complexity_limits_from_env(self):
        config = TrialConfig.from_env({"TRIAL_MAX_WORDS_PER_PAGE": "1234", "TRIAL_MAX_DRAWINGS_PER_PAGE": "567"})
        self.assertEqual(1234, config.limits.max_words_per_page)
        self.assertEqual(567, config.limits.max_drawings_per_page)
        defaults = TrialConfig.from_env({})
        self.assertEqual(8000, defaults.limits.max_words_per_page)
        self.assertEqual(10000, defaults.limits.max_drawings_per_page)
```

`test_trial_api.py`:

```python
    def test_event_records_page_complexity(self):
        info = PdfInfo(page_count=16, scanned_pages=3, pages_without_text=0, max_page_area_pt=500_000.0, max_words_per_page=674, max_drawings_per_page=191)
        client = self.make_client(inspector=FakeInspector(info=info))
        self.assertEqual(200, self.post_pdf(client).status_code)
        self.assertEqual({"words": 674, "drawings": 191}, self.store.events[-1]["complexity"])

    def test_too_complex_pages_are_rejected_before_charging(self):
        info = PdfInfo(page_count=3, scanned_pages=3, pages_without_text=0, max_page_area_pt=500_000.0, max_words_per_page=9000, max_drawings_per_page=0)
        client = self.make_client(inspector=FakeInspector(info=info))
        response = self.post_pdf(client)
        self.assertEqual(422, response.status_code)
        self.assertEqual("page_too_complex", response.json()["error"]["code"])
        self.assertEqual("ai", response.json()["error"]["feature"])
        self.assertEqual(0, self.used())
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_problem_parser.py test_trial_input.py test_trial_config.py test_trial_api.py -k "complex or words or bomb"`
Expected: FAIL — `TypeError: PdfInfo.__init__() got an unexpected keyword argument 'max_words_per_page'` 등

- [ ] **Step 3: `problem_parser.py` 수정**

모듈 상단 `PDF_RENDER_DPI = 200` 아래:

```python
# 2×A3 at 200 DPI is about 15.5M pixels; larger renders are decompression bombs for this service.
Image.MAX_IMAGE_PIXELS = 20_000_000
```

`PdfInfo`:

```python
@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    scanned_pages: int
    pages_without_text: int
    max_page_area_pt: float
    max_words_per_page: int = 0
    max_drawings_per_page: int = 0
```

`inspect_pdf`의 루프:

```python
        max_words_per_page = 0
        max_drawings_per_page = 0
        for index in range(scanned_pages):
            page = doc[index]
            text = page.get_text("text")
            if len("".join(text.split())) < MIN_TEXT_CHARS_PER_PAGE:
                pages_without_text += 1
            max_words_per_page = max(max_words_per_page, len(text.split()))
            max_drawings_per_page = max(max_drawings_per_page, len(page.get_drawings()))
            max_page_area_pt = max(max_page_area_pt, float(page.rect.width * page.rect.height))
    return PdfInfo(
        page_count=page_count,
        scanned_pages=scanned_pages,
        pages_without_text=pages_without_text,
        max_page_area_pt=max_page_area_pt,
        max_words_per_page=max_words_per_page,
        max_drawings_per_page=max_drawings_per_page,
    )
```

- [ ] **Step 4: `trial_input.py` 수정**

`REJECTIONS`에 `page_too_large` 다음 줄로 추가:

```python
        Rejection(422, "page_too_complex", "이 파일은 무료 체험에서 처리하기에 너무 복잡해요.", "ai"),
```

`InputLimits`:

```python
@dataclass(frozen=True)
class InputLimits:
    max_bytes: int
    max_pages: int
    max_source_pages: int
    max_page_area_pt: float
    # Pathological PDFs (thousands of text spans or vector paths per page) could run past
    # Vercel's 60 s limit; the corpus maximum is 674 words and 613 drawings per page.
    max_words_per_page: int = 8000
    max_drawings_per_page: int = 10000
```

`check_pdf_info` 끝에 추가(`pages_without_text` 검사 뒤):

```python
    if info.max_words_per_page > limits.max_words_per_page or info.max_drawings_per_page > limits.max_drawings_per_page:
        raise reject("page_too_complex")
```

- [ ] **Step 5: `trial_config.py` 수정**

`from_env`의 `InputLimits(...)`에 두 줄 추가:

```python
                max_words_per_page=_positive_int(env, "TRIAL_MAX_WORDS_PER_PAGE", 8000),
                max_drawings_per_page=_positive_int(env, "TRIAL_MAX_DRAWINGS_PER_PAGE", 10000),
```

(`TrialConfig`의 `default_factory` 쪽 `InputLimits(...)`는 기본값을 쓰므로 그대로 둔다.)

- [ ] **Step 6: `trial_server.py` 수정**

`inspect` 결과를 받은 직후:

```python
                event["source_pages"] = info.page_count
                event["complexity"] = {"words": info.max_words_per_page, "drawings": info.max_drawings_per_page}
                check_pdf_info(info, config.limits)
```

- [ ] **Step 7: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_problem_parser.py test_trial_input.py test_trial_config.py test_trial_api.py`
Expected: 모두 PASS

- [ ] **Step 8: 커밋**

```bash
git add problem_parser.py trial_input.py trial_config.py trial_server.py test_problem_parser.py test_trial_input.py test_trial_config.py test_trial_api.py
git commit -m "feat: reject pathologically complex pages and cap decoded image size"
```

---

### Task 6: L3 — 미리보기 축소 비용

**Files:**
- Modify: `trial_preview.py:55-63` (`encode_jpeg_data_uri`)
- Test: `test_trial_preview.py`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
    def test_downscale_uses_two_step_reduce(self):
        with mock.patch.object(Image.Image, "resize", wraps=Image.new("RGB", (2339, 3308), "white").resize) as resize:
            encode_jpeg_data_uri(Image.new("RGB", (2339, 3308), "white"), long_side=1200, quality=70)
        self.assertEqual(Image.Resampling.HAMMING, resize.call_args.args[1])
        self.assertEqual(2.0, resize.call_args.kwargs["reducing_gap"])

    def test_downscaled_dimensions_are_unchanged(self):
        preview = _decode(encode_jpeg_data_uri(_noise(2339, 3308, seed=1), long_side=1200, quality=70))
        self.assertEqual((848, 1200), preview.size)
```

파일 상단에 `from unittest import mock`을 더한다. `mock.patch.object(...)`의 `wraps` 대상은 어떤 이미지의 바운드 메서드든 되지만 패치가 클래스 속성이라 `self`가 빠진다. 위 코드가 `TypeError`를 내면 아래 단순 형태로 바꾼다:

```python
    def test_downscale_uses_two_step_reduce(self):
        calls = []
        original = Image.Image.resize

        def spy(self, size, resample=None, box=None, reducing_gap=None):
            calls.append((resample, reducing_gap))
            return original(self, size, resample, box, reducing_gap)

        with mock.patch.object(Image.Image, "resize", spy):
            encode_jpeg_data_uri(Image.new("RGB", (2339, 3308), "white"), long_side=1200, quality=70)
        self.assertEqual([(Image.Resampling.HAMMING, 2.0)], calls)
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_preview.py -k "reduce or dimensions"`
Expected: `test_downscale_uses_two_step_reduce` FAIL(LANCZOS, None), `test_downscaled_dimensions_are_unchanged` PASS

- [ ] **Step 3: 구현**

```python
def encode_jpeg_data_uri(image: Image.Image, *, long_side: int, quality: int) -> str:
    preview = image.convert("RGB")
    scale = long_side / max(preview.size)
    if scale < 1:
        size = (max(1, round(preview.width * scale)), max(1, round(preview.height * scale)))
        # HAMMING instead of LANCZOS: same preview size, roughly half the resize cost (LANCZOS
        # resize x25 cost about 0.17 s per request locally, ~0.7 s on Vercel) for no visible gain
        # at preview sizes. reducing_gap=2.0 additionally lets Pillow run an integer reduce()
        # first, which only engages on the >4x fallback steps (구현 중 정정, 2026-09-16: 기본
        # 프리뷰 단계에서는 reduce()가 실행되지 않는다).
        preview = preview.resize(size, Image.Resampling.HAMMING, reducing_gap=2.0)
    buffer = io.BytesIO()
    preview.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=False)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
```

- [ ] **Step 4: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_preview.py`
Expected: 모두 PASS (예산 단계 테스트가 크기 변화로 깨지면 그 테스트의 기대 단계를 실제 값으로 갱신하고 결정적인지 두 번 실행해 확인)

- [ ] **Step 5: 커밋**

```bash
git add trial_preview.py test_trial_preview.py
git commit -m "perf: downscale trial previews in two steps instead of full LANCZOS"
```

---

### Task 7: L6 — 응답 직렬화 1회

**Files:**
- Modify: `trial_preview.py` (`build_parse_payload` → `build_parse_body`)
- Modify: `trial_server.py` (`parse_and_encode`)
- Test: `test_trial_preview.py`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
    def test_build_parse_body_returns_the_bytes_it_measured(self):
        payload, body = build_parse_body(_result(), remaining_today=2, elapsed_ms=10, processed_page_limit=3)
        self.assertEqual(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"), body)
        self.assertEqual(payload, build_parse_payload(_result(), remaining_today=2, elapsed_ms=10, processed_page_limit=3))
```

import 줄에 `build_parse_body`를 더한다.

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_preview.py -k measured`
Expected: FAIL — `ImportError: cannot import name 'build_parse_body'`

- [ ] **Step 3: 구현**

```python
def build_parse_body(
    result: ParseResult,
    *,
    remaining_today: int,
    elapsed_ms: int,
    processed_page_limit: int,
    budget_bytes: int = RESPONSE_BUDGET_BYTES,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Return the first preview step whose compact JSON fits the budget (else the smallest) and that JSON."""
    payload: dict[str, Any] = {}
    body = b""
    for step_index in range(len(PREVIEW_STEPS)):
        payload = _payload_for_step(
            result,
            step_index,
            remaining_today=remaining_today,
            elapsed_ms=elapsed_ms,
            processed_page_limit=processed_page_limit,
            extra=extra,
        )
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(body) <= budget_bytes:
            return payload, body
    return payload, body


def build_parse_payload(
    result: ParseResult,
    *,
    remaining_today: int,
    elapsed_ms: int,
    processed_page_limit: int,
    budget_bytes: int = RESPONSE_BUDGET_BYTES,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_parse_body(
        result,
        remaining_today=remaining_today,
        elapsed_ms=elapsed_ms,
        processed_page_limit=processed_page_limit,
        budget_bytes=budget_bytes,
        extra=extra,
    )[0]
```

`trial_server.parse_and_encode`에서:

```python
            payload, body = build_parse_body(
                result,
                remaining_today=remaining_today,
                elapsed_ms=_ms(started_at),
                processed_page_limit=config.limits.max_pages,
                extra={
                    "timing_ms": dict(timing),
                    "instance_id": INSTANCE_ID,
                    "instance_age_s": round(time.time() - INSTANCE_STARTED_AT, 1),
                },
            )
```

로 바꾸고 그 아래 `body = json.dumps(...)` 줄을 지운다. import를 `from trial_preview import build_parse_body`로 바꾼다.

- [ ] **Step 4: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_preview.py test_trial_api.py`
Expected: 모두 PASS

- [ ] **Step 5: 커밋**

```bash
git add trial_preview.py trial_server.py test_trial_preview.py
git commit -m "perf: serialize the trial response once"
```

---

### Task 8: 벤치 공통 모듈 · 입력 압축본 · 체험판 관측

**Files:**
- Create: `scripts/trial_bench/__init__.py` (빈 파일)
- Create: `scripts/trial_bench/common.py`
- Create: `scripts/trial_bench/make_inputs.py`
- Create: `scripts/trial_bench/observe.py`
- Test: `test_trial_bench.py` (신규)

- [ ] **Step 1: 실패하는 테스트 작성**

`test_trial_bench.py`:

```python
import json
import math
import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image

from problem_parser import ParsedPage, ParsedProblem, ParsedRegion, ParseResult
from scripts.trial_bench import common
from scripts.trial_bench.make_inputs import make_input
from structured_schema import Box


def _result() -> ParseResult:
    page = ParsedPage(page_id="p1", index=0, width=600, height=800, image=Image.new("RGB", (600, 800), "white"))
    problems = [
        ParsedProblem(
            problem_id="q1", number=1, title="1번",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.0, top=10.0, width=200.0, height=80.0))],
            risk_flags=[], image=Image.new("RGB", (200, 80), "white"),
        ),
        ParsedProblem(
            problem_id="passage", number=None, title="지문 1~3",
            regions=[ParsedRegion(page_id="p1", bbox=Box(left=10.0, top=100.0, width=200.0, height=300.0))],
            risk_flags=["passage_cross_page_merge_check"], image=Image.new("RGB", (200, 300), "white"),
        ),
    ]
    return ParseResult(pages=[page], problems=problems, source_page_count=16, parser_version="dev", timing_ms={"total": 7})


def _write_pdf(path: Path, page_count: int) -> Path:
    doc = fitz.open()
    for index in range(page_count):
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 80), f"{index + 1}. problem stem with enough words to count as text on the page", fontsize=12)
        page.insert_text((40, 300), "① a   ② b   ③ c   ④ d   ⑤ e", fontsize=12)
    doc.save(path)
    doc.close()
    return path


class TestCommon(unittest.TestCase):
    def test_passage_range_from_title(self):
        self.assertEqual([4, 9], common.passage_range_from_title("지문 4~9"))
        self.assertEqual([10, 13], common.passage_range_from_title("지문 10 ~ 13"))
        self.assertIsNone(common.passage_range_from_title("3번"))
        self.assertIsNone(common.passage_range_from_title(None))

    def test_problem_key(self):
        self.assertEqual("q12", common.problem_key(12, "12번"))
        self.assertEqual("p1-3", common.problem_key(None, "지문 1~3"))
        self.assertEqual("t:그림", common.problem_key(None, "그림"))

    def test_case_id_sanitizes_names(self):
        self.assertEqual("2026학년도_수능_국어", common.case_id(Path("/x/2026학년도 수능 국어.pdf")))
        self.assertEqual("01_물리학Ⅰ_문제지", common.case_id(Path("01 물리학Ⅰ_문제지.pdf")))

    def test_observation_has_numbers_boxes_and_no_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            observation = common.observation_from_result("case", _result(), crops_dir=Path(temp_dir) / "crops")
            self.assertTrue((Path(temp_dir) / "crops" / "q1.png").is_file())
        self.assertEqual(["q1", "p1-3"], [problem["key"] for problem in observation["problems"]])
        self.assertEqual([[1, 3]], observation["passage_ranges"])
        self.assertEqual(0, observation["problems"][0]["regions"][0]["page_index"])
        self.assertEqual({"left": 10.0, "top": 10.0, "width": 200.0, "height": 80.0}, observation["problems"][0]["regions"][0]["bbox"])
        self.assertEqual([[600, 800]], observation["page_sizes"])
        self.assertNotIn("text", json.dumps(observation, ensure_ascii=False))

    def test_percentile_is_nearest_rank(self):
        self.assertEqual(10, common.percentile(range(1, 11), 95))
        self.assertEqual(5, common.percentile(range(1, 11), 50))
        self.assertTrue(math.isnan(common.percentile([], 50)))

    def test_markdown_table(self):
        table = common.markdown_table(["a", "b"], [[1, None]])
        self.assertEqual("| a | b |\n|---|---|\n| 1 |  |", table)


class TestMakeInputs(unittest.TestCase):
    def test_trims_to_three_pages_and_records_the_case(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "긴 시험지.pdf", page_count=5)
            target = make_input(source, "korean", root=root)
            with fitz.open(target) as trimmed:
                self.assertEqual(3, trimmed.page_count)
            cases = json.loads((root / "cases.json").read_text(encoding="utf-8"))
        self.assertEqual({"subject": "korean", "source_page_count": 5, "source_name": "긴 시험지.pdf"}, cases["긴_시험지"])
        self.assertEqual(root / "inputs" / "긴_시험지.pdf", target)

    def test_short_documents_are_copied_whole(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "bench"
            source = _write_pdf(Path(temp_dir) / "short.pdf", page_count=2)
            target = make_input(source, "science", root=root)
            with fitz.open(target) as copied:
                self.assertEqual(2, copied.page_count)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_bench.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.trial_bench'`

- [ ] **Step 3: `scripts/trial_bench/__init__.py`(빈 파일)와 `common.py` 작성**

```python
"""Shared helpers for the trial measurement scripts.

Everything measured lives outside the repository under BENCH_ROOT
(default ~/edb-trial-bench): exam PDFs, observations, labels, crops.
Only scripts, tests, and result tables are committed.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

BENCH_ROOT = Path(os.environ.get("TRIAL_BENCH_ROOT") or Path.home() / "edb-trial-bench")
MAX_PAGES = 3
PASSAGE_RANGE = re.compile(r"(\d+)\s*[~∼～\-–]\s*(\d+)")


def bench_dir(name: str, root: Path = BENCH_ROOT) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def case_id(source: Path) -> str:
    stem = re.sub(r"[^\w가-힣.-]+", "_", source.stem).strip("_")
    return stem or "case"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def passage_range_from_title(title: str | None) -> list[int] | None:
    match = PASSAGE_RANGE.search(str(title or ""))
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return [min(start, end), max(start, end)]


def problem_key(number: int | None, title: str | None) -> str:
    if number is not None:
        return f"q{number}"
    span = passage_range_from_title(title)
    if span:
        return f"p{span[0]}-{span[1]}"
    return f"t:{title or ''}"


def observation_from_result(case: str, result: Any, *, crops_dir: Path | None = None) -> dict[str, Any]:
    """Privacy-minimized view of a ParseResult: numbers, boxes, flags. No text."""
    page_index = {page.page_id: page.index for page in result.pages}
    problems: list[dict[str, Any]] = []
    passage_ranges: list[list[int]] = []
    for problem in result.problems:
        key = problem_key(problem.number, problem.title)
        span = None if problem.number is not None else passage_range_from_title(problem.title)
        if span:
            passage_ranges.append(span)
        crop_path: Path | None = None
        if crops_dir is not None:
            crops_dir.mkdir(parents=True, exist_ok=True)
            crop_path = crops_dir / f"{key.replace(':', '_')}.png"
            problem.image.save(crop_path)
        problems.append(
            {
                "key": key,
                "number": problem.number,
                "title": problem.title,
                "passage_range": span,
                "regions": [
                    {
                        "page_index": page_index.get(region.page_id, -1),
                        "bbox": {
                            "left": float(region.bbox.left),
                            "top": float(region.bbox.top),
                            "width": float(region.bbox.width),
                            "height": float(region.bbox.height),
                        },
                    }
                    for region in problem.regions
                ],
                "risk_flags": list(problem.risk_flags),
                "crop": str(crop_path) if crop_path else None,
            }
        )
    return {
        "case": case,
        "pages": len(result.pages),
        "source_page_count": result.source_page_count,
        "page_sizes": [[page.width, page.height] for page in result.pages],
        "problems": problems,
        "passage_ranges": passage_ranges,
        "timing_ms": dict(result.timing_ms),
    }


def parse_in_scratch(source: Path, parse: Callable[..., Any], **kwargs: Any) -> Any:
    """Copy the PDF into a fresh temp dir first.

    A .pipeline_cache next to the input would make second runs unrealistically
    fast (0.2 s recognize). Returned images are detached, so the dir can go.
    """
    with tempfile.TemporaryDirectory(prefix="trial-bench-") as temp_dir:
        copied = Path(temp_dir) / source.name
        shutil.copyfile(source, copied)
        return parse(copied, work_dir=Path(temp_dir) / "work", max_pages=MAX_PAGES, **kwargs)


def percentile(values: Iterable[float], pct: float) -> float:
    """Nearest-rank percentile; empty input gives nan."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return math.nan
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        lines.append("| " + " | ".join("" if cell is None else str(cell) for cell in row) + " |")
    return "\n".join(lines)
```

- [ ] **Step 4: `scripts/trial_bench/make_inputs.py` 작성**

```python
"""Create the trial's exact input (first 3 pages, compacted) for each exam PDF.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/make_inputs.py --subject korean /path/a.pdf /path/b.pdf
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import _leading_pages_copy  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, MAX_PAGES, bench_dir, case_id, load_json, save_json  # noqa: E402


def make_input(source: Path, subject: str, root: Path = BENCH_ROOT) -> Path:
    case = case_id(source)
    target = bench_dir("inputs", root) / f"{case}.pdf"
    with tempfile.TemporaryDirectory(prefix="trial-bench-") as temp_dir:
        trimmed, page_count = _leading_pages_copy(source, Path(temp_dir), MAX_PAGES)
        shutil.copyfile(trimmed, target)
    cases_path = root / "cases.json"
    cases = load_json(cases_path) if cases_path.is_file() else {}
    cases[case] = {"subject": subject, "source_page_count": page_count, "source_name": source.name}
    save_json(cases_path, cases)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--subject", required=True, help="korean, english, math, science, social, unknown")
    parser.add_argument("pdfs", nargs="+", type=Path)
    args = parser.parse_args(argv)
    for source in args.pdfs:
        target = make_input(source, args.subject)
        print(f"{source.name} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: `scripts/trial_bench/observe.py` 작성**

```python
"""Run the trial parser on every bench input; store observations and crops.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/observe.py [case ...]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, bench_dir, markdown_table, observation_from_result, parse_in_scratch, save_json  # noqa: E402


def observe_case(input_pdf: Path, root: Path = BENCH_ROOT) -> dict[str, Any]:
    case = input_pdf.stem
    result = parse_in_scratch(input_pdf, parse_problems)
    observation = observation_from_result(case, result, crops_dir=bench_dir("trial_crops", root) / case)
    save_json(bench_dir("trial", root) / f"{case}.json", observation)
    return observation


def select_inputs(cases: list[str], root: Path = BENCH_ROOT) -> list[Path]:
    inputs = sorted(bench_dir("inputs", root).glob("*.pdf"))
    if cases:
        wanted = set(cases)
        inputs = [path for path in inputs if path.stem in wanted]
    return inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    rows = []
    for pdf in select_inputs(args.cases):
        observation = observe_case(pdf)
        rows.append([pdf.stem, observation["pages"], len(observation["problems"]), len(observation["passage_ranges"]), observation["timing_ms"].get("total")])
    print(markdown_table(["case", "pages", "problems", "passages", "total_ms"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_bench.py`
Expected: 모두 PASS

- [ ] **Step 7: 커밋**

```bash
git add scripts/trial_bench/__init__.py scripts/trial_bench/common.py scripts/trial_bench/make_inputs.py scripts/trial_bench/observe.py test_trial_bench.py
git commit -m "feat: add trial bench inputs and trial-side observations"
```

---

### Task 9: 오라클 (데스크톱 AI 인식을 같은 하류 코드로)

**Files:**
- Modify: `problem_parser.py` (`parse_problems` 인자)
- Create: `scripts/trial_bench/oracle.py`
- Test: `test_problem_parser.py`, `test_trial_bench.py`

Fable 리뷰 대상: `parse_problems`에 인자 두 개를 더한다. 기본값이 지금 동작과 같아야 한다.

- [ ] **Step 1: 실패하는 테스트 추가**

`test_problem_parser.py`의 `TestParseProblems`:

```python
    def test_explicit_no_ai_arguments_match_the_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_text_exam_pdf(root / "exam.pdf", [[1, 2], [3, 4]])
            default = parse_problems(path, work_dir=root / "a")
            explicit = parse_problems(path, work_dir=root / "b", ocr_mode="none", ai_fallback_config=None)
        self.assertEqual([p.number for p in default.problems], [p.number for p in explicit.problems])
        self.assertEqual([p.regions[0].bbox for p in default.problems], [p.regions[0].bbox for p in explicit.problems])
```

`test_trial_bench.py`:

```python
from scripts.trial_bench.oracle import force_config


class TestOracleConfig(unittest.TestCase):
    def test_force_config_fails_loudly_and_forces_repair(self):
        config = force_config("")
        self.assertEqual("force", config["mode"])
        self.assertEqual("gemini", config["provider"])
        self.assertTrue(config["fail_on_error"])
        self.assertEqual("gemini-x", force_config("gemini-x")["model"])
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_problem_parser.py test_trial_bench.py -k "explicit or force_config"`
Expected: FAIL — `TypeError: parse_problems() got an unexpected keyword argument 'ocr_mode'`, `ModuleNotFoundError: ...oracle`

- [ ] **Step 3: `parse_problems` 인자 추가**

```python
def parse_problems(
    source: Path,
    *,
    work_dir: Path,
    max_pages: int | None = None,
    subject: str = "unknown",
    ocr_mode: str = "none",
    ai_fallback_config: dict[str, Any] | None = None,
) -> ParseResult:
    """Recognize problems in a text-layer PDF.

    The trial calls this with the defaults: no OCR, no AI, no board rendering.
    The bench oracle passes ``ocr_mode="auto"`` and a forced AI repair config
    so both sides share every downstream step and coordinate frame.
    """
```

`build_pages(...)` 호출에서 `ocr_mode="none", ai_fallback_config=None,`을 `ocr_mode=ocr_mode, ai_fallback_config=ai_fallback_config,`로 바꾼다. 파일 상단 `from typing import Any`를 더한다.

- [ ] **Step 4: `scripts/trial_bench/oracle.py` 작성**

```python
"""Desktop-grade recognition (Gemini page repair forced) on the bench inputs.

Usage:
  .venv/bin/python scripts/trial_bench/oracle.py --runtime-dir /Users/clmagi/Desktop/Projects/edb_mak/.app_runtime [case ...]

The Gemini key is promoted from the app's user settings into the environment
by user_settings.apply_to_env and is never printed or written anywhere.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table, observation_from_result, parse_in_scratch, save_json  # noqa: E402
from scripts.trial_bench.observe import select_inputs  # noqa: E402
from user_settings import apply_to_env, load_user_settings  # noqa: E402


def force_config(model: str = "") -> dict[str, Any]:
    """The desktop app's 'AI 정밀 인식' settings (app_server: ai_fallback='force'), failing loudly."""
    return {
        "mode": "force",
        "provider": "gemini",
        "model": model,
        "threshold": 0.72,
        "max_regions": 48,
        "max_tokens": 4096,
        "timeout_ms": 60000,
        "save_debug": False,
        "fail_on_error": True,
    }


def oracle_case(input_pdf: Path, subject: str, *, ocr_mode: str = "auto", model: str = "", root: Path = BENCH_ROOT) -> dict[str, Any]:
    case = input_pdf.stem
    result = parse_in_scratch(input_pdf, parse_problems, subject=subject, ocr_mode=ocr_mode, ai_fallback_config=force_config(model))
    observation = observation_from_result(case, result, crops_dir=bench_dir("oracle_crops", root) / case)
    observation["oracle"] = {"ocr_mode": ocr_mode, "model": model or "default", "ai_mode": "force"}
    save_json(bench_dir("oracle", root) / f"{case}.json", observation)
    return observation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-dir", type=Path, required=True, help="directory holding user_settings.json")
    parser.add_argument("--ocr-mode", default="auto")
    parser.add_argument("--model", default="", help="empty = pipeline default repair model")
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    apply_to_env(load_user_settings(args.runtime_dir))
    if not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY not found in runtime settings", file=sys.stderr)
        return 2
    cases = load_json(BENCH_ROOT / "cases.json") if (BENCH_ROOT / "cases.json").is_file() else {}
    rows = []
    for pdf in select_inputs(args.cases):
        subject = str(cases.get(pdf.stem, {}).get("subject") or "unknown")
        observation = oracle_case(pdf, subject, ocr_mode=args.ocr_mode, model=args.model)
        rows.append([pdf.stem, subject, len(observation["problems"]), len(observation["passage_ranges"]), observation["timing_ms"].get("total")])
    print(markdown_table(["case", "subject", "problems", "passages", "total_ms"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_problem_parser.py test_trial_bench.py test_trial_api.py`
Expected: 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add problem_parser.py scripts/trial_bench/oracle.py test_problem_parser.py test_trial_bench.py
git commit -m "feat: run the desktop AI recognition as a bench oracle through the trial parser"
```

---

### Task 10: 채점과 보고

**Files:**
- Create: `scripts/trial_bench/score.py`
- Create: `docs/web-trial-quality.md`
- Test: `test_trial_bench.py`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
from scripts.trial_bench.score import bbox_iou, expected_from, regions_iou, render_report, score_case


def _obs(case: str, problems: list[tuple[str, int | None, str, list[tuple[int, float, float, float, float]]]], total_ms: int = 100) -> dict:
    entries = []
    ranges = []
    for key, number, title, regions in problems:
        span = common.passage_range_from_title(title) if number is None else None
        if span:
            ranges.append(span)
        entries.append(
            {
                "key": key, "number": number, "title": title, "passage_range": span,
                "regions": [{"page_index": p, "bbox": {"left": l, "top": t, "width": w, "height": h}} for p, l, t, w, h in regions],
                "risk_flags": [], "crop": None,
            }
        )
    return {"case": case, "pages": 3, "source_page_count": 16, "page_sizes": [[600, 800]] * 3, "problems": entries, "passage_ranges": ranges, "timing_ms": {"total": total_ms}}


class TestScore(unittest.TestCase):
    def test_bbox_iou(self):
        box = {"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0}
        self.assertEqual(1.0, bbox_iou(box, box))
        self.assertEqual(0.0, bbox_iou(box, {"left": 20.0, "top": 0.0, "width": 10.0, "height": 10.0}))
        self.assertAlmostEqual(0.25, bbox_iou(box, {"left": 0.0, "top": 0.0, "width": 5.0, "height": 5.0}))

    def test_regions_iou_counts_pages_present_on_one_side_as_union(self):
        a = [{"page_index": 0, "bbox": {"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0}}]
        b = a + [{"page_index": 1, "bbox": {"left": 0.0, "top": 0.0, "width": 10.0, "height": 10.0}}]
        self.assertAlmostEqual(0.5, regions_iou(a, b))
        self.assertEqual(1.0, regions_iou(b, b))

    def test_score_case_recall_precision_and_low_iou(self):
        trial = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 20, 10, 10)]), ("p1-3", None, "지문 1~3", [(0, 0, 40, 10, 10)])])
        expected = {p["key"]: p for p in _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 25, 10, 10)]), ("q3", 3, "3번", [(1, 0, 0, 10, 10)]), ("p1-3", None, "지문 1~3", [(0, 0, 40, 10, 10)])])["problems"]}
        score = score_case(trial, expected)
        self.assertAlmostEqual(2 / 3, score["question_recall"])
        self.assertEqual(1.0, score["question_precision"])
        self.assertEqual(1.0, score["passage_recall"])
        self.assertEqual(["q3"], score["missing"])
        self.assertEqual([], score["extra"])
        self.assertEqual(1, score["low_iou"])  # q2 overlaps by half
        self.assertEqual(100, score["trial_ms"])

    def test_expected_from_applies_approved_labels(self):
        trial = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q9", 9, "9번", [(0, 0, 0, 10, 10)])])
        oracle = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 0, 10, 10)])])
        pending, status = expected_from(oracle, trial, None)
        self.assertEqual("pending", status)
        self.assertEqual({"q1", "q2"}, set(pending))
        labels = {"case": "c", "status": "approved", "items": [{"key": "q2", "truth": "neither"}, {"key": "q9", "truth": "trial"}]}
        approved, status = expected_from(oracle, trial, labels)
        self.assertEqual("approved", status)
        self.assertEqual({"q1", "q9"}, set(approved))

    def test_render_report_has_one_row_per_case_and_an_aggregate(self):
        rows = [{"case": "a", "status": "approved", "question_recall": 1.0, "question_precision": 1.0, "passage_recall": 1.0, "passage_precision": 1.0, "mean_iou": 0.95, "low_iou": 0, "review_rate": 0.0, "missing": [], "extra": [], "trial_ms": 1500, "oracle_ms": 9000}]
        report = render_report(rows)
        self.assertIn("| a | approved | 1.00 |", report)
        self.assertIn("| 합계 |", report)
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_bench.py -k "Score"`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.trial_bench.score'`

- [ ] **Step 3: `scripts/trial_bench/score.py` 작성**

```python
"""Score trial observations against approved labels (or the oracle while pending).

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/score.py [--doc docs/web-trial-quality.md] [case ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table  # noqa: E402
from trial_preview import needs_review  # noqa: E402

LOW_IOU = 0.8
DOC_START = "<!-- corpus-table -->"
DOC_END = "<!-- /corpus-table -->"


def _area(box: dict[str, float]) -> float:
    return max(0.0, box["width"]) * max(0.0, box["height"])


def _intersection(a: dict[str, float], b: dict[str, float]) -> float:
    width = min(a["left"] + a["width"], b["left"] + b["width"]) - max(a["left"], b["left"])
    height = min(a["top"] + a["height"], b["top"] + b["height"]) - max(a["top"], b["top"])
    return max(0.0, width) * max(0.0, height)


def bbox_iou(a: dict[str, float], b: dict[str, float]) -> float:
    inter = _intersection(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def regions_iou(a_regions: list[dict[str, Any]], b_regions: list[dict[str, Any]]) -> float:
    """Sum of per-page intersections over per-page unions. A page present on one side only adds union."""
    by_a = {region["page_index"]: region["bbox"] for region in a_regions}
    by_b = {region["page_index"]: region["bbox"] for region in b_regions}
    inter = union = 0.0
    for page in set(by_a) | set(by_b):
        a, b = by_a.get(page), by_b.get(page)
        if a and b:
            overlap = _intersection(a, b)
            inter += overlap
            union += _area(a) + _area(b) - overlap
        else:
            union += _area(a or b)
    return inter / union if union > 0 else 0.0


def expected_from(oracle: dict[str, Any], trial: dict[str, Any], labels: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], str]:
    """Ground truth per key: the oracle, corrected by approved labels (truth: trial | oracle | both | neither)."""
    expected = {problem["key"]: problem for problem in oracle["problems"]}
    if not labels or labels.get("status") != "approved":
        return expected, "pending"
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    for item in labels.get("items", []):
        key, truth = item.get("key"), item.get("truth")
        if truth == "neither":
            expected.pop(key, None)
        elif truth == "trial" and key in trial_by_key:
            expected[key] = trial_by_key[key]
    return expected, "approved"


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def score_case(trial: dict[str, Any], expected: dict[str, dict[str, Any]]) -> dict[str, Any]:
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    questions_t = {key for key in trial_by_key if key.startswith("q")}
    questions_e = {key for key in expected if key.startswith("q")}
    passages_t = {key for key in trial_by_key if key.startswith("p")}
    passages_e = {key for key in expected if key.startswith("p")}
    matched = (questions_t & questions_e) | (passages_t & passages_e)
    ious = {key: regions_iou(trial_by_key[key]["regions"], expected[key]["regions"]) for key in matched}
    return {
        "case": trial["case"],
        "question_recall": _ratio(len(questions_t & questions_e), len(questions_e)),
        "question_precision": _ratio(len(questions_t & questions_e), len(questions_t)),
        "passage_recall": _ratio(len(passages_t & passages_e), len(passages_e)),
        "passage_precision": _ratio(len(passages_t & passages_e), len(passages_t)),
        "mean_iou": sum(ious.values()) / len(ious) if ious else 1.0,
        "low_iou": sum(1 for value in ious.values() if value < LOW_IOU),
        "review_rate": sum(1 for problem in trial["problems"] if needs_review(problem["risk_flags"])) / max(1, len(trial_by_key)),
        "missing": sorted(questions_e - questions_t) + sorted(passages_e - passages_t),
        "extra": sorted(questions_t - questions_e) + sorted(passages_t - passages_e),
        "trial_ms": trial["timing_ms"].get("total"),
    }


def render_report(rows: list[dict[str, Any]]) -> str:
    headers = ["case", "status", "q_recall", "q_prec", "p_recall", "p_prec", "mean_iou", "low_iou", "review", "missing", "extra", "trial_ms", "oracle_ms"]
    table_rows = [
        [
            row["case"], row["status"], f"{row['question_recall']:.2f}", f"{row['question_precision']:.2f}",
            f"{row['passage_recall']:.2f}", f"{row['passage_precision']:.2f}", f"{row['mean_iou']:.2f}", row["low_iou"],
            f"{row['review_rate']:.2f}", " ".join(row["missing"]), " ".join(row["extra"]), row["trial_ms"], row["oracle_ms"],
        ]
        for row in rows
    ]
    if rows:
        count = len(rows)
        mean = lambda field: sum(row[field] for row in rows) / count  # noqa: E731
        table_rows.append(
            [
                "합계", f"{sum(1 for row in rows if row['status'] == 'approved')}/{count} approved",
                f"{mean('question_recall'):.2f}", f"{mean('question_precision'):.2f}", f"{mean('passage_recall'):.2f}",
                f"{mean('passage_precision'):.2f}", f"{mean('mean_iou'):.2f}", sum(row["low_iou"] for row in rows),
                f"{mean('review_rate'):.2f}", sum(len(row["missing"]) for row in rows), sum(len(row["extra"]) for row in rows), "", "",
            ]
        )
    return markdown_table(headers, table_rows)


def score_all(cases: list[str], root: Path = BENCH_ROOT) -> list[dict[str, Any]]:
    rows = []
    for trial_path in sorted(bench_dir("trial", root).glob("*.json")):
        case = trial_path.stem
        if cases and case not in cases:
            continue
        oracle_path = bench_dir("oracle", root) / f"{case}.json"
        if not oracle_path.is_file():
            continue
        labels_path = bench_dir("labels", root) / f"{case}.json"
        trial, oracle = load_json(trial_path), load_json(oracle_path)
        labels = load_json(labels_path) if labels_path.is_file() else None
        expected, status = expected_from(oracle, trial, labels)
        row = score_case(trial, expected)
        row.update(status=status, oracle_ms=oracle["timing_ms"].get("total"))
        rows.append(row)
    return rows


def update_doc(doc_path: Path, table: str) -> None:
    stamp = f"측정일 {dt.date.today().isoformat()} · 라벨 없는 케이스는 오라클을 임시 정답으로 채점(pending)"
    block = f"{DOC_START}\n{stamp}\n\n{table}\n{DOC_END}"
    text = doc_path.read_text(encoding="utf-8") if doc_path.is_file() else "# 웹 체험판 정확성 코퍼스 결과\n\n"
    if DOC_START in text and DOC_END in text:
        head, rest = text.split(DOC_START, 1)
        _, tail = rest.split(DOC_END, 1)
        text = head + block + tail
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    doc_path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--doc", type=Path, default=None, help="markdown file whose corpus-table block is replaced")
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    rows = score_all(args.cases)
    table = render_report(rows)
    print(table)
    (BENCH_ROOT / "report.md").write_text(table + "\n", encoding="utf-8")
    if args.doc:
        update_doc(args.doc, table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: `docs/web-trial-quality.md` 뼈대 작성**

```markdown
# 웹 체험판 정확성 코퍼스 결과

- 설계: `docs/superpowers/specs/2026-09-15-web-trial-quality-speed-load-design.md` §5-1
- 코퍼스 위치: 저장소 밖 `~/edb-trial-bench/` (시험지·관측 JSON·라벨은 커밋하지 않는다)
- 갱신: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/score.py --doc docs/web-trial-quality.md`

## 지표

| 열 | 뜻 |
|---|---|
| `q_recall` / `q_prec` | 정답 문항 번호 중 체험판이 찾은 비율 / 체험판 문항 중 정답에 있는 비율 |
| `p_recall` / `p_prec` | 지문 범위(예: 1~3) 기준 같은 비율 |
| `mean_iou` / `low_iou` | 짝지은 문항의 박스 IoU 평균 / 0.8 미만 개수 |
| `review` | "확인 필요" 배지 비율 |
| `status` | `approved`는 Fable이 판정한 라벨 기준, `pending`은 오라클을 임시 정답으로 |

## 결과

<!-- corpus-table -->
(아직 측정 전)
<!-- /corpus-table -->
```

- [ ] **Step 5: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_bench.py`
Expected: 모두 PASS

- [ ] **Step 6: 커밋**

```bash
git add scripts/trial_bench/score.py docs/web-trial-quality.md test_trial_bench.py
git commit -m "feat: score trial observations against labels or the oracle"
```

---

### Task 11: 불일치 비교 이미지와 라벨 뼈대

**Files:**
- Create: `scripts/trial_bench/adjudicate.py`
- Test: `test_trial_bench.py`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
from scripts.trial_bench.adjudicate import compose, disagreements, labels_skeleton


class TestAdjudicate(unittest.TestCase):
    def test_disagreements_cover_missing_extra_and_low_iou(self):
        trial = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 20, 10, 10)]), ("q9", 9, "9번", [(0, 0, 0, 10, 10)])])
        oracle = _obs("c", [("q1", 1, "1번", [(0, 0, 0, 10, 10)]), ("q2", 2, "2번", [(0, 0, 25, 10, 10)]), ("q3", 3, "3번", [(1, 0, 0, 10, 10)])])
        items = disagreements(trial, oracle)
        self.assertEqual(["q2", "q3", "q9"], [item["key"] for item in items])
        self.assertEqual(["iou 0.33", "oracle only", "trial only"], [item["reason"] for item in items])

    def test_labels_skeleton_is_pending_with_empty_truth(self):
        skeleton = labels_skeleton("c", [{"key": "q3", "reason": "oracle only", "trial": None, "oracle": {}}])
        self.assertEqual({"case": "c", "status": "pending", "items": [{"key": "q3", "reason": "oracle only", "truth": None, "note": ""}]}, skeleton)

    def test_compose_writes_a_png_even_without_crops(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            out = Path(temp_dir) / "c" / "q3.png"
            compose({"key": "q3", "reason": "oracle only", "trial": None, "oracle": {"crop": None}}, out)
            with Image.open(out) as image:
                self.assertEqual("RGB", image.mode)
                self.assertGreater(image.width, 400)
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_bench.py -k Adjudicate`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: `scripts/trial_bench/adjudicate.py` 작성**

```python
"""Side-by-side crops for every trial/oracle disagreement, plus a labels skeleton.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/adjudicate.py [case ...]

Fable reviews adjudication/<case>/<key>.png and fills labels/<case>.json:
truth = "trial" | "oracle" | "both" | "neither", then status = "approved".
Existing label files are never overwritten.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import BENCH_ROOT, bench_dir, load_json, markdown_table, save_json  # noqa: E402
from scripts.trial_bench.score import LOW_IOU, regions_iou  # noqa: E402

PANEL_MAX = (900, 1200)


def disagreements(trial: dict[str, Any], oracle: dict[str, Any], low_iou: float = LOW_IOU) -> list[dict[str, Any]]:
    trial_by_key = {problem["key"]: problem for problem in trial["problems"]}
    oracle_by_key = {problem["key"]: problem for problem in oracle["problems"]}
    items = []
    for key in sorted(set(trial_by_key) | set(oracle_by_key)):
        t, o = trial_by_key.get(key), oracle_by_key.get(key)
        if t and o:
            iou = regions_iou(t["regions"], o["regions"])
            if iou >= low_iou:
                continue
            reason = f"iou {iou:.2f}"
        else:
            reason = "trial only" if t else "oracle only"
        items.append({"key": key, "reason": reason, "trial": t, "oracle": o})
    return items


def labels_skeleton(case: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "case": case,
        "status": "pending",
        "items": [{"key": item["key"], "reason": item["reason"], "truth": None, "note": ""} for item in items],
    }


def _panel(entry: dict[str, Any] | None) -> Image.Image:
    crop = entry.get("crop") if entry else None
    if crop and Path(crop).is_file():
        with Image.open(crop) as image:
            panel = image.convert("RGB")
            panel.thumbnail(PANEL_MAX)
            return panel
    return Image.new("RGB", (400, 200), "lightgray")


def compose(item: dict[str, Any], out_path: Path) -> None:
    panels = [_panel(item["trial"]), _panel(item["oracle"])]
    canvas = Image.new("RGB", (sum(panel.width for panel in panels) + 30, max(panel.height for panel in panels) + 40), "white")
    draw = ImageDraw.Draw(canvas)
    x = 10
    for side, panel in zip(("trial", "oracle"), panels):
        draw.text((x, 8), f"{side}: {item['key']} ({item['reason']})", fill="black")
        canvas.paste(panel, (x, 30))
        x += panel.width + 10
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def adjudicate_case(case: str, root: Path = BENCH_ROOT) -> int:
    trial = load_json(bench_dir("trial", root) / f"{case}.json")
    oracle = load_json(bench_dir("oracle", root) / f"{case}.json")
    items = disagreements(trial, oracle)
    for item in items:
        compose(item, bench_dir("adjudication", root) / case / f"{item['key'].replace(':', '_')}.png")
    labels_path = bench_dir("labels", root) / f"{case}.json"
    if not labels_path.is_file():
        save_json(labels_path, labels_skeleton(case, items))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="*")
    args = parser.parse_args(argv)
    rows = []
    for oracle_path in sorted(bench_dir("oracle").glob("*.json")):
        case = oracle_path.stem
        if args.cases and case not in args.cases:
            continue
        if not (bench_dir("trial") / f"{case}.json").is_file():
            continue
        rows.append([case, adjudicate_case(case)])
    print(markdown_table(["case", "disagreements"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_bench.py`
Expected: 모두 PASS

- [ ] **Step 5: 커밋**

```bash
git add scripts/trial_bench/adjudicate.py test_trial_bench.py
git commit -m "feat: render trial/oracle disagreements for adjudication"
```

---

### Task 12: 프로브 · 동시 요청 · 복잡도 · 메모리 스크립트

**Files:**
- Create: `scripts/trial_bench/probe.py`, `scripts/trial_bench/load.py`, `scripts/trial_bench/complexity.py`, `scripts/trial_bench/memory.py`
- Test: `test_trial_bench.py`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
from scripts.trial_bench.load import summarize_wave
from scripts.trial_bench.probe import summarize_file


class TestProbeSummaries(unittest.TestCase):
    def test_summarize_file_treats_second_call_onward_as_warm(self):
        rows = [
            {"status": 200, "wall_ms": 9000, "bytes": 10, "timing_ms": {"total": 8000, "render": 1000, "segment": 3000, "assets": 2000}, "instance_id": "a", "instance_age_s": 5.0},
            {"status": 200, "wall_ms": 7000, "bytes": 12, "timing_ms": {"total": 6500, "render": 900, "segment": 2900, "assets": 1900}, "instance_id": "a", "instance_age_s": 20.0},
            {"status": 200, "wall_ms": 7200, "bytes": 12, "timing_ms": {"total": 6600, "render": 950, "segment": 2950, "assets": 1950}, "instance_id": "a", "instance_age_s": 30.0},
        ]
        summary = summarize_file(rows)
        self.assertEqual(9000, summary["first_wall_ms"])
        self.assertEqual(7000, summary["warm_p50_ms"])  # nearest-rank p50 of [7000, 7200]
        self.assertEqual(7200, summary["warm_max_ms"])
        self.assertEqual(6500, summary["parse_p50_ms"])
        self.assertEqual(900, summary["render_p50_ms"])
        self.assertEqual(["a"], summary["instances"])
        self.assertEqual(12, summary["bytes_max"])

    def test_summarize_wave_counts_distribution(self):
        rows = [
            {"status": 200, "wall_ms": 7000, "instance_id": "a", "instance_age_s": 100.0},
            {"status": 200, "wall_ms": 12000, "instance_id": "a", "instance_age_s": 100.0},
            {"status": 200, "wall_ms": 13000, "instance_id": "b", "instance_age_s": 4.0},
            {"status": 503, "wall_ms": 20000, "instance_id": None, "instance_age_s": None},
            {"status": 504, "wall_ms": 60000, "instance_id": None, "instance_age_s": None},
        ]
        summary = summarize_wave(rows)
        self.assertEqual({"requests": 5, "ok": 3, "busy": 1, "failed_other": 1, "p50_ms": 12000, "p95_ms": 13000, "max_ms": 13000, "instances": 2, "max_per_instance": 2, "cold": 1}, summary)
```

- [ ] **Step 2: 실패 확인**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_bench.py -k ProbeSummaries`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: `scripts/trial_bench/probe.py` 작성**

```python
"""Measure the trial parse endpoint on a preview deployment, file by file.

Usage:
  VERCEL_AUTOMATION_BYPASS_SECRET=... .venv/bin/python scripts/trial_bench/probe.py https://<preview>.vercel.app a.pdf b.pdf --repeat 5

Preview deployments skip Turnstile (no secret in the Preview env) and keep
quotas in memory; the bypass header gets through Vercel's login protection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import markdown_table, percentile  # noqa: E402

BYPASS_HEADER = "x-vercel-protection-bypass"


def call_parse(base_url: str, payload: bytes, *, bypass_secret: str | None, timeout: float = 90.0) -> dict[str, Any]:
    headers = {"content-type": "application/pdf"}
    if bypass_secret:
        headers[BYPASS_HEADER] = bypass_secret
    request = urllib.request.Request(base_url.rstrip("/") + "/api/parse", data=payload, method="POST", headers=headers)
    started_at = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"status": 0, "wall_ms": round((time.perf_counter() - started_at) * 1000), "bytes": 0, "timing_ms": {}, "instance_id": None, "instance_age_s": None, "error": repr(error)}
    wall_ms = round((time.perf_counter() - started_at) * 1000)
    try:
        body = json.loads(raw)
    except ValueError:
        body = {}
    return {
        "status": status,
        "wall_ms": wall_ms,
        "bytes": len(raw),
        "elapsed_ms": body.get("elapsed_ms"),
        "timing_ms": body.get("timing_ms") or {},
        "instance_id": body.get("instance_id"),
        "instance_age_s": body.get("instance_age_s"),
        "problems": len(body.get("problems") or []),
        "error": (body.get("error") or {}).get("code") if isinstance(body.get("error"), dict) else None,
    }


def summarize_file(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in rows if row["status"] == 200]
    warm = ok[1:] or ok
    stage = lambda key: percentile([row["timing_ms"].get(key, 0) for row in warm], 50)  # noqa: E731
    return {
        "first_wall_ms": rows[0]["wall_ms"] if rows else None,
        "warm_p50_ms": percentile([row["wall_ms"] for row in warm], 50),
        "warm_max_ms": max((row["wall_ms"] for row in warm), default=None),
        "parse_p50_ms": stage("total"),
        "render_p50_ms": stage("render"),
        "segment_p50_ms": stage("segment"),
        "assets_p50_ms": stage("assets"),
        "instances": sorted({row["instance_id"] for row in ok if row["instance_id"]}),
        "bytes_max": max((row["bytes"] for row in ok), default=0),
        "failures": len(rows) - len(ok),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("pdfs", nargs="+", type=Path)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--json", type=Path, default=None, help="append raw rows as JSON lines")
    args = parser.parse_args(argv)
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET") or None
    table = []
    for pdf in args.pdfs:
        payload = pdf.read_bytes()
        rows = []
        for attempt in range(args.repeat):
            row = call_parse(args.base_url, payload, bypass_secret=bypass)
            row.update(file=pdf.name, attempt=attempt + 1)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), file=sys.stderr)
            if args.json:
                with args.json.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        summary = summarize_file(rows)
        table.append([pdf.name, len(payload), summary["first_wall_ms"], summary["warm_p50_ms"], summary["warm_max_ms"], summary["parse_p50_ms"], summary["render_p50_ms"], summary["segment_p50_ms"], summary["assets_p50_ms"], summary["bytes_max"], len(summary["instances"]), summary["failures"]])
    print(markdown_table(["file", "bytes", "first_wall", "warm_p50", "warm_max", "parse_p50", "render", "segment", "assets", "resp_bytes", "instances", "failures"], table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: `scripts/trial_bench/load.py` 작성**

```python
"""Fire N concurrent uploads at a preview deployment and report the distribution.

Usage:
  VERCEL_AUTOMATION_BYPASS_SECRET=... .venv/bin/python scripts/trial_bench/load.py https://<preview>.vercel.app exam.pdf --concurrency 10 --waves 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.trial_bench.common import markdown_table, percentile  # noqa: E402
from scripts.trial_bench.probe import call_parse  # noqa: E402

COLD_AGE_S = 30.0


def run_wave(base_url: str, payload: bytes, concurrency: int, bypass_secret: str | None) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(call_parse, base_url, payload, bypass_secret=bypass_secret, timeout=120.0) for _ in range(concurrency)]
        return [future.result() for future in futures]


def summarize_wave(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in rows if row["status"] == 200]
    walls = [row["wall_ms"] for row in ok]
    per_instance = Counter(row["instance_id"] for row in ok)
    return {
        "requests": len(rows),
        "ok": len(ok),
        "busy": sum(1 for row in rows if row["status"] == 503),
        "failed_other": sum(1 for row in rows if row["status"] not in (200, 503)),
        "p50_ms": percentile(walls, 50),
        "p95_ms": percentile(walls, 95),
        "max_ms": max(walls) if walls else None,
        "instances": len(per_instance),
        "max_per_instance": max(per_instance.values()) if per_instance else 0,
        "cold": sum(1 for row in ok if (row["instance_age_s"] or 0) < COLD_AGE_S),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_url")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--waves", type=int, default=1)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    bypass = os.environ.get("VERCEL_AUTOMATION_BYPASS_SECRET") or None
    payload = args.pdf.read_bytes()
    table = []
    for wave in range(1, args.waves + 1):
        rows = run_wave(args.base_url, payload, args.concurrency, bypass)
        if args.json:
            with args.json.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps({"wave": wave, "concurrency": args.concurrency, **row}, ensure_ascii=False) + "\n")
        summary = summarize_wave(rows)
        table.append([wave, args.concurrency, summary["ok"], summary["busy"], summary["failed_other"], summary["p50_ms"], summary["p95_ms"], summary["max_ms"], summary["instances"], summary["max_per_instance"], summary["cold"]])
    print(markdown_table(["wave", "concurrency", "ok", "busy", "other_fail", "p50_ms", "p95_ms", "max_ms", "instances", "max_per_inst", "cold"], table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: `scripts/trial_bench/complexity.py` 작성**

```python
"""How parse time grows with words and drawings per page (synthetic 3-page PDFs).

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 500 1000 2000 4000 8000 --drawings 0 2000 8000
Vercel estimate = local × 4.3 (docs/web-trial-spike-results.md §2-3).
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import inspect_pdf, parse_problems  # noqa: E402
from scripts.trial_bench.common import markdown_table, parse_in_scratch  # noqa: E402

VERCEL_FACTOR = 4.3
LINE = "이 문장은 복잡도 실험을 위한 채움 글입니다 하나 둘 셋 넷 다섯 여섯 일곱 여덟 아홉 열"  # 14 words
WORDS_PER_LINE = 15
TOP_Y = 40.0
BOTTOM_Y = 1150.0
MAX_PITCH = 11.0
MAX_FONTSIZE = 8.0


def line_layout(words_per_page: int) -> tuple[float, float]:
    """Line pitch and font size that fit ``words_per_page`` between TOP_Y and BOTTOM_Y.

    A fixed 11 pt pitch stops at ~101 lines, so every target above ~1500 words
    hit the page-bottom guard first and produced the same saturated page: the
    documented `--words 500 1000 2000 4000 8000` sweep collapsed its top three
    levels into one measurement. Deriving the pitch (and a font size that keeps
    the same 8/11 text-to-pitch ratio) from the target keeps the dense end of
    the sweep a real data point.
    """
    lines = max(1, math.ceil(words_per_page / WORDS_PER_LINE))
    pitch = min(MAX_PITCH, (BOTTOM_Y - TOP_Y) / lines)
    return pitch, min(MAX_FONTSIZE, pitch * MAX_FONTSIZE / MAX_PITCH)


def write_synthetic(path: Path, *, words_per_page: int, drawings_per_page: int, pages: int = 3) -> Path:
    doc = fitz.open()
    pitch, fontsize = line_layout(words_per_page)
    number = 1
    for page_index in range(pages):
        page = doc.new_page(width=842, height=1191)  # A3-ish like CSAT papers at 72 pt/in
        y = TOP_Y
        words = 0
        line_index = 0
        while words < words_per_page and y < BOTTOM_Y:
            if line_index % 5 == 0:
                page.insert_text((40, y), f"{number}. {LINE}", fontsize=fontsize)
                number += 1
            else:
                page.insert_text((40, y), LINE, fontsize=fontsize)
            words += WORDS_PER_LINE
            line_index += 1
            y += pitch
        for index in range(drawings_per_page):
            x = 40 + (index % 70) * 11
            yy = 40 + (index // 70) * 11
            page.draw_line((x, yy), (x + 8, yy), color=(0, 0, 0), width=0.3)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--words", type=int, nargs="+", default=[500, 1000, 2000, 4000, 8000])
    parser.add_argument("--drawings", type=int, nargs="+", default=[0, 2000, 8000])
    args = parser.parse_args(argv)
    rows = []
    with tempfile.TemporaryDirectory(prefix="trial-complexity-") as temp_dir:
        for words in args.words:
            for drawings in args.drawings:
                pdf = write_synthetic(Path(temp_dir) / f"w{words}_d{drawings}.pdf", words_per_page=words, drawings_per_page=drawings)
                info = inspect_pdf(pdf, max_pages=3)
                result = parse_in_scratch(pdf, parse_problems)
                total = result.timing_ms["total"]
                rows.append([info.max_words_per_page, info.max_drawings_per_page, result.timing_ms.get("render"), result.timing_ms.get("segment"), result.timing_ms.get("assets"), total, round(total * VERCEL_FACTOR / 1000, 1), len(result.problems)])
    print(markdown_table(["words/pg", "drawings/pg", "render_ms", "segment_ms", "assets_ms", "total_ms", "vercel_est_s", "problems"], rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: `scripts/trial_bench/memory.py` 작성**

```python
"""Peak RSS when two parses overlap, as they can on one Fluid instance.

Usage:
  GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py ~/edb-trial-bench/inputs/*.pdf [--synthetic-2xa3]
"""

from __future__ import annotations

import argparse
import resource
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from problem_parser import parse_problems  # noqa: E402
from scripts.trial_bench.common import markdown_table, parse_in_scratch  # noqa: E402


def max_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(usage / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def write_2xa3(path: Path) -> Path:
    doc = fitz.open()
    for page_index in range(3):
        page = doc.new_page(width=1190, height=1684)  # twice A3 in area
        for line in range(90):
            page.insert_text((40, 40 + line * 18), f"{page_index * 30 + line // 3 + 1}. 큰 페이지 문항 본문 줄 {line} ① a ② b ③ c ④ d ⑤ e", fontsize=9)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pdfs", nargs="*", type=Path)
    parser.add_argument("--synthetic-2xa3", action="store_true")
    args = parser.parse_args(argv)
    rows = []
    with tempfile.TemporaryDirectory(prefix="trial-memory-") as temp_dir:
        pdfs = list(args.pdfs)
        if args.synthetic_2xa3:
            pdfs.append(write_2xa3(Path(temp_dir) / "2xa3.pdf"))
        for pdf in pdfs:
            before = max_rss_mb()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: parse_in_scratch(pdf, parse_problems), range(2)))
            rows.append([pdf.name, before, max_rss_mb(), results[0].timing_ms["total"], results[1].timing_ms["total"]])
    print(markdown_table(["file", "rss_before_mb", "rss_after_mb", "total_ms_a", "total_ms_b"], rows))
    print("rss is process-wide and cumulative: run small files first, and read each row as 'at most this much for two overlapping parses'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: 통과 확인 + 로컬 스모크**

Run: `GEMINI_API_KEY= .venv/bin/python -m pytest -q test_trial_bench.py`
Expected: 모두 PASS

Run: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py --words 500 --drawings 0`
Expected: 표 한 줄, `problems` 15~30 안팎 (5줄마다 번호), 오류 없음

Run: `GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py --synthetic-2xa3`
Expected: 표 한 줄, `rss_after_mb`가 1200 이하

- [ ] **Step 8: 커밋**

```bash
git add scripts/trial_bench/probe.py scripts/trial_bench/load.py scripts/trial_bench/complexity.py scripts/trial_bench/memory.py test_trial_bench.py
git commit -m "feat: add trial probe, load, complexity, and memory measurement scripts"
```

---

### Task 13: 운영 문서와 결과 문서 뼈대

**Files:**
- Modify: `docs/web-trial-operations.md` (§2-3 환경변수 표, §3 점검, §4 SQL, 새 §2-5 프리뷰)
- Create: `docs/web-trial-load.md`
- Modify: `docs/web-trial-spike-results.md` (§4 뼈대)

- [ ] **Step 1: 운영 문서 §2-3 환경변수 표에 행 추가**

```markdown
| `TRIAL_MAX_WORDS_PER_PAGE` / `TRIAL_MAX_DRAWINGS_PER_PAGE` | 기본 8000 / 10000. 앞 3쪽 중 한 쪽이라도 넘으면 422 `page_too_complex` | 아니오 |
| `EDB_PROBLEM_ASSET_WORKERS` | 1 vCPU에서 crop 렌더 스레드 수. Task 15의 A/B 결과로 정한다 | 아니오 |
```

- [ ] **Step 2: 운영 문서에 §2-5 추가**

```markdown
### 2-5. 프리뷰 배포 (측정용)

| 설정 | 값 |
|---|---|
| Ignored Build Step | `case "$VERCEL_GIT_COMMIT_REF" in web-trial\|web-trial-bench) exit 1;; *) exit 0;; esac` |
| Deployment Protection > Protection Bypass for Automation | 켜고 비밀값을 `VERCEL_AUTOMATION_BYPASS_SECRET`으로 로컬 셸에만 둔다 |
| Preview 환경변수 | `TRIAL_DAILY_LIMIT=10000`, `TRIAL_GLOBAL_DAILY_LIMIT=10000` (프리뷰 한도는 인스턴스 메모리). Turnstile·Supabase 비밀은 넣지 않는다 → 봇 확인 생략, 메모리 한도 |

`web-trial-bench` 브랜치를 푸시하면 프리뷰가 뜬다. 프로브는 `scripts/trial_bench/probe.py`, 동시 요청은 `scripts/trial_bench/load.py`이며 둘 다 `x-vercel-protection-bypass` 헤더를 붙인다. 운영 환경에는 우회 경로가 없다.
```

- [ ] **Step 3: 운영 문서 §4에 SQL 추가**

```markdown
### 4-5. 단계별 시간 · 인스턴스 · busy 사유

```sql
-- 단계별 p50/p95 (최근 7일, 성공만)
select
  percentile_cont(0.5) within group (order by (timing->>'render')::int) as render_p50,
  percentile_cont(0.5) within group (order by (timing->>'segment')::int) as segment_p50,
  percentile_cont(0.5) within group (order by (timing->>'assets')::int) as assets_p50,
  percentile_cont(0.5) within group (order by (timing->>'encode')::int) as encode_p50,
  percentile_cont(0.95) within group (order by elapsed_ms) as total_p95
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
```

(위 코드 블록 안의 `sql` 펜스는 문서에서 그대로 쓴다.)

- [ ] **Step 4: `docs/web-trial-load.md` 작성**

```markdown
# 웹 체험판 부하·리소스 분배 결과

- 설계: `docs/superpowers/specs/2026-09-15-web-trial-quality-speed-load-design.md` §6
- 판정 기준: 동시 20건에서 504와 JSON 아닌 500이 0건, 슬롯 대기 초과는 미차감 `busy`, RSS 2 GB의 60% 이하, 요청이 한 인스턴스에 몰리지 않음. p95는 기록만(참고 15초)

## 1. 동시 요청 (프리뷰, 국어 3쪽)

`scripts/trial_bench/load.py` 결과를 붙인다. `TRIAL_PARSE_CONCURRENCY`별로 표를 나눈다.

### concurrency = 2 (현재 기본)

(측정 전)

### concurrency = 1

(측정 전)

## 2. 콜드 스타트

| 유휴 | 첫 호출 wall | instance_age_s | 비고 |
|---|---|---|---|
| 30분 | | | |
| 2시간 | | | |

## 3. 복잡도 대 시간 (로컬, `scripts/trial_bench/complexity.py`)

(측정 전)

상한 결정: Vercel 추정(로컬 × 4.3)이 30초를 넘지 않는 가장 큰 값을 `TRIAL_MAX_WORDS_PER_PAGE`·`TRIAL_MAX_DRAWINGS_PER_PAGE` 기본값으로 쓴다. 모두 30초 아래면 기본값 8000 / 10000을 유지한다.

## 4. 메모리 (로컬, `scripts/trial_bench/memory.py`)

(측정 전)

## 5. Performance CPU 실험

(측정 전 — 켜고 프로브, 되돌린 뒤 기록)

## 6. 판단

- 운영 `TRIAL_PARSE_CONCURRENCY`:
- L1·L2·L4 필요 여부(분배 문제 + CPU 원인일 때만):
```

- [ ] **Step 5: 스파이크 문서 §4 뼈대**

`docs/web-trial-spike-results.md` 끝에 추가:

```markdown
## 4. 프리뷰 프로브 (Plan 3, `scripts/trial_bench/probe.py`)

- 배포:
- 설정 변경 없음 / `EDB_PROBLEM_ASSET_WORKERS=1` 각각 측정

(측정 전)
```

- [ ] **Step 6: 커밋**

```bash
git add docs/web-trial-operations.md docs/web-trial-load.md docs/web-trial-spike-results.md
git commit -m "docs: describe preview measurement setup and result tables"
```

---

### Task 14: 사용자 체크포인트 — 대시보드 작업과 운영 검증

이 Task는 사용자가 한다. 끝나면 Fable이 검증한다.

- [ ] **Step 1: Supabase SQL Editor에서 마이그레이션 실행** (Task 1의 새 열 포함)

```bash
pbcopy < /Users/clmagi/Desktop/Projects/edb_mak/.claude/worktrees/web-trial/supabase/migrations/20260915000000_web_trial.sql
```

붙여넣고 Run. 확인 SQL: `select column_name from information_schema.columns where table_name = 'trial_events';` 에 `timing`, `instance_id`, `reject_detail`, `complexity`가 보인다.

- [ ] **Step 2: Vercel Production 환경변수 6개 + `TRIAL_HOSTNAMES=edb-parser-trial.vercel.app`** (운영 문서 §2-3). `TRIAL_SPIKE_TOKEN`은 지운다.

- [ ] **Step 3: Turnstile 위젯 Hostname에 `edb-parser-trial.vercel.app` 추가**

- [ ] **Step 4: 사용자가 `web-trial`을 푸시** (Task 1~13 커밋) → 운영 재배포. 환경변수만 바꾼 경우에도 Redeploy.

- [ ] **Step 5: Ignored Build Step, 자동화 우회, Preview 환경변수** (운영 문서 §2-5)

- [ ] **Step 6: Fable 검증**

```bash
curl -s https://edb-parser-trial.vercel.app/api/health
# {"status":"ok","commit":"<7자리>","ready":true,"instance_id":"...","instance_age_s":...}
curl -s https://edb-parser-trial.vercel.app/api/config
# turnstile_site_key가 null이 아님
```

앱 내 브라우저로 공개 주소에서 국어 원본 16쪽 PDF를 올려 결과·"앞 3쪽" 배너·박스·카드를 확인한다. Supabase에서:

```sql
select status, reject_code, reject_detail, instance_id, timing, complexity, elapsed_ms
from public.trial_events order by id desc limit 5;
```

`timing`에 `render`·`segment`·`assets`·`encode`가 있고 `complexity`에 `words`·`drawings`가 있으면 통과.

---

### Task 15: 프리뷰 측정 실행 (Haiku 실행, Fable 판단)

- [ ] **Step 1: 측정 브랜치**

```bash
git branch -f web-trial-bench web-trial
```

사용자가 `git push classinkr web-trial-bench`로 푸시한다. Vercel 대시보드에서 프리뷰 URL을 받아 `PREVIEW=https://<...>.vercel.app`로 둔다.

- [ ] **Step 2: 프로브 (기본 설정)**

```bash
export VERCEL_AUTOMATION_BYPASS_SECRET=<대시보드 값>
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/probe.py $PREVIEW ~/edb-trial-bench/inputs/*.pdf --repeat 5 --json ~/edb-trial-bench/probe-default.jsonl
```

표를 `docs/web-trial-spike-results.md` §4에 붙인다.

- [ ] **Step 3: 동시 요청 (concurrency 2 → 1)**

```bash
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/load.py $PREVIEW "~/edb-trial-bench/inputs/2026학년도-수능-국어-언어와매체-홀수형.pdf" --concurrency 5 --json ~/edb-trial-bench/load-c2.jsonl
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/load.py $PREVIEW "<같은 파일>" --concurrency 10 --json ~/edb-trial-bench/load-c2.jsonl
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/load.py $PREVIEW "<같은 파일>" --concurrency 20 --json ~/edb-trial-bench/load-c2.jsonl
```

사용자가 Preview 환경변수 `TRIAL_PARSE_CONCURRENCY=1`을 넣고 재배포하면 같은 세 번을 `load-c1.jsonl`로 반복한다. 두 표를 `docs/web-trial-load.md` §1에 붙인다.

- [ ] **Step 4: L5 A/B**

사용자가 Preview 환경변수 `EDB_PROBLEM_ASSET_WORKERS=1`을 넣고 재배포하면 Step 2를 `probe-workers1.jsonl`로 반복한다. `assets` p50 차이를 §4에 적는다. 차이가 5% 미만이면 운영에는 넣지 않는다.

- [ ] **Step 5: 콜드 스타트**

프리뷰를 30분 두었다가 국어 3쪽을 1회, 다시 2시간 두었다가 1회 호출해 `docs/web-trial-load.md` §2를 채운다.

- [ ] **Step 6: 복잡도·메모리 (로컬)**

```bash
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/complexity.py
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/memory.py ~/edb-trial-bench/inputs/*.pdf --synthetic-2xa3
```

두 표를 §3·§4에 붙이고 §3의 규칙대로 상한 기본값을 정한다. 기본값이 바뀌면 `trial_input.InputLimits`·`trial_config.from_env`·`test_trial_config.py`를 같이 고친다.

- [ ] **Step 7: Performance CPU 실험**

사용자가 한가한 시간에 Function CPU를 Performance로 바꾸면 Step 2를 `probe-performance.jsonl`로 한 번 돌리고, 사용자가 되돌린다. §5에 기록만 한다.

- [ ] **Step 8: Fable 판단과 커밋**

`docs/web-trial-load.md` §6에 운영 `TRIAL_PARSE_CONCURRENCY`와 L1·L2·L4 필요 여부를 적는다. 판단 규칙(스펙 §6·§7): 실패 0건이면서 인스턴스당 겹침이 적고 p95가 낮은 concurrency를 고른다. 504·메모리 초과·슬롯 대기 초과가 나왔고 원인이 CPU 시간이면 Plan 4에 L1·L2·L4를 연다.

```bash
git add docs/web-trial-load.md docs/web-trial-spike-results.md
git commit -m "docs: record preview probe, load, cold start, complexity, and memory results"
```

---

### Task 16: 코퍼스 실행과 판정 준비 (Haiku 실행, Fable 판정)

- [ ] **Step 1: 입력 압축본 7개**

```bash
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/make_inputs.py --subject korean "/Users/clmagi/Desktop/Projects/omr_maker/output/pdf/2025학년도-수능-국어-언어와매체-홀수형.pdf" "/Users/clmagi/Desktop/Projects/omr_maker/output/pdf/2026학년도-9월-모평-국어-언어와매체.pdf" "/Users/clmagi/Desktop/Projects/omr_maker/output/pdf/2026학년도-수능-국어-언어와매체-홀수형.pdf"
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/make_inputs.py --subject science /Users/clmagi/Desktop/Projects/edb_mak/tmp_test_inputs/earth_input.pdf /Users/clmagi/Desktop/Projects/edb_mak/tmp_test_inputs/physics_input.pdf "/Users/clmagi/Downloads/파일/문제 모음집/01 물리학Ⅰ_문제지.pdf" "/Users/clmagi/Downloads/클래스인 다운로드 위컴/전자기 교재문제.pdf"
ls ~/edb-trial-bench/inputs
```

Expected: PDF 7개, `~/edb-trial-bench/cases.json`에 7개 항목

- [ ] **Step 2: 체험판 관측**

```bash
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/observe.py
```

Expected: 국어 3개는 문항 9·지문 2~3, 과학 계열은 문항 16·16·16·12 (2026-09-15 로컬 결과와 같아야 한다)

- [ ] **Step 3: 오라클 (Gemini 호출, 케이스당 3쪽)**

```bash
.venv/bin/python scripts/trial_bench/oracle.py --runtime-dir /Users/clmagi/Desktop/Projects/edb_mak/.app_runtime
```

`fail_on_error=True`라 Gemini 오류는 케이스 실패로 나온다. 실패한 케이스만 다시 돌린다. 같은 케이스를 두 번 돌려 문항 번호 집합이 다르면 그 사실을 `docs/web-trial-quality.md`에 적는다(스펙 §13).

- [ ] **Step 4: 채점과 판정 자료**

```bash
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/score.py --doc docs/web-trial-quality.md
GEMINI_API_KEY= .venv/bin/python scripts/trial_bench/adjudicate.py
ls ~/edb-trial-bench/adjudication/*/
```

- [ ] **Step 5: 커밋 (결과 표만)**

```bash
git add docs/web-trial-quality.md
git commit -m "docs: record the first trial corpus scores (labels pending)"
```

- [ ] **Step 6: Plan 4로 넘길 것**

`~/edb-trial-bench/labels/*.json`(모두 `pending`)과 `adjudication/` 이미지가 Fable 판정 입력이다. 판정·수정·배지 보정·과목 확장은 Plan 4에서 한다.
