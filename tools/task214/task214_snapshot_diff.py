#!/usr/bin/env python3
"""TASK#214 offline analysis of snapshot files (input format: docs/TASK214_SNAPSHOT_PROCEDURE.md section 2).  Reads files only: no database, no network.

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


EXIT_OK, EXIT_INPUT, EXIT_NO_PAIR = 0, 2, 3   # 0 >=1 valid pair | 2 usage/input/output error, nothing analysed | 3 inputs fine but ZERO analysable pairs (incl. ANALYSIS_EMPTY)
REQUIRED = ("schema", "phase", "profile", "outcome", "t_start_utc", "t_start_kst", "sections")


class InputError(Exception):
    pass


def load(p):
    try:
        raw = Path(p).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise InputError(f"{p}: cannot read ({type(e).__name__})")
    try:
        d = json.loads(raw)
    except ValueError:
        raise InputError(f"{p}: not valid JSON")
    if not isinstance(d, dict) or d.get("schema") not in ("task214-snapshot-v1", "task214-snapshot-v2"):
        raise InputError(f"{p}: not a task214 snapshot")
    missing = [k for k in REQUIRED if k not in d]
    if missing:
        raise InputError(f"{p}: required field(s) missing: {', '.join(missing)}")
    if not isinstance(d["sections"], dict) or not isinstance(d["outcome"], str) or not isinstance(d["phase"], str):
        raise InputError(f"{p}: sections must be an object, phase and outcome strings")
    try:
        ts(d["t_start_utc"])
    except (ValueError, TypeError):
        raise InputError(f"{p}: t_start_utc is not an ISO time")
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


STMT_KEY = ("dbid", "userid", "queryid", "toplevel")


def find_duplicates(rows, idkeys):
    seen, dup = set(), []
    for r in rows:
        k = tuple(r.get(x) for x in idkeys)
        if k in seen:
            dup.append(list(k))
        seen.add(k)
    return dup


def rows_delta(ra, rb, idkeys, secs):
    ia = {tuple(r.get(k) for k in idkeys): r for r in ra}
    res, flagged = [], []
    for r in rb:
        key = tuple(r.get(k) for k in idkeys)
        if key not in ia:
            continue
        d, bad = row_delta(ia[key], r, skip=idkeys)
        if bad:
            flagged.append({"id": list(key), "backwards": bad}); continue
        res.append({"id": list(key), "delta": d, "rate": {k: v / secs for k, v in d.items()}})
    return res, flagged


def resets_equal(ra, rb, idkeys=None):
    def m(rows):
        return {tuple(r.get(k) for k in (idkeys or ())): r.get("stats_reset") for r in rows}
    return m(ra) == m(rb)


def coverage(x, y, ta, tb):
    """Top-N sets are cut per snapshot (top 20 by time, calls and rows): a query may be in one set and not the other.  Rates exist only for the
    intersection; everything else is listed so nobody reads the table as 'all activity'."""
    ka, kb = {tuple(r.get(k) for k in STMT_KEY) for r in x}, {tuple(r.get(k) for k in STMT_KEY) for r in y}
    inter, entered, exited = ka & kb, kb - ka, ka - kb
    cov = {"keys_from": len(ka), "keys_to": len(kb), "in_both": len(inter), "entered": [list(k) for k in sorted(entered, key=str)], "exited": [list(k) for k in sorted(exited, key=str)],
           "complete": not entered and not exited}
    cov["warning"] = None if cov["complete"] else (f"top-N membership changed between the snapshots: {len(entered)} entered, {len(exited)} left; rates cover the {len(inter)} queries present in both only. "
                                                  "Activity of queries outside the top sets, and of those that entered/left, is NOT in this table.")
    if ta and tb and all(isinstance(t.get(k), (int, float)) for t in (ta, tb) for k in ("sum_calls", "sum_total_exec_time")):
        dcalls, dtime = tb["sum_calls"] - ta["sum_calls"], tb["sum_total_exec_time"] - ta["sum_total_exec_time"]
        ia = {tuple(r.get(k) for k in STMT_KEY): r for r in x}
        shown_calls = sum(max(0, r["calls"] - ia[tuple(r.get(k) for k in STMT_KEY)]["calls"]) for r in y if tuple(r.get(k) for k in STMT_KEY) in ia)
        shown_time = sum(max(0, r["total_exec_time"] - ia[tuple(r.get(k) for k in STMT_KEY)]["total_exec_time"]) for r in y if tuple(r.get(k) for k in STMT_KEY) in ia)
        cov["totals"] = {"delta_calls_all": dcalls, "delta_calls_shown": shown_calls, "delta_exec_ms_all": dtime, "delta_exec_ms_shown": shown_time,
                         "calls_share": (shown_calls / dcalls) if dcalls > 0 else None, "exec_share": (shown_time / dtime) if dtime > 0 else None,
                         "note": "'all' comes from pg_stat_statements totals; entries evicted from pg_stat_statements make 'all' a lower bound too (see statements_info dealloc)"}
    else:
        cov["totals"] = None
    return cov


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
    compared = 0   # numeric counter comparisons actually performed (delta 0 counts: "nothing happened" is a measurement; "nothing compared" is not)
    for name, idk in (("io", ("backend_type", "object", "context")), ("database", ("datname",)), ("statements_top", STMT_KEY)):
        x, y = sec(a, name), sec(b, name)
        if x is None or y is None:
            r["notes"].append(f"{name}: missing in one snapshot"); continue
        rejected = False
        for label, rows in (("from", x), ("to", y)):
            if name == "statements_top" and any(k not in row for row in rows for k in idk):
                r["notes"].append(f"statements_top: {label}-snapshot lacks the composite key {list(idk)} (old schema v1: queryid alone is not unique across users/databases/toplevel) -> section discarded")
                rejected = True; break
            dup = find_duplicates(rows, idk)
            if dup:
                r["notes"].append(f"{name}: DUPLICATE KEY in the {label}-snapshot {dup[:3]} (+{max(0, len(dup) - 3)} more) -> section REJECTED, nothing computed from it")
                rejected = True; break
        if rejected:
            continue
        if name != "statements_top" and not resets_equal(x, y, idk):
            r["notes"].append(f"{name}: stats_reset differs -> section discarded"); continue
        if name == "statements_top":
            ia, ib = sec(a, "statements_info"), sec(b, "statements_info")
            if not ia or not ib or ia.get("stats_reset") != ib.get("stats_reset"):
                r["notes"].append("statements_top: stats_reset differs or unknown -> section discarded"); continue
        rows, flagged = rows_delta(x, y, idk, secs)
        r["sections"][name] = {"rows": rows, "flagged_backwards": flagged}
        compared += sum(len(x["delta"]) for x in rows)
        if name == "statements_top":
            r["sections"][name]["coverage"] = coverage(x, y, sec(a, "statements_totals"), sec(b, "statements_totals"))
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
        compared += len(d)
    r["compared_counters"] = compared
    if compared == 0:
        r["valid"] = False
        r["verdict"] = ("ANALYSIS_EMPTY: the snapshots are compatible but ZERO numeric counters were compared (every section missing, discarded or without comparable "
                        "counters); see the notes. A delta of 0 would have counted as valid.")
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
        for sname, sv in p["sections"].items():      # dropped rows are reported for EVERY pair, valid or ANALYSIS_EMPTY
            for f in sv.get("flagged_backwards", []):
                L.append(f"- flagged (counter went backwards, row dropped): {sname} {f['id']} {f['backwards']}")
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
            L += ["", "pg_stat_statements: top by delta total_exec_time (queries present in both top sets only; key = dbid/userid/queryid/toplevel)", "",
                  "| dbid | userid | queryid | toplevel | calls/s | exec ms/s | rows/s |", "|---|---|---|---|---|---|---|"]
            for r in sorted(st, key=lambda r: -r["rate"].get("total_exec_time", 0))[:8]:
                L.append(f"| {' | '.join(str(v) for v in r['id'])} | {fmt(r['rate'].get('calls', 0))} | {fmt(r['rate'].get('total_exec_time', 0))} | {fmt(r['rate'].get('rows', 0))} |")
        cv = p["sections"].get("statements_top", {}).get("coverage")
        if cv:
            L += ["", f"statements coverage: {cv['in_both']} queries in both top sets ({cv['keys_from']} -> {cv['keys_to']}); entered {len(cv['entered'])}, left {len(cv['exited'])}."]
            if cv["warning"]:
                L.append(f"- WARNING: {cv['warning']}")
            t = cv.get("totals")
            if t:
                sh = lambda v: "n/a" if v is None else f"{v * 100:.1f}%"
                L.append(f"- the shown queries account for {sh(t['calls_share'])} of all calls and {sh(t['exec_share'])} of all exec time in this interval (all: {t['delta_calls_all']} calls, {fmt(t['delta_exec_ms_all'])} ms).")
            else:
                L.append("- no totals section in these snapshots: the share of all activity that this table covers is unknown.")
    L += ["", "Interpretation rules (pre-registered): no correlation here is a cause; a phase-dependent delta is a candidate hypothesis to confirm with the dashboard Disk IO graph and Supabase support."]
    return "\n".join(L) + "\n"


def summarize(snaps, pairs):
    """Counts that stay true whatever the exit code: nothing is dropped from the report."""
    valid = [p for p in pairs if p["valid"]]
    failed = [{"file": x["_file"], "phase": x["phase"], "outcome": x["outcome"]} for x in snaps if x["outcome"] not in ("ok", "partial")]
    degraded = [{"file": x["_file"], "phase": x["phase"], "outcome": x["outcome"]} for x in snaps if x["outcome"] == "partial"]
    bad_pairs = [{"from": p["from_phase"], "to": p["to_phase"], "verdict": p["verdict"]} for p in pairs if not p["valid"]]
    # a section that is simply absent from one snapshot (light profile, extension not installed) is informational; a discarded section (reset, duplicate key, ...) is not
    noted = sum(1 for p in pairs if p["valid"] and any(not n.endswith("missing in one snapshot") for n in p["notes"]))
    info = sum(1 for p in pairs if p["valid"] for n in p["notes"] if n.endswith("missing in one snapshot"))
    return {"snapshots": len(snaps), "pairs": len(pairs), "valid_pairs": len(valid), "analysis_empty_pairs": [{"from": p["from_phase"], "to": p["to_phase"]} for p in pairs if p["verdict"].startswith("ANALYSIS_EMPTY")],
            "failed_observations": failed, "partial_observations": degraded,
            "non_valid_pairs": bad_pairs, "valid_pairs_with_notes": noted, "informational_missing_section_notes": info,
            "complete": len(valid) == len(pairs) and not failed and not degraded and not noted,
            "analysable": bool(valid)}


def banner(sm):
    if not sm["analysable"]:
        return [f"**NO ANALYSABLE PAIR** - {sm['snapshots']} snapshot(s), {sm['pairs']} pair(s), 0 valid ({len(sm['analysis_empty_pairs'])} ANALYSIS_EMPTY: compatible snapshots, no counter compared). This is NOT a completed analysis; the observations below are the result "
                f"({len(sm['failed_observations'])} failed observation(s)).", ""]
    if not sm["complete"]:
        return [f"**PARTIAL** - {sm['valid_pairs']} of {sm['pairs']} pair(s) valid; {len(sm['failed_observations'])} failed observation(s), {len(sm['partial_observations'])} partial, "
                f"{len(sm['non_valid_pairs'])} pair(s) not valid, {sm['valid_pairs_with_notes']} valid pair(s) with discarded sections. Failed observations are listed, not hidden.", ""]
    return [f"Complete: {sm['valid_pairs']} of {sm['pairs']} pair(s) valid, no failed observation.", ""]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__ + "\nExit codes: 0 at least one valid pair | 2 input/usage/output error (nothing analysed) | 3 inputs readable but ZERO analysable pairs (report still written).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snapshots", nargs="*"); ap.add_argument("--md"); ap.add_argument("--json")
    a = ap.parse_args(argv)
    def err(msg):
        print(f"error: {msg}", file=sys.stderr); return EXIT_INPUT
    if len(a.snapshots) < 2:
        return err(f"at least 2 snapshot files are needed to form a pair (got {len(a.snapshots)})")
    resolved = [str(Path(p).resolve()) for p in a.snapshots]
    if len(set(resolved)) != len(resolved):
        return err("the same snapshot file is listed more than once")
    try:
        snaps = [load(p) for p in a.snapshots]
    except InputError as e:
        return err(str(e))
    pairs = [pair(snaps[i], snaps[i + 1]) for i in range(len(snaps) - 1)]
    for p, x, y in zip(pairs, snaps, snaps[1:]):
        if ts(y["t_start_utc"]) <= ts(x["t_start_utc"]):
            p["notes"].append("snapshots were given out of time order (client start times)")
    sm = summarize(snaps, pairs)
    md = "\n".join(banner(sm)) + "\n" + render(snaps, pairs)
    print(md)
    try:
        if a.md: Path(a.md).write_text(md, encoding="utf-8")
        if a.json: Path(a.json).write_text(json.dumps({"summary": sm, "snapshots": [s["_file"] for s in snaps], "pairs": pairs}, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    except OSError as e:
        return err(f"could not write the report file ({type(e).__name__}); the analysis above is on stdout only")
    return EXIT_OK if sm["analysable"] else EXIT_NO_PAIR


if __name__ == "__main__":
    sys.exit(main())
