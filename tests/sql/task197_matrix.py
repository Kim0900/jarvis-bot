"""Measurement matrix for the bounded rollback rehearsal (THROWAWAY PostgreSQL only; never a production DSN).

    PGHOST=<socket dir> PGSUPER=supabase_admin python3 tests/sql/task197_matrix.py --out matrix.json [--n 20]

lock cap 100/250/500 ms  x  idle table / busy table (holder lifetime 60-800 ms measured from holder start; runner start-up (~200 ms) precedes the DO) / 3 concurrent clients
statement_timeout 2/3/5 s x  normal runs / induced stall (pg_sleep inside the DO) / background write+fsync load ("slow I/O" proxy)
Every row is produced by task197_safe_runner.py with a per-variant pinned hash; verdict strings come from the runner itself.
"""
import argparse, concurrent.futures as cf, json, os, statistics, subprocess, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from task197_pgtest_lib import *   # noqa: F401,F403
import psycopg

ap = argparse.ArgumentParser(); ap.add_argument("--out", default="matrix.json"); ap.add_argument("--n", type=int, default=20)
A = ap.parse_args()
ensure_base()
RUNNER = str(ROOT / "tests/sql/task197_safe_runner.py")
EXPECT = json.loads((ROOT / "tests/sql/task197_prod_rollback_rehearsal.expect.json").read_text())
TMP = Path(tempfile.mkdtemp(prefix="t197matrix_"))
def dsn(db, user="postgres"): return f"host={os.environ['PGHOST']} dbname={db} user={user}"

def variant(cap, tag, inject=None):
    text = HARNESS
    assert "v_lt0 > 250" in text and "'250ms'" in text
    text = text.replace("v_lt0 > 250", f"v_lt0 > {cap}").replace("'250ms'", f"'{cap}ms'")
    if inject: text = text.replace("    execute v_gate;\n", "    execute v_gate;\n" + inject, 1)
    p = TMP / f"{tag}.sql"; p.write_text(text, encoding="utf-8")
    ex = dict(EXPECT, sql_sha256=hashlib.sha256(text.encode()).hexdigest(), lock_timeout_ms=cap)
    e = TMP / f"{tag}.expect.json"; e.write_text(json.dumps(ex))
    return str(p), str(e), ex["sql_sha256"]

_n = [0]
def runner(db, v, cap, st_ms, watchdog=8.0):
    _n[0] += 1; rp = TMP / f"rep{_n[0]}.json"; sql, ex, sha = v
    t0 = time.time()
    p = subprocess.run([sys.executable, RUNNER, "--test-mode", "--sql", sql, "--expect", ex, "--sha256", sha, "--report", str(rp),
                        "--lock-timeout-ms", str(cap), "--statement-timeout-ms", str(st_ms), "--watchdog-s", str(watchdog)],
                       capture_output=True, text=True, env=dict(os.environ, PGDSN=dsn(db), TASK197_TEST_MODE="throwaway-server"), timeout=120)
    wall = (time.time() - t0) * 1000
    rep = json.loads(rp.read_text()) if rp.exists() else {}
    return dict(rc=p.returncode, wall_ms=round(wall), do_ms=rep.get("do_wall_ms"), verdict=rep.get("verdict"), msg=(rep.get("message") or "")[-90:], reasons=rep.get("reasons", [])[:3])

def pct(xs, q):
    xs = sorted(xs); 
    if not xs: return None
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1) + 0.4999)))]
def summ(rows, key):
    xs = [r[key] for r in rows if r.get(key) is not None]
    return dict(n=len(xs), p50=pct(xs, .5), p95=pct(xs, .95), max=max(xs) if xs else None)
def outcome(rows):
    c = {}
    for r in rows:
        k = ("PASS" if r["rc"] == 0 else "refused(single flight)" if any(("already exists" in x or "single flight" in x) for x in r["reasons"]) else "55P03" if "FATAL=55P03" in r["msg"] else "other-exit%d:%s" % (r["rc"], r["msg"][-50:])); c[k] = c.get(k, 0) + 1
    return c

class Holder:
    """holds ACCESS SHARE on magi_tasks for hold_ms then releases (a busy table)"""
    def __init__(self, db, hold_ms):
        self.c = psycopg.connect(dsn(db, SUP), autocommit=True); self.c.execute("begin"); self.c.execute("lock table public.magi_tasks in access exclusive mode" if False else "lock table public.magi_tasks in row exclusive mode")
        threading.Timer(hold_ms / 1000, self.rel).start()
    def rel(self):
        try: self.c.execute("rollback"); self.c.close()
        except Exception: pass

def lockwatch(db, box, stop):
    with psycopg.connect(dsn(db, SUP), autocommit=True) as c:
        while not stop.is_set():
            if c.execute("select count(*) from pg_locks where relation='public.magi_tasks'::regclass and mode='AccessExclusiveLock' and pid <> pg_backend_pid()").fetchone()[0]:
                box.append(time.time())
            time.sleep(0.02)

OUT = dict(env=dict(server=psql("postgres", "select version()", user=SUP)[1].strip(), n=A.n, host_cpus=os.cpu_count()))
def wal(db): return int(psql(db, "select pg_current_wal_lsn() - '0/0'::pg_lsn", user=SUP)[1].strip())

# ---- A: lock caps, idle table
OUT["idle_by_lock_cap"] = {}
for cap in (100, 250, 500):
    v = variant(cap, f"cap{cap}"); db = f"t_m_idle{cap}"; clone(db); w0 = wal(db)
    rows = [runner(db, v, cap, 3000) for _ in range(A.n)]
    OUT["idle_by_lock_cap"][cap] = dict(outcome=outcome(rows), do_ms=summ(rows, "do_ms"), wall_ms=summ(rows, "wall_ms"), wal_bytes_per_run=(wal(db) - w0) // A.n)
    print("idle cap", cap, OUT["idle_by_lock_cap"][cap], flush=True)

# ---- B: busy table (a writer-class lock holder releases after hold_ms)
OUT["busy_by_lock_cap"] = {}
for cap in (100, 250, 500):
    v = variant(cap, f"cap{cap}"); db = f"t_m_busy{cap}"; clone(db)
    for hold in (60, 200, 400, 800):
        rows = []
        for _ in range(max(5, A.n // 4)):
            Holder(db, hold); time.sleep(0.05); rows.append(runner(db, v, cap, 3000)); time.sleep(hold / 1000 + 0.1)
        OUT["busy_by_lock_cap"].setdefault(cap, {})[f"holder_{hold}ms"] = dict(outcome=outcome(rows), do_ms=summ(rows, "do_ms"))
        print("busy cap", cap, hold, OUT["busy_by_lock_cap"][cap][f"holder_{hold}ms"], flush=True)

# ---- C: 3 concurrent clients on the same database
OUT["concurrent3_by_lock_cap"] = {}
for cap in (100, 250, 500):
    v = variant(cap, f"cap{cap}"); db = f"t_m_conc{cap}"; clone(db); rows = []
    for _ in range(max(5, A.n // 4)):
        with cf.ThreadPoolExecutor(3) as ex: rows += list(ex.map(lambda _: runner(db, v, cap, 3000), range(3)))
    OUT["concurrent3_by_lock_cap"][cap] = dict(outcome=outcome(rows), do_ms=summ(rows, "do_ms"), runs=len(rows))
    print("conc cap", cap, OUT["concurrent3_by_lock_cap"][cap], flush=True)

# ---- D: statement limits: normal, induced stall, background write+fsync load
OUT["statement_limit"] = {}
for st in (2000, 3000, 5000):
    v = variant(250, "cap250"); stall = variant(250, "stall", inject="    perform pg_sleep(7);\n")
    db = f"t_m_st{st}"; clone(db); res = {}
    rows = [runner(db, v, 250, st) for _ in range(A.n)]; res["normal"] = dict(outcome=outcome(rows), do_ms=summ(rows, "do_ms"))
    rows = []; held = []
    for _ in range(5):
        box, stop = [], threading.Event(); th = threading.Thread(target=lockwatch, args=(db, box, stop), daemon=True); th.start()
        r = runner(db, stall, 250, st); stop.set(); th.join(3); r["lock_held_ms"] = round((box[-1] - box[0]) * 1000) if box else None
        rows.append(r); held.append(r["lock_held_ms"])
    res["stall_7s"] = dict(outcome=outcome(rows), do_ms=summ(rows, "do_ms"), lock_held_ms=summ([dict(x=h) for h in held], "x"),
                           sample=rows[0]["msg"])
    # slow-I/O proxy: concurrent writers with synchronous commits + CHECKPOINT storm on a different database of the same server
    ldb = f"t_m_load{st}"; clone(ldb); psql(ldb, "create table if not exists load_t(i int, pad text)", user=SUP)
    stop = threading.Event()
    def load():
        with psycopg.connect(dsn(ldb, SUP), autocommit=True) as c:
            while not stop.is_set():
                c.execute("insert into load_t select g, repeat('x',2000) from generate_series(1,2000) g"); c.execute("checkpoint")
    ths = [threading.Thread(target=load, daemon=True) for _ in range(3)]; [t.start() for t in ths]
    time.sleep(1); rows = [runner(db, v, 250, st) for _ in range(A.n)]; stop.set(); [t.join(5) for t in ths]
    res["under_write_checkpoint_load"] = dict(outcome=outcome(rows), do_ms=summ(rows, "do_ms"))
    OUT["statement_limit"][st] = res; print("st", st, json.dumps(res)[:400], flush=True)

Path(A.out).write_text(json.dumps(OUT, indent=1, ensure_ascii=False)); print("written", A.out)
