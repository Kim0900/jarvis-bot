# TASK#197 운영 ROLLBACK-ONLY 리허설 시험안 — 정정본 (캐스퍼, 2026-10-17 KST)

대상: 설계 v1.9.1 (PR#70 head `e6ba0f4`, **이 브랜치는 PR#70을 건드리지 않는다**). 요구 출처: 카산드라 #5261, #5487(정적 사전점검 CONDITIONAL_PASS), #5490(P0_PRELIVE_CORRECTIONS_REQUIRED), 마기 #5486(운영 실행 보류).
상태: **제출 전용. 운영에서 실행하지 않았다.** 자체 PASS 선언 없음. 이 문서의 어떤 수치도 운영 측정값이 아니다(로컬 PostgreSQL 17.10 임시 서버 측정). 운영 실행은 마기 허가·카산드라 독립 실행 이후다.

## 0. 이번 정정이 바꾼 것 (#5490 대응)
| 항목 | 변경 |
|---|---|
| P0-FIX-01 | **같은 연결 한계 실행기** `tests/sql/task197_safe_runner.py` 추가. `BEGIN` → `SET LOCAL statement_timeout=3000ms`, `SET LOCAL lock_timeout=250ms`를 **별도 문장**으로 → 같은 연결에서 유효값 확인 → DO 한 문장 → 센티널·P0001 확인 → `ROLLBACK`. 재시도 없음, 자격증명 로그 없음, 해시 고정 |
| P0-FIX-02 | DO 안 `lock_timeout` 1500 ms → **250 ms**. 호출자 값이 0 또는 250 초과면 250으로 조이고, 더 작으면 **느슨하게 만들지 않는다**. 생성기·산출물·expect·시험이 모두 250 기준 |
| P1-FIX-03 | 스냅샷을 불변식(`INV_*`)과 관찰값(`OBS_*`)으로 분리. 전역 건수·pg_stat·WAL·시퀀스는 관찰용이며 판정에 쓰지 않음. 자동 정리 DDL 없음 |
| P1-FIX-04 | 판정 엄격화: `checks=57/57 scenarios=39/39 replay_ok=13 replay_drift=0` + 해시 + 잔여물 0만 PASS. **표류는 수용하지 않는다**(`PASS_WITH_REPLAY_DRIFT` 폐기 → `REPLAY_DRIFT`는 FAIL). 맹목 재시도 금지. 55P03은 새 마기 조용한 시간대 허가가 있어야 재시도 |
| P1-FIX-05 | PostgreSQL **17.10** 재현 시험(아래 5절) + PG17 CI 워크플로우 |
| P1-FIX-06 | 문서 문구: 관찰 전용, #202 전 deny 모드 없음, 허가된 운영 리허설·관찰자 배포·모니터링 게이트 전 태스크 종료·구현 PASS 없음. PR#70 게이트 함수 의미는 변경하지 않음 |

## 1. 산출물
| 파일 | 역할 |
|---|---|
| `tests/sql/task197_prod_rollback_rehearsal.sql` | 하네스 본체(문장 하나짜리 DO, 설계 5절 SQL 바이트 그대로 내장, 끝에 무조건 `RAISE EXCEPTION`) |
| `tests/sql/task197_prod_rollback_rehearsal.expect.json` | 고정 기대값: `sql_sha256`, `gate_sql_md5`, checks 57, scenarios 39, replay_ok 13, replay_drift 0, lock_timeout_ms 250 |
| `tests/sql/task197_rollback_rehearsal_build.py` | 생성기. `--check`는 .sql과 .expect.json이 모두 최신인지 확인 |
| `tests/sql/task197_safe_runner.py` | 한계 실행기(유일하게 허용되는 실행 경로) |
| `tests/sql/task197_prod_rollback_snapshot.sql` | 읽기 전용 스냅샷(`INV_*` 판정, `OBS_*` 관찰) |
| `tests/sql/task197_pgtest_lib.py`, `task197_prod_like_schema.sql` | 시험 공용 모듈, 운영 모양 스키마(임시 서버 전용) |
| `tests/sql/task197_rehearsal_selftest.py` | 하네스 자체 시험 57건 |
| `tests/sql/task197_runner_selftest.py` | 실행기 시험 14건(R1~R13) |
| `tests/sql/task197_matrix.py` | 잠금 100/250/500 ms × 문장 한도 2/3/5 s 비교 측정 |
| `tests/sql/evidence_task197/` | PG17.10 원본 로그·측정 JSON |
| `.github/workflows/task197-rollback-rehearsal.yml` | PG17 CI(서비스 컨테이너, 임시 서버만) |

## 2. 실행기 (`task197_safe_runner.py`)
```
PGDSN='<운영 DSN: 환경변수로만>' python3 tests/sql/task197_safe_runner.py \
  --sha256 <task197_prod_rollback_rehearsal.sql의 sha256> --approval-event <마기 허가 이벤트 id> \
  --statement-timeout-ms 3000 --lock-timeout-ms 250 --report run.json
```
순서: ① 해시 3중 대조(고정 인자·expect 파일·실제 파일) 및 금지 토큰 린트(commit, savepoint, nextval, delete, notify … ) → ② 관찰자 연결(별도 세션)로 기존 `task197_rehearsal` 세션이 있으면 거부 → ③ 사전 스냅샷 → ④ 리허설 연결: `BEGIN`; `SET LOCAL statement_timeout`; `SET LOCAL lock_timeout`; (세션 수준 `idle_in_transaction_session_timeout`) → ⑤ `show`로 **같은 연결의** 유효값이 요청값과 같은지 확인, 아니면 DO 전에 중단 → ⑥ DO 한 문장 실행(클라이언트 워치독 8초: 취소 후 2초 뒤 프로세스 종료) → ⑦ 예외 `P0001` + `REHEARSAL_ROLLBACK_SENTINEL`이어야 정상, 그 외는 FAIL → ⑧ `ROLLBACK` → ⑨ 관찰자가 리허설 세션 소멸·사후 스냅샷·불변식 대조 → 엄격 판정. **재시도는 없다.** DSN·자격증명은 로그·보고서에 쓰지 않는다(환경변수에서만 읽음). `--lock-timeout-ms`가 산출물의 상한(250)보다 크면 거부한다. `--mode session`(자동 커밋)은 비교용이며 운영 지침은 `begin`이다.

## 3. 엄격 판정과 사후 처리 (사전 등록)
PASS는 **전부** 충족될 때만: 센티널 `result=PASS`, `checks=57/57`, `scenarios=39/39`, `replay_ok=13`, `replay_drift=0`, `gate_sql_md5` 일치, `lock_timeout_ms=250`, `statement_timeout_ms=3000`, 리허설 세션 소멸, 사후 `INV_*` 불변·게이트 함수/트리거/픽스처/이벤트/교정 로그 잔여 0. 하나라도 어긋나면 실행기는 종료코드 2.
| 결과 | 조치 |
|---|---|
| `REPLAY_DRIFT` | 수용하지 않음. 원인 분석(해당 태스크의 이후 이벤트) 보고, 재시도 안 함 |
| `FATAL=55P03` | 한 번 포기한 것. 같은 허가로 재시도 금지 — **새 마기 조용한 시간대 허가** 필요 |
| `FAIL`, `REHEARSAL_ABORTED_BEFORE_DDL` | 보고만. 재시도 안 함 |
| 세션 잔존·잔여물 감지 | 자동 정리 DDL 없음. 마기에 보고 후 판단 |
스냅샷 의미: 불변식은 리허설 전용 임시 스키마·게이트 객체·픽스처·교정 로그만 본다. 무관한 동시 쓰기와 시퀀스 결번은 `OBS_*`로만 기록하고 판정에서 제외한다. 전역 행 수·pg_stat·WAL은 관찰값이다.

## 4. 측정 (PostgreSQL 17.10 임시 서버, 소규모 DB, 2 vCPU 컨테이너; 운영은 17.6·실제 규모·디스크 지연이 다르다)
원본: `tests/sql/evidence_task197/*.pg17.10.*`. 각 행은 실행기 경유(변형본은 해시를 따로 고정). 시간은 DO 구간(`do_ms`, 실행기 인식) 기준, 반복 20회(바쁜 테이블은 5회).

**4-1 잠금 상한 100/250/500 ms (DO 상한을 바꾼 변형 산출물)**
| 상한 | 유휴 테이블 20회 | 바쁜 테이블(보유자가 쓰기급 잠금을 *보유자 시작 기준* N ms 유지; 실행기 기동 약 0.2 s가 앞섬) | 3클라이언트 동시 |
|---|---|---|---|
| 100 | PASS 20/20, p95 112, max 112 ms | 60·200: PASS / 400·800: 55P03 (≈115 ms에 포기) | 15회 중 PASS 9·동시 세션 거부 6 |
| **250** | PASS 20/20, p95 90, max 90 ms | 60·200·400: PASS(400은 ≈262 ms 대기) / 800: 55P03(≈265 ms) | PASS 10·거부 5 |
| 500 | PASS 20/20, p95 109, max 109 ms | 60·200·400: PASS / 800: 55P03(≈515 ms) | PASS 9·거부 6 |
- 모든 55P03은 깔끔한 종료(센티널 `FATAL=55P03`, 종료코드 2, 잔여 0). 동시 실행은 **실행기가 거부**(이미 `task197_rehearsal` 세션 있음) — 의도된 동작, 경합 실패가 아님. 거부 전에 DDL·쓰기 없음.
- 선택: **250 ms**. 읽기 지연 최악이 deadlock_timeout(1 s)보다 훨씬 작고, 측정된 짧은 쓰기 보유(≤ 약 0.2 s)는 견딘다. 100 ms는 전형적인 짧은 보유에도 너무 일찍 포기하고, 500 ms는 얻는 것이 400~800 ms 구간뿐인데 대기열에 선 읽기 지연이 두 배가 된다.

**4-2 문장 한도 2/3/5 s**
| 한도 | 정상 20회 p95/max (ms) | 쓰기+체크포인트 부하(3 스레드) 20회 p95/max | 7초 정지 주입 5회: 끝난 시각 / 바깥에서 본 ACCESS EXCLUSIVE 보유 |
|---|---|---|---|
| 2 s | 103/103 | 196/196 | 2001 ms / 약 1970 ms |
| **3 s** | 132/132 | 206/206 | 3001 ms / 약 2975 ms |
| 5 s | 103/103 | 144/144 | 5001 ms / 약 4967 ms |
- 모두 정지 주입은 `57014` 취소 후 롤백, 잔여 0, 종료코드 2.
- 선택: **3 s**. 정상은 최대 약 0.2 s(15배 여유), 장애 시 잠금 보유 상한이 약 3 s. 2 s도 정상 구간엔 충분하나 운영은 DB 크기·디스크 지연이 미측정이라 여유를 택함. 5 s는 장애 시 쓰기 차단이 5 s까지 감.
- 느린 I/O: 실제 디스크 지연은 만들지 못했다. 대체로 다른 DB에서 3개 스레드가 쓰기+체크포인트를 반복하는 부하로 측정(위 표). 이는 대용량 운영 I/O의 대체물이 아니다.
- 연결 상실 거동(실행기 시험 R8~R13): 문장 한도 1 s는 바깥에서 잠금 약 0.96 s 후 해제. 클라이언트 SIGKILL(한도 3 s): 약 2.96 s에 해제, 세션 소멸. 클라이언트 SIGSTOP 블랙홀(한도 2 s): 약 1.97 s에 해제(**문장 오류가 잠금을 풀어준다 — 명시적 트랜잭션이어도 abort 시점에 해제, ROLLBACK을 기다리지 않음**), 남은 유휴 세션은 `idle_in_transaction_session_timeout`이 정리(해제하지 않으면 세션이 남음: R12 대조군에서 확인).
- 로컬 WAL: 1회 약 167~173 KB, 이벤트 시퀀스 소모 44. 운영은 재생 이력이 커서 약 0.5 MB 추정(미측정).

## 5. 재현 시험 (PostgreSQL 17.10, `embedded-postgres` 임시 서버; psql/pgbench 16 클라이언트)
```
PGHOST=<소켓 디렉터리> PGSUPER=supabase_admin
python3 tests/sql/task197_rollback_rehearsal_build.py --check            # exit 0
python3 tests/sql/task197_rehearsal_selftest.py                          # 57/57, 약 87 s
python3 tests/sql/task197_runner_selftest.py --log runner.json           # 14/14
python3 tests/sql/task197_matrix.py --n 20 --out matrix.json             # 약 3 분
```
- 하네스 57건: 정적 6(생성물 최신성·5절 md5·금지 구문·무조건 RAISE·문장 1개), 정상 실행 9(57/57·39/39·13건 재생·스키마 지문·데이터 해시·스냅샷 전후 동일·시퀀스 소모량 일치·재실행), 감싸기 5(BEGIN/COMMIT, `--single-transaction`, `-c`, ON_ERROR_STOP 해제, 뒤따르는 COMMIT), 비정상 종료 6(0 나눗셈·제약 위반·권한 오류·백엔드 종료·클라이언트 SIGKILL·세션 statement_timeout), 보호조건 6, 잠금 3(바쁜 테이블 250 ms 포기·대기열 읽기 지연 0.25 s·3클라이언트 동시 8회), pgrst NOTIFY 미전달 1(통제군 전달), 게이트 SQL 변이 14종 검출, 이력 변경 시 표류 검출, 순라 분류기 1, 잠금·문장 한도 5(호출자 lock_timeout 100→100, 250→250, 5 s→250, 0→250 ms; 호출자 statement_timeout 3000 ms가 DO 안에서 보이고 실행 통과). 역할 전환은 T4(SET ROLE 불가 시 FAIL 보고+롤백)·T5(authenticator가 anon으로 전환 불가 시 DDL 전 중단)로 시험하고, 13건 재생은 T2a/T9에서 확인한다(16개 이력 태스크 중 재생 대상 13건, 나머지 3건은 기대값 없이 정보 줄).
- 실행기 14건: 정상 2모드, 해시 불일치·변조·COMMIT 포함 거부, 상한 완화 거부, 표류 거부, 바쁜 테이블 1회 포기, 한도 취소, 워치독, SIGKILL, SIGSTOP 2모드, 유휴 타임아웃 대조군.
- 모든 시험은 정상/실패 후 잔여 0(스키마 지문·데이터 해시·`INV_*` 대조), pgrst NOTIFY 없음.
- **독립 관측 가능한 CI**: `.github/workflows/task197-rollback-rehearsal.yml`(postgres:17 서비스 컨테이너, 임시 서버). 실행 결과는 PR#77 Actions에서 확인(아래 7절에 실행 URL을 기록).

## 6. 읽기 전용 운영 사전 점검과 위험
- 2026-10-17 11:2x KST 읽기 전용 점검(쓰기·DDL 없음): 서버 17.6, 실행 역할 postgres(비 superuser, bypassrls), 테이블 소유자·RLS 일치, 게이트 객체 없음, 고정 id 범위 비어 있음, 10초 넘는 트랜잭션 없음, 기존 magi_tasks 트리거 2종, 재생 원천 16건 805 이벤트 확인. **하네스 본체는 운영에서 실행하지 않았다.**
- 이번 정정본에서 운영에 닿은 것: 없음(운영 접속 자체를 하지 않았다).
- 남는 위험: ① 로컬 17.10 대 운영 17.6, 소규모 DB 대 실제 규모·디스크 → 시간 수치는 운영 보증이 아님 ② 클라이언트 워치독의 `os._exit` 경로는 단위로 시험하지 않았다(취소 경로만 R9에서 시험) ③ 롤백되지 않는 것은 시퀀스 결번(약 44), WAL 약 0.5 MB 추정, pg_stat 카운터, DB 로그 ERROR 1줄 — #214 pg_stat 스냅샷과 겹치지 않게 시각 기록 ④ 짧은 ACCESS EXCLUSIVE(로컬 40~60 ms)가 있다 — 상한 250 ms 대기 + 문장 한도 3 s가 최악 경계 ⑤ 55P03이 나면 같은 허가로 재시도하지 않는다.
- **시험 TEST HOLD 권고:** #214(Supabase Disk IO) 방향 결정 전, 업로드가 없는 시간대 확보 전에는 운영 실행을 보류한다(마기 #5486 조건과 동일). 이 문서는 그 보류를 해제하지 않는다.

## 7. 실행 조건과 범위 문구
**조건:** (1) 마기의 시각 지정 허가(승인 이벤트 id를 `--approval-event`로 기록) — 업로드 없는 시간대, #214 방향 결정 후, 카산드라 검토 완료. (2) 배포 진행 없음. (3) 실행 역할 `postgres`. (4) 실행기만 사용; 하네스 .sql을 다른 경로로 실행하지 않는다. (5) 전·후 스냅샷은 실행기가 같은 관찰자 세션으로 수행.
**범위:** 이 리허설과 PR#70 설계는 **관찰 전용(observation)** 이다. #202 전까지 deny 모드는 없다. 허가된 운영 리허설·관찰자 배포·모니터링 게이트가 끝나기 전에는 Task#197 종료도 구현 PASS도 없다. PR#70 게이트 함수의 의미는 이 브랜치가 바꾸지 않는다(설계 5절 SQL은 md5로 고정).
**증명하지 못하는 것:** 운영 동시 트래픽 아래 관찰 배포 후 동작과 첫날 위반 건수 대조, anon 역할의 `statement_timeout` 3초(SET ROLE은 역할별 설정을 적용하지 않음 → `max_gate_update_ms`로 대체 보고), READ COMMITTED에서 커밋 전 이벤트를 게이트가 못 보는 한계, 원장 쓰기 위조(설계 8절 위험 수용), 17.6 실서버 동작.

**CI 실행 기록:** PR#77 Actions run 39003813588 (postgres:17 서비스 컨테이너, `bash -eo pipefail`): 서버 버전 확인·생성기 `--check`·하네스 57건·실행기 14건·측정 매트릭스(n=10) 전부 success, 증거 아티팩트 `task197-pg17-evidence`. 앞선 run 39002747165는 `| tee`가 실패를 가린 설정이라 증거로 쓰지 않는다(그 설정을 pipefail로 고치자 3.12에서 시험 코드의 stdin 처리 결함 2건 — `communicate()` 전에 stdin 닫기, 입력 파이프 — 과 TCP용 역할 비밀번호 누락이 드러나 수정; 하네스·게이트·실행기 로직 변경 아님).
