"""Self-test of the production ROLLBACK-ONLY rehearsal harness (tests/sql/task197_prod_rollback_rehearsal.sql) on a THROWAWAY server.

    PGHOST=<socket dir of a server you control> PGSUPER=supabase_admin python3 tests/sql/task197_rehearsal_selftest.py

See task197_pgtest_lib.py for the production-shaped database.  Never point this at production.
"""
import hashlib, os, re, signal, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from task197_pgtest_lib import *   # noqa: F401,F403
import task197_pgtest_lib as L
B = L.B

ensure_base()

# ---------------------------------------------------------------- T1 builder / lint
check("T1a committed .sql equals a fresh build (no stale artifact)", B.build() == HARNESS)
check("T1b header md5 equals md5 of docs section 5", f"gate SQL md5 (docs section 5, embedded verbatim): {hashlib.md5(B.gate_sql().encode()).hexdigest()}" in HARNESS)
outside = re.sub(r"\$gate\$.*?\$gate\$", "", HARNESS, flags=re.S)
outside = re.sub(r"\$scn\$.*?\$scn\$", "", outside, flags=re.S)
forbidden = r"\b(commit|savepoint|release|nextval|setval|truncate|vacuum|concurrently|dblink|notify|pg_notify|copy|alter\s+system|create\s+extension|grant|alter\s+role|pg_advisory\w*|pg_terminate_backend|pg_cancel_backend|delete\s+from)\b"
hits = sorted(set(m.lower() for m in re.findall(forbidden, re.sub(r"--[^\n]*", "", outside), re.I)))
check("T1c harness body has no commit/savepoint/sequence/notify/grant/delete or other non-transactional step", not hits, str(hits))
gate = B.gate_sql()
gh = sorted(set(m.lower() for m in re.findall(r"\b(commit|nextval|setval|truncate|vacuum|concurrently|dblink|notify|pg_notify|copy|grant|delete\s+from|set\s+session|set\s+role)\b", re.sub(r"--[^\n]*", "", gate), re.I)))
check("T1d embedded gate SQL has none of them either", not gh, str(gh))
tail = HARNESS[HARNESS.rindex("  v_total_ms :="):]
check("T1e the statement ends with the unconditional RAISE EXCEPTION and nothing after it", re.fullmatch(r"  v_total_ms :=[^;]*;\n  -- UNCONDITIONAL.*?\n  raise exception using .*?;\nend \$rehearsal\$;\n", tail, re.S) is not None)
check("T1f exactly one statement (one DO block)", len(re.findall(r"^do \$rehearsal\$", HARNESS, re.M)) == 1 and HARNESS.rstrip().endswith("end $rehearsal$;"))

# ---------------------------------------------------------------- T2 happy path, repeatability, residue
db = clone("t_happy")
s0 = state(db); rc, err, r = run(db); s1 = state(db)
check("T2a harness passes on a production-shaped database", r and r["result"] == "PASS" and r["ok"] == r["total"] and r["scn_ok"] == r["scn"] and r["drift"] == 0 and r["rep_ok"] == 13,
      str(r) if not (r and r["result"] == "PASS") else f"checks {r['ok']}/{r['total']}, scenarios {r['scn_ok']}/{r['scn']}, replay_ok {r['rep_ok']}, hold {r['hold']} ms, total {r['total_ms']} ms, event_seq {r['seq']}")
check("T2b the run ends with the sentinel ERROR (rollback by exception)", "ERROR:  REHEARSAL_ROLLBACK_SENTINEL" in err and "ROLLED_BACK_BY_EXCEPTION" in err)
check("T2c schema dump identical before/after", s0[1] == s1[1])
check("T2d table contents identical before/after (tasks, events, correction_log)", s0[2] == s1[2])
check("T2e snapshot identical except the event sequence", strip_seq(s0[0]) == strip_seq(s1[0]), "\n".join(set(strip_seq(s0[0]).splitlines()) ^ set(strip_seq(s1[0]).splitlines())))
check("T2f the only residue is the event identity sequence, exactly as reported", seq_last(s1[0]) - seq_last(s0[0]) == r["seq"] and r["seq"] > 0, f"consumed {r['seq']}")
check("T2g magi_tasks identity sequence untouched (fixtures use explicit ids)", re.search(r"OBS_tasks_seq_last\|(\d+)", s0[0])[1] == re.search(r"OBS_tasks_seq_last\|(\d+)", s1[0])[1])
rc, err2, r2 = run(db); s2 = state(db)
check("T2h second run gives the same verdict and consumes the same sequence amount", r2 and r2["result"] == "PASS" and r2["seq"] == r["seq"] and s2[1] == s0[1] and s2[2] == s0[2])
check("T2i harness reports the embedded gate md5", r["md5"] == hashlib.md5(gate.encode()).hexdigest())

# ---------------------------------------------------------------- T3 wrappers / client modes
for label, text, args in [("explicit BEGIN ... COMMIT around it", "begin;\n" + HARNESS + "\ncommit;\n", ()),
                          ("psql --single-transaction", HARNESS, ("--single-transaction",)),
                          ("psql -c (one simple-query message)", None, ("-c", HARNESS)),
                          ("ON_ERROR_STOP off (statements after the error still run)", HARNESS + "\nselect 'after error';\n", ()),
                          ("pipelined with a trailing COMMIT-like noise", HARNESS + "\ncommit;\ncommit;\n", ())]:
    db = clone("t_mode"); s0 = state(db); rc, err, r = run(db, text, args=args); s1 = state(db)
    check(f"T3 {label}: sentinel returned, no residue", r and r["result"] == "PASS" and s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0]))

# ---------------------------------------------------------------- T4 faults
def fault(name, inject, expect_fatal, db_name="t_fault"):
    db = clone(db_name); s0 = state(db)
    text = HARNESS.replace("    execute v_gate;\n", "    execute v_gate;\n" + inject, 1); assert text != HARNESS
    rc, err, r = run(db, text); s1 = state(db)
    clean = s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0])
    check(f"T4 {name}: reported as FAIL with the cause, still rolled back, no residue", r and r["result"] == "FAIL" and expect_fatal in err and clean, (err.splitlines() or [''])[0][:160])
fault("division by zero right after the DDL", "    perform 1/0;\n", "FATAL=22012")
fault("integrity violation after fixtures are inserted", "    insert into public.magi_task_events(event_id,task_id,event_type) overriding system value values (9000000001, 1, 'X'); insert into public.magi_task_events(event_id,task_id,event_type) overriding system value values (9000000001, 1, 'X');\n", "FATAL=23505")
fault("permission failure mid-way (SET ROLE to a role it may not assume)", "    set local role authenticator;\n", "FATAL=")
# killed backend and killed client
db = clone("t_kill"); s0 = state(db)
text = HARNESS.replace("    execute v_gate;\n", "    execute v_gate;\n    perform pg_sleep(30);\n", 1)
p = subprocess.Popen(["psql", "-X", "-q", "-d", db, "-U", "postgres"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ, PGAPPNAME="rehearsal_kill"))
p.stdin.write(text); p.stdin.close(); time.sleep(2.0)
during = psql(db, "select count(*) from pg_locks l join pg_stat_activity a on a.pid=l.pid where a.application_name='rehearsal_kill' and l.mode='AccessExclusiveLock'", user=SUP)[1].strip()
psql(db, "select pg_terminate_backend(pid) from pg_stat_activity where application_name='rehearsal_kill'", user=SUP)
try: p.wait(10)
except Exception: p.kill()
time.sleep(0.5); s1 = state(db)
locks = psql(db, "select count(*) from pg_locks l where l.relation in ('public.magi_tasks'::regclass,'public.magi_task_events'::regclass) and l.mode in ('AccessExclusiveLock','ShareRowExclusiveLock')", user=SUP)[1].strip()
check("T4 backend terminated mid-run: no residue, no lock left", int(during) >= 1 and locks == "0" and s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0]), f"locks while running={during}, after={locks}")
db = clone("t_kill2"); s0 = state(db)
p = subprocess.Popen(["psql", "-X", "-q", "-d", db, "-U", "postgres"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ, PGAPPNAME="rehearsal_kill2"))
p.stdin.write(text.replace("pg_sleep(30)", "pg_sleep(3)")); p.stdin.close(); time.sleep(1.0); p.send_signal(signal.SIGKILL); p.wait()
time.sleep(5.0); s1 = state(db)
locks = psql(db, "select count(*) from pg_locks l where l.relation in ('public.magi_tasks'::regclass,'public.magi_task_events'::regclass) and l.mode in ('AccessExclusiveLock','ShareRowExclusiveLock')", user=SUP)[1].strip()
check("T4 client killed (SIGKILL) mid-run: server finishes/aborts, no residue, no lock left", locks == "0" and s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0]), f"locks={locks}")
db = clone("t_timeout"); s0 = state(db); t0 = time.time()
rc, err, r = run(db, HARNESS.replace("    execute v_gate;\n", "    execute v_gate;\n    perform pg_sleep(8);\n", 1), env={"PGOPTIONS": "-c statement_timeout=3000"}, timeout=60); dt = time.time() - t0; s1 = state(db)
check("T4 session statement_timeout cancels a runaway run, no residue, no lock left", 2.5 < dt < 6 and "statement timeout" in err and s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0]), f"{dt:.1f}s")

# ---------------------------------------------------------------- T5 guards (abort BEFORE any DDL)
def guard(name, prep, want, user="postgres", env=None, args=()):
    db = clone("t_guard"); prep(db); s0 = state(db); rc, err, _ = run(db, user=user, env=env, args=args); s1 = state(db)
    check(f"T5 guard '{name}' aborts before DDL, no change", "REHEARSAL_ABORTED_BEFORE_DDL: " + want in err and s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0]), (err.splitlines() or [''])[0][:140])
guard("gate already installed", lambda d: sup(d, B.gate_sql()), "gate objects already exist")
guard("fixture task id range occupied", lambda d: sup(d, "insert into magi_tasks(task_id,title,owner_agent,status,priority,domain) overriding system value values (9000005,'x','x','CREATED','P1','magi');"), "fixture id range is occupied")
guard("fixture event id range occupied", lambda d: sup(d, "insert into magi_task_events(event_id,event_type) overriding system value values (9000000007,'x');"), "fixture id range is occupied")
guard("read-only transaction", lambda d: None, "transaction is read only", env={"PGOPTIONS": "-c default_transaction_read_only=on"})
guard("role cannot SET ROLE anon", lambda d: None, "current_user authenticator cannot SET ROLE anon", user="authenticator")
db = clone("t_guard2"); s0 = state(db)
p = subprocess.Popen(["psql", "-X", "-q", "-d", db, "-U", "postgres", "-c", "begin; select pg_sleep(16);"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
time.sleep(12.0); rc, err, _ = run(db); p.wait(); s1 = state(db)
check("T5 guard 'another session holds a transaction older than 10 s' aborts before DDL", "REHEARSAL_ABORTED_BEFORE_DDL: 1 session(s) hold a transaction older than 10 s" in err and s0[1] == s1[1] and s0[2] == s1[2])

# ---------------------------------------------------------------- T6 lock behaviour
db = clone("t_lock"); s0 = state(db)
hold = subprocess.Popen(["psql", "-X", "-q", "-d", db, "-U", "postgres", "-c", "begin; lock table magi_tasks in row exclusive mode; select pg_sleep(5);"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
time.sleep(0.8)
t0 = time.time(); h = subprocess.Popen(["psql", "-X", "-q", "-d", db, "-U", "postgres"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True); h.stdin.write(HARNESS); h.stdin.close()
time.sleep(0.05); t1 = time.time(); rd = subprocess.run(["psql", "-X", "-q", "-d", db, "-U", "postgres", "-tA", "-c", "select count(*) from magi_tasks"], capture_output=True, text=True); reader_wait = time.time() - t1
herr = h.stderr.read(); h.wait(); harness_wait = time.time() - t0; hold.wait(); s1 = state(db)
check("T6a busy table: harness gives up after its 250 ms lock_timeout, reports it, no residue", "FATAL=55P03" in herr and "result=FAIL" in herr and 0.15 < harness_wait < 1.2 and s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0]), f"{harness_wait:.2f}s")
check("T6b a reader queued behind the waiting harness is delayed at most about the 250 ms lock_timeout", rd.returncode == 0 and reader_wait < 0.8, f"reader waited {reader_wait:.2f}s")
open("/tmp/_upd197.sql", "w").write("update magi_tasks set updated_at = now() where task_id = 25;\nselect count(*) from magi_tasks;\n")
worst, holds, bad_runs, nclient, succ, misses = 0.0, [], 0, 0, 0, 0
for i in range(8):   # default deadlock_timeout (1 s): a lock-upgrade queue would show up as a ~1 s stall in some of the runs
    db = clone("t_conc")
    pb = subprocess.Popen(["pgbench", "-n", "-f", "/tmp/_upd197.sql", "-c", "3", "-T", "4", "-l", "--log-prefix=/tmp/_pgb197", "-U", "postgres", db], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.5); rc, err, r = run(db); out, perr = pb.communicate()
    mx = 0.0
    for f in Path("/tmp").glob("_pgb197*"):
        for line in f.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3: mx = max(mx, int(parts[2]) / 1000); nclient += 1
        f.unlink()
    worst = max(worst, mx); holds.append(r["hold"] if r else -1)
    ok_run = r is not None and r["result"] == "PASS"
    lock_miss = r is not None and "55P03" in err and r["result"] == "FAIL"
    succ += ok_run; misses += lock_miss
    if pb.returncode != 0 or re.search(r"failed transactions: 0 ", out + perr) is None or not (ok_run or lock_miss): bad_runs += 1
check("T6c 8 runs against 3 concurrent writer/reader clients: no client error, every run is either PASS or a clean 55P03 give-up, worst client stall stays near lock_timeout + hold",
      bad_runs == 0 and worst < 250 + 300 and (max(holds) < 300), f"{succ} PASS, {misses} lock give-ups, hold {min(holds)}-{max(holds)} ms, worst client latency {worst:.0f} ms over {nclient} client txns")

# ---------------------------------------------------------------- T7 NOTIFY must not leak
def listen_run(do_run):
    lp = subprocess.Popen(["psql", "-X", "-d", "t_notify", "-U", "postgres"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lp.stdin.write("listen pgrst;\nselect pg_sleep(4);\nselect 1;\n"); lp.stdin.close()
    time.sleep(1.0); do_run(); o, e = lp.communicate(); return o + e
clone("t_notify")
leak = listen_run(lambda: run("t_notify"))
ctrl = listen_run(lambda: psql("t_notify", "create table zz197(i int); drop table zz197;"))
check("T7 no PostgREST schema-reload NOTIFY escapes the rolled-back DDL (control: a committed DDL does notify)", "pgrst" not in leak and "pgrst" in ctrl, "control saw it" if "pgrst" in ctrl else "control did not see a notification: test invalid")

# ---------------------------------------------------------------- T8 mutants must be caught
MUT = [("search_path without pg_temp and unqualified table", [("set search_path = pg_catalog, public, pg_temp", "set search_path = public"), ("from public.magi_task_events e where e.task_id = NEW.task_id\n      and e.actor not in", "from magi_task_events e where e.task_id = NEW.task_id\n      and e.actor not in")]),
       ("INSERT trigger never fires", [("when (NEW.verification_required is true and NEW.status in ('COMPLETED','CLOSED'))", "when (false)")]),
       ("final-validation allowlist replaced by substring", [("upper(v_jtype) = any (v_final_types)", "upper(v_jtype) ~ 'VALIDATION'")]),
       ("revocation gate removed", [("elsif upper(v_jtype) ~ v_revoke then", "elsif false then")]),
       ("CASSANDRA actors allowed as implementation evidence", [("      and e.actor not ilike 'CASSANDRA%'\n      and e.event_type !~*", "      and e.event_type !~*")]),
       ("JSON object check removed", [("jsonb_typeof(e.payload) = 'object' and ", "")]),
       ("output sanitizing removed", [("nullif(left(regexp_replace(coalesce(v_stage,''), '[^A-Z0-9_]', '', 'g'), 32), '')", "v_stage")]),
       ("fail-open handler narrowed (errors escape)", [("exception when others then\n    raise warning", "exception when no_data_found then\n    raise warning")]),
       ("transition filter drops OLD.status check (re-fires)", [("\n      and OLD.status not in ('COMPLETED','CLOSED')", "")]),
       ("vr-cleared rule removed", [("if NEW.verification_required is not true then v_rev", "if false then v_rev")]),
       ("EXECUTE left granted (revoke removed)", [("revoke all on function public.fn_completion_gate_observe() from public, anon, authenticated;", "")]),
       ("later-event rule removed", [("if v_later then v_rev", "if false then v_rev")]),
       ("newer-evidence rule removed", [("if v_evid_max is not null and v_evid_max > v_jid then", "if false then")]),
       ("neutral list widened to everything", [("upper(e.event_type) <> all (v_neutral_types)", "true = false")])]
for name, subs in MUT:
    g = B.gate_sql()
    for old, new in subs:
        assert old in g, f"mutation anchor missing: {old[:60]!r}"
        g = g.replace(old, new, 1)
    db = clone("t_mut"); rc, err, r = run(db, B.build(g))
    failing = re.findall(r"^(\S[^\n]*?) got=", err, re.M)
    check(f"T8 mutant '{name}' is detected", r is not None and r["result"] in ("FAIL",), "fails: " + "; ".join(x[:28] for x in failing[:4]) + (" FATAL" if "FATAL=" in err else ""))

# ---------------------------------------------------------------- T9 replay drift is visible
db = clone("t_drift"); sup(db, "insert into magi_task_events(task_id,event_type,actor,detail,payload) values (160,'CASSANDRA_VALIDATION','CASSANDRA','x','{\"verdict\":\"PASS\",\"stage\":\"FINAL\"}'::jsonb);")
rc, err, r = run(db)
check("T9 a changed real history shows up as REPLAY DRIFT (not hidden, not a harness FAIL)", r and r["result"] == "REPLAY_DRIFT" and r["drift"] == 1 and "REPLAY#160" in err, str(r and r["result"]))

# ---------------------------------------------------------------- T10 patrol regression (design 6.2 R3): feed the produced events to the real classifier
import ast, json
tree = ast.parse((ROOT / "bot_v5_legacy.py").read_text(encoding="utf-8"))
wanted = {"_CONTROL_ALWAYS_MEANINGFUL_TYPES", "_CONTROL_ALWAYS_NOISE_TYPES", "_classify_control_event"}
nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in wanted) or (isinstance(n, ast.Assign) and any(isinstance(x, ast.Name) and x.id in wanted for x in n.targets))]
ns = {}; exec(compile(ast.Module(body=nodes, type_ignores=[]), "bot_v5_legacy.py(extract)", "exec"), ns)
db = clone("t_r3")
variant = HARNESS.replace("  raise exception using errcode = 'P0001',", "  create table public.r3_out as select event_id, event_type, actor, detail, old_status, new_status, payload from public.magi_task_events where event_type = 'COMPLETION_GATE_VIOLATION';\n  return;\n  raise exception using errcode = 'P0001',", 1)
assert variant != HARNESS
rc, o, e = psql(db, variant)   # test-only variant that KEEPS its output table (this run commits; it is a throwaway database)
rows = [json.loads(l) for l in psql(db, "select row_to_json(r) from public.r3_out r order by event_id", user=SUP)[1].splitlines() if l.strip()]
under = []; explicit = 0
for ev in rows:
    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
    severity = str(payload.get("severity") or payload.get("level") or "").upper(); priority = str(payload.get("priority") or "").upper(); du = str(ev.get("detail") or "").upper()
    crit = severity in {"CRITICAL", "ERROR", "HIGH"} or priority in {"P0", "P1"} or any(m in du for m in ("[CRITICAL]", "🔴", "P0 ", "P0급", "P0격상", "P1 "))
    explicit += crit
    meaningful, reason = ns["_classify_control_event"](ev)
    if crit and not meaningful: under.append(ev["event_id"])
    if meaningful: under.append(("meaningful?", ev["event_id"], reason))
check("T10 patrol classifier: violation events are non_control_event and never look explicitly critical (no underposting alert)", len(rows) >= 20 and explicit == 0 and not under, f"{len(rows)} events checked, explicit-critical {explicit}")

# ---------------------------------------------------------------- T11 the harness never loosens a stricter caller-set lock_timeout (spec P0-FIX-02)
for caller, want_eff in [("100ms", 100), ("250ms", 250), ("5s", 250), ("0", 250)]:
    db = clone("t_lt"); rc, err, r = run(db, env={"PGOPTIONS": f"-c lock_timeout={caller}"})
    check(f"T11 caller lock_timeout={caller}: in force inside the harness = {want_eff} ms", r and r["result"] == "PASS" and r["lt"] == want_eff, str(r and (r["lt_caller"], r["lt"])))
db = clone("t_st"); rc, err, r = run(db, env={"PGOPTIONS": "-c statement_timeout=3000"})
check("T11 caller statement_timeout=3000 ms is visible inside the harness and the run still passes", r and r["result"] == "PASS" and r["st"] == 3000, str(r and r["st"]))

print(f"\n{sum(results)}/{len(results)} passed"); sys.exit(0 if all(results) else 1)
