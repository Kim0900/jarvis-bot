"""Task #198: READ-ONLY smoke of the REAL PostgREST for the exact GET request shapes the patrol ledger/ingestion check uses.

The WRITE probe (create / CAS / losing CAS / cleanup of a unique ``patrol_meta::f4_probe::<id>`` row) is NOT part of this file or this PR any more
(MAGI #5721, CASSANDRA #5718): its approval check was forgeable (#202 actor spoofing, free-form marker, caller-chosen --pin).  It lives only in the
git history (commit 23ec8aa) and is a separate follow-up task that needs a signed MAGI authority and an upload-free window.

    SUPABASE_URL=... SUPABASE_KEY=... [SUPABASE_INTERNAL_RPC_SECRET=...] python tests/live_smoke_task198_readonly.py --not-after <ISO time> [--report out.json]

Guarantees (each is tested against the fake server in tests/test_task198_live_probe.py):
  * GET only: the HTTP client handed to the probe exposes nothing but ``get``; post/patch/put/delete/request raise before any network traffic.
  * ``--not-after`` (the end of MAGI's permission window) is REQUIRED; at or after that instant no request is sent.
  * the first 5xx, timeout, transport error or any unexpected exception stops the run at once (exit 3); there is no retry.
  * requests are sequential, about ten in total, 15 s timeout each.
  * the report contains only: commit, times, the request list (table + parameter NAMES), status codes, Content-Range, row counts, pass/fail per check.
    No key, no secret, no row contents.  Without SUPABASE_INTERNAL_RPC_SECRET the secret-gated call_image_ingestions checks are reported NOT RUN (not passed).
Exit codes: 0 every check passed, 1 a check failed, 2 usage/expired, 3 stopped early.  The result is evidence for CASSANDRA, never a self-PASS.
"""
import argparse, json, os, subprocess, sys
from datetime import datetime, timedelta, timezone

import httpx


class Stop(Exception):
    """first 5xx / timeout / transport error / expired window: end the run, no retry"""


class GetOnlyClient:
    def __init__(self, client):
        self._c = client

    def get(self, *a, **k):
        return self._c.get(*a, **k)

    def __getattr__(self, name):
        if name in ("post", "patch", "put", "delete", "request", "send", "stream", "head", "options"):
            raise Stop(f"BLOCKED: {name.upper()} is not available in the read-only smoke")
        raise AttributeError(name)


class ReadSmoke:
    def __init__(self, url, key, secret=None, not_after=None, transport=None, now=None):
        self.url = url.rstrip("/")
        self.h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.secret = secret
        self.not_after = not_after
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.client = GetOnlyClient(httpx.Client(timeout=15, transport=transport))
        self.results, self.requests = [], []

    def rec(self, name, ok, info=""):
        self.results.append({"name": name, "ok": bool(ok), "info": str(info)[:200]})
        print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)
        return ok

    def not_run(self, name, why):
        self.results.append({"name": name, "ok": False, "not_run": True, "info": why})
        print("NOT RUN " + name + f"  [{why}]", flush=True)

    def get(self, table, params, headers=None, count=True):
        if self.not_after is not None and self._now() >= self.not_after:
            raise Stop("permission window ended: no further request is sent")
        h = dict(self.h, **(headers or {}))
        if count:
            h["Prefer"] = "count=exact"
        e = {"n": len(self.requests) + 1, "method": "GET", "table": table, "param_names": sorted(params), "secret_header": bool(headers)}
        self.requests.append(e)
        try:
            r = self.client.get(f"{self.url}/rest/v1/{table}", params=params, headers=h)
        except Stop:
            raise
        except Exception as ex:                       # timeout, connection error, anything: stop, never retry
            e.update(status=None, error=type(ex).__name__)
            raise Stop(f"{type(ex).__name__} on request {e['n']}")
        rows = None
        try:
            j = r.json(); rows = len(j) if isinstance(j, list) else None
        except Exception:
            pass
        e.update(status=r.status_code, content_range=r.headers.get("content-range"), rows=rows)
        if r.status_code >= 500:
            raise Stop(f"HTTP {r.status_code} on request {e['n']}")
        return r

    def run(self):
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
        for name, params in (("status=eq.PROCESSING & source_type=eq.google_drive", {"status": "eq.PROCESSING", "source_type": "eq.google_drive"}),
                             ("status=eq.FAILED & source_type=eq.google_drive (ALL ages)", {"status": "eq.FAILED", "source_type": "eq.google_drive"})):
            label = f"GET call_image_ingestions {name} (secret-gated, complete-read shape)"
            if not self.secret:
                self.not_run(label, "SUPABASE_INTERNAL_RPC_SECRET not set")
                continue
            r = self.get("call_image_ingestions", dict(params, order="source_id.asc", limit="1000"), headers={"X-MAGI-RPC-Secret": self.secret})
            self.rec(label, r.status_code in (200, 206) and isinstance(r.json(), list) and "/" in r.headers.get("content-range", ""),
                     f"HTTP {r.status_code} range={r.headers.get('content-range')}")
        # db-max-rows: how many rows does one unlimited request really return?
        r = self.get("scheduler_status", {"select": "job_name", "order": "job_name.asc"})
        total = r.headers.get("content-range", "").rsplit("/", 1)[-1]
        self.rec("db-max-rows observation (rows returned vs total)", r.status_code in (200, 206), f"returned={len(r.json())} total={total}")


def git_commit():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        run = lambda *a: subprocess.run(["git", "-C", here, *a], capture_output=True, text=True, timeout=20).stdout.strip()
        return {"commit": run("rev-parse", "HEAD"), "tracked_tree_clean": not run("status", "--porcelain", "--untracked-files=no")}
    except Exception:
        return {"commit": None, "tracked_tree_clean": None}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--not-after", required=True, help="end of MAGI's permission window, ISO 8601 with offset (e.g. 2026-10-20T14:00:00+09:00)")
    ap.add_argument("--report", default="")
    a = ap.parse_args(argv)
    try:
        not_after = datetime.fromisoformat(a.not_after)
        if not_after.tzinfo is None:
            raise ValueError
    except ValueError:
        print("--not-after must be an ISO 8601 time WITH a UTC offset"); return 2
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_KEY not set"); return 2
    rf = None
    if a.report:                                       # claim the report file BEFORE any request: an existing/unwritable path must not cost a run
        try:
            rf = open(a.report, "x", encoding="utf-8")
        except OSError as ex:
            print(f"--report {a.report!r} cannot be created ({type(ex).__name__}); nothing was sent"); return 2
    p = ReadSmoke(url, key, os.environ.get("SUPABASE_INTERNAL_RPC_SECRET"), not_after=not_after)
    start = datetime.now(timezone.utc).isoformat(timespec="seconds"); stopped = None
    try:
        p.run()
    except Stop as s:
        stopped = str(s)
    except Exception as ex:                            # anything unexpected also ends the run
        stopped = f"unexpected {type(ex).__name__}"
    except KeyboardInterrupt:                          # Ctrl-C: still write what was observed
        stopped = "interrupted"
    ok = stopped is None and all(r["ok"] for r in p.results)
    rep = dict(git_commit(), start_utc=start, end_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"), not_after=a.not_after, stopped_early=stopped,
               requests=p.requests, results=p.results, secret_set=bool(p.secret), ok=ok)
    if rf is not None:
        with rf:
            json.dump(rep, rf, ensure_ascii=False, indent=1)
    print("STOPPED EARLY: " + stopped if stopped else ("ALL PASS" if ok else "FAILED"))
    return 3 if stopped else (0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
