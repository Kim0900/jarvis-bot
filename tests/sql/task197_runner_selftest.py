"""Tests of tests/sql/task197_safe_runner.py (the bounded single-connection driver) against a THROWAWAY PostgreSQL server.

    PGHOST=<socket dir> PGSUPER=supabase_admin python3 tests/sql/task197_runner_selftest.py [--log out.json]

Measured facts are printed as [bracketed] values and also written to --log (lock hold seen from an independent session, timings).
"""
import json, os, signal, subprocess, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from task197_pgtest_lib import *   # noqa: F401,F403
import psycopg

ensure_base()
RUNNER = str(ROOT / "tests/sql/task197_safe_runner.py")
EXPECT = json.loads((ROOT / "tests/sql/task197_prod_rollback_rehearsal.expect.json").read_text())
TMP = Path(tempfile.mkdtemp(prefix="t197runner_"))
LOG = {}

def dsn(db, user="postgres"): return f"host={os.environ['PGHOST']} dbname={db} user={user}"

def variant(name, inject=None, replace=None, append=None):
    """a modified copy of the harness + a matching expect file (so the runner's own pin/lint logic is what is under test)"""
    text = HARNESS
    if inject: text = text.replace("    execute v_gate;\n", "    execute v_gate;\n" + inject, 1)
    if replace: text = text.replace(*replace, 1)
    if append: text += append
    assert text != HARNESS or not (inject or replace or append)
    p = TMP / f"{name}.sql"; p.write_text(text, encoding="utf-8")
    ex = dict(EXPECT, sql_sha256=hashlib.sha256(text.encode()).hexdigest()); e = TMP / f"{name}.expect.json"; e.write_text(json.dumps(ex))
    return str(p), str(e), ex["sql_sha256"]

class LockWatch:
    """independent session polling for ACCESS EXCLUSIVE on magi_tasks: reports when it is first/last seen (seconds from start)"""
    def __init__(self, db):
        self.db, self.t0, self.seen, self.stop = db, time.time(), [], threading.Event()
        self.th = threading.Thread(target=self.loop, daemon=True); self.th.start()
    def loop(self):
        with psycopg.connect(dsn(self.db, SUP), autocommit=True) as c:
            while not self.stop.is_set():
                n = c.execute("select count(*) from pg_locks where relation='public.magi_tasks'::regclass and mode='AccessExclusiveLock' and pid <> pg_backend_pid()").fetchone()[0]
                if n: self.seen.append(time.time() - self.t0)
                time.sleep(0.02)
    def result(self):
        self.stop.set(); self.th.join(3)
        return (round(self.seen[0], 2), round(self.seen[-1], 2), round(self.seen[-1] - self.seen[0], 2)) if self.seen else None

TESTENV = {"TASK197_TEST_MODE": "throwaway-server"}

def runner(db, sha=None, sql=None, expect=None, extra=(), env=None, timeout=90, report="r.json"):
    rp = TMP / report
    cmd = [sys.executable, RUNNER, "--test-mode", "--sha256", sha or EXPECT["sql_sha256"], "--report", str(rp)] + list(extra)
    if sql: cmd += ["--sql", sql]
    if expect: cmd += ["--expect", expect]
    p = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, PGDSN=dsn(db), **TESTENV, **(env or {})), timeout=timeout)
    rep = json.loads(rp.read_text()) if rp.exists() else json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
    if rp.exists(): rp.unlink()
    return p.returncode, rep

def clean(s0, s1): return s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0])
def reasons(rep): return " | ".join(rep.get("reasons", []))[:200]

# ---- R1/R2 happy paths
for mode in ("begin", "session"):
    db = clone("t_run"); s0 = state(db); lw = LockWatch(db); rc, rep = runner(db, extra=["--mode", mode]); hold = lw.result(); s1 = state(db)
    s = rep.get("sentinel", {})
    check(f"R1 mode={mode}: strict PASS, exit 0, limits verified on the same connection, session gone, no residue",
          rc == 0 and rep.get("verdict") == "PASS" and rep["effective_settings_on_rehearsal_connection"]["statement_timeout"] == "3000"
          and rep["effective_settings_on_rehearsal_connection"]["lock_timeout"] == "250" and rep["rehearsal_session_gone_after_s"] < 2 and clean(s0, s1)
          and rep["observed_event_seq_delta_noisy"] == int(s["event_seq_consumed"]),
          f"do_wall {rep.get('do_wall_ms')} ms, ACCESS EXCLUSIVE seen from outside {hold}, gone after {rep.get('rehearsal_session_gone_after_s')} s")
    LOG[f"happy_{mode}"] = dict(do_wall_ms=rep.get("do_wall_ms"), lock_seen=hold, gone_after_s=rep.get("rehearsal_session_gone_after_s"), sentinel=s)

# ---- R3..R6 refusals before any rehearsal connection
db = clone("t_run"); rc, rep = runner(db, sha="0" * 64)
check("R3 wrong pinned hash: refused (exit 2) before any rehearsal connection", rc == 2 and not any("rehearsal connection opened" in e for e in rep.get("events", [])) and "sha256" in reasons(rep), reasons(rep))
p, e, h = variant("tampered", append="-- trailing edit\n")
rc, rep = runner(db, sha=EXPECT["sql_sha256"], sql=p, expect=None)
check("R4 edited artifact against the original pin: refused", rc == 2 and "sha256" in reasons(rep), reasons(rep))
p, e, h = variant("lint", replace=("    execute v_gate;\n", "    execute v_gate;\n    commit;\n"))
rc, rep = runner(db, sha=h, sql=p, expect=e)
check("R5 artifact that contains COMMIT is refused by the lint even when pinned and hash-consistent", rc == 2 and "lint" in reasons(rep) and not any("rehearsal connection opened" in x for x in rep.get("events", [])), reasons(rep))
rc, rep = runner(db, extra=["--lock-timeout-ms", "500"])
check("R5b a looser --lock-timeout-ms than the artifact's cap is refused", rc == 2 and "looser" in reasons(rep), reasons(rep))

# ---- R6 replay drift is a failure, never a pass
db = clone("t_run"); sup(db, "insert into magi_task_events(task_id,event_type,actor,detail,payload) values (160,'CASSANDRA_VALIDATION','CASSANDRA','x','{\"verdict\":\"PASS\",\"stage\":\"FINAL\"}'::jsonb);")
s0 = state(db); rc, rep = runner(db); s1 = state(db)
check("R6 replay drift: exit 2, strict reason recorded, no residue, no retry", rc == 2 and "result=REPLAY_DRIFT" in reasons(rep) and clean(s0, s1) and sum("executing the pinned DO" in x for x in rep["events"]) == 1, reasons(rep))

# ---- R7 busy table: clean give-up after 250 ms, one attempt only
db = clone("t_run"); s0 = state(db)
blocker = subprocess.Popen(["psql", "-X", "-q", "-d", db, "-U", "postgres", "-c", "begin; lock table magi_tasks in row exclusive mode; select pg_sleep(4);"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
time.sleep(0.8); t0 = time.time(); rc, rep = runner(db); dt = time.time() - t0; blocker.wait(); s1 = state(db)
check("R7 busy table: one attempt, gives up at the 250 ms lock_timeout (FATAL 55P03), exit 2, no residue", rc == 2 and "55P03" in rep.get("message", "") and sum("executing the pinned DO" in x for x in rep["events"]) == 1 and clean(s0, s1) and rep["do_wall_ms"] < 1200, f"do_wall {rep.get('do_wall_ms')} ms")

# ---- R8/R9 time limits: server-side statement_timeout and client-side watchdog
sl = "    perform pg_sleep(8);\n"
p, e, h = variant("sleep8", inject=sl)
db = clone("t_run"); s0 = state(db); lw = LockWatch(db); rc, rep = runner(db, sha=h, sql=p, expect=e, extra=["--statement-timeout-ms", "1000"]); hold = lw.result(); s1 = state(db)
check("R8 statement_timeout 1000 ms cuts a runaway DO; lock held ~1 s as seen from outside; exit 2; no residue", rc == 2 and ("57014" in json.dumps(rep) or "timeout" in json.dumps(rep).lower()) and hold and hold[2] < 2.0 and clean(s0, s1), f"lock seen {hold}")
LOG["statement_timeout_1000"] = dict(lock_seen=hold, do_wall_ms=rep.get("do_wall_ms"))
db = clone("t_run"); s0 = state(db); lw = LockWatch(db); rc, rep = runner(db, sha=h, sql=p, expect=e, extra=["--statement-timeout-ms", "60000", "--watchdog-s", "1.0"]); hold = lw.result(); s1 = state(db)
check("R9 client watchdog cancels at 1 s even with a long server limit; exit 2; no residue", rc == 2 and any("WATCHDOG" in x for x in rep["events"]) and hold and hold[2] < 2.2 and clean(s0, s1), f"lock seen {hold}")
LOG["watchdog_1s"] = dict(lock_seen=hold)

# ---- R10..R13 client / network loss.  injected sleep 30 s after the DDL, so the lock is held while the client misbehaves.
p30, e30, h30 = variant("sleep30", inject="    perform pg_sleep(30);\n")
def lossy(mode, stop_signal, idle_ms, st_ms, observe_s, label):
    db = clone("t_run"); s0 = state(db); lw = LockWatch(db)
    env = dict(os.environ, PGDSN=dsn(db), **TESTENV)
    cmd = [sys.executable, RUNNER, "--test-mode", "--sha256", h30, "--sql", p30, "--expect", e30, "--mode", mode, "--statement-timeout-ms", str(st_ms), "--idle-tx-timeout-ms", str(idle_ms), "--watchdog-s", "30"]
    pr = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    kid = None
    for _ in range(100):                                                # wait for the supervisor to start the CHILD (the database client now)
        kids = [x for x in subprocess.run(["ps", "-o", "pid=", "--ppid", str(pr.pid)], capture_output=True, text=True).stdout.split()]
        if kids: kid = int(kids[0]); break
        time.sleep(0.1)
    time.sleep(0.9)                                                     # let the child reach the injected sleep inside the DO
    os.kill(kid, stop_signal)                                          # SIGSTOP = silent blackhole (socket stays open); SIGKILL = process dies
    time.sleep(observe_s)
    hold = lw.result()
    lingering = psql(db, "select count(*) from pg_stat_activity where application_name='task197_rehearsal'", user=SUP)[1].strip()
    if pr.poll() is None: pr.kill()
    try: os.kill(kid, signal.SIGCONT); os.kill(kid, signal.SIGKILL)
    except ProcessLookupError: pass
    pr.wait(); time.sleep(0.5); s1 = state(db)
    still = psql(db, "select count(*) from pg_locks where relation='public.magi_tasks'::regclass and mode='AccessExclusiveLock'", user=SUP)[1].strip()
    LOG[label] = dict(lock_seen=hold, lock_still_present_at_end=still, rehearsal_session_still_present=lingering)
    return hold, still, clean(s0, s1), lingering
h_, still, ok, ling = lossy("begin", signal.SIGKILL, 2000, 3000, 6.0, "begin_client_sigkill")
check("R10 client SIGKILL during the DO (begin mode): lock freed by the server statement_timeout (~3 s), session gone, no residue", h_ and h_[1] < 4.5 and still == "0" and ok, f"lock seen (first,last,dur) {h_}")
h_, still, ok, ling = lossy("begin", signal.SIGSTOP, 1500, 2000, 8.0, "begin_blackhole")
check("R11 client frozen (SIGSTOP blackhole) during the DO, begin mode: lock freed when the statement errors (statement_timeout 2 s), the lingering session is ended by the idle-in-transaction timeout, no residue", h_ and h_[1] < 3.5 and still == "0" and ok and ling == "0", f"lock seen {h_}, session still present {ling}")
h_, still, ok, ling = lossy("begin", signal.SIGSTOP, 30000, 2000, 9.0, "begin_blackhole_no_idle_timeout")
check("R12 control, idle timeout disabled: the lock is STILL freed at statement_timeout (an erroring statement releases its locks at abort, not at ROLLBACK); only the idle session lingers", h_ and h_[1] < 3.5 and still == "0" and ok and ling == "1", f"lock seen {h_}, session still present {ling}")
h_, still, ok, ling = lossy("session", signal.SIGSTOP, 30000, 2000, 6.0, "session_blackhole")
check("R13 session mode, frozen client: the autocommit statement error frees the lock at statement_timeout (~2 s) without any idle timeout", h_ and h_[1] < 3.5 and still == "0" and ok, f"lock seen {h_}, session still present {ling}")

(Path(sys.argv[sys.argv.index("--log") + 1]) if "--log" in sys.argv else TMP / "log.json").write_text(json.dumps(LOG, indent=2, ensure_ascii=False))
print(f"\n{sum(results)}/{len(results)} passed"); sys.exit(0 if all(results) else 1)
