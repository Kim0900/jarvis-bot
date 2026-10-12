# TASK#197 완료 게이트(관찰 모드) 설계 v1.1 (캐스퍼, 2026-10-12 KST)

상태: 설계 단계(v1.1). DB·코드·배포 변경 없음. 운영 적용은 카산드라 설계 재검증 PASS 이후.
범위(마기 지시): verification_required=true 태스크가 증거 이벤트·카산드라 PASS 없이 COMPLETED/CLOSED로 전이되면 위반 이벤트를 기록한다. 1차는 관찰(경고만), 거부 전환은 2주 관찰 후 별도 승인.

## 0. v1 → v1.1 변경 요약 (카산드라 이벤트 4967 FAIL 반영)
| 카산드라 지적 | v1 결함 | v1.1 조치 |
|---|---|---|
| C2가 `PARTIAL PASS / 전체 HOLD`, `FAIL — PASS 조건 미충족`을 PASS로 판정 | 본문 정규식 `PASS` 포함 + 시작 앵커. 본문이 `[CASSANDRA ...]`로 시작해 앵커가 무력 | 본문 해석 폐기. 구조화 판정값 `payload.verdict`만 사용 |
| C1이 VERIFICATION_REQUESTED·IMPLEMENTATION_REQUESTED를 증거로 인정 | event_type 단순 포함 정규식 | 요청·요구(REQUEST/REQUIRE) 계열 제외, 시험표로 고정 |
| 증거 목적/단계 미구별 | 설계 PASS와 최종 PASS 혼용 가능 | 설계 단계 PASS는 별도 분류(DESIGN_PASS_ONLY), 최종 PASS로 인정 안 함 |
| 애매한 기록 fail-safe 미검토 | 알 수 없으면 PASS로 기울 수 있음 | 알 수 없음은 절대 PASS가 아님. 위반(HARD) 또는 검토(REVIEW)로만 분류 |
| 골든테스트·백테스트 재실행 | 13건 결과를 재현 근거 없이 기재 | 아래 3·6절에 현 스냅샷(2026-10-12 16:50 KST)으로 재실행한 결과 기재 |

## 1. 실측 (v1에서 유지 + 재확인)
- magi_tasks에는 상태 전이를 검사·기록하는 트리거가 없다(트리거: delete_log, require_domain_on_insert뿐). 완료 전이는 에이전트의 직접 SQL, 봇 `approve_task`(PATCH), 마기(자동) 검증(bot_v5_legacy.py L7187) 등 여러 경로로 일어난다. → 모든 쓰기 경로를 포괄하려면 행 단위 DB 트리거가 유일한 지점이다.
- evidence_registry 테이블은 완료된 verification_required 태스크에서 쓰이지 않는다(0행). 실제 증거 기록은 magi_task_events 이벤트다.
- **신규 실측 (v1.1 설계 근거):** actor=CASSANDRA 이벤트 중 `payload.verdict`가 있는 것은 4965~4970 6건뿐이고 나머지 과거 이벤트는 자유서술이다. 값 어휘: PARTIAL_PASS, HOLD, FAIL_DESIGN, FAIL_PATCH. 과거 본문은 `PASS`로 시작해도 300자 안에 HOLD·FAIL 같은 단어가 흔히 섞여, 정규식으로 PASS를 안전하게 판정하는 방법이 없다(시도한 보수 규칙에서 PASS 시작 본문 다수가 판정불가로 남음). → 본문 해석은 포기하고 구조화값을 기준으로 한다.

## 2. 판정 규칙 v1.1 (관찰 모드)
대상: `NEW.verification_required IS TRUE AND NEW.status IN ('COMPLETED','CLOSED') AND OLD.status NOT IN ('COMPLETED','CLOSED')`.

### 2.1 카산드라 판정값 분류 (구조화 전용)
대상 이벤트: 같은 태스크의 `actor ILIKE 'CASSANDRA%'` 이면서 `payload ? 'verdict'`인 것. 가장 최근(event_id 최대) 1건이 결정한다(최신 우선: PASS 뒤에 HOLD가 오면 HOLD).
- NEG: verdict(대문자화)가 `^(PARTIAL|HOLD|FAIL|CONDITIONAL|REJECT)`로 시작.
- PASS_FINAL: `^PASS(_[A-Z]+)?$` 이면서 verdict에 DESIGN이 없고 event_type에도 DESIGN이 없음.
- PASS_DESIGN: 위 PASS 형태인데 verdict 또는 event_type에 DESIGN 포함.
- UNKNOWN: 그 외 모든 값. UNKNOWN은 PASS가 아니다.
- 판정값 없는 과거 카산드라 이벤트는 LEGACY(본문을 읽지 않는다).

### 2.2 증거 이벤트(C1)
같은 태스크에 actor가 GEOMNURI_PATROL·마기(자동)·COMPLETION_GATE가 아닌 이벤트 중 `event_type ~* '(EVIDENCE|VERIFICATION|VALIDATION|REVALIDATION|IMPLEMENTATION)'` 이고 `event_type !~* '(REQUEST|REQUIRE)'`.
- 요청·요구 계열(…_REQUESTED, …_REQUEST, …_REQUIRED, …_REQUIREMENTS)은 증거가 아니다.
- 한계: 증거의 "내용 충분성"은 판정하지 않는다(존재 여부만). 충분성은 카산드라 판정의 몫.

### 2.3 결과 분류
| 구분 | reason | 조건 |
|---|---|---|
| HARD (gate_level=WARN) | NO_CASSANDRA_RECORD | 같은 태스크에 actor=CASSANDRA% 이벤트가 0건 |
| HARD | LATEST_VERDICT_NOT_PASS | 최신 구조화 판정이 NEG 또는 UNKNOWN |
| REVIEW (gate_level=REVIEW) | LEGACY_UNSTRUCTURED_ONLY | 카산드라 이벤트는 있으나 구조화 판정이 한 건도 없음 |
| REVIEW | DESIGN_PASS_ONLY | 최신 구조화 판정이 PASS_DESIGN |
| REVIEW | EVIDENCE_NOT_RECOGNIZED | C1 불충족 |
- 최신 구조화 판정이 PASS_FINAL이고 C1도 충족이면 기록 없음.
- HARD가 하나라도 있으면 gate_level=WARN, REVIEW만 있으면 REVIEW.
- v1의 C2'(다른 태스크에 기재된 PASS를 본문 정규식으로 추적)는 같은 취약점이 있고 전체 스캔 비용도 있어 **트리거에서 제거**하고, 관찰 기간의 배치 보고서 SQL로 이동한다(트리거는 본문을 읽지 않는다).
- 기록: magi_task_events에 event_type=`COMPLETION_GATE_VIOLATION`, actor=`COMPLETION_GATE`, old/new_status는 NULL, payload에 mode·gate_level·reasons·last_verdict·last_verdict_event_id·checks·전이 전후 상태·task_priority.
- 항상 fail-open: 트리거 내부 오류는 EXCEPTION으로 삼키고 RAISE WARNING만. 거부 모드는 이 설계 범위 밖.

### 2.4 선결 합의 사항 (운영 적용의 전제, 마기·카산드라)
1. 카산드라가 최종·설계 판정 이벤트마다 `payload.verdict`를 표준 어휘로 기록한다. 이미 4965~4970이 이 방식이라 관행의 공식화다. 어휘 확정 필요: `PASS`(최종), `PASS_DESIGN`(설계), `PARTIAL_PASS`, `HOLD`, `FAIL_*`, `CONDITIONAL`. 특히 최종 PASS의 정확한 값 표기는 아직 관측된 적이 없다.
2. 합의 전까지 이 게이트가 낼 수 있는 결과는 사실상 HARD(NO_CASSANDRA_RECORD·NEG)와 REVIEW 뿐이며, "정상 통과" 판정은 합의 이후에만 가능하다. 이것이 의도된 fail-safe다.

## 3. 백테스트 (read-only, 2026-10-12 16:50 KST 스냅샷 재실행, 완료 vr=true 13건)
| 태스크 | C1(v1.1) | 카산드라 이벤트 | 구조화 판정 | v1.1 결과 |
|---|---|---|---|---|
| 25 | 충족 | 0 | 0 | HARD NO_CASSANDRA_RECORD |
| 145 | 불충족 | 0 | 0 | HARD NO_CASSANDRA_RECORD + REVIEW EVIDENCE_NOT_RECOGNIZED |
| 147 | 불충족 | 0 | 0 | HARD NO_CASSANDRA_RECORD + REVIEW EVIDENCE_NOT_RECOGNIZED |
| 83, 157, 160, 164, 167, 170, 171, 172, 174, 175 | 모두 충족 | 1~10건 | 0 | REVIEW LEGACY_UNSTRUCTURED_ONLY |
- 해석: 알려진 위반 3건(25, 145, 147)은 HARD로 잡힌다. 나머지 10건은 카산드라 검증 기록이 있지만 구조화 판정이 없어 "자동 통과"가 아니라 REVIEW로 남는다. v1에서 OK 9건으로 분류했던 것은 본문 정규식 덕분이었고, 그 근거가 신뢰할 수 없으므로 철회한다.
- 트리거는 신규 전이에만 발동하므로 이 표는 규칙 점검용이며 과거 태스크에 이벤트를 기록하지 않는다. 소급 처리 여부는 마기·대표님 판단.
- #174 추적성 문제(PASS가 #176 이벤트 안에 기재)는 배치 보고서에서 별도 점검한다.
- 한계: actor 필드는 쓰기 권한자가 임의로 채울 수 있어 이 게이트는 "누락 탐지"이지 "위조 방지"가 아니다.

## 4. 기존 경로·순라 회귀 방지 (v1 유지)
- 순라 `control_classifier_underposting`(bot_v5_legacy.py L6344~)은 payload의 severity/level/priority 또는 detail의 특정 표식 문자열(적색 이모지, 대괄호 크리티컬 표기, P0·P1 뒤 공백 등)이 있는데 `_classify_control_event`가 non_control_event로 분류하면 CRITICAL 경보를 낸다. 2026-10-12 16:08 캐스퍼의 이벤트 4958이 이 표식을 인용해 실제 오탐 경보를 일으켰다(이벤트 4962). → 위반 이벤트의 payload는 severity/level/priority 키를 쓰지 않고(`gate_level` 사용), detail 템플릿에 표식을 넣지 않는다. 태스크 우선순위는 payload.task_priority로만 기록.
- 신규 event_type은 현 분류기에서 `non_control_event`(Slack 게시 후보 아님)로 분류된다. Slack 게시는 별도 코드 변경이라 이번 범위 제외(관찰 기간 중 SQL 집계).
- 이벤트 insert는 magi_tasks를 건드리지 않아 재귀 없음. `fn_inherit_domain_from_task`가 domain을 채운다.
- 쓰기 경로 회귀: (a) 봇 PATCH `approve_task`, (b) 마기(자동) COMPLETED(vr=false → 대상 아님), (c) 에이전트 직접 SQL UPDATE, (d) 비상태 컬럼 UPDATE(트리거 미발동)를 롤백 트랜잭션 안에서 전/후 비교.

## 5. 초안 SQL v1.1 (미적용)
```sql
create or replace function public.fn_completion_gate_observe() returns trigger
language plpgsql security definer set search_path = public as $$
declare
  v_c1 boolean; v_cass_total int;
  v_ev bigint; v_ver text; v_vtype text; v_found boolean := false; v_class text;
  v_hard text[] := '{}'; v_rev text[] := '{}';
begin
  begin
    select exists(select 1 from magi_task_events e where e.task_id = NEW.task_id
      and e.actor not in ('GEOMNURI_PATROL','마기(자동)','COMPLETION_GATE')
      and e.event_type ~* '(EVIDENCE|VERIFICATION|VALIDATION|REVALIDATION|IMPLEMENTATION)'
      and e.event_type !~* '(REQUEST|REQUIRE)') into v_c1;
    select count(*) into v_cass_total from magi_task_events e
      where e.task_id = NEW.task_id and e.actor ilike 'CASSANDRA%';
    select e.event_id, upper(e.payload->>'verdict'), e.event_type into v_ev, v_ver, v_vtype
      from magi_task_events e
      where e.task_id = NEW.task_id and e.actor ilike 'CASSANDRA%' and e.payload ? 'verdict'
      order by e.event_id desc limit 1;
    v_found := found;

    if v_cass_total = 0 then
      v_hard := v_hard || 'NO_CASSANDRA_RECORD'; v_class := 'NONE';
    elsif not v_found then
      v_rev := v_rev || 'LEGACY_UNSTRUCTURED_ONLY'; v_class := 'LEGACY';
    elsif v_ver ~ '^(PARTIAL|HOLD|FAIL|CONDITIONAL|REJECT)' then
      v_hard := v_hard || 'LATEST_VERDICT_NOT_PASS'; v_class := 'NEG';
    elsif v_ver ~ '^PASS(_[A-Z]+)?$' then
      if v_ver ~ 'DESIGN' or v_vtype ~ 'DESIGN' then
        v_rev := v_rev || 'DESIGN_PASS_ONLY'; v_class := 'PASS_DESIGN';
      else v_class := 'PASS_FINAL'; end if;
    else
      v_hard := v_hard || 'LATEST_VERDICT_NOT_PASS'; v_class := 'UNKNOWN';
    end if;
    if not v_c1 then v_rev := v_rev || 'EVIDENCE_NOT_RECOGNIZED'; end if;

    if array_length(v_hard,1) > 0 or array_length(v_rev,1) > 0 then
      insert into magi_task_events(task_id, event_type, actor, detail, payload)
      values (NEW.task_id, 'COMPLETION_GATE_VIOLATION', 'COMPLETION_GATE',
        '완료 게이트 관찰: 검증 기록 점검 필요 ('||array_to_string(v_hard||v_rev, ',')||'). 관찰 모드로 전이는 차단하지 않음.',
        jsonb_build_object('mode','observe',
          'gate_level', case when array_length(v_hard,1) > 0 then 'WARN' else 'REVIEW' end,
          'reasons', v_hard||v_rev,'from_status',OLD.status,'to_status',NEW.status,'task_priority',NEW.priority,
          'last_verdict', v_ver,'last_verdict_event_id', v_ev,'verdict_class', v_class,
          'checks', jsonb_build_object('evidence_event',v_c1,'cassandra_events',v_cass_total)));
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
### 6.1 이미 실행한 읽기 전용 골든테스트 (2026-10-12, 분류식을 SELECT로 실행, 운영 무변경)
| 케이스 | 입력 | 기대 | 결과 |
|---|---|---|---|
| G1 | verdict=PARTIAL_PASS | NEG | 일치 |
| G2 | verdict=FAIL_DESIGN | NEG | 일치 |
| G3 | verdict=HOLD | NEG | 일치 |
| G4 | verdict=PASS (CASSANDRA_VALIDATION) | PASS_FINAL | 일치 |
| G5 | verdict=PASS_DESIGN | PASS_DESIGN | 일치 |
| G6 | verdict=pass(소문자) | PASS_FINAL | 일치 |
| G7 | verdict=MAYBE | UNKNOWN | 일치 |
| G8 | 판정값 없음(과거 본문) | LEGACY | 일치 |
| G9 | actor=TAEO가 verdict=PASS를 기재 | IGNORED(카산드라로 불인정) | 일치 |
| G10 | verdict=PASS, event_type=CASSANDRA_DESIGN_VALIDATION | PASS_DESIGN | 일치 |
증거 이벤트 타입 14종(허용 6: EVIDENCE, MAGI_EVIDENCE, EVIDENCE_SUBMITTED, IMPLEMENTATION_CI_PASS, VERIFICATION_COMPLETED, CASSANDRA_VALIDATION / 불허 8: VERIFICATION_REQUESTED, IMPLEMENTATION_REQUESTED, VERIFICATION_REQUEST, CASSANDRA_VALIDATION_REQUESTED, CASSANDRA_EVIDENCE_REQUEST, DESIGN_V2_EVIDENCE_REQUIREMENTS, CASSANDRA_REVALIDATION_REQUESTED, IMPLEMENTATION_PATCH_COMMITTED_REVALIDATION_REQUIRED): 14/14 기대와 일치. 카산드라가 지적한 두 반례(`PARTIAL PASS / 전체 HOLD`, `FAIL — PASS 조건 미충족`)는 본문을 읽지 않으므로 구조화 판정이 NEG이면 NEG, 판정값이 없으면 LEGACY로만 분류된다.

### 6.2 카산드라 설계 PASS 후 실행(롤백 드라이런, 단일 DO 블록 안에서 DDL+시험 후 강제 롤백)
양성: (P1) 카산드라 이벤트 0건 → HARD NO_CASSANDRA_RECORD. (P2) 최신 판정 HOLD → HARD. (P3) PASS 뒤에 HOLD(혼재 이력, 최신 우선) → HARD. (P4) 최신 PASS_DESIGN만 → REVIEW DESIGN_PASS_ONLY. (P5) 과거 본문만 → REVIEW LEGACY_UNSTRUCTURED_ONLY. (P6) 요청 이벤트만 있고 증거 없음 → REVIEW EVIDENCE_NOT_RECOGNIZED. (P7) 판정값 UNKNOWN → HARD.
음성: (N1) 증거 + 최신 PASS_FINAL → 기록 없음. (N2) HOLD 뒤에 PASS_FINAL(최신 우선) → 기록 없음. (N3) vr=false 전이. (N4) status 외 컬럼 UPDATE. (N5) COMPLETED→CLOSED 재전이 비발동. (N6) 재오픈 후 재완료는 다시 발동. (N7) actor=TAEO가 기재한 verdict=PASS는 불인정 → 카산드라 이벤트 0건이면 HARD.
회귀·안전: (R1) 트리거 함수가 예외를 내도 UPDATE 성공(fail-open). (R2) 봇 approve_task PATCH 경로. (R3) 위반 이벤트가 순라 underposting/slack_policy 점검을 유발하지 않음(분류기 함수와 순라 판정식에 payload/detail을 직접 입력). (R4) 3절 백테스트 13건과 결과 일치. (R5) 비용: 트리거는 `idx_magi_task_events_task`(task_id) 조회만 하며 task_id 외 전체 스캔 없음(C2' 제거).
종료증거: 위 시험 결과 이벤트 + 카산드라 독립 PASS. 관찰 2주 후 위반 집계·오탐 검토를 마기에 보고하고, 거부 전환은 별도 설계·승인.

## 7. 미확인/한계
- 봇 외 쓰기 경로(마기·카산드라·영실 커넥터)가 `session_replication_role=replica` 등으로 트리거를 우회할 가능성은 이론상 존재(실사용 증거 없음).
- 판정값 표준 어휘와 카산드라의 기록 관행이 합의되기 전에는 정상 통과 판정이 불가능하다(2.4절).
- 증거 이벤트 타입 정규식은 현 관행 기준이라 새 타입이 생기면 오탐/미탐 가능 → 관찰 기간에 오탐 목록으로 보정.
- 과거 위반(#145, #147, #25) 및 #174 추적성에 대한 소급 기록·정정 여부는 마기·대표님 판단.
- 이 문서는 설계이며 구현·스키마·배포는 (대표 승인 + 마기 배정 + 카산드라 해당 단계 PASS) 전부 충족 전까지 금지.
