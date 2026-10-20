#!/usr/bin/env python3
"""Self-test of the TASK#214 snapshot tools against a THROWAWAY local PostgreSQL (socket dir in PGHOST, superuser in PGSUPER).  Never run it
against anything else; it only ever connects to that local server (directly or through an in-process TCP proxy that can stall or cut it).

    PGHOST=/tmp/pg17x PGSUPER=supabase_admin python3 tools/task214/task214_snapshot_selftest.py
(PGHOST may also be a TCP host with PGPORT and PGPASSWORD, as in CI.)
"""
import base64, importlib.util, json, os, re, signal, socket, stat, subprocess, sys, tempfile, threading, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL_SHIPPED, DIFF = HERE / "task214_snapshot.py", HERE / "task214_snapshot_diff.py"
TOOL = TOOL_SHIPPED   # replaced below by an anchored COPY (the shipped file has no trust anchor and cannot run live)
SOCK, SUP = os.environ["PGHOST"], os.environ.get("PGSUPER", "supabase_admin")
import psycopg
results = []
PW = os.environ.get("PGPASSWORD") or "S3cretPw!"   # the proxy DSN must authenticate against the throwaway server (trust on the socket, md5 in CI)


def check(name, ok, info=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)


def load_mod():
    spec = importlib.util.spec_from_file_location("t214", TOOL); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def run(args, dsn=None, timeout=90, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k != "TASK214_DSN"}
    if dsn is not None: env["TASK214_DSN"] = dsn
    env.update(env_extra or {})
    t = time.time()
    p = subprocess.run([sys.executable, "-I", str(TOOL)] + args, capture_output=True, text=True, env=env, timeout=timeout)
    return p.returncode, p.stdout, p.stderr, time.time() - t


class Proxy:
    """TCP listener in front of the local unix socket.  mode: forward | close | silent | freeze (forward, then stop after N server bytes)."""
    def __init__(self, mode="forward", freeze_after=0):
        self.mode, self.freeze_after, self.count, self.stop = mode, freeze_after, 0, False
        self.ls = socket.socket(); self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); self.ls.bind(("127.0.0.1", 0)); self.ls.listen(20)
        self.port = self.ls.getsockname()[1]; self.conns = []
        threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        while not self.stop:
            try: c, _ = self.ls.accept()
            except OSError: return
            self.count += 1; self.conns.append(c)
            if self.mode == "close": c.close(); continue
            if self.mode == "silent": continue
            if SOCK.startswith("/"):
                u = socket.socket(socket.AF_UNIX); u.connect(f"{SOCK}/.s.PGSQL.5432")
            else:
                u = socket.create_connection((SOCK, int(os.environ.get("PGPORT", "5432"))))
            self.conns.append(u)
            sent = [0]
            def pump(a, b, count):
                try:
                    while True:
                        d = a.recv(65536)
                        if not d: break
                        if count:
                            if self.mode == "freeze" and sent[0] >= self.freeze_after:
                                while not self.stop: time.sleep(0.2)
                                return
                            sent[0] += len(d)
                        b.sendall(d)
                except OSError: pass
            threading.Thread(target=pump, args=(c, u, False), daemon=True).start()
            threading.Thread(target=pump, args=(u, c, True), daemon=True).start()
    def close(self):
        self.stop = True
        for c in self.conns:
            try: c.close()
            except OSError: pass
        self.ls.close()
    def dsn(self, extra=""):
        return f"host=127.0.0.1 port={self.port} dbname=postgres user={SUP} password={PW} sslmode=disable {extra}"

DIRECT = f"host={SOCK} port={os.environ.get('PGPORT', '5432')} dbname=postgres user={SUP}" + (f" password={os.environ['PGPASSWORD']} sslmode=disable" if os.environ.get("PGPASSWORD") else "")
tmp = Path(tempfile.mkdtemp(prefix="t214_"))
# ---- approval infrastructure: the shipped tool has TRUST_ANCHOR_PUBKEY_SHA256 = None.  The test builds an ANCHORED COPY (anchor patched into the text,
# so the copy has its own sha256) and signs approvals with a throwaway key.  The shipped code has no override switch.
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
import hashlib
def newkey():
    k = Ed25519PrivateKey.generate(); raw = k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return k, hashlib.sha256(raw).hexdigest(), k.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
KEY, ANCHOR, PUBPEM = newkey()
KEY2, ANCHOR2, PUBPEM2 = newkey()
src = TOOL_SHIPPED.read_text()
assert src.count("TRUST_ANCHOR_PUBKEY_SHA256 = None") == 1
TOOLA = tmp0 = Path(tempfile.mkdtemp(prefix="t214a_")) / "task214_snapshot.py"
TOOLA.write_text(src.replace("TRUST_ANCHOR_PUBKEY_SHA256 = None", f'TRUST_ANCHOR_PUBKEY_SHA256 = "{ANCHOR}"'))
TOOLA_SHA = hashlib.sha256(TOOLA.read_bytes()).hexdigest()
def canon(o): return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
def mkpayload(**over):
    now = datetime.now(timezone.utc)
    p = {"schema": "task214-snapshot-approval-v1", "approval_id": str(uuid.uuid4()), "magi_event": 5999, "tool_sha256": TOOLA_SHA,
         "phases": ["pre", "during1", "during2", "post_immediate", "post_normal"], "repeat_phases": [], "not_before": (now - timedelta(minutes=2)).isoformat(),
         "not_after": (now + timedelta(hours=5)).isoformat(), "operator": "test-operator", "db_role": SUP, "allow_privileged_role": True}
    p.update(over); return p
def sign_with(k, payload): return {"payload": payload, "signature_b64": base64.b64encode(k.sign(canon(payload))).decode()}
_n = [0]
def approval_args(payload=None, key=None, pem=None, **over):
    """write an approval + pubkey file and return the CLI args for them"""
    _n[0] += 1; d = Path(tempfile.mkdtemp(prefix="t214ap_"))
    ap = sign_with(key or KEY, payload if payload is not None else mkpayload(**over))
    (d / "a.json").write_text(json.dumps(ap)); (d / "k.pem").write_bytes(pem or PUBPEM)
    return ["--approval", str(d / "a.json"), "--approval-pubkey", str(d / "k.pem")]
AP = approval_args()                                    # master approval: all phases, no repeat
APR = approval_args(repeat_phases=["pre"])              # allows ONE repeat of 'pre'
TOOL = TOOLA                                            # every live run below uses the anchored copy
def snaps(d): return sorted(p for p in Path(d).glob("task214_snap_*.json"))
def leak(*texts, extra=()):
    blob = "\n".join(texts)
    return [s for s in ("S3cretPw!", PW, "abcdefghijklmnop", "127.0.0.1", *extra) if s in blob]

# --- S: the SQL itself is read-only by construction
m = load_mod()
FORBID = re.compile(r"\b(insert|update|delete|create|alter|drop|truncate|grant|revoke|vacuum|analyze|copy|call|do|lock|listen|notify|refresh|reindex|cluster|checkpoint|set)\b", re.I)
sqls = [(n, s) for n, _, s in m.SECTIONS] + [("guard", m.GUARD_SQL)]
bad = [n for n, s in sqls if not re.match(r"\s*(select|with)\b", s, re.I) or ";" in s or FORBID.search(s) or "pg_stat_reset" in s.lower() or "pg_terminate" in s.lower() or "pg_cancel" in s.lower()]
check("S1 every SQL the tool can run is one plain SELECT/WITH: no ';', no write/DDL/lock/reset/terminate keywords", not bad, f"{len(sqls)} statements; bad={bad}")
stt = dict((n, s_) for n, _, s_ in m.SECTIONS)["statements_top"]
check("S3 statements_top selects the full pg_stat_statements identity (dbid, userid, queryid, toplevel), not queryid alone", all(re.search(rf"\b{k}\b", stt.split(" from pg_stat_statements")[0]) for k in ("dbid", "userid", "queryid", "toplevel")))
c = m.open_readonly(DIRECT, connect_timeout=5, application_name="t214_ro", statement_timeout_ms=3000)
try:
    c.execute("create temp table t214_x(i int)"); wrote = True
except psycopg.errors.ReadOnlySqlTransaction: wrote = False
c.rollback(); c.close()
check("S2 the transaction the tool opens refuses a write (ReadOnlySqlTransaction)", not wrote)

# --- D: dry run and refusals open no connection
sent = "SENTINEL_DSN_VALUE_9f3"
rc, o, e, _ = run(["--phase", "during1", "--out-dir", str(tmp / "dry")], dsn=sent)
check("D1 dry run (default): exit 0, prints plan + SQL, DSN value never printed, no output dir/file created, DSN not read", rc == 0 and "DRY RUN" in o and "[io]" in o and sent not in o + e and not (tmp / "dry").exists())
rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(tmp / "r1"), "--live"], dsn=DIRECT)
check("D2 --live without an approval refused (exit 2), nothing created", rc == 2 and not (tmp / "r1").exists())
rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(tmp / "r2"), "--live", *AP])
check("D3 --live without the DSN variable refused (exit 2), nothing connected", rc == 2 and not snaps(tmp / "r2"))
rc, o, e, _ = run(["--phase", "bogus", "--out-dir", str(tmp / "r3")], dsn=DIRECT)
check("D4 unknown phase refused by the parser (exit 2)", rc == 2)
rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(tmp / "r4"), "--connect-timeout", "999"], dsn=DIRECT)
check("D5 out-of-range timeouts refused (exit 2)", rc == 2)
rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(tmp / "r5"), "--live", *AP], dsn="not a dsn at all =")
check("D6 unparseable DSN refused without echoing it", rc == 2 and "not a dsn" not in o + e)

# --- F: full / light live runs on the throwaway server
fdir = tmp / "full"
rc, o, e, el = run(["--phase", "pre", "--out-dir", str(fdir), "--live", *AP, "--planned-at", "test"], dsn=DIRECT)
sn = snaps(fdir); doc = json.loads(sn[0].read_text()) if sn else {}
check("F1 full run: exit 0, outcome ok, one file + sha256, mode 0600", rc == 0 and doc.get("outcome") == "ok" and len(sn) == 1 and stat.S_IMODE(sn[0].stat().st_mode) == 0o600
      and (sn[0].parent / (sn[0].name + ".sha256")).exists(), f"rc={rc} {o.strip()[:120]}")
import hashlib
check("F2 .sha256 matches the file", sn and (sn[0].parent / (sn[0].name + ".sha256")).read_text().split()[0] == hashlib.sha256(sn[0].read_bytes()).hexdigest())
eff = doc.get("effective_settings") or {}
check("F3 effective settings read back: read-only on, statement 8000, lock 500, idle 10000", eff == {"transaction_read_only": "on", "statement_timeout_ms": "8000", "lock_timeout_ms": "500", "idle_in_transaction_session_timeout_ms": "10000"}, str(eff))
S = doc.get("sections", {})
check("F4 full profile has io/checkpointer/bgwriter/wal/database/activity/user_tables, meta has server time + track_io_timing", all(S.get(k, {}).get("ok") for k in ("meta", "io", "checkpointer", "bgwriter", "wal", "database", "activity", "user_tables")) and S["meta"]["data"].get("track_io_timing") in ("on", "off"))
check("F5 an extension that is not installed (pg_stat_statements) is recorded as unavailable, not as an outage: outcome stays ok", S.get("statements_top", {}).get("unavailable") is True and doc.get("outcome") == "ok", str(S.get("statements_top", {}).get("sqlstate")))
check("F6 the snapshot contains no user names or query text beyond the 120-char normalised statements", "usename" not in json.dumps(doc) and SUP not in json.dumps(S.get("activity")) and SUP not in json.dumps(S.get("meta")))   # (a database may legitimately be named like the role, e.g. the CI image)
rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(fdir), "--live", *AP], dsn=DIRECT)
check("F7 second attempt for the same phase refused (exit 2), no new snapshot (no automatic retry)", rc == 2 and len(snaps(fdir)) == 1)
rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(fdir), "--live", *APR], dsn=DIRECT)
check("F8 a repeat is possible only when the SIGNED approval lists the phase in repeat_phases", rc == 0 and len(snaps(fdir)) == 2)
rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(fdir), "--live", *approval_args(repeat_phases=["pre"])], dsn=DIRECT)
check("F8b a third attempt is refused even with a repeat-allowing approval", rc == 2 and len(snaps(fdir)) == 2)
mk = (fdir / ".attempt_pre").read_text()
check("F8c the attempt marker carries the approval id; the snapshot records approval id, MAGI event, role check (no role name) and the tool sha256",
      re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", mk) is not None and doc.get("auth_event") == 5999 and re.fullmatch(r"[0-9a-f-]{36}", doc.get("approval_id", "")) is not None
      and doc.get("tool_sha256") == TOOLA_SHA and doc.get("role_check", {}).get("matches_approved_role") is True and SUP not in json.dumps(doc.get("role_check")), str(doc.get("role_check")))
ldir = tmp / "light"
rc, o, e, _ = run(["--phase", "during1", "--out-dir", str(ldir), "--live", *AP], dsn=DIRECT)
ld = json.loads(snaps(ldir)[0].read_text()) if snaps(ldir) else {}
check("F9 during phase defaults to the light profile: 3000 ms limit, no statements/user_tables queries", rc == 0 and ld.get("profile") == "light" and set(ld.get("sections", {})) == {"meta", "io", "checkpointer", "bgwriter", "wal", "database", "activity"} and ld["effective_settings"]["statement_timeout_ms"] == "3000")

# --- G: execution gate (CASSANDRA C214-79-03): the approval cannot be self-issued
gd = tmp / "gate"
def gate(name, argsx, dsn=DIRECT, tool=None, ok_refused=True):
    d = gd / name
    global TOOL
    old = TOOL
    if tool is not None: TOOL = tool
    try: rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(d), "--live", *argsx], dsn=dsn)
    finally: TOOL = old
    return rc, o + e, d
rc, txt, d = gate("g1", AP, tool=TOOL_SHIPPED)
check("G1 the SHIPPED tool (trust anchor None) refuses every live run: exit 2, no output dir, no connection", rc == 2 and "trust anchor" in txt and not d.exists(), txt.strip()[:100])
rc, txt, d = gate("g2", approval_args(key=KEY2, pem=PUBPEM2))
check("G2 SELF-ISSUED approval (own key, own pubkey) is refused: the key is not the trust anchor", rc == 2 and "trust anchor" in txt and not d.exists(), txt.strip()[:100])
rc, txt, d = gate("g2b", approval_args(key=KEY2))
check("G2b right pubkey but a signature made with another key is refused", rc == 2 and "signature" in txt and not d.exists(), txt.strip()[:100])
ap = sign_with(KEY, mkpayload()); ap["payload"]["phases"] = ["pre", "during1"]; ap["payload"]["operator"] = "someone-else"
dd = Path(tempfile.mkdtemp(prefix="t214ap_")); (dd / "a.json").write_text(json.dumps(ap)); (dd / "k.pem").write_bytes(PUBPEM)
rc, txt, d = gate("g3", ["--approval", str(dd / "a.json"), "--approval-pubkey", str(dd / "k.pem")])
check("G3 a payload edited after signing is refused", rc == 2 and "signature" in txt and not d.exists())
rc, txt, d = gate("g4", approval_args(tool_sha256="0" * 64))
check("G4 approval that names a different tool sha256 (not the file being run) is refused", rc == 2 and "sha256" in txt and not d.exists())
now = datetime.now(timezone.utc)
rc, txt, d = gate("g5a", approval_args(not_before=(now - timedelta(hours=3)).isoformat(), not_after=(now - timedelta(hours=1)).isoformat()))
rc2, txt2, d2 = gate("g5b", approval_args(not_before=(now + timedelta(hours=1)).isoformat(), not_after=(now + timedelta(hours=2)).isoformat()))
rc3, txt3, d3 = gate("g5c", approval_args(not_before=(now - timedelta(hours=1)).isoformat(), not_after=(now + timedelta(hours=7)).isoformat()))
check("G5 expired, not-yet-valid and longer-than-6h windows are refused", rc == rc2 == rc3 == 2 and "window" in txt + txt2 + txt3 and not (d.exists() or d2.exists() or d3.exists()), (txt + txt2 + txt3).strip()[:140])
rc, txt, d = gate("g6", approval_args(phases=["during1"]))
check("G6 a phase the approval does not list is refused", rc == 2 and "phase" in txt and not d.exists())
rc, txt, d = gate("g6b", approval_args(extra_field=1))
check("G6b an approval with unknown/missing payload fields is refused", rc == 2 and "fields" in txt and not d.exists())
rc, o, e, _ = run(["--phase", "pre", "--out-dir", str(gd / "g7"), "--live", "--auth-event", "5999"], dsn=DIRECT)
rc2, o2, e2, _ = run(["--phase", "pre", "--out-dir", str(gd / "g7"), "--live", *AP, "--allow-repeat"], dsn=DIRECT)
check("G7 the old self-issue switches --auth-event and --allow-repeat no longer exist (parser error, exit 2, nothing created)", rc == 2 and rc2 == 2 and "unrecognized" in e + e2 and not (gd / "g7").exists())
rc, txt, d = gate("g8", approval_args(db_role="some_other_role"))
doc8 = json.loads(snaps(d)[0].read_text()) if snaps(d) else {}
check("G8 connected session role differs from the approved db_role: guard_failed, exit 3, NO statistics section read", rc == 3 and doc8.get("outcome") == "guard_failed" and all(k not in doc8.get("sections", {}) for k in ("io", "wal", "database")) and doc8.get("role_check", {}).get("matches_approved_role") is False, str(doc8.get("role_check")))
rc, txt, d = gate("g9", approval_args(allow_privileged_role=False))
doc9 = json.loads(snaps(d)[0].read_text()) if snaps(d) else {}
check("G9 superuser session with allow_privileged_role=false: guard_failed, exit 3, no statistics read (production must use a read-only role)", rc == 3 and doc9.get("outcome") == "guard_failed" and not doc9.get("sections") and doc9.get("role_check", {}).get("privileged_role") is True, str(doc9.get("role_check")))
ro = "t214_ro_" + uuid.uuid4().hex[:6]
with psycopg.connect(DIRECT, autocommit=True) as cc:
    cc.execute(f"create role {ro} login password 'ro_pw_1' nosuperuser nocreatedb nocreaterole noreplication nobypassrls in role pg_monitor")
try:
    rodsn = f"host={SOCK} port={os.environ.get('PGPORT', '5432')} dbname=postgres user={ro} password=ro_pw_1" + (" sslmode=disable" if os.environ.get("PGPASSWORD") else "")
    rc, txt, d = gate("g10", approval_args(db_role=ro, allow_privileged_role=False), dsn=rodsn)
    doc10 = json.loads(snaps(d)[0].read_text()) if snaps(d) else {}
    check("G10 a non-privileged monitoring role that matches the approval passes the gate and the full snapshot is taken", rc == 0 and doc10.get("outcome") == "ok" and doc10.get("role_check") == {"matches_approved_role": True, "privileged_role": False, "privileged_allowed": False}, f"rc={rc} {txt.strip()[:120]}")
finally:
    with psycopg.connect(DIRECT, autocommit=True) as cc: cc.execute(f"drop role {ro}")

# --- X: outage behaviour (every failure is a recorded result; one attempt only)
px = Proxy("close")
d = tmp / "x_close"; rc, o, e, el = run(["--phase", "during2", "--out-dir", str(d), "--live", *AP], dsn=px.dsn()); time.sleep(1.5)
doc = json.loads(snaps(d)[0].read_text()) if snaps(d) else {}
check("X1 server accepts and cuts: outcome connect_failed recorded, exit 3, exactly ONE connection attempt (no retry)", rc == 3 and doc.get("outcome") == "connect_failed" and px.count == 1 and doc["connect"]["ok"] is False and doc["connect"]["error_class"], f"attempts={px.count} {doc.get('connect')}")
check("X2 failure record carries start time, elapsed ms and the (scrubbed) error; no password/host/IP leak anywhere", doc.get("t_start_kst") and doc.get("elapsed_ms") is not None and not leak(o, e, snaps(d)[0].read_text()))
px.close()
px = Proxy("silent")
d = tmp / "x_silent"; rc, o, e, el = run(["--phase", "during1", "--out-dir", str(d), "--live", *AP, "--connect-timeout", "2"], dsn=px.dsn()); time.sleep(1.0)
doc = json.loads(snaps(d)[0].read_text()) if snaps(d) else {}
check("X3 server accepts but never answers: connect_timeout honoured (<=6 s), recorded as connect_failed, one attempt", rc == 3 and doc.get("outcome") == "connect_failed" and el < 6 and px.count == 1, f"{el:.1f}s attempts={px.count} {doc.get('connect', {}).get('message', '')[:80]}")
px.close()
s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
d = tmp / "x_refused"; rc, o, e, el = run(["--phase", "during1", "--out-dir", str(d), "--live", *AP], dsn=f"host=127.0.0.1 port={port} dbname=postgres user={SUP} password=S3cretPw! sslmode=disable")
doc = json.loads(snaps(d)[0].read_text()) if snaps(d) else {}
check("X4 connection refused: recorded (connect_failed), fast, exit 3, no leak", rc == 3 and doc.get("outcome") == "connect_failed" and el < 5 and not leak(o, e, snaps(d)[0].read_text()), f"{el:.1f}s")
d = tmp / "x_dns"; rc, o, e, el = run(["--phase", "during1", "--out-dir", str(d), "--live", *AP, "--connect-timeout", "3"], dsn="host=db.abcdefghijklmnop.invalid port=5432 dbname=postgres user=postgres.abcdefghijklmnop password=S3cretPw! sslmode=require")
doc = json.loads(snaps(d)[0].read_text()) if snaps(d) else {}
check("X5 unresolvable host: recorded; host, project ref, user and password never appear in output or file", rc == 3 and doc.get("outcome") == "connect_failed" and not leak(o, e, snaps(d)[0].read_text()), (doc.get("connect") or {}).get("message", "")[:100])
px = Proxy("freeze", freeze_after=1500)
d = tmp / "x_wd"; rc, o, e, el = run(["--phase", "during1", "--out-dir", str(d), "--live", *AP, "--deadline-s", "5"], dsn=px.dsn())
doc = json.loads(snaps(d)[0].read_text()) if snaps(d) else {}
check("X6 link stalls mid-run (no reply, socket stays open): watchdog ends the run at the deadline (<=9 s), partial data + outcome watchdog, exit 3", rc == 3 and doc.get("outcome") == "watchdog" and el < 9, f"{el:.1f}s outcome={doc.get('outcome')} sections={list(doc.get('sections', {}))}")
check("X7 only ONE connection was opened in the stalled run", px.count == 1, f"attempts={px.count}")
px.close()

# --- A: analysis tool
adir = tmp / "an"
run(["--phase", "pre", "--out-dir", str(adir), "--live", *AP], dsn=DIRECT)
with psycopg.connect(DIRECT, autocommit=True) as cc:
    cc.execute("create table if not exists t214_load(i int, p text)")
    for _ in range(30): cc.execute("insert into t214_load select g, repeat('x', 200) from generate_series(1, 500) g")
    cc.execute("checkpoint")
time.sleep(1.2)
run(["--phase", "post_normal", "--out-dir", str(adir), "--live", *AP], dsn=DIRECT)
files = sorted((str(p) for p in snaps(adir)), key=lambda f: json.loads(Path(f).read_text())["t_start_utc"])
def diff(paths, *extra):
    p = subprocess.run([sys.executable, "-I", str(DIFF), *paths, *extra], capture_output=True, text=True); return p.returncode, p.stdout, p.stderr
rc, o, e = diff(files, "--json", str(tmp / "d.json"))
dj = json.loads((tmp / "d.json").read_text()); pr = dj["pairs"][0]
check("A1 valid pair: VALID, elapsed > 0, checkpointer/wal/io/database rates present, database xact_commit rate > 0", rc == 0 and pr["verdict"] == "VALID" and pr["elapsed_s"] > 0 and all(k in pr["sections"] for k in ("io", "checkpointer", "wal", "database")) and any(r["rate"].get("xact_commit", 0) > 0 for r in pr["sections"]["database"]["rows"]), pr["verdict"])
check("A2 the checkpoint forced between the snapshots shows in the checkpointer delta", pr["sections"]["checkpointer"]["delta"].get("num_requested", 0) >= 1 or pr["sections"]["checkpointer"]["delta"].get("buffers_written", 0) > 0, str({k: v for k, v in pr["sections"]["checkpointer"]["delta"].items() if v}))
a, b = [json.loads(Path(f).read_text()) for f in files]
fail_doc = json.loads(snaps(tmp / "x_close")[0].read_text()); Path(tmp / "fail.json").write_text(json.dumps(fail_doc))
rc, o, e = diff([files[0], str(tmp / "fail.json"), files[1]], "--json", str(tmp / "d2.json"))
d2 = json.loads((tmp / "d2.json").read_text())
check("A3 a failed snapshot in the series makes BOTH adjacent pairs UNMEASURABLE and is listed as a result (connect_failed), never skipped", [p["verdict"].startswith("UNMEASURABLE") for p in d2["pairs"]] == [True, True] and "connect_failed" in o)
bb = json.loads(json.dumps(b)); 
for r in bb["sections"]["io"]["data"]: r["stats_reset"] = "2099-01-01T00:00:00+00:00"
Path(tmp / "reset.json").write_text(json.dumps(bb)); rc, o, e = diff([files[0], str(tmp / "reset.json")], "--json", str(tmp / "d3.json")); d3 = json.loads((tmp / "d3.json").read_text())["pairs"][0]
check("A4 changed pg_stat_io stats_reset: that section is discarded with a note (other sections kept)", "io" not in d3["sections"] and any("stats_reset differs" in n for n in d3["notes"]) and "checkpointer" in d3["sections"])
bb = json.loads(json.dumps(b)); bb["sections"]["meta"]["data"]["postmaster_start"] = "2099-01-01T00:00:00+00:00"
Path(tmp / "pm.json").write_text(json.dumps(bb)); rc, o, e = diff([files[0], str(tmp / "pm.json")], "--json", str(tmp / "d4.json")); d4 = json.loads((tmp / "d4.json").read_text())["pairs"][0]
check("A5 postmaster restart between snapshots: pair DISCARDED", d4["verdict"].startswith("DISCARDED") and not d4["valid"])
aa = json.loads(json.dumps(a)); 
for r in aa["sections"]["io"]["data"]:
    if r.get("reads") is not None: r["reads"] = r["reads"] + 10**9
Path(tmp / "back.json").write_text(json.dumps(aa)); rc, o, e = diff([str(tmp / "back.json"), files[1]], "--json", str(tmp / "d5.json")); d5 = json.loads((tmp / "d5.json").read_text())["pairs"][0]
check("A6 a counter that went backwards drops its row (flagged), no negative rate is reported", any(f["backwards"] for f in d5["sections"]["io"]["flagged_backwards"]) and all(v >= 0 for r in d5["sections"]["io"]["rows"] for v in r["rate"].values()))

# --- K: composite statement key, duplicate rejection, coverage (CASSANDRA C214-79-01/02)
def stmt(dbid, userid, queryid, toplevel, calls, ms, rows=0): return {"dbid": dbid, "userid": userid, "queryid": queryid, "toplevel": toplevel, "calls": calls, "total_exec_time": ms, "rows": rows, "q": "select 1"}
def with_stmts(base, rows, totals=None, reset="2026-02-12T00:00:00+00:00"):
    d = json.loads(json.dumps(base)); d["sections"]["statements_info"] = {"ok": True, "data": {"dealloc": 0, "stats_reset": reset}}
    d["sections"]["statements_top"] = {"ok": True, "data": rows}
    if totals: d["sections"]["statements_totals"] = {"ok": True, "data": totals}
    return d
def pdiff(x, y, tag):
    px_, py_ = tmp / f"k_{tag}_a.json", tmp / f"k_{tag}_b.json"; px_.write_text(json.dumps(x)); py_.write_text(json.dumps(y))
    rc, o, e = diff([str(px_), str(py_)], "--json", str(tmp / f"k_{tag}.json")); return rc, o, json.loads((tmp / f"k_{tag}.json").read_text())["pairs"][0]
same = lambda: ([stmt(1, 10, 777, True, 100, 1000), stmt(1, 11, 777, True, 50, 500), stmt(2, 10, 777, True, 30, 300), stmt(1, 10, 777, False, 10, 100)])
grown = [stmt(1, 10, 777, True, 160, 1600), stmt(1, 11, 777, True, 51, 510), stmt(2, 10, 777, True, 90, 900), stmt(1, 10, 777, False, 12, 130)]
rc, o, pr = pdiff(with_stmts(a, same()), with_stmts(b, grown), "key")
rows = {tuple(r["id"]): r["delta"] for r in pr["sections"].get("statements_top", {}).get("rows", [])}
check("K1 the SAME queryid under different dbid / userid / toplevel stays four separate rows with their own deltas (no merge, no overwrite)",
      rows == {(1, 10, 777, True): {"calls": 60, "total_exec_time": 600, "rows": 0}, (1, 11, 777, True): {"calls": 1, "total_exec_time": 10, "rows": 0},
               (2, 10, 777, True): {"calls": 60, "total_exec_time": 600, "rows": 0}, (1, 10, 777, False): {"calls": 2, "total_exec_time": 30, "rows": 0}}, str(rows)[:200])
dupb = grown + [stmt(1, 10, 777, True, 999, 9999)]
rc, o, pr = pdiff(with_stmts(a, same()), with_stmts(b, dupb), "dup")
check("K2 a DUPLICATE composite key in a snapshot REJECTS the section: no rows computed, note names the key and the side", "statements_top" not in pr["sections"] and any("DUPLICATE KEY in the to-snapshot" in n for n in pr["notes"]) and "DUPLICATE KEY" in o, str(pr["notes"])[:160])
rc, o, pr = pdiff(with_stmts(a, same() + [stmt(1, 10, 777, True, 5, 50)]), with_stmts(b, grown), "dupa")
check("K2b the same rejection when the duplicate is in the FIRST snapshot", "statements_top" not in pr["sections"] and any("DUPLICATE KEY in the from-snapshot" in n for n in pr["notes"]))
old = [{"queryid": 777, "calls": 1, "total_exec_time": 1, "rows": 0}]
rc, o, pr = pdiff(with_stmts(a, old), with_stmts(b, old), "v1")
check("K3 old-schema statements (queryid only) are discarded with an explicit note, never compared", "statements_top" not in pr["sections"] and any("lacks the composite key" in n for n in pr["notes"]))
ta, tb = {"n_entries": 6, "sum_calls": 1000, "sum_total_exec_time": 10000.0, "sum_rows": 0}, {"n_entries": 7, "sum_calls": 1400, "sum_total_exec_time": 14000.0, "sum_rows": 0}
xa = same() + [stmt(5, 5, 1, True, 10, 10)]                     # left the top set
yb = grown + [stmt(6, 6, 2, True, 20, 20)]                     # entered the top set
rc, o, pr = pdiff(with_stmts(a, xa, ta), with_stmts(b, yb, tb), "cov")
cv = pr["sections"]["statements_top"]["coverage"]
check("K4 top-20 membership change is reported: entered/left keys listed, in_both counted, warning present in JSON AND in the markdown", cv["complete"] is False and cv["in_both"] == 4 and cv["entered"] == [[6, 6, 2, True]] and cv["exited"] == [[5, 5, 1, True]] and cv["warning"] and "WARNING" in o and "entered 1, left 1" in o, str(cv)[:200])
t = cv["totals"]
check("K5 coverage against pg_stat_statements totals: shown delta calls 123 of 400 all-calls (30.8%), exec 1240 of 4000 ms; the table says it is NOT all activity",
      t and t["delta_calls_all"] == 400 and t["delta_calls_shown"] == 123 and abs(t["calls_share"] - 123 / 400) < 1e-9 and abs(t["exec_share"] - 1240 / 4000) < 1e-9 and "30.8%" in o, str(t)[:200])
rc, o, pr = pdiff(with_stmts(a, same()), with_stmts(b, grown), "full")
cv = pr["sections"]["statements_top"]["coverage"]
check("K6 identical membership: complete=True, no warning; without a totals section the report says the covered share is unknown", cv["complete"] is True and cv["warning"] is None and cv["totals"] is None and "share of all activity that this table covers is unknown" in o)

print(f"\n{sum(results)}/{len(results)} passed"); sys.exit(0 if all(results) else 1)
