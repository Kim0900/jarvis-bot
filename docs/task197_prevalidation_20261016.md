# Task #197 — 독립검증 전 사전실사 및 인수인계 (2026-10-16 KST)

## 범위·지위
- 대표님 승인 범위: 독립검증 전 준비 작업. **운영 설치·배포·원장 상태 변경 없음**.
- 공식 담당: CASPER. 기존 설계는 PR #70 (draft, head `e6ba0f4dee8406fab81a68c9ee68bca54650df99`), v1.9.1.
- CASSANDRA 설계 PASS = 원장 #5060. 원장 #5261: 운영 롤백 리허설 미실행. 독립 운영 검증과 최종 승인 아님.
- 별도 브랜치에만 `tests/sql/task197_prevalidation_readonly.sql` 추가. 읽기 전용 스냅샷으로 실행 확인.

## 운영 직접 조회 결과 (2026-10-16)
| 항목 | 확인 결과 |
|---|---|
| PostgreSQL | **17.6** (기존 로컬 시험은 PG16) |
| `fn_completion_gate_observe()` | 없음 |
| `magi_tasks` 완료 게이트 트리거 | 없음 |
| `idx_magi_task_events_task` | 있음 |
| 완료/종결 + verification_required=true | **16건** (과거 설계 13건보다 +3) |
| CASSANDRA 이벤트 0건 | #25, #145, #147, **#205** (4건) |
| anon 사건 INSERT 및 태스크 UPDATE | 둘 다 허용; #202 보안 선결 |
| RLS | 두 원장 테이블 ENABLED, postgres BYPASSRLS |

#205는 MAGI 원장 #5109의 LOW 읽기 전용 감사 승인 건이다. **CASSANDRA 이벤트 부재 자체를 잘못된 완료 판정으로 해석하면 안 된다.** #200 등급 정책 구현 전에는 v1.9.1의 HIGH 기본값 때문에 관찰 경보 후보가 된다. 이번에는 관찰 외 자동 차단을 하지 않는다.

## 실행한 사전검사
- 별도 SQL 파일에 `BEGIN; SET TRANSACTION READ ONLY; SET LOCAL lock_timeout='1s'; SET LOCAL statement_timeout='8s'; ... ROLLBACK;`를 사용.
- GitHub 저장 후 다시 읽어 확인하고 Supabase에서 SQL을 직접 실행, 오류 없이 종료. 연결 도구가 마지막 SELECT만 반환하므로 각 핵심 A/C 항목은 단일 SELECT로 재실행: PG17.6 / 게이트 미설치 / 16건 / 미기록 4건을 독립 확인.
- 실제 PostgreSQL 게이트 함수·트리거 DDL 실행, 이벤트 INSERT, 현행 상태 전이, 운영 배포는 하지 않았다.

## 검증자에게 넘길 작업
1. PR #70 v1.9.1의 PG17.6 호환성을 검토. PG16 로컬 32,606 매트릭스만으로 운영 PASS 주장 금지.
2. 운영 ROLLBACK-ONLY 리허설은 MAGI와 잠금 시간/서비스 영향 사전 점검. **ROLLBACK이더라도 DDL 잠금으로 다른 트랜잭션이 일시 대기할 수 있다.**
3. 단일 트랜잭션 운영 시험: N3~N7, I1~I4, S1(pg_temp shadow), A1~A2(anon/RLS), R1~R5 및 최신 16건 역사 재현. 종료 후 함수·트리거 없음과 원장 변경 0 확인.
4. 기존 `tests/sql/task197_gate_real_replay.py`는 `DROP DATABASE`/`CREATE DATABASE`/`COMMIT` 포함. **운영 DB 실행 엄금**. 별도의 운영-안전 롤백 리허설 수단이 필요하다.
5. #194/#204/#205 신규 종료 이력 및 #205 LOW 예외를 상세 검증하고, #200 등급 정책 연동 전 관찰 경보 해석을 정한다.
6. CASSANDRA 독립 운영 리허설/검증 이전엔 PR 병합·관찰 배포 금지, #202 권한 통제 전 거부 모드 금지.

## 상태
**사전 실사 및 읽기 전용 시험 자료 작성 완료. 운영 롤백 DDL 리허설과 CASSANDRA 독립 검증 미실시.** 공식 Registry 상태/태스크 소유자/배포는 미변경.
