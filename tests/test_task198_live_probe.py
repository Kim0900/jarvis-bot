"""The READ-ONLY live smoke (tests/live_smoke_task198_readonly.py) is itself tested against the fake server: it must pass on correct behaviour,
send nothing but GET, stop at the first 5xx / timeout / expired window without retrying, and never leak a key.  The WRITE probe was removed from
this PR (MAGI #5721, CASSANDRA #5718); there is nothing left here that can write."""
import json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import httpx  # noqa: E402
_RealClient = httpx.Client
import live_smoke_task198_readonly as ls  # noqa: E402
from fake_postgrest import FakePostgREST  # noqa: E402


class ReadSmokeAgainstFake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakePostgREST().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        s = self.srv
        s.tables.clear(); s.max_rows = None; s.ts_style = "utc"; s.range_quirk = None; s.requests.clear(); s.write_log.clear(); s.fail_mode = None; s.fail_tables = None
        now = datetime.now(timezone.utc)
        s.tables["scheduler_status"] = [
            {"job_name": n, "last_run_at": s._norm_ts((now - timedelta(minutes=i)).isoformat()), "last_result": "OK"}
            for i, n in enumerate(["job_a", "job_b", "patrol_alert::x", "patrol_meta::y"])]
        s.tables["call_image_ingestions"] = [
            {"source_type": "google_drive", "source_id": f"s{i}", "status": st, "updated_at": s._norm_ts(now.isoformat())} for i, st in enumerate(["COMPLETED", "FAILED", "PROCESSING"])]

    def smoke(self, secret="secret", not_after=None, **kw):
        return ls.ReadSmoke(self.srv.url, "KEYVALUE", secret, not_after=not_after, **kw)

    def test_passes_on_correct_behaviour_and_sends_only_get(self):
        for style in ("utc", "kst"):        # the server's timezone setting changes the timestamp text
            self.srv.ts_style = style; self.srv.requests.clear()
            p = self.smoke(not_after=datetime.now(timezone.utc) + timedelta(hours=1)); p.run()
            self.assertEqual([r for r in p.results if not r["ok"]], [], style)
            self.assertEqual(self.srv.write_log, [])
            self.assertTrue(self.srv.requests and all(m == "GET" for m, _ in self.srv.requests), self.srv.requests)
            self.assertTrue(all(e["method"] == "GET" for e in p.requests))
            self.assertEqual(len(self.srv.tables["scheduler_status"]), 4)

    def test_the_client_exposes_nothing_but_get(self):
        p = self.smoke()
        for m in ("post", "patch", "put", "delete", "request", "send", "stream", "head", "options"):
            with self.assertRaises(ls.Stop, msg=m):
                getattr(p.client, m)
        self.assertEqual(self.srv.requests, [])            # no traffic from the attempts

    def test_without_the_secret_ingestion_checks_are_not_run_and_never_counted_as_passed(self):
        p = self.smoke(secret=None); p.run()
        nr = [r for r in p.results if r.get("not_run")]
        self.assertEqual(len(nr), 2)
        self.assertTrue(all(not r["ok"] for r in nr))
        self.assertNotIn("call_image_ingestions", [t for _, t in self.srv.requests])

    def test_first_5xx_stops_the_run_without_retry(self):
        self.srv.fail_mode = "503"; self.srv.fail_tables = {"scheduler_status"}
        p = self.smoke()
        with self.assertRaises(ls.Stop):
            p.run()
        self.assertEqual(len(p.requests), 1)
        self.assertEqual(len(self.srv.requests), 1)           # exactly one request reached the server: no retry, no next probe
        self.assertEqual(p.requests[0]["status"], 503)

    def test_timeout_and_transport_errors_stop_the_run(self):
        calls = []
        def handler(request):
            calls.append(1); raise httpx.ReadTimeout("slow", request=request)
        p = self.smoke(transport=httpx.MockTransport(handler))
        with self.assertRaises(ls.Stop):
            p.run()
        self.assertEqual(len(calls), 1)
        self.assertEqual(p.requests[0]["error"], "ReadTimeout")
        calls.clear()
        def refuse(request):
            calls.append(1); raise httpx.ConnectError("no route", request=request)
        p = self.smoke(transport=httpx.MockTransport(refuse))
        with self.assertRaises(ls.Stop):
            p.run()
        self.assertEqual(len(calls), 1)

    def test_expired_permission_window_sends_nothing(self):
        p = self.smoke(not_after=datetime.now(timezone.utc) - timedelta(seconds=1))
        with self.assertRaises(ls.Stop):
            p.run()
        self.assertEqual(self.srv.requests, [])
        # the window can also end in the middle of a run: the next request is not sent
        t = {"now": datetime(2026, 10, 20, 13, 59, 58, tzinfo=timezone.utc)}
        def tick():
            t["now"] += timedelta(seconds=1); return t["now"]
        self.srv.requests.clear()
        p = self.smoke(not_after=datetime(2026, 10, 20, 14, 0, 0, tzinfo=timezone.utc), now=tick)
        with self.assertRaises(ls.Stop):
            p.run()
        self.assertLess(len(self.srv.requests), 3)

    APPROVED = "a" * 40

    def _main(self, *args, env=None, raw=False, pin=True, now=None):
        """main() with the approval arguments filled in unless raw=True; the commit pin is mocked here (verify_pin has its own tests on a temp repo)"""
        base = {"SUPABASE_URL": self.srv.url, "SUPABASE_KEY": "KEYVALUE", "SUPABASE_INTERNAL_RPC_SECRET": "SECRETVALUE"}
        args = list(args)
        if not raw:
            if "--approved-commit" not in args:
                args += ["--approved-commit", self.APPROVED]
            if "--not-before" not in args:
                args += ["--not-before", (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()]
        with mock.patch.dict(os.environ, dict(base, **(env or {})), clear=False), \
                mock.patch.object(ls, "verify_pin", return_value=(pin, "approved commit verified" if pin else "pin mismatch")):
            return ls.main(args, now=now)

    def test_main_requires_a_window_and_writes_a_report_without_secrets(self):
        for missing in (["--not-before", "2026-10-20T13:00:00+09:00"], ["--not-after", "2026-10-20T14:00:00+09:00"], ["--approved-commit", self.APPROVED]):
            with self.assertRaises(SystemExit):
                self._main(*missing, raw=True)                # window start, window end and the approved commit are all mandatory
        self.assertEqual(self._main("--not-after", "2026-10-20T14:00:00"), 2)       # no UTC offset
        with tempfile.TemporaryDirectory() as d:
            rp = os.path.join(d, "r.json")
            rc = self._main("--not-after", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "--report", rp)
            self.assertEqual(rc, 0)
            text = open(rp, encoding="utf-8").read()
            rep = json.loads(text)
            self.assertTrue(rep["ok"]); self.assertIsNone(rep["stopped_early"])
            for secret in ("KEYVALUE", "SECRETVALUE", "job_a", "patrol_meta::y"):
                self.assertNotIn(secret, text)               # no key, no secret, no row contents
            self.assertTrue(all("param_names" in r and "status" in r for r in rep["requests"]))
            before = len(self.srv.requests)
            self.assertEqual(self._main("--not-after", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "--report", rp), 2)   # never overwrite,
            self.assertEqual(len(self.srv.requests), before)                                                                                 # and nothing is sent
            self.assertEqual(json.loads(open(rp, encoding="utf-8").read()), rep)
            self.assertEqual(self._main("--not-after", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "--report", os.path.join(d, "no_dir", "r.json")), 2)
            self.assertEqual(len(self.srv.requests), before)

    def test_ctrl_c_still_writes_the_report(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ls.ReadSmoke, "run", side_effect=KeyboardInterrupt):
            rp = os.path.join(d, "r.json")
            self.assertEqual(self._main("--not-after", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "--report", rp), 3)
            self.assertEqual(json.load(open(rp, encoding="utf-8"))["stopped_early"], "interrupted")

    def test_main_exit_codes(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.srv.fail_mode = "503"; self.srv.fail_tables = {"scheduler_status"}
        self.assertEqual(self._main("--not-after", future), 3)
        self.srv.fail_mode = None
        self.assertEqual(self._main("--not-after", (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), "--not-before", (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()), 2)   # expired: nothing sent
        self.assertEqual(self._main("--not-after", future, env={"SUPABASE_INTERNAL_RPC_SECRET": ""}), 1)    # not-run checks are not a pass

    def test_procedure_doc_matches_the_script_and_stays_read_only(self):
        import hashlib, re
        root = os.path.join(os.path.dirname(__file__), "..")
        doc = open(os.path.join(root, "docs", "TASK198_READONLY_SMOKE.md"), encoding="utf-8").read()
        digest = hashlib.sha256(open(ls.__file__, "rb").read()).hexdigest()
        self.assertIn(digest, doc.lower(), "the script changed: update the hash in docs/TASK198_READONLY_SMOKE.md (step 4)")
        blocks = re.findall(r"```\n(.*?)\n```", doc, re.S)
        self.assertGreaterEqual(len(blocks), 18)
        for b in blocks:
            self.assertEqual(len(b.splitlines()), 1, "every block is ONE copy-paste line: " + b)
        cmds = "\n".join(blocks)
        for bad in ("Invoke-WebRequest", "Invoke-RestMethod", "curl ", "-Method Post", "-Method Patch", "-Method Delete", "git push", "git merge", "git commit"):
            self.assertNotIn(bad, cmds)
        self.assertIn("live_smoke_task198_readonly.py", cmds); self.assertIn("--not-after", cmds); self.assertIn("--not-before", cmds); self.assertIn("--approved-commit", cmds)
        self.assertNotIn("--allow-write", cmds)

    # ---------------- CASSANDRA #5916 -------------------------------------------------------------------------------------------------------------
    def _future(self, **kw):
        return (datetime.now(timezone.utc) + timedelta(**kw)).isoformat()

    def test_sec01_window_start_and_end_are_both_enforced(self):
        n0 = len(self.srv.requests)
        self.assertEqual(self._main("--not-before", self._future(minutes=5), "--not-after", self._future(hours=1)), 2)            # not open yet
        self.assertEqual(self._main("--not-before", self._future(hours=2), "--not-after", self._future(hours=1)), 2)             # start after end
        self.assertEqual(self._main("--not-before", self._future(hours=-8), "--not-after", self._future(hours=3)), 2)            # longer than 6 h (wrong day?)
        self.assertEqual(len(self.srv.requests), n0, "no request in any rejected case")
        p = self.smoke(not_after=datetime.now(timezone.utc) + timedelta(hours=1), not_before=datetime.now(timezone.utc) + timedelta(minutes=5))
        with self.assertRaises(ls.Stop):
            p.run()
        self.assertEqual(len(self.srv.requests), n0)
        # exact boundaries with an injected clock: start inclusive, end exclusive with a 0.5 s guard
        nb, na = datetime(2026, 10, 22, 10, 45, tzinfo=timezone.utc), datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        for now, sends in ((nb - timedelta(milliseconds=1), False), (nb, True), (na - timedelta(seconds=2), True), (na - timedelta(milliseconds=400), False), (na, False), (na + timedelta(hours=1), False)):
            self.srv.requests.clear()
            p = self.smoke(not_before=nb, not_after=na, now=lambda now=now: now)
            try:
                p.get("scheduler_status", {"limit": "1"})
            except ls.Stop:
                pass
            self.assertEqual(bool(self.srv.requests), sends, str(now))

    def test_sec01_request_budget_is_the_time_left_and_a_slow_request_cannot_outlive_the_window(self):
        seen = []
        real = ls.GetOnlyClient.get
        def spy(self_, *a, **k):
            seen.append(k.get("timeout")); return real(self_, *a, **k)
        with mock.patch.object(ls.GetOnlyClient, "get", spy):
            self.smoke(not_after=datetime.now(timezone.utc) + timedelta(hours=1)).get("scheduler_status", {"limit": "1"})
            self.smoke(not_after=datetime.now(timezone.utc) + timedelta(seconds=4)).get("scheduler_status", {"limit": "1"})
        self.assertEqual(seen[0], 15.0)
        self.assertLessEqual(seen[1], 4.0)
        import time
        calls = []
        def slow(request):
            calls.append(time.monotonic()); time.sleep(6); return httpx.Response(200, json=[])
        p = self.smoke(not_after=datetime.now(timezone.utc) + timedelta(seconds=2), transport=httpx.MockTransport(slow))
        t0 = time.monotonic()
        with self.assertRaises(ls.Stop) as cm:
            p.run()
        self.assertLess(time.monotonic() - t0, 3.2, "the run must end at the window end, not after the 15 s request timeout")
        self.assertEqual(len(calls), 1)                         # and nothing was sent after it
        self.assertIn("deadline", str(cm.exception))

    def test_sec02_secret_values_are_redacted_and_a_dirty_report_is_never_written(self):
        p = self.smoke()
        p.rec("x", True, "value KEYVALUE and secret")
        self.assertNotIn("KEYVALUE", p.results[-1]["info"])
        self.assertIn("[REDACTED]", p.results[-1]["info"])
        import contextlib, io
        def dirty(self_):
            self_.requests.append({"n": 1, "method": "GET", "table": "t", "param_names": ["KEYVALUE"], "status": 200})
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ls.ReadSmoke, "run", dirty):
            rp = os.path.join(d, "r.json"); out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = self._main("--not-after", self._future(hours=1), "--report", rp)
            self.assertEqual(rc, 4)
            self.assertFalse(os.path.exists(rp), "a report containing a secret must not exist")
            self.assertNotIn("KEYVALUE", out.getvalue())

    def test_sec04_content_range_is_never_reflected(self):
        import contextlib, io
        for bad in ("KEYVALUE", "0-9/KEYVALUE", "bytes 0-9/10", "0-9/10; KEYVALUE", "1" * 40 + "/1"):
            def reflect(request, bad=bad):
                return httpx.Response(200, json=[], headers={"Content-Range": bad})
            p = self.smoke(transport=httpx.MockTransport(reflect)); p.run()
            blob = json.dumps({"r": p.requests, "x": p.results})
            self.assertNotIn("KEYVALUE", blob, bad)
            self.assertEqual(p.requests[0]["content_range"], "INVALID_FORMAT", bad)
            self.assertFalse(p.results[0]["ok"], "a malformed range must fail the check, not pass it")
        for good in ("0-4/5", "*/0", "0-0/1"):
            def fine(request, good=good):
                return httpx.Response(200, json=[], headers={"Content-Range": good})
            p = self.smoke(transport=httpx.MockTransport(fine)); p.run()
            self.assertEqual(p.requests[0]["content_range"], good)
        with tempfile.TemporaryDirectory() as d:                 # end to end through main(): stdout and report stay clean
            rp = os.path.join(d, "r.json"); out = io.StringIO()
            def reflect(request):
                return httpx.Response(200, json=[], headers={"Content-Range": "KEYVALUE"})
            with mock.patch.object(ls.httpx, "Client", lambda **kw: _RealClient(**dict(kw, transport=httpx.MockTransport(reflect)))), contextlib.redirect_stdout(out):
                self._main("--not-after", self._future(hours=1), "--report", rp)
            self.assertNotIn("KEYVALUE", out.getvalue() + open(rp, encoding="utf-8").read())

    def test_sec03_commit_pin_is_enforced_by_the_script_before_anything_is_read_or_sent(self):
        import shutil, subprocess, sys as _sys
        root = tempfile.mkdtemp(prefix="t198pin_")
        def git(*a, cwd=root):
            return subprocess.run(["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True, check=True).stdout.strip()
        git("init", "-q"); os.makedirs(os.path.join(root, "tests"))
        script = os.path.join(root, "tests", "live_smoke_task198_readonly.py")
        shutil.copy(ls.__file__, script)
        git("add", "-A"); git("commit", "-q", "-m", "x"); head = git("rev-parse", "HEAD")
        env = dict(os.environ, SUPABASE_URL=self.srv.url, SUPABASE_KEY="KEYVALUE")
        def run(approved, *extra):
            n = len(self.srv.requests)
            r = subprocess.run([_sys.executable, "-I", script, "--approved-commit", approved, "--not-before", self._future(minutes=-5), "--not-after", self._future(hours=1), *extra],
                               capture_output=True, text=True, env=env)
            return r.returncode, r.stdout + r.stderr, len(self.srv.requests) - n
        rc, out, n = run(head, "--check-only"); self.assertEqual((rc, n), (0, 0), out); self.assertIn("CHECK OK", out)
        rc, out, n = run("b" * 40, "--check-only"); self.assertEqual((rc, n), (2, 0), out)                       # another commit than the approved one
        rc, out, n = run(head.upper(), "--check-only"); self.assertEqual((rc, n), (2, 0), out)                    # not the exact 40 lowercase hex
        rc, out, n = run(head[:12], "--check-only"); self.assertEqual((rc, n), (2, 0), out)                       # abbreviations are refused
        with open(script, "a") as f:
            f.write("\n# local edit\n")
        rc, out, n = run(head); self.assertEqual((rc, n), (2, 0), out); self.assertIn("modified", out)           # dirty tracked file: nothing sent
        git("update-index", "--assume-unchanged", "tests/live_smoke_task198_readonly.py")                        # hides the edit from `git status`
        rc, out, n = run(head); self.assertEqual((rc, n), (2, 0), out); self.assertIn("differs from the committed file", out)
        rc, out, n = run(head, "--check-only"); self.assertEqual(rc, 2, out)
        shutil.rmtree(os.path.join(root, ".git"))                                                                 # not a git checkout at all: fail closed
        rc, out, n = run(head, "--check-only"); self.assertEqual((rc, n), (2, 0), out)
        shutil.rmtree(root, ignore_errors=True)

    def test_sec03_doc_gates_every_step_and_never_prints_a_secret(self):
        import re
        doc = open(os.path.join(os.path.dirname(__file__), "..", "docs", "TASK198_READONLY_SMOKE.md"), encoding="utf-8").read()
        blocks = re.findall(r"```\n(.*?)\n```", doc, re.S)
        sel = [b for b in blocks if "Select-String" in b]
        self.assertEqual(len(sel), 1)
        self.assertIn("-Quiet", sel[0])                                   # boolean only: matched lines (= the secret) are never echoed
        self.assertIn("Remove-Item", sel[0].split("else")[0])             # a dirty report is deleted instead of shown
        for b in blocks:
            if "Get-Content" in b:
                self.assertIn("-Quiet", b); self.assertIn("else { Get-Content", b)     # the report is displayed only in the clean branch
        self.assertIn("finally", sel[0]); self.assertIn("Env:SUPABASE_KEY", sel[0].split("finally")[1])   # env cleanup even on an exception / Ctrl-C
        for key in ("-cmatch", "-ceq"):                                    # the commit pin is a hard stop (exit closes the window), exact 40 lowercase hex
            self.assertTrue(any(key in b and "exit 1" in b for b in blocks), key)
        self.assertTrue(any("-Algorithm SHA256" in b and "exit 1" in b for b in blocks))
        pin = [i for i, b in enumerate(blocks) if "--approved-commit" in b]
        self.assertTrue(all("-I " in blocks[i] for i in pin))
        checkline = next(i for i, b in enumerate(blocks) if "--check-only" in b)
        keyline = next(i for i, b in enumerate(blocks) if "SUPABASE_KEY" in b and "Read-Host" in b)
        runline = next(i for i, b in enumerate(blocks) if "--check-only" not in b and "live_smoke_task198_readonly.py" in b and "--approved-commit" in b)
        self.assertLess(checkline, keyline); self.assertLess(keyline, runline)    # pin/window check first, secrets typed after, run last
        for flag in ("--not-before", "--not-after", "--approved-commit"):
            self.assertIn(flag, blocks[runline])

    def test_this_repository_contains_no_write_probe_any_more(self):
        with open(ls.__file__, encoding="utf-8") as f:
            src = f.read()
        for needle in ("--allow-write", "def run_write", "verify_approval", ".post(", ".patch(", ".delete(", "--approval-event"):
            self.assertNotIn(needle, src, needle)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(__file__), "live_probe_task198.py")))


if __name__ == "__main__":
    unittest.main()
