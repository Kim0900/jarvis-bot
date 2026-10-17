#!/usr/bin/env python3
"""TASK#197 bounded, single-connection driver for the production ROLLBACK-ONLY rehearsal (CASSANDRA corrective spec #5490 P0-FIX-01).

Why this exists: lock_timeout only bounds lock ACQUISITION, and a statement_timeout set inside the running DO cannot bound that same
statement.  Separate pooled calls (for example a SQL-over-HTTP connector) do not share a session, so a timeout set in one call is not
in force in the next.  This driver therefore uses ONE dedicated PostgreSQL connection and sets the limits itself:

    BEGIN; SET LOCAL statement_timeout; SET LOCAL lock_timeout; (verify the effective values on THIS connection)
    <the pinned DO statement, alone>; expect SQLSTATE P0001 + REHEARSAL_ROLLBACK_SENTINEL; ROLLBACK; close.

Rules it enforces: the SQL file must hash to the value you pin on the command line; it is one DO statement ending in an unconditional
RAISE; no automatic retry of any kind (any exception, timeout, cancel, lost connection or sentinel mismatch ends the run); a client-side
watchdog cancels the statement; the verdict is strict (checks, scenarios, replay_ok=13, replay_drift=0, lock_timeout not loosened); and an
independent observer connection takes the before/after snapshots and checks for leftovers.  It NEVER runs cleanup DDL by itself.

    PGDSN='postgresql://...'  python3 tests/sql/task197_safe_runner.py --sha256 <sql sha256> --approval-event <MAGI approval id> [--report out.json]

The DSN comes from the environment and is never printed or written.  Use a DIRECT or session-mode connection (not statement pooling).
Requires: pip install "psycopg[binary]".  Exit codes: 0 strict PASS and clean; 2 anything else (read the report; do not re-run blindly).
"""
import argparse, hashlib, json, os, re, sys, threading, time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
SENT = re.compile(r"REHEARSAL_ROLLBACK_SENTINEL result=(\S+) checks=(\d+)/(\d+) scenarios=(\d+)/(\d+) replay_ok=(\d+) replay_drift=(\d+) "
                  r"hold_ms=(\d+) total_ms=(\d+) max_gate_update_ms=(\d+) lock_timeout_ms=(\d+) lock_timeout_caller_ms=(\d+) statement_timeout_ms=(\d+) "
                  r"event_seq_consumed=(-?\d+) gate_sql_md5=(\w+)")
FIELDS = ["result", "checks_ok", "checks_total", "scn_ok", "scn", "replay_ok", "replay_drift", "hold_ms", "total_ms", "max_gate_update_ms",
          "lock_timeout_ms", "lock_timeout_caller_ms", "statement_timeout_ms", "event_seq_consumed", "gate_sql_md5"]


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


def snapshot(obs, sql):
    return {k: v for k, v in obs.execute(sql).fetchall()}


def settings(conn):
    rows = conn.execute("select name, setting from pg_settings where name in ('statement_timeout','lock_timeout','idle_in_transaction_session_timeout','transaction_read_only')").fetchall()
    return {n: s for n, s in rows}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sql", default=str(ROOT / "tests/sql/task197_prod_rollback_rehearsal.sql"))
    ap.add_argument("--expect", default=str(ROOT / "tests/sql/task197_prod_rollback_rehearsal.expect.json"))
    ap.add_argument("--snapshot", default=str(ROOT / "tests/sql/task197_prod_rollback_snapshot.sql"))
    ap.add_argument("--sha256", required=True, help="the artifact hash YOU pin (must equal the file and the expect file)")
    ap.add_argument("--approval-event", required=True, help="registry id of the MAGI approval for this live window (recorded, not interpreted)")
    ap.add_argument("--statement-timeout-ms", type=int, default=3000)
    ap.add_argument("--lock-timeout-ms", type=int, default=250)
    ap.add_argument("--idle-tx-timeout-ms", type=int, default=2000, help="session-level cleanup of a lingering aborted-transaction session if the client vanishes after the error (locks are already released by the error itself)")
    ap.add_argument("--watchdog-s", type=float, default=8.0, help="client wall-clock limit for the DO: cancel, then exit the process 2 s later")
    ap.add_argument("--mode", choices=("begin", "session"), default="begin", help="begin = BEGIN + SET LOCAL (spec); session = autocommit + session SET (locks free at the error)")
    ap.add_argument("--dsn-env", default="PGDSN")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    rep = {"approval_event": a.approval_event, "mode": a.mode, "limits": {"statement_timeout_ms": a.statement_timeout_ms, "lock_timeout_ms": a.lock_timeout_ms,
           "idle_tx_timeout_ms": a.idle_tx_timeout_ms, "watchdog_s": a.watchdog_s}, "events": [], "verdict": "FAIL", "reasons": []}
    t_start = time.time()
    def ev(msg): rep["events"].append(f"+{time.time() - t_start:6.3f}s {msg}")
    def finish(code):
        rep["verdict"] = "PASS" if code == 0 else "FAIL"
        out = json.dumps(rep, ensure_ascii=False, indent=2)
        if a.report: Path(a.report).write_text(out + "\n", encoding="utf-8")
        print(out); sys.exit(code)
    def fail(reason): rep["reasons"].append(reason); ev("FAIL: " + reason)

    dsn = os.environ.get(a.dsn_env)
    if not dsn: fail(f"environment variable {a.dsn_env} is not set"); finish(2)
    text = Path(a.sql).read_text(encoding="utf-8"); expect = json.loads(Path(a.expect).read_text(encoding="utf-8"))
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest(); rep.update(sql_sha256=sha, pinned=a.sha256.lower(), expect=expect)
    if sha != a.sha256.lower() or sha != expect["sql_sha256"]: fail("sql sha256 differs from the pinned/expected value"); finish(2)
    probs = lint(text)
    if probs: fail("lint: " + "; ".join(probs)); finish(2)
    if a.lock_timeout_ms > expect["lock_timeout_ms"]: fail("--lock-timeout-ms looser than the artifact's own cap"); finish(2)

    obs = rehearsal = None
    sentinel = None; err_text = ""
    try:
        obs = psycopg.connect(dsn, autocommit=True, connect_timeout=5, application_name="task197_observer")
        rep["server_version"] = obs.execute("show server_version").fetchone()[0]; ev("observer connected, server " + rep["server_version"])
        if obs.execute("select count(*) from pg_stat_activity where application_name='task197_rehearsal'").fetchone()[0]:
            fail("a task197_rehearsal session already exists (previous attempt?) - investigate first"); finish(2)
        pre = snapshot(obs, Path(a.snapshot).read_text(encoding="utf-8")); rep["snapshot_before"] = pre; ev("pre-snapshot taken")
        if pre["INV_gate_functions"] != "0" or pre["INV_gate_triggers"] != "0" or pre["INV_fixture_tasks"] != "0" or pre["INV_fixture_events"] != "0":
            fail("pre-snapshot is not clean (gate objects or fixtures already exist)"); finish(2)

        rehearsal = psycopg.connect(dsn, autocommit=True, connect_timeout=5, application_name="task197_rehearsal",
                                    keepalives=1, keepalives_idle=1, keepalives_interval=1, keepalives_count=3, tcp_user_timeout=3000)
        ev("rehearsal connection opened (dedicated, autocommit client-side, explicit transaction control)")
        rehearsal.execute(f"set idle_in_transaction_session_timeout = {int(a.idle_tx_timeout_ms)}")      # session level: SET LOCAL values are reverted when the transaction aborts
        if a.mode == "begin":
            rehearsal.execute("begin")
            rehearsal.execute(f"set local statement_timeout = '{int(a.statement_timeout_ms)}ms'")
            rehearsal.execute(f"set local lock_timeout = '{int(a.lock_timeout_ms)}ms'")
        else:
            rehearsal.execute(f"set statement_timeout = '{int(a.statement_timeout_ms)}ms'")
            rehearsal.execute(f"set lock_timeout = '{int(a.lock_timeout_ms)}ms'")
        eff = settings(rehearsal); rep["effective_settings_on_rehearsal_connection"] = eff
        if (int(eff["statement_timeout"]) != a.statement_timeout_ms or int(eff["lock_timeout"]) != a.lock_timeout_ms
                or int(eff["idle_in_transaction_session_timeout"]) != a.idle_tx_timeout_ms or eff["transaction_read_only"] != "off"):
            fail(f"effective settings on the rehearsal connection are not what was requested: {eff}"); raise SystemExit
        ev(f"effective settings verified on the same connection: {eff}")

        done = threading.Event()
        def watchdog():
            if done.wait(a.watchdog_s): return
            ev("WATCHDOG: client limit reached, cancelling the statement")
            try: rehearsal.cancel()
            except Exception as e: ev(f"cancel failed: {e!r}")
            if done.wait(2.0): return
            rep["reasons"].append("watchdog escalation: process exits to drop the connection"); rep["verdict"] = "FAIL"
            if a.report: Path(a.report).write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os._exit(3)
        threading.Thread(target=watchdog, daemon=True).start()

        t0 = time.time(); ev("executing the pinned DO (one statement)")
        try:
            rehearsal.execute(text)
            fail("the DO returned without raising: that must never happen")
        except psycopg.errors.RaiseException as e:
            err_text = e.diag.message_primary or ""; rep["message"] = err_text[:1500]; rep["do_wall_ms"] = round((time.time() - t0) * 1000)
            rep["sqlstate"] = e.sqlstate; rep["detail"] = (e.diag.message_detail or "")[:6000]
            m = SENT.search(err_text)
            if e.sqlstate == "P0001" and m: sentinel = dict(zip(FIELDS, m.groups())); ev("sentinel received")
            else: fail(f"error without a valid sentinel: {e.sqlstate} {err_text[:300]}")
        except Exception as e:   # timeouts, cancellations, lost connection, anything else: never retried
            rep["do_wall_ms"] = round((time.time() - t0) * 1000); fail(f"{type(e).__name__}: {str(e)[:300]}")
        finally:
            done.set()
        try:
            if a.mode == "begin": rehearsal.execute("rollback"); ev("explicit ROLLBACK sent")
        except Exception as e: fail(f"ROLLBACK failed: {e!r}")
    except SystemExit:
        pass
    except Exception as e:
        fail(f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        try:
            if rehearsal is not None: rehearsal.close()
        except Exception: pass

    # independent observer: the rehearsal session must be gone, nothing may remain
    if obs is not None and "snapshot_before" in rep:      # refused before the first snapshot: nothing of ours to verify afterwards
        try:
            t1 = time.time(); gone = False
            while time.time() - t1 < a.idle_tx_timeout_ms / 1000 + 4:
                if obs.execute("select count(*) from pg_stat_activity where application_name='task197_rehearsal'").fetchone()[0] == 0: gone = True; break
                time.sleep(0.1)
            rep["rehearsal_session_gone_after_s"] = round(time.time() - t1, 2)
            if not gone: fail("the rehearsal session is still present (possible lock holder): manual review, do NOT run cleanup DDL")
            post = snapshot(obs, Path(a.snapshot).read_text(encoding="utf-8")); rep["snapshot_after"] = post
            for k, v in post.items():
                if k.startswith("INV_") and v != rep.get("snapshot_before", {}).get(k): fail(f"invariant changed: {k} {rep['snapshot_before'].get(k)} -> {v}")
            for k in ("INV_gate_functions", "INV_gate_triggers", "INV_fixture_tasks", "INV_fixture_events", "INV_gate_events", "INV_rehearsal_correction_log"):
                if post.get(k) != "0": fail(f"leftover detected: {k}={post.get(k)} (committed objects?) - manual review, no automatic cleanup")
            seq = int(post["OBS_event_seq_last"]) - int(rep["snapshot_before"]["OBS_event_seq_last"]); rep["observed_event_seq_delta_noisy"] = seq
        except Exception as e:
            fail(f"post-run observer check failed: {e!r}")
        finally:
            obs.close()

    # strict verdict
    if sentinel:
        rep["sentinel"] = sentinel
        s = sentinel
        checks = [(s["result"] == "PASS", f"result={s['result']}"),
                  (int(s["checks_ok"]) == int(s["checks_total"]) == expect["checks"], f"checks {s['checks_ok']}/{s['checks_total']} (expected {expect['checks']}/{expect['checks']})"),
                  (int(s["scn_ok"]) == int(s["scn"]) == expect["scenarios"], f"scenarios {s['scn_ok']}/{s['scn']}"),
                  (int(s["replay_ok"]) == expect["replay_ok"] and int(s["replay_drift"]) == 0, f"replay_ok={s['replay_ok']} replay_drift={s['replay_drift']}"),
                  (s["gate_sql_md5"] == expect["gate_sql_md5"], "embedded gate md5"),
                  (int(s["lock_timeout_ms"]) <= a.lock_timeout_ms, f"lock_timeout in force {s['lock_timeout_ms']} ms (caller {s['lock_timeout_caller_ms']} ms)"),
                  (int(s["statement_timeout_ms"]) == a.statement_timeout_ms, f"statement_timeout seen inside the DO {s['statement_timeout_ms']} ms")]
        for ok, what in checks:
            if not ok: fail("strict verdict: " + what)
        rep["strict_checks"] = [("ok " if ok else "BAD ") + what for ok, what in checks]
    else:
        rep["reasons"].append("no sentinel")
    ev("finished")
    finish(0 if not rep["reasons"] and sentinel else 2)


if __name__ == "__main__":
    main()
