# TASK#197 완료 게이트(관찰 모드) 설계 v1.8 (캐스퍼, 2026-10-12 KST)

상태: 설계 단계(v1.8). DB·코드·배포 변경 없음. 운영 적용은 카산드라 설계 재검증 PASS 이후.
범위(마기 지시): verification_required=true 태스크가 증거 이벤트·카산드라 PASS 없이 COMPLETED/CLOSED로 전이되면 위반 이벤트를 기록한다. 1차는 관찰(경고만), 거부 전환은 2주 관찰 후 별도 승인.

## 0. 변경 이력
### v1.7 → v1.8 (카산드라 5028 FAIL: F1 HIGH, F2 MEDIUM)
| 지적 | 확인 | 조치 |
|---|---|---|
| F1 SECURITY DEFINER 함수의 `search_path = public`만 지정, 테이블 비한정 → pg_temp가 먼저 검색될 수 있음(운영 anon은 TEMP 권한 true) | **로컬에서 재현.** anon이 같은 이름의 임시 테이블을 만들고 가짜 PASS+증거를 넣은 뒤 완료하면, 옛 함수는 임시 테이블을 읽어 위반이 없다고 판단하고 기록도 임시 테이블로 보내 **공식 events에 아무 흔적이 남지 않음**(시험 S1: 임시/공식 = 0/0). 가짜 이벤트를 공식 테이블에 남기지 않고도 감사 기록을 지울 수 있다는 점에서 anon이 이미 events에 쓸 수 있는 것보다 한 단계 나쁨 | `set search_path = pg_catalog, public, pg_temp`(pg_temp를 명시적으로 마지막에) + 함수 안 모든 테이블을 `public.`으로 한정. 회귀 시험 S1 추가(수정 후 0/1). 참고: 변이 시험에서 pg_temp를 마지막에 두기만 해도 S1이 통과했으며, 테이블 한정은 이중 방어 |
| F2 INSERT 경로 맹점: `verification_required=true`인 태스크를 COMPLETED/CLOSED로 바로 생성하면 UPDATE 트리거가 발동하지 않음(운영 status CHECK는 두 상태 허용) | **로컬에서 재현**(시험 I1·I2 실패: 이벤트 0건) | 같은 함수를 쓰는 `AFTER INSERT` 트리거 추가(`NEW.vr is true and NEW.status in 종료 상태`). 함수는 `TG_OP`로 분기해 INSERT에서 존재하지 않는 `OLD`를 참조하지 않음(참조하면 예외가 fail-open에 삼켜져 조용히 무동작이 됨). payload에 `op`(UPDATE/INSERT) 기록. 시험 I1~I4와 INSERT 조합표 36건 추가. 정책으로 금지하는 대안은 MAGI 승인이 필요해 채택하지 않음 |
시험(로컬 임시 PostgreSQL 16, 참고치): **32,600 조합 불일치 0**(증거 6600 / 판정열 25,380 / secondary 100 / 전이 432 / vr 동시 변경 27 / INSERT 36 / 손기대값 25), 시나리오 **29/29**, 운영 실제 이력 13태스크 재생 HARD 3 / REVIEW 10 유지. 변이: 옛 search_path 복원 시 S1 실패, INSERT 트리거 제거 시 I1·I2 + 조합표 26건 실패. 운영 PostgreSQL 독립 재현은 카산드라 몫.
**적용 시 확인(카산드라 PASS 이후 롤백 드라이런):** 함수 소유자가 RLS를 우회하는 postgres인지, 임시 테이블 shadowing 회귀 S1을 운영 DB의 롤백 트랜잭션에서 재현.

### v1.6 → v1.7 (제출 후 캐스퍼가 카산드라 입장으로 다시 공격해 찾은 결함, 지적 전에 자진 수정)
v1.6 제출(5025) 직후 검증자 관점으로 공격 점검을 새로 수행해 로컬에서 재현한 구멍 4건. 5025는 이 개정으로 대체된다.
| 재현한 구멍 | 조치 |
|---|---|
| H1·H2·H4: 목록에 없는 표현의 철회(`CASSANDRA_RESCINDED_PASS`, `CASSANDRA_PASS_ANNULLED`, `CASSANDRA_HOLD_NOTICE`, payload 없음)가 판정 이벤트로 안 잡혀 과거 PASS 유지 | 어휘 목록은 완전할 수 없으므로 반대로 접근: PASS_FINAL 뒤에 **중립 목록(AGREEMENT_RESPONSE, CASSANDRA_BACKLOG_REVIEW, CASSANDRA_OPERATIONAL_GUIDANCE, CASSANDRA_REGRESSION_STARTED, 정확 일치) 밖의 카산드라 이벤트가 하나라도 있으면** REVIEW `LATER_CASSANDRA_EVENT_UNCLASSIFIED` |
| H3: 재오픈·변경 후 새 증거가 나왔는데 새 검증 없이 옛 PASS로 완료 | PASS_FINAL 뒤에 독립 증거(C1) 이벤트가 있으면 REVIEW `EVIDENCE_NEWER_THAN_PASS`. 새 PASS가 나오면 해소 |
| 같은 UPDATE에서 `verification_required`를 끄며 완료하면 트리거 조건(NEW.vr is true)이 걸리지 않음 | 트리거 조건을 `NEW.vr is true OR OLD.vr is true`로 확장, 끈 경우 REVIEW `VERIFICATION_REQUIRED_CLEARED` |
시험(로컬 임시 PostgreSQL 16, 참고치): **32,564 조합 불일치 0** (증거 6600 / 판정열 25,380 / secondary 100 / 전이 432 / vr 동시 변경 27 / 손기대값 25), 시나리오 24/24, 운영 실제 이력 13태스크 재생 HARD 3 / REVIEW 10 유지. 변이 4종(후속 이벤트 규칙, 증거 최신성 규칙, vr 해제 표시, 트리거 OLD 조건 제거)은 338 / 186 / 4 / 4건 불일치로 검출됨. 운영 PostgreSQL 독립 재현은 카산드라 몫.
**설계상 미해결 한계(관찰 모드이므로 허용, 관찰 2주 보고서에 포함):** PASS가 가리키는 대상(payload.target의 PR head SHA 등)과 실제 완료 대상의 일치는 검사하지 않는다. `verification_required`를 완료 이전의 별도 UPDATE로 끈 경우는 이 트리거로 잡지 못한다(별도 감시 필요). anon 쓰기 정책 때문에 actor 위조는 막지 못한다.

### v1.5 → v1.6 (카산드라 5024 FAIL: event_type 부분 문자열 일치)
| 지적 | v1.5 결함 | v1.6 조치 |
|---|---|---|
| 철회·무효 이벤트가 최종 PASS로 통과 | `event_type ~* 'VALIDATION'`이 `CASSANDRA_INVALIDATION`, `CASSANDRA_VALIDATION_REVOKED`에도 걸려 J 선택과 PASS_FINAL 조건을 통과 → 나중의 철회 이벤트에 남은 PASS/FINAL과 독립 증거가 있으면 위반 0건 | (1) PASS_FINAL은 **정확 일치 허용 목록**의 event_type에서만 인정: CASSANDRA_VALIDATION·REVALIDATION·IMPLEMENTATION_(RE)VALIDATION·FINAL_(RE)VALIDATION. 그 밖은 REVIEW `PASS_NOT_FROM_VALIDATION_EVENT`. (2) 철회 계열 단어(REVOKED, INVALIDATION, WITHDRAWN, SUPERSEDED, RETRACTED, CANCELLED, VOID, ROLLBACK, REVERTED, REJECTED 등을 **밑줄 경계 단어**로 일치)가 event_type에 있는 이벤트는 J 후보에 포함하고, 최신이면 payload와 무관하게 HARD `LATEST_JUDGMENT_REVOKED`. 이후 새 VALIDATION PASS/FINAL이 나오면 최신 우선으로 다시 통과 |
| 같은 종류의 결함(제출 전 전수 점검으로 추가 발견) | 증거(C1) 판정도 `~* '(EVIDENCE\|IMPLEMENTATION\|CI_)'` 부분 문자열이라 `EVIDENCE_REVOKED`, `NO_EVIDENCE`, `IMPLEMENTATION_REJECTED`, `YOUNGSIL_GAS_IMPLEMENTATION_SPEC`(명세), `EVIDENCE_ONLY_BRIEFING_LIVE`가 증거로 인정됨(운영 실제 event_type에서 확인) | 증거는 정확 일치 허용 목록(EVIDENCE, MAGI_EVIDENCE, EVIDENCE_SUBMITTED, IMPLEMENTATION_EVIDENCE, IMPLEMENTATION_CI_PASS, IMPLEMENTATION_PR_DRAFT_CREATED, TEST_EVIDENCE, CI_EVIDENCE, PRODUCTION_BRIDGE_GATE_EVIDENCE, FINAL_PRODUCTION_HTTP_E2E_EVIDENCE) 또는 `payload.evidence_kind` 경로로만 인정하고, 두 경로 모두에 철회·실패·누락 단어(FAILED, MISSING, NOT, NO 등 밑줄 경계 단어) 제외를 적용. 증거 판정은 REVIEW 수준이라 판정 결과를 HARD로 바꾸지 않음 |
| 기대값이 부분 문자열 정책을 반복 | 기대값이 `"VALIDATION" in event_type` | 기대값을 밑줄 단위 단어 집합과 허용 목록으로 다시 작성(정규식 미사용). 카산드라 반례 2건 포함 손기대값 15건 |
**운영 실제 데이터 재생(신규):** 운영의 완료 verification_required 태스크 13건·이벤트 653건(읽기 전용 내보내기: event_type·actor·payload)을 로컬 임시 DB에 그대로 넣고 v1.6 트리거를 실행 → **HARD 3(25·145·147) / REVIEW 10, 예외 0**(3절 표와 일치). `tests/sql/task197_gate_real_replay.py`.
시험 결과(로컬 임시 PostgreSQL 16, 참고치): **43,267 조합 불일치 0** (증거 6600 / 판정열 36,120 / secondary 100 / 전이 432 / 손기대값 15), 시나리오 24/24. 변이 4종(PASS_FINAL 부분 일치, 철회 분기 제거, 증거 제외 제거, 증거 부분 일치)은 각각 2,472 / 5,265 / 469 / 193건 불일치로 검출됨. 운영 PostgreSQL 독립 재현은 카산드라 몫.

### v1.4 → v1.5 (제출 전 자체 점검: 이 작업이 운영에서 만들 수 있는 문제 목록화)
카산드라 지적 없이 캐스퍼가 제출 전에 찾아 고친 것. 운영 DB는 읽기 전용 조회만 했다(2026-10-13 07:4x KST).
| 점검 항목 | 운영 실측 | 조치 |
|---|---|---|
| 로컬 시험 스키마가 운영과 달라 결과가 운영을 대변하지 못함 | event_id=identity ALWAYS, 두 테이블 RLS 켜짐(FORCE 아님, 소유자 postgres), anon 읽기·쓰기 정책(events는 insert, tasks는 update), 도메인 상속 트리거, status/priority CHECK | 로컬 스키마를 동일 조건으로 재작성. 봇이 anon 권한으로 상태를 바꾸는 경로 시험 A1·A2 추가(RLS 아래에서도 위반 이벤트 기록, domain 상속 확인). **적용 시 함수 소유자는 RLS를 우회하는 postgres여야 함**(SECURITY DEFINER 전제, 6.2 롤백 드라이런에서 확인) |
| payload가 객체가 아닌 JSON(배열·문자열) | 운영은 객체 229건·NULL 4,649건뿐이고 verdict 12건 전부 문자열 | `payload ? 'verdict'`는 배열 원소에도 참이 됨 → `jsonb_typeof(payload)='object'` 조건 추가(J 선택과 구조화 판정 2곳). 배열·문자열·null·숫자·리스트 verdict, 앞뒤 공백 값 시험 추가(모두 안전 방향: 비구조 또는 HARD) |
| 변이 확인 | | jsonb_typeof 제거 시 판정열 853건, J 선택의 요청 제외 제거 시 판정열 2,522건 + 손기대값 4건 불일치로 검출 |
시험 결과(로컬 임시 PostgreSQL 16, 운영 조건 재현, 참고치): **22,787 조합 불일치 0** (증거 3600 / 판정열 18,647 / secondary 100 / 전이 432 / 손기대값 8), 시나리오 **24/24**. 운영 PostgreSQL 독립 재현은 카산드라 몫.
**알려진 한계(미수정, 관찰 모드이므로 허용):** (1) anon 키로 events insert가 허용돼 있어 누구나 actor='CASSANDRA'로 기재할 수 있다 → 게이트는 위조 방지가 아님(3절). (2) `REQUIRE` 같은 부분 문자열이 든 정상 판정 event_type(예: …REQUIREMENTS_VALIDATION)은 판정에서 제외돼 REVIEW로 기록된다(안전 방향 오탐). 관찰 기간 오탐 목록으로 확인.

### v1.3 → v1.4 (카산드라 5018 FAIL)
| 지적 | v1.3 결함 | v1.4 조치 |
|---|---|---|
| (1) 요청이 최종 PASS로 취급(#99021, #99022) | J 선택의 `OR payload ? verdict`가 REQUEST/REQUIRE 제외를 우회 → `CASSANDRA_REVALIDATION_REQUESTED`에 verdict=PASS/stage=FINAL이 있으면 통과, 증거가 있으면 위반 0건(미탐) | `event_type !~* '(REQUEST\|REQUIRE)'`를 J 선택의 최상위 AND로 이동(구조화 여부와 무관). 요청은 어떤 payload를 가져도 판정이 아님. 추가로 PASS_FINAL은 event_type에 VALIDATION이 있는 명시 판정 이벤트에서만 인정; 합의·안내·VERIFICATION_COMPLETED 등의 PASS/FINAL은 REVIEW `PASS_NOT_FROM_VALIDATION_EVENT`(부정 판정 HOLD 등은 종전대로 HARD) |
| (2) DESIGN 판별 대소문자 구분(#99110) | `v_jtype ~ 'DESIGN'` → 소문자 event_type이 PASS_FINAL로 오분류 | `~*`로 변경. 소문자·혼합 표기 event_type 시험 추가 |
| (3) 기대값이 SQL과 같은 오류를 복제 | `oracle_judgment`가 typed or structured를 그대로 사용 | 기대값을 정책 문장 기준으로 다시 작성(요청 제외 최우선, 명시 판정 이벤트만 PASS). 기대값과 별개로 카산드라 반례 #99021·#99022·#99023·#99110·#99111 등 8건을 손으로 쓴 기대값(E)으로 고정. 판정 알파벳에 요청·합의·안내·소문자·혼합 표기 16종 추가 |
시험 결과(로컬 임시 PostgreSQL 16, 참고치): 16,161 조합 불일치 0 (증거 3600 / 판정열 12,261 / secondary 100 / 전이 192 / 손기대값 8). **v1.3 SQL을 같은 시험에 넣으면 판정열 2,401건 + 손기대값 5건 불일치로 검출됨.** 운영 PostgreSQL 독립 재현은 카산드라 몫.

### v1.2 → v1.3 (카산드라 5009 FAIL, 마기 5014 지시 반영)
| 지적 | v1.2 결함 | v1.3 조치 |
|---|---|---|
| 증거 중복 인정 우회 | 제외 규칙이 event_type 경로에만 걸려 `payload.evidence_kind`가 있으면 요청·검증 이벤트도 증거로 인정됨 | 제외 규칙(actor·event_type)을 OR 바깥 최상위 AND 한 곳으로 일원화. evidence_kind 경로도 동일 적용, 값 비교는 `upper()` |
| secondary 오분류 | 마기·SECONDARY 이벤트가 존재하기만 하면 SECONDARY_ONLY_NOT_ACCEPTED로 분류(마기 FAIL, 보조검증 요청도 해당) | SECONDARY_ONLY 분류 폐기. 보조검증은 통과 근거가 아니며 판정 reason에 영향 없음. 정보용 checks만 기록: `secondary_pass_recorded`(마기+SECONDARY+요청 아님+PASS+FINAL) / `secondary_events_recorded`(마기+SECONDARY 이벤트 존재) |
| 카산드라 0건 + secondary | v1.2는 secondary가 있으면 HARD를 REVIEW로 낮춤 | 카산드라 이벤트 0건이면 secondary 유무와 무관하게 HARD NO_CASSANDRA_RECORD. 기본 tier=HIGH 유지, HARD를 낮추지 않음 |
| 수작업 반례 한계 | 반례를 사람이 골라 시험 | `tests/sql/task197_gate_matrix.py`: event_type × evidence_kind × actor 3600, 카산드라 이벤트열(길이 1·2 전수 + 길이 3 축약) 6284, secondary × 카산드라 상태 × 증거 100, 상태 전이 192 = **10,176 조합**을 실제 트리거에 실행해 정규식 없이 따로 작성한 기대값(oracle)과 대조. 불일치 0. 변이 시험: v1.2 결함 재현 3종 주입 시 각각 576/18/372건 불일치로 검출됨 |
**한계 명시:** 위 결과는 이 작업 환경의 임시 로컬 PostgreSQL 16 기준 참고치이며, 운영 PostgreSQL에서의 독립 재현은 카산드라의 몫이다.

### v1.1 → v1.2 (카산드라 4997 FAIL, 4996 조건부 동의 반영)
| 카산드라 지적 | v1.1 결함 | v1.2 조치 |
|---|---|---|
| (1) 최신 비구조 판정이 과거 PASS를 덮지 못함 | 판정 추출을 `payload ? verdict`로 먼저 걸러 최신 구조화 이벤트만 선택. PASS/FINAL 뒤에 비구조 HOLD가 와도 과거 PASS를 최종으로 선택(카산드라 실증: 가상 태스크 99001) | 구조화 여부를 보기 **전에** 카산드라 판정 이벤트 전체에서 최신 event_id를 먼저 선택. 선택된 이벤트가 비구조면 LATEST_VERDICT_UNSTRUCTURED(REVIEW)이고 과거 PASS는 무시 |
| (2) 카산드라 판정 이벤트가 구현 증거 C1까지 충족 | C1 정규식이 VALIDATION을 포함해 CASSANDRA_VALIDATION 하나가 C1과 C2를 동시에 만족(가상 태스크 99011) | C1에서 actor=CASSANDRA% 와 검증·판정·요청 계열 event_type 제외. 독립 구현·시험 증거만 인정 |
| (4996 c) tier·보조검증 분기 불명확 | 미기술 | 2.4절에 HIGH/MEDIUM/LOW와 마기 보조검증(SECONDARY) 분기 규칙 명시 |
| 반례 포함 골든 + 13건 백테스트 재실행 | | 6.1절 |
추가로 점검 중 발견해 같이 고친 결함: `payload`가 NULL인 이벤트에서 `payload ? 'verdict'`가 NULL이 되어 분기가 틀어질 수 있음(과거 이벤트 다수가 해당) → 모든 곳에서 `coalesce(payload ? 'verdict', false)`.

### v1 → v1.1 (카산드라 4967 FAIL)
본문 정규식 판정을 폐기하고 구조화 판정값(payload.verdict)으로 전환, 요청 이벤트는 증거에서 제외, 설계 PASS와 최종 PASS 구분, 애매하면 HARD/REVIEW로 fail-safe.

## 1. 실측 (v1에서 유지 + 재확인)
- magi_tasks에는 상태 전이를 검사·기록하는 트리거가 없다(트리거: delete_log, require_domain_on_insert뿐). 완료 전이는 에이전트의 직접 SQL, 봇 `approve_task`(PATCH), 마기(자동) 검증(bot_v5_legacy.py L7187) 등 여러 경로로 일어난다. → 모든 쓰기 경로를 포괄하려면 행 단위 DB 트리거가 유일한 지점이다.
- evidence_registry 테이블은 완료된 verification_required 태스크에서 쓰이지 않는다(0행). 실제 증거 기록은 magi_task_events 이벤트다.
- **신규 실측 (v1.1 설계 근거):** actor=CASSANDRA 이벤트 중 `payload.verdict`가 있는 것은 4965~4970 6건뿐이고 나머지 과거 이벤트는 자유서술이다. 값 어휘: PARTIAL_PASS, HOLD, FAIL_DESIGN, FAIL_PATCH. 과거 본문은 `PASS`로 시작해도 300자 안에 HOLD·FAIL 같은 단어가 흔히 섞여, 정규식으로 PASS를 안전하게 판정하는 방법이 없다(시도한 보수 규칙에서 PASS 시작 본문 다수가 판정불가로 남음). → 본문 해석은 포기하고 구조화값을 기준으로 한다.

## 2. 판정 규칙 v1.8 (관찰 모드)
대상: `(NEW.verification_required IS TRUE OR OLD.verification_required IS TRUE) AND NEW.status IN ('COMPLETED','CLOSED') AND OLD.status NOT IN ('COMPLETED','CLOSED')`. (OLD도 보는 이유: 같은 UPDATE에서 verification_required를 끄며 완료하는 우회를 막기 위함. 그 경우 REVIEW `VERIFICATION_REQUIRED_CLEARED`가 붙는다.)

### 2.1 카산드라 판정 이벤트 선택과 분류
**선택(구조화 여부를 보기 전에):** 같은 태스크에서 `actor ILIKE 'CASSANDRA%'` 이고
`event_type !~* '(REQUEST|REQUIRE)' AND (event_type ~* '(VALIDATION|REVALIDATION|DESIGN_PASS)' OR coalesce(payload ? 'verdict', false))` (요청 제외는 최상위 AND) 인 이벤트 중 **event_id가 가장 큰 1건(J)** 이 현재 판정이다. 새 event_type이라도 payload.verdict가 있으면 판정 이벤트로 본다. 합의 응답·백로그 검토·증거 요청 등 판정이 아닌 카산드라 이벤트는 J 선택에 영향을 주지 않는다.
**분류(J에 대해서만):**
- J가 없음 → NO_JUDGMENT_EVENT. J에 `payload.verdict`가 없음 → LATEST_VERDICT_UNSTRUCTURED(과거에 구조화 PASS가 있어도 인정하지 않음).
- 표준 필드(2.4절): `verdict` ∈ {PASS, PARTIAL_PASS, CONDITIONAL, HOLD, FAIL}, `stage` ∈ {DESIGN, IMPLEMENTATION, FINAL}.
- NEG: verdict(대문자화)가 `^(PARTIAL|HOLD|FAIL|CONDITIONAL|REJECT)`로 시작(과도기 값 FAIL_DESIGN·FAIL_PATCH 포함).
- PASS_FINAL: verdict가 정확히 `PASS`이고 `stage = FINAL`이며 verdict·event_type에 DESIGN이 없고(대소문자 무관) **event_type에 VALIDATION이 있는 명시 판정 이벤트**일 것.
- PASS_NON_VALIDATION: PASS+FINAL이지만 판정 이벤트가 아님(합의·안내 등) → REVIEW `PASS_NOT_FROM_VALIDATION_EVENT`.
- PASS_DESIGN: PASS 형태인데 stage=DESIGN, 또는 verdict·event_type에 DESIGN 포함.
- PASS_PARTIAL_STAGE: PASS인데 stage=IMPLEMENTATION, 또는 stage 누락·미정의 값. 최종으로 인정하지 않는다.
- UNKNOWN: 그 외 모든 값. UNKNOWN은 PASS가 아니다.

### 2.2 증거 이벤트(C1): 독립 구현·시험 증거만
같은 태스크에서 다음을 모두 만족하는 이벤트가 1건 이상:
- (공통 제외, 모든 경로에 적용) actor가 GEOMNURI_PATROL·마기(자동)·COMPLETION_GATE가 아니고 **CASSANDRA%도 아님**, 그리고 `event_type !~* '(REQUEST|REQUIRE|READY|VALIDATION|VERIFICATION|CASSANDRA)'`.
- (증거 형태, 둘 중 하나) `event_type ~* '(EVIDENCE|IMPLEMENTATION|CI_)'` **또는** `upper(payload.evidence_kind) ∈ {IMPLEMENTATION, TEST, CI, PRODUCTION}`. evidence_kind가 있어도 공통 제외를 우회할 수 없다.
- 검증 요청·준비·판정(…_REQUESTED, …_READY, …VALIDATION, VERIFICATION_COMPLETED 등)은 증거가 아니다.
- 한계: 증거의 "내용 충분성"은 판정하지 않는다(존재 여부만). 충분성은 카산드라 판정의 몫. 과거 이벤트 이름에 의존하므로 구형 태스크에서는 증거를 못 알아볼 수 있다(3절 참고).

### 2.3 결과 분류
| 구분 | reason | 조건 |
|---|---|---|
| HARD (gate_level=WARN) | NO_CASSANDRA_RECORD | 같은 태스크에 actor=CASSANDRA% 이벤트가 0건(마기 보조검증 유무와 무관) |
| HARD | LATEST_VERDICT_NOT_PASS | J가 구조화돼 있으나 NEG 또는 UNKNOWN |
| REVIEW (gate_level=REVIEW) | LATEST_VERDICT_UNSTRUCTURED | J에 구조화 판정값이 없음 |
| REVIEW | NO_JUDGMENT_EVENT | 카산드라 이벤트는 있으나 판정 이벤트가 없음 |
| REVIEW | DESIGN_PASS_ONLY | J가 PASS_DESIGN |
| HARD | LATEST_JUDGMENT_REVOKED | J의 event_type이 철회·무효·취소 계열 단어(REVOKED, INVALIDATION, WITHDRAWN, SUPERSEDED 등, 밑줄 경계 단어 일치)이면 payload에 남은 PASS/FINAL과 무관하게 통과 아님 |
| REVIEW | LATER_CASSANDRA_EVENT_UNCLASSIFIED | J가 PASS_FINAL이어도 그 뒤에 중립 목록(AGREEMENT_RESPONSE, BACKLOG_REVIEW, OPERATIONAL_GUIDANCE, REGRESSION_STARTED; 정확 일치) 밖의 카산드라 이벤트가 있음. 어휘 목록이 예측하지 못한 표현의 철회를 잡는 안전망 |
| REVIEW | EVIDENCE_NEWER_THAN_PASS | J가 PASS_FINAL이나 그보다 나중에 독립 증거 이벤트가 있음(재오픈 후 변경 가능성, 새 검증 필요) |
| REVIEW | VERIFICATION_REQUIRED_CLEARED | 같은 UPDATE에서 verification_required를 true→false/NULL로 바꾸며 완료한 경우(우회 차단). 트리거 조건은 OLD 또는 NEW가 true |
| REVIEW | PASS_NOT_FROM_VALIDATION_EVENT | J가 PASS+FINAL이나 event_type이 최종 판정 허용 목록(정확 일치)에 없음 |
| REVIEW | PASS_STAGE_NOT_FINAL | J가 PASS이나 stage가 IMPLEMENTATION이거나 누락 |
| REVIEW | EVIDENCE_NOT_RECOGNIZED | C1 불충족(다른 reason과 함께 기록) |
- J가 PASS_FINAL이고 C1도 충족이면 기록 없음.
- HARD가 하나라도 있으면 gate_level=WARN, REVIEW만 있으면 REVIEW. 증거 없음은 REVIEW 수준이지만 위반 이벤트는 반드시 남으므로 미탐은 아니다.
- v1의 C2'(다른 태스크에 기재된 PASS 추적)는 트리거에서 제거하고 관찰 기간의 배치 보고서로 이동한다.
- 기록: magi_task_events에 event_type=`COMPLETION_GATE_VIOLATION`, actor=`COMPLETION_GATE`, old/new_status NULL, payload에 mode·gate_level·reasons·last_judgment_event_id·last_verdict·last_stage·verdict_class·checks·전이 전후 상태·task_priority·tier(판정 payload의 기록용 복사).
- 항상 fail-open: 트리거 내부 오류는 EXCEPTION으로 삼키고 RAISE WARNING만. 거부 모드는 이 설계 범위 밖.

### 2.4 선결 합의 사항과 tier·보조검증 규칙
마기 4973(조건부 동의), 카산드라 4996(조건부 동의, 설계 PASS 아님)의 응답을 반영한다.
1. 카산드라는 판정 이벤트마다 `payload.verdict`, `payload.stage`를 기록한다(PASS는 조건·보류 없이 통과일 때만). 마기 권장 필드: `payload.tier`(HIGH|MEDIUM|LOW), `payload.target`(PR 번호·head SHA 또는 문서·쿼리 id). 필수는 verdict·stage뿐이다. 기존 `scope` 키(4969·4970)는 건드리지 않는다. 과도기 값 FAIL_DESIGN·FAIL_PATCH·PASS_DESIGN은 게이트가 앞부분 기준으로 읽는다. 최종 PASS는 `verdict=PASS, stage=FINAL`이다.
2. **tier 규칙:** 통과 판정에 쓰는 tier의 출처는 마기가 착수 전에 지정하는 태스크 단위 tier이며(#200에서 구현), 판정자가 자기 이벤트에 적는 tier는 신뢰 근거가 아니다. 따라서 #200이 태스크 단위 tier 기록을 구현하기 전에는 **모든 태스크를 HIGH로 간주**한다.
3. **HIGH:** 통과 근거는 카산드라의 최신 판정 PASS+FINAL뿐이다.
4. **MEDIUM/LOW 보조검증:** 마기 보조검증은 actor=마기, `payload.verifier_role=SECONDARY`, verdict=PASS, stage=FINAL 로 별도 구분하며 마기가 설계·작성한 건은 제외한다(#200 규칙). **이 설계에서는 보조검증을 통과 근거로 인정하지 않으며** 판정 reason에도 영향을 주지 않는다(카산드라 이벤트 0건이면 항상 HARD). payload.checks의 `secondary_pass_recorded`·`secondary_events_recorded`로 정보만 남긴다. 마기 FAIL·보조검증 요청 이벤트는 secondary PASS로 세지 않는다. 인정하려면 #200 구현 후 별도 설계 변경과 카산드라 검증이 필요하다.
5. `payload.tier`와 `payload.target`은 위반 이벤트 payload에 기록용으로 복사할 뿐 판정 로직에는 쓰지 않는다.
6. 어휘를 코드로 강제하는 입력 검사는 이번 범위 밖이다. 관찰 2주 동안 어휘 이탈 건수를 보고서로 세고 필요하면 별도 태스크로 올린다.
7. 합의 전까지 이 게이트가 낼 수 있는 결과는 HARD와 REVIEW 뿐이며 "정상 통과"는 합의 이후에만 가능하다(의도된 fail-safe). 카산드라 4996은 합의 응답이며 설계 PASS가 아니다.

## 3. 백테스트 (read-only, 2026-10-12 17:44 KST 스냅샷 재실행, 완료 vr=true 13건)
| 태스크 | C1(v1.6) | 카산드라 이벤트 | 최신 판정 이벤트 | v1.6 결과 |
|---|---|---|---|---|
| 25 | 불충족 | 0 | 없음 | HARD NO_CASSANDRA_RECORD + EVIDENCE_NOT_RECOGNIZED |
| 145 | 불충족 | 0 | 없음 | HARD NO_CASSANDRA_RECORD + EVIDENCE_NOT_RECOGNIZED |
| 147 | 불충족 | 0 | 없음 | HARD NO_CASSANDRA_RECORD + EVIDENCE_NOT_RECOGNIZED |
| 160, 174 | 충족 | 3, 1 | 3774, 4233 | REVIEW LATEST_VERDICT_UNSTRUCTURED |
| 83, 157, 164, 167, 170, 171, 172, 175 | 불충족(v1.6: 164 추가, 증거 허용 목록 정확 일치) | 4, 1, 10, 2, 2, 3, 1, 1 | 3709, 3725, 4003, 3926, 4011, 4077, 4070, 4182 | REVIEW LATEST_VERDICT_UNSTRUCTURED + EVIDENCE_NOT_RECOGNIZED |
- 합계 HARD 3 / REVIEW 10. 카산드라 4997의 독립 재현(HARD 3/REVIEW 10)과 일치한다.
- 해석: 알려진 위반 3건(25, 145, 147)은 HARD로 잡힌다. 나머지 10건은 최신 카산드라 이벤트가 구조화 전이라 "통과"로 인정되지 않는다. C1을 독립 구현·시험 증거로 좁힌 결과 구형 태스크 10건 중 8건은 증거 이벤트를 알아보지 못한다(v1.6 정확 일치 허용 목록 적용 후). 이는 과거 이벤트 이름 관행 때문이며(검증·요청 이벤트로 증거를 대신하던 관행) 신규 전이에서는 `payload.evidence_kind` 관행이 정착되면 줄어든다. 구형 태스크 판정 자체가 틀렸다는 뜻은 아니다.
- 트리거는 신규 전이에만 발동하므로 이 표는 규칙 점검용이며 과거 태스크에 이벤트를 기록하지 않는다. 소급 처리 여부는 마기·대표님 판단.
- #174 추적성 문제(PASS가 #176 이벤트 안에 기재)는 배치 보고서에서 별도 점검한다.
- 한계: actor 필드는 쓰기 권한자가 임의로 채울 수 있어 이 게이트는 "누락 탐지"이지 "위조 방지"가 아니다.

## 4. 기존 경로·순라 회귀 방지 (v1 유지)
- 순라 `control_classifier_underposting`(bot_v5_legacy.py L6344~)은 payload의 severity/level/priority 또는 detail의 특정 표식 문자열(적색 이모지, 대괄호 크리티컬 표기, P0·P1 뒤 공백 등)이 있는데 `_classify_control_event`가 non_control_event로 분류하면 CRITICAL 경보를 낸다. 2026-10-12 16:08 캐스퍼의 이벤트 4958이 이 표식을 인용해 실제 오탐 경보를 일으켰다(이벤트 4962). → 위반 이벤트의 payload는 severity/level/priority 키를 쓰지 않고(`gate_level` 사용), detail 템플릿에 표식을 넣지 않는다. 태스크 우선순위는 payload.task_priority로만 기록.
- 신규 event_type은 현 분류기에서 `non_control_event`(Slack 게시 후보 아님)로 분류된다. Slack 게시는 별도 코드 변경이라 이번 범위 제외(관찰 기간 중 SQL 집계).
- 이벤트 insert는 magi_tasks를 건드리지 않아 재귀 없음. `fn_inherit_domain_from_task`가 domain을 채운다.
- 쓰기 경로 회귀: (a) 봇 PATCH `approve_task`, (b) 마기(자동) COMPLETED(vr=false → 대상 아님), (c) 에이전트 직접 SQL UPDATE, (d) 비상태 컬럼 UPDATE(트리거 미발동)를 롤백 트랜잭션 안에서 전/후 비교.

## 5. 초안 SQL v1.8 (미적용)
```sql
create or replace function public.fn_completion_gate_observe() returns trigger
language plpgsql security definer set search_path = pg_catalog, public, pg_temp as $$
declare
  v_c1 boolean; v_cass_total int; v_sec_pass boolean; v_sec_other boolean;
  v_old_status text; v_evid_max bigint; v_later boolean; v_jid bigint; v_jtype text; v_structured boolean := false; v_ver text; v_stage text; v_tier text;
  v_found boolean := false; v_class text;
  v_hard text[] := '{}'; v_rev text[] := '{}';
  -- event_type vocabulary is matched by whole underscore-delimited words or exact allowlists, never by bare substrings
  -- of words that also occur inside negations (INVALIDATION contains VALIDATION).
  v_neg  constant text := '(^|_)(REVOKED|REVOKE|REVOCATION|INVALIDATED|INVALIDATION|INVALID|WITHDRAWN|WITHDRAWAL|SUPERSEDED|RETRACTED|RETRACTION|CANCELLED|CANCELED|VOID|VOIDED|ROLLBACK|REVERTED|REJECTED|FAILED|FAILURE|STALE|OBSOLETE|MISSING|NOT|NO)(_|$)';
  v_revoke constant text := '(^|_)(REVOKED|REVOKE|REVOCATION|INVALIDATED|INVALIDATION|INVALID|WITHDRAWN|WITHDRAWAL|SUPERSEDED|RETRACTED|RETRACTION|CANCELLED|CANCELED|VOID|VOIDED|ROLLBACK|REVERTED|REJECTED)(_|$)';
  -- CASSANDRA events that may follow a PASS without questioning it; anything else after a PASS needs a human look
  v_neutral_types constant text[] := array['AGREEMENT_RESPONSE','CASSANDRA_BACKLOG_REVIEW','CASSANDRA_OPERATIONAL_GUIDANCE','CASSANDRA_REGRESSION_STARTED'];
  v_final_types constant text[] := array['CASSANDRA_VALIDATION','CASSANDRA_REVALIDATION','CASSANDRA_IMPLEMENTATION_VALIDATION','CASSANDRA_IMPLEMENTATION_REVALIDATION','CASSANDRA_FINAL_VALIDATION','CASSANDRA_FINAL_REVALIDATION'];
  v_evid_types constant text[] := array['EVIDENCE','MAGI_EVIDENCE','EVIDENCE_SUBMITTED','IMPLEMENTATION_EVIDENCE','IMPLEMENTATION_CI_PASS','IMPLEMENTATION_PR_DRAFT_CREATED','TEST_EVIDENCE','CI_EVIDENCE','PRODUCTION_BRIDGE_GATE_EVIDENCE','FINAL_PRODUCTION_HTTP_E2E_EVIDENCE'];
begin
  begin
    if TG_OP = 'UPDATE' then v_old_status := OLD.status; end if;   -- OLD does not exist for INSERT
    -- C1: independent implementation/test evidence only.  Every exclusion is a top-level AND:
    -- no alternative path (type pattern OR evidence_kind) can bypass it.
    select max(e.event_id) from public.magi_task_events e where e.task_id = NEW.task_id
      and e.actor not in ('GEOMNURI_PATROL','마기(자동)','COMPLETION_GATE')
      and e.actor not ilike 'CASSANDRA%'
      and e.event_type !~* '(REQUEST|REQUIRE|READY|VALIDATION|VERIFICATION|CASSANDRA)'
      and upper(e.event_type) !~ v_neg                       -- revoked / invalid / failed / missing evidence is not evidence
      and (upper(e.event_type) = any (v_evid_types)          -- exact allowlist, no substring
           or upper(coalesce(e.payload->>'evidence_kind','')) in ('IMPLEMENTATION','TEST','CI','PRODUCTION')) into v_evid_max;
    v_c1 := v_evid_max is not null;
    select count(*) into v_cass_total from public.magi_task_events e
      where e.task_id = NEW.task_id and e.actor ilike 'CASSANDRA%';
    -- Secondary (마기) verification is recorded for information only; it never lowers a HARD result.
    select exists(select 1 from public.magi_task_events e where e.task_id = NEW.task_id
      and e.actor = '마기' and upper(coalesce(e.payload->>'verifier_role','')) = 'SECONDARY'
      and e.event_type !~* '(REQUEST|REQUIRE)'
      and upper(coalesce(e.payload->>'verdict','')) = 'PASS'
      and upper(coalesce(e.payload->>'stage','')) = 'FINAL') into v_sec_pass;
    select exists(select 1 from public.magi_task_events e where e.task_id = NEW.task_id
      and e.actor = '마기' and upper(coalesce(e.payload->>'verifier_role','')) = 'SECONDARY') into v_sec_other;

    -- J: latest CASSANDRA judgment event, chosen BEFORE looking at structure
    select e.event_id, e.event_type, coalesce(jsonb_typeof(e.payload) = 'object' and e.payload ? 'verdict', false),
           upper(e.payload->>'verdict'), upper(e.payload->>'stage'), e.payload->>'tier'
      into v_jid, v_jtype, v_structured, v_ver, v_stage, v_tier
      from public.magi_task_events e
      where e.task_id = NEW.task_id and e.actor ilike 'CASSANDRA%'
        and e.event_type !~* '(REQUEST|REQUIRE)'          -- common exclusion: a request is never a judgment
        and ((e.event_type ~* '(VALIDATION|DESIGN_PASS)' or upper(e.event_type) ~ v_revoke)
             or coalesce(jsonb_typeof(e.payload) = 'object' and e.payload ? 'verdict', false))
      order by e.event_id desc limit 1;
    v_found := found;

    if v_cass_total = 0 then
      v_hard := array_append(v_hard, 'NO_CASSANDRA_RECORD'::text); v_class := 'NONE';
    elsif not v_found then
      v_rev := array_append(v_rev, 'NO_JUDGMENT_EVENT'::text); v_class := 'NO_JUDGMENT';
    elsif upper(v_jtype) ~ v_revoke then
      -- the latest CASSANDRA judgment-type event withdraws/invalidates an earlier verdict: never a pass,
      -- whatever stale verdict/stage its payload still carries
      v_hard := array_append(v_hard, 'LATEST_JUDGMENT_REVOKED'::text); v_class := 'REVOKED';
    elsif not v_structured then
      v_rev := array_append(v_rev, 'LATEST_VERDICT_UNSTRUCTURED'::text); v_class := 'UNSTRUCTURED';
    elsif v_ver ~ '^(PARTIAL|HOLD|FAIL|CONDITIONAL|REJECT)' then
      v_hard := array_append(v_hard, 'LATEST_VERDICT_NOT_PASS'::text); v_class := 'NEG';
    elsif v_ver ~ '^PASS(_[A-Z]+)?$' then
      if v_ver ~ 'DESIGN' or v_jtype ~* 'DESIGN' or v_stage = 'DESIGN' then
        v_rev := array_append(v_rev, 'DESIGN_PASS_ONLY'::text); v_class := 'PASS_DESIGN';
      elsif v_ver = 'PASS' and v_stage = 'FINAL' and upper(v_jtype) = any (v_final_types) then v_class := 'PASS_FINAL';  -- exact allowlist
      elsif v_ver = 'PASS' and v_stage = 'FINAL' then
        v_rev := array_append(v_rev, 'PASS_NOT_FROM_VALIDATION_EVENT'::text); v_class := 'PASS_NON_VALIDATION';
      else v_rev := array_append(v_rev, 'PASS_STAGE_NOT_FINAL'::text); v_class := 'PASS_PARTIAL_STAGE'; end if;
    else
      v_hard := array_append(v_hard, 'LATEST_VERDICT_NOT_PASS'::text); v_class := 'UNKNOWN';
    end if;
    -- a PASS only stands if nothing after it questions it: (a) no later CASSANDRA event of an unknown kind
    -- (a withdrawal can be worded in ways no vocabulary list anticipates), (b) no independent evidence newer than the PASS
    -- (reopened and changed work needs a fresh validation).
    if v_class = 'PASS_FINAL' then
      select exists(select 1 from public.magi_task_events e where e.task_id = NEW.task_id and e.actor ilike 'CASSANDRA%'
        and e.event_id > v_jid and upper(e.event_type) <> all (v_neutral_types)) into v_later;
      if v_later then v_rev := array_append(v_rev, 'LATER_CASSANDRA_EVENT_UNCLASSIFIED'::text); end if;
      if v_evid_max is not null and v_evid_max > v_jid then v_rev := array_append(v_rev, 'EVIDENCE_NEWER_THAN_PASS'::text); end if;
    end if;
    if not v_c1 then v_rev := array_append(v_rev, 'EVIDENCE_NOT_RECOGNIZED'::text); end if;
    -- clearing verification_required in the same UPDATE that completes the task must not bypass the gate
    if NEW.verification_required is not true then v_rev := array_append(v_rev, 'VERIFICATION_REQUIRED_CLEARED'::text); end if;

    if array_length(v_hard,1) > 0 or array_length(v_rev,1) > 0 then
      insert into public.magi_task_events(task_id, event_type, actor, detail, payload)
      values (NEW.task_id, 'COMPLETION_GATE_VIOLATION', 'COMPLETION_GATE',
        '완료 게이트 관찰: 검증 기록 점검 필요 ('||array_to_string(v_hard||v_rev, ',')||'). 관찰 모드로 전이는 차단하지 않음.',
        jsonb_build_object('mode','observe',
          'gate_level', case when array_length(v_hard,1) > 0 then 'WARN' else 'REVIEW' end,
          'reasons', v_hard||v_rev,'op',TG_OP,'from_status',v_old_status,'to_status',NEW.status,'task_priority',NEW.priority,
          'last_judgment_event_id', v_jid,'last_verdict', v_ver,'last_stage', v_stage,'tier_recorded', v_tier,
          'verdict_class', v_class,
          'checks', jsonb_build_object('independent_evidence',v_c1,'cassandra_events',v_cass_total,
            'secondary_pass_recorded',v_sec_pass,'secondary_events_recorded',v_sec_other)));
    end if;
  exception when others then
    raise warning 'completion_gate_observe failed: %', sqlerrm;
  end;
  return NEW;
end $$;
revoke all on function public.fn_completion_gate_observe() from public, anon, authenticated;

create trigger trg_magi_tasks_completion_gate_observe
after update of status on public.magi_tasks
for each row
when ((NEW.verification_required is true or OLD.verification_required is true) and NEW.status in ('COMPLETED','CLOSED')
      and OLD.status not in ('COMPLETED','CLOSED'))
execute function public.fn_completion_gate_observe();

-- a task created directly in a terminal state with verification_required = true never passes through an UPDATE
create trigger trg_magi_tasks_completion_gate_observe_ins
after insert on public.magi_tasks
for each row
when (NEW.verification_required is true and NEW.status in ('COMPLETED','CLOSED'))
execute function public.fn_completion_gate_observe();
```
롤백: `drop trigger trg_magi_tasks_completion_gate_observe on magi_tasks; drop trigger trg_magi_tasks_completion_gate_observe_ins on magi_tasks; drop function fn_completion_gate_observe();` 기록된 위반 이벤트는 삭제하지 않는다(append-only).

## 6. 시험 계획
### 6.1 이미 실행한 시험 (2026-10-12 17:4x KST, 운영 무변경, 가상 태스크는 CTE 값으로만 존재)
**(a) 카산드라 4997 실증 반례 + 추가 반례를 v1.2 선택·분류식에 적용: 10/10 기대와 일치**
| 가상 태스크 | 이벤트 이력 | 기대 | 결과 |
|---|---|---|---|
| 99001 | 증거 → 카산드라 PASS/FINAL → 카산드라 비구조 검증 이벤트(나중) | REVIEW LATEST_VERDICT_UNSTRUCTURED (과거 PASS 불인정) | 일치 |
| 99011 | 카산드라 PASS/FINAL 단독, 별개 증거 0건 | 증거 불충족 + PASS_FINAL (자기충족 아님) | 일치(PASS_FINAL +EVIDENCE_NOT_RECOGNIZED) |
| 99012 | 캐스퍼 증거 + 카산드라 PASS/FINAL | 기록 없음 | 일치 |
| 99013 | PASS/FINAL 뒤에 FAIL/FINAL | HARD LATEST_VERDICT_NOT_PASS | 일치 |
| 99014 | 비구조 뒤에 PASS/FINAL(나중) | 최신 우선으로 통과 | 일치 |
| 99015 | PASS/FINAL 뒤에 AGREEMENT_RESPONSE(판정 아님) | 통과 유지 | 일치 |
| 99016 | 카산드라 0건, 마기 SECONDARY PASS/FINAL | HARD NO_CASSANDRA_RECORD (secondary는 정보 checks만, v1.3) | 일치 |
| 99017 | 카산드라 VALIDATION_REQUESTED만 | REVIEW NO_JUDGMENT_EVENT | 일치 |
| 99018 | 요청 이벤트(VERIFICATION_REQUESTED, IMPLEMENTATION_REQUESTED)만 + 카산드라 PASS/FINAL | 증거 불충족 | 일치 |
| 99019 | 증거 + 카산드라 PASS/DESIGN | REVIEW DESIGN_PASS_ONLY | 일치 |
**(b) 증거 이벤트 타입 20종 판정: 20/20 일치.** 허용 7(EVIDENCE, MAGI_EVIDENCE, EVIDENCE_SUBMITTED, IMPLEMENTATION_CI_PASS, IMPLEMENTATION_PR_DRAFT_CREATED, PRODUCTION_BRIDGE_GATE_EVIDENCE, FINAL_PRODUCTION_HTTP_E2E_EVIDENCE) / 불허 13(VERIFICATION_COMPLETED, CASSANDRA_VALIDATION, CASSANDRA_DESIGN_PASS, IMPLEMENTATION_READY_FOR_VERIFICATION, VERIFICATION_READY, VERIFICATION_REQUESTED, IMPLEMENTATION_REQUESTED, VERIFICATION_REQUEST, CASSANDRA_VALIDATION_REQUESTED, CASSANDRA_EVIDENCE_REQUEST, DESIGN_V2_EVIDENCE_REQUIREMENTS, CASSANDRA_REVALIDATION_REQUESTED, IMPLEMENTATION_PATCH_COMMITTED_REVALIDATION_REQUIRED).
**(c) verdict·stage 분류 15/15 일치(읽기 전용 SELECT):** PARTIAL_PASS/FINAL, FAIL_DESIGN(과도기 값), HOLD/FINAL, FAIL/DESIGN → NEG / PASS+FINAL, 소문자 pass+final → PASS_FINAL / PASS 단계 누락, PASS+IMPLEMENTATION, 비표준 PASS_FINAL → PASS_PARTIAL_STAGE / PASS+DESIGN, 과도기 PASS_DESIGN, PASS+FINAL이지만 설계 검증 event_type → PASS_DESIGN / MAYBE → UNKNOWN / 판정값 없는 과거 이벤트 → 비구조 / 카산드라가 아닌 actor의 PASS+FINAL → 불인정.
**(d) 13건 백테스트 재실행:** 3절 표.
**(f) v1.8 전수 조합 시험: 32,600 조합 불일치 0** (0절, `tests/sql/task197_gate_matrix.py`, 변이 3종 검출 확인). 참고치이며 운영 PostgreSQL 독립 재현은 카산드라의 몫.
**(e) 임시 로컬 PostgreSQL 16에서 실제 트리거 실행: 29/29 통과 (v1.5: 운영과 같은 RLS·identity 스키마, anon 경로 A1·A2 추가 / v1.8: S1 임시 테이블 shadowing, I1~I4 INSERT 경로)** (운영이 아닌 이 작업 환경의 임시 서버, 최소 대역 스키마. 재현: `tests/sql/task197_gate_local_dryrun.py`, 스키마 `tests/sql/task197_gate_schema.sql`). 함수·트리거 생성, 양성 13(P1~P13: 위 (a)의 10개 이력 + 구형 NULL payload, 미정의 판정값, 단계 누락, 타 actor PASS, evidence_kind 경로), 음성 6(N1~N6: 증거+PASS 정상, HOLD 뒤 PASS 최신 우선, vr=false, 비완료 전이, 판정 아닌 카산드라 이벤트, COMPLETED→CLOSED 비재발동 및 재오픈 후 재발동), 회귀 3(R1 fail-open: 위반 이벤트 insert가 막혀도 UPDATE 성공+경고, R3 위반 이벤트에 순라 키·표식 없음, anon 실행 권한 없음).
**시험 중 발견·수정한 결함 2건:**
1. (읽기 전용 단계) payload가 NULL인 과거 이벤트가 `NOT NULL`로 평가돼 구형 10건이 HARD로 잘못 분류됨 → 모든 곳에 `coalesce(payload ? 'verdict', false)`.
2. (로컬 실행 단계) **v1·v1.1 초안 SQL은 실제로는 동작하지 않았다.** `text[] || '문자열'`에서 문자열이 배열 리터럴로 해석돼 `malformed array literal` 오류가 났고, fail-open 설계 때문에 이 오류가 삼켜져 위반 이벤트가 하나도 기록되지 않았다(첫 실행 9/22). `array_append(배열, '값'::text)`로 고쳐 22/22 통과. 교훈: fail-open 트리거는 결함이 있어도 조용히 아무것도 안 하므로, 적용 전 반드시 실제 실행 시험이 필요하고 관찰 기간 첫날 위반 이벤트 건수 대비 완료 전이 건수를 점검해야 한다(위반 이벤트 0건이 "정상"인지 "고장"인지 구분 불가).
**한계:** 로컬 시험은 최소 대역 스키마라 운영의 다른 트리거(domain 상속 등)·RLS·권한 구조는 재현하지 않는다. 운영 롤백 드라이런(6.2)은 카산드라 설계 PASS 이후 별도로 수행한다.

### 6.2 카산드라 설계 PASS 후 실행(롤백 드라이런, 단일 DO 블록 안에서 DDL+시험 후 강제 롤백)
6.1(a)의 10개 이력을 실제 magi_tasks 행 전이로 재현(P/N 시험)하고 추가로: (N3) vr=false 전이, (N4) status 외 컬럼 UPDATE, (N5) COMPLETED→CLOSED 재전이 비발동, (N6) 재오픈 후 재완료 재발동, (N7) actor=TAEO가 기재한 verdict=PASS 불인정.
회귀·안전: (R1) 트리거 함수가 예외를 내도 UPDATE 성공(fail-open). (R2) 봇 approve_task PATCH 경로. (R3) 위반 이벤트가 순라 underposting/slack_policy 점검을 유발하지 않음(분류기 함수와 순라 판정식에 payload/detail을 직접 입력). (R4) 3절 백테스트 13건과 결과 일치. (R5) 비용: 트리거는 `idx_magi_task_events_task`(task_id) 조회만, task_id 외 전체 스캔 없음.
종료증거: 위 시험 결과 이벤트 + 카산드라 독립 PASS. 관찰 2주 후 위반 집계·오탐 검토를 마기에 보고하고, 거부 전환은 별도 설계·승인.

## 7. 미확인/한계
- 봇 외 쓰기 경로(마기·카산드라·영실 커넥터)가 `session_replication_role=replica` 등으로 트리거를 우회할 가능성은 이론상 존재(실사용 증거 없음).
- 판정값 표준 어휘와 카산드라의 기록 관행이 합의되기 전에는 정상 통과 판정이 불가능하다(2.4절).
- 증거 이벤트 타입 정규식은 현 관행 기준이라 새 타입이 생기면 오탐/미탐 가능 → 관찰 기간에 오탐 목록으로 보정.
- 과거 위반(#145, #147, #25) 및 #174 추적성에 대한 소급 기록·정정 여부는 마기·대표님 판단.
- 이 문서는 설계이며 구현·스키마·배포는 (대표 승인 + 마기 배정 + 카산드라 해당 단계 PASS) 전부 충족 전까지 금지.
