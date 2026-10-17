-- TASK#197 rollback rehearsal: READ-ONLY state snapshot (no writes).  Run from an OBSERVER session before and after the rehearsal.
--   INV_*  : invariants.  Must be identical before/after AND (for gate_*/fixture_*/rehearsal_*) zero.  A change here is a failure.
--   OBS_*  : observational only (shared database, legitimate writers, sequence gaps, pg_stat/WAL).  Never a mutation proof either way.
-- Temp relations of OTHER connections are deliberately not counted (they are not this trial's leftovers); only this session's own
-- temp schema is checked, and the rehearsal's S1 fixture table lives in the rehearsal session, which is gone after ROLLBACK.
select 'INV_gate_functions'      k, count(*)::text v from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname = 'fn_completion_gate_observe'
union all select 'INV_gate_triggers',        count(*)::text from pg_trigger where tgname like '%completion_gate%' and not tgisinternal
union all select 'INV_tasks_triggers',       coalesce(string_agg(tgname, ',' order by tgname), '') from pg_trigger where tgrelid = 'public.magi_tasks'::regclass and not tgisinternal
union all select 'INV_events_triggers',      coalesce(string_agg(tgname, ',' order by tgname), '') from pg_trigger where tgrelid = 'public.magi_task_events'::regclass and not tgisinternal
union all select 'INV_public_function_count', count(*)::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public'
union all select 'INV_fixture_tasks',        count(*)::text from public.magi_tasks where task_id >= 9000000
union all select 'INV_fixture_events',       count(*)::text from public.magi_task_events where event_id >= 9000000000
union all select 'INV_gate_events',          count(*)::text from public.magi_task_events where event_type = 'COMPLETION_GATE_VIOLATION' or actor = 'COMPLETION_GATE'
union all select 'INV_rehearsal_correction_log', count(*)::text from public.correction_log where table_name = 'magi_tasks' and (old_value like '%rehearsal%' or row_id >= 9000000)
union all select 'INV_own_temp_relations',   count(*)::text from pg_class where relpersistence = 't' and relnamespace = pg_my_temp_schema()
union all select 'OBS_tasks_rows_max_id',    count(*)::text || '/' || coalesce(max(task_id), 0)::text from public.magi_tasks
union all select 'OBS_events_rows_max_id',   count(*)::text || '/' || coalesce(max(event_id), 0)::text from public.magi_task_events
union all select 'OBS_tasks_seq_last',       coalesce(last_value, 0)::text from pg_sequences where schemaname = 'public' and sequencename = 'magi_tasks_task_id_seq'
union all select 'OBS_event_seq_last',       coalesce(last_value, 0)::text from pg_sequences where schemaname = 'public' and sequencename = 'magi_task_events_event_id_seq'
union all select 'OBS_rehearsal_sessions',   count(*)::text from pg_stat_activity where application_name = 'task197_rehearsal' and pid <> pg_backend_pid()
union all select 'OBS_exclusive_lockers',    count(*)::text from pg_locks l where l.relation in ('public.magi_tasks'::regclass, 'public.magi_task_events'::regclass) and l.pid <> pg_backend_pid() and l.mode in ('AccessExclusiveLock', 'ShareRowExclusiveLock')
order by 1;
