"""The READ-ONLY live smoke (tests/live_smoke_task198_readonly.py) is itself tested against the fake server: it must pass on correct behaviour,
send nothing but GET, stop at the first 5xx / timeout / expired window without retrying, and never leak a key.  The WRITE probe was removed from
this PR (MAGI #5721, CASSANDRA #5718); there is nothing left here that can write."""
import json, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import httpx  # noqa: E402
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

    def _main(self, *args, env=None):
        base = {"SUPABASE_URL": self.srv.url, "SUPABASE_KEY": "KEYVALUE", "SUPABASE_INTERNAL_RPC_SECRET": "SECRETVALUE"}
        with mock.patch.dict(os.environ, dict(base, **(env or {})), clear=False):
            return ls.main(list(args))

    def test_main_requires_a_window_and_writes_a_report_without_secrets(self):
        with self.assertRaises(SystemExit):
            self._main()                                      # --not-after is mandatory
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
        self.assertEqual(self._main("--not-after", (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()), 3)
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
        self.assertIn("live_smoke_task198_readonly.py --not-after", cmds)
        self.assertNotIn("--allow-write", cmds)

    def test_this_repository_contains_no_write_probe_any_more(self):
        with open(ls.__file__, encoding="utf-8") as f:
            src = f.read()
        for needle in ("--allow-write", "def run_write", "verify_approval", ".post(", ".patch(", ".delete(", "--approval-event"):
            self.assertNotIn(needle, src, needle)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(__file__), "live_probe_task198.py")))


if __name__ == "__main__":
    unittest.main()
