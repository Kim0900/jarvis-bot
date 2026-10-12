create table magi_tasks(task_id bigserial primary key, title text, status text, priority text, verification_required boolean default false);
create table magi_task_events(event_id bigserial primary key, task_id bigint, event_type text, old_status text, new_status text, actor text, detail text, created_at timestamptz default now(), domain text, source_type text, source_id text, payload jsonb);
create index idx_magi_task_events_task on magi_task_events(task_id);
do $$ begin if not exists (select 1 from pg_roles where rolname='anon') then create role anon; end if; if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if; end $$;
