-- The redacted + run-length-encoded export query used for the TASK#197 offline replay (read-only SELECT on the MAGI registry; output not committed).
with t as (select unnest(array[25,145,147,160,174,83,157,164,167,170,171,172,175,194,204,205]) as task_id),
ev as (select e.task_id, e.event_id, e.event_type, e.actor, e.payload from magi_task_events e join t using (task_id)),
red as (
 select task_id, event_id, event_type, actor,
  case when payload is null then null
       when jsonb_typeof(payload)='object' then (
         select coalesce(jsonb_object_agg(k,
           case jsonb_typeof(v)
             when 'string' then case when (v #>> '{}') ~ '^[A-Za-z0-9_.:-]{1,40}$' then v else to_jsonb('H:'||left(encode(sha256(convert_to(v::text,'UTF8')),'hex'),12)) end
             when 'number' then v when 'boolean' then v when 'null' then v
             when 'array' then jsonb_build_array('H:'||left(encode(sha256(convert_to(v::text,'UTF8')),'hex'),12))
             else jsonb_build_object('h','H:'||left(encode(sha256(convert_to(v::text,'UTF8')),'hex'),12)) end), '{}'::jsonb)
         from jsonb_each(payload) as x(k,v) where k in ('verdict','stage','tier','evidence_kind','verifier_role'))
       when jsonb_typeof(payload)='string' then to_jsonb('H:'||left(encode(sha256(convert_to(payload::text,'UTF8')),'hex'),12))
       when jsonb_typeof(payload)='array' then jsonb_build_array('H:'||left(encode(sha256(convert_to(payload::text,'UTF8')),'hex'),12))
       else payload end as p
 from ev),
numbered as (select *, row_number() over (order by task_id, event_id) rn from red),
keyed as (select rn, task_id, event_type, actor, p, task_id||'|'||event_type||'|'||coalesce(actor,'<null>')||'|'||coalesce(p::text,'<null>') as line from numbered),
grp as (select *, rn - row_number() over (partition by line order by rn) as g from keyed),
runs as (select min(rn) first_rn, task_id, event_type, actor, p, count(*) n from grp group by line, g, task_id, event_type, actor, p),
types as (select event_type, row_number() over (order by event_type)-1 as i from (select distinct event_type from red) q),
actors as (select actor, row_number() over (order by actor)-1 as i from (select distinct actor from red) q)
select jsonb_build_object(
 'redacted_content_sha256', (select encode(sha256(convert_to(string_agg(line, E'\n' order by rn),'UTF8')),'hex') from keyed),
 'n_events', (select count(*) from keyed), 'n_runs', (select count(*) from runs),
 'types', (select jsonb_agg(event_type order by i) from types),
 'actors', (select jsonb_agg(actor order by i) from actors),
 'runs', (select jsonb_agg(jsonb_build_array(r.task_id, ty.i, ac.i, r.p, r.n) order by r.first_rn) from runs r join types ty using (event_type) left join actors ac on ac.actor is not distinct from r.actor)
) as j;
