"""Task #198 / CASSANDRA 5431 F4: probe the REAL PostgREST for the exact request shapes the patrol ledger uses.

Read-only by default (GET only).  The single write test (create / CAS / losing CAS / cleanup of ONE run-unique row
``patrol_meta::f4_probe::<id>``, only ever removed if this run created it) runs only with ``--allow-write --approval-event <id> --pin <sha>``;
the id must be a fresh MAGI registry event for task 198 naming that pin; it never touches patrol_alert rows.

    SUPABASE_URL=... SUPABASE_KEY=... SUPABASE_INTERNAL_RPC_SECRET=... python tests/live_probe_task198.py [--report out.json]

Nothing is printed except request shapes, status codes and counts (no keys, no row contents).  Exit 0 only if every probe passed.
"""
import argparse, json, os, sys, urllib.parse, uuid
from datetime import datetime, timedelta, timezone

import httpx


class Probe:
    def __init__(self, url, key, secret=None, transport=None):
        self.url = url.rstrip("/")
        self.h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.secret = secret
        self.client = httpx.Client(timeout=15, transport=transport)
        self.results = []

    def rec(self, name, ok, info=""):
        self.results.append({"name": name, "ok": bool(ok), "info": str(info)[:200]})
        print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)
        return ok

    def get(self, table, params, headers=None, count=True):
        h = dict(self.h, **(headers or {}))
        if count:
            h["Prefer"] = "count=exact"
        return self.client.get(f"{self.url}/rest/v1/{table}", params=params, headers=h)

    def run_read_only(self):
        r = self.get("scheduler_status", {"job_name": "like.patrol_alert::*", "order": "job_name.asc", "limit": "5"})
        self.rec("GET like.patrol_alert::* (wildcard '*', Content-Range, text order)", r.status_code in (200, 206)
                 and "/" in r.headers.get("content-range", ""), f"HTTP {r.status_code} range={r.headers.get('content-range')}")
        r = self.get("scheduler_status", {"job_name": "like.patrol_meta::*", "order": "job_name.asc", "limit": "5"})
        self.rec("GET like.patrol_meta::*", r.status_code in (200, 206), f"HTTP {r.status_code}")
        r = self.get("scheduler_status", {"last_run_at": "is.null", "limit": "5"})
        self.rec("GET last_run_at=is.null (nullable timestamp filter)", r.status_code in (200, 206) and isinstance(r.json(), list), f"HTTP {r.status_code}")
        # exact timestamp round-trip: the value PostgREST returned must match itself under eq. (the CAS predicate)
        r = self.get("scheduler_status", {"last_run_at": "not.is.null", "order": "job_name.asc", "limit": "1", "select": "job_name,last_run_at"})
        ok = r.status_code in (200, 206) and r.json() and isinstance(r.json()[0].get("last_run_at"), str)
        self.rec("GET one scheduler row with last_run_at", ok, f"HTTP {r.status_code}")
        if ok:
            row = r.json()[0]
            r2 = self.get("scheduler_status", {"job_name": f"eq.{row['job_name']}", "last_run_at": f"eq.{row['last_run_at']}", "select": "job_name"})
            self.rec("GET last_run_at=eq.<value PostgREST returned> matches exactly that row (CAS predicate round-trip, '+' encoded)",
                     r2.status_code == 200 and len(r2.json()) == 1, f"HTTP {r2.status_code} rows={len(r2.json()) if r2.status_code == 200 else '-'} ts_len={len(row['last_run_at'])}")
            shifted = (datetime.fromisoformat(row["last_run_at"].replace("Z", "+00:00")) + timedelta(microseconds=1)).isoformat()
            r3 = self.get("scheduler_status", {"job_name": f"eq.{row['job_name']}", "last_run_at": f"eq.{shifted}", "select": "job_name"})
            self.rec("GET last_run_at=eq.<value +1 microsecond> matches NOTHING (precision is not lost)", r3.status_code == 200 and r3.json() == [], f"HTTP {r3.status_code}")
        if self.secret:
            ih = {"X-MAGI-RPC-Secret": self.secret}
            for name, params in (("status=eq.PROCESSING & source_type=eq.google_drive", {"status": "eq.PROCESSING", "source_type": "eq.google_drive"}),
                                 ("status=eq.FAILED & source_type=eq.google_drive (ALL ages)", {"status": "eq.FAILED", "source_type": "eq.google_drive"})):
                p = dict(params, order="source_id.asc", limit="1000")
                r = self.get("call_image_ingestions", p, headers=ih)
                self.rec(f"GET call_image_ingestions {name} (secret-gated, complete-read shape)",
                         r.status_code in (200, 206) and isinstance(r.json(), list) and "/" in r.headers.get("content-range", ""),
                         f"HTTP {r.status_code} range={r.headers.get('content-range')}")
        else:
            self.rec("call_image_ingestions probes", False, "SUPABASE_INTERNAL_RPC_SECRET not set")
        # db-max-rows: how many rows does one unlimited request really return?
        r = self.get("scheduler_status", {"select": "job_name", "order": "job_name.asc"})
        total = r.headers.get("content-range", "").rsplit("/", 1)[-1]
        self.rec("db-max-rows observation (rows returned vs total)", r.status_code in (200, 206), f"returned={len(r.json())} total={total}")

    APPROVAL_ACTOR = "마기"
    APPROVAL_MAX_AGE = timedelta(hours=24)

    def verify_approval(self, event_id, pin):
        """CASSANDRA 5707 B2: the approval id is looked up in the MAGI registry and must be a REAL, fresh MAGI event for
        task 198 that names this exact pin and the probe scope.  Any lookup problem fails closed.

        Limit (stated, not hidden): registry rows can be written under any actor name (#202), so this proves a
        registry-level authorization exists, not who is holding the key; the executor is whoever holds the Supabase key,
        and the write test still must not run without MAGI naming that executor in the same approval text."""
        try:
            eid = int(str(event_id).strip())
            if eid <= 0 or not pin or len(pin) < 12:
                return False, "approval id / pin malformed"
            r = self.get("magi_task_events", {"event_id": f"eq.{eid}", "select": "event_id,task_id,actor,event_type,created_at,detail"}, count=False)
            rows = r.json() if r.status_code == 200 else None
            if not isinstance(rows, list) or len(rows) != 1:
                return False, f"approval event lookup HTTP {r.status_code}"
            e = rows[0]
            if e.get("task_id") != 198 or e.get("actor") != self.APPROVAL_ACTOR:
                return False, "approval event is not a MAGI event for task 198"
            created = datetime.fromisoformat(str(e.get("created_at")).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - created
            if age < timedelta(seconds=-60) or age > self.APPROVAL_MAX_AGE:
                return False, "approval event not within its 24h window"
            marker = f"F4_PROBE_WRITE_APPROVED pin={pin[:12]} key=patrol_meta::f4_probe"
            if marker not in str(e.get("detail") or ""):
                return False, "approval event does not name this pin and probe scope"
            return True, "approved"
        except Exception as exc:        # fail closed
            return False, f"approval check error {type(exc).__name__}"

    def run_write(self, approval_event, pin=""):
        ok, why = self.verify_approval(approval_event, pin)
        if not self.rec("write approval verified against the MAGI registry", ok, why):
            return
        run_id = uuid.uuid4().hex[:12]
        key = f"patrol_meta::f4_probe::{run_id}"
        tag = f"f4 probe owner={run_id}"
        j = {"job_name": key, "last_run_at": datetime.now(timezone.utc).isoformat(), "last_result": tag}
        wh = dict(self.h, Prefer="return=representation")
        created = False
        try:
            pre = self.get("scheduler_status", {"job_name": f"eq.{key}", "select": "job_name"})
            if not self.rec("pre-check: probe key does not exist", pre.status_code == 200 and pre.json() == [], f"HTTP {pre.status_code}"):
                return
            r = self.client.post(f"{self.url}/rest/v1/scheduler_status", json=j, headers=wh)      # plain insert: a duplicate must FAIL
            body = r.json() if r.status_code == 201 else []
            created = r.status_code == 201 and len(body) == 1 and body[0].get("last_result") == tag
            # the table may have no unique constraint on job_name: prove that exactly ONE row with this key exists and is ours
            if created:
                chk = self.get("scheduler_status", {"job_name": f"eq.{key}", "select": "last_result"}).json()
                created = len(chk) == 1 and chk[0].get("last_result") == tag
            if not self.rec("POST create probe row (plain insert, ownership tag echoed)", created, f"HTTP {r.status_code}"):
                return
            cur = self.get("scheduler_status", {"job_name": f"eq.{key}", "select": "last_run_at"}).json()
            old = cur[0]["last_run_at"] if cur else None
            new = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
            r = self.client.patch(f"{self.url}/rest/v1/scheduler_status", params={"job_name": f"eq.{key}", "last_run_at": f"eq.{old}"},
                                  json={"last_run_at": new, "last_result": tag}, headers=wh)
            self.rec("PATCH CAS with the exact old timestamp WINS (200 + 1 row)", r.status_code == 200 and len(r.json()) == 1, f"HTTP {r.status_code}")
            r = self.client.patch(f"{self.url}/rest/v1/scheduler_status", params={"job_name": f"eq.{key}", "last_run_at": f"eq.{old}"},
                                  json={"last_run_at": new, "last_result": tag}, headers=wh)
            self.rec("PATCH CAS with the stale timestamp LOSES (200 + [])", r.status_code == 200 and r.json() == [], f"HTTP {r.status_code}")
            r = self.client.patch(f"{self.url}/rest/v1/scheduler_status", params={"job_name": f"eq.{key}", "last_run_at": "is.null"},
                                  json={"last_result": tag}, headers=wh)
            self.rec("PATCH CAS expecting NULL on a non-null row LOSES (200 + [])", r.status_code == 200 and r.json() == [], f"HTTP {r.status_code}")
        finally:
            if created:        # remove ONLY the row this attempt created, and only while it still carries this attempt's tag
                try:
                    r = self.client.delete(f"{self.url}/rest/v1/scheduler_status",
                                           params={"job_name": f"eq.{key}", "last_result": f"eq.{tag}"}, headers=wh)
                    left = self.get("scheduler_status", {"job_name": f"eq.{key}", "select": "job_name"}).json()
                    self.rec("cleanup: own probe row removed", left == [], f"HTTP {r.status_code} left={len(left)}")
                except Exception as exc:
                    self.rec("cleanup: own probe row removed", False, f"cleanup failed {type(exc).__name__}; row {key} may remain")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-write", action="store_true")
    ap.add_argument("--approval-event", default="")
    ap.add_argument("--pin", default="", help="full commit SHA of the probe being run (must be named by the MAGI approval)")
    ap.add_argument("--report", default="")
    a = ap.parse_args(argv)
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_KEY not set"); return 2
    if a.allow_write and not a.approval_event:
        print("--allow-write needs --approval-event (MAGI approval id)"); return 2
    p = Probe(url, key, os.environ.get("SUPABASE_INTERNAL_RPC_SECRET"))
    p.run_read_only()
    if a.allow_write:
        p.run_write(a.approval_event, a.pin)
    ok = all(r["ok"] for r in p.results)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump({"approval_event": a.approval_event, "results": p.results, "ok": ok}, f, ensure_ascii=False, indent=1)
    print("ALL PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
