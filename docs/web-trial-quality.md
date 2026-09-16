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
