# TASK#214 측정 스냅샷 분석 절차 (범위 축소판 — 마기 #5848)

상태: **분석 도구(파일만 읽음)와 이 문서만 남았다.** 수집기와 서명 승인 장치는 삭제했다(마기 #5848: 읽기 전용 통계 조회에 비해 승인 장치가 복잡해져 같은 건의 반복 FAIL — 카산드라 C214-79-04/05 — 을 만들었다). 이 PR의 어떤 코드도 데이터베이스나 네트워크에 접속하지 않는다. 운영 DB에는 한 번도 접속한 적이 없다.
근거: 설계 `docs/design/task214_amplification_v1.md` 4절(PR#78), 마기 판정 #5720·#5848.

## 1. 구성
| 파일 | 역할 |
|---|---|
| `tools/task214/task214_snapshot_diff.py` | 오프라인 분석. 스냅샷 파일 2개 이상을 시간순으로 받아 쌍별 초당 변화율·무효/실패 쌍 표시·쿼리 통계 커버리지를 낸다. 표준 라이브러리만 쓴다(`argparse, datetime, json, sys, pathlib`) |
| `tools/task214/task214_snapshot_selftest.py` | 자체 시험 34건. DB·네트워크·추가 패키지 없이 합성 파일로 돈다(CI: `.github/workflows/task214-snapshot-tools.yml`) |

## 2. 입력 형식 (`task214-snapshot-v2`, 분석 도구가 읽는 계약)
수집은 마기가 직접 읽기 전용 SQL로 한다(10/20 수행분 #5726·#5758·#5779·#5794). 분석 도구에 넣으려면 아래 JSON으로 정리한다. **#5794 같은 원장 요약 형식(`db/io/bgw/wal/ckpt/ts_utc`)은 이 도구가 직접 읽지 못한다** — 필요하면 변환기를 따로 만들 수 있다(미작성).
```
{"schema": "task214-snapshot-v2", "phase": "pre|during1|during2|post_immediate|post_normal", "profile": "full|light",
 "outcome": "ok|partial|connect_failed|query_timeout|connection_lost|watchdog|guard_failed|tool_error",
 "t_start_utc": ISO, "t_start_kst": ISO, "connect": {"ok": bool, "ms": int, "error_class": str|null, "sqlstate": str|null, "message": str|null},
 "skipped": [section names], 
 "sections": { "<name>": {"ok": true, "data": ...} | {"ok": false, "unavailable": bool, ...} } }
```
섹션 이름과 `data`: `meta`(server_clock, postmaster_start 필수), `io`(pg_stat_io 행 배열), `checkpointer`, `bgwriter`, `wal`(각 1행 객체, stats_reset 포함), `database`(pg_stat_database 행 배열), `activity`, `statements_info`(stats_reset 필수), `statements_top`(행 배열, **키 dbid·userid·queryid·toplevel 필수**), `statements_totals`(n_entries, sum_calls, sum_total_exec_time, sum_rows).
`outcome`이 ok/partial이 아닌 스냅샷(연결 실패·타임아웃 등)은 **그 자체가 측정치**로 표에 올라가고 그 스냅샷을 포함한 쌍은 `UNMEASURABLE`이 된다.

## 3. 분석 규칙 (사전 등록)
- 쌍이 유효하려면 같은 `postmaster_start`, 같은 통계 `stats_reset`이어야 한다. 다르면 해당 섹션(또는 쌍)을 버리고 이유를 쓴다. 카운터가 거꾸로 간 행은 표시하고 버린다(음수 변화율 없음).
- **쿼리 통계 키(C214-79-01)**: `(dbid, userid, queryid, toplevel)` 합성키. 같은 `queryid`라도 사용자·DB·toplevel이 다르면 별개 행이다. 합성키가 없는 행(구형 queryid 단독)은 비교하지 않고 이유를 쓴다. 한 스냅샷에 같은 합성키가 두 번 나오면 그 섹션을 **거부**하고 계산하지 않는다(어느 쪽인지 표시).
- **상위 20개 커버리지(C214-79-02)**: 상위 N은 스냅샷마다 다시 잘리므로 두 스냅샷 모두에 있는 키만 변화율을 계산한다. 보고서에 양쪽 키 수, 공통 수, 새로 들어온/빠진 키, 경고, 그리고 `statements_totals`가 있으면 표가 보여주는 호출·실행시간이 구간 전체의 몇 %인지를 쓴다. 표는 \"전체 활동\"이 아니다. 합계가 없으면 커버리지를 알 수 없다고 쓴다. pg_stat_statements에서 밀려난 항목(dealloc)이 있으면 \"전체\"조차 하한이다.
- 어떤 상관도 원인 확정이 아니다. 단계 간 차이는 후보 가설이며 대시보드 Disk IO 그래프(24h 단위)와 Supabase 지원 답변으로 확인한다.

## 4. 입력 규칙과 종료코드 (C214-79-06)
| 종료코드 | 의미 |
|---|---|
| 0 | 입력이 모두 정상이고 **유효한 쌍이 1개 이상**. 단 `PARTIAL` 배너가 붙을 수 있다(아래) |
| 2 | 사용·입력·출력 오류 — **분석하지 않았다**: 파일 2개 미만, 같은 파일을 두 번 지정, 읽을 수 없음/JSON 아님/스냅샷 아님/필수 필드(`schema, phase, profile, outcome, t_start_utc, t_start_kst, sections`) 누락/시각 형식 오류, 보고서 파일 쓰기 실패 |
| 3 | 입력은 읽혔지만 **분석 가능한 쌍이 0건**(전부 UNMEASURABLE/DISCARDED). 정상 완료가 아니다. 보고서(`--md`/`--json`)는 **그대로 써지고** 실패한 관측이 결과로 들어 있다 |
- 보고서 맨 위 배너: `Complete`(모든 쌍 유효·실패 관측 없음) / `PARTIAL`(유효한 쌍이 있으나 실패·부분 관측, 유효하지 않은 쌍, 또는 버려진 섹션이 있음 — 모두 나열됨) / `NO ANALYSABLE PAIR`(종료코드 3). JSON에는 같은 내용이 `summary`(`analysable, complete, valid_pairs, pairs, failed_observations, partial_observations, non_valid_pairs, valid_pairs_with_notes, informational_missing_section_notes`)로 들어간다.
- 실패한 관측(연결 실패·타임아웃 등)은 **어느 종료코드에서도 보고서 표에서 빠지지 않는다.** 알 수 없는 `outcome` 값은 ok로 취급하지 않고 UNMEASURABLE로 올린다.
- 한 스냅샷에만 없는 섹션(light 프로필, 확장 미설치)은 정보 항목이며 `Complete`를 막지 않는다. 버려진 섹션(reset 불일치, 중복 키 등)은 막는다.
- **결정 사항(마기 확인 요청)**: 유효한 쌍이 하나라도 있으면 종료코드는 0이고 부분성은 배너·`summary`로만 알린다. 부분 결과에 별도 종료코드(예: 4)를 주길 원하면 알려 달라.

## 5. 사용
`python3 tools/task214/task214_snapshot_diff.py pre.json during1.json post_immediate.json --md report.md --json report.json` (시간순). 출력은 표준출력과 선택 파일뿐이다.

## 6. 참고용 읽기 전용 SQL (마기가 수집할 때 쓰는 목록 — 이 PR이 실행하지 않는다)
삭제된 수집기가 쓰던 고정 SELECT 11개. 한 읽기 전용 트랜잭션에서 하나씩 순서대로, `statement_timeout`·`lock_timeout`을 짧게 건다. `track_io_timing=off`이면 `pg_stat_io`의 `*_time`은 0이다(카산드라 #5715, 설정은 바꾸지 않는다). statements 결과의 `dbid/userid`는 숫자 oid이고 질의 문장은 앞 120자 정규화 텍스트뿐이다.
```sql
-- [meta] (light/full)
select now() as server_now, clock_timestamp() as server_clock, pg_postmaster_start_time() as postmaster_start, pg_is_in_recovery() as in_recovery, version() as version, current_setting('track_io_timing') as track_io_timing, current_setting('stats_fetch_consistency') as stats_fetch_consistency;
-- [io] (light/full)
select coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) from pg_stat_io t;
-- [checkpointer] (light/full)
select to_jsonb(t) from pg_stat_checkpointer t;
-- [bgwriter] (light/full)
select to_jsonb(t) from pg_stat_bgwriter t;
-- [wal] (light/full)
select to_jsonb(t) from pg_stat_wal t;
-- [database] (light/full)
select coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) from pg_stat_database t;
-- [activity] (light/full)
select coalesce(jsonb_agg(to_jsonb(a)), '[]'::jsonb) from (select backend_type, application_name, state, wait_event_type, count(*) as n, max(extract(epoch from now() - xact_start))::int as max_xact_age_s from pg_stat_activity group by 1, 2, 3, 4 order by 1, 2, 3, 4) a;
-- [statements_info] (full)
select to_jsonb(i) from pg_stat_statements_info i;
-- [statements_top] (full)
with s as (select dbid, userid, queryid, toplevel, calls, total_exec_time, rows, shared_blks_hit, shared_blks_read, shared_blks_dirtied, shared_blks_written, left(query, 120) as q from pg_stat_statements), t as (select * from s order by total_exec_time desc limit 20), c as (select * from s order by calls desc limit 20), r as (select * from s order by rows desc limit 20) select coalesce(jsonb_agg(to_jsonb(u)), '[]'::jsonb) from (select * from t union select * from c union select * from r) u;
-- [statements_totals] (full)
select to_jsonb(x) from (select count(*) as n_entries, coalesce(sum(calls), 0) as sum_calls, coalesce(sum(total_exec_time), 0) as sum_total_exec_time, coalesce(sum(rows), 0) as sum_rows from pg_stat_statements) x;
-- [user_tables] (full)
select coalesce(jsonb_agg(to_jsonb(u)), '[]'::jsonb) from (select schemaname, relname, seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del, n_live_tup, n_dead_tup, vacuum_count, autovacuum_count, analyze_count, autoanalyze_count from pg_stat_user_tables) u;
```

## 7. 한계 (숨기지 않음)
- 이 도구는 마기의 수동 수집 형식(#5794)을 직접 읽지 못한다(2절). 변환기는 **이번 범위가 아니다**(마기 #5891).
- 합성 파일로만 시험했다. 실제 운영 서버(PG17.6) 출력과의 형식 일치는 마기가 첫 변환 때 확인해야 한다.
- 수집 자체의 안전성(읽기 전용 보장·부하·재시도·승인)은 이 PR이 보장하지 않는다 — 마기가 직접 수행하는 절차의 몫이다.
- 삭제된 수집기와 승인 장치는 git 이력(커밋 73598e4)에 남아 있다. 되살릴 경우 카산드라 C214-79-04(출력 폴더에 의존하는 1회 제한)·C214-79-05(서명이 profile/timeout/DSN을 묶지 않음)를 먼저 풀어야 한다.
