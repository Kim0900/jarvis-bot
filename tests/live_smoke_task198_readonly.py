"""Task #198: READ-ONLY smoke of the REAL PostgREST for the exact GET request shapes the patrol ledger/ingestion check uses.

The WRITE probe (create / CAS / losing CAS / cleanup of a unique ``patrol_meta::f4_probe::<id>`` row) is NOT part of this file or this PR any more
(MAGI #5721, CASSANDRA #5718): its approval check was forgeable (#202 actor spoofing, free-form marker, caller-chosen --pin).  It lives only in the
git history (commit 23ec8aa) and is a separate follow-up task that needs a signed MAGI authority and an upload-free window.

    SUPABASE_URL=... SUPABASE_KEY=... [SUPABASE_INTERNAL_RPC_SECRET=...] python -I tests/live_smoke_task198_readonly.py \
        --approved-commit <40 hex from the approval event> --not-before <ISO time> --not-after <ISO time> [--report out.json] [--check-only]

Guarantees (each is tested against the fake server in tests/test_task198_live_probe.py):
  * GET only: the HTTP client handed to the probe exposes nothing but ``get``; post/patch/put/delete/request raise before any network traffic.
  * the permission window is a DOUBLE gate (C198-75-SEC-01): ``--not-before`` (start) and ``--not-after`` (end) are both REQUIRED; before the start or at/after the
    end no request is sent.  Each request gets min(15 s, time left in the window) as its timeout AND a hard deadline at the window end (the request runs in a
    daemon thread, the run stops at the deadline and the process exits without waiting for it), so no GET is in flight after ``--not-after``.
  * the approved commit is pinned (C198-75-SEC-03): ``--approved-commit`` (40 hex, copied from the approval event, never derived from the checkout) must equal
    ``git rev-parse HEAD``, the tracked tree must be clean and this file must be byte-identical to ``git show HEAD:<this file>``; otherwise exit 2 BEFORE any
    environment value is read and before any request.  ``--check-only`` runs these checks and the window/report checks without keys or network.
  * nothing secret is ever printed or written (C198-75-SEC-02/04): every printed or written text is passed through a redactor that replaces the key and the RPC
    secret, Content-Range is stored only if it matches ``<a>-<b>/<n>`` or ``*/<n>`` (otherwise ``INVALID_FORMAT``), and the finished report is re-scanned for the
    secrets (a hit aborts the write and exits 4 without printing the value).
  * the first 5xx, timeout, transport error or any unexpected exception stops the run at once (exit 3); there is no retry.
  * requests are sequential, about ten in total, 15 s timeout each.
  * the report contains only: commit, times, the request list (table + parameter NAMES), status codes, Content-Range, row counts, pass/fail per check.
    No key, no secret, no row contents.  Without SUPABASE_INTERNAL_RPC_SECRET the secret-gated call_image_ingestions checks are reported NOT RUN (not passed).
Exit codes: 0 every check passed, 1 a check failed, 2 usage/window/pin/report-path error (nothing sent), 3 stopped early, 4 secret found in the report text (report not written).  The result is evidence for CASSANDRA, never a self-PASS.
"""
import argparse, hashlib, json, os, re, subprocess, sys, threading
from datetime import datetime, timedelta, timezone

import httpx


RANGE_RE = re.compile(r"^(\d{1,12}-\d{1,12}|\*)/(\d{1,12}|\*)$")
REDACTED = "[REDACTED]"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
MAX_WINDOW = timedelta(hours=6)
REQ_TIMEOUT = 15.0


def safe_range(v):
    """a server header is never reflected: only a well-formed Content-Range is kept"""
    return v if isinstance(v, str) and RANGE_RE.match(v) else ("INVALID_FORMAT" if v else None)


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
    def __init__(self, url, key, secret=None, not_after=None, transport=None, now=None, not_before=None):
        self.url = url.rstrip("/")
        self.not_before = not_before
        self._secrets = [x for x in (key, secret) if x]
        self.h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.secret = secret
        self.not_after = not_after
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.client = GetOnlyClient(httpx.Client(timeout=15, transport=transport))
        self.results, self.requests = [], []

    def redact(self, text):
        text = str(text)
        for sec in self._secrets:
            text = text.replace(sec, REDACTED)
        return text

    def rec(self, name, ok, info=""):
        info = self.redact(info)[:200]
        self.results.append({"name": name, "ok": bool(ok), "info": info})
        print(("PASS " if ok else "FAIL ") + name + (f"  [{info}]" if info else ""), flush=True)
        return ok

    def not_run(self, name, why):
        self.results.append({"name": name, "ok": False, "not_run": True, "info": why})
        print("NOT RUN " + name + f"  [{why}]", flush=True)

    def _remaining(self):
        """seconds left in the permission window; Stop if it has not opened or has closed (SEC-01)"""
        now = self._now()
        if self.not_before is not None and now < self.not_before:
            raise Stop("permission window has not started: no request is sent")
        if self.not_after is not None:
            left = (self.not_after - now).total_seconds()
            if left <= 0.5:
                raise Stop("permission window ended: no further request is sent")
            return left
        return None

    def _call(self, url, params, headers, left):
        """one GET with min(15 s, window left) as timeout and a HARD deadline at the window end: the request runs in a daemon thread; if it is still
        running at the deadline the run stops and the process exits without waiting for it"""
        budget = REQ_TIMEOUT if left is None else min(REQ_TIMEOUT, left)
        box = {}
        def work():
            try:
                box["r"] = self.client.get(url, params=params, headers=headers, timeout=budget)
            except BaseException as ex:                  # noqa: BLE001 - handed to the caller
                box["ex"] = ex
        t = threading.Thread(target=work, daemon=True)
        t.start(); t.join(budget + 0.25)
        if t.is_alive():
            raise Stop("request still running at its deadline (window end or 15 s)")
        if "ex" in box:
            raise box["ex"]
        return box["r"]

    def get(self, table, params, headers=None, count=True):
        left = self._remaining()
        h = dict(self.h, **(headers or {}))
        if count:
            h["Prefer"] = "count=exact"
        e = {"n": len(self.requests) + 1, "method": "GET", "table": table, "param_names": sorted(params), "secret_header": bool(headers)}
        self.requests.append(e)
        try:
            r = self._call(f"{self.url}/rest/v1/{table}", params, h, left)
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
        e.update(status=r.status_code, content_range=safe_range(r.headers.get("content-range")), rows=rows)
        if r.status_code >= 500:
            raise Stop(f"HTTP {r.status_code} on request {e['n']}")
        return r

    @staticmethod
    def rng(r):
        return safe_range(r.headers.get("content-range"))

    def run(self):
        r = self.get("scheduler_status", {"job_name": "like.patrol_alert::*", "order": "job_name.asc", "limit": "5"})
        self.rec("GET like.patrol_alert::* (wildcard '*', Content-Range, text order)", r.status_code in (200, 206)
                 and self.rng(r) not in (None, "INVALID_FORMAT"), f"HTTP {r.status_code} range={self.rng(r)}")
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
            self.rec(label, r.status_code in (200, 206) and isinstance(r.json(), list) and self.rng(r) not in (None, "INVALID_FORMAT"),
                     f"HTTP {r.status_code} range={self.rng(r)}")
        # db-max-rows: how many rows does one unlimited request really return?
        r = self.get("scheduler_status", {"select": "job_name", "order": "job_name.asc"})
        total = (self.rng(r) or "").rsplit("/", 1)[-1]
        self.rec("db-max-rows observation (rows returned vs total)", r.status_code in (200, 206), f"returned={len(r.json())} total={total}")


def git_commit():
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        run = lambda *a: subprocess.run(["git", "-C", here, *a], capture_output=True, text=True, timeout=20).stdout.strip()
        return {"commit": run("rev-parse", "HEAD"), "tracked_tree_clean": not run("status", "--porcelain", "--untracked-files=no")}
    except Exception:
        return {"commit": None, "tracked_tree_clean": None}


def verify_pin(approved):
    """SEC-03: the approved 40-hex commit comes from the approval event (an argument), never from the checkout.  Fail closed on anything unverifiable."""
    if not HEX40.match(approved or ""):
        return False, "--approved-commit must be the 40 lowercase hex characters of the approved commit"
    here = os.path.dirname(os.path.abspath(__file__))
    me = os.path.abspath(__file__)
    try:
        g = lambda *a: subprocess.run(["git", "-C", here, *a], capture_output=True, timeout=20)
        head = g("rev-parse", "HEAD").stdout.decode().strip()
        if head != approved:
            return False, f"HEAD {head[:12] or '?'} is not the approved commit {approved[:12]}"
        if g("status", "--porcelain", "--untracked-files=no").stdout.strip():
            return False, "tracked files are modified: the tree is not the approved commit"
        top = g("rev-parse", "--show-toplevel").stdout.decode().strip()
        rel = os.path.relpath(os.path.realpath(me), os.path.realpath(top)).replace(os.sep, "/")
        committed = g("show", f"HEAD:{rel}")
        if committed.returncode != 0:
            return False, "this script is not part of the approved commit"
        with open(me, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != hashlib.sha256(committed.stdout).hexdigest():
                return False, "this script differs from the committed file"
    except Exception as ex:
        return False, f"cannot verify the commit ({type(ex).__name__})"
    return True, "approved commit verified"


def parse_time(label, text):
    try:
        t = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        raise ValueError(f"{label} must be an ISO 8601 time WITH a UTC offset")
    if t.tzinfo is None:
        raise ValueError(f"{label} must be an ISO 8601 time WITH a UTC offset")
    return t


def main(argv=None, now=None):
    now = now or (lambda: datetime.now(timezone.utc))
    ap = argparse.ArgumentParser()
    ap.add_argument("--approved-commit", required=True, help="the 40-hex commit named in the approval event")
    ap.add_argument("--not-before", required=True, help="start of the permission window, ISO 8601 with offset (e.g. 2026-10-22T19:45:00+09:00)")
    ap.add_argument("--not-after", required=True, help="end of the permission window, ISO 8601 with offset (e.g. 2026-10-22T21:00:00+09:00)")
    ap.add_argument("--report", default="")
    ap.add_argument("--check-only", action="store_true", help="verify commit pin, window and report path; read no key, send nothing")
    a = ap.parse_args(argv)
    try:
        not_before, not_after = parse_time("--not-before", a.not_before), parse_time("--not-after", a.not_after)
    except ValueError as ex:
        print(str(ex)); return 2
    if not not_before < not_after:
        print("--not-before must be earlier than --not-after"); return 2
    if not_after - not_before > MAX_WINDOW:
        print(f"the window is longer than {int(MAX_WINDOW.total_seconds() // 3600)} h: check the dates"); return 2
    ok_pin, why = verify_pin(a.approved_commit)
    if not ok_pin:
        print("STOP: " + why + "; nothing was sent"); return 2
    t = now()
    state = "NOT YET OPEN" if t < not_before else ("EXPIRED" if t >= not_after else "OPEN")
    print(f"commit pin OK; window {state}")
    if state == "EXPIRED":
        print("the permission window has ended; nothing was sent"); return 2
    rf = None
    if a.report:                                       # claim the report file BEFORE any request: an existing/unwritable path must not cost a run
        try:
            rf = open(a.report, "x", encoding="utf-8")
        except OSError as ex:
            print(f"--report {a.report!r} cannot be created ({type(ex).__name__}); nothing was sent"); return 2
    if a.check_only:
        if rf:
            rf.close(); os.remove(a.report)
        print("CHECK OK" if state == "OPEN" else "CHECK OK (the window is not open yet: no run before the start time)"); return 0
    if state != "OPEN":
        if rf:
            rf.close(); os.remove(a.report)
        print("the permission window has not started; nothing was sent"); return 2
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        if rf:
            rf.close(); os.remove(a.report)
        print("SUPABASE_URL / SUPABASE_KEY not set"); return 2
    p = ReadSmoke(url, key, os.environ.get("SUPABASE_INTERNAL_RPC_SECRET"), not_after=not_after, not_before=not_before, now=now)
    start = datetime.now(timezone.utc).isoformat(timespec="seconds"); stopped = None
    try:
        p.run()
    except Stop as s:
        stopped = p.redact(s)
    except Exception as ex:                            # anything unexpected also ends the run
        stopped = f"unexpected {type(ex).__name__}"
    except KeyboardInterrupt:                          # Ctrl-C: still write what was observed
        stopped = "interrupted"
    ok = stopped is None and all(r["ok"] for r in p.results)
    rep = dict(git_commit(), approved_commit=a.approved_commit, commit_verified=True, start_utc=start, end_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               not_before=a.not_before, not_after=a.not_after, stopped_early=stopped, requests=p.requests, results=p.results, secret_set=bool(p.secret), ok=ok)
    text = json.dumps(rep, ensure_ascii=False, indent=1)
    leaked = any(sec in text for sec in p._secrets)
    if leaked:                                          # defence in depth: never write or print the value
        text = p.redact(text)
        if rf:
            rf.close(); os.remove(a.report)
        print("STOPPED: a secret value appeared in the report text; the report was NOT written (exit 4)"); return 4
    if rf is not None:
        with rf:
            rf.write(text)
    print("STOPPED EARLY: " + stopped if stopped else ("ALL PASS" if ok else "FAILED"))
    return 3 if stopped else (0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
