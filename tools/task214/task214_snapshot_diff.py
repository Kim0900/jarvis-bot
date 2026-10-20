#!/usr/bin/env python3
"""TASK#214 offline analysis of snapshot files written by task214_snapshot.py.  Reads files only: no database, no network.

    python3 task214_snapshot_diff.py SNAP1.json SNAP2.json [SNAP3.json ...] [--md report.md] [--json report.json]

Snapshots are paired in the order given (consecutive pairs).  For each pair it reports rates (delta / elapsed server seconds) of every numeric
counter in pg_stat_io, pg_stat_checkpointer, pg_stat_bgwriter, pg_stat_wal, pg_stat_database and pg_stat_statements, plus the pg_stat_activity
picture side by side.  Rules (design section 4 / 'interpretation rules', pre-registered):
  * a pair is valid only if postmaster_start and the statistics stats_reset values are the same in both snapshots; otherwise the affected
    section (or the pair) is discarded and the reason is printed;
  * a failed snapshot (connect_failed / query_timeout / watchdog / ...) is itself a measurement: it is listed with its facts, and any pair that
    contains it is reported UNMEASURABLE (not silently skipped);
  * a counter that went backwards invalidates its row (flagged), never a negative rate;
  * nothing here says 'cause': a delta that differs between phases is at most a candidate hypothesis.
"""
import argparse, json, sys
from datetime import datetime
from pathlib import Path

SKIP_KEYS = {"stats_reset", "datid", "oid"}


def load(p):
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    if d.get("schema") != "task214-snapshot-v1":
        raise SystemExit(f"{p}: not a task214 snapshot")
    d["_file"] = Path(p).name
    return d


def sec(d, name):
    s = (d.get("sections") or {}).get(name)
    return s["data"] if s and s.get("ok") else None


def ts(x):
    return datetime.fromisoformat(x) if x else None


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def row_delta(a, b, skip=()):
    out, bad = {}, []
    for k, vb in b.items():
        if k in SKIP_KEYS or k in skip or not num(vb):
            continue
        va = a.get(k)
        if not num(va):
            continue
        if vb < va:
            bad.append(k)
        out[k] = vb - va
    return out, bad


def rows_delta(ra, rb, idkeys, secs):
    ia = {tuple(r.get(k) for k in idkeys): r for r in ra}
    res, flagged = [], []
    for r in rb:
        key = tuple(r.get(k) for k in idkeys)
        if key not in ia:
            continue
        d, bad = row_delta(ia[key], r)
        if bad:
            flagged.append({"id": list(key), "backwards": bad}); continue
        res.append({"id": list(key), "delta": d, "rate": {k: v / secs for k, v in d.items()}})
    return res, flagged


def resets_equal(ra, rb, idkeys=None):
    def m(rows):
        return {tuple(r.get(k) for k in (idkeys or ())): r.get("stats_reset") for r in rows}
    return m(ra) == m(rb)


def pair(a, b):
    r = {"from": a["_file"], "to": b["_file"], "from_phase": a["phase"], "to_phase": b["phase"], "valid": False, "notes": [], "sections": {}}
    bad = [x for x in (a, b) if x["outcome"] not in ("ok", "partial")]
    if bad:
        r["verdict"] = "UNMEASURABLE: " + "; ".join(f"{x['phase']}={x['outcome']}" for x in bad)
        return r
    ma, mb = sec(a, "meta"), sec(b, "meta")
    if not ma or not mb:
        r["verdict"] = "UNMEASURABLE: meta missing"; return r
    if ma["postmaster_start"] != mb["postmaster_start"]:
        r["verdict"] = "DISCARDED: postmaster restarted between the snapshots (counters not comparable)"; return r
    secs = (ts(mb["server_clock"]) - ts(ma["server_clock"])).total_seconds()
    if secs <= 0:
        r["verdict"] = "DISCARDED: non-positive elapsed time"; return r
    r["elapsed_s"] = secs
    r["valid"] = True
    r["verdict"] = "VALID"
    for name, idk in (("io", ("backend_type", "object", "context")), ("database", ("datname",)), ("statements_top", ("queryid",))):
        x, y = sec(a, name), sec(b, name)
        if x is None or y is None:
            r["notes"].append(f"{name}: missing in one snapshot"); continue
        if name != "statements_top" and not resets_equal(x, y, idk):
            r["notes"].append(f"{name}: stats_reset differs -> section discarded"); continue
        if name == "statements_top":
            ia, ib = sec(a, "statements_info"), sec(b, "statements_info")
            if not ia or not ib or ia.get("stats_reset") != ib.get("stats_reset"):
                r["notes"].append("statements_top: stats_reset differs or unknown -> section discarded"); continue
        rows, flagged = rows_delta(x, y, idk, secs)
        r["sections"][name] = {"rows": rows, "flagged_backwards": flagged}
    for name in ("checkpointer", "bgwriter", "wal"):
        x, y = sec(a, name), sec(b, name)
        if x is None or y is None:
            r["notes"].append(f"{name}: missing in one snapshot"); continue
        if x.get("stats_reset") != y.get("stats_reset"):
            r["notes"].append(f"{name}: stats_reset differs -> section discarded"); continue
        d, badk = row_delta(x, y)
        if badk:
            r["notes"].append(f"{name}: counters went backwards {badk} -> section discarded"); continue
        r["sections"][name] = {"delta": d, "rate": {k: v / secs for k, v in d.items()}}
    r["activity"] = {"from": sec(a, "activity"), "to": sec(b, "activity")}
    return r


def fmt(v):
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def render(snaps, pairs):
    L = ["# TASK#214 snapshot analysis (offline; no cause is asserted)", "", "## Snapshots", "",
         "| file | phase | outcome | start (KST) | connect | note |", "|---|---|---|---|---|---|"]
    for s in snaps:
        c = s.get("connect") or {}
        note = ""
        if s["outcome"] != "ok":
            note = f"{c.get('error_class') or ''} {c.get('sqlstate') or ''} {(c.get('message') or '')[:80]}".strip()
            if s.get("skipped"):
                note += f" skipped={','.join(s['skipped'])}"
        L.append(f"| {s['_file']} | {s['phase']} ({s['profile']}) | {s['outcome']} | {s['t_start_kst']} | {'ok' if c.get('ok') else 'FAILED'} {c.get('ms', '')}ms | {note} |")
    for p in pairs:
        L += ["", f"## {p['from_phase']} -> {p['to_phase']}: {p['verdict']}" + (f"  (elapsed {p['elapsed_s']:.1f} s)" if p.get("elapsed_s") else "")]
        for n in p["notes"]:
            L.append(f"- note: {n}")
        if not p["valid"]:
            continue
        io = p["sections"].get("io", {}).get("rows", [])
        rows = sorted(io, key=lambda r: -(r["rate"].get("writes", 0) + r["rate"].get("fsyncs", 0)))[:12]
        if rows:
            L += ["", "pg_stat_io: top rows by writes+fsyncs per second (units: counts; *_time is 0 when track_io_timing=off)", "",
                  "| backend_type / object / context | reads/s | writes/s | extends/s | fsyncs/s | evictions/s |", "|---|---|---|---|---|---|"]
            for r in rows:
                k = r["rate"]
                L.append(f"| {' / '.join(str(x) for x in r['id'])} | {fmt(k.get('reads', 0))} | {fmt(k.get('writes', 0))} | {fmt(k.get('extends', 0))} | {fmt(k.get('fsyncs', 0))} | {fmt(k.get('evictions', 0))} |")
        for name in ("checkpointer", "bgwriter", "wal"):
            s = p["sections"].get(name)
            if s:
                L += ["", f"{name}: " + ", ".join(f"{k}={fmt(v)}/s" for k, v in sorted(s["rate"].items()) if v)]
        for r in p["sections"].get("database", {}).get("rows", []):
            k = r["rate"]
            if k.get("xact_commit", 0) or k.get("blks_read", 0):
                L.append(f"\ndatabase {r['id'][0]}: xact_commit={fmt(k.get('xact_commit', 0))}/s rollback={fmt(k.get('xact_rollback', 0))}/s blks_read={fmt(k.get('blks_read', 0))}/s tup_updated={fmt(k.get('tup_updated', 0))}/s")
        st = p["sections"].get("statements_top", {}).get("rows", [])
        if st:
            L += ["", "pg_stat_statements: top by delta total_exec_time (queries present in both top sets only)", "", "| queryid | calls/s | exec ms/s | rows/s |", "|---|---|---|---|"]
            for r in sorted(st, key=lambda r: -r["rate"].get("total_exec_time", 0))[:8]:
                L.append(f"| {r['id'][0]} | {fmt(r['rate'].get('calls', 0))} | {fmt(r['rate'].get('total_exec_time', 0))} | {fmt(r['rate'].get('rows', 0))} |")
        for sname, sv in p["sections"].items():
            for f in sv.get("flagged_backwards", []):
                L.append(f"- flagged (counter went backwards, row dropped): {sname} {f['id']} {f['backwards']}")
    L += ["", "Interpretation rules (pre-registered): no correlation here is a cause; a phase-dependent delta is a candidate hypothesis to confirm with the dashboard Disk IO graph and Supabase support."]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshots", nargs="+"); ap.add_argument("--md"); ap.add_argument("--json")
    a = ap.parse_args(argv)
    snaps = [load(p) for p in a.snapshots]
    pairs = [pair(snaps[i], snaps[i + 1]) for i in range(len(snaps) - 1)]
    for p, x, y in zip(pairs, snaps, snaps[1:]):
        if ts(y["t_start_utc"]) <= ts(x["t_start_utc"]):
            p["notes"].append("snapshots were given out of time order (client start times)")
    md = render(snaps, pairs)
    print(md)
    if a.md: Path(a.md).write_text(md, encoding="utf-8")
    if a.json: Path(a.json).write_text(json.dumps({"snapshots": [s["_file"] for s in snaps], "pairs": pairs}, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
