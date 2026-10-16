-- Task #197 | READ-ONLY operational prevalidation snapshot
-- Run only using psql -X -v ON_ERROR_STOP=1 -f tests/sql/task197_prevalidation_readonly.sql
-- This file DOES NOT create or alter objects, tasks, events, policies or privileges.
-- It is NOT the operating DDL/ROLLBACK rehearsal required by CASSANDRA #5060/#5261.
BEGIN;
SET TRANSACTION READ ONLY;
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '8s';

-- A. Baseline: gate installation and required physical index.
SELECT
  current_database() AS database_name,
  current_setting('server_version') AS postgres_version,
  to_regprocedure('public.fn_completion_gate_observe()') IS NOT NULL AS gate_function_installed,
  EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'public.magi_tasks'::regclass
      AND NOT tgisinternal AND tgname ILIKE '%completion%gate%'
  ) AS gate_trigger_installed,
  EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'public' AND tablename = 'magi_task_events'
      AND indexname = 'idx_magi_task_events_task'
  ) AS events_task_index_present;

-- B. Live snapshot: NEVER equate "no CASSANDRA event" with an invalid close.
-- #200 tier routing is not implemented; LOW/MEDIUM MAGI exceptions must be reviewed.
WITH completed_verification_tasks AS (
  SELECT task_id, status, verification_status, verified_by
  FROM public.magi_tasks
  WHERE verification_required IS TRUE
    AND status IN ('COMPLETED', 'CLOSED')
)
SELECT
  t.task_id, t.status, t.verification_status,
  COALESCE(c.cass_event_count, 0) AS cassandra_event_count,
  COALESCE(c.evidence_submitted_count, 0) AS evidence_submitted_count
FROM completed_verification_tasks t
LEFT JOIN LATERAL (
  SELECT
    count(*) FILTER (WHERE actor ILIKE 'CASSANDRA%') AS cass_event_count,
    count(*) FILTER (WHERE event_type = 'EVIDENCE_SUBMITTED') AS evidence_submitted_count
  FROM public.magi_task_events e
  WHERE e.task_id = t.task_id
) c ON TRUE
ORDER BY t.task_id;

-- C. Summary; no static 13-row assumption. Retest after each new closure.
WITH verification_tasks AS (
 SELECT t.task_id,
        EXISTS (SELECT 1 FROM public.magi_task_events e
                WHERE e.task_id=t.task_id AND e.actor ILIKE 'CASSANDRA%') AS has_cassandra
 FROM public.magi_tasks t
 WHERE t.verification_required IS TRUE AND t.status IN ('COMPLETED','CLOSED')
)
SELECT
 count(*) AS closed_verification_required,
 count(*) FILTER (WHERE NOT has_cassandra) AS no_cassandra_events,
 array_agg(task_id ORDER BY task_id) FILTER (WHERE NOT has_cassandra) AS check_policy_exceptions
FROM verification_tasks;

-- D. Security precondition. Positive privileges mean observational, NOT tamper-proof.
SELECT
  has_table_privilege('anon', 'public.magi_task_events', 'INSERT') AS anon_events_insert,
  has_table_privilege('anon', 'public.magi_tasks', 'UPDATE') AS anon_tasks_update,
  (SELECT relrowsecurity FROM pg_class WHERE oid='public.magi_task_events'::regclass) AS events_rls_enabled,
  (SELECT relrowsecurity FROM pg_class WHERE oid='public.magi_tasks'::regclass) AS tasks_rls_enabled,
  (SELECT rolbypassrls FROM pg_roles WHERE rolname='postgres') AS postgres_bypasses_rls;

-- Deliberate explicit rollback, even though the transaction was read-only.
ROLLBACK;
