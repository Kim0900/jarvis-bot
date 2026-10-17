# TASK#197 운영 ROLLBACK-ONLY 리허설 시험안 (캐스퍼, 2026-10-17 KST)

대상: 설계 v1.9.1 (PR#70 head `e6ba0f4`, **이 문서와 파일은 PR#70을 건드리지 않는 별도 브랜치**). 요구 출처: 카산드라 원장 #5261("실행 가능한 시험된 산출물로서의 운영 롤백 전용 하네스 미제공"), #5060, 마기 #5034.
상태: **제출 전용. 운영에서 실행하지 않았다.** 자체 PASS 선언 없음. 운영 실행은 카산드라 독립 실행(마기 허가·업로드 없는 시간대 조건 아래)이다.

## 1. 산출물
| 파일 | 역할 |
|---|---|
| `tests/sql/task197_prod_rollback_rehearsal.sql` | 하네스 본체. **문장 하나짜리 DO 블록.** 설계 5절 SQL을 바이트 그대로 내장(생성기가 md5 검증), 마지막에 무조건 `RAISE EXCEPTION`으로 전체를 롤백하고 결과를 예외 문구로 돌려준다 |
| `tests/sql/task197_rollback_rehearsal_build.py` | 하네스 생성기(시나리오 39개·13건 재생 기대값 포함). `--check`로 산출물이 최신인지 확인 |
| `tests/sql/task197_prod_rollback_snapshot.sql` | **읽기 전용** 상태 스냅샷. 실행 전·후 두 번 돌려 잔여물 0을 대조 |
| `tests/sql/task197_prod_like_schema.sql` + `task197_rehearsal_selftest.py` | 운영 실측 모양의 임시 로컬 DB와 하네스 자체 시험 52건 |

## 2. 하네스가 운영에 흔적을 남기지 않는 이유와, 남기는 것
**원자성:** 문장이 하나(DO)이고 끝에 무조건 예외가 있다. COMMIT이 없어서 클라이언트가 자동 커밋이든, 앞뒤를 BEGIN/COMMIT으로 감싸든, 중간에 연결이 끊기든 서버는 롤백한다(자체 시험 T3·T4). 생성 파일 본문에 commit·savepoint·nextval·setval·truncate·delete·notify·grant·copy·vacuum·concurrently 등 비트랜잭션 구문이 없음을 정적 검사한다(T1c·T1d, 내장 5절 SQL 포함).
**롤백되지 않는 것(전부 공개):**
| 항목 | 크기 | 통제 |
|---|---|---|
| `magi_task_events.event_id` 시퀀스 소모 | 약 44(위반 이벤트 insert 횟수만큼) | 번호 결번일 뿐. 사전·사후 스냅샷의 `event_seq_last`로 정확히 확인. 태스크 시퀀스는 고정 id를 쓰므로 0 |
| WAL·통계 카운터 | 로컬 1회 약 170 KB. 운영은 13+3건 실제 이력 복제 때문에 약 0.5 MB 추정. `pg_stat_user_tables`의 insert/dead tuple 카운터가 올라감 | **#214 pg_stat 스냅샷 구간과 겹치지 않게 시각을 기록**(마기·캐스퍼 측정 해석 오염 방지) |
| DB 로그의 ERROR 1줄 | `REHEARSAL_ROLLBACK_SENTINEL ...` | 설계상 의도. 로그 알림에 걸릴 수 있음을 사전 통지 |
**잠금:** 설계 5절의 `drop trigger if exists`가 `magi_tasks`에 ACCESS EXCLUSIVE를 요구하므로 하네스가 시작 직후 한 번에 잡는다. 로컬 실측 보유 시간 50~80 ms. 안전장치: 잠금 요청 전에 10초 넘게 열린 다른 트랜잭션이 있으면 중단, `lock_timeout` 1.5초(바쁘면 1.5초 뒤 포기, 대기열에 선 읽기도 최대 그만큼만 지연: T6a·T6b), 이미 가진 약한 잠금을 승격하는 순서를 제거(승격 대기열이 동시 쓰기를 deadlock_timeout(1초)만큼 멈추게 하던 현상을 찾아 고쳤다. 고친 뒤 3클라이언트 동시 부하에서 8회 최악 지연 86 ms: T6c). `raw_calls`·업로드 경로는 건드리지 않는다. `statement_timeout`은 실행 중인 문장 안에서 조일 수 없으므로 하네스 자체 상한은 없고 세션의 값(운영 postgres 120 s 실측)에 의존한다. 정상 실행은 1초 미만.
**알림·전파:** 운영 이벤트 트리거 6종 실측. `pgrst_ddl_watch`는 CREATE FUNCTION/TRIGGER에서 `NOTIFY pgrst, 'reload schema'`를 보내지만 NOTIFY는 커밋 때만 전달되어 롤백 시 폐기된다(로컬에서 같은 구조로 시험, 통제군은 커밋된 DDL이 실제 알림을 보냄: T7). 나머지 `grant_pg_*`는 해당 확장 생성에만 동작해 무동작, `set_graphql_placeholder`는 graphql_public 삭제에만 동작. `supabase_realtime` 발행에 테이블 없음, 확장은 pg_net 미설치. 삭제 구문이 없으므로 `trg_magi_tasks_delete_log`(correction_log 기록)는 발동하지 않는다.

**운영 읽기 전용 사전 점검(2026-10-17 11:2x KST, 쓰기·DDL 없음):** 하네스 보호조건 블록을 그대로 옮긴 읽기 전용 DO를 운영에서 실행해 전부 통과를 확인했다(서버 17.6, 실행 역할 postgres, 두 테이블 소유자·RLS 일치, 게이트 객체 없음, 고정 id 범위 비어 있음, 10초 넘는 트랜잭션 없음, 기존 magi_tasks 트리거 2종, 한글 문자열 정상, 13+3건 재생 원천 이벤트 수 확인: 160=531, 164=87 등). 결과 반환 경로도 확인했다(`RAISE EXCEPTION`의 메시지·DETAIL이 그대로 돌아옴). **하네스 본체(DDL 포함)는 운영에서 실행하지 않았다.**

## 3. 실행 조건(마기 허가 필요)과 절차
**조건:** (1) 마기 허가와 시각 지정 — "업로드 중 쓰기 금지"(마기 #5356) 때문에 업로드 단계가 없는 시간대. 롤백되는 쓰기라도 WAL과 잠금이 있다. (2) 배포 진행 없음. (3) 실행 역할 `postgres`(운영 SQL 경로에서 current_user=postgres, anon 멤버, anon TEMP 권한, 서버 17.6 확인 완료). (4) 요청 하나에 하네스만 넣는다. 감싸거나 다른 문장을 붙이지 않는다.
1. `task197_prod_rollback_snapshot.sql` 실행, 출력 보관(S0).
2. `task197_prod_rollback_rehearsal.sql` 전체를 한 요청으로 실행. **정상 결과는 에러**다. 메시지가 `REHEARSAL_ROLLBACK_SENTINEL result=...`로 시작하고, DETAIL에 실패·표류·정보 줄과 항목별 ok 목록이 있다.
3. 같은 스냅샷 SQL을 다시 실행(S1). S0과 S1은 `event_seq_last`를 제외하고 같아야 하고, 그 차이는 메시지의 `event_seq_consumed`와 같아야 한다. `fixture_*`, `gate_*`, `temp_relations`는 0/동일.
4. 결과 이벤트(EVIDENCE)를 원장에 기록. 합격 판정은 카산드라가 한다.
**결과 해석(사전 등록):**
| 결과 | 의미·조치 |
|---|---|
| `result=PASS checks=57/57 scenarios=39/39 replay_ok=13 replay_drift=0` | 기대값. `hold_ms`는 로컬 50~80 ms, 1000 ms를 넘으면 보고 |
| `PASS_WITH_REPLAY_DRIFT` | 13건 중 일부가 5절 백테스트와 다름. DETAIL의 `REPLAY#id` 줄과 그 태스크의 이후 이벤트로 설명이 되어야 한다. 하네스 로직 결함은 아님(논리 검사는 별도 집계) |
| `result=FAIL` | `checks` 중 실패 항목 ID가 DETAIL에 있다. **재시도하지 말고** 보고(설계 또는 운영 환경 차이 가능성) |
| `FATAL=55P03`(잠금 시간 초과) | 바쁜 시간대. 한 번만 다른 시각에 재시도 |
| `REHEARSAL_ABORTED_BEFORE_DDL` | 보호조건 위반(이미 설치됨, 고정 id 점유, 긴 트랜잭션, 읽기 전용, 역할 불일치). DDL 전에 멈췄음 |
| 메시지 없이 연결 끊김 | 서버가 롤백한다. 스냅샷으로 잔여물 0 확인 |
**사전 등록 예상(사후 합리화 방지):** 13건 모두 일치 예상. 태스크 160은 5절 백테스트 이후 이벤트가 107건 늘었지만(PATROL_ALERT 다수, `신규 실행 책임 에이전트`의 EVIDENCE 8건, CASSANDRA_OPERATIONAL_GUIDANCE 1건) 판정 이벤트가 아니므로 `REVIEW ["LATEST_VERDICT_UNSTRUCTURED"]` 유지가 예상된다. 194·204·205는 기대값 없이 정보 줄로만 출력한다.
**비상(발생하면 안 되는 상황):** 스냅샷에서 `gate_functions`/`gate_triggers`가 0이 아니거나 `fixture_*`가 0이 아니면 무엇인가 커밋된 것이다. 설계 5절의 롤백 문(`drop trigger` 둘, `drop function`)을 실행하고 즉시 마기에 보고한다. 고정 id 행 삭제는 `trg_magi_tasks_delete_log`가 correction_log에 기록하므로 마기 판단 후에만 한다.

## 4. 이 시험이 증명하는 것과 증명하지 못하는 것
증명: 운영 PostgreSQL 17.6의 실제 소유자·RLS·기존 트리거(domain 필수, domain 상속, delete log)·anon 권한 위에서 5절 DDL이 적용되고 재실행되며, 시나리오 39(양성 26·음성 7·INSERT 4·anon 2) + 상태 재발동·anon 직접 호출·읽기·임시 테이블 shadowing·fail-open·13건 실제 이력 재생이 기대대로 동작하는지.
**증명하지 못함:** (a) 운영의 동시 트래픽 아래 관찰 배포 후 동작과 첫날 위반 건수 대조(2.5절 후속 단계), (b) anon 역할의 `statement_timeout`(3초): `SET ROLE`은 역할별 설정을 적용하지 않으므로 대신 게이트 발동 UPDATE의 최대 소요 `max_gate_update_ms`를 보고한다, (c) 커밋 전 이벤트를 게이트가 못 보는 READ COMMITTED 한계, (d) 원장 쓰기 위조(8절 위험 수용 사항), (e) 로컬 자체 시험은 PostgreSQL 16.15이고 운영은 17.6이다. 17에서의 첫 증거는 이 운영 리허설 자체이며, 그래서 결과 해석표가 FAIL 재시도를 막는다.

## 5. 로컬 자체 시험 요약 (52/52, `PGHOST=<소켓 디렉터리> PGSUPER=supabase_admin python3 tests/sql/task197_rehearsal_selftest.py`)
- T1 정적 검사 6: 산출물 최신성, 5절 md5, 금지 구문 없음, 마지막이 무조건 RAISE, 문장 1개.
- T2 정상 실행 9: 57/57, 시나리오 39/39, 13건 재생 일치, 스키마 덤프·테이블 내용·스냅샷 전후 동일, 시퀀스 소모량 보고값과 일치, 재실행 동일.
- T3 5: BEGIN/COMMIT 감쌈, `--single-transaction`, `-c`, ON_ERROR_STOP 해제, 뒤따르는 COMMIT에도 잔여물 0.
- T4 6: 중간 0 나눗셈, 제약 위반, 권한 오류(모두 FAIL로 보고하고 롤백), 백엔드 종료, 클라이언트 SIGKILL, 세션 statement_timeout 취소. 모두 잔여물·잠금 0.
- T5 6: 이미 설치됨·고정 id 점유(태스크/이벤트)·읽기 전용·SET ROLE 불가·긴 트랜잭션 보호조건.
- T6 3: 바쁜 테이블에서 1.5초 포기, 대기열 읽기 지연 1.14초, 3클라이언트 동시 부하 8회 오류 0·최악 86 ms.
- T7 1: 롤백된 DDL의 pgrst NOTIFY 미전달(통제군은 전달됨).
- T8 14: 게이트 SQL 변이 14종 전부 검출(search_path/pg_temp, INSERT 트리거, 허용 목록→부분 문자열, 철회 게이트, 증거 행위자 제외, JSON 객체 조건, 출력 정제, fail-open 핸들러, OLD.status 조건, vr 해제, EXECUTE 회수, 후속 이벤트 규칙, 최신 증거 규칙, 중립 목록).
- T9 1: 실제 이력이 바뀌면 `PASS_WITH_REPLAY_DRIFT`로 드러남. T10 1: 순라 `_classify_control_event`와 과소게시 판정식에 실제로 만들어진 위반 이벤트 수십 건을 넣어 전부 `non_control_event`, 중요도 표식 0.
**만드는 과정에서 찾아 고친 결함(기록):** ① 하네스 안에서 `statement_timeout`을 조인다는 설명은 틀렸다(실행 중 문장에는 적용 안 됨) → 주장 삭제하고 세션 값 의존으로 정정 ② 잠금 승격 순서가 동시 쓰기를 1초 멈추게 함(변이 없는 정상 코드의 문제) → 잠금을 먼저 잡도록 순서 변경 ③ 변이 시험이 "증거 행위자 제외" 규칙을 시나리오가 못 잡음을 드러냄 → 시나리오 P26 추가 ④ 인자 없는 psql은 SQL 오류에도 종료코드 0이므로 판정은 종료코드가 아니라 메시지로 한다.
