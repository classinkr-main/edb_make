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

1차 스윕(2026-09-16, Task 12 사전 측정)에서 시간을 지배하는 쪽은 그림이었다: 8000 drawings/쪽이 로컬 렌더 17~28초(Vercel 추정 77~126초)였고, 단어는 9185개/쪽이 로컬 3.8초로 훨씬 여유 있었다. 그림 8000이 이미 너무 높으므로, drawings 상한은 이 §3의 본 스윕이 아니라 3000~6000 사이를 더 촘촘히 훑어 정한다.

상한 결정: Vercel 추정(로컬 × 4.3)이 30초를 넘지 않는 가장 큰 값을 `TRIAL_MAX_WORDS_PER_PAGE`·`TRIAL_MAX_DRAWINGS_PER_PAGE` 기본값으로 쓴다. 모두 30초 아래면 기본값 8000 / 10000을 유지한다.

## 4. 메모리 (로컬, `scripts/trial_bench/memory.py`)

(측정 전)

## 5. Performance CPU 실험

(측정 전 — 켜고 프로브, 되돌린 뒤 기록)

## 6. 판단

- 운영 `TRIAL_PARSE_CONCURRENCY`:
- L1·L2·L4 필요 여부(분배 문제 + CPU 원인일 때만):
