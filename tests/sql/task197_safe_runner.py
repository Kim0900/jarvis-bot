#!/usr/bin/env python3
"""TASK#197 bounded driver for the production ROLLBACK-ONLY rehearsal (CASSANDRA corrective spec #5490, hardened for #5673).

Three processes/sessions, each with one job:

  SUPERVISOR (this process)  validates everything BEFORE any connection, holds the admission lock and takes the before/after snapshots on
                             an independent OBSERVER session, starts the CHILD, enforces a wall-clock deadline (SIGKILL), and writes the
                             report itself - so a child that hangs, exits, is killed or loses its network still produces a full report.
  OBSERVER session           pg_try_advisory_lock (single flight) held until the residual check is done; read-only snapshots.
  CHILD process              ONE dedicated connection: BEGIN; SET LOCAL statement_timeout / lock_timeout; verify them on the same
                             connection; the pinned DO (one statement, ends in an unconditional RAISE); expect P0001 + sentinel; ROLLBACK.

THE CHILD IS NOT A COMMAND-LINE MODE (CASSANDRA #5719 C197-CHILD-BYPASS, P0-FIX-197-01).  There is no --child option: the supervisor starts the
child as `python -I -c "...child_entry(<fd>)"` and hands it ONE sealed capability over an inherited pipe (a JSON document: the signed approval, the
manifest, a random nonce).  The child accepts nothing from argv or the files named there: it independently (1) re-verifies the Ed25519 approval
(trust anchor, signature, exact git commit, hashes of all guarded files, fixed manifest, time window, HOLD + upload-free claims), (2) re-hashes and
re-lints the DO text it is about to execute and compares it with the approved hash, (3) checks that the supervisor already admitted this approval
(single-use record) and claims its own one-time child token, (4) checks that a supervisor observer session really holds the admission advisory
lock, and only then connects.  A direct/unsigned/forged/replayed call is refused before any database connection.  Entry points: see
docs/TASK197_ROLLBACK_REHEARSAL_PLAN.md section 8-D.

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
THROWAWAY_DB = AP.THROWAWAY_DB
LOCAL_HOSTS = AP.LOCAL_HOSTS
SQL_REL = "tests/sql/task197_prod_rollback_rehearsal.sql"
SNAPSHOT_SQL_SHA256 = "3ff142041013874011639a0b815857e0d7a8aed3afb25cc9d62c38c0d4c6a176"   # the observer's read-only SQL is pinned too (post-check-only has no approval)
HOSTILE_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONBREAKPOINT", "PYTHONSAFEPATH", "PYTHONUSERBASE")
CAP_KEYS = {"v", "mode", "nonce", "approval", "limits", "dsn_env", "fault", "sql_path", "pin"}
DSN_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,40}$")
FAULTS = ("sigkill", "exit", "netbreak", "hang", "cancelhang")
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
    i = text.rfind("  v_total_ms :=")
    tail = text[i:] if i >= 0 else ""
    if not re.fullmatch(r"  v_total_ms :=[^;]*;\n  -- UNCONDITIONAL.*?\n  raise exception using .*?;\nend \$rehearsal\$;\n", tail, re.S):
        problems.append("does not end with the unconditional RAISE EXCEPTION")
    body = re.sub(r"\$gate\$.*?\$gate\$|\$scn\$.*?\$scn\$|--[^\n]*", "", text, flags=re.S)
    bad = sorted(set(m.lower() for m in re.findall(r"\b(commit|savepoint|nextval|setval|truncate|vacuum|concurrently|dblink|notify|pg_notify|copy|grant|delete\s+from)\b", body, re.I)))
    if bad: problems.append("forbidden tokens: " + ",".join(bad))
    return problems


def lint_snapshot(text):
    """the observer SQL must be one read-only SELECT; the observer session is also read-only on the server side"""
    body = re.sub(r"'(?:[^']|'')*'", "''", re.sub(r"--[^\n]*", "", text))
    problems = []
    if not re.match(r"\s*(select|with)\b", body, re.I): problems.append("snapshot SQL is not a SELECT")
    if body.strip().rstrip(";").count(";"): problems.append("snapshot SQL has more than one statement")
    bad = sorted(set(m.lower() for m in re.findall(r"\b(insert|update|delete|create|alter|drop|truncate|grant|revoke|commit|vacuum|copy|call|do|lock|listen|notify|set|reset|analyze|refresh|nextval|setval|pg_terminate_backend|pg_cancel_backend|pg_advisory_\w+|pg_sleep)\b", body, re.I)))
    if bad: problems.append("snapshot SQL contains forbidden tokens: " + ",".join(bad))
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
def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def child_entry(fd):
    """The ONLY way to reach the rehearsal connection.  Not reachable from the command line (no option, argparse rejects unknown flags);
    started by the supervisor as `python -I -c ...child_entry(fd)`.  Reads ONE sealed capability from the inherited pipe, verifies it on its own
    (nothing from argv, nothing it trusts from the parent beyond the signed approval it re-verifies), and refuses BEFORE any connection if any
    check fails.  Prints one JSON line (always, including refusals)."""
    out = {"events": [], "ok": False}
    def refuse(why):
        out["refused"] = why
        sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush(); return 2
    try:
        with os.fdopen(fd, "rb") as f:
            raw = f.read(1 << 20)
        cap = json.loads(raw.decode("utf-8"))
    except Exception:
        return refuse("no valid capability on the inherited pipe")
    if not isinstance(cap, dict) or set(cap) != CAP_KEYS or cap["v"] != 1 or cap["mode"] not in ("production", "test"):
        return refuse("capability malformed")
    if not re.fullmatch(r"[0-9a-f]{32}", str(cap["nonce"])) or not isinstance(cap["dsn_env"], str) or not DSN_ENV_RE.match(cap["dsn_env"]):
        return refuse("capability fields malformed")
    out["nonce"] = cap["nonce"]; out["cap_sha256"] = hashlib.sha256(raw).hexdigest()
    dsn = os.environ.get(cap["dsn_env"])
    if not dsn:
        return refuse("DSN variable not set")
    try:
        host, dbname = dsn_parts(dsn)
    except Exception:
        return refuse("DSN unparseable")
    used_file = None
    if cap["mode"] == "production":
        if cap["fault"] is not None or cap["pin"] is not None or cap["limits"] != AP.PROD_MANIFEST:
            return refuse("production capability carries test-only fields or a manifest other than the fixed one")
        try:
            appr = AP.verify(cap["approval"], PUBKEY_PATH.read_bytes(), ROOT)            # single use is the supervisor's; the child proves its own claim below
        except AP.Refused as e:
            return refuse("approval: " + str(e))
        except Exception as e:
            return refuse(f"approval check failed: {type(e).__name__}")
        sql_path, want_sha, L, fault = SQL_PATH, appr["files"][SQL_REL], dict(AP.PROD_MANIFEST), None
        used_file = AP.state_dir(host, dbname) / "used_approvals"
        if not AP.claimed(used_file, appr["approval_id"]):
            return refuse("the supervisor has not admitted this approval (no single-use record)")
        if not AP.consume_approval_id(used_file, appr["approval_id"] + ":child"):
            return refuse("this approval's child token was already used")
    else:
        if os.environ.get("TASK197_TEST_MODE") != "throwaway-server" or not (host in LOCAL_HOSTS or host.startswith("/")) or not THROWAWAY_DB.match(dbname or ""):
            return refuse("test capability outside the isolated throwaway setup")
        if cap["approval"] is not None or not re.fullmatch(r"[0-9a-f]{64}", str(cap["pin"])):
            return refuse("test capability malformed (approval/pin)")
        L = cap["limits"]
        try:
            ok = (isinstance(L, dict) and set(L) == set(AP.PROD_MANIFEST) and 100 <= L["statement_timeout_ms"] <= 60000 and 10 <= L["lock_timeout_ms"] <= 1000
                  and 100 <= L["idle_tx_timeout_ms"] <= 30000 and 0.5 <= L["watchdog_s"] <= 30 and L["mode"] in ("begin", "session")
                  and all(isinstance(L[k], (int, float)) and math.isfinite(L[k]) for k in ("statement_timeout_ms", "lock_timeout_ms", "idle_tx_timeout_ms", "watchdog_s")))
        except Exception:
            ok = False
        if not ok:
            return refuse("test limits out of bounds")
        fault = cap["fault"]
        if fault is not None and fault not in FAULTS:
            return refuse("unknown test fault")
        sql_path, want_sha = Path(str(cap["sql_path"])), cap["pin"]
    try:
        text = sql_path.read_text(encoding="utf-8")
    except OSError:
        return refuse("DO file unreadable")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if sha != want_sha:
        return refuse("DO text differs from the approved/pinned hash")
    if cap["mode"] == "production":
        try:
            if sha != json.loads(EXPECT_PATH.read_text(encoding="utf-8"))["sql_sha256"]:
                return refuse("DO text differs from the expect file")
        except Exception:
            return refuse("expect file unreadable")
    probs = lint(text)
    if probs:
        return refuse("lint: " + "; ".join(probs))
    return _child_execute(text, L, cap["dsn_env"], fault, out, os.getppid())


def _child_execute(text, L, dsn_env, fault, out, parent_pid):
    """one connection, one DO.  Prints ONE json line.  Faults are injected only for a test capability that passed the throwaway-setup checks."""
    import psycopg
    t0 = time.time()
    def ev(m): out["events"].append(f"+{time.time() - t0:6.3f}s {m}")
    rehearsal = psycopg.connect(os.environ[dsn_env], autocommit=True, connect_timeout=5, application_name="task197_rehearsal",
                                keepalives=1, keepalives_idle=1, keepalives_interval=1, keepalives_count=3, tcp_user_timeout=3000)
    ev("rehearsal connection opened (dedicated, autocommit client-side, explicit transaction control)")
    try:
        # the supervisor's observer must be alive and hold the admission lock (a directly started child has no supervisor)
        held = rehearsal.execute("select count(*) from pg_locks l join pg_stat_activity a on a.pid = l.pid where l.locktype = 'advisory' and l.classid = %s "
                                 "and l.objid = %s and l.objsubid = 2 and l.granted and a.application_name = 'task197_observer' and l.pid <> pg_backend_pid()",
                                 ADMISSION_LOCK).fetchone()[0]
        if held != 1:
            out["refused"] = "no supervisor observer holds the admission lock: this child was not started by a live supervisor"
            sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush(); return 2
        ev("a supervisor observer holds the admission lock")
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

        def parent_watch():                          # supervisor gone (crash / SIGKILL): stop at once instead of running on unsupervised
            while not done.wait(0.2):
                if os.getppid() != parent_pid:
                    ev("PARENT GONE: cancelling the statement")
                    try: rehearsal.cancel()
                    except Exception as e: ev(f"cancel failed: {e!r}")
                    if done.wait(2.0): return
                    out["error"] = "supervisor vanished; process exits to drop the connection"
                    sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush(); os._exit(3)
        threading.Thread(target=parent_watch, daemon=True).start()

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
    import psycopg

    rep = {"mode": "test" if a.test_mode else "production", "events": [], "verdict": "FAIL", "reasons": []}
    t_start = time.time()
    def ev(msg): rep["events"].append(f"+{time.time() - t_start:6.3f}s {msg}")
    def finish(code):
        rep["verdict"] = "PASS" if code == 0 else "FAIL"
        out = json.dumps(rep, ensure_ascii=False, indent=2)
        if a.report:
            try:                                               # exclusive create: an existing file is never overwritten, not even by a refusal report
                fd = os.open(a.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh: fh.write(out + "\n")
            except FileExistsError:
                pass
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
        if not DSN_ENV_RE.match(a.dsn_env or ""):
            raise Refuse("--dsn-env must be an upper-case environment variable name")
        hostile = [k for k in HOSTILE_ENV if os.environ.get(k)]
        if hostile and not a.test_mode:
            raise Refuse("interpreter-altering environment variables are set (" + ",".join(hostile) + "): run `python3 -I` with a clean environment")
        if a.report and Path(a.report).exists():
            raise Refuse("--report path already exists (reports are never overwritten)")
        dsn = os.environ.get(a.dsn_env)
        if not dsn:
            raise Refuse(f"environment variable {a.dsn_env} is not set")
        host, dbname = dsn_parts(dsn)
        state_dir = AP.state_dir(host, dbname)
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
        snap_sql = snap_path.read_text(encoding="utf-8")                      # read ONCE; this exact text is what the observer runs
        if snap_path == SNAPSHOT_PATH and hashlib.sha256(snap_sql.encode("utf-8")).hexdigest() != SNAPSHOT_SQL_SHA256:
            raise Refuse("observer snapshot SQL differs from the pinned hash")
        sprobs = lint_snapshot(snap_sql)
        if sprobs:
            raise Refuse("; ".join(sprobs))
        approval = None
        if not a.post_check_only:
            if a.test_mode:
                if not a.sha256:
                    raise Refuse("test mode: --sha256 (the artifact hash you pin) is required")
            else:
                if not a.approval_file:
                    raise Refuse("production mode needs --approval-file (MAGI-signed approval)")
                try:
                    approval_raw = json.loads(Path(a.approval_file).read_text())
                    approval = AP.verify(approval_raw, PUBKEY_PATH.read_bytes(), ROOT, used_ids_file=state_dir / "used_approvals")
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
    try:
        obs = psycopg.connect(dsn, autocommit=True, connect_timeout=5, application_name="task197_observer")
        obs.execute("set default_transaction_read_only = on")                   # the observer can read and take the advisory lock, nothing else
        if obs.execute("show default_transaction_read_only").fetchone()[0] != "on":
            raise Early()
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
            if not AP.consume_approval_id(state_dir / "used_approvals", approval["approval_id"]):
                fail("approval_id was already used (single use)"); raise Early()
        if obs.execute("select count(*) from pg_stat_activity where application_name='task197_rehearsal'").fetchone()[0]:
            fail("a task197_rehearsal session already exists (previous attempt?) - investigate first"); raise Early()
        pre = snapshot(obs, snap_sql); rep["snapshot_before"] = pre; ev("pre-snapshot taken")
        if pre["INV_gate_functions"] != "0" or pre["INV_gate_triggers"] != "0" or pre["INV_fixture_tasks"] != "0" or pre["INV_fixture_events"] != "0":
            fail("pre-snapshot is not clean (gate objects or fixtures already exist)"); raise Early()

        # ---- 3. the child does the DO; the supervisor only waits, with a deadline it enforces by SIGKILL
        nonce = os.urandom(16).hex()
        cap = {"v": 1, "mode": "test" if a.test_mode else "production", "nonce": nonce, "approval": None if a.test_mode else approval_raw, "limits": limits,
               "dsn_env": a.dsn_env, "fault": a.test_child_fault if a.test_mode else None, "sql_path": str(sql_path) if a.test_mode else None,
               "pin": a.sha256.lower() if a.test_mode else None}
        cap_bytes = canonical_json(cap); rep["capability_sha256"] = hashlib.sha256(cap_bytes).hexdigest()
        deadline = limits["watchdog_s"] + 2.0 + SUPERVISOR_MARGIN_S
        rfd, wfd = os.pipe()
        boot = f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent)!r}); import task197_safe_runner as R; sys.exit(R.child_entry({rfd}))"
        child = subprocess.Popen([sys.executable, "-I", "-c", boot], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(rfd,))
        os.close(rfd)
        try:
            os.write(wfd, cap_bytes)
        finally:
            os.close(wfd)
        ev(f"child started (pid {child.pid}) with a sealed capability on an inherited pipe, supervisor deadline {deadline:.1f} s")
        try:
            out, err = child.communicate(timeout=deadline)
        except subprocess.TimeoutExpired:
            child.kill(); out, err = child.communicate()
            fail(f"child exceeded the supervisor deadline ({deadline:.1f} s) and was SIGKILLed"); rep["child_killed_by_supervisor"] = True
        rep["child_returncode"] = child.returncode
        if child.returncode is not None and child.returncode < 0:
            rep["child_signal"] = -child.returncode
        if err.strip(): rep["child_stderr_tail"] = err.strip()[-300:]
        lines = [l for l in out.splitlines() if l.startswith("{")]
        try:
            result = json.loads(lines[-1]) if lines else None
        except ValueError:
            result = None
        if result is not None and result.get("refused"):
            fail("child refused the capability: " + str(result["refused"])); result = {"events": result.get("events", [])}
        elif result is not None and (result.get("nonce") != nonce or result.get("cap_sha256") != rep["capability_sha256"]):
            fail("child report does not echo the nonce / capability digest it was given: discarded"); result = None
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
