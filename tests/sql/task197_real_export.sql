-- TASK#197 P1-E: read-only export of the REDACTED real replay histories (run on the MAGI registry; SELECT only).  The output is NOT committed.
-- Replay tasks: the 16 verification_required tasks the rehearsal harness replays.  Kept: event order, event_type, actor, and the payload keys the gate reads
-- (verdict, stage, tier, evidence_kind, verifier_role) - values that are not short tokens become 'H:<12 hex of sha256>'.  Dropped: detail, timestamps, raw ids, other keys.
-- raw_sha256 is the commitment over the UNREDACTED rows (anyone with registry access can recompute it); the redacted commitment is recomputed by
-- tests/sql/task197_real_replay_offline.py from what it loads.
with t as (select unnest(array[25,145,147,160,174,83,157,164,167,170,171,172,175,194,204,205]) as task_id),
ev as (select e.task_id, e.event_id, e.event_type, e.actor, e.payload from magi_task_events e join t using (task_id))
select count(*) as n_events,
       (select max(event_id) from magi_task_events) as as_of_max_event_id,
       encode(sha256(convert_to(string_agg(task_id||'|'||event_id||'|'||event_type||'|'||coalesce(actor,'<null>')||'|'||coalesce(payload::text,'<null>'), E'\n' order by task_id, event_id),'UTF8')),'hex') as raw_sha256
from ev;
-- (the redacted, run-length-encoded export used for the offline replay is produced by the query recorded in evidence_task197/real_replay_export_query.sql)
