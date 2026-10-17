-- TASK#197 rollback-rehearsal self-test: a THROWAWAY database shaped like the production facts measured read-only on 2026-10-17
-- (Supabase PostgreSQL 17.6; local self-test runs on whatever server the tester controls).  Never run against production.
--  * postgres is NOT a superuser (rolsuper=false, rolbypassrls=true, member of anon/authenticated/service_role); it owns both tables.
--  * magi_tasks / magi_task_events: RLS on (not forced); anon read/update (tasks) and read/insert (events) with `true` policies;
--    anon has table-level INSERT incl. event_id/actor; anon TEMP privilege; anon statement_timeout=3s.
--  * identity ALWAYS on task_id/event_id; existing triggers: delete log (SECURITY DEFINER, writes correction_log), domain required on
--    task insert, domain inheritance on events; FK events.task_id -> tasks.task_id, tasks.parent_task_id -> tasks.
--  * event triggers pgrst_ddl_watch (NOTIFY pgrst) / pgrst_drop_watch like Supabase; supabase_realtime publication with no tables.
do $r$ begin
  if not exists (select 1 from pg_roles where rolname = 'postgres') then create role postgres nosuperuser login createrole createdb bypassrls; end if;
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticator') then create role authenticator noinherit login; end if;
end $r$;
grant anon, authenticated, service_role to postgres;
alter role anon set statement_timeout = '3s';
alter role authenticated set statement_timeout = '8s';
grant all on database gate to postgres;
grant temp, connect on database gate to anon, authenticated, service_role;
\connect gate
create publication supabase_realtime;
grant create, usage on schema public to postgres;
grant usage on schema public to anon, authenticated, service_role;
set role postgres;
create table public.magi_tasks(
  task_id bigint generated always as identity primary key,
  title text not null, owner_agent text not null, status text not null default 'CREATED', priority text,
  task_type text not null default 'SIMPLE', domain text, verification_required boolean not null default false,
  architect_decision_required boolean not null default false, orch_attempts int not null default 0, review_attempts int not null default 0,
  parent_task_id bigint references public.magi_tasks(task_id),
  created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  constraint magi_tasks_priority_check check (priority = any (array['P0','P1','P2'])),
  constraint magi_tasks_task_type_check check (task_type = any (array['SIMPLE','COMPLEX','NOTIFICATION'])),
  constraint magi_tasks_status_check check (status = any (array['CREATED','ASSIGNED','IN_PROGRESS','WAITING','BLOCKED','HANDOFF','REVIEW','VERIFICATION','COMPLETED','HOLD','CANCELLED','CLOSED'])));
create table public.magi_task_events(
  event_id bigint generated always as identity primary key, task_id bigint references public.magi_tasks(task_id),
  event_type text not null, old_status text, new_status text, actor text, detail text, created_at timestamptz not null default now(),
  domain text, source_type text, source_id text, payload jsonb);
create index idx_magi_task_events_task on public.magi_task_events(task_id);
create table public.correction_log(id bigint generated always as identity primary key, table_name text, row_id bigint, field_changed text, old_value text, new_value text, changed_by text, reason text, domain text);
create function public.fn_inherit_domain_from_task() returns trigger language plpgsql as $f$
begin if NEW.domain is null and NEW.task_id is not null then select t.domain into NEW.domain from public.magi_tasks t where t.task_id = NEW.task_id; end if; return NEW; end $f$;
create function public.fn_require_magi_task_domain_on_insert() returns trigger language plpgsql set search_path to '' as $f$
begin if new.domain is null or btrim(new.domain) = '' then raise exception 'magi_tasks.domain is required for new task inserts' using errcode='23514'; end if; return new; end $f$;
create function public.fn_log_delete_to_correction_log() returns trigger language plpgsql security definer as $f$
begin insert into public.correction_log(table_name,row_id,field_changed,old_value,new_value,changed_by,reason,domain) values (TG_TABLE_NAME, coalesce((to_jsonb(OLD)->>'task_id')::bigint,0),'DELETE(전체행)',to_jsonb(OLD)::text,'(삭제됨)','x','x',nullif(to_jsonb(OLD)->>'domain','')); return OLD; end $f$;
create trigger trg_magi_task_events_inherit_domain before insert or update of task_id, domain on public.magi_task_events for each row execute function public.fn_inherit_domain_from_task();
create trigger trg_require_magi_task_domain_on_insert before insert on public.magi_tasks for each row execute function public.fn_require_magi_task_domain_on_insert();
create trigger trg_magi_tasks_delete_log before delete on public.magi_tasks for each row execute function public.fn_log_delete_to_correction_log();
alter table public.magi_tasks enable row level security; alter table public.magi_task_events enable row level security;
create policy magi_tasks_anon_read on public.magi_tasks for select to anon using (true);
create policy magi_tasks_anon_update on public.magi_tasks for update to anon using (true) with check (true);
create policy magi_tasks_read_only on public.magi_tasks for select to authenticated using (true);
create policy magi_tasks_service_all on public.magi_tasks for all to service_role using (true) with check (true);
create policy magi_task_events_anon_read on public.magi_task_events for select to anon using (true);
create policy magi_task_events_anon_write on public.magi_task_events for insert to anon with check (true);
create policy magi_task_events_read_only on public.magi_task_events for select to authenticated using (true);
create policy magi_task_events_service_all on public.magi_task_events for all to service_role using (true) with check (true);
grant all on public.magi_tasks, public.magi_task_events to anon, authenticated, service_role;
reset role;
-- Supabase-style event triggers (superuser-owned, fire inside the DDL transaction)
set role supabase_admin;
create function public.pgrst_ddl_watch() returns event_trigger language plpgsql as $f$ begin notify pgrst, 'reload schema'; end $f$;
create function public.pgrst_drop_watch() returns event_trigger language plpgsql as $f$ begin notify pgrst, 'reload schema'; end $f$;
create event trigger pgrst_ddl_watch on ddl_command_end execute function public.pgrst_ddl_watch();
create event trigger pgrst_drop_watch on sql_drop execute function public.pgrst_drop_watch();
-- representative history so counts/sequences are non-trivial
insert into public.magi_tasks(title,owner_agent,status,priority,domain,verification_required) overriding system value values ('seed','x','CREATED','P1','magi',false);
