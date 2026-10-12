# TASK#197 완료 게이트(관찰 모드) 설계 v1 (캐스퍼, 2026-10-12 KST)

상태: 설계 단계. DB·코드·배포 변경 없음. 운영 적용은 카산드라 설계검증 PASS 이후.
범위(마기 지시): verification_required=true 태스크가 증거 이벤트·카산드라 PASS 없이 COMPLETED/CLOSED로 전이되면 위반 이벤트를 기록한다. 1차는 관찰(경고만), 거부 전환은 2주 관찰 후 별도 승인.

## 1. 실측
- magi_tasks에는 상태 전이를 검사·기록하는 트리거가 없다(트리거: delete_log, require_domain_on_insert뿐). 완료 전이는 에이전트의 직접 SQL, 봇 `approve_task`(PATCH), 마기(자동) 검증(bot_v5_legacy.py L7187) 등 여러 경로로 일어난다.
- 이벤트 기반 탐지는 불가: 완료 처리된 verification_required 태스크 13건 중 new_status=COMPLETED/CLOSED 이벤트가 남은 것은 3건뿐이고, 이벤트 타입도 TASK_COMPLETED/STATUS_CHANGE/TASK_VERIFIED/TASK_UPDATED로 제각각이다(전체 91건 완료/종결, 그중 verification_required=true는 13건, 전체 vr=true 43건).
- evidence_registry 테이블은 위 13건 중 단 한 건도 쓰이지 않는다(0행). 실제 증거 기록 관행은 magi_task_events의 *_EVIDENCE / *_VALIDATION / VERIFICATION_REQUEST 이벤트다.
- → 모든 쓰기 경로를 포괄하려면 magi_tasks 행 단위 DB 트리거가 유일한 지점이다(service_role도 트리거는 실행됨).

## 2. 판정 규칙 (관찰 모드)
대상: `NEW.verification_required IS TRUE AND NEW.status IN ('COMPLETED','CLOSED') AND OLD.status NOT IN ('COMPLETED','CLOSED')`.
- C1 증거 이벤트: 같은 태스크에 actor가 GEOMNURI_PATROL·마기(자동)·COMPLETION_GATE가 아닌 이벤트 중 event_type이 `(EVIDENCE|VERIFICATION|VALIDATION|REVALIDATION|IMPLEMENTATION)`에 매칭하는 것이 1건 이상.
- C2 독립검증: 같은 태스크에 actor가 `CASSANDRA%`이고 detail에 단어 PASS(PARTIAL PASS 단독 제외)가 있는 이벤트.
- C2'(참고): 다른 태스크에 올린 카산드라 이벤트가 `(task|#|태스크) <id> ... PASS`로 이 태스크를 지목 → `PASS_FILED_ON_OTHER_TASK`(위반 아님, 추적성 경고).
- reasons: `NO_EVIDENCE_EVENT`, `NO_CASSANDRA_PASS`, `PASS_FILED_ON_OTHER_TASK`. reasons가 비면 기록 없음.
- 기록: magi_task_events에 event_type=`COMPLETION_GATE_VIOLATION`, actor=`COMPLETION_GATE`, old/new_status는 NULL(완료 이벤트로 오집계 방지), payload에 reasons·checks·전이 전후 상태·mode='observe'.
- 항상 fail-open: 트리거 내부 오류는 EXCEPTION으로 삼키고 RAISE WARNING만, 기존 UPDATE는 절대 실패시키지 않는다. 거부 모드는 이 설계 범위 밖.

## 3. 백테스트 (read-only, 2026-10-12 현재 완료 vr=true 13건)
| 판정 | 건수 | 태스크 |
|---|---|---|
| OK | 9 | 83, 157, 160, 164, 167, 170, 171, 172, 175 |
| REVIEW(PASS가 다른 태스크에 기재) | 1 | 174 — 카산드라 "Task 174 technical PASS"가 #176 이벤트(4302) 안에 있고 #174 스레드에는 PASS 이벤트 없음. verified_by 공란 |
| PARTIAL(카산드라 PASS 없음, 마기 VERIFIED) | 1 | 25 (2026-08-17, 정책 이전) |
| VIOLATION(증거·PASS 모두 없음) | 2 | 145, 147 (2026-09-26) |
- 규칙은 알려진 위반 3건(145, 147, 25)을 잡고 정상 9건을 통과시킨다. #174는 실질 검증은 있었으나 추적성 결함 → REVIEW로 분류되는 것이 의도된 동작.
- 한계: PASS 판정은 detail 텍스트 정규식이라 휴리스틱이다. actor 필드는 쓰기 권한자가 임의로 채울 수 있어 이 게이트는 "누락 탐지"이지 "위조 방지"가 아니다.

## 4. 기존 경로·순라 회귀 방지
- 순라 `control_classifier_underposting`(bot_v5_legacy.py L6344~)은 payload의 severity/level/priority 또는 detail의 `🔴`, `[CRITICAL]`, `P0 `, `P1 ` 문자열이 있는데 `_classify_control_event`가 non_control_event로 분류하면 CRITICAL 경보를 낸다(현재 이벤트 4811로 이미 발동 중). → 위반 이벤트의 payload는 severity/level/priority 키를 쓰지 않고(`gate_level` 사용), detail에 위 문자열을 넣지 않는다. 태스크 우선순위는 payload.task_priority로만 기록.
- 신규 event_type은 현 분류기에서 `non_control_event`(Slack 게시 후보 아님)로 분류된다. Slack 게시가 필요하면 `_CONTROL_ALWAYS_MEANINGFUL_TYPES` 추가가 별도 코드 변경·배포라서 이번 범위 제외(관찰기간 중 SQL로 집계).
- 이벤트 insert는 magi_tasks를 건드리지 않아 재귀 없음. `fn_inherit_domain_from_task`가 domain을 채운다.
- 쓰기 경로 회귀: (a) 봇 PATCH `approve_task`, (b) 마기(자동) COMPLETED(vr=false → 대상 아님), (c) 에이전트 직접 SQL UPDATE, (d) title 등 비상태 컬럼 UPDATE(트리거 미발동)를 롤백 트랜잭션 안에서 전/후 비교.

## 5. 초안 SQL (미적용)
```sql
create or replace function public.fn_completion_gate_observe() returns trigger
language plpgsql security definer set search_path = public as $$
declare v_c1 boolean; v_c2 boolean; v_c2o boolean; v_reasons text[] := '{}';
begin
  begin
    select exists(select 1 from magi_task_events e where e.task_id = NEW.task_id
      and e.actor not in ('GEOMNURI_PATROL','마기(자동)','COMPLETION_GATE')
      and e.event_type ~* '(EVIDENCE|VERIFICATION|VALIDATION|REVALIDATION|IMPLEMENTATION)') into v_c1;
    select exists(select 1 from magi_task_events e where e.task_id = NEW.task_id
      and e.actor ilike 'CASSANDRA%' and e.detail ~* '\mPASS\M' and e.detail !~* '^\s*(PARTIAL|HOLD|FAIL)') into v_c2;
    select exists(select 1 from magi_task_events e where e.task_id <> NEW.task_id
      and e.actor ilike 'CASSANDRA%'
      and e.detail ~* ('(task|#|태스크) ?' || NEW.task_id || '\M[^.;]{0,40}PASS')) into v_c2o;
    if not v_c1 then v_reasons := v_reasons || 'NO_EVIDENCE_EVENT'; end if;
    if not v_c2 then
      v_reasons := v_reasons || case when v_c2o then 'PASS_FILED_ON_OTHER_TASK' else 'NO_CASSANDRA_PASS' end;
    end if;
    if array_length(v_reasons,1) > 0 then
      insert into magi_task_events(task_id, event_type, actor, detail, payload)
      values (NEW.task_id, 'COMPLETION_GATE_VIOLATION', 'COMPLETION_GATE',
        '완료 게이트 관찰: 증거/독립검증 기록 미충족 ('||array_to_string(v_reasons, ',')||'). 관찰 모드로 전이는 차단하지 않음.',
        jsonb_build_object('mode','observe','gate_level','WARN','reasons',v_reasons,
          'from_status',OLD.status,'to_status',NEW.status,'task_priority',NEW.priority,
          'checks',jsonb_build_object('evidence_event',v_c1,'cassandra_pass_same_task',v_c2,'cassandra_pass_other_task',v_c2o)));
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

## 6. 시험 계획 (카산드라 PASS 후, 단일 DO 블록 안에서 DDL+시험 후 강제 롤백으로 운영 무변경 드라이런)
양성: (P1) 증거·PASS 없는 vr=true 전이 → 위반 1건, reasons=2개. (P2) 증거만 있고 PASS 없음 → NO_CASSANDRA_PASS. (P3) 카산드라 PARTIAL만 → 위반. (P4) 다른 태스크에 PASS 기재 → PASS_FILED_ON_OTHER_TASK.
음성: (N1) 증거+같은 태스크 PASS → 기록 없음. (N2) vr=false 전이. (N3) status 외 컬럼 UPDATE. (N4) COMPLETED→CLOSED 재전이 비발동. (N5) 재오픈 후 재완료는 다시 발동.
회귀·안전: (R1) 트리거 함수가 예외를 내도 UPDATE 성공(fail-open). (R2) 봇 approve_task PATCH 경로 동작. (R3) 위반 이벤트가 순라 underposting/slack_policy 점검을 유발하지 않음(분류기 함수에 payload/detail 입력 시험). (R4) 백테스트 13건 결과와 일치. (R5) 비용: `idx_magi_task_events_task`(task_id) 인덱스 존재 확인됨 → C1·C2는 인덱스 조회. C2'만 `task_id <> NEW.task_id` 전체 스캔이므로 이벤트 수 증가 시 비용을 측정하고, 필요하면 C2'를 관찰 보고서(배치 SQL)로 이동.
종료증거: 위 시험 결과 이벤트 + 카산드라 독립 PASS. 관찰 2주 후 위반 집계·오탐 검토를 마기에 보고하고, 거부 전환은 별도 설계·승인.

## 7. 미확인/한계
- 봇 외 쓰기 경로(마기·카산드라·영실 커넥터)가 `session_replication_role=replica` 등으로 트리거를 우회할 가능성은 이론상 존재(실사용 증거 없음).
- 증거 이벤트 타입 정규식은 현 관행 기준이라 새 타입이 생기면 오탐/미탐 가능 → 관찰 기간에 오탐 목록으로 보정.
- 과거 위반(#145, #147, #25, #174 추적성)에 대한 소급 기록·정정 여부는 마기·대표님 판단.
