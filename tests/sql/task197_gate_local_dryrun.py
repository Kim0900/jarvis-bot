"""TASK#197 completion gate: local dry-run of the draft trigger on a THROWAWAY PostgreSQL.

Never point this at production.  Usage (needs a local server you control):
    PGHOST=/path/to/socket_dir PGUSER=postgres python3 tests/sql/task197_gate_local_dryrun.py
It creates a database named ``gate`` with a minimal stand-in schema (tests/sql/task197_gate_schema.sql),
extracts the SQL block from docs/COMPLETION_GATE_DESIGN_V1.md section 5, installs it and replays the
positive / negative / regression scenarios of section 6.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PGHOST = os.environ.get("PGHOST", "")
PSQL = os.environ.get("PSQL", "psql")


def sql(q, db="gate"):
    cmd = [PSQL, "-d", db, "-v", "ON_ERROR_STOP=1", "-tA"] + (["-h", PGHOST] if PGHOST else [])
    r = subprocess.run(cmd, input=q, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("SQL ERROR: " + r.stderr[-800:])
    return r.stdout.strip(), r.stderr.strip()


doc = (ROOT / "docs" / "COMPLETION_GATE_DESIGN_V1.md").read_text(encoding="utf-8")
gate_sql = re.search(r"## 5\. .*?```sql\n(.*?)```", doc, re.S).group(1)
sql("drop database if exists gate;", "postgres")
sql("create database gate;", "postgres")
sql((ROOT / "tests" / "sql" / "task197_gate_schema.sql").read_text(encoding="utf-8"))
sql(gate_sql)
print("function + trigger created OK")

def scenario(name, vr, events, new_status="COMPLETED", start_status="VERIFICATION", extra_sql="", expect=None, cols_only=False):
    out,_=sql(f"insert into magi_tasks(title,status,priority,verification_required) values ('{name}','{start_status}','P1',{str(vr).lower()}) returning task_id;")
    tid=int(out.split()[0])
    for et,actor,payload in events:
        p=json.dumps(payload,ensure_ascii=False).replace("'","''") if payload is not None else None
        sql(f"insert into magi_task_events(task_id,event_type,actor,detail,payload) values ({tid},'{et}','{actor}','x',{('%r'%p).replace(chr(34),chr(39)) if False else ('NULL' if p is None else chr(39)+p+chr(39)+'::jsonb')});")
    if extra_sql: sql(extra_sql.replace("{tid}",str(tid)))
    upd = new_status
    sql(f"update magi_tasks set status='{upd}' where task_id={tid};")
    res,_=sql(f"select coalesce(string_agg(payload->>'gate_level'||':'||(payload->'reasons')::text, ' | '),'(none)') from magi_task_events where task_id={tid} and event_type='COMPLETION_GATE_VIOLATION';")
    cnt,_=sql(f"select count(*) from magi_task_events where task_id={tid} and event_type='COMPLETION_GATE_VIOLATION';")
    return tid,res,int(cnt)

results=[]
def check(name, got, want):
    ok = want in got
    results.append(ok)
    print(("PASS " if ok else "FAIL ")+name+" -> "+got+("" if ok else "   [expected contains: "+want+"]"))

EV=lambda payload={"verdict":"PASS","stage":"FINAL"}: ("CASSANDRA_VALIDATION","CASSANDRA",payload)
EVID=("EVIDENCE","캐스퍼",None)
# positive
t,r,c=scenario("P1 nothing",True,[]);                                          check("P1 no evidence no cassandra",r,'WARN:["NO_CASSANDRA_RECORD", "EVIDENCE_NOT_RECOGNIZED"]')
t,r,c=scenario("P2 latest HOLD",True,[EVID,EV({"verdict":"HOLD","stage":"FINAL"})]); check("P2 latest HOLD",r,'WARN:["LATEST_VERDICT_NOT_PASS"]')
t,r,c=scenario("P3 PASS then FAIL",True,[EVID,EV(),EV({"verdict":"FAIL","stage":"FINAL"})]); check("P3 PASS then FAIL",r,'WARN:["LATEST_VERDICT_NOT_PASS"]')
t,r,c=scenario("P4 design pass only",True,[EVID,("CASSANDRA_DESIGN_VALIDATION","CASSANDRA",{"verdict":"PASS","stage":"DESIGN"})]); check("P4 design pass only",r,'REVIEW:["DESIGN_PASS_ONLY"]')
t,r,c=scenario("P5 legacy only",True,[EVID,("CASSANDRA_VALIDATION","CASSANDRA",None)]); check("P5 legacy unstructured (NULL payload)",r,'REVIEW:["LATEST_VERDICT_UNSTRUCTURED"]')
t,r,c=scenario("P6 requests only",True,[("VERIFICATION_REQUESTED","캐스퍼",None),("IMPLEMENTATION_REQUESTED","마기",None),EV()]); check("P6 request events not evidence",r,'REVIEW:["EVIDENCE_NOT_RECOGNIZED"]')
t,r,c=scenario("P7 unknown verdict",True,[EVID,EV({"verdict":"MAYBE","stage":"FINAL"})]); check("P7 unknown verdict",r,'WARN:["LATEST_VERDICT_NOT_PASS"]')
t,r,c=scenario("P8 pass then latest unstructured (99001)",True,[EVID,EV(),("CASSANDRA_VALIDATION","CASSANDRA",{"audit":"x"})]); check("P8 latest unstructured overrides earlier PASS",r,'REVIEW:["LATEST_VERDICT_UNSTRUCTURED"]')
t,r,c=scenario("P9 verifier self-evidence (99011)",True,[EV()]); check("P9 verifier event is not implementation evidence",r,'REVIEW:["EVIDENCE_NOT_RECOGNIZED"]')
t,r,c=scenario("P10 stage missing",True,[EVID,EV({"verdict":"PASS"})]); check("P10 PASS without stage",r,'REVIEW:["PASS_STAGE_NOT_FINAL"]')
t,r,c=scenario("P11 secondary only",True,[EVID,("MAGI_DECISION","마기",{"verdict":"PASS","stage":"FINAL","verifier_role":"SECONDARY","tier":"LOW"})]); check("P11 secondary never lowers HARD",r,'WARN:["NO_CASSANDRA_RECORD"]')
t,r,c=scenario("P12 non-cassandra PASS",True,[EVID,("CASSANDRA_VALIDATION","TAEO",{"verdict":"PASS","stage":"FINAL"})]); check("P12 PASS by other actor ignored",r,'WARN:["NO_CASSANDRA_RECORD"]')
t,r,c=scenario("P13 evidence_kind path",True,[("MAGI_DECISION","마기",{"evidence_kind":"TEST"}),EV()],); check("P13 evidence_kind accepted -> clean",r,'(none)')
# negative
t,r,c=scenario("N1 clean",True,[EVID,EV()]);                                   check("N1 evidence + PASS/FINAL",r,'(none)')
t,r,c=scenario("N2 HOLD then PASS",True,[EVID,EV({"verdict":"HOLD","stage":"FINAL"}),EV()]); check("N2 latest wins",r,'(none)')
t,r,c=scenario("N3 vr false",False,[]);                                         check("N3 verification_required=false",r,'(none)')
t,r,c=scenario("N4 non-status update",True,[],new_status="VERIFICATION");       check("N4 non-completing update",r,'(none)')
t,r,c=scenario("N5 non judgment after PASS (99015)",True,[EVID,EV(),("AGREEMENT_RESPONSE","CASSANDRA",{"agreement":"CONDITIONAL_AGREE"})]); check("N5 agreement event does not override",r,'(none)')
# N6: COMPLETED -> CLOSED does not fire again; reopen then complete fires again
out,_=sql("insert into magi_tasks(title,status,priority,verification_required) values ('N6','VERIFICATION','P1',true) returning task_id;"); tid=int(out.split()[0])
sql(f"update magi_tasks set status='COMPLETED' where task_id={tid}; update magi_tasks set status='CLOSED' where task_id={tid};")
c1,_=sql(f"select count(*) from magi_task_events where task_id={tid} and event_type='COMPLETION_GATE_VIOLATION'")
sql(f"update magi_tasks set status='IN_PROGRESS' where task_id={tid}; update magi_tasks set status='COMPLETED' where task_id={tid};")
c2,_=sql(f"select count(*) from magi_task_events where task_id={tid} and event_type='COMPLETION_GATE_VIOLATION'")
ok=(c1=="1" and c2=="2"); results.append(ok); print(("PASS " if ok else "FAIL ")+f"N6/N7 COMPLETED->CLOSED not re-fired ({c1}); reopen+complete fires again ({c2})")
# regression: fail-open
sql("alter table magi_task_events add constraint no_gate check (event_type <> 'COMPLETION_GATE_VIOLATION') not valid;")
out,_=sql("insert into magi_tasks(title,status,priority,verification_required) values ('R1','VERIFICATION','P1',true) returning task_id;"); tid=int(out.split()[0])
o,e=sql(f"update magi_tasks set status='COMPLETED' where task_id={tid} returning status;")
ok=("COMPLETED" in o and "completion_gate_observe failed" in e); results.append(ok); print(("PASS " if ok else "FAIL ")+"R1 fail-open: UPDATE succeeds, warning emitted -> "+o+" / "+e[:90])
sql("alter table magi_task_events drop constraint no_gate;")
# payload shape: no patrol keys, no marker strings
row,_=sql("select count(*) from magi_task_events where event_type='COMPLETION_GATE_VIOLATION' and (payload ? 'severity' or payload ? 'level' or payload ? 'priority' or detail ~ '🔴|\\[CRITICAL\\]|P0 |P1 |P0급|P0격상')")
ok=(row=="0"); results.append(ok); print(("PASS " if ok else "FAIL ")+f"R3 violation events carry no patrol trigger keys/markers (count={row})")
anon,_=sql("select has_function_privilege('anon','fn_completion_gate_observe()','execute')")
ok=(anon=="f"); results.append(ok); print(("PASS " if ok else "FAIL ")+f"anon cannot execute function (has_function_privilege={anon})")
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
