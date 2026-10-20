#!/usr/bin/env python3
"""TASK#197 P1-E: OFFLINE replay of the REDACTED real production event history through the rollback rehearsal (CASSANDRA #5673).

    PGHOST=<socket dir of a THROWAWAY PG17> PGSUPER=supabase_admin python3 tests/sql/task197_real_replay_offline.py --export <redacted export.json> [--out result.json]

The export (NOT in the repository, NOT uploaded to CI) is produced by a read-only SELECT on magi_task_events for the 16 replay tasks.  It keeps what the
gate reads and nothing else:  event order, event_type, actor and a payload reduced to the keys verdict / stage / tier / evidence_kind / verifier_role
(other keys dropped; non-token values replaced by 'H:<12 hex of sha256>'); detail text, timestamps, raw ids and every other payload key are gone.
Two commitments make the replay checkable without publishing the data:
  * redacted_content_sha256  = sha256 over the ordered lines  task|event_type|actor|payload   (this script recomputes it from what it loaded: any
    transcription or tampering error changes it);
  * raw_sha256 (recorded in the manifest, computed server-side over the UNREDACTED rows) - MAGI/CASSANDRA can recompute it from the registry.

The script loads the redacted events into a throwaway production-shaped database, checks the commitment, runs the committed DO statement through the
safe runner (test mode) and prints ACTUAL numbers: events per task (real vs the 35 synthetic ones in the CI fixture), the harness sentinel (checks,
scenarios, replay_ok/replay_drift) and every classification line.  A drift or a mismatch is reported as it is; nothing is adjusted to pass.
"""
import argparse, hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from task197_pgtest_lib import *   # noqa: F401,F403
import psycopg

ap = argparse.ArgumentParser()
ap.add_argument("--export", required=True); ap.add_argument("--out", default="")
a = ap.parse_args()
ex = json.loads(Path(a.export).read_text(encoding="utf-8"))
types, actors = ex["types"], ex["actors"]
events = []                                   # expanded, in order: (task, type, actor, payload|None)
for task, ti, ai, p, n in ex["runs"]:
    events += [(task, types[ti], actors[ai], p)] * n
check("E0 export is complete: event count and run count match its own header", len(events) == ex["n_events"] and len(ex["runs"]) == ex["n_runs"], f"{len(events)} events, {len(ex['runs'])} runs")

DB = "t_real"
sup("postgres", f"drop database if exists {DB} with (force); drop database if exists gate with (force); create database gate;")
rc, o, e = psql("postgres", None, user=SUP, args=("-v", "ON_ERROR_STOP=1"), file=str(ROOT / "tests/sql/task197_prod_like_schema.sql"))
if rc: raise SystemExit("schema load failed: " + e[-800:])
tasks = sorted({t for t, *_ in events})
with psycopg.connect(f"host={os.environ['PGHOST']} dbname=gate user={SUP}", autocommit=True) as c:
    for t in tasks:
        c.execute("insert into magi_tasks(task_id,title,owner_agent,status,priority,domain,verification_required) overriding system value values (%s,%s,'x','COMPLETED','P1','magi',true)", (t, f"real {t}"))
    # event ids are renumbered 100000.. in order (raw ids are not exported); the harness only needs the order and event_id < its fixture base
    for i, (t, ty, ac, p) in enumerate(events):
        c.execute("insert into magi_task_events(event_id,task_id,event_type,actor,detail,payload) overriding system value values (%s,%s,%s,%s,'redacted',%s::jsonb)",
                  (100000 + i, t, ty, ac, None if p is None else json.dumps(p)))
    got = c.execute("select encode(sha256(convert_to(string_agg(task_id||'|'||event_type||'|'||coalesce(actor,'<null>')||'|'||coalesce(payload::text,'<null>'), E'\\n' order by event_id),'UTF8')),'hex') from magi_task_events where event_id >= 100000").fetchone()[0]
    per = dict(c.execute("select task_id, count(*) from magi_task_events where event_id >= 100000 group by 1 order by 1").fetchall())
    syn = c.execute("select count(*) from magi_task_events").fetchone()[0]
check("E1 loaded data reproduces the server-side redacted_content_sha256 (nothing lost or altered in the export)", got == ex["redacted_content_sha256"], got[:16] + "…")
sup("postgres", f"alter database gate rename to {DB};")
s0 = state(DB)

sha = hashlib.sha256(HARNESS.encode()).hexdigest()
rp = Path(tempfile.mkdtemp(prefix="t197real_")) / "report.json"
p = subprocess.run([sys.executable, str(ROOT / "tests/sql/task197_safe_runner.py"), "--test-mode", "--sha256", sha, "--report", str(rp)],
                   capture_output=True, text=True, timeout=180,
                   env=dict(os.environ, PGDSN=f"host={os.environ['PGHOST']} dbname={DB} user=postgres", TASK197_TEST_MODE="throwaway-server"))
rep = json.loads(rp.read_text()) if rp.exists() else {}
s1 = state(DB)
sent = rep.get("sentinel", {})
EXPECT = json.loads((ROOT / "tests/sql/task197_prod_rollback_rehearsal.expect.json").read_text())
clean = s0[1] == s1[1] and s0[2] == s1[2] and strip_seq(s0[0]) == strip_seq(s1[0])
check("E2 runner on the REAL redacted history: strict verdict recorded, no residue (rollback-only holds on the real shape)", rep.get("verdict") in ("PASS", "FAIL") and clean and "snapshot_after" in rep,
      f"verdict={rep.get('verdict')} reasons={rep.get('reasons')}")
check("E3 replay classes of the 13 baseline tasks equal the registered expectation (replay_ok=13, replay_drift=0)", sent.get("replay_ok") == str(EXPECT["replay_ok"]) and sent.get("replay_drift") == "0", f"replay_ok={sent.get('replay_ok')} drift={sent.get('replay_drift')}")
info = [l for l in (rep.get("detail") or "").splitlines() if l.startswith("replay #") or "REPLAY#" in l]
result = {
    "export_redacted_content_sha256": ex["redacted_content_sha256"], "events": len(events), "tasks": len(tasks), "events_per_task": per,
    "synthetic_ci_fixture_events": 35, "runner_verdict": rep.get("verdict"), "reasons": rep.get("reasons"), "sentinel": sent,
    "info_lines": info, "detail_head": (rep.get("detail") or "")[:2500], "residue_free": clean, "server_version": rep.get("server_version"),
}
print(json.dumps({k: v for k, v in result.items() if k not in ("detail_head",)}, ensure_ascii=False, indent=1))
if a.out: Path(a.out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"\n{sum(results)}/{len(results)} passed"); sys.exit(0 if all(results) else 1)
