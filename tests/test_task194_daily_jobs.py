"""Task #194 unit tests for scheduler_daily_jobs (no network, no production access)."""
import ast
import asyncio
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

import scheduler_daily_jobs as sdj

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]


def kst(h, m=0, day=12):
    return datetime(2026, 10, day, h, m, 5, tzinfo=KST)


class FakeLogger:
    def __init__(self):
        self.lines = []

    def error(self, *a, **k):
        self.lines.append(("error", a))

    def info(self, *a, **k):
        self.lines.append(("info", a))


class FakeLegacy:
    """In-memory scheduler_status with switchable read/write failure modes."""

    def __init__(self):
        self.rows = {}
        self.read_mode = "ok"  # ok | raise | none | dup | naive | future | nostamp | badrow
        self.write_fail = False
        self.writes = []
        self.reads = 0
        self.logger = FakeLogger()
        self.snapshot_dates = []
        self.calls = {n: 0 for n in sdj.DAILY_JOBS}
        self.job_error = {}
        self.catchup_calls = []

    async def sb_h(self, method, path, params=None, **kw):
        self.reads += 1
        if path == "daily_calc_snapshot":
            if self.read_mode in ("raise", "none"):
                if self.read_mode == "raise":
                    raise TimeoutError()
                return None
            return [{"calc_date": d} for d in self.snapshot_dates]
        job = params["job_name"].removeprefix("eq.")
        if self.read_mode == "raise":
            raise TimeoutError()
        if self.read_mode == "none":
            return None
        row = self.rows.get(job)
        if self.read_mode == "dup":
            return [row or {"last_run_at": "x"}, row or {"last_run_at": "y"}]
        if self.read_mode == "badrow":
            return ["bad"]
        if row is None:
            return []
        row = dict(row)
        if self.read_mode == "naive":
            row["last_run_at"] = row["last_run_at"][:19]
        if self.read_mode == "future":
            row["last_run_at"] = (datetime.fromisoformat(row["last_run_at"]) + timedelta(days=1)).isoformat()
        if self.read_mode == "nostamp":
            row["last_run_at"] = None
        return [row]

    async def sb_get_strict(self, table, params=None):
        result = await self.sb_h("GET", table, params=params)
        if not isinstance(result, list):
            raise RuntimeError("strict read failed")
        return result

    async def sb_upsert(self, table, data, on_conflict):
        if self.write_fail:
            raise RuntimeError("sb_upsert 실패")
        self.writes.append(dict(data))
        self.rows[data["job_name"]] = dict(data)
        return [data]

    def job(self, name):
        async def run(*args):
            if args:
                self.catchup_calls.append(args[0])
                if self.job_error.get("catchup"):
                    raise self.job_error["catchup"]
                return None
            self.calls[name] += 1
            err = self.job_error.get(name)
            if err:
                raise err
            return {"날짜": "2026-10-11", "source": None, "operated": None} if name == "check_daily_ingestion_detail" else None
        return run

    def actions(self):
        return {n: self.job(n) for n in sdj.DAILY_JOBS}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class EmptyMessageError(Exception):
    def __str__(self):
        return ""


class DailyJobsTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.lg = FakeLegacy()
        self.state = sdj.DailyJobState()
        self.clock = Clock()

    async def tick(self, now, state=None):
        return await sdj.run_daily_jobs(self.lg, now, state or self.state, self.clock, self.lg.actions())

    # ---- happy path / duplicate prevention
    async def test_first_run_executes_all_jobs_and_records_ok(self):
        out = await self.tick(kst(4))
        self.assertEqual(set(out.values()), {"ok"})
        self.assertEqual(self.lg.calls, {n: 1 for n in sdj.DAILY_JOBS})
        self.assertEqual(self.lg.rows["calc_daily_snapshot"]["last_result"], "OK")
        self.assertTrue(self.lg.rows["check_daily_ingestion_detail"]["last_result"].startswith("{"))

    async def test_success_blocks_duplicate_even_after_restart(self):
        await self.tick(kst(4))
        self.clock.advance(3600)
        out = await self.tick(kst(5), sdj.DailyJobState())  # new state == process restart
        self.assertEqual(set(out.values()), {"done"})
        self.assertEqual(self.lg.calls, {n: 1 for n in sdj.DAILY_JOBS})

    async def test_yesterday_success_does_not_block_today(self):
        await self.tick(kst(4, day=11))
        self.clock.advance(86400)
        out = await self.tick(kst(4, day=12), sdj.DailyJobState())
        self.assertEqual(set(out.values()), {"ok"})
        self.assertEqual(self.lg.calls, {n: 2 for n in sdj.DAILY_JOBS})

    async def test_success_cache_avoids_rereading_status_every_tick(self):
        await self.tick(kst(4))
        reads = self.lg.reads
        for minute in range(1, 6):
            await self.tick(kst(4, minute))
        self.assertEqual(self.lg.reads, reads)

    # ---- fail closed on unreadable state (the CASSANDRA 4968 counterexample)
    async def test_read_timeout_does_not_run_jobs(self):
        self.lg.read_mode = "raise"
        out = await self.tick(kst(4))
        self.assertEqual(set(out.values()), {"state_unknown"})
        self.assertEqual(self.lg.calls, {n: 0 for n in sdj.DAILY_JOBS})
        self.assertEqual(self.lg.writes, [])

    async def test_non_200_read_none_does_not_run_jobs(self):
        self.lg.read_mode = "none"
        out = await self.tick(kst(4))
        self.assertEqual(set(out.values()), {"state_unknown"})
        self.assertEqual(self.lg.calls, {n: 0 for n in sdj.DAILY_JOBS})

    async def test_ambiguous_state_never_runs_jobs(self):
        for mode in ("dup", "badrow", "naive", "future", "nostamp"):
            with self.subTest(mode=mode):
                lg = FakeLegacy()
                lg.rows = {n: {"job_name": n, "last_run_at": kst(3).isoformat(), "last_result": "OK"}
                           for n in sdj.DAILY_JOBS}
                lg.read_mode = mode
                out = await sdj.run_daily_jobs(lg, kst(4), sdj.DailyJobState(), Clock(), lg.actions())
                self.assertEqual(set(out.values()), {"state_unknown"}, mode)
                self.assertEqual(sum(lg.calls.values()), 0, mode)

    # ---- retry rules
    async def test_fail_record_does_not_block_retry_and_next_success_is_stored(self):
        self.lg.job_error["calc_daily_snapshot"] = RuntimeError("boom")
        out = await self.tick(kst(4))
        self.assertEqual(out["calc_daily_snapshot"], "failed")
        self.assertTrue(self.lg.rows["calc_daily_snapshot"]["last_result"].startswith("FAIL: RuntimeError"))
        del self.lg.job_error["calc_daily_snapshot"]
        self.clock.advance(sdj.retry_interval_seconds() + 1)
        out = await self.tick(kst(4, 20))
        self.assertEqual(out["calc_daily_snapshot"], "ok")
        self.assertEqual(self.lg.rows["calc_daily_snapshot"]["last_result"], "OK")
        # the other three succeeded the first time and were not rerun
        self.assertEqual(self.lg.calls["recalc_7day_average"], 1)

    async def test_failure_text_is_never_empty(self):
        self.lg.job_error["recalc_7day_average"] = EmptyMessageError()
        await self.tick(kst(4))
        result = self.lg.rows["recalc_7day_average"]["last_result"]
        self.assertTrue(result.startswith("FAIL: EmptyMessageError"))
        self.assertGreater(len(result), len("FAIL: "))

    async def test_cooldown_prevents_hammering(self):
        self.lg.job_error["calc_daily_snapshot"] = RuntimeError("boom")
        await self.tick(kst(4))
        self.clock.advance(60)
        out = await self.tick(kst(4, 1))
        self.assertEqual(out["calc_daily_snapshot"], "cooldown")
        self.assertEqual(self.lg.calls["calc_daily_snapshot"], 1)

    async def test_failed_job_does_not_stop_the_others(self):
        self.lg.job_error["recalc_daily_summary_totals"] = RuntimeError("boom")
        out = await self.tick(kst(4))
        self.assertEqual(out["recalc_daily_summary_totals"], "failed")
        self.assertEqual({out[n] for n in sdj.DAILY_JOBS[1:]}, {"ok"})

    async def test_window_bounds(self):
        for now, expected in ((kst(3, 59), "outside_window"), (kst(12, 0), "outside_window"),
                              (kst(11, 59), "ok")):
            with self.subTest(now=now):
                lg = FakeLegacy()
                out = await sdj.run_daily_jobs(lg, now, sdj.DailyJobState(), Clock(), lg.actions())
                self.assertEqual(set(out.values()), {expected})

    async def test_long_outage_recovers_inside_window(self):
        """10/11 pattern: database unreachable 03:47-07:33, jobs must run once it is back."""
        self.lg.read_mode = "raise"
        now = kst(4)
        recovered = None
        while now.hour < 12:
            out = await self.tick(now)
            if now >= kst(7, 33) and self.lg.read_mode == "raise":
                self.lg.read_mode = "ok"
            if set(out.values()) == {"ok"} and recovered is None:
                recovered = now
            now += timedelta(minutes=1)
            self.clock.advance(60)
        self.assertIsNotNone(recovered)
        self.assertLess(recovered, kst(8, 0))
        self.assertEqual(self.lg.calls, {n: 1 for n in sdj.DAILY_JOBS})

    # ---- status write failures
    async def test_success_status_write_failure_retries_idempotent_jobs_only(self):
        self.lg.write_fail = True
        out = await self.tick(kst(4))
        # the alert job's durable claim cannot be stored -> it must not send at all
        self.assertEqual(out["check_daily_ingestion_detail"], "claim_failed")
        self.assertEqual(self.lg.calls["check_daily_ingestion_detail"], 0)
        self.assertEqual({v for k, v in out.items() if k != "check_daily_ingestion_detail"},
                         {"ok_status_write_failed"})
        self.lg.write_fail = False
        self.clock.advance(sdj.retry_interval_seconds() + 1)
        out = await self.tick(kst(4, 20))
        self.assertEqual(self.lg.calls["recalc_daily_summary_totals"], 2)
        self.assertEqual(self.lg.calls["check_daily_ingestion_detail"], 1)
        self.assertEqual(out["check_daily_ingestion_detail"], "ok")

    async def test_claim_without_outcome_blocks_resend_after_restart(self):
        # process 1 claimed, sent, then died / lost the outcome write
        self.lg.rows["check_daily_ingestion_detail"] = {
            "job_name": "check_daily_ingestion_detail", "last_run_at": kst(4).isoformat(),
            "last_result": "CLAIMED: attempt=1"}
        out = await self.tick(kst(4, 30))
        self.assertEqual(out["check_daily_ingestion_detail"], "uncertain_done")
        self.assertEqual(self.lg.calls["check_daily_ingestion_detail"], 0)
        self.assertFalse(await sdj.succeeded_today(self.lg, "check_daily_ingestion_detail", kst(4, 30)))

    async def test_claim_is_written_before_the_job_runs(self):
        order = []
        orig = self.lg.sb_upsert

        async def spy(table, data, on_conflict):
            order.append(("write", data["last_result"][:7]))
            return await orig(table, data, on_conflict)
        self.lg.sb_upsert = spy

        async def alert():
            order.append(("run", ""))
            return {"x": 1}
        acts = self.lg.actions()
        acts["check_daily_ingestion_detail"] = alert
        await sdj.run_daily_jobs(self.lg, kst(4), sdj.DailyJobState(), self.clock, jobs=acts)
        alert_events = [e for e in order if e[0] == "run" or e[1] in ("CLAIMED",)]
        self.assertEqual(alert_events[:2], [("write", "CLAIMED"), ("run", "")])

    async def test_claim_from_yesterday_does_not_block_today(self):
        self.lg.rows["check_daily_ingestion_detail"] = {
            "job_name": "check_daily_ingestion_detail",
            "last_run_at": (kst(4) - timedelta(days=1)).isoformat(), "last_result": "CLAIMED: attempt=1"}
        out = await self.tick(kst(4))
        self.assertEqual(out["check_daily_ingestion_detail"], "ok")

    async def test_failure_status_write_failure_is_survivable(self):
        self.lg.job_error["calc_daily_snapshot"] = RuntimeError("boom")
        self.lg.write_fail = True
        out = await self.tick(kst(4))
        self.assertEqual(out["calc_daily_snapshot"], "failed")

    # ---- helpers
    def test_is_success_result(self):
        for ok in ("OK", "{'날짜': '2026-10-11', 'source': None, 'operated': None}"):
            self.assertTrue(sdj.is_success_result(ok))
        for bad in ("FAIL: ", "FAIL: X", "", "  ", None, 5):
            self.assertFalse(sdj.is_success_result(bad))

    def test_format_failure_for_empty_message_exceptions(self):
        import httpx
        text = sdj.format_failure(httpx.ReadTimeout(""))
        self.assertEqual(str(httpx.ReadTimeout("")), "")  # the original bug: empty str()
        self.assertIn("ReadTimeout", text)

    # ---- catch-up (disabled unless TASK194_CATCHUP_DAYS > 0)
    async def test_catchup_disabled_by_default(self):
        self.lg.snapshot_dates = ["2026-10-08"]
        await self.tick(kst(4))
        self.assertEqual(self.lg.catchup_calls, [])

    async def test_catchup_recomputes_only_missing_dates_when_enabled(self):
        self.lg.snapshot_dates = ["2026-10-07", "2026-10-09"]
        with patch.dict(os.environ, {"TASK194_CATCHUP_DAYS": "5"}):
            await self.tick(kst(4))
        # window is 10/06..10/10 (yesterday 10/11 is the regular run); missing: 06, 08, 10
        self.assertEqual(self.lg.catchup_calls, ["2026-10-06", "2026-10-08", "2026-10-10"])

    async def test_catchup_skipped_when_regular_snapshot_failed_or_read_fails(self):
        self.lg.snapshot_dates = []
        self.lg.job_error["calc_daily_snapshot"] = RuntimeError("boom")
        with patch.dict(os.environ, {"TASK194_CATCHUP_DAYS": "3"}):
            await self.tick(kst(4))
        self.assertEqual(self.lg.catchup_calls, [])
        lg = FakeLegacy()
        lg.read_mode = "none"
        with self.assertRaises(sdj.SchedulerStateUnknown):
            await sdj.missing_snapshot_dates(lg, kst(4), 3)

    async def test_catchup_stops_at_first_failure(self):
        self.lg.snapshot_dates = []
        self.lg.job_error["catchup"] = RuntimeError("boom")
        with patch.dict(os.environ, {"TASK194_CATCHUP_DAYS": "3"}):
            await self.tick(kst(4))
        self.assertEqual(len(self.lg.catchup_calls), 1)


class WiringTests(unittest.TestCase):
    """The production scheduler is sitecustomize.fish_scheduler (it replaces the legacy one)."""

    def setUp(self):
        self.site = (ROOT / "sitecustomize.py").read_text(encoding="utf-8")
        self.legacy = (ROOT / "bot_v5_legacy.py").read_text(encoding="utf-8")

    def test_sitecustomize_replaces_legacy_scheduler(self):
        self.assertIn("legacy.fish_scheduler = fish_scheduler", self.site)

    def test_production_dispatch_uses_single_module(self):
        self.assertIn("scheduler_daily_jobs.run_daily_jobs(legacy, now, daily_job_state)", self.site)
        self.assertIn("import scheduler_daily_jobs", self.site)

    def test_old_inline_04_blocks_are_gone(self):
        for name in sdj.DAILY_JOBS:
            self.assertNotIn(f'mark_scheduler_run("{name}"', self.site, name)
            self.assertNotIn(f"already_ran_today_kst(\"{name}\")", self.site, name)

    def test_legacy_scheduler_delegates_to_the_same_module(self):
        """PR#66 edited the legacy copy of the 04:00 block, which production replaces.
        Both schedulers must now call the one module, with no second copy of the job list."""
        tree = ast.parse(self.legacy)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fish_scheduler")
        src = ast.get_source_segment(self.legacy, fn)
        self.assertIn("scheduler_daily_jobs.run_daily_jobs(", src)
        for name in sdj.DAILY_JOBS:
            self.assertNotIn(f'mark_scheduler_run("{name}"', src, name)

    def test_jobs_read_with_required_reads(self):
        tree = ast.parse(self.legacy)
        fns = {n.name: n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
        for name in ("recalc_daily_summary_totals", "recalc_7day_average", "calc_daily_snapshot",
                     "check_daily_ingestion_detail"):
            calls = [n.func.id for n in ast.walk(fns[name]) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)]
            self.assertIn("sb_select_required", calls, name)
            self.assertNotIn("sb_select", calls, name)

    def test_sb_select_required_is_strict_getter(self):
        tree = ast.parse(self.legacy)
        fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "sb_select_required")
        calls = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("sb_get_strict", calls)
        self.assertNotIn("sb_h", calls)  # behaviour is covered by the E2E tests on the real HTTP stack



if __name__ == "__main__":
    unittest.main()
