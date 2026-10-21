#!/usr/bin/env python3
"""Self-test of tools/task214/task214_snapshot_diff.py (the only tool left in PR#79 after MAGI #5848).  Pure files: no database, no network, no
extra packages.  Fixtures are synthetic snapshot files in the input format documented in docs/TASK214_SNAPSHOT_PROCEDURE.md.

    python3 tools/task214/task214_snapshot_selftest.py
"""
import json, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIFF = HERE / "task214_snapshot_diff.py"
tmp = Path(tempfile.mkdtemp(prefix="t214d_"))
results = []


def check(name, ok, info=""):
    results.append(bool(ok)); print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)


def snap(phase, t, outcome="ok", pm="2026-03-18T04:49:29+00:00", reset="2026-02-12T23:51:26+00:00", reads=100, writes=1000, commits=5000, ckpt_written=500):
    S = lambda d: {"ok": True, "data": d}
    return {"schema": "task214-snapshot-v2", "phase": phase, "profile": "full", "outcome": outcome, "t_start_utc": t, "t_start_kst": t, "connect": {"ok": outcome != "connect_failed", "ms": 12},
            "sections": {
                "meta": S({"server_clock": t, "postmaster_start": pm}),
                "io": S([{"backend_type": "checkpointer", "object": "relation", "context": "normal", "reads": None, "writes": writes, "fsyncs": 10, "extends": None, "evictions": None, "stats_reset": reset},
                         {"backend_type": "client backend", "object": "relation", "context": "normal", "reads": reads, "writes": 0, "fsyncs": 0, "extends": 5, "evictions": 0, "stats_reset": reset}]),
                "checkpointer": S({"num_timed": 10, "num_requested": 1, "buffers_written": ckpt_written, "stats_reset": reset}),
                "bgwriter": S({"buffers_clean": 0, "buffers_alloc": 50, "stats_reset": reset}),
                "wal": S({"wal_bytes": 10 ** 6, "wal_records": 1000, "stats_reset": reset}),
                "database": S([{"datname": "postgres", "xact_commit": commits, "blks_read": reads, "tup_updated": 3, "stats_reset": reset}]),
                "activity": S([{"backend_type": "client backend", "state": "idle", "n": 3}])}}


def write(name, d):
    p = tmp / name; p.write_text(json.dumps(d)); return str(p)


def diff(paths, tag):
    o = tmp / f"{tag}.json"
    p = subprocess.run([sys.executable, "-I", str(DIFF), *paths, "--json", str(o)], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr, (json.loads(o.read_text()) if o.exists() else None)


T0, T1, T2 = "2026-10-20T11:00:00+00:00", "2026-10-20T11:10:00+00:00", "2026-10-20T11:20:00+00:00"
a = snap("pre", T0); b = snap("post_normal", T1, reads=700, writes=1600, commits=6200, ckpt_written=800)
fa, fb = write("a.json", a), write("b.json", b)

# --- A: general pair rules (unchanged behaviour)
rc, o, e, j = diff([fa, fb], "a1"); pr = j["pairs"][0]
io = {tuple(r["id"]): r["rate"] for r in pr["sections"]["io"]["rows"]}
check("A1 valid pair: VALID, elapsed 600 s, per-second rates are delta/elapsed (checkpointer writes 1.0/s, db xact_commit 2.0/s, checkpointer buffers 0.5/s)",
      rc == 0 and pr["verdict"] == "VALID" and pr["elapsed_s"] == 600 and abs(io[("checkpointer", "relation", "normal")]["writes"] - 1.0) < 1e-9
      and abs(pr["sections"]["database"]["rows"][0]["rate"]["xact_commit"] - 2.0) < 1e-9 and abs(pr["sections"]["checkpointer"]["rate"]["buffers_written"] - 0.5) < 1e-9)
fail = snap("during1", T1, outcome="connect_failed"); fail["sections"] = {}; fail["connect"] = {"ok": False, "ms": 3000, "error_class": "OperationalError", "sqlstate": None, "message": "timeout"}
rc, o, e, j = diff([fa, write("f.json", fail), write("b2.json", snap("post_normal", T2))], "a3")
check("A3 a failed snapshot makes BOTH adjacent pairs UNMEASURABLE and is listed as a result (connect_failed), never skipped", [p["verdict"].startswith("UNMEASURABLE") for p in j["pairs"]] == [True, True] and "connect_failed" in o)
rc, o, e, j = diff([fa, write("r.json", snap("post_normal", T1, reset="2099-01-01T00:00:00+00:00"))], "a4"); pr = j["pairs"][0]
check("A4 changed stats_reset: the affected sections are discarded with a note", "io" not in pr["sections"] and any("stats_reset differs" in n for n in pr["notes"]))
rc, o, e, j = diff([fa, write("pm.json", snap("post_normal", T1, pm="2099-01-01T00:00:00+00:00"))], "a5")
check("A5 postmaster restart between snapshots: pair DISCARDED", j["pairs"][0]["verdict"].startswith("DISCARDED") and not j["pairs"][0]["valid"])
rc, o, e, j = diff([write("hi.json", snap("pre", T0, reads=10 ** 9)), fb], "a6"); pr = j["pairs"][0]
check("A6 a counter that went backwards drops its row (flagged); no negative rate", any(f["backwards"] for f in pr["sections"]["io"]["flagged_backwards"]) and all(v >= 0 for r in pr["sections"]["io"]["rows"] for v in r["rate"].values()))
rc, o, e, j = diff([fb, fa], "a7")
check("A7 snapshots given out of time order: pair is not valid (non-positive elapsed) and the order problem is noted", not j["pairs"][0]["valid"] and any("out of time order" in n for n in j["pairs"][0]["notes"]))
bad = tmp / "bad.json"; bad.write_text(json.dumps({"schema": "something-else"}))
p = subprocess.run([sys.executable, "-I", str(DIFF), fa, str(bad)], capture_output=True, text=True)
check("A8 a file that is not a task214 snapshot is refused (non-zero exit, message)", p.returncode != 0 and "not a task214 snapshot" in p.stderr + p.stdout)

# --- K: composite statement key, duplicate rejection, coverage (CASSANDRA C214-79-01/02)
def stmt(dbid, userid, queryid, toplevel, calls, ms, rows=0): return {"dbid": dbid, "userid": userid, "queryid": queryid, "toplevel": toplevel, "calls": calls, "total_exec_time": ms, "rows": rows, "q": "select 1"}
def with_stmts(base, rows, totals=None, reset="2026-02-12T00:00:00+00:00"):
    d = json.loads(json.dumps(base)); d["sections"]["statements_info"] = {"ok": True, "data": {"dealloc": 0, "stats_reset": reset}}
    d["sections"]["statements_top"] = {"ok": True, "data": rows}
    if totals: d["sections"]["statements_totals"] = {"ok": True, "data": totals}
    return d
def pdiff(x, y, tag):
    rc, o, e, j = diff([write(f"k_{tag}_a.json", x), write(f"k_{tag}_b.json", y)], f"k_{tag}"); return o, j["pairs"][0]
same = [stmt(1, 10, 777, True, 100, 1000), stmt(1, 11, 777, True, 50, 500), stmt(2, 10, 777, True, 30, 300), stmt(1, 10, 777, False, 10, 100)]
grown = [stmt(1, 10, 777, True, 160, 1600), stmt(1, 11, 777, True, 51, 510), stmt(2, 10, 777, True, 90, 900), stmt(1, 10, 777, False, 12, 130)]
o, pr = pdiff(with_stmts(a, same), with_stmts(b, grown), "key")
rows = {tuple(r["id"]): r["delta"] for r in pr["sections"].get("statements_top", {}).get("rows", [])}
check("K1 the SAME queryid under different dbid / userid / toplevel stays four separate rows with their own deltas (no merge, no overwrite)",
      rows == {(1, 10, 777, True): {"calls": 60, "total_exec_time": 600, "rows": 0}, (1, 11, 777, True): {"calls": 1, "total_exec_time": 10, "rows": 0},
               (2, 10, 777, True): {"calls": 60, "total_exec_time": 600, "rows": 0}, (1, 10, 777, False): {"calls": 2, "total_exec_time": 30, "rows": 0}}, str(rows)[:200])
o, pr = pdiff(with_stmts(a, same), with_stmts(b, grown + [stmt(1, 10, 777, True, 999, 9999)]), "dup")
check("K2 a DUPLICATE composite key in the later snapshot REJECTS the section: no rows computed, the note names the side", "statements_top" not in pr["sections"] and any("DUPLICATE KEY in the to-snapshot" in n for n in pr["notes"]) and "DUPLICATE KEY" in o, str(pr["notes"])[:160])
o, pr = pdiff(with_stmts(a, same + [stmt(1, 10, 777, True, 5, 50)]), with_stmts(b, grown), "dupa")
check("K2b the same rejection when the duplicate is in the FIRST snapshot", "statements_top" not in pr["sections"] and any("DUPLICATE KEY in the from-snapshot" in n for n in pr["notes"]))
old = [{"queryid": 777, "calls": 1, "total_exec_time": 1, "rows": 0}]
o, pr = pdiff(with_stmts(a, old), with_stmts(b, old), "v1")
check("K3 statements rows without the composite key (queryid only) are discarded with an explicit note, never compared", "statements_top" not in pr["sections"] and any("lacks the composite key" in n for n in pr["notes"]))
ta, tb = {"n_entries": 6, "sum_calls": 1000, "sum_total_exec_time": 10000.0, "sum_rows": 0}, {"n_entries": 7, "sum_calls": 1400, "sum_total_exec_time": 14000.0, "sum_rows": 0}
o, pr = pdiff(with_stmts(a, same + [stmt(5, 5, 1, True, 10, 10)], ta), with_stmts(b, grown + [stmt(6, 6, 2, True, 20, 20)], tb), "cov")
cv = pr["sections"]["statements_top"]["coverage"]
check("K4 top-N membership change is reported: entered/left keys listed, in_both counted, warning present in JSON AND in the markdown", cv["complete"] is False and cv["in_both"] == 4 and cv["entered"] == [[6, 6, 2, True]] and cv["exited"] == [[5, 5, 1, True]] and cv["warning"] and "WARNING" in o and "entered 1, left 1" in o, str(cv)[:200])
t = cv["totals"]
check("K5 coverage against pg_stat_statements totals: shown delta calls 123 of 400 (30.8%), exec 1240 of 4000 ms; the report says it is NOT all activity",
      t and t["delta_calls_all"] == 400 and t["delta_calls_shown"] == 123 and abs(t["calls_share"] - 123 / 400) < 1e-9 and abs(t["exec_share"] - 1240 / 4000) < 1e-9 and "30.8%" in o, str(t)[:200])
o, pr = pdiff(with_stmts(a, same), with_stmts(b, grown), "full")
cv = pr["sections"]["statements_top"]["coverage"]
check("K6 identical membership: complete=True, no warning; without a totals section the report says the covered share is unknown", cv["complete"] is True and cv["warning"] is None and cv["totals"] is None and "share of all activity that this table covers is unknown" in o)
o, pr = pdiff(with_stmts(a, same, reset="2026-02-12T00:00:00+00:00"), with_stmts(b, grown, reset="2026-09-01T00:00:00+00:00"), "sreset")
check("K7 pg_stat_statements_info stats_reset differs: statements section discarded", "statements_top" not in pr["sections"] and any("statements_top: stats_reset differs" in n for n in pr["notes"]))

# --- N: scope (MAGI #5848): analysis only, no database access, no approval machinery
src = DIFF.read_text()
imports = set(re.findall(r"^\s*(?:import|from)\s+([a-zA-Z0-9_\.]+)", src, re.M))
check("N1 the tool imports only the standard library pieces it needs (no psycopg, socket, subprocess, urllib, requests, cryptography): it cannot reach a database or network", not (imports & {"psycopg", "psycopg2", "socket", "subprocess", "urllib", "requests", "http", "cryptography", "ssl"}), str(sorted(imports)))
check("N2 the collector and the signed-approval machinery are gone from the tools directory", sorted(p.name for p in HERE.glob("*.py")) == ["task214_snapshot_diff.py", "task214_snapshot_selftest.py"] and "approval" not in src.lower() and "--live" not in src)

print(f"\n{sum(results)}/{len(results)} passed"); sys.exit(0 if all(results) else 1)
