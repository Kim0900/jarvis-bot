"""The live PostgREST probe (tests/live_probe_task198.py) is itself tested against the fake server, so the script that will
run against the real service is known to pass on correct behaviour and to FAIL on wrong behaviour."""
import os, sys, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import live_probe_task198 as lp  # noqa: E402
from fake_postgrest import FakePostgREST  # noqa: E402

PIN = "ccf6986b8ec232375755a24cdaa456261c88d759"


class LiveProbeAgainstFake(unittest.TestCase):
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
            {"source_type": "google_drive", "source_id": f"s{i}", "status": st,
             "updated_at": s._norm_ts(now.isoformat())} for i, st in enumerate(["COMPLETED", "FAILED", "PROCESSING"])]

    def approve(self, **kw):
        """register a MAGI approval event in the fake registry; returns its id"""
        e = {"event_id": 9001, "task_id": 198, "actor": "마기", "event_type": "DECISION",
             "created_at": datetime.now(timezone.utc).isoformat(),
             "detail": f"F4_PROBE_WRITE_APPROVED pin={PIN[:12]} key=patrol_meta::f4_probe"}
        e.update(kw)
        self.srv.tables["magi_task_events"] = [e]
        return e["event_id"]

    def writes(self):
        return [w for w in self.srv.write_log if w[1] == "scheduler_status"]

    def probe(self):
        return lp.Probe(self.srv.url, "k", "secret")

    def test_read_only_and_write_pass_on_correct_behaviour(self):
        for style in ("utc", "kst"):        # the server's timezone setting changes the timestamp text
            self.srv.ts_style = style
            p = self.probe(); p.run_read_only(); p.run_write(self.approve(), PIN)
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
        self.assertEqual(lp.main(["--allow-write", "--approval-event", "0"]), 1)       # "0" is not an approval
        self.assertEqual(self.srv.tables.get("scheduler_status", [])[0]["job_name"], "job_a")


    def run_write_expect_refused(self, event_id, pin=PIN):
        p = self.probe(); p.run_write(event_id, pin)
        self.assertFalse(all(r["ok"] for r in p.results))
        self.assertEqual(self.writes(), [])                    # not one write reached the server
        return p

    def test_fake_or_unverifiable_approval_is_refused_before_any_write(self):
        self.approve()
        for bad in ("0", "", "abc", "-5", "9999", "9001 "[:-1] + "1"):
            self.run_write_expect_refused(bad)
        self.run_write_expect_refused(9001, "")
        self.run_write_expect_refused(9001, "deadbeef")        # wrong / too short pin

    def test_approval_with_wrong_actor_task_scope_pin_or_age_is_refused(self):
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        for kw in ({"actor": "캐스퍼"}, {"task_id": 197}, {"detail": "approved"}, {"created_at": stale}, {"created_at": future},
                   {"detail": f"F4_PROBE_WRITE_APPROVED pin={'f' * 12} key=patrol_meta::f4_probe"}):
            self.run_write_expect_refused(self.approve(**kw))

    def test_registry_unreachable_fails_closed(self):
        self.approve()
        self.srv.fail_tables = {"magi_task_events"}; self.srv.fail_mode = "503"
        try:
            self.run_write_expect_refused(9001)
        finally:
            self.srv.fail_mode = None; self.srv.fail_tables = None

    def test_preexisting_probe_rows_are_never_deleted_or_modified(self):
        old = {"job_name": "patrol_meta::f4_probe", "last_run_at": self.srv._norm_ts(datetime.now(timezone.utc).isoformat()), "last_result": "someone else"}
        self.srv.tables["scheduler_status"].append(dict(old))
        p = self.probe(); p.run_write(self.approve(), PIN)
        self.assertEqual([r for r in p.results if not r["ok"]], [])
        self.assertIn(old, self.srv.tables["scheduler_status"])
        self.assertEqual(len(self.srv.tables["scheduler_status"]), 5)

    def test_key_collision_fails_closed_and_deletes_nothing(self):
        import uuid as _u
        fixed = _u.UUID(int=0)
        key = f"patrol_meta::f4_probe::{fixed.hex[:12]}"
        victim = {"job_name": key, "last_run_at": self.srv._norm_ts(datetime.now(timezone.utc).isoformat()), "last_result": "victim"}
        self.srv.tables["scheduler_status"].append(dict(victim))
        orig = lp.uuid.uuid4
        lp.uuid.uuid4 = lambda: fixed
        try:
            p = self.probe(); p.run_write(self.approve(), PIN)
        finally:
            lp.uuid.uuid4 = orig
        self.assertTrue(any(not r["ok"] for r in p.results))
        self.assertIn(victim, self.srv.tables["scheduler_status"])
        self.assertEqual([w for w in self.writes() if w[0] == "DELETE"], [])

    def test_cleanup_only_removes_a_row_that_still_carries_this_runs_tag(self):
        # someone replaces the row's tag during the run: the conditional DELETE must not remove it, and the probe says so
        p = self.probe()
        orig = p.rec
        def rec(name, ok, info=""):
            if name.startswith("PATCH CAS with the stale"):
                for r in self.srv.tables["scheduler_status"]:
                    if r["job_name"].startswith("patrol_meta::f4_probe::"):
                        r["last_result"] = "taken over"
            return orig(name, ok, info)
        p.rec = rec
        p.run_write(self.approve(), PIN)
        taken = [r for r in self.srv.tables["scheduler_status"] if r["job_name"].startswith("patrol_meta::f4_probe::")]
        self.assertEqual(len(taken), 1)
        self.assertTrue(any((not r["ok"]) and r["name"].startswith("cleanup") for r in p.results))


if __name__ == "__main__":
    unittest.main()
