#!/usr/bin/env python3
"""TASK#214 read-only statistics snapshot collector (design: docs/design/task214_amplification_v1.md section 4, MAGI #5720).

NOTHING HERE RUNS AGAINST PRODUCTION UNLESS MAGI HAS SAID "수집 시작".  Default mode is a DRY RUN: it prints the plan and the exact SQL and
opens no connection and does not even read the DSN variable.

    python3 task214_snapshot.py --phase pre --out-dir D                                    # dry run (default)
    TASK214_DSN=... python3 task214_snapshot.py --phase pre --out-dir D --live --approval A.json --approval-pubkey P.pem   # MAGI-signed approval

Phases (one snapshot each):  pre | during1 | during2 | post_immediate | post_normal
Profiles: 'full' (pre, post_immediate, post_normal) and 'light' (during1, during2: fewer, cheaper queries, shorter limits).

Safety properties (each one is tested in task214_snapshot_selftest.py):
  * read only: one connection, one READ ONLY transaction, only the fixed SELECTs below; SET LOCAL statement/lock/idle timeouts; the effective
    values and transaction_read_only='on' are read back and the run aborts before any statistics query if they differ.  No writes, no DDL, no
    pg_stat_reset*, no setting changes beyond SET LOCAL in that one transaction.
  * no extra load while the database may be stalled: ONE connection attempt (libpq connect_timeout, tcp_user_timeout, keepalives), queries run
    one after another, never in parallel; the FIRST query timeout or connection loss ends the run (remaining sections are listed as skipped);
    a watchdog thread writes what exists and exits the process at a hard deadline even if a socket call never returns.  There is NO retry
    inside the tool, and an attempt marker (.attempt_<phase>) makes a second attempt for the same phase refuse unless --allow-repeat.
  * a failed connection, a timeout and a watchdog stop are RESULTS: they are written to the snapshot file with start time, elapsed time,
    error class and SQLSTATE (scrubbed), exit code 3.  They are not retried and not hidden.
  * the DSN and the credentials, host names and IP addresses in error texts never reach stdout, stderr or the file (scrubbed).
  * output: <out-dir>/task214_snap_<phase>_<UTC time>.json (0600, atomic rename) and a .sha256 beside it.

Execution gate (v2, CASSANDRA C214-79-03): --live needs an Ed25519-signed approval (--approval, --approval-pubkey) made by MAGI's key.  The tool
checks, before the DSN is read: the key's SHA-256 equals TRUST_ANCHOR_PUBKEY_SHA256 (shipped as None => live runs are impossible until MAGI sets
it, which changes this file's hash), the signature, that THIS file's sha256 equals the one the approval names (recorded SHA), phase, time window
(<= 6 h), the registry event number and the expected DB role.  There is no self-issued switch: --auth-event and --allow-repeat no longer exist; a
second attempt for a phase is possible only if the SIGNED approval lists it in repeat_phases.  After connecting, the session role must equal the
approved db_role and must not be a superuser/createrole/createdb/replication/bypassrls role unless the signed approval says allow_privileged_role.
Stated limits: a person who can edit this file or run arbitrary Python can bypass any check inside it (the trust anchor is a constant in the file);
the mitigation is procedural: MAGI compares the file's sha256 with the one it signed, and the DB role is read-only on the server side.  track_io_timing is off on the production server (CASSANDRA #5715), so the *_time columns of pg_stat_io
will be 0: counts and bytes are measurable, timing is not, and this tool does not change the setting.  Exit codes: 0 ok, 2 refused/usage, 3 a
measurement failure was recorded.
"""
import argparse, base64, hashlib, json, os, re, sys, threading, time, uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCHEMA = "task214-snapshot-v2"
SCHEMAS_OK = ("task214-snapshot-v1", "task214-snapshot-v2")
APPROVAL_SCHEMA = "task214-snapshot-approval-v1"
TRUST_ANCHOR_PUBKEY_SHA256 = None      # MAGI sets the SHA-256 (hex) of the raw Ed25519 public key it holds; None => no live run is possible
MAX_APPROVAL_WINDOW = timedelta(hours=6)
KST = timezone(timedelta(hours=9))
PHASES = ("pre", "during1", "during2", "post_immediate", "post_normal")
LIGHT_PHASES = ("during1", "during2")
PROFILES = {"light": {"statement_timeout_ms": 3000, "deadline_s": 25},
            "full": {"statement_timeout_ms": 8000, "deadline_s": 60}}
LOCK_TIMEOUT_MS = 500
IDLE_TX_MS = 10000
DEFAULT_CONNECT_TIMEOUT = 10

# ---- the ONLY SQL this tool runs.  Every statement is a single SELECT (the self-test enforces it).  profile: which profiles include it.
SECTIONS = [
    ("meta", ("light", "full"),
     "select now() as server_now, clock_timestamp() as server_clock, pg_postmaster_start_time() as postmaster_start, pg_is_in_recovery() as in_recovery, "
     "version() as version, current_setting('track_io_timing') as track_io_timing, current_setting('stats_fetch_consistency') as stats_fetch_consistency"),
    ("io", ("light", "full"), "select coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) from pg_stat_io t"),
    ("checkpointer", ("light", "full"), "select to_jsonb(t) from pg_stat_checkpointer t"),
    ("bgwriter", ("light", "full"), "select to_jsonb(t) from pg_stat_bgwriter t"),
    ("wal", ("light", "full"), "select to_jsonb(t) from pg_stat_wal t"),
    ("database", ("light", "full"), "select coalesce(jsonb_agg(to_jsonb(t)), '[]'::jsonb) from pg_stat_database t"),
    ("activity", ("light", "full"),
     "select coalesce(jsonb_agg(to_jsonb(a)), '[]'::jsonb) from (select backend_type, application_name, state, wait_event_type, count(*) as n, "
     "max(extract(epoch from now() - xact_start))::int as max_xact_age_s from pg_stat_activity group by 1, 2, 3, 4 order by 1, 2, 3, 4) a"),
    ("statements_info", ("full",), "select to_jsonb(i) from pg_stat_statements_info i"),
    ("statements_top", ("full",),
     "with s as (select dbid, userid, queryid, toplevel, calls, total_exec_time, rows, shared_blks_hit, shared_blks_read, shared_blks_dirtied, shared_blks_written, "
     "left(query, 120) as q from pg_stat_statements), "
     "t as (select * from s order by total_exec_time desc limit 20), c as (select * from s order by calls desc limit 20), "
     "r as (select * from s order by rows desc limit 20) "
     "select coalesce(jsonb_agg(to_jsonb(u)), '[]'::jsonb) from (select * from t union select * from c union select * from r) u"),
    ("statements_totals", ("full",),
     "select to_jsonb(x) from (select count(*) as n_entries, coalesce(sum(calls), 0) as sum_calls, coalesce(sum(total_exec_time), 0) as sum_total_exec_time, "
     "coalesce(sum(rows), 0) as sum_rows from pg_stat_statements) x"),
    ("user_tables", ("full",),
     "select coalesce(jsonb_agg(to_jsonb(u)), '[]'::jsonb) from (select schemaname, relname, seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del, "
     "n_live_tup, n_dead_tup, vacuum_count, autovacuum_count, analyze_count, autoanalyze_count from pg_stat_user_tables) u"),
]
GUARD_SQL = ("select current_setting('transaction_read_only'), "
             "(select setting from pg_settings where name = 'statement_timeout'), "
             "(select setting from pg_settings where name = 'lock_timeout'), "
             "(select setting from pg_settings where name = 'idle_in_transaction_session_timeout'), "
             "(select rolsuper or rolcreaterole or rolcreatedb or rolreplication or rolbypassrls from pg_roles where rolname = current_user), "
             "current_user::text")

_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}\b")
_HOST_RE = re.compile(r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:supabase\.(?:co|com|net)|pooler\.supabase\.com|invalid|local|internal)\b", re.I)


def make_scrubber(dsn_text, info):
    secrets = [dsn_text] + [str(v) for k, v in (info or {}).items() if k in ("host", "hostaddr", "user", "password", "service", "passfile") and v and len(str(v)) >= 3]
    secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def scrub(text):
        t = str(text)
        for s in secrets:
            t = t.replace(s, "<redacted>")
        t = _HOST_RE.sub("<host>", t)
        t = _IP_RE.sub("<ip>", t)
        return t
    return scrub


def now_utc():
    return datetime.now(timezone.utc)


def iso(d):
    return d.astimezone(timezone.utc).isoformat(timespec="milliseconds")


class Writer:
    """Finalises the snapshot exactly once (main thread or watchdog), atomically."""
    def __init__(self, out_dir, phase, state):
        self.out_dir, self.phase, self.state = Path(out_dir), phase, state
        self.lock = threading.Lock()
        self.done = None

    def finalize(self, outcome, code):
        with self.lock:
            if self.done is not None:
                return self.done
            st = self.state
            end = now_utc()
            st["outcome"] = outcome
            st["t_end_utc"] = iso(end)
            st["elapsed_ms"] = int((end - st["_t0"]).total_seconds() * 1000)
            body = {k: v for k, v in st.items() if not k.startswith("_")}
            data = (json.dumps(body, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n").encode("utf-8")
            name = f"task214_snap_{self.phase}_{end.strftime('%Y%m%dT%H%M%S') + f'{end.microsecond // 1000:03d}Z'}.json"
            final, tmp = self.out_dir / name, self.out_dir / ("." + name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, data); os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, final)
            (self.out_dir / (name + ".sha256")).write_text(hashlib.sha256(data).hexdigest() + "  " + name + "\n")
            self.done = (str(final), code)
            return self.done


class Abort(Exception):
    def __init__(self, outcome):
        self.outcome = outcome


def _canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _utc(x, name):
    try:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    except Exception:
        raise ValueError(f"{name} is not an ISO time")
    if d.tzinfo is None:
        raise ValueError(f"{name} has no timezone")
    return d.astimezone(timezone.utc)


def verify_approval(approval, pubkey_pem, *, phase, tool_sha256, now, anchor):
    """Returns the verified payload or raises ValueError(reason).  Pure function (no I/O) so the self-test can drive every branch."""
    if not anchor:
        raise ValueError("no trust anchor in this tool (TRUST_ANCHOR_PUBKEY_SHA256 is None): MAGI must set it before any live run")
    from cryptography.hazmat.primitives.serialization import load_pem_public_key, Encoding, PublicFormat
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        pub = load_pem_public_key(pubkey_pem)
    except Exception:
        raise ValueError("public key is not a PEM public key")
    if not isinstance(pub, Ed25519PublicKey):
        raise ValueError("public key is not Ed25519")
    fp = hashlib.sha256(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).hexdigest()
    if fp != anchor:
        raise ValueError("public key does not match the trust anchor shipped in this tool")
    if not isinstance(approval, dict) or set(approval) != {"payload", "signature_b64"} or not isinstance(approval.get("payload"), dict):
        raise ValueError("approval file has the wrong shape")
    try:
        sig = base64.b64decode(approval["signature_b64"], validate=True)
        pub.verify(sig, _canon(approval["payload"]))
    except (InvalidSignature, ValueError, TypeError):
        raise ValueError("signature does not verify")
    p = approval["payload"]
    need = {"schema", "approval_id", "magi_event", "tool_sha256", "phases", "repeat_phases", "not_before", "not_after", "operator", "db_role", "allow_privileged_role"}
    if set(p) != need:
        raise ValueError("payload fields differ from the schema: " + ",".join(sorted(set(p) ^ need)))
    if p["schema"] != APPROVAL_SCHEMA:
        raise ValueError("unknown approval schema")
    if not isinstance(p["approval_id"], str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", p["approval_id"]):
        raise ValueError("approval_id is not a uuid")
    if not isinstance(p["magi_event"], int) or isinstance(p["magi_event"], bool) or p["magi_event"] <= 0:
        raise ValueError("magi_event must be a positive integer")
    if p["tool_sha256"] != tool_sha256:
        raise ValueError("this tool file's sha256 is not the one the approval names (recorded SHA mismatch)")
    if not (isinstance(p["phases"], list) and p["phases"] and all(x in PHASES for x in p["phases"])) or phase not in p["phases"]:
        raise ValueError("phase is not approved")
    if not (isinstance(p["repeat_phases"], list) and all(x in p["phases"] for x in p["repeat_phases"])):
        raise ValueError("repeat_phases must be a subset of phases")
    if not isinstance(p["operator"], str) or not p["operator"].strip() or not isinstance(p["db_role"], str) or not p["db_role"].strip() or not isinstance(p["allow_privileged_role"], bool):
        raise ValueError("operator/db_role/allow_privileged_role malformed")
    nb, na = _utc(p["not_before"], "not_before"), _utc(p["not_after"], "not_after")
    if na <= nb or na - nb > MAX_APPROVAL_WINDOW:
        raise ValueError("approval window is empty or longer than 6 hours")
    if not (nb <= now <= na):
        raise ValueError("outside the approved time window")
    return p


def claim_attempt(out, phase, payload):
    """Exclusive markers in the output directory.  First attempt: .attempt_<phase>.  A second one only if the SIGNED approval lists the phase
    in repeat_phases (marker .attempt_<phase>.2); never a third.  The approval id is stamped into the marker."""
    stamp = (iso(now_utc()) + " " + payload["approval_id"] + "\n").encode()
    for name in (f".attempt_{phase}", f".attempt_{phase}.2"):
        if name.endswith(".2") and phase not in payload["repeat_phases"]:
            return False, f"phase {phase} was already attempted here; the signed approval does not allow a repeat"
        try:
            fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, stamp); os.close(fd)
            return True, name
        except FileExistsError:
            continue
    return False, f"phase {phase} was already attempted twice here; no further attempt"


def open_readonly(dsn, *, connect_timeout, application_name, statement_timeout_ms):
    """One connection, one READ ONLY transaction with SET LOCAL limits.  Returns the connection (transaction open)."""
    import psycopg
    conn = psycopg.connect(dsn, connect_timeout=connect_timeout, application_name=application_name, prepare_threshold=None,
                           tcp_user_timeout=10000, keepalives=1, keepalives_idle=5, keepalives_interval=5, keepalives_count=2)
    conn.read_only = True
    conn.execute(f"set local statement_timeout = {int(statement_timeout_ms)}")
    conn.execute(f"set local lock_timeout = {LOCK_TIMEOUT_MS}")
    conn.execute(f"set local idle_in_transaction_session_timeout = {IDLE_TX_MS}")
    return conn


def collect(args, dsn, info, scrub, writer, state):
    import psycopg
    prof = PROFILES[state["profile"]]
    t = time.monotonic()
    try:
        conn = open_readonly(dsn, connect_timeout=args.connect_timeout, application_name=f"task214_snapshot_{args.phase}", statement_timeout_ms=prof["statement_timeout_ms"])
    except BaseException as e:   # connection failure IS a measurement
        state["connect"] = {"ok": False, "ms": int((time.monotonic() - t) * 1000), "error_class": type(e).__name__,
                            "sqlstate": getattr(e, "sqlstate", None), "message": scrub(e)[:400]}
        return writer.finalize("connect_failed", 3)
    state["connect"] = {"ok": True, "ms": int((time.monotonic() - t) * 1000)}
    try:
        g = conn.execute(GUARD_SQL).fetchone()
        eff = {"transaction_read_only": g[0], "statement_timeout_ms": g[1], "lock_timeout_ms": g[2], "idle_in_transaction_session_timeout_ms": g[3]}
        state["effective_settings"] = eff
        appr = state.get("_approval") or {}
        state["role_check"] = {"matches_approved_role": g[5] == appr.get("db_role"), "privileged_role": bool(g[4]), "privileged_allowed": bool(appr.get("allow_privileged_role"))}
        if not state["role_check"]["matches_approved_role"] or (g[4] and not appr.get("allow_privileged_role")):
            state["skipped"] = [s[0] for s in SECTIONS if state["profile"] in s[1]]
            state["guard_note"] = "session role differs from the approved db_role, or is a privileged role that the signed approval does not allow"
            return writer.finalize("guard_failed", 3)
        if g[0] != "on" or g[1] != str(prof["statement_timeout_ms"]) or g[2] != str(LOCK_TIMEOUT_MS) or g[3] != str(IDLE_TX_MS):
            state["skipped"] = [s[0] for s in SECTIONS if state["profile"] in s[1]]
            return writer.finalize("guard_failed", 3)
        for i, (name, profs, sql) in enumerate(SECTIONS):
            if state["profile"] not in profs:
                continue
            t1 = time.monotonic()
            try:
                with conn.transaction():
                    row = conn.execute(sql).fetchone()
                state["sections"][name] = {"ok": True, "ms": int((time.monotonic() - t1) * 1000), "data": row_to_obj(row, name)}
            except psycopg.errors.QueryCanceled as e:
                state["sections"][name] = {"ok": False, "ms": int((time.monotonic() - t1) * 1000), "error_class": "QueryCanceled", "sqlstate": e.sqlstate, "message": scrub(e)[:300]}
                state["skipped"] = [s[0] for s in SECTIONS[i + 1:] if state["profile"] in s[1]]
                return writer.finalize("query_timeout", 3)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                state["sections"][name] = {"ok": False, "ms": int((time.monotonic() - t1) * 1000), "error_class": type(e).__name__, "sqlstate": getattr(e, "sqlstate", None), "message": scrub(e)[:300]}
                state["skipped"] = [s[0] for s in SECTIONS[i + 1:] if state["profile"] in s[1]]
                return writer.finalize("connection_lost", 3)
            except psycopg.Error as e:   # e.g. extension not installed / no privilege: unavailable, not an outage
                state["sections"][name] = {"ok": False, "unavailable": True, "ms": int((time.monotonic() - t1) * 1000), "error_class": type(e).__name__, "sqlstate": getattr(e, "sqlstate", None), "message": scrub(e)[:300]}
        bad = [n for n, s in state["sections"].items() if not s["ok"] and not s.get("unavailable")]
        return writer.finalize("ok" if not bad else "partial", 0 if not bad else 3)
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


def row_to_obj(row, name):
    if name == "meta":
        keys = ["server_now", "server_clock", "postmaster_start", "in_recovery", "version", "track_io_timing", "stats_fetch_consistency"]
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in zip(keys, row)}
    return row[0]


def plan_text(args, profile):
    p = PROFILES[profile]
    lines = [f"DRY RUN (no connection, DSN variable not read).  phase={args.phase} profile={profile}",
             f"limits: connect_timeout={args.connect_timeout}s statement_timeout={p['statement_timeout_ms']}ms lock_timeout={LOCK_TIMEOUT_MS}ms idle_tx={IDLE_TX_MS}ms hard deadline={args.deadline_s or p['deadline_s']}s; one attempt, no retry",
             f"output: {args.out_dir}/task214_snap_{args.phase}_<UTC>.json (+ .sha256); attempt marker .attempt_{args.phase}",
             "SQL that would run, in order, inside ONE READ ONLY transaction:", "  " + GUARD_SQL]
    for name, profs, sql in SECTIONS:
        if profile in profs:
            lines.append(f"  [{name}] {sql}")
    lines.append("To run for real: set TASK214_DSN, add  --live --approval A.json --approval-pubkey P.pem  (MAGI-signed; there is no self-issued switch).  Do NOT run before MAGI says 수집 시작.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=PHASES)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--profile", choices=tuple(PROFILES), default=None, help="default: light for during1/during2, full otherwise (override is recorded)")
    ap.add_argument("--planned-at", default="", help="the scheduled time label for this snapshot (free text, recorded)")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--approval", default=None, help="MAGI-signed approval (json); required with --live")
    ap.add_argument("--approval-pubkey", default=None, help="MAGI's Ed25519 public key (PEM); its SHA-256 must equal the trust anchor in this tool")
    ap.add_argument("--dsn-env", default="TASK214_DSN")
    ap.add_argument("--connect-timeout", type=int, default=DEFAULT_CONNECT_TIMEOUT)
    ap.add_argument("--deadline-s", type=int, default=0, help="hard deadline override (5..120); default per profile")
    a = ap.parse_args(argv)
    profile = a.profile or ("light" if a.phase in LIGHT_PHASES else "full")
    if not (1 <= a.connect_timeout <= 30) or (a.deadline_s and not (5 <= a.deadline_s <= 120)):
        print("refused: connect-timeout must be 1..30 s, deadline 5..120 s", file=sys.stderr); return 2
    if not a.live:
        print(plan_text(a, profile)); return 0
    if not a.approval or not a.approval_pubkey:
        print("refused: --live needs --approval <MAGI-signed approval json> and --approval-pubkey <PEM>", file=sys.stderr); return 2
    tool_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        approval = json.loads(Path(a.approval).read_text(encoding="utf-8"))
        payload = verify_approval(approval, Path(a.approval_pubkey).read_bytes(), phase=a.phase, tool_sha256=tool_sha, now=now_utc(), anchor=TRUST_ANCHOR_PUBKEY_SHA256)
    except (ValueError, OSError) as e:
        print(f"refused: approval not accepted: {e}", file=sys.stderr); return 2
    dsn = os.environ.get(a.dsn_env, "")
    if not dsn:
        print(f"refused: environment variable {a.dsn_env} is not set", file=sys.stderr); return 2
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    import psycopg
    try:
        info = psycopg.conninfo.conninfo_to_dict(dsn)
    except Exception:
        print("refused: the DSN is not parseable (not shown)", file=sys.stderr); return 2
    scrub = make_scrubber(dsn, info)
    ok, why = claim_attempt(out, a.phase, payload)
    if not ok:
        print(f"refused: {why}", file=sys.stderr); return 2
    t0 = now_utc()
    state = {"schema": SCHEMA, "phase": a.phase, "profile": profile, "profile_overridden": bool(a.profile and a.profile != ("light" if a.phase in LIGHT_PHASES else "full")),
             "planned_at": a.planned_at, "auth_event": payload["magi_event"], "approval_id": payload["approval_id"], "operator": payload["operator"], "tool_sha256": tool_sha, "_approval": payload,
             "t_start_utc": iso(t0), "t_start_kst": t0.astimezone(KST).isoformat(timespec="milliseconds"),
             "limits": {"connect_timeout_s": a.connect_timeout, "statement_timeout_ms": PROFILES[profile]["statement_timeout_ms"], "lock_timeout_ms": LOCK_TIMEOUT_MS,
                        "idle_tx_ms": IDLE_TX_MS, "deadline_s": a.deadline_s or PROFILES[profile]["deadline_s"]},
             "connect": None, "effective_settings": None, "sections": {}, "skipped": [], "_t0": t0}
    writer = Writer(out, a.phase, state)

    def watchdog():
        state["watchdog_note"] = "hard deadline reached while a call did not return; partial data written by the watchdog"
        done = writer.finalize("watchdog", 3)
        print(f"watchdog: {done[0]}", file=sys.stderr, flush=True)
        os._exit(3)
    timer = threading.Timer(state["limits"]["deadline_s"], watchdog)
    timer.daemon = True
    timer.start()
    try:
        path, code = collect(a, dsn, info, scrub, writer, state)
    except BaseException as e:
        state["unexpected"] = scrub(f"{type(e).__name__}: {e}")[:300]
        path, code = writer.finalize("tool_error", 3)
    finally:
        timer.cancel()
    print(json.dumps({"snapshot": path, "outcome": state["outcome"], "exit": code}))
    return code


if __name__ == "__main__":
    sys.exit(main())
