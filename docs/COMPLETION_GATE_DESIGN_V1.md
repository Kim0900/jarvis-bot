# TASK#197 완료 게이트(관찰 모드) 설계 v1.2 (캐스퍼, 2026-10-12 KST)

상태: 설계 단계(v1.2). DB·코드·배포 변경 없음. 운영 적용은 카산드라 설계 재검증 PASS 이후.
범위(마기 지시): verification_required=true 태스크가 증거 이벤트·카산드라 PASS 없이 COMPLETED/CLOSED로 전이되면 위반 이벤트를 기록한다. 1차는 관찰(경고만), 거부 전환은 2주 관찰 후 별도 승인.

## 0. 변경 이력
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

## 2. 판정 규칙 v1.2 (관찰 모드)
대상: `NEW.verification_required IS TRUE AND NEW.status IN ('COMPLETED','CLOSED') AND OLD.status NOT IN ('COMPLETED','CLOSED')`.

### 2.1 카산드라 판정 이벤트 선택과 분류
**선택(구조화 여부를 보기 전에):** 같은 태스크에서 `actor ILIKE 'CASSANDRA%'` 이고
`(event_type ~* '(VALIDATION|REVALIDATION|DESIGN_PASS)' AND event_type !~* '(REQUEST|REQUIRE)') OR coalesce(payload ? 'verdict', false)` 인 이벤트 중 **event_id가 가장 큰 1건(J)** 이 현재 판정이다. 새 event_type이라도 payload.verdict가 있으면 판정 이벤트로 본다. 합의 응답·백로그 검토·증거 요청 등 판정이 아닌 카산드라 이벤트는 J 선택에 영향을 주지 않는다.
**분류(J에 대해서만):**
- J가 없음 → NO_JUDGMENT_EVENT. J에 `payload.verdict`가 없음 → LATEST_VERDICT_UNSTRUCTURED(과거에 구조화 PASS가 있어도 인정하지 않음).
- 표준 필드(2.4절): `verdict` ∈ {PASS, PARTIAL_PASS, CONDITIONAL, HOLD, FAIL}, `stage` ∈ {DESIGN, IMPLEMENTATION, FINAL}.
- NEG: verdict(대문자화)가 `^(PARTIAL|HOLD|FAIL|CONDITIONAL|REJECT)`로 시작(과도기 값 FAIL_DESIGN·FAIL_PATCH 포함).
- PASS_FINAL: verdict가 정확히 `PASS`이고 `stage = FINAL`이며 verdict·event_type에 DESIGN이 없음.
- PASS_DESIGN: PASS 형태인데 stage=DESIGN, 또는 verdict·event_type에 DESIGN 포함.
- PASS_PARTIAL_STAGE: PASS인데 stage=IMPLEMENTATION, 또는 stage 누락·미정의 값. 최종으로 인정하지 않는다.
- UNKNOWN: 그 외 모든 값. UNKNOWN은 PASS가 아니다.

### 2.2 증거 이벤트(C1): 독립 구현·시험 증거만
같은 태스크에서 다음을 모두 만족하는 이벤트가 1건 이상:
- actor가 GEOMNURI_PATROL·마기(자동)·COMPLETION_GATE가 아니고 **CASSANDRA%도 아님**(독립 검증자의 판정은 구현 증거가 아니다).
- `event_type ~* '(EVIDENCE|IMPLEMENTATION|CI_)'` 이고 `event_type !~* '(REQUEST|REQUIRE|READY|VALIDATION|VERIFICATION|CASSANDRA)'`, **또는** 신규 제안 필드 `payload.evidence_kind ∈ {IMPLEMENTATION, TEST, CI, PRODUCTION}`.
- 검증 요청·준비·판정(…_REQUESTED, …_READY, …VALIDATION, VERIFICATION_COMPLETED 등)은 증거가 아니다.
- 한계: 증거의 "내용 충분성"은 판정하지 않는다(존재 여부만). 충분성은 카산드라 판정의 몫. 과거 이벤트 이름에 의존하므로 구형 태스크에서는 증거를 못 알아볼 수 있다(3절 참고).

### 2.3 결과 분류
| 구분 | reason | 조건 |
|---|---|---|
| HARD (gate_level=WARN) | NO_CASSANDRA_RECORD | 같은 태스크에 actor=CASSANDRA% 이벤트가 0건이고 마기 보조검증도 없음 |
| HARD | LATEST_VERDICT_NOT_PASS | J가 구조화돼 있으나 NEG 또는 UNKNOWN |
| REVIEW (gate_level=REVIEW) | LATEST_VERDICT_UNSTRUCTURED | J에 구조화 판정값이 없음 |
| REVIEW | NO_JUDGMENT_EVENT | 카산드라 이벤트는 있으나 판정 이벤트가 없음 |
| REVIEW | DESIGN_PASS_ONLY | J가 PASS_DESIGN |
| REVIEW | PASS_STAGE_NOT_FINAL | J가 PASS이나 stage가 IMPLEMENTATION이거나 누락 |
| REVIEW | SECONDARY_ONLY_NOT_ACCEPTED | 카산드라 이벤트 0건이고 actor=마기, verifier_role=SECONDARY 판정만 존재(2.4절) |
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
4. **MEDIUM/LOW 보조검증:** 마기 보조검증은 actor=마기, `payload.verifier_role=SECONDARY`, verdict=PASS, stage=FINAL 로 별도 구분하며 마기가 설계·작성한 건은 제외한다(#200 규칙). **이 설계에서는 보조검증을 통과 근거로 인정하지 않고** SECONDARY_ONLY_NOT_ACCEPTED(REVIEW)로 기록만 한다. 인정하려면 #200 구현 후 별도 설계 변경과 카산드라 검증이 필요하다.
5. `payload.tier`와 `payload.target`은 위반 이벤트 payload에 기록용으로 복사할 뿐 판정 로직에는 쓰지 않는다.
6. 어휘를 코드로 강제하는 입력 검사는 이번 범위 밖이다. 관찰 2주 동안 어휘 이탈 건수를 보고서로 세고 필요하면 별도 태스크로 올린다.
7. 합의 전까지 이 게이트가 낼 수 있는 결과는 HARD와 REVIEW 뿐이며 "정상 통과"는 합의 이후에만 가능하다(의도된 fail-safe). 카산드라 4996은 합의 응답이며 설계 PASS가 아니다.

## 3. 백테스트 (read-only, 2026-10-12 17:44 KST 스냅샷 재실행, 완료 vr=true 13건)
| 태스크 | C1(v1.2) | 카산드라 이벤트 | 최신 판정 이벤트 | v1.2 결과 |
|---|---|---|---|---|
| 25 | 불충족 | 0 | 없음 | HARD NO_CASSANDRA_RECORD + EVIDENCE_NOT_RECOGNIZED |
| 145 | 불충족 | 0 | 없음 | HARD NO_CASSANDRA_RECORD + EVIDENCE_NOT_RECOGNIZED |
| 147 | 불충족 | 0 | 없음 | HARD NO_CASSANDRA_RECORD + EVIDENCE_NOT_RECOGNIZED |
| 160, 164, 174 | 충족 | 3, 10, 1 | 3774, 4003, 4233 | REVIEW LATEST_VERDICT_UNSTRUCTURED |
| 83, 157, 167, 170, 171, 172, 175 | 불충족 | 1~4 | 3709, 3725, 3926, 4011, 4077, 4070, 4182 | REVIEW LATEST_VERDICT_UNSTRUCTURED + EVIDENCE_NOT_RECOGNIZED |
- 합계 HARD 3 / REVIEW 10. 카산드라 4997의 독립 재현(HARD 3/REVIEW 10)과 일치한다.
- 해석: 알려진 위반 3건(25, 145, 147)은 HARD로 잡힌다. 나머지 10건은 최신 카산드라 이벤트가 구조화 전이라 "통과"로 인정되지 않는다. C1을 독립 구현·시험 증거로 좁힌 결과 구형 태스크 10건 중 7건은 증거 이벤트를 알아보지 못한다. 이는 과거 이벤트 이름 관행 때문이며(검증·요청 이벤트로 증거를 대신하던 관행) 신규 전이에서는 `payload.evidence_kind` 관행이 정착되면 줄어든다. 구형 태스크 판정 자체가 틀렸다는 뜻은 아니다.
- 트리거는 신규 전이에만 발동하므로 이 표는 규칙 점검용이며 과거 태스크에 이벤트를 기록하지 않는다. 소급 처리 여부는 마기·대표님 판단.
- #174 추적성 문제(PASS가 #176 이벤트 안에 기재)는 배치 보고서에서 별도 점검한다.
- 한계: actor 필드는 쓰기 권한자가 임의로 채울 수 있어 이 게이트는 "누락 탐지"이지 "위조 방지"가 아니다.

## 4. 기존 경로·순라 회귀 방지 (v1 유지)
- 순라 `control_classifier_underposting`(bot_v5_legacy.py L6344~)은 payload의 severity/level/priority 또는 detail의 특정 표식 문자열(적색 이모지, 대괄호 크리티컬 표기, P0·P1 뒤 공백 등)이 있는데 `_classify_control_event`가 non_control_event로 분류하면 CRITICAL 경보를 낸다. 2026-10-12 16:08 캐스퍼의 이벤트 4958이 이 표식을 인용해 실제 오탐 경보를 일으켰다(이벤트 4962). → 위반 이벤트의 payload는 severity/level/priority 키를 쓰지 않고(`gate_level` 사용), detail 템플릿에 표식을 넣지 않는다. 태스크 우선순위는 payload.task_priority로만 기록.
- 신규 event_type은 현 분류기에서 `non_control_event`(Slack 게시 후보 아님)로 분류된다. Slack 게시는 별도 코드 변경이라 이번 범위 제외(관찰 기간 중 SQL 집계).
- 이벤트 insert는 magi_tasks를 건드리지 않아 재귀 없음. `fn_inherit_domain_from_task`가 domain을 채운다.
- 쓰기 경로 회귀: (a) 봇 PATCH `approve_task`, (b) 마기(자동) COMPLETED(vr=false → 대상 아님), (c) 에이전트 직접 SQL UPDATE, (d) 비상태 컬럼 UPDATE(트리거 미발동)를 롤백 트랜잭션 안에서 전/후 비교.

## 5. 초안 SQL v1.2 (미적용)
```sql
create or replace function public.fn_completion_gate_observe() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_c1 boolean; v_cass_total int; v_secondary boolean;
  v_jid bigint; v_jtype text; v_structured boolean := false; v_ver text; v_stage text; v_tier text;
  v_found boolean := false; v_class text;
  v_hard text[] := '{}'; v_rev text[] := '{}';
begin
  begin
    -- C1: independent implementation/test evidence only (never the verifier's own events)
    select exists(select 1 from magi_task_events e where e.task_id = NEW.task_id
      and e.actor not in ('GEOMNURI_PATROL','마기(자동)','COMPLETION_GATE')
      and e.actor not ilike 'CASSANDRA%'
      and ((e.event_type ~* '(EVIDENCE|IMPLEMENTATION|CI_)'
            and e.event_type !~* '(REQUEST|REQUIRE|READY|VALIDATION|VERIFICATION|CASSANDRA)')
           or coalesce(e.payload->>'evidence_kind','') in ('IMPLEMENTATION','TEST','CI','PRODUCTION'))) into v_c1;
    select count(*) into v_cass_total from magi_task_events e
      where e.task_id = NEW.task_id and e.actor ilike 'CASSANDRA%';
    select exists(select 1 from magi_task_events e where e.task_id = NEW.task_id
      and e.actor = '마기' and e.payload->>'verifier_role' = 'SECONDARY') into v_secondary;

    -- J: latest CASSANDRA judgment event, chosen BEFORE looking at structure
    select e.event_id, e.event_type, coalesce(e.payload ? 'verdict', false),
           upper(e.payload->>'verdict'), upper(e.payload->>'stage'), e.payload->>'tier'
      into v_jid, v_jtype, v_structured, v_ver, v_stage, v_tier
      from magi_task_events e
      where e.task_id = NEW.task_id and e.actor ilike 'CASSANDRA%'
        and ((e.event_type ~* '(VALIDATION|REVALIDATION|DESIGN_PASS)' and e.event_type !~* '(REQUEST|REQUIRE)')
             or coalesce(e.payload ? 'verdict', false))
      order by e.event_id desc limit 1;
    v_found := found;

    if v_cass_total = 0 then
      if v_secondary then v_rev := array_append(v_rev, 'SECONDARY_ONLY_NOT_ACCEPTED'::text); v_class := 'SECONDARY';
      else v_hard := array_append(v_hard, 'NO_CASSANDRA_RECORD'::text); v_class := 'NONE'; end if;
    elsif not v_found then
      v_rev := array_append(v_rev, 'NO_JUDGMENT_EVENT'::text); v_class := 'NO_JUDGMENT';
    elsif not v_structured then
      v_rev := array_append(v_rev, 'LATEST_VERDICT_UNSTRUCTURED'::text); v_class := 'UNSTRUCTURED';
    elsif v_ver ~ '^(PARTIAL|HOLD|FAIL|CONDITIONAL|REJECT)' then
      v_hard := array_append(v_hard, 'LATEST_VERDICT_NOT_PASS'::text); v_class := 'NEG';
    elsif v_ver ~ '^PASS(_[A-Z]+)?$' then
      if v_ver ~ 'DESIGN' or v_jtype ~ 'DESIGN' or v_stage = 'DESIGN' then
        v_rev := array_append(v_rev, 'DESIGN_PASS_ONLY'::text); v_class := 'PASS_DESIGN';
      elsif v_ver = 'PASS' and v_stage = 'FINAL' then v_class := 'PASS_FINAL';
      else v_rev := array_append(v_rev, 'PASS_STAGE_NOT_FINAL'::text); v_class := 'PASS_PARTIAL_STAGE'; end if;
    else
      v_hard := array_append(v_hard, 'LATEST_VERDICT_NOT_PASS'::text); v_class := 'UNKNOWN';
    end if;
    if not v_c1 then v_rev := array_append(v_rev, 'EVIDENCE_NOT_RECOGNIZED'::text); end if;

    if array_length(v_hard,1) > 0 or array_length(v_rev,1) > 0 then
      insert into magi_task_events(task_id, event_type, actor, detail, payload)
      values (NEW.task_id, 'COMPLETION_GATE_VIOLATION', 'COMPLETION_GATE',
        '완료 게이트 관찰: 검증 기록 점검 필요 ('||array_to_string(v_hard||v_rev, ',')||'). 관찰 모드로 전이는 차단하지 않음.',
        jsonb_build_object('mode','observe',
          'gate_level', case when array_length(v_hard,1) > 0 then 'WARN' else 'REVIEW' end,
          'reasons', v_hard||v_rev,'from_status',OLD.status,'to_status',NEW.status,'task_priority',NEW.priority,
          'last_judgment_event_id', v_jid,'last_verdict', v_ver,'last_stage', v_stage,'tier_recorded', v_tier,
          'verdict_class', v_class,
          'checks', jsonb_build_object('independent_evidence',v_c1,'cassandra_events',v_cass_total)));
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
when (NEW.verification_required is true and NEW.status in ('COMPLETED','CLOSED')
      and OLD.status not in ('COMPLETED','CLOSED'))
execute function public.fn_completion_gate_observe();
```
롤백: `drop trigger trg_magi_tasks_completion_gate_observe on magi_tasks; drop function fn_completion_gate_observe();` 기록된 위반 이벤트는 삭제하지 않는다(append-only).

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
| 99016 | 카산드라 0건, 마기 SECONDARY PASS/FINAL | REVIEW SECONDARY_ONLY_NOT_ACCEPTED | 일치 |
| 99017 | 카산드라 VALIDATION_REQUESTED만 | REVIEW NO_JUDGMENT_EVENT | 일치 |
| 99018 | 요청 이벤트(VERIFICATION_REQUESTED, IMPLEMENTATION_REQUESTED)만 + 카산드라 PASS/FINAL | 증거 불충족 | 일치 |
| 99019 | 증거 + 카산드라 PASS/DESIGN | REVIEW DESIGN_PASS_ONLY | 일치 |
**(b) 증거 이벤트 타입 20종 판정: 20/20 일치.** 허용 7(EVIDENCE, MAGI_EVIDENCE, EVIDENCE_SUBMITTED, IMPLEMENTATION_CI_PASS, IMPLEMENTATION_PR_DRAFT_CREATED, PRODUCTION_BRIDGE_GATE_EVIDENCE, FINAL_PRODUCTION_HTTP_E2E_EVIDENCE) / 불허 13(VERIFICATION_COMPLETED, CASSANDRA_VALIDATION, CASSANDRA_DESIGN_PASS, IMPLEMENTATION_READY_FOR_VERIFICATION, VERIFICATION_READY, VERIFICATION_REQUESTED, IMPLEMENTATION_REQUESTED, VERIFICATION_REQUEST, CASSANDRA_VALIDATION_REQUESTED, CASSANDRA_EVIDENCE_REQUEST, DESIGN_V2_EVIDENCE_REQUIREMENTS, CASSANDRA_REVALIDATION_REQUESTED, IMPLEMENTATION_PATCH_COMMITTED_REVALIDATION_REQUIRED).
**(c) verdict·stage 분류 15/15 일치(읽기 전용 SELECT):** PARTIAL_PASS/FINAL, FAIL_DESIGN(과도기 값), HOLD/FINAL, FAIL/DESIGN → NEG / PASS+FINAL, 소문자 pass+final → PASS_FINAL / PASS 단계 누락, PASS+IMPLEMENTATION, 비표준 PASS_FINAL → PASS_PARTIAL_STAGE / PASS+DESIGN, 과도기 PASS_DESIGN, PASS+FINAL이지만 설계 검증 event_type → PASS_DESIGN / MAYBE → UNKNOWN / 판정값 없는 과거 이벤트 → 비구조 / 카산드라가 아닌 actor의 PASS+FINAL → 불인정.
**(d) 13건 백테스트 재실행:** 3절 표.
**(e) 임시 로컬 PostgreSQL 16에서 실제 트리거 실행: 22/22 통과** (운영이 아닌 이 작업 환경의 임시 서버, 최소 대역 스키마. 재현: `tests/sql/task197_gate_local_dryrun.py`, 스키마 `tests/sql/task197_gate_schema.sql`). 함수·트리거 생성, 양성 13(P1~P13: 위 (a)의 10개 이력 + 구형 NULL payload, 미정의 판정값, 단계 누락, 타 actor PASS, evidence_kind 경로), 음성 6(N1~N6: 증거+PASS 정상, HOLD 뒤 PASS 최신 우선, vr=false, 비완료 전이, 판정 아닌 카산드라 이벤트, COMPLETED→CLOSED 비재발동 및 재오픈 후 재발동), 회귀 3(R1 fail-open: 위반 이벤트 insert가 막혀도 UPDATE 성공+경고, R3 위반 이벤트에 순라 키·표식 없음, anon 실행 권한 없음).
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
