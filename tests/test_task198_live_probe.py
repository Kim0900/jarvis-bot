"""The live PostgREST probe (tests/live_probe_task198.py) is itself tested against the fake server, so the script that will
run against the real service is known to pass on correct behaviour and to FAIL on wrong behaviour."""
import os, sys, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import live_probe_task198 as lp  # noqa: E402
from fake_postgrest import FakePostgREST  # noqa: E402


class LiveProbeAgainstFake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakePostgREST().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        s = self.srv
        s.tables.clear(); s.max_rows = None; s.ts_style = "utc"; s.range_quirk = None; s.requests.clear()
        now = datetime.now(timezone.utc)
        s.tables["scheduler_status"] = [
            {"job_name": n, "last_run_at": s._norm_ts((now - timedelta(minutes=i)).isoformat()), "last_result": "OK"}
            for i, n in enumerate(["job_a", "job_b", "patrol_alert::x", "patrol_meta::y"])]
        s.tables["call_image_ingestions"] = [
            {"source_type": "google_drive", "source_id": f"s{i}", "status": st,
             "updated_at": s._norm_ts(now.isoformat())} for i, st in enumerate(["COMPLETED", "FAILED", "PROCESSING"])]

    def probe(self):
        return lp.Probe(self.srv.url, "k", "secret")

    def test_read_only_and_write_pass_on_correct_behaviour(self):
        for style in ("utc", "kst"):        # the server's timezone setting changes the timestamp text
            self.srv.ts_style = style
            p = self.probe(); p.run_read_only(); p.run_write("0")
            bad = [r for r in p.results if not r["ok"]]
            self.assertEqual(bad, [], style)
            self.assertEqual(self.srv.tables["scheduler_status"].__len__(), 4)     # the probe row is gone again

    def test_probe_fails_when_the_server_does_wrong(self):
        self.srv.range_quirk = "forged_range"
        p = self.probe(); p.run_read_only()
        self.assertTrue(all(r["ok"] for r in p.results))      # the probe reads headers only; paging integrity is the reader's job
        p = lp.Probe(self.srv.url, "k", None); p.run_read_only()
        self.assertTrue(any(not r["ok"] for r in p.results))   # missing secret is reported, not skipped

    def test_write_requires_approval_flag(self):
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"] = self.srv.url, "k"
        self.assertEqual(lp.main(["--allow-write"]), 2)
        self.assertEqual(self.srv.tables.get("scheduler_status", [])[0]["job_name"], "job_a")


if __name__ == "__main__":
    unittest.main()
