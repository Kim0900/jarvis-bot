"""Shared helpers for the TASK#197 rehearsal self-tests (throwaway server only).

Self-test of the production ROLLBACK-ONLY rehearsal (tests/sql/task197_prod_rollback_rehearsal.sql) on a THROWAWAY server.

    PGHOST=<socket dir of a server you control> PGSUPER=supabase_admin python3 tests/sql/task197_rehearsal_selftest.py

It builds a production-shaped database (tests/sql/task197_prod_like_schema.sql), seeds history shaped like the section 3 backtest,
then proves properties of the harness itself: it passes, leaves no residue (schema dump, table contents, triggers, temp objects,
locks), survives injected faults / killed clients / transaction wrappers, aborts on its guards, bounds its lock impact, never leaks a
NOTIFY, and FAILS when the gate SQL is mutated.  Never point this at production.
"""
import hashlib, os, re, signal, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "sql"))
import task197_rollback_rehearsal_build as B  # noqa: E402

SUP = os.environ.get("PGSUPER", "supabase_admin")
HARNESS = (ROOT / "tests/sql/task197_prod_rollback_rehearsal.sql").read_text(encoding="utf-8")
SNAPSHOT = (ROOT / "tests/sql/task197_prod_rollback_snapshot.sql").read_text(encoding="utf-8")
SENT = re.compile(r"REHEARSAL_ROLLBACK_SENTINEL result=(\S+) checks=(\d+)/(\d+) scenarios=(\d+)/(\d+) replay_ok=(\d+) replay_drift=(\d+) "
                  r"hold_ms=(\d+) total_ms=(\d+) max_gate_update_ms=(\d+) lock_timeout_ms=(\d+) lock_timeout_caller_ms=(\d+) statement_timeout_ms=(\d+) "
                  r"event_seq_consumed=(-?\d+) gate_sql_md5=(\w+)")
results = []

def check(name, ok, info=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else "")); sys.stdout.flush()

def psql(db, sql=None, user="postgres", args=(), env=None, timeout=120, file=None):
    cmd = ["psql", "-X", "-q", "-d", db, "-U", user, "-tA"] + list(args) + (["-f", file] if file else [])
    e = dict(os.environ, **(env or {}))
    # the script goes in through a file handle on stdin (same as a pipe for psql): subprocess.run(input=...) raises
    # "ValueError: I/O operation on closed file" in CPython 3.12 when the child exits before reading, which hid the real psql error in CI
    import tempfile
    with tempfile.TemporaryFile("w+", encoding="utf-8") as fin:
        if sql is not None: fin.write(sql); fin.flush(); fin.seek(0)
        r = subprocess.run(cmd, stdin=fin if sql is not None else subprocess.DEVNULL, capture_output=True, text=True, env=e, timeout=timeout)
    return r.returncode, r.stdout, r.stderr

def sup(db, sql): 
    rc, o, e = psql(db, sql, user=SUP, args=("-v", "ON_ERROR_STOP=1"))
    if rc: raise SystemExit("SQL ERROR: " + e[-600:])
    return o.strip()

def clone(name):
    sup("postgres", f"drop database if exists {name} with (force); create database {name} template gate_base;")
    return name

def sentinel(err):
    m = SENT.search(err); 
    return None if not m else dict(result=m[1], ok=int(m[2]), total=int(m[3]), scn_ok=int(m[4]), scn=int(m[5]), rep_ok=int(m[6]), drift=int(m[7]),
                                   hold=int(m[8]), total_ms=int(m[9]), upd=int(m[10]), lt=int(m[11]), lt_caller=int(m[12]), st=int(m[13]), seq=int(m[14]), md5=m[15])

FINGERPRINT = """select md5(string_agg(x, '|' order by x)) from (
  select 'c:' || c.oid::regclass || c.relkind || c.relpersistence || coalesce(c.reloptions::text,'') || coalesce(c.relacl::text,'') || c.relrowsecurity || c.relforcerowsecurity || pg_get_userbyid(c.relowner) x
    from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' or n.nspname like 'pg_temp%'
  union all select 'a:' || a.attrelid::regclass || a.attname || a.atttypid || a.attnotnull || a.attidentity || coalesce(pg_get_expr(d.adbin, d.adrelid), '')
    from pg_attribute a left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum join pg_class c on c.oid = a.attrelid join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and a.attnum > 0 and not a.attisdropped
  union all select 'k:' || conrelid::regclass || conname || pg_get_constraintdef(oid) from pg_constraint where connamespace = 'public'::regnamespace
  union all select 't:' || pg_get_triggerdef(oid) || tgenabled::text from pg_trigger where not tgisinternal
  union all select 'f:' || pg_get_functiondef(p.oid) || coalesce(p.proacl::text,'') from pg_proc p where p.pronamespace = 'public'::regnamespace
  union all select 'p:' || tablename || policyname || cmd || roles::text || coalesce(qual,'') || coalesce(with_check,'') from pg_policies
  union all select 'i:' || indexdef from pg_indexes where schemaname = 'public'
  union all select 'e:' || evtname || evtevent from pg_event_trigger
) q"""

def state(db):
    """(snapshot text, catalog fingerprint, data fingerprint): everything a rolled-back transaction must not change (sequences reported separately)"""
    rc, snap, e = psql(db, None, file=str(ROOT / "tests/sql/task197_prod_rollback_snapshot.sql")); assert rc == 0, e
    rc, fp, e = psql(db, FINGERPRINT); assert rc == 0, e
    rc, data, e = psql(db, "select md5(coalesce((select string_agg(t::text, '|' order by task_id) from magi_tasks t),'')) || md5(coalesce((select string_agg(t::text, '|' order by event_id) from magi_task_events t),'')) || md5(coalesce((select string_agg(t::text, '|' order by id) from correction_log t),''));"); assert rc == 0, e
    return snap.strip(), fp.strip(), data.strip()

def strip_seq(snap): return "\n".join(l for l in snap.splitlines() if l.startswith("INV_"))
def seq_last(snap): return int(re.search(r"^OBS_event_seq_last\|(\d+)$", snap, re.M)[1])

def run(db, text=HARNESS, user="postgres", env=None, args=(), timeout=120):
    rc, o, e = psql(db, text, user=user, env=env, args=args, timeout=timeout)
    return rc, e, sentinel(e)



def ensure_base():
    """(re)create the production-shaped template database gate_base with committed history shaped like the section 3 backtest"""
    # ---------------------------------------------------------------- base database (committed, production-like history)
    sup("postgres", "drop database if exists gate_base with (force); drop database if exists gate with (force); create database gate;")
    rc, o, e = psql("postgres", None, user=SUP, args=("-v", "ON_ERROR_STOP=1"), file=str(ROOT / "tests/sql/task197_prod_like_schema.sql"))
    if rc: raise SystemExit("schema load failed: " + e[-800:])
    def hist(t, rows):
        sup("gate", f"insert into magi_tasks(task_id,title,owner_agent,status,priority,domain,verification_required) overriding system value values ({t},'real {t}','x','COMPLETED','P1','magi',true);")
        for et, actor, payload in rows:
            pj = "NULL" if payload is None else "'" + payload + "'::jsonb"
            sup("gate", f"insert into magi_task_events(task_id,event_type,actor,detail,payload) values ({t},'{et}','{actor}','x',{pj});")
    for t in (25, 145, 147): hist(t, [("TASK_CREATED", "마기", None), ("STATUS_CHANGE", "마기", None), ("MAGI_DECISION", "마기", None)])
    for t in (160, 174): hist(t, [("EVIDENCE", "캐스퍼", None), ("CASSANDRA_VALIDATION", "CASSANDRA", None)])
    for t in (83, 157, 164, 167, 170, 171, 172, 175): hist(t, [("VERIFICATION_REQUESTED", "캐스퍼", None), ("CASSANDRA_VALIDATION", "CASSANDRA", None)])
    for t in (194, 204, 205): hist(t, [("EVIDENCE", "캐스퍼", None), ("CASSANDRA_VALIDATION", "CASSANDRA", '{"verdict":"PASS","stage":"FINAL"}')])
    sup("postgres", "alter database gate rename to gate_base;")

