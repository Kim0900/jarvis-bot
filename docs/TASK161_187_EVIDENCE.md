# Task #161 / #187 증거 패키지 (캐스퍼 제출, 자체 PASS 아님 — 검증 카산드라, 판단 마기)

범위: 시험만 추가. `bot_v5_legacy.py` 변경 없음, 운영 접속·배포·쓰기 없음. 실제 코드(HealthHandler `/mcp/ops_snapshot`, `build_agent_context_snapshot`, `run_geomnuri_patrol_once`, `queue_ingestion_notification` + 워커)를 프로세스 내 PostgREST 대역(`tests/fake_postgrest.py` 하위 클래스: `not.in`·다중 정렬 추가)과 가짜 Telegram 위에서 실행.

## 카산드라 4932 항목별 증거
| 항목 | 시험 |
|---|---|
| E2E 응답 | H1–H3 (실제 HTTP 서버, 키 검증 401, GET 외 요청 0건) |
| 전역 최근 20건 밖 옛 사건 복구 | H1, H4 (task 68 → #3553, 최근 20건과 서로소 확인) |
| 6시간 쿨다운·장애 주입 | P1–P7 (5h59 억제 / 6h01 재경보, 상태 불변, 쓰기 실패 시 쿨다운 미소진) |
| 재배포 후 회귀 | `.github/workflows/task161-187-evidence.yml`로 PR마다 재실행 |

## 발견 (현재 동작을 시험으로 고정 — 옳다는 주장 아님)
- **F161-1** 복구 쿼리가 전 HOLD 태스크 합쳐 `limit=500` 한 번 → 소란한 HOLD 태스크가 조용한 태스크의 latest_event를 밀어냄(`latest_event=None`). 제안: 태스크별 limit=1 또는 태스크별 창. (H5)
- **F161-2** `sb_select`가 오류를 삼킴 → 읽기 실패가 `success:true`+빈/None으로 보임(H6, H7), 순라 HOLD 점검도 실패 경보 없음(P6).
- **F161-3** 쿨다운 상태 읽기 실패 또는 `last_run_at` 파싱 불가 시 fail-open → 순라 주기마다 재경보(P4, P5).
- **F187-1** 중복 키를 포맷 **전에** 등록 → 포맷 예외 1회로 해당 알림이 프로세스 수명 동안 영구 소실(N5).
- **F187-2** ERROR 지문이 `error[:180]` → 앞 180자 같은 서로 다른 오류가 병합(N6).
- **F187-3** 키 5000개 초과 시 전체 clear → 이미 보낸 알림 재발송(N7). **F187-4** 중복제거는 프로세스 메모리 → 재시작 시 재발송(N8).
- 통과한 보증: 전송 실패가 호출자에 전파되지 않음·워커 생존(N2), 전송 정체 중에도 enqueue 즉시 반환·FIFO(N3).

## 한계
대역은 실제 PostgREST가 아님(`not.in`/다중 order 구문 실서버 검증 미실시). 운영 데이터 규모(500행 한계 도달 여부)는 미관측. 수정은 별도 지시 후.
