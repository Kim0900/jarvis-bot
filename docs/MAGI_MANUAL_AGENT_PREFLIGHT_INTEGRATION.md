# MAGI Manual GPT Agent Preflight Integration v1.0

목적: CASSANDRA / ARGOS / ATLAS / YOUNGSIL / ATHENA / TAEO가 세션 또는 작업 시작 시
공식 Supabase Registry 상태를 먼저 읽고, 담당 중첩·P0/P1·HOLD/BLOCKER·최근 변경자를 확인하도록 한다.

## 공식 조회 경로

- Base URL: `https://jarvis-bot-gsez.onrender.com`
- Endpoint: `POST /mcp/agent_context`
- Auth header: `X-MCP-Key: <MCP_API_KEY>`
- Body:
  ```json
  {"agent":"CASSANDRA"}
  ```

지원 agent 값:
- CASPER
- CASSANDRA
- ARGOS
- ATLAS
- YOUNGSIL
- ATHENA
- TAEO
- MAGI

## 시작 규칙

각 GPT는 작업 시작 시 반드시 agent_context를 먼저 호출한다.
응답에서 아래를 확인한 뒤 본 작업을 시작한다.

1. my_tasks
2. global_critical
3. recent_relevant_events
4. 동일 Task의 owner_agent / support_agents
5. 최근 event actor와 변경시각
6. HOLD / BLOCKER / 승인대기 여부

같은 Task를 다른 Agent가 최근 변경 중이면 중복수정 전에 충돌 또는 인계 여부를 확인한다.

Slack은 대표님용 표시 계층이다.
공식 상태 원장은 Supabase의 magi_tasks / magi_task_events다.

## GPT Action용 최소 OpenAPI 예시

```yaml
openapi: 3.1.0
info:
  title: MAGI Agent Context
  version: 1.0.0
servers:
  - url: https://jarvis-bot-gsez.onrender.com
paths:
  /mcp/agent_context:
    post:
      operationId: getAgentContext
      summary: Read current MAGI registry context for an agent
      parameters:
        - in: header
          name: X-MCP-Key
          required: true
          schema:
            type: string
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              properties:
                agent:
                  type: string
              required: [agent]
      responses:
        '200':
          description: Agent context
          content:
            application/json:
              schema:
                type: object
```

## 에이전트별 호출값

- CASSANDRA → `{"agent":"CASSANDRA"}`
- ARGOS → `{"agent":"ARGOS"}`
- ATLAS → `{"agent":"ATLAS"}`
- YOUNGSIL → `{"agent":"YOUNGSIL"}`
- ATHENA → `{"agent":"ATHENA"}`
- TAEO → `{"agent":"TAEO"}`

## 보안

- MCP_API_KEY 자체를 문서/Slack/Registry에 기록하지 않는다.
- GPT Action 인증 설정에서만 Secret으로 등록한다.
- Slack에는 토큰·비밀키·개인정보를 게시하지 않는다.
