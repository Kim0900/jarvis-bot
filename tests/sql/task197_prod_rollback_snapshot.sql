-- TASK#197 rollback rehearsal: READ-ONLY state snapshot.  Run once BEFORE and once AFTER the rehearsal; the two outputs must be
-- identical except `event_seq_last` (sequences are the only thing a rolled-back transaction cannot restore) and rows that other
-- writers legitimately added in between (compare max ids/counts with that in mind).  Contains no writes.
select 'gate_functions'      k, count(*)::text v from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public' and p.proname = 'fn_completion_gate_observe'
union all select 'gate_triggers',        count(*)::text from pg_trigger where tgname like '%completion_gate%' and not tgisinternal
union all select 'tasks_triggers',       coalesce(string_agg(tgname, ',' order by tgname), '') from pg_trigger where tgrelid = 'public.magi_tasks'::regclass and not tgisinternal
union all select 'events_triggers',      coalesce(string_agg(tgname, ',' order by tgname), '') from pg_trigger where tgrelid = 'public.magi_task_events'::regclass and not tgisinternal
union all select 'public_function_count', count(*)::text from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = 'public'
union all select 'fixture_tasks',        count(*)::text from public.magi_tasks where task_id >= 9000000
union all select 'fixture_events',       count(*)::text from public.magi_task_events where event_id >= 9000000000
union all select 'gate_events',          count(*)::text from public.magi_task_events where event_type = 'COMPLETION_GATE_VIOLATION' or actor = 'COMPLETION_GATE'
union all select 'rehearsal_correction_log', count(*)::text from public.correction_log where table_name = 'magi_tasks' and old_value like '%rehearsal%'
union all select 'temp_relations',       count(*)::text from pg_class where relpersistence = 't'
union all select 'tasks_rows_max_id',    count(*)::text || '/' || coalesce(max(task_id), 0)::text from public.magi_tasks
union all select 'events_rows_max_id',   count(*)::text || '/' || coalesce(max(event_id), 0)::text from public.magi_task_events
union all select 'tasks_seq_last',       coalesce(last_value, 0)::text from pg_sequences where schemaname = 'public' and sequencename = 'magi_tasks_task_id_seq'
union all select 'event_seq_last',       coalesce(last_value, 0)::text from pg_sequences where schemaname = 'public' and sequencename = 'magi_task_events_event_id_seq'
union all select 'locks_on_tasks_by_others', count(*)::text from pg_locks l where l.relation in ('public.magi_tasks'::regclass, 'public.magi_task_events'::regclass) and l.pid <> pg_backend_pid() and l.mode in ('AccessExclusiveLock', 'ShareRowExclusiveLock')
order by 1;
