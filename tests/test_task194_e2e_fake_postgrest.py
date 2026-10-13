"""Task #194 end-to-end failure injection.

Real code under test: ``bot_v5_legacy.sb_h`` / ``sb_upsert`` / ``sb_select_required`` and the
four real job functions, driven by the real ``scheduler_daily_jobs.run_daily_jobs``.
Only the database is replaced, by an in-process PostgREST stand-in on 127.0.0.1 that can
return 503 or drop connections.  No production credentials or hosts are touched.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

import bot_v5_legacy as legacy  # noqa: E402
import scheduler_daily_jobs as sdj  # noqa: E402
from fake_postgrest import FakePostgREST  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
JOBS = sdj.DAILY_JOBS


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class E2E(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakePostgREST().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        self.srv.tables.clear()
        self.srv.fail_mode = None
        self.srv.fail_tables = None
        self.srv.fail_methods = None
        self.srv.get_quirk = None
        self.srv.fail_body_contains = {}
        self.send_result = {"sent": 1, "failed": 0}
        self.srv.requests.clear()
        self.patches = [
            patch.object(legacy, "SUPABASE_URL", self.srv.url),
            patch.object(legacy, "send_telegram_broadcast", self._fake_send),
            # jobs derive "today" from the real clock; freeze it to the test's `now`
            patch.object(legacy, "today_kst", lambda: datetime(2026, 10, 12, tzinfo=KST).date()),
        ]
        for p in self.patches:
            p.start()
        self.sent = []
        self.now = datetime(2026, 10, 12, 4, 0, 5, tzinfo=KST)
        self.yesterday = str(self.now.date() - timedelta(days=1))
        self.srv.tables["raw_calls"] = [{
            "날짜": self.yesterday, "콜유형": "일반", "요금": 12000,
            "배차시각": f"{self.yesterday}T20:10:00+09:00", "verify_status": "verified"}]
        self.state = sdj.DailyJobState()
        self.clock = Clock()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    async def _fake_send(self, msg):
        self.sent.append(msg)
        if isinstance(self.send_result, BaseException):
            raise self.send_result
        return self.send_result

    def alerts(self):
        return [m for m in self.sent if "부분누락" in m]

    def arm_alert(self):
        """operated_status says 'GPX only' -> check_daily_ingestion_detail must alert."""
        self.srv.tables["operated_status"] = [
            {"날짜": self.yesterday, "source": "gpx_proxy", "operated": True}]
        p = patch.object(legacy, "sync_operated_status", self._noop)
        p.start()
        self.patches.append(p)

    @staticmethod
    async def _noop(*a, **k):
        return None

    async def tick(self, state=None, now=None):
        return await sdj.run_daily_jobs(legacy, now or self.now, state or self.state, self.clock)

    def status(self, job):
        rows = [r for r in self.srv.tables.get("scheduler_status", []) if r["job_name"] == job]
        return rows[0]["last_result"] if rows else None

    def posts(self, table):
        return sum(1 for m, t in self.srv.requests if (m, t) == ("POST", table))

    # 1. the 10/11 incident: whole database unreachable at 04:00
    async def test_total_outage_runs_nothing_then_recovers(self):
        for mode in ("503", "reset"):
            with self.subTest(mode=mode):
                self.srv.tables.pop("scheduler_status", None)
                self.srv.requests.clear()
                self.state = sdj.DailyJobState()
                self.srv.fail_mode = mode
                out = await self.tick()
                self.assertEqual(set(out.values()), {"state_unknown"}, out)
                self.assertEqual(self.posts("daily_summary") + self.posts("kpi_7day_snapshot")
                                 + self.posts("daily_calc_snapshot"), 0)
                self.assertNotIn("scheduler_status", [t for _, t in self.srv.requests if _ == "POST"])
                self.srv.fail_mode = None
                self.clock.advance(sdj.retry_interval_seconds() + 1)
                out = await self.tick(now=self.now + timedelta(minutes=20))
                self.assertEqual(set(out.values()), {"ok"}, out)
                for job in JOBS:
                    self.assertNotIn(self.status(job), (None, "", "FAIL: "))
                self.assertTrue(self.srv.tables["daily_calc_snapshot"])

    # 2. the dangerous half-outage: status is readable but data reads fail -> must not store wrong aggregates
    async def test_data_read_outage_fails_jobs_without_writing_aggregates(self):
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"raw_calls"}
        self.srv.fail_methods = {"GET"}
        out = await self.tick()
        self.assertEqual(out["recalc_daily_summary_totals"], "failed")
        self.assertEqual(out["recalc_7day_average"], "failed")
        self.assertEqual(out["calc_daily_snapshot"], "failed")
        for job in ("recalc_daily_summary_totals", "recalc_7day_average", "calc_daily_snapshot"):
            self.assertTrue(self.status(job).startswith("FAIL: SupabaseReadError"), self.status(job))
            self.assertIn("raw_calls", self.status(job))
        self.assertEqual(self.posts("daily_summary") + self.posts("kpi_7day_snapshot")
                         + self.posts("daily_calc_snapshot"), 0)
        # recovery: FAIL record must not block the retry
        self.srv.fail_mode = None
        self.clock.advance(sdj.retry_interval_seconds() + 1)
        out = await self.tick(now=self.now + timedelta(minutes=20))
        self.assertEqual({out[j] for j in JOBS[:3]}, {"ok"}, out)
        self.assertEqual(self.status("calc_daily_snapshot"), "OK")
        self.assertEqual(len(self.srv.tables["kpi_7day_snapshot"]), 1)

    async def test_gpx_read_outage_does_not_store_snapshot_with_missing_idle_time(self):
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"gpx_sessions"}
        out = await self.tick()
        self.assertEqual(out["calc_daily_snapshot"], "failed")
        self.assertEqual(self.posts("daily_calc_snapshot"), 0)

    async def test_ingestion_check_read_failure_is_a_failure_not_a_silent_ok(self):
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"operated_status"}
        self.srv.fail_methods = {"GET"}
        out = await self.tick()
        self.assertEqual(out["check_daily_ingestion_detail"], "failed")
        self.assertTrue(self.status("check_daily_ingestion_detail").startswith("FAIL:"))
        self.assertFalse([m for m in self.sent if "부분누락" in m or "데이터 없음" in m], self.sent)

    # 3. restart and duplicate prevention against the real DB path
    async def test_restart_after_success_does_not_run_twice(self):
        await self.tick()
        daily_posts = self.posts("daily_summary")
        self.assertEqual(daily_posts, 1)
        out = await self.tick(state=sdj.DailyJobState(), now=self.now + timedelta(hours=2))
        self.assertEqual(set(out.values()), {"done"})
        self.assertEqual(self.posts("daily_summary"), daily_posts)

    async def test_unreadable_status_after_restart_does_not_duplicate(self):
        await self.tick()
        before = self.posts("daily_calc_snapshot")
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"scheduler_status"}
        out = await self.tick(state=sdj.DailyJobState(), now=self.now + timedelta(hours=1))
        self.assertEqual(set(out.values()), {"state_unknown"})
        self.assertEqual(self.posts("daily_calc_snapshot"), before)

    # CASSANDRA 5067 F1: HTTP 200 with an empty / garbled body is not "no row yet"
    async def test_untrusted_200_answers_are_state_unknown_and_run_nothing(self):
        for quirk in ("empty", "badjson", "dict"):
            with self.subTest(quirk=quirk):
                self.srv.tables.pop("scheduler_status", None)
                self.srv.requests.clear()
                self.state = sdj.DailyJobState()
                self.srv.get_quirk = quirk
                self.srv.fail_tables = {"scheduler_status"}
                out = await self.tick()
                self.assertEqual(set(out.values()), {"state_unknown"}, out)
                self.assertEqual(self.posts("daily_summary") + self.posts("kpi_7day_snapshot")
                                 + self.posts("daily_calc_snapshot") + self.posts("scheduler_status"), 0)
        self.srv.get_quirk = None
        self.srv.fail_tables = None

    async def test_real_empty_list_is_a_first_run_not_an_error(self):
        out = await self.tick()
        self.assertEqual({out[j] for j in JOBS[:3]}, {"ok"}, out)

    async def test_sb_select_required_rejects_untrusted_answers_but_accepts_real_empty(self):
        self.assertEqual(await legacy.sb_select_required("nothing_here", {"a": "eq.1"}), [])
        for quirk in ("empty", "badjson", "dict"):
            self.srv.get_quirk = quirk
            with self.assertRaises(legacy.SupabaseReadError, msg=quirk):
                await legacy.sb_select_required("raw_calls", {})
            # the lenient reader keeps its old contract for other callers
            self.assertEqual(await legacy.sb_select("raw_calls", {}), [])
        self.srv.get_quirk = None
        self.srv.fail_mode = "reset"
        with self.assertRaises(legacy.SupabaseReadError):
            await legacy.sb_select_required("raw_calls", {})

    async def test_job_input_read_with_empty_200_fails_instead_of_aggregating_zero(self):
        self.srv.get_quirk = "empty"
        self.srv.fail_tables = {"raw_calls"}
        out = await self.tick()
        self.assertEqual(out["recalc_daily_summary_totals"], "failed")
        self.assertEqual(out["recalc_7day_average"], "failed")
        self.assertEqual(self.posts("kpi_7day_snapshot"), 0)

    # CASSANDRA 5067 F3: delivery failure propagates
    async def test_alert_sent_once_and_recorded(self):
        self.arm_alert()
        out = await self.tick()
        self.assertEqual(out["check_daily_ingestion_detail"], "ok")
        self.assertEqual(len(self.alerts()), 1)

    async def test_alert_delivery_failure_is_a_failure_and_is_retried(self):
        self.arm_alert()
        for err in ({"sent": 0, "failed": 2}, RuntimeError("telegram down"), None):
            with self.subTest(err=repr(err)):
                self.srv.tables.pop("scheduler_status", None)
                self.state = sdj.DailyJobState()
                self.sent.clear()
                self.send_result = err
                out = await self.tick()
                self.assertEqual(out["check_daily_ingestion_detail"], "failed", out)
                self.assertTrue(self.status("check_daily_ingestion_detail").startswith("FAIL:"))
        self.send_result = {"sent": 1, "failed": 0}
        self.clock.advance(sdj.retry_interval_seconds() + 1)
        out = await self.tick(now=self.now + timedelta(minutes=20))
        self.assertEqual(out["check_daily_ingestion_detail"], "ok")
        self.assertEqual(self.status("check_daily_ingestion_detail")[:1], "{")

    async def test_partial_delivery_counts_as_delivered(self):
        self.arm_alert()
        self.send_result = {"sent": 1, "failed": 1}
        out = await self.tick()
        self.assertEqual(out["check_daily_ingestion_detail"], "ok")

    # 마기 5079 ②: GPX NULL policy -> snapshot stored, status PARTIAL (not OK, not FAIL)
    def gpx(self, **cols):
        self.srv.tables["gpx_sessions"] = [{"날짜": self.yesterday, **cols}]

    def snapshot_rows(self):
        return [r for r in self.srv.tables.get("daily_calc_snapshot", []) if r["calc_date"] == self.yesterday]

    async def test_no_gpx_row_is_plain_ok(self):
        out = await self.tick()
        self.assertEqual(out["calc_daily_snapshot"], "ok")
        self.assertEqual(self.status("calc_daily_snapshot"), "OK")
        self.assertEqual(len(self.snapshot_rows()), 1)

    async def test_complete_gpx_is_plain_ok_and_idle_is_computed(self):
        self.gpx(구속시간_h=7.64, 총주행거리_km=147.21)
        out = await self.tick()
        self.assertEqual(out["calc_daily_snapshot"], "ok")
        self.assertEqual(self.status("calc_daily_snapshot"), "OK")
        self.assertEqual(self.snapshot_rows()[0]["total_idle_min"], round(7.64 * 60, 2))

    async def test_gpx_with_null_fields_stores_snapshot_and_records_partial(self):
        cases = {
            "hours_null": ({"구속시간_h": None, "총주행거리_km": 196.73}, "null 구속시간_h"),   # real sample 2026-07-31
            "km_null": ({"구속시간_h": 5.0, "총주행거리_km": None}, "null 총주행거리_km"),
            "both_null": ({"구속시간_h": None, "총주행거리_km": None}, "null 구속시간_h,총주행거리_km"),
            "zero_km": ({"구속시간_h": 5.0, "총주행거리_km": 0}, "non-positive"),
            "negative_hours": ({"구속시간_h": -5, "총주행거리_km": 100.0}, "non-positive"),
            "negative_km": ({"구속시간_h": 5, "총주행거리_km": -100.0}, "non-positive"),
        }
        for label, (cols, reason) in cases.items():
            with self.subTest(case=label):
                for t in ("scheduler_status", "daily_calc_snapshot"):
                    self.srv.tables.pop(t, None)
                self.state = sdj.DailyJobState()
                self.gpx(**cols)
                out = await self.tick()
                self.assertEqual(out["calc_daily_snapshot"], "ok_partial", out)
                st = self.status("calc_daily_snapshot")
                self.assertTrue(st.startswith("PARTIAL: gpx_idle_unavailable"), st)
                self.assertIn(reason, st)
                rows = self.snapshot_rows()
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["total_idle_min"])
                self.assertIsNone(rows[0]["avg_idle_min"])
                # patrol alerts on startswith FAIL or MISMATCH anywhere; PARTIAL must trigger neither
                self.assertFalse(st.upper().startswith("FAIL") or "MISMATCH" in st.upper())
                # PARTIAL counts as done: no re-run in-process and none after a restart
                before = self.posts("daily_calc_snapshot")
                out2 = await self.tick(state=sdj.DailyJobState(), now=self.now + timedelta(hours=1))
                self.assertEqual(out2["calc_daily_snapshot"], "done")
                self.assertEqual(self.posts("daily_calc_snapshot"), before)

    async def test_two_gpx_rows_for_one_date_is_partial_not_a_guess(self):
        self.srv.tables["gpx_sessions"] = [
            {"날짜": self.yesterday, "구속시간_h": 5.0, "총주행거리_km": 100.0},
            {"날짜": self.yesterday, "구속시간_h": 3.0, "총주행거리_km": 50.0}]
        out = await self.tick()
        self.assertEqual(out["calc_daily_snapshot"], "ok_partial", out)
        self.assertIn("2 gpx rows", self.status("calc_daily_snapshot"))
        self.assertIsNone(self.snapshot_rows()[0]["total_idle_min"])

    async def test_real_duplicate_date_fixtures_are_partial(self):
        """Live gpx_sessions (2026-10-13): 39 rows / 34 dates; 3 dates have several rows
        (5 surplus rows).  Values copied from production (read-only query)."""
        real = {
            "2026-05-30 x4 identical": [(8.14, 191.65)] * 4,
            "2026-07-14 valid + NULL": [(7.16, 150.07), (None, None)],
            "2026-07-25 valid + NULL hours, different km": [(7.4, 158.52), (None, 124.78)],
        }
        for label, pairs in real.items():
            with self.subTest(case=label):
                for t in ("scheduler_status", "daily_calc_snapshot"):
                    self.srv.tables.pop(t, None)
                self.state = sdj.DailyJobState()
                self.srv.tables["gpx_sessions"] = [
                    {"날짜": self.yesterday, "구속시간_h": h, "총주행거리_km": k} for h, k in pairs]
                out = await self.tick()
                self.assertEqual(out["calc_daily_snapshot"], "ok_partial", out)
                self.assertIn(f"{len(pairs)} gpx rows", self.status("calc_daily_snapshot"))
                rows = self.snapshot_rows()
                self.assertEqual(len(rows), 1)
                self.assertIsNone(rows[0]["total_idle_min"])

    async def test_malformed_gpx_value_is_partial_not_ok_and_not_blocking(self):
        self.gpx(구속시간_h="abc", 총주행거리_km="12.5")
        out = await self.tick()
        self.assertEqual(out["calc_daily_snapshot"], "ok_partial", out)
        self.assertTrue(self.status("calc_daily_snapshot").startswith("PARTIAL: gpx_idle_compute_error"))
        self.assertEqual(len(self.snapshot_rows()), 1)

    async def test_catchup_proceeds_when_regular_snapshot_is_partial(self):
        missing = str(self.now.date() - timedelta(days=2))
        self.srv.tables["daily_calc_snapshot"] = [
            {"calc_date": str(self.now.date() - timedelta(days=d)), "axis": "A"} for d in (3, 4)]
        self.srv.tables["raw_calls"].append({
            "날짜": missing, "콜유형": "일반", "요금": 9000,
            "배차시각": f"{missing}T21:00:00+09:00", "verify_status": "verified"})
        self.gpx(구속시간_h=None, 총주행거리_km=100.0)
        with patch.dict(os.environ, {"TASK194_CATCHUP_DAYS": "5"}):
            out = await self.tick()
        self.assertEqual(out["calc_daily_snapshot"], "ok_partial")
        self.assertIn(missing, {r["calc_date"] for r in self.srv.tables["daily_calc_snapshot"]})

    async def test_gpx_read_outage_still_fails_the_job(self):
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"gpx_sessions"}
        out = await self.tick()
        self.assertEqual(out["calc_daily_snapshot"], "failed")

    # CASSANDRA 5072 F4: per-date daily_summary write failures must fail the job
    def add_second_day(self):
        self.other_day = str(self.now.date() - timedelta(days=2))
        self.srv.tables["raw_calls"].append({
            "날짜": self.other_day, "콜유형": "일반", "요금": 8000,
            "배차시각": f"{self.other_day}T20:10:00+09:00", "verify_status": "verified"})

    def summary_dates(self):
        return sorted(r["날짜"] for r in self.srv.tables.get("daily_summary", []))

    async def test_one_of_two_daily_summary_writes_failing_is_FAIL_then_recovers(self):
        self.add_second_day()
        self.srv.fail_body_contains = {"daily_summary": self.other_day}
        out = await self.tick()
        self.assertEqual(out["recalc_daily_summary_totals"], "failed", out)
        status = self.status("recalc_daily_summary_totals")
        self.assertTrue(status.startswith("FAIL:") and self.other_day in status, status)
        self.assertEqual(self.summary_dates(), [self.yesterday])  # the healthy date was still saved
        self.srv.fail_body_contains = {}
        self.clock.advance(sdj.retry_interval_seconds() + 1)
        out = await self.tick(now=self.now + timedelta(minutes=20))
        self.assertEqual(out["recalc_daily_summary_totals"], "ok", out)
        self.assertEqual(self.summary_dates(), sorted([self.yesterday, self.other_day]))
        self.assertEqual(self.status("recalc_daily_summary_totals"), "OK")

    async def test_all_daily_summary_writes_failing_is_FAIL_not_OK(self):
        self.add_second_day()
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"daily_summary"}
        self.srv.fail_methods = {"POST"}
        out = await self.tick()
        self.assertEqual(out["recalc_daily_summary_totals"], "failed", out)
        self.assertTrue(self.status("recalc_daily_summary_totals").startswith("FAIL:"))
        self.srv.fail_mode = self.srv.fail_tables = self.srv.fail_methods = None
        self.clock.advance(sdj.retry_interval_seconds() + 1)
        out = await self.tick(now=self.now + timedelta(minutes=20))
        self.assertEqual(out["recalc_daily_summary_totals"], "ok", out)
        self.assertEqual(len(self.summary_dates()), 2)

    # CASSANDRA 5072 F5: null / blank result is ambiguous, FAIL still retries
    async def test_null_blank_result_rows_run_nothing(self):
        for label, row in (("null", {"last_result": None}), ("blank", {"last_result": ""}),
                           ("spaces", {"last_result": "   "}), ("missing", {})):
            with self.subTest(case=label):
                self.srv.tables["scheduler_status"] = [
                    {"job_name": j, "last_run_at": self.now.isoformat(), **row} for j in sdj.DAILY_JOBS]
                self.srv.requests.clear()
                self.sent.clear()
                self.state = sdj.DailyJobState()
                out = await self.tick()
                self.assertEqual(set(out.values()), {"state_unknown"}, out)
                self.assertEqual(self.posts("daily_summary") + self.posts("kpi_7day_snapshot")
                                 + self.posts("daily_calc_snapshot") + self.posts("scheduler_status"), 0)
                self.assertEqual(self.sent, [])

    async def test_explicit_fail_row_still_retries(self):
        self.srv.tables["scheduler_status"] = [
            {"job_name": j, "last_run_at": self.now.isoformat(), "last_result": "FAIL: X"} for j in sdj.DAILY_JOBS]
        out = await self.tick()
        self.assertEqual({out[j] for j in JOBS[:3]}, {"ok"}, out)

    # independent review: the alert path's own reader (sync_operated_status) must be strict too
    async def test_sync_read_outage_never_produces_false_no_data_alert(self):
        pre = [{"날짜": self.yesterday, "source": "confirmed", "operated": True}]
        for label, setup in (
            ("503", lambda: (setattr(self.srv, "fail_mode", "503"),
                             setattr(self.srv, "fail_tables", {"raw_calls", "gpx_sessions"}),
                             setattr(self.srv, "fail_methods", {"GET"}))),
            ("empty200", lambda: (setattr(self.srv, "get_quirk", "empty"),
                                  setattr(self.srv, "fail_tables", {"raw_calls", "gpx_sessions"}))),
            ("operated_status_503", lambda: (setattr(self.srv, "fail_mode", "503"),
                                             setattr(self.srv, "fail_tables", {"operated_status"}),
                                             setattr(self.srv, "fail_methods", {"GET"}))),
        ):
            with self.subTest(case=label):
                self.srv.fail_mode = self.srv.fail_tables = self.srv.fail_methods = self.srv.get_quirk = None
                self.srv.tables.pop("scheduler_status", None)
                self.srv.tables["operated_status"] = [dict(r) for r in pre]
                self.sent.clear()
                self.state = sdj.DailyJobState()
                setup()
                out = await self.tick()
                self.assertEqual(out["check_daily_ingestion_detail"], "failed", out)
                self.assertEqual(self.alerts() + [m for m in self.sent if "데이터 없음" in m], [])
                self.assertEqual(self.srv.tables["operated_status"], pre)  # nothing stamped None/None
        self.srv.fail_mode = self.srv.fail_tables = self.srv.fail_methods = self.srv.get_quirk = None

    # CASSANDRA 5067 F2: send succeeds, outcome write fails, process restarts
    async def test_send_ok_status_write_fails_restart_does_not_resend(self):
        self.arm_alert()
        real_send = self._fake_send

        async def send_then_break_status(msg):
            r = await real_send(msg)
            if "부분누락" in msg:
                self.srv.fail_mode = "503"
                self.srv.fail_tables = {"scheduler_status"}
                self.srv.fail_methods = {"POST"}
            return r
        legacy.send_telegram_broadcast = send_then_break_status
        out = await self.tick()
        self.assertEqual(out["check_daily_ingestion_detail"], "ok_status_write_failed")
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.status("check_daily_ingestion_detail")[:7], "CLAIMED")
        # restart: new process state, database healthy again
        self.srv.fail_mode = None
        self.srv.fail_tables = None
        self.srv.fail_methods = None
        self.clock.advance(sdj.retry_interval_seconds() + 1)
        out = await self.tick(state=sdj.DailyJobState(), now=self.now + timedelta(minutes=30))
        self.assertEqual(out["check_daily_ingestion_detail"], "uncertain_done")
        self.assertEqual(len(self.alerts()), 1)

    async def test_claim_write_failure_means_no_send(self):
        self.arm_alert()
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"scheduler_status"}
        self.srv.fail_methods = {"POST"}
        out = await self.tick()
        self.assertEqual(out["check_daily_ingestion_detail"], "claim_failed")
        self.assertEqual(self.alerts(), [])

    # 4. the original bug, documented on the real stack
    async def test_original_guards_were_fail_open(self):
        self.srv.fail_mode = "503"
        self.assertEqual(await legacy.sb_select("scheduler_status", {"job_name": "eq.x"}), [])
        with self.assertRaises(RuntimeError):
            await legacy.sb_select_required("scheduler_status", {"job_name": "eq.x"})
        # old guard: unreadable state -> "not run yet" -> duplicate run allowed (kept for other jobs)
        self.assertFalse(await legacy.already_ran_today_kst("calc_daily_snapshot"))

    # 5. status write outage after a successful job
    async def test_status_write_outage_after_success(self):
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"scheduler_status"}
        self.srv.fail_methods = {"POST"}
        out = await self.tick()
        self.assertEqual({out[j] for j in JOBS[:3]}, {"ok_status_write_failed"}, out)
        # data really was written, status is unknown -> idempotent jobs rerun on the next interval
        self.srv.fail_mode = None
        self.clock.advance(sdj.retry_interval_seconds() + 1)
        out = await self.tick(now=self.now + timedelta(minutes=20))
        self.assertEqual({out[j] for j in JOBS[:3]}, {"ok"}, out)
        self.assertEqual(len(self.srv.tables["kpi_7day_snapshot"]), 1)  # upsert, no duplicate row
        self.assertEqual(len(self.srv.tables["daily_calc_snapshot"]), 1)

    # 6. catch-up of a missed date (flag off by default)
    async def test_catchup_recomputes_missed_day_only_when_enabled(self):
        missing = str(self.now.date() - timedelta(days=2))
        self.srv.tables["daily_calc_snapshot"] = [
            {"calc_date": str(self.now.date() - timedelta(days=d)), "axis": "A"} for d in (3, 4)]
        self.srv.tables["raw_calls"].append({
            "날짜": missing, "콜유형": "일반", "요금": 9000,
            "배차시각": f"{missing}T21:00:00+09:00", "verify_status": "verified"})
        await self.tick()
        dates = {r["calc_date"] for r in self.srv.tables["daily_calc_snapshot"]}
        self.assertNotIn(missing, dates)  # disabled by default

        self.srv.tables.pop("scheduler_status", None)
        with patch.dict(os.environ, {"TASK194_CATCHUP_DAYS": "5"}):
            await self.tick(state=sdj.DailyJobState(), now=self.now + timedelta(minutes=30))
        dates = {r["calc_date"] for r in self.srv.tables["daily_calc_snapshot"]}
        self.assertIn(missing, dates)

    async def test_catchup_with_unreadable_snapshot_table_does_nothing(self):
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"daily_calc_snapshot"}
        self.srv.fail_methods = {"GET"}
        with patch.dict(os.environ, {"TASK194_CATCHUP_DAYS": "5"}):
            await self.tick()
        dates = {r["calc_date"] for r in self.srv.tables["daily_calc_snapshot"]}
        self.assertEqual(dates, {self.yesterday})  # regular run only; no catch-up burst
        with self.assertRaises(sdj.SchedulerStateUnknown):
            await sdj.missing_snapshot_dates(legacy, self.now, 5)


if __name__ == "__main__":
    unittest.main()
