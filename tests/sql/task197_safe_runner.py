#!/usr/bin/env python3
"""TASK#197 bounded driver for the production ROLLBACK-ONLY rehearsal (CASSANDRA corrective spec #5490, hardened for #5673).

Three processes/sessions, each with one job:

  SUPERVISOR (this process)  validates everything BEFORE any connection, holds the admission lock and takes the before/after snapshots on
                             an independent OBSERVER session, starts the CHILD, enforces a wall-clock deadline (SIGKILL), and writes the
                             report itself - so a child that hangs, exits, is killed or loses its network still produces a full report.
  OBSERVER session           pg_try_advisory_lock (single flight) held until the residual check is done; read-only snapshots.
  CHILD process              ONE dedicated connection: BEGIN; SET LOCAL statement_timeout / lock_timeout; verify them on the same
                             connection; the pinned DO (one statement, ends in an unconditional RAISE); expect P0001 + sentinel; ROLLBACK.

PRODUCTION MODE (the default) accepts exactly:   --approval-file <MAGI-signed approval>  [--report out.json]  [--dsn-env NAME]
  * limits are the fixed manifest 3000 ms statement / 250 ms lock / 2000 ms idle-in-transaction / 8 s watchdog, mode begin; no flag can
    change them (any other option is refused before a connection is made);
  * the approval (Ed25519, tests/sql/task197_approval.py) binds task 197, the exact git commit, the SHA256 of the runner / DO / expect /
    snapshot / generator files, the manifest, a time window of at most 30 minutes, the #5486 HOLD clearance and an upload-free window;
  * fixed file paths: --sql / --expect / --snapshot / --sha256 do not exist in production mode.

TEST MODE (--test-mode) exists for the self-tests only.  It is refused unless TASK197_TEST_MODE=throwaway-server is set, the host is
local (unix socket, localhost, 127.0.0.1, ::1) and the database name looks like a throwaway database.  Only there may limits, files and fault
injections vary - and every numeric value is validated (finite, positive, bounded) before any connection.

    --post-check-only   read-only: take the observer snapshot and report residue (use it if a supervisor itself died).

The DSN comes from the environment and is never printed or written.  Use a DIRECT or session-mode connection (not statement pooling).
Requires: pip install "psycopg[binary]" cryptography.  Exit codes: 0 strict PASS and clean; 2 anything else (read the report; do not re-run blindly).
"""
import argparse, hashlib, json, math, os, re, signal, subprocess, sys, tempfile, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import task197_approval as AP  # noqa: E402

SENT = re.compile(r"REHEARSAL_ROLLBACK_SENTINEL result=(\S+) checks=(\d+)/(\d+) scenarios=(\d+)/(\d+) replay_ok=(\d+) replay_drift=(\d+) "
                  r"hold_ms=(\d+) total_ms=(\d+) max_gate_update_ms=(\d+) lock_timeout_ms=(\d+) lock_timeout_caller_ms=(\d+) statement_timeout_ms=(\d+) "
                  r"event_seq_consumed=(-?\d+) gate_sql_md5=(\w+)")
FIELDS = ["result", "checks_ok", "checks_total", "scn_ok", "scn", "replay_ok", "replay_drift", "hold_ms", "total_ms", "max_gate_update_ms",
          "lock_timeout_ms", "lock_timeout_caller_ms", "statement_timeout_ms", "event_seq_consumed", "gate_sql_md5"]

SQL_PATH = ROOT / "tests/sql/task197_prod_rollback_rehearsal.sql"
EXPECT_PATH = ROOT / "tests/sql/task197_prod_rollback_rehearsal.expect.json"
SNAPSHOT_PATH = ROOT / "tests/sql/task197_prod_rollback_snapshot.sql"
PUBKEY_PATH = ROOT / "tests/sql/task197_approval_pubkey.pem"
ADMISSION_LOCK = (1970617, 197001)            # pg_try_advisory_lock(int4, int4): one admitted run per database
SUPERVISOR_MARGIN_S = 3.0                       # child watchdog (cancel at limit, exit 2 s later) + this = the supervisor's SIGKILL deadline
THROWAWAY_DB = re.compile(r"^(t_[a-z0-9_]+|gate[a-z0-9_]*|task197_throwaway_[a-z0-9_]+)$")
LOCAL_HOSTS = ("", "localhost", "127.0.0.1", "::1")
TEST_ONLY = ("sql", "expect", "snapshot", "sha256", "statement_timeout_ms", "lock_timeout_ms", "idle_tx_timeout_ms", "watchdog_s", "mode",
             "test_child_fault", "test_hold_lock_s")


class Refuse(Exception):
    pass


class Early(Exception):
    """a refusal after the observer is connected: skip straight to the residual check and the lock release"""


def lint(text):
    """structural checks on the pinned file (the hash already pins the content; this guards against pinning the wrong artifact)"""
    problems = []
    if len(re.findall(r"^do \$rehearsal\$", text, re.M)) != 1 or not text.rstrip().endswith("end $rehearsal$;"):
        problems.append("not exactly one DO statement")
    tail = text[text.rindex("  v_total_ms :="):]
    if not re.fullmatch(r"  v_total_ms :=[^;]*;\n  -- UNCONDITIONAL.*?\n  raise exception using .*?;\nend \$rehearsal\$;\n", tail, re.S):
        problems.append("does not end with the unconditional RAISE EXCEPTION")
    body = re.sub(r"\$gate\$.*?\$gate\$|\$scn\$.*?\$scn\$|--[^\n]*", "", text, flags=re.S)
    bad = sorted(set(m.lower() for m in re.findall(r"\b(commit|savepoint|nextval|setval|truncate|vacuum|concurrently|dblink|notify|pg_notify|copy|grant|delete\s+from)\b", body, re.I)))
    if bad: problems.append("forbidden tokens: " + ",".join(bad))
    return problems


def finite_number(text):
    try:
        v = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    if not math.isfinite(v):
        raise argparse.ArgumentTypeError("NaN / infinity is not allowed")
    return v


def bounded(lo, hi, integer=True):
    def conv(text):
        v = finite_number(text)
        if integer and v != int(v):
            raise argparse.ArgumentTypeError("must be a whole number")
        if not (lo <= v <= hi):
            raise argparse.ArgumentTypeError(f"must be within [{lo}, {hi}]")
        return int(v) if integer else v
    return conv


def build_parser():
    ap = argparse.ArgumentParser(description="TASK#197 production rollback-only rehearsal driver (see module docstring)")
    ap.add_argument("--approval-file", default=None, help="MAGI-signed approval (production mode)")
    ap.add_argument("--report", default="")
    ap.add_argument("--dsn-env", default="PGDSN")
    ap.add_argument("--post-check-only", action="store_true", help="read-only residue check; opens no rehearsal connection")
    ap.add_argument("--child", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--test-mode", action="store_true", help="self-tests only; requires TASK197_TEST_MODE=throwaway-server and a local throwaway database")
    t = ap.add_argument_group("test-mode only")
    t.add_argument("--sql", default=None); t.add_argument("--expect", default=None); t.add_argument("--snapshot", default=None)
    t.add_argument("--sha256", default=None, help="test mode: the artifact hash you pin (must equal the file and the expect file)")
    t.add_argument("--statement-timeout-ms", type=bounded(100, 60000), default=None)
    t.add_argument("--lock-timeout-ms", type=bounded(10, 1000), default=None)
    t.add_argument("--idle-tx-timeout-ms", type=bounded(100, 30000), default=None)
    t.add_argument("--watchdog-s", type=bounded(0.5, 30, integer=False), default=None)
    t.add_argument("--mode", choices=("begin", "session"), default=None)
    t.add_argument("--test-child-fault", choices=("sigkill", "exit", "netbreak", "hang", "cancelhang"), default=None)
    t.add_argument("--test-hold-lock-s", type=bounded(0, 30, integer=False), default=None)
    return ap


def dsn_parts(dsn):
    from psycopg.conninfo import conninfo_to_dict
    d = conninfo_to_dict(dsn)
    return d.get("host", ""), d.get("dbname", "")


def settings(conn):
    rows = conn.execute("select name, setting from pg_settings where name in ('statement_timeout','lock_timeout','idle_in_transaction_session_timeout','transaction_read_only')").fetchall()
    return {n: s for n, s in rows}


def snapshot(obs, sql):
    return {k: v for k, v in obs.execute(sql).fetchall()}


# ------------------------------------------------------------------------------------------------------------------ CHILD
def child_main(cfg_path):
    """one connection, one DO.  Prints ONE json line.  Faults are injected only when the supervisor was started in test mode."""
    import psycopg
    cfg = json.loads(Path(cfg_path).read_text())
    L, fault = cfg["limits"], cfg.get("fault")
    out = {"events": [], "ok": False}
    t0 = time.time()
    def ev(m): out["events"].append(f"+{time.time() - t0:6.3f}s {m}")
    text = Path(cfg["sql"]).read_text(encoding="utf-8")
    rehearsal = psycopg.connect(os.environ[cfg["dsn_env"]], autocommit=True, connect_timeout=5, application_name="task197_rehearsal",
                                keepalives=1, keepalives_idle=1, keepalives_interval=1, keepalives_count=3, tcp_user_timeout=3000)
    ev("rehearsal connection opened (dedicated, autocommit client-side, explicit transaction control)")
    try:
        rehearsal.execute(f"set idle_in_transaction_session_timeout = {int(L['idle_tx_timeout_ms'])}")     # session level: SET LOCAL is reverted at abort
        if L["mode"] == "begin":
            rehearsal.execute("begin")
            rehearsal.execute(f"set local statement_timeout = '{int(L['statement_timeout_ms'])}ms'")
            rehearsal.execute(f"set local lock_timeout = '{int(L['lock_timeout_ms'])}ms'")
        else:
            rehearsal.execute(f"set statement_timeout = '{int(L['statement_timeout_ms'])}ms'")
            rehearsal.execute(f"set lock_timeout = '{int(L['lock_timeout_ms'])}ms'")
        eff = settings(rehearsal); out["effective"] = eff
        if (int(eff["statement_timeout"]) != L["statement_timeout_ms"] or int(eff["lock_timeout"]) != L["lock_timeout_ms"]
                or int(eff["idle_in_transaction_session_timeout"]) != L["idle_tx_timeout_ms"] or eff["transaction_read_only"] != "off"):
            out["error"] = f"effective settings are not what was requested: {eff}"
            print(json.dumps(out)); return 2
        ev(f"effective settings verified on the same connection: {eff}")

        done = threading.Event()
        def watchdog():
            if done.wait(L["watchdog_s"]): return
            ev("WATCHDOG: client limit reached, cancelling the statement")
            try:
                if fault == "cancelhang": time.sleep(3600)            # the cancel call itself never returns
                rehearsal.cancel()
            except Exception as e: ev(f"cancel failed: {e!r}")
            if done.wait(2.0): return
            out["error"] = "watchdog escalation: process exits to drop the connection"
            sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush(); os._exit(3)
        threading.Thread(target=watchdog, daemon=True).start()

        def inject():
            time.sleep(0.4)
            if fault == "sigkill": os.kill(os.getpid(), signal.SIGKILL)
            if fault == "exit": os._exit(7)
            if fault == "netbreak":
                import socket
                s = socket.socket(fileno=os.dup(rehearsal.pgconn.socket)); s.shutdown(socket.SHUT_RDWR)
        if fault in ("sigkill", "exit", "netbreak"): threading.Thread(target=inject, daemon=True).start()

        t1 = time.time(); ev("executing the pinned DO (one statement)")
        try:
            rehearsal.execute(text)
            out["error"] = "the DO returned without raising: that must never happen"
        except psycopg.errors.RaiseException as e:
            msg = e.diag.message_primary or ""
            out.update(message=msg[:1500], sqlstate=e.sqlstate, detail=(e.diag.message_detail or "")[:6000], do_wall_ms=round((time.time() - t1) * 1000))
        except Exception as e:                      # timeouts, cancellations, lost connection: never retried
            out.update(do_wall_ms=round((time.time() - t1) * 1000), error=f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            done.set()
        try:
            if L["mode"] == "begin": rehearsal.execute("rollback"); ev("explicit ROLLBACK sent")
        except Exception as e:
            out["rollback_error"] = repr(e)[:200]
        out["ok"] = True
    finally:
        try: rehearsal.close()
        except Exception: pass
    if fault == "hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3600)          # never reports: only SIGKILL ends it
    print(json.dumps(out)); sys.stdout.flush()
    return 0


# ------------------------------------------------------------------------------------------------------------------ SUPERVISOR
def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.child:
        sys.exit(child_main(a.child))
    import psycopg

    rep = {"mode": "test" if a.test_mode else "production", "events": [], "verdict": "FAIL", "reasons": []}
    t_start = time.time()
    def ev(msg): rep["events"].append(f"+{time.time() - t_start:6.3f}s {msg}")
    def finish(code):
        rep["verdict"] = "PASS" if code == 0 else "FAIL"
        out = json.dumps(rep, ensure_ascii=False, indent=2)
        if a.report: Path(a.report).write_text(out + "\n", encoding="utf-8")
        print(out); sys.exit(code)
    def fail(reason): rep["reasons"].append(reason); ev("FAIL: " + reason)

    # ---- 1. everything that can be refused WITHOUT a connection is refused here
    try:
        given = [n for n in TEST_ONLY if getattr(a, n) is not None]
        if a.test_mode:
            if os.environ.get("TASK197_TEST_MODE") != "throwaway-server":
                raise Refuse("--test-mode needs TASK197_TEST_MODE=throwaway-server")
        elif given:
            raise Refuse("option(s) only valid in the isolated test mode, refused in production: " + ",".join("--" + g.replace("_", "-") for g in given))
        dsn = os.environ.get(a.dsn_env)
        if not dsn:
            raise Refuse(f"environment variable {a.dsn_env} is not set")
        host, dbname = dsn_parts(dsn)
        if a.test_mode and ((host not in LOCAL_HOSTS and not host.startswith("/")) or not THROWAWAY_DB.match(dbname or "")):
            raise Refuse("test mode is only allowed against a local server and a throwaway database name")
        if a.post_check_only:
            limits = dict(AP.PROD_MANIFEST)
        elif a.test_mode:
            limits = dict(statement_timeout_ms=a.statement_timeout_ms or 3000, lock_timeout_ms=a.lock_timeout_ms or 250,
                          idle_tx_timeout_ms=a.idle_tx_timeout_ms or 2000, watchdog_s=a.watchdog_s or 8, mode=a.mode or "begin")
        else:
            limits = dict(AP.PROD_MANIFEST)
        rep["limits"] = limits
        rep["scope_note"] = {      # CASSANDRA #5673 P1-G: what is a guarantee and what is only a measurement
            "guaranteed_by_server_settings": "statement_timeout / lock_timeout / idle_in_transaction timeout as verified on the rehearsal connection; locks are released when a statement errors",
            "measured_not_guaranteed": "all timings come from PostgreSQL 17.10 throwaway servers; production runs 17.6 on a host with unexplained I/O stalls (#214) - durations there are unknown",
            "authorization": "this report is not an authorization; a live run needs a MAGI-signed approval and the #5486 HOLD cleared"}
        sql_path = Path(a.sql) if a.test_mode and a.sql else SQL_PATH
        expect_path = Path(a.expect) if a.test_mode and a.expect else EXPECT_PATH
        snap_path = Path(a.snapshot) if a.test_mode and a.snapshot else SNAPSHOT_PATH
        approval = None
        if not a.post_check_only:
            if a.test_mode:
                if not a.sha256:
                    raise Refuse("test mode: --sha256 (the artifact hash you pin) is required")
            else:
                if not a.approval_file:
                    raise Refuse("production mode needs --approval-file (MAGI-signed approval)")
                try:
                    approval = AP.verify(json.loads(Path(a.approval_file).read_text()), PUBKEY_PATH.read_bytes(), ROOT,
                                         used_ids_file=Path(os.environ.get("TASK197_STATE_DIR", str(Path.home() / ".task197"))) / "used_approvals")
                except AP.Refused as e:
                    raise Refuse("approval refused: " + str(e))
                except (OSError, ValueError) as e:
                    raise Refuse(f"approval unreadable: {type(e).__name__}")
                rep["approval"] = {k: approval[k] for k in ("approval_id", "operator", "commit", "not_before", "not_after")}
            text = sql_path.read_text(encoding="utf-8"); expect = json.loads(expect_path.read_text(encoding="utf-8"))
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest(); rep.update(sql_sha256=sha, expect=expect)
            if sha != expect["sql_sha256"] or (a.test_mode and sha != a.sha256.lower()):
                raise Refuse("sql sha256 differs from the pinned/expected value")
            probs = lint(text)
            if probs:
                raise Refuse("lint: " + "; ".join(probs))
            if limits["lock_timeout_ms"] > expect["lock_timeout_ms"]:
                raise Refuse("lock timeout looser than the artifact's own cap")
    except Refuse as e:
        fail(str(e)); finish(2)
    except Exception as e:
        fail(f"unexpected {type(e).__name__} before any connection: {str(e)[:200]}"); finish(2)

    # ---- 2. observer session, single-flight admission lock, snapshots
    def on_term(signum, frame): raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, on_term)
    obs = None; child = None; lock_held = False; sentinel = None; result = None
    snap_sql = snap_path.read_text(encoding="utf-8")
    try:
        obs = psycopg.connect(dsn, autocommit=True, connect_timeout=5, application_name="task197_observer")
        rep["server_version"] = obs.execute("show server_version").fetchone()[0]; ev("observer connected, server " + rep["server_version"])
        # the observer must never wait behind the rehearsal's own locks (a stalled DO would otherwise stall the residual check too)
        obs.execute("set lock_timeout = '1000ms'"); obs.execute("set statement_timeout = '5000ms'")
        if a.post_check_only:
            rep["snapshot_after"] = snapshot(obs, snap_sql)
            for k in ("INV_gate_functions", "INV_gate_triggers", "INV_fixture_tasks", "INV_fixture_events", "INV_gate_events", "INV_rehearsal_correction_log"):
                if rep["snapshot_after"].get(k) != "0": fail(f"residue: {k}={rep['snapshot_after'].get(k)}")
            for k in ("OBS_rehearsal_sessions", "OBS_exclusive_lockers"):
                if rep["snapshot_after"].get(k) != "0": fail(f"live activity: {k}={rep['snapshot_after'].get(k)}")
            raise Early()
        if not obs.execute("select pg_try_advisory_lock(%s,%s)", ADMISSION_LOCK).fetchone()[0]:
            fail("single flight: another rehearsal holds the admission lock; nothing was started"); raise Early()
        lock_held = True; ev("admission advisory lock acquired on the observer session")
        if a.test_mode and a.test_hold_lock_s: time.sleep(a.test_hold_lock_s)
        if approval is not None:
            sd = Path(os.environ.get("TASK197_STATE_DIR", str(Path.home() / ".task197"))) / "used_approvals"
            if not AP.consume_approval_id(sd, approval["approval_id"]):
                fail("approval_id was already used (single use)"); raise Early()
        if obs.execute("select count(*) from pg_stat_activity where application_name='task197_rehearsal'").fetchone()[0]:
            fail("a task197_rehearsal session already exists (previous attempt?) - investigate first"); raise Early()
        pre = snapshot(obs, snap_sql); rep["snapshot_before"] = pre; ev("pre-snapshot taken")
        if pre["INV_gate_functions"] != "0" or pre["INV_gate_triggers"] != "0" or pre["INV_fixture_tasks"] != "0" or pre["INV_fixture_events"] != "0":
            fail("pre-snapshot is not clean (gate objects or fixtures already exist)"); raise Early()

        # ---- 3. the child does the DO; the supervisor only waits, with a deadline it enforces by SIGKILL
        cfg = {"sql": str(sql_path), "limits": limits, "dsn_env": a.dsn_env, "fault": a.test_child_fault if a.test_mode else None}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f); cfg_path = f.name
        deadline = limits["watchdog_s"] + 2.0 + SUPERVISOR_MARGIN_S
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child", cfg_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ev(f"child started (pid {child.pid}), supervisor deadline {deadline:.1f} s")
        try:
            out, err = child.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            child.kill(); out, err = child.communicate()
            fail(f"child exceeded the supervisor deadline ({deadline:.1f} s) and was SIGKILLed"); rep["child_killed_by_supervisor"] = True
        finally:
            try: os.unlink(cfg_path)
            except OSError: pass
        rep["child_returncode"] = child.returncode
        if child.returncode is not None and child.returncode < 0:
            rep["child_signal"] = -child.returncode
        if err.strip(): rep["child_stderr_tail"] = err.strip()[-300:]
        lines = [l for l in out.splitlines() if l.startswith("{")]
        try:
            result = json.loads(lines[-1]) if lines else None
        except ValueError:
            result = None
        if result is None:
            fail(f"child produced no report (returncode {child.returncode}): the outcome of the DO is unprovable from the client side, residue check follows")
        else:
            rep["events"] += ["child: " + e for e in result.get("events", [])]
            for k in ("effective", "message", "sqlstate", "detail", "do_wall_ms", "rollback_error"):
                if k in result: rep[{"effective": "effective_settings_on_rehearsal_connection"}.get(k, k)] = result[k]
            if result.get("error"): fail(result["error"])
            if result.get("rollback_error"): fail("ROLLBACK failed: " + result["rollback_error"])
            if result.get("effective") and not result.get("error"):
                m = SENT.search(result.get("message") or "")
                if result.get("sqlstate") == "P0001" and m: sentinel = dict(zip(FIELDS, m.groups())); ev("sentinel received")
                elif not result.get("error"): fail(f"error without a valid sentinel: {result.get('sqlstate')} {(result.get('message') or '')[:300]}")
    except Early:
        pass
    except SystemExit as e:
        if child is not None and child.poll() is None: child.kill()
        fail(f"supervisor interrupted (exit {e.code}); residue check follows")
    except Exception as e:
        if child is not None and child.poll() is None: child.kill()
        fail(f"{type(e).__name__}: {str(e)[:300]}")

    # ---- 4. independent residual check (still holding the admission lock), then release
    if obs is not None and "snapshot_before" in rep:
        try:
            t1 = time.time(); gone = False
            while time.time() - t1 < limits["idle_tx_timeout_ms"] / 1000 + 4:
                if obs.execute("select count(*) from pg_stat_activity where application_name='task197_rehearsal'").fetchone()[0] == 0: gone = True; break
                time.sleep(0.1)
            rep["rehearsal_session_gone_after_s"] = round(time.time() - t1, 2)
            if not gone: fail("the rehearsal session is still present (possible lock holder): manual review, HOLD further live runs, do NOT run cleanup DDL")
            try:
                post = snapshot(obs, snap_sql); rep["snapshot_after"] = post
            except Exception as e:
                # blocked (lock held by a stuck DO) or unreachable: say so, and record what the lock-free catalog views show
                fail(f"post-run snapshot could not be taken ({type(e).__name__}): residue is UNPROVABLE - HOLD further live runs, manual review")
                try:
                    rep["residual_activity_lock_free"] = {
                        "rehearsal_sessions": obs.execute("select count(*) from pg_stat_activity where application_name='task197_rehearsal'").fetchone()[0],
                        "exclusive_locks_on_gate_tables": obs.execute("select count(*) from pg_locks l where l.relation in ('public.magi_tasks'::regclass,'public.magi_task_events'::regclass) and l.mode in ('AccessExclusiveLock','ShareRowExclusiveLock')").fetchone()[0]}
                except Exception as e2:
                    rep["residual_activity_lock_free"] = f"unavailable: {type(e2).__name__}"
                raise Early()
            for k, v in post.items():
                if k.startswith("INV_") and v != rep["snapshot_before"].get(k): fail(f"invariant changed: {k} {rep['snapshot_before'].get(k)} -> {v}")
            for k in ("INV_gate_functions", "INV_gate_triggers", "INV_fixture_tasks", "INV_fixture_events", "INV_gate_events", "INV_rehearsal_correction_log"):
                if post.get(k) != "0": fail(f"leftover detected: {k}={post.get(k)} (committed objects?) - manual review, no automatic cleanup")
            if post.get("OBS_exclusive_lockers") != "0": fail(f"exclusive locks still present on the gate tables: {post.get('OBS_exclusive_lockers')} - manual review")
            rep["observed_event_seq_delta_noisy"] = int(post["OBS_event_seq_last"]) - int(rep["snapshot_before"]["OBS_event_seq_last"])
        except Exception as e:
            fail(f"post-run observer check failed: {e!r}; residue is UNPROVABLE - HOLD")
    if obs is not None:
        try:
            if lock_held: obs.execute("select pg_advisory_unlock(%s,%s)", ADMISSION_LOCK)
        except Exception: pass
        try: obs.close()
        except Exception: pass

    if a.post_check_only:
        finish(0 if not rep["reasons"] else 2)

    # ---- 5. strict verdict
    if sentinel:
        rep["sentinel"] = sentinel
        s, expect_ = sentinel, rep["expect"]
        checks = [(s["result"] == "PASS", f"result={s['result']}"),
                  (int(s["checks_ok"]) == int(s["checks_total"]) == expect_["checks"], f"checks {s['checks_ok']}/{s['checks_total']} (expected {expect_['checks']}/{expect_['checks']})"),
                  (int(s["scn_ok"]) == int(s["scn"]) == expect_["scenarios"], f"scenarios {s['scn_ok']}/{s['scn']}"),
                  (int(s["replay_ok"]) == expect_["replay_ok"] and int(s["replay_drift"]) == 0, f"replay_ok={s['replay_ok']} replay_drift={s['replay_drift']}"),
                  (s["gate_sql_md5"] == expect_["gate_sql_md5"], "embedded gate md5"),
                  (int(s["lock_timeout_ms"]) <= limits["lock_timeout_ms"], f"lock_timeout in force {s['lock_timeout_ms']} ms (caller {s['lock_timeout_caller_ms']} ms)"),
                  (int(s["statement_timeout_ms"]) == limits["statement_timeout_ms"], f"statement_timeout seen inside the DO {s['statement_timeout_ms']} ms")]
        for ok, what in checks:
            if not ok: fail("strict verdict: " + what)
        rep["strict_checks"] = [("ok " if ok else "BAD ") + what for ok, what in checks]
    else:
        rep["reasons"].append("no sentinel")
    ev("finished")
    finish(0 if not rep["reasons"] and sentinel else 2)


if __name__ == "__main__":
    main()
