-- Stand-in for the production tables, mirroring what was measured read-only on 2026-10-13:
-- event_id identity ALWAYS, RLS enabled (not forced) with anon/service policies, owner bypass,
-- domain inheritance trigger, status/priority CHECK constraints.
do $$ begin if not exists (select 1 from pg_roles where rolname='anon') then create role anon; end if; if not exists (select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if; end $$;
create table magi_tasks(task_id bigserial primary key, title text, status text, priority text, verification_required boolean default false, domain text default 'magi',
  constraint magi_tasks_priority_check check (priority = any (array['P0','P1','P2'])),
  constraint magi_tasks_status_check check (status = any (array['CREATED','ASSIGNED','IN_PROGRESS','WAITING','BLOCKED','HANDOFF','REVIEW','VERIFICATION','COMPLETED','HOLD','CANCELLED','CLOSED'])));
create table magi_task_events(event_id bigint generated always as identity primary key, task_id bigint references magi_tasks(task_id), event_type text not null, old_status text, new_status text, actor text, detail text, created_at timestamptz not null default now(), domain text, source_type text, source_id text, payload jsonb);
create index idx_magi_task_events_task on magi_task_events(task_id);
create function fn_inherit_domain_from_task() returns trigger language plpgsql as $f$
begin if NEW.domain is null and NEW.task_id is not null then select t.domain into NEW.domain from public.magi_tasks t where t.task_id = NEW.task_id; end if; return NEW; end $f$;
create trigger trg_magi_task_events_inherit_domain before insert or update of task_id, domain on magi_task_events for each row execute function fn_inherit_domain_from_task();
alter table magi_tasks enable row level security;
alter table magi_task_events enable row level security;
create policy magi_tasks_anon_read on magi_tasks for select to anon using (true);
create policy magi_tasks_anon_update on magi_tasks for update to anon using (true) with check (true);
create policy magi_task_events_anon_read on magi_task_events for select to anon using (true);
create policy magi_task_events_anon_write on magi_task_events for insert to anon with check (true);
grant select, update on magi_tasks to anon;
grant select, insert on magi_task_events to anon;
grant temp on database gate to anon;   -- production anon has TEMP privilege (has_database_privilege('anon', current_database(), 'TEMP') = true)
