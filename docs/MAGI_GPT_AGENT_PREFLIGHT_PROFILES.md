# MAGI GPT Agent Preflight Profiles v1.0

공통 원칙:
- 세션/작업 시작 직후 본 업무보다 먼저 `POST /mcp/agent_context` 호출
- 공식 상태 원장은 Supabase Registry
- Slack은 대표님용 표시계층
- 동일 Task의 owner_agent와 최근 event actor가 다르면 중복수정 전 충돌/인계 여부 확인
- P0/P1, HOLD, BLOCKER, 승인대기 사항을 우선 반영
- Secret/MCP_API_KEY는 문서·대화·Slack에 출력 금지

## CASSANDRA
호출값: `{"agent":"CASSANDRA"}`

추가지침:
외부검증 착수 전에 my_tasks/global_critical/recent_relevant_events를 읽고 현재 검증 Gate와 최근 구현 변경자를 확인한다. 이미 다른 검증이 진행 중이면 중복검증 필요성을 먼저 판단한다.

## ARGOS
호출값: `{"agent":"ARGOS"}`

추가지침:
운행분석 착수 전에 데이터 파이프라인 관련 P0/P1/HOLD와 최근 정정 이벤트를 확인한다. 데이터 정합성 이슈가 열려 있으면 분석결과를 확정값으로 취급하지 않는다.

## ATLAS
호출값: `{"agent":"ATLAS"}`

추가지침:
외부조사 착수 전에 관련 정책/로드맵 Task와 최근 변경사항을 확인한다. 이미 진행 중인 동일 조사축이 있으면 중복조사 대신 보완조사로 전환한다.

## YOUNGSIL
호출값: `{"agent":"YOUNGSIL"}`

추가지침:
GPT/연결/운영지원 작업 전 현재 담당 Agent, 최근 변경자, 배포/보안 Blocker를 확인한다. 다른 Agent가 같은 설정·연결·코드를 수정 중이면 먼저 인계 여부를 확인한다.

## ATHENA
호출값: `{"agent":"ATHENA"}`

추가지침:
대표님 일정·브리핑·승인 보좌 전 P0/P1, 승인대기, BLOCKER를 먼저 확인한다. 공식 상태와 개인비서 기억이 충돌하면 Registry를 우선하고 차이를 대표님께 알린다.

## TAEO
호출값: `{"agent":"TAEO"}`

추가지침:
시스템 점검·구축 작업 전 전체 critical Task와 최근 actor를 확인한다. 캐스퍼/영실 등 다른 구현 Agent가 이미 같은 영역을 만지고 있으면 중복변경을 피하고 현재 작업 연속성을 기준으로 역할을 유동 조정한다.
