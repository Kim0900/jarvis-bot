# TASK#214 측정용 스냅샷 수집 절차 (준비본 — 실행 전)

상태: **준비만 됨. 운영 DB에는 한 번도 접속하지 않았다.** 마기가 "수집 시작"이라고 하기 전에는 `--live`로 돌리지 않는다(대표 지시 2026-10-20).
근거: 설계 `docs/design/task214_amplification_v1.md` 4절(PR#78), 마기 판정 #5720 (읽기 전용 pg_stat 스냅샷, 쓰기·DDL·설정 변경·부하 추가 금지, 연결 불가 시 재시도 폭주 금지·1회, 연결 실패도 측정치).

## 1. 구성

| 파일 | 역할 |
|---|---|
| `tools/task214/task214_snapshot.py` | 수집기. 기본은 **드라이런**(접속 없음, DSN 변수도 읽지 않음). `--live`일 때만 접속 |
| `tools/task214/task214_snapshot_diff.py` | 오프라인 분석(파일만 읽음). 스냅샷 쌍의 초당 변화율, 무효·실패 쌍 표시 |
| `tools/task214/task214_snapshot_selftest.py` | 자체 시험 52건(로컬 일회용 PG17에서만; 승인 게이트·합성키·커버리지 포함). CI: `.github/workflows/task214-snapshot-tools.yml` |

## 2. 수집 시점 (5회)

| 단계 `--phase` | 의미 | 프로필 | 시각(계획) |
|---|---|---|---|
| `pre` | 정상 전 기준선 | full | 마기 지정 (정지가 없는 정상 시각) |
| `during1` | 정지 중 1 | **light** | 마기 지정 (설계 예: 17:30) |
| `during2` | 정지 중 2 | **light** | 마기 지정 (설계 예: 18:30) |
| `post_immediate` | 정지 직후 | full | 마기 지정 (설계 예: 19:35; 종료는 19:26~19:29 관측) |
| `post_normal` | 정상 후 | full | 마기 지정 (설계 예: 21:00) |

- 정지 시작 시각은 날마다 다르다(10/19는 14:15). 그래서 **정지 중 시각은 달력 시각이 아니라 마기가 정지를 확인한 뒤** 정한다. 정지가 없는 날은 `pre`/`post_normal`만 찍히고 비교 대상(정지 중)이 없다 — 그 자체가 결과다.
- 모든 스냅샷은 같은 `stats_reset`(2026-02-12 23:51:26Z) 기준 누적치다. 값이 바뀌면 그 쌍은 폐기된다(분석 도구가 자동 검사).
- 단계별 시각은 `--planned-at "17:30 KST"` 로 기록하되, 실제 시각은 파일 안에 서버·클라이언트 시계로 따로 남는다.

## 3. 정지 중 부하 최소화 설계

- **light 프로필**: 질의 7개(메타, pg_stat_io, checkpointer, bgwriter, wal, database, activity 집계) + 보호값 확인 1개. `pg_stat_statements` 상위와 `pg_stat_user_tables`(무거운 쪽)는 **정지 중에는 수집하지 않는다**. 대신 `pre`/`post_*`의 full 스냅샷으로 그 부분을 본다(대가: 정지 구간 안의 statements 변화는 직접 못 본다. 마기가 원하면 `--profile full`로 바꿀 수 있고 파일에 기록된다).
- **접속은 1회**. 질의는 하나씩 순서대로(병렬 없음), 하나의 읽기 전용 트랜잭션. `statement_timeout` 3 s(light) / 8 s(full), `lock_timeout` 500 ms, `idle_in_transaction_session_timeout` 10 s를 `SET LOCAL`로 걸고 **실제 적용값을 되읽어** 다르면 질의 없이 중단(`guard_failed`).
- **첫 번째 타임아웃·연결 끊김에서 즉시 종료**. 남은 질의는 `skipped`로 기록한다(이어서 두들기지 않는다).
- 접속 한도: `connect_timeout` 10 s, `tcp_user_timeout` 10 s, keepalive. 그래도 소켓 호출이 안 돌아오면 **워치독**이 deadline(light 25 s / full 60 s)에 그때까지의 데이터를 쓰고 프로세스를 끝낸다.
- **재시도 없음**. 단계마다 시도 표식(`.attempt_<phase>`)을 접속 전에 만든다. 같은 단계의 두 번째 시도는 거부된다. **서명된 승인이 그 단계를 `repeat_phases`에 적었을 때만** 1회 더 허용되고(세 번째는 어떤 경우에도 거부), 접속이 도중에 죽어도 표식은 남는다. 예전의 `--allow-repeat` 스위치는 **없어졌다**(자기 발급 가능했음).
- **연결 실패·타임아웃·워치독 종료는 결과**다: `outcome`(connect_failed / query_timeout / connection_lost / watchdog / guard_failed / partial)과 시작 시각(UTC·KST), 경과 ms, 오류 종류, SQLSTATE, 스크럽한 메시지가 파일에 남고 종료 코드는 3이다. 분석 도구는 이런 스냅샷을 건너뛰지 않고 표에 올리며, 포함된 쌍은 `UNMEASURABLE`로 표시한다.

## 4. 실행 전 점검표 (마기 "수집 시작" 이후에만)

1. **실행 승인(v2, 카산드라 C214-79-03)**: `--live`는 마기가 서명한 승인 파일(`--approval A.json`)과 마기의 공개키(`--approval-pubkey P.pem`)가 있어야만 돈다. 자기 발급 스위치(`--auth-event`, `--allow-repeat`)는 없앴다. 도구가 접속 전에 확인하는 것: ① 공개키 SHA-256이 도구 안의 `TRUST_ANCHOR_PUBKEY_SHA256`과 같다(출고 값은 **None → 마기가 앵커를 넣기 전에는 어떤 실행도 불가능**). ② Ed25519 서명이 맞다. ③ 승인에 적힌 `tool_sha256`이 지금 실행하는 파일의 sha256과 같다(기록된 SHA). ④ 단계가 `phases`에 있다. ⑤ 시간 창(최대 6시간) 안이다. ⑥ `magi_event`(마기 "수집 시작" 원장 번호)가 양의 정수다 — 스냅샷에 승인 id·이벤트 번호·도구 sha256이 기록된다. ⑦ 접속 뒤 세션 역할이 `db_role`과 같고, 슈퍼유저/createrole/createdb/replication/bypassrls 역할이면 서명된 `allow_privileged_role`이 없을 때 **통계를 읽기 전에** `guard_failed`로 끝난다.
   - 승인 payload 필드: `schema, approval_id(uuid), magi_event, tool_sha256, phases, repeat_phases, not_before, not_after, operator, db_role, allow_privileged_role`(서명 대상은 정렬 JSON). 필드가 더하거나 모자라면 거부된다.
   - 마기가 할 일: 키쌍 생성·보관, 공개키 SHA-256을 `TRUST_ANCHOR_PUBKEY_SHA256`에 넣은 파일의 sha256을 확인·서명. 키 보관은 마기/대표 몫이며 캐스퍼는 개인키를 갖지 않는다.
2. 체크아웃의 `task214_snapshot.py` sha256이 승인에 적힌 값과 같은지는 도구가 검사하지만, **마기가 검토한 파일과 같은지**는 마기가 서명할 때 비교한다(스냅샷 파일에도 `tool_sha256`이 남는다).
3. DSN은 환경변수 `TASK214_DSN`으로만 받는다(파일·인자·로그에 쓰지 않는다). **권한은 `pg_monitor`/`pg_read_all_stats` 정도의 별도 읽기 전용 역할을 쓴다**(권고가 아니라 승인의 `db_role`+`allow_privileged_role:false`로 강제된다). 엔드포인트(직접 5432 또는 풀러)는 마기가 정한다.
4. 단계마다 먼저 드라이런으로 계획과 SQL을 확인한다:
   `python3 tools/task214/task214_snapshot.py --phase during1 --out-dir <dir>`
5. 실행: `TASK214_DSN=... python3 tools/task214/task214_snapshot.py --phase during1 --out-dir <dir> --live --approval A.json --approval-pubkey P.pem --planned-at "17:30 KST"`
6. 정지 중 연결 확인용 사전 핑·수동 재접속은 하지 않는다. 한 번 시도한 결과가 곧 측정치다.

## 5. 결과 처리

- 출력: `<dir>/task214_snap_<phase>_<UTC>.json`(0600) + `.sha256`. 파일 내용은 통계 카운터와 집계뿐이다(사용자명·질의 값 없음; statements는 정규화된 질의 앞 120자, `dbid`/`userid`는 숫자 oid). 스냅샷 스키마는 v2다(v1 파일은 분석에서 statements를 비교하지 않는다).
- 원장에는 각 스냅샷의 `outcome`, 시각, `sha256`, 요약을 올린다(원본 파일 전체는 마기 지정 위치에 보관).
- 분석: `python3 tools/task214/task214_snapshot_diff.py pre.json during1.json during2.json post_immediate.json post_normal.json --md report.md --json report.json` (시간순으로 넣는다. 순서가 어긋나면 표시된다).
- **쿼리 통계 비교 규칙(v2, C214-79-01/02)**: 쿼리 키는 `(dbid, userid, queryid, toplevel)` 합성키다. 같은 `queryid`가 다른 사용자·DB·toplevel이면 별개 행이다. 한 스냅샷에 같은 합성키가 두 번 나오면 그 섹션을 **거부**하고(계산하지 않음) 이유를 쓴다. 상위 20개는 스냅샷마다 다시 자르므로(시간·호출·행 각 20) 비교는 두 쪽에 모두 있는 키만 계산하고, 보고서에 **커버리지**를 쓴다: 양쪽에 있는 키 수, 새로 들어온/빠진 키 목록, 경고, 그리고 `statements_totals`(pg_stat_statements 전체 합계)가 있으면 표가 보여주는 호출/실행시간이 구간 전체 중 몇 %인지. 표는 "전체 활동"이 아니다.
- 해석 규칙(설계 4절, 사전 등록): 어떤 상관도 원인 확정이 아니다. 단계 간 구조적 차이가 나온 항목은 후보 가설이며, 대시보드 Disk IO 그래프 캡처와 Supabase 지원 답변으로 확인한다. 이 측정은 통계를 reset하지 않는다.

## 6. 한계 (숨기지 않음)

- 운영 서버(PG17.6)에서는 한 번도 돌려보지 않았다. 시험은 로컬 일회용 PG17.10 서버와 프록시(연결 차단·무응답·중간 정지)로 했다. 풀러(Supavisor) 경유 동작은 미검증 — 트랜잭션 `SET LOCAL`만 쓰고 시작 파라미터 `options`는 쓰지 않아 호환되도록 했다.
- `track_io_timing=off`(카산드라 #5715)라 `pg_stat_io`의 `*_time`은 0이다. 횟수·바이트만 측정되고 지연은 측정되지 않는다. 설정은 바꾸지 않는다(변경 금지).
- 수집 자체도 부하다(접속 1개, 작은 질의 약 10개; full은 `pg_stat_user_tables` 전체 포함). "부하 0"은 보장하지 않는다 — 정지 중에는 light로 최소화했을 뿐이다. 정지된 서버에 접속 시도 자체가 쌓일 수 있으므로 정지 중 시도는 단계당 1회로 제한했다.
- 같은 정지에서 `stats_reset`/재시작이 끼면 쌍이 폐기될 수 있다.
- 읽기 전용은 코드(READ ONLY 트랜잭션, 고정 SELECT 12개, 자체 시험이 SQL 키워드를 검사)와 되읽기 검사, 서명 승인이 강제하는 읽기 전용 DB 역할로 보장한다. **도구 파일을 고치거나 임의의 파이썬을 돌릴 수 있는 사람은 도구 안의 어떤 검사도 우회할 수 있다**(신뢰 앵커가 파일 안의 상수이므로) — 대응은 절차다: 마기가 서명하기 전에 파일 sha256을 비교하고, DB 역할은 서버 쪽에서 읽기 전용이어야 한다. 이 점은 숨기지 않는다.

## 7. 마기가 정해야 할 것

0. **서명 키쌍과 신뢰 앵커**(없으면 어떤 실행도 불가능): 키 생성·보관, 앵커 값 확정, 승인 파일 서명.
1. "수집 시작" 시점과 단계별 시각(특히 정지 확인 기준).
2. DSN의 역할(읽기 전용 권고)과 엔드포인트(직접/풀러), 자격 전달 방식.
3. 수집 파일 보관 위치.
4. 마기 #5720의 "지금 기준선 1회"는 대표 지시(수집 시작 전 실행 금지)에 따라 **아직 하지 않았다** — 필요하면 `pre`를 "수집 시작" 이후 첫 단계로 실행한다.
