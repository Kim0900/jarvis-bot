"""Replay REAL production event histories (exported read-only, payload/actor/event_type only) through the gate.
Never touches production.  Usage: PGHOST=<socket dir> python3 tests/sql/task197_gate_real_replay.py <export.json>
export.json: [{"t": task_id, "e": [{"i": event_id, "ty": event_type, "a": actor, "p": payload}, ...]}, ...]
Prints one line per task: reasons under the current SQL (docs section 5)."""
import json, os, re, subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
PGHOST = os.environ.get("PGHOST", "")
def psql(q, db="gate"):
    cmd = ["psql", "-d", db, "-v", "ON_ERROR_STOP=1", "-tA", "-F", "\t"] + (["-h", PGHOST] if PGHOST else [])
    r = subprocess.run(cmd, input=q, capture_output=True, text=True)
    if r.returncode: raise SystemExit(r.stderr[-800:])
    return r.stdout
lit = lambda v: "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"
data = json.load(open(sys.argv[1], encoding="utf-8"))
psql("drop database if exists gate;", "postgres"); psql("create database gate;", "postgres")
psql((ROOT / "tests/sql/task197_gate_schema.sql").read_text(encoding="utf-8"))
psql(re.search(r"## 5\. .*?```sql\n(.*?)```", (ROOT / "docs/COMPLETION_GATE_DESIGN_V1.md").read_text(encoding="utf-8"), re.S).group(1))
st = ["begin;"]
for t in data:
    st.append(f"insert into magi_tasks(task_id,title,status,priority,verification_required) overriding system value values ({t['t']},'real','VERIFICATION','P1',true);")
    for e in t["e"]:
        pj = "NULL" if e["p"] is None else lit(json.dumps(e["p"], ensure_ascii=False)) + "::jsonb"
        st.append(f"insert into magi_task_events(task_id,event_type,actor,detail,payload) overriding system value values ({t['t']},{lit(e['ty'])},{lit(e['a'])},'x',{pj});" if False else
                  f"insert into magi_task_events(task_id,event_type,actor,detail,payload) values ({t['t']},{lit(e['ty'])},{lit(e['a'])},'x',{pj});")
st.append("update magi_tasks set status='COMPLETED' where verification_required;")
st.append("commit;")
psql("\n".join(st))
out = psql("select task_id, payload->>'gate_level', payload->'reasons', payload->>'last_verdict', payload->>'verdict_class' from magi_task_events where event_type='COMPLETION_GATE_VIOLATION' order by task_id;")
seen = set()
for line in out.splitlines():
    tid, lvl, reasons, lv, cls = line.split("\t"); seen.add(int(tid))
    print(f"#{tid}\t{lvl}\t{reasons}\tclass={cls}\tlast_verdict={lv}")
clean = [t["t"] for t in data if t["t"] not in seen]
print(f"clean (no violation): {clean}")
print(f"summary: {len(data)} tasks, {sum(1 for l in out.splitlines() if l.split(chr(9))[1]=='WARN')} HARD, {sum(1 for l in out.splitlines() if l.split(chr(9))[1]=='REVIEW')} REVIEW, {len(clean)} clean")
