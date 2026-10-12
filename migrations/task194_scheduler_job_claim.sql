-- Task #194: atomic per-day scheduler claims; review before production migration.
create table if not exists public.scheduler_job_runs (
  job_name text not null,
  run_date date not null,
  status text not null check (status in ('RUNNING','SUCCEEDED','FAILED','UNCERTAIN')),
  claim_token uuid not null,
  claimed_at timestamptz not null default now(),
  completed_at timestamptz,
  result text,
  primary key (job_name, run_date)
);
revoke all on public.scheduler_job_runs from anon, authenticated;
create or replace function public.claim_scheduler_job(p_job_name text, p_run_date date, p_token uuid)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if auth.role() <> 'service_role' then raise exception 'service_role required'; end if;
  insert into public.scheduler_job_runs(job_name,run_date,status,claim_token)
  values (p_job_name,p_run_date,'RUNNING',p_token)
  on conflict do nothing;
  return found;
end; $$;
create or replace function public.finish_scheduler_job(p_job_name text,p_run_date date,p_token uuid,p_status text,p_result text)
returns boolean language plpgsql security definer set search_path = public, pg_temp as $$
begin
  if auth.role() <> 'service_role' then raise exception 'service_role required'; end if;
  if p_status not in ('SUCCEEDED','FAILED','UNCERTAIN') then raise exception 'invalid status'; end if;
  update public.scheduler_job_runs set status=p_status,completed_at=now(),result=p_result
  where job_name=p_job_name and run_date=p_run_date and claim_token=p_token and status='RUNNING';
  return found;
end; $$;
revoke all on function public.claim_scheduler_job(text,date,uuid) from public,anon,authenticated;
revoke all on function public.finish_scheduler_job(text,date,uuid,text,text) from public,anon,authenticated;
grant execute on function public.claim_scheduler_job(text,date,uuid) to service_role;
grant execute on function public.finish_scheduler_job(text,date,uuid,text,text) to service_role;
