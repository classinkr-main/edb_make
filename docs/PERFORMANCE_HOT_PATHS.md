# 성능 핫패스 기록 (2026-09)

파이프라인과 로컬 서버의 실제 병목을 프로파일링으로 측정하고 고친 기록이다. 숫자는 모두
같은 컨테이너에서 측정했으므로 절대값이 아니라 **변경 전후 비율**로 읽는다. 이후에 최적화를
이어갈 때는 추측하지 말고 아래 하네스로 먼저 측정한다.

## 측정 방법

### 1. 엔드투엔드 내보내기

```bash
python3 - <<'PY'
import fitz
doc = fitz.open()
for page_index in range(6):
    page = doc.new_page(width=595, height=842)
    y = 60
    for item in range(6):
        number = page_index * 6 + item + 1
        page.insert_text((60, y), f"{number}. sample problem stem", fontsize=11)
        y += 18
        for _ in range(3):
            page.insert_text((72, y), "body text line", fontsize=10)
            y += 14
        for choice in range(4):
            page.insert_text((80, y), f"{chr(9312 + choice)} choice", fontsize=10)
            y += 14
        page.draw_rect(fitz.Rect(60, y, 300, y + 40))
        y += 50
doc.save("/tmp/sample_exam.pdf")
PY

rm -rf /tmp/out /tmp/.pipeline_cache
python3 -m cProfile -o /tmp/prof.out build_problem_board_edb.py /tmp/sample_exam.pdf \
  --output-dir /tmp/out --ocr noop --subject math
python3 -c "import pstats; pstats.Stats('/tmp/prof.out').sort_stats('tottime').print_stats(25)"
```

캐시를 지우지 않으면 두 번째 실행부터 결과가 의미 없다. 반드시 `.pipeline_cache`를 함께 지운다.

### 2. 문항별 페이지 크롬 정리

`_trim_source_page_chrome`은 문항 크롭마다 한 번 호출되므로 문항 수만큼 선형으로 늘어난다.
`build_problem_board_edb._trim_source_page_chrome`을 1200x900 / 1650x2340 크롭으로 직접
호출해 측정한다.

### 3. 세션 요청 단위 작업

문항 300개, 페이지 40개짜리 합성 세션을 만들고 `collect_session_file_paths`,
`save_session_history`, `rewrite_session_for_http` 등을 개별로 잰다. 실제 사용자 세션에서
요청당 비용이 어디로 가는지 이 단위가 가장 잘 보여준다.

## 고친 것

| 위치 | 문제 | 결과 |
|---|---|---:|
| `image_ops.channel_*` | `rgb.max(axis=2)`가 길이 3인 마지막 축을 stride 3으로 훑어서 pairwise `np.maximum` 대비 10배 느렸다. 14곳에서 사용 중 | 1600x1200 채도 218ms → 21ms, 배경 거리 56ms → 16ms |
| `_remove_background_with_numpy` | 위 패턴 + 루프 불변식 재계산 | 문항당 73ms → 약 25ms |
| `_trim_edge_attached_page_chrome`, `_erase_corner_page_badges` | 파이썬 스택 flood fill + 전체 이미지 마스크. 실제로는 가장자리 띠와 네 모서리만 읽는다 | `cv2.connectedComponentsWithStats` + ROI 마스크 |
| `_trim_edge_vertical_guides` | numpy 경로가 아예 없고 같은 열을 두 번 훑었다 | dark mask 한 번 계산 후 재사용 |
| 위 세 개 합계 (`_trim_source_page_chrome`) | | 1650x2340 442ms → 159ms |
| `_detect_document_columns` | 열마다 `crop().histogram()`. 형제 함수에는 이미 numpy 경로가 있었다 | 2480x3508 80ms → 19ms, 결과 바이트 동일 |
| `detect_pdf_visual_column_divider_x` | 같은 페이지 이미지로 지문 문항마다 재실행 | 페이지당 1회 |
| `deskew_image` | `np.column_stack(np.where(...))`로 전경 좌표 전체를 파이썬에서 구성 | `cv2.findNonZero`, 55ms → 35ms, 각도 동일 |
| `preprocess._file_sha1` | 호출 지점 15곳이 같은 원본을 매번 다시 읽음 | path/size/mtime 메모이즈 |
| `AppRequestHandler.end_headers` | 모든 응답에 `no-store`. UI 번들 약 800KB를 새로고침마다 재다운로드하고, `/api/file`이 직접 보낸 `max-age=3600`도 덮어썼다 | 정적 자산은 `no-cache`로 재검증(304), API만 `no-store` |
| `AppRequestHandler` | `protocol_version` 미설정 → HTTP/1.0, 응답마다 연결 종료 | HTTP/1.1 keep-alive. 본문을 읽지 않고 거절한 응답은 `Connection: close` |
| `register_current_session` | 읽기 전용 폴링인 `GET /api/session/latest`가 히스토리 전체를 매번 다시 쓰고 fsync 2회 | 헤드가 같으면 쓰지 않음, 300문항 기준 88ms 제거 |
| `collect_session_file_paths` | 참조마다 URL 파싱 + realpath, 히스토리 스캔은 이를 20번 반복 | 참조→정규 경로 메모이즈(존재 확인은 매번), 118ms → 8.8ms |
| `_session_pages_json_pages` | 응답 하나를 만들면서 `pages.json`을 2~3번 파싱 | 디스크 리비전당 1회 |
| `_build_file_preview_payload` | `lru_cache(maxsize=32)`. 문항 수백 개 보드에서는 계속 축출 | 바이트 상한 LRU |
| `_mutate_exclude_many`, `_mutate_merge` | 요청 id마다 문항 목록 전체 재스캔 | 한 번의 패스 |

엔드투엔드 6페이지/30문항 내보내기(캐시 없음) 10.1s → 7.0s.

## 아직 남은 것

- **`_artifact_job_lock`이 아티팩트 엔드포인트 전체를 직렬화한다.** `/api/export`가 인식과
  EDB 빌드를 이 락 안에서 통째로 돌리기 때문에 수 분짜리 내보내기가 1초짜리 크롭을 막는다.
  `ThreadingHTTPServer`의 동시성이 사실상 무효가 된다. 다만 락을 좁히면 파일 쓰기 경합
  범위가 바뀌므로 세션 CAS가 실제로 무엇을 보호하는지 확인한 뒤에 손대야 한다.
- **업로드 본문이 요청마다 최대 3번 해시된다.** `_save_uploaded_file`이 구한 다이제스트를
  `_source_identity_suffix`가 재사용하지 않는다.
- **`GET /api/session/history`가 세션 스냅샷 10개를 파싱한 뒤 전부 버린다.**
  (`_public_session_history`가 `session` 키를 제거한다.) 스냅샷을 별도 파일로 두면
  이 엔드포인트는 거의 공짜가 된다.
- **`_smooth_projection`이 O(n·window)다.** prefix sum이면 O(n)이지만 float 입력에서
  마지막 자리 차이가 생길 수 있으므로, 정수 투영만 쓰는 것을 확인하고 바꾼다.
- **`_align_content_mask`의 81개 후보 오프셋**은 여전히 전체 해상도에서 돈다. 4배 축소로
  대략 찾고 최적 오프셋만 원해상도로 다듬으면 더 줄일 수 있다. 결과가 바뀌므로 AI 재구성
  품질 확인이 함께 필요하다.

## 최적화할 때 지킨 규칙

1. **결과가 바뀌지 않는 것부터 한다.** `channel_saturation`, `channel_distance`,
   `channel_magnitude`는 대체한 식과 연산 순서까지 같게 두어 비트 단위로 동일하다.
   `test_image_ops.py`가 이를 직접 검증한다.
2. **결과가 바뀌는 최적화는 품질 코퍼스 없이 하지 않는다.** `deskew_image`를 축소 이미지로
   추정하면 30배 빨라지지만 0.2도 임계값 근처에서 판정이 뒤집힐 수 있어, 완전히 동일한
   `cv2.findNonZero` 쪽만 적용했다.
3. **캐시는 무엇이 바뀌면 무효가 되는지 먼저 정한다.** 파일 기반 캐시는 (경로, 크기, mtime)을
   키로 쓰고, 존재 확인처럼 값싸고 자주 바뀌는 검사는 캐시 밖에 둔다.
