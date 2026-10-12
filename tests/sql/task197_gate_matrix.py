"""TASK#197 completion gate: exhaustive combination test on a THROWAWAY local PostgreSQL.

Never point this at production.  Usage:
    PGHOST=/path/to/socket_dir python3 tests/sql/task197_gate_matrix.py
The trigger from docs/COMPLETION_GATE_DESIGN_V1.md section 5 is installed in a scratch database and driven
with every combination below.  Expected values come from ``oracle_*`` functions that are written from the
design text (plain string logic, no regular expressions) so the SQL is not compared with a copy of itself.

A  evidence matrix     : event_type x evidence_kind x actor   (is a candidate event independent evidence?)
B  judgment sequences  : all CASSANDRA event sequences up to length 2 over 47 symbols, length 3 over 14
C  secondary matrix    : 마기 / other-actor secondary-style events x CASSANDRA presence
D  transition matrix   : verification_required x old status x new status (when does the trigger fire?)
"""
import itertools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PGHOST = os.environ.get("PGHOST", "")
PSQL = os.environ.get("PSQL", "psql")


def psql(q, db="gate", fetch=False):
    cmd = [PSQL, "-d", db, "-v", "ON_ERROR_STOP=1", "-tA", "-F", "\t"] + (["-h", PGHOST] if PGHOST else [])
    r = subprocess.run(cmd, input=q, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("SQL ERROR: " + r.stderr[-1200:])
    return r.stdout


def lit(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def jlit(payload):
    return "NULL" if payload is None else lit(json.dumps(payload, ensure_ascii=False)) + "::jsonb"


# ----------------------------------------------------------------------------- oracle (plain logic)
NOT_EVIDENCE_ACTORS = {"GEOMNURI_PATROL", "마기(자동)", "COMPLETION_GATE"}
EXCLUDED_WORDS = ("REQUEST", "REQUIRE", "READY", "VALIDATION", "VERIFICATION", "CASSANDRA")
# policy: event_type words are matched as whole underscore-delimited tokens or by exact allowlist, never as bare
# substrings of a word that also occurs inside a negation (INVALIDATION contains VALIDATION).
REVOKE_TOKENS = {"REVOKED", "REVOKE", "REVOCATION", "INVALIDATED", "INVALIDATION", "INVALID", "WITHDRAWN", "WITHDRAWAL",
                 "SUPERSEDED", "RETRACTED", "RETRACTION", "CANCELLED", "CANCELED", "VOID", "VOIDED", "ROLLBACK", "REVERTED", "REJECTED"}
NEG_TOKENS = REVOKE_TOKENS | {"FAILED", "FAILURE", "STALE", "OBSOLETE", "MISSING", "NOT", "NO"}
EVIDENCE_TYPES = {"EVIDENCE", "MAGI_EVIDENCE", "EVIDENCE_SUBMITTED", "IMPLEMENTATION_EVIDENCE", "IMPLEMENTATION_CI_PASS",
                  "IMPLEMENTATION_PR_DRAFT_CREATED", "TEST_EVIDENCE", "CI_EVIDENCE", "PRODUCTION_BRIDGE_GATE_EVIDENCE",
                  "FINAL_PRODUCTION_HTTP_E2E_EVIDENCE"}
NEUTRAL_TYPES = {"AGREEMENT_RESPONSE", "CASSANDRA_BACKLOG_REVIEW", "CASSANDRA_OPERATIONAL_GUIDANCE", "CASSANDRA_REGRESSION_STARTED"}
FINAL_TYPES = {"CASSANDRA_VALIDATION", "CASSANDRA_REVALIDATION", "CASSANDRA_IMPLEMENTATION_VALIDATION",
               "CASSANDRA_IMPLEMENTATION_REVALIDATION", "CASSANDRA_FINAL_VALIDATION", "CASSANDRA_FINAL_REVALIDATION"}


def toks(etype):
    return set(etype.upper().split("_"))
EVIDENCE_KINDS = {"IMPLEMENTATION", "TEST", "CI", "PRODUCTION"}
NEG_PREFIXES = ("PARTIAL", "HOLD", "FAIL", "CONDITIONAL", "REJECT")


def pd(payload):
    """payload as a dict; jsonb arrays/scalars/NULL carry no structured fields."""
    return payload if isinstance(payload, dict) else {}


def oracle_evidence(actor, etype, payload):
    if actor in NOT_EVIDENCE_ACTORS or actor.upper().startswith("CASSANDRA"):
        return False
    t = etype.upper()
    if any(w in t for w in EXCLUDED_WORDS):
        return False
    if toks(etype) & NEG_TOKENS:
        return False
    if t in EVIDENCE_TYPES:
        return True
    kind = (pd(payload).get("evidence_kind") or "")
    return str(kind).upper() in EVIDENCE_KINDS


def is_pass_word(v):
    """verdict is PASS or PASS_<LETTERS> (upper-case letters only)."""
    if v == "PASS":
        return True
    if v.startswith("PASS_") and len(v) > 5:
        tail = v[5:]
        return all("A" <= c <= "Z" for c in tail)
    return False


def oracle_judgment(events):
    """events: list of (actor, etype, payload) of one task, oldest first.  Returns (hard, review) lists."""
    cass = [(i, e) for i, e in enumerate(events) if e[0].upper().startswith("CASSANDRA")]
    if not cass:
        return ["NO_CASSANDRA_RECORD"], []
    judg = []
    for i, (actor, etype, payload) in cass:
        t = etype.upper()
        if "REQUEST" in t or "REQUIRE" in t:      # policy: a request is never a judgment, structured or not
            continue
        typed = "VALIDATION" in t or "DESIGN_PASS" in t or bool(toks(etype) & REVOKE_TOKENS)
        structured = "verdict" in pd(payload)
        if typed or structured:
            judg.append((i, etype, payload))
    if not judg:
        return [], ["NO_JUDGMENT_EVENT"]
    j_idx, etype, payload = judg[-1]
    if toks(etype) & REVOKE_TOKENS:   # a withdrawal/invalidation is never a pass, whatever stale verdict it carries
        return ["LATEST_JUDGMENT_REVOKED"], []
    if "verdict" not in pd(payload):
        return [], ["LATEST_VERDICT_UNSTRUCTURED"]
    v = str(pd(payload).get("verdict") or "").upper()
    st = str(pd(payload).get("stage") or "").upper()
    if v.startswith(NEG_PREFIXES):
        return ["LATEST_VERDICT_NOT_PASS"], []
    if is_pass_word(v):
        if "DESIGN" in v or "DESIGN" in etype.upper() or st == "DESIGN":
            return [], ["DESIGN_PASS_ONLY"]
        if v == "PASS" and st == "FINAL":
            if etype.upper() not in FINAL_TYPES:
                return [], ["PASS_NOT_FROM_VALIDATION_EVENT"]   # only an explicit validation verdict event may pass
            review = []
            # a PASS only stands if nothing after it questions it
            if any(k > j_idx and e[1].upper() not in NEUTRAL_TYPES for k, e in cass):
                review.append("LATER_CASSANDRA_EVENT_UNCLASSIFIED")
            if any(k > j_idx and oracle_evidence(*e) for k, e in enumerate(events)):
                review.append("EVIDENCE_NEWER_THAN_PASS")
            return [], review
        return [], ["PASS_STAGE_NOT_FINAL"]
    return ["LATEST_VERDICT_NOT_PASS"], []


def oracle_secondary(events):
    flags = [False, False]  # pass recorded, any secondary event
    for actor, etype, payload in events:
        p = pd(payload)
        if actor != "마기" or str(p.get("verifier_role") or "").upper() != "SECONDARY":
            continue
        flags[1] = True
        if ("REQUEST" not in etype.upper() and "REQUIRE" not in etype.upper()
                and str(p.get("verdict") or "").upper() == "PASS" and str(p.get("stage") or "").upper() == "FINAL"):
            flags[0] = True
    return flags


# ----------------------------------------------------------------------------- bulk runner
class Bulk:
    def __init__(self):
        self.next_id = 1000
        self.stmts = []
        self.cases = {}

    def task(self, label, events, vr=True, status="VERIFICATION"):
        tid = self.next_id
        self.next_id += 1
        self.stmts.append(
            f"insert into magi_tasks(task_id,title,status,priority,verification_required) "
            f"values ({tid},{lit(label)},{lit(status)},'P1',{str(vr).lower()});")
        for actor, etype, payload in events:
            self.stmts.append(
                f"insert into magi_task_events(task_id,event_type,actor,detail,payload) "
                f"values ({tid},{lit(etype)},{lit(actor)},'x',{jlit(payload)});")
        self.cases[tid] = (label, events)
        return tid

    def run(self, new_status="COMPLETED"):
        ids = sorted(self.cases)
        sql = "begin;\n" + "\n".join(self.stmts) + (
            f"\nupdate magi_tasks set status={lit(new_status)} where task_id between {ids[0]} and {ids[-1]};\ncommit;\n")
        psql(sql)
        out = psql("select task_id, payload->>'gate_level', payload->'reasons', payload->'checks' "
                   "from magi_task_events where event_type='COMPLETION_GATE_VIOLATION' order by task_id;")
        got = {}
        for line in out.splitlines():
            tid, level, reasons, checks = line.split("\t")
            got.setdefault(int(tid), []).append((level, json.loads(reasons), json.loads(checks)))
        return got


def reset():
    psql("drop database if exists gate;", "postgres")
    psql("create database gate;", "postgres")
    psql((ROOT / "tests" / "sql" / "task197_gate_schema.sql").read_text(encoding="utf-8"))
    doc = (ROOT / "docs" / "COMPLETION_GATE_DESIGN_V1.md").read_text(encoding="utf-8")
    psql(re.search(r"## 5\. .*?```sql\n(.*?)```", doc, re.S).group(1))


failures = []


def compare(part, got, bulk, expect_fn):
    n = 0
    for tid, (label, events) in bulk.cases.items():
        n += 1
        exp = expect_fn(events)
        rows = got.get(tid, [])
        if exp is None:  # expect no violation event
            if rows:
                failures.append((part, label, "expected no event", rows))
            continue
        if len(rows) != 1:
            failures.append((part, label, f"expected 1 event, got {len(rows)}", exp))
            continue
        level, reasons, checks = rows[0]
        exp_reasons, exp_level, exp_checks = exp
        if sorted(reasons) != sorted(exp_reasons) or level != exp_level:
            failures.append((part, label, "reasons/level differ", {"got": [level, reasons], "want": [exp_level, exp_reasons]}))
        for k, v in (exp_checks or {}).items():
            if checks.get(k) != v:
                failures.append((part, label, f"check {k} differs", {"got": checks.get(k), "want": v}))
    print(f"{part}: {n} cases, {sum(1 for f in failures if f[0] == part)} mismatches")
    return n


PASS_FINAL = ("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": "PASS", "stage": "FINAL", "tier": "HIGH"})
EVID = ("캐스퍼", "EVIDENCE", None)


def expected(events, check_secondary=False):
    hard, review = oracle_judgment(events)
    evid = any(oracle_evidence(a, t, p) for a, t, p in events)
    if not evid:
        review = review + ["EVIDENCE_NOT_RECOGNIZED"]
    if not hard and not review:
        return None
    checks = {"independent_evidence": evid}
    if check_secondary:
        sp, so = oracle_secondary(events)
        checks.update({"secondary_pass_recorded": sp, "secondary_events_recorded": so})
    return hard + review, ("WARN" if hard else "REVIEW"), checks


def main():
    reset()
    total = 0

    # A. evidence matrix ------------------------------------------------------------------
    types = ["EVIDENCE", "MAGI_EVIDENCE", "EVIDENCE_SUBMITTED", "IMPLEMENTATION_CI_PASS", "IMPLEMENTATION_PR_DRAFT_CREATED",
             "PRODUCTION_BRIDGE_GATE_EVIDENCE", "FINAL_PRODUCTION_HTTP_E2E_EVIDENCE", "VERIFICATION_COMPLETED",
             "CASSANDRA_VALIDATION", "CASSANDRA_DESIGN_PASS", "IMPLEMENTATION_READY_FOR_VERIFICATION", "VERIFICATION_READY",
             "VERIFICATION_REQUESTED", "IMPLEMENTATION_REQUESTED", "VERIFICATION_REQUEST", "CASSANDRA_VALIDATION_REQUESTED",
             "CASSANDRA_EVIDENCE_REQUEST", "DESIGN_V2_EVIDENCE_REQUIREMENTS", "CASSANDRA_REVALIDATION_REQUESTED",
             "IMPLEMENTATION_PATCH_COMMITTED_REVALIDATION_REQUIRED", "MAGI_DECISION", "TASK_UPDATED", "DESIGN_SUBMITTED",
             "evidence", "Implementation_Note", "CI_RESULT", "TEST_RESULT", "EVIDENCE_VALIDATION", "IMPLEMENTATION_VERIFICATION",
             "EVIDENCE_READY",
             # added after CASSANDRA 5024 and from the real production vocabulary
             "EVIDENCE_REVOKED", "EVIDENCE_INVALIDATED", "INVALID_EVIDENCE", "NO_EVIDENCE", "EVIDENCE_MISSING", "EVIDENCE_NOT_FOUND",
             "IMPLEMENTATION_REJECTED", "IMPLEMENTATION_CI_FAILED", "IMPLEMENTATION_CI_PASS_WITHDRAWN", "EVIDENCE_SUPERSEDED",
             "YOUNGSIL_GAS_IMPLEMENTATION_SPEC", "EVIDENCE_ONLY_BRIEFING_LIVE", "TEST_EVIDENCE", "IMPLEMENTATION_EVIDENCE",
             "CI_EVIDENCE", "CI_EVIDENCE_RECOVERED", "GOLDEN_TEST_STAGED", "DEPENDENCY_EVIDENCE_LINKED", "TASK_CANCELLED",
             "STATUS_REVERTED", "CASSANDRA_HOLD_EVIDENCE_VISIBILITY_GAP", "METER_COMPLETENESS_EVIDENCE_FROM_TASK186",
             "GAS_GPX_IMPLEMENTATION_REQUESTED", "MCP_BROKER_PROPOSAL_RETRACTED", "FINAL_CONTROL_PLANE_NATURAL_E2E_EVIDENCE"]
    kinds = [None, "", "IMPLEMENTATION", "TEST", "CI", "PRODUCTION", "implementation", "ci", "OTHER", "VERIFICATION"]
    actors = ["캐스퍼", "마기", "마기(자동)", "TAEO", "CASSANDRA", "cassandra", "CASSANDRA_AGENT", "GEOMNURI_PATROL",
              "COMPLETION_GATE", "영실", "신규 실행 책임 에이전트", "ARGOS"]
    A = Bulk()
    for et, ek, ac in itertools.product(types, kinds, actors):
        payload = None if ek is None else {"evidence_kind": ek}
        A.task(f"A|{ac}|{et}|{ek}", [(ac, et, payload), PASS_FINAL])
    total += compare("A evidence matrix", A.run(), A, lambda ev: expected(ev))

    # B. judgment sequences ---------------------------------------------------------------
    reset()
    verdicts = ["PASS", "pass", "PARTIAL_PASS", "HOLD", "FAIL", "CONDITIONAL", "MAYBE", "PASS_DESIGN", "FAIL_DESIGN", "REJECT", "PASS_FINAL", "PASS_X1"]
    stages = ["FINAL", "DESIGN", "IMPLEMENTATION", None]
    full = []
    for v in verdicts:
        for st in stages:
            payload = {"verdict": v}
            if st:
                payload["stage"] = st
            full.append((f"V[{v}/{st}]", ("CASSANDRA", "CASSANDRA_VALIDATION", payload)))
    extra = [
        ("U{}", ("CASSANDRA", "CASSANDRA_VALIDATION", {})),
        ("Unull", ("CASSANDRA", "CASSANDRA_VALIDATION", None)),
        ("Udesign", ("CASSANDRA", "CASSANDRA_DESIGN_PASS", None)),
        ("Nagree", ("CASSANDRA", "AGREEMENT_RESPONSE", {"agreement": "CONDITIONAL_AGREE"})),
        ("Nbacklog", ("CASSANDRA", "CASSANDRA_BACKLOG_REVIEW", None)),
        ("Nevreq", ("CASSANDRA", "CASSANDRA_EVIDENCE_REQUEST", None)),
        ("Nvreq", ("CASSANDRA", "CASSANDRA_VALIDATION_REQUESTED", None)),
        ("Xguide", ("CASSANDRA", "CASSANDRA_OPERATIONAL_GUIDANCE", {"verdict": "HOLD", "stage": "FINAL"})),
        ("Vdesignevt", ("CASSANDRA", "CASSANDRA_DESIGN_VALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vrevalid", ("CASSANDRA", "CASSANDRA_IMPLEMENTATION_REVALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vreq", ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUESTED", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vreqlow", ("CASSANDRA", "cassandra_revalidation_requested", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vrequire", ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUIRED", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vreq2", ("CASSANDRA", "CASSANDRA_VALIDATION_REQUESTED", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vreqhold", ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUESTED", {"verdict": "HOLD", "stage": "FINAL"})),
        ("Vagree", ("CASSANDRA", "AGREEMENT_RESPONSE", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vguide", ("CASSANDRA", "CASSANDRA_OPERATIONAL_GUIDANCE", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vcomplete", ("CASSANDRA", "VERIFICATION_COMPLETED", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vdlow", ("CASSANDRA", "cassandra_design_validation", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vdmix", ("CASSANDRA", "Cassandra_Design_Validation", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vdup", ("CASSANDRA", "CASSANDRA_DESIGN_VALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vvlow", ("CASSANDRA", "cassandra_validation", {"verdict": "PASS", "stage": "FINAL"})),
        ("Vrevalow", ("CASSANDRA", "cassandra_implementation_revalidation", {"verdict": "PASS", "stage": "FINAL"})),
        ("Parr", ("CASSANDRA", "CASSANDRA_VALIDATION", ["verdict"])),
        ("Pstr", ("CASSANDRA", "CASSANDRA_VALIDATION", "verdict")),
        ("Pnullv", ("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": None, "stage": "FINAL"})),
        ("Pnumv", ("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": 123, "stage": "FINAL"})),
        ("Plistv", ("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": ["PASS"], "stage": "FINAL"})),
        ("Pspace", ("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": " PASS", "stage": "FINAL"})),
        ("Pstagespace", ("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": "PASS", "stage": "FINAL "})),
        ("Rinval", ("CASSANDRA", "CASSANDRA_INVALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Rrevoked", ("CASSANDRA", "CASSANDRA_VALIDATION_REVOKED", {"verdict": "PASS", "stage": "FINAL"})),
        ("Rrevlow", ("CASSANDRA", "cassandra_validation_revoked", {"verdict": "PASS", "stage": "FINAL"})),
        ("Rrevnull", ("CASSANDRA", "CASSANDRA_VALIDATION_REVOKED", None)),
        ("Rwithdrawn", ("CASSANDRA", "CASSANDRA_VALIDATION_WITHDRAWN", {"verdict": "PASS", "stage": "FINAL"})),
        ("Rsuperseded", ("CASSANDRA", "CASSANDRA_VALIDATION_SUPERSEDED", None)),
        ("Rrejected", ("CASSANDRA", "CASSANDRA_VALIDATION_REJECTED", {"verdict": "PASS", "stage": "FINAL"})),
        ("Rrollback", ("CASSANDRA", "CASSANDRA_ROLLBACK", {"verdict": "PASS", "stage": "FINAL"})),
        ("Rvoid", ("CASSANDRA", "CASSANDRA_VOID_VALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Ainval2", ("CASSANDRA", "CASSANDRA_VALIDATION_STATUS", {"verdict": "PASS", "stage": "FINAL"})),
        ("Apre", ("CASSANDRA", "CASSANDRA_PREVALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Aimpl", ("CASSANDRA", "CASSANDRA_IMPLEMENTATION_VALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Afinal", ("CASSANDRA", "CASSANDRA_FINAL_VALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Areval", ("CASSANDRA", "CASSANDRA_REVALIDATION", {"verdict": "PASS", "stage": "FINAL"})),
        ("Anote", ("CASSANDRA", "CASSANDRA_VALIDATION_NOTE", {"verdict": "PASS", "stage": "FINAL"})),
        ("Lresc", ("CASSANDRA", "CASSANDRA_RESCINDED_PASS", None)),
        ("Lannul", ("CASSANDRA", "CASSANDRA_PASS_ANNULLED", {"verdict": "PASS", "stage": "FINAL"})),
        ("Lhold", ("CASSANDRA", "CASSANDRA_HOLD_NOTICE", None)),
        ("Nback", ("CASSANDRA", "CASSANDRA_BACKLOG_REVIEW", None)),
        ("Nguide2", ("CASSANDRA", "CASSANDRA_OPERATIONAL_GUIDANCE", None)),
        ("Nreg", ("CASSANDRA", "CASSANDRA_REGRESSION_STARTED", None)),
        ("Nagr2", ("CASSANDRA", "agreement_response", None)),
        ("Eaft", ("캐스퍼", "EVIDENCE", None)),
        ("Ekind", ("캐스퍼", "TEST_RESULT", {"evidence_kind": "TEST"})),
        ("Erev", ("캐스퍼", "EVIDENCE_REVOKED", None)),
        ("Eother", ("캐스퍼", "TASK_UPDATED", None)),
        ("Eauto", ("마기(자동)", "EVIDENCE", None)),
        ("Ureq", ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUESTED", None)),
        ("Ureq2", ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUESTED", {"note": "x"})),
    ]
    alphabet = full + extra
    B = Bulk()
    for n in (1, 2):
        for seq in itertools.product(alphabet, repeat=n):
            B.task("B|" + ">".join(s[0] for s in seq), [EVID] + [s[1] for s in seq])
    reduced_names = {"V[PASS/FINAL]", "V[PASS/DESIGN]", "V[HOLD/FINAL]", "V[FAIL/FINAL]", "U{}", "Unull", "Nagree", "Xguide",
                     "Rinval", "Rrevoked", "Rrevnull", "Ainval2", "Aimpl", "Anote", "Vreq", "Vagree", "Lresc", "Lhold", "Nback",
                     "Eaft", "Ekind", "Erev", "Eother", "Eauto"}
    reduced = [a for a in alphabet if a[0] in reduced_names]
    assert len(reduced) == len(reduced_names), (len(reduced), reduced_names - {a[0] for a in reduced})
    for seq in itertools.product(reduced, repeat=3):
        B.task("B|" + ">".join(s[0] for s in seq), [EVID] + [s[1] for s in seq])
    total += compare("B judgment sequences", B.run(), B, lambda ev: expected(ev))

    # C. secondary matrix -----------------------------------------------------------------
    reset()
    sec_events = {
        "none": None,
        "valid": ("마기", "MAGI_DECISION", {"verdict": "PASS", "stage": "FINAL", "verifier_role": "SECONDARY", "tier": "LOW"}),
        "fail": ("마기", "MAGI_DECISION", {"verdict": "FAIL", "stage": "FINAL", "verifier_role": "SECONDARY"}),
        "design": ("마기", "MAGI_DECISION", {"verdict": "PASS", "stage": "DESIGN", "verifier_role": "SECONDARY"}),
        "norole": ("마기", "MAGI_DECISION", {"verdict": "PASS", "stage": "FINAL"}),
        "primary": ("마기", "MAGI_DECISION", {"verdict": "PASS", "stage": "FINAL", "verifier_role": "PRIMARY"}),
        "request": ("마기", "SECONDARY_VERIFICATION_REQUESTED", {"verdict": "PASS", "stage": "FINAL", "verifier_role": "SECONDARY"}),
        "auto": ("마기(자동)", "MAGI_DECISION", {"verdict": "PASS", "stage": "FINAL", "verifier_role": "SECONDARY"}),
        "taeo": ("TAEO", "MAGI_DECISION", {"verdict": "PASS", "stage": "FINAL", "verifier_role": "SECONDARY"}),
        "lower": ("마기", "MAGI_DECISION", {"verdict": "pass", "stage": "final", "verifier_role": "secondary"}),
    }
    cass_parts = {
        "none": [], "pass": [PASS_FINAL],
        "hold": [("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": "HOLD", "stage": "FINAL"})],
        "unstructured": [("CASSANDRA", "CASSANDRA_VALIDATION", None)],
        "pass_then_hold": [PASS_FINAL, ("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": "HOLD", "stage": "FINAL"})],
    }
    C = Bulk()
    for (sn, se), (cn, cp), evidence in itertools.product(sec_events.items(), cass_parts.items(), (True, False)):
        evs = ([EVID] if evidence else []) + ([se] if se else []) + cp
        C.task(f"C|sec={sn}|cass={cn}|evid={evidence}", evs)
    total += compare("C secondary matrix", C.run(), C, lambda ev: expected(ev, check_secondary=True))

    # D. transition matrix ----------------------------------------------------------------
    reset()
    statuses = ["CREATED", "ASSIGNED", "IN_PROGRESS", "WAITING", "BLOCKED", "HANDOFF", "REVIEW", "VERIFICATION", "COMPLETED", "HOLD", "CANCELLED", "CLOSED"]
    D_fail = 0
    cases = []
    for vr, old, new in itertools.product([True, False, None], statuses, statuses):
        tid = 50000 + len(cases)
        cases.append((tid, vr, old, new))
    sql = ["begin;"]
    for tid, vr, old, new in cases:
        vrl = "NULL" if vr is None else str(vr).lower()
        sql.append(f"insert into magi_tasks(task_id,title,status,priority,verification_required) values ({tid},'D',{lit(old)},'P1',{vrl});")
        sql.append(f"update magi_tasks set status={lit(new)} where task_id={tid};")
    sql.append("commit;")
    psql("\n".join(sql))
    out = psql("select task_id, count(*) from magi_task_events where event_type='COMPLETION_GATE_VIOLATION' group by 1;")
    counts = {int(l.split("\t")[0]): int(l.split("\t")[1]) for l in out.splitlines()}
    for tid, vr, old, new in cases:
        should = vr is True and new in ("COMPLETED", "CLOSED") and old not in ("COMPLETED", "CLOSED")
        if counts.get(tid, 0) != (1 if should else 0):
            D_fail += 1
            failures.append(("D transition matrix", f"vr={vr} {old}->{new}", "fire mismatch", counts.get(tid, 0)))
    print(f"D transition matrix: {len(cases)} cases, {D_fail} mismatches")
    total += len(cases)

    # D2. verification_required changed in the same UPDATE as the completion
    reset()
    vrs = [True, False, None]
    c2 = []
    for old_vr, new_vr, new_status in itertools.product(vrs, vrs, ["COMPLETED", "CLOSED", "IN_PROGRESS"]):
        c2.append((60000 + len(c2), old_vr, new_vr, new_status))
    sql = ["begin;"]
    nl = lambda v: "NULL" if v is None else str(v).lower()
    for tid, ov, nv, ns in c2:
        sql.append(f"insert into magi_tasks(task_id,title,status,priority,verification_required) values ({tid},'D2','VERIFICATION','P1',{nl(ov)});")
        sql.append(f"update magi_tasks set verification_required={nl(nv)}, status={lit(ns)} where task_id={tid};")
    sql.append("commit;")
    psql("\n".join(sql))
    out = psql("select task_id, payload->'reasons' from magi_task_events where event_type='COMPLETION_GATE_VIOLATION';")
    got2 = {int(l.split("\t")[0]): json.loads(l.split("\t")[1]) for l in out.splitlines()}
    d2_fail = 0
    for tid, ov, nv, ns in c2:
        should = (ov is True or nv is True) and ns in ("COMPLETED", "CLOSED")
        cleared = should and nv is not True
        reasons = got2.get(tid)
        ok = (reasons is not None) == should and (not should or ("VERIFICATION_REQUIRED_CLEARED" in reasons) == cleared)
        if not ok:
            d2_fail += 1
            failures.append(("D2 vr change", f"{ov}->{nv} {ns}", "mismatch", reasons))
    print(f"D2 verification_required change: {len(c2)} cases, {d2_fail} mismatches")
    total += len(c2)

    # E. hand-written spec expectations (independent of the oracle): CASSANDRA 5018 counterexamples
    reset()
    E = Bulk()
    HOLD = ("CASSANDRA", "CASSANDRA_VALIDATION", {"verdict": "HOLD", "stage": "FINAL"})
    spec = [
        ("99021 request w/ PASS+FINAL only", [EVID, ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUESTED", {"verdict": "PASS", "stage": "FINAL"})], ["NO_JUDGMENT_EVENT"], "REVIEW"),
        ("99022 HOLD then request w/ PASS+FINAL", [EVID, HOLD, ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUESTED", {"verdict": "PASS", "stage": "FINAL"})], ["LATEST_VERDICT_NOT_PASS"], "WARN"),
        ("99023 HOLD then unstructured request", [EVID, HOLD, ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUESTED", None)], ["LATEST_VERDICT_NOT_PASS"], "WARN"),
        ("99110 lowercase design validation PASS/FINAL", [EVID, ("CASSANDRA", "cassandra_design_validation", {"verdict": "PASS", "stage": "FINAL"})], ["DESIGN_PASS_ONLY"], "REVIEW"),
        ("99111 uppercase design validation PASS/FINAL", [EVID, ("CASSANDRA", "CASSANDRA_DESIGN_VALIDATION", {"verdict": "PASS", "stage": "FINAL"})], ["DESIGN_PASS_ONLY"], "REVIEW"),
        ("agreement PASS/FINAL is not a pass", [EVID, ("CASSANDRA", "AGREEMENT_RESPONSE", {"verdict": "PASS", "stage": "FINAL"})], ["PASS_NOT_FROM_VALIDATION_EVENT"], "REVIEW"),
        ("PASS/FINAL then request w/ HOLD: request is not a judgment but questions the PASS", [EVID, PASS_FINAL, ("CASSANDRA", "CASSANDRA_REVALIDATION_REQUESTED", {"verdict": "HOLD", "stage": "FINAL"})], ["LATER_CASSANDRA_EVENT_UNCLASSIFIED"], "REVIEW"),
        ("agreement response after PASS is neutral", [EVID, PASS_FINAL, ("CASSANDRA", "AGREEMENT_RESPONSE", {"agreement": "OK"})], None, None),
        ("H1 unlisted revocation word after PASS", [EVID, PASS_FINAL, ("CASSANDRA", "CASSANDRA_RESCINDED_PASS", None)], ["LATER_CASSANDRA_EVENT_UNCLASSIFIED"], "REVIEW"),
        ("H2 annulled after PASS", [EVID, PASS_FINAL, ("CASSANDRA", "CASSANDRA_PASS_ANNULLED", None)], ["LATER_CASSANDRA_EVENT_UNCLASSIFIED"], "REVIEW"),
        ("H3 stale PASS then newer evidence", [EVID, PASS_FINAL, ("캐스퍼", "EVIDENCE", None)], ["EVIDENCE_NEWER_THAN_PASS"], "REVIEW"),
        ("H3b newer evidence_kind event", [EVID, PASS_FINAL, ("캐스퍼", "TEST_RESULT", {"evidence_kind": "TEST"})], ["EVIDENCE_NEWER_THAN_PASS"], "REVIEW"),
        ("H3c newer non-evidence event does not matter", [EVID, PASS_FINAL, ("캐스퍼", "TASK_UPDATED", None)], None, None),
        ("H3d newer revoked-evidence event does not matter", [EVID, PASS_FINAL, ("캐스퍼", "EVIDENCE_REVOKED", None)], None, None),
        ("H3e evidence only before PASS passes", [EVID, ("캐스퍼", "EVIDENCE_SUBMITTED", None), PASS_FINAL], None, None),
        ("H4 hold notice after PASS", [EVID, PASS_FINAL, ("CASSANDRA", "CASSANDRA_HOLD_NOTICE", None)], ["LATER_CASSANDRA_EVENT_UNCLASSIFIED"], "REVIEW"),
        ("PASS again after stale evidence passes", [EVID, PASS_FINAL, ("캐스퍼", "EVIDENCE", None), PASS_FINAL], None, None),
        ("5024 invalidation with stale PASS/FINAL", [EVID, ("CASSANDRA", "CASSANDRA_INVALIDATION", {"verdict": "PASS", "stage": "FINAL"})], ["LATEST_JUDGMENT_REVOKED"], "WARN"),
        ("5024 validation revoked with stale PASS/FINAL", [EVID, PASS_FINAL, ("CASSANDRA", "CASSANDRA_VALIDATION_REVOKED", {"verdict": "PASS", "stage": "FINAL"})], ["LATEST_JUDGMENT_REVOKED"], "WARN"),
        ("revoked then re-issued validation passes", [EVID, PASS_FINAL, ("CASSANDRA", "CASSANDRA_VALIDATION_REVOKED", None), PASS_FINAL], None, None),
        ("validation status event is not an authorised final type", [EVID, ("CASSANDRA", "CASSANDRA_VALIDATION_STATUS", {"verdict": "PASS", "stage": "FINAL"})], ["PASS_NOT_FROM_VALIDATION_EVENT"], "REVIEW"),
        ("revoked evidence is not evidence", [("캐스퍼", "EVIDENCE_REVOKED", None), PASS_FINAL], ["EVIDENCE_NOT_RECOGNIZED"], "REVIEW"),
        ("spec event is not evidence", [("영실", "YOUNGSIL_GAS_IMPLEMENTATION_SPEC", None), PASS_FINAL], ["EVIDENCE_NOT_RECOGNIZED"], "REVIEW"),
        ("revoked evidence_kind event is not evidence", [("캐스퍼", "TEST_RESULT_INVALIDATED", {"evidence_kind": "TEST"}), PASS_FINAL], ["EVIDENCE_NOT_RECOGNIZED"], "REVIEW"),
        ("lowercase validation PASS/FINAL passes", [EVID, ("CASSANDRA", "cassandra_validation", {"verdict": "PASS", "stage": "FINAL"})], None, None),
    ]
    for label, evs, _, _ in spec:
        E.task("E|" + label, evs)
    got = E.run()
    by_label = {lab: tid for tid, (lab, _) in E.cases.items()}
    for label, evs, reasons, level in spec:
        rows = got.get(by_label["E|" + label], [])
        if reasons is None:
            ok = not rows
        else:
            ok = len(rows) == 1 and rows[0][0] == level and sorted(rows[0][1]) == sorted(reasons)
        if not ok:
            failures.append(("E spec", label, "differs from hand-written expectation", rows))
    print(f"E spec expectations: {len(spec)} cases, {sum(1 for f in failures if f[0] == 'E spec')} mismatches")
    total += len(spec)

    print(f"\nTOTAL {total} combinations, {len(failures)} mismatches")
    for f in failures[:25]:
        print("MISMATCH", f)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
