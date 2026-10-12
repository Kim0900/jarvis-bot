"""Regression tests for fail-closed scheduler state checks."""
import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock


class SchedulerGuardTests(unittest.TestCase):
    def _guard(self, db_result, error=None):
        source = Path(__file__).resolve().parents[1].joinpath("bot_v5_legacy.py").read_text()
        module = ast.parse(source)
        fn = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "already_ran_today_kst")
        code = compile(ast.Module(body=[fn], type_ignores=[]), "<scheduler_guard>", "exec")
        from datetime import datetime
        from zoneinfo import ZoneInfo
        class Logger:
            def error(self, *args):
                pass
        db = AsyncMock(return_value=db_result, side_effect=error)
        namespace = {"sb_h": db, "logger": Logger(), "datetime": datetime, "KST": ZoneInfo("Asia/Seoul")}
        exec(code, namespace)
        return asyncio.run(namespace["already_ran_today_kst"]("calc_daily_snapshot"))

    def test_timeout_failure_reproduces_original_fail_open_bug(self):
        """A timeout caused legacy guard to return False (allow duplicate)."""
        self.assertTrue(self._guard(None, TimeoutError()))

    def test_empty_valid_result_remains_first_run(self):
        self.assertFalse(self._guard([]))

    def test_today_completed_run_blocks_duplicate(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        stamp = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
        self.assertTrue(self._guard([{"last_run_at": stamp, "last_result": "OK"}]))

    def test_yesterday_completed_run_allows_next_day(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        stamp = (datetime.now(ZoneInfo("Asia/Seoul")) - timedelta(days=1)).isoformat()
        self.assertFalse(self._guard([{"last_run_at": stamp, "last_result": "OK"}]))

    def test_non_object_row_blocks(self):
        self.assertTrue(self._guard(["bad-row"]))

    def test_multiple_rows_block(self):
        self.assertTrue(self._guard([{"last_run_at": "2026-10-10T00:00:00+00:00"}, {"last_run_at": "2026-10-11T00:00:00+00:00"}]))

    def test_naive_timestamp_blocks(self):
        self.assertTrue(self._guard([{"last_run_at": "2026-10-12T04:00:00"}]))

    def test_future_timestamp_blocks(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        stamp = (datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(days=1)).isoformat()
        self.assertTrue(self._guard([{"last_run_at": stamp, "last_result": "OK"}]))

    def test_utc_z_timestamp_parses(self):
        from datetime import datetime, timedelta, timezone
        stamp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        self.assertFalse(self._guard([{"last_run_at": stamp, "last_result": "OK"}]))

    def test_transient_failure_then_db_recovery(self):
        source = Path(__file__).resolve().parents[1].joinpath("bot_v5_legacy.py").read_text()
        fn = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "already_ran_today_kst")
        from datetime import datetime
        from zoneinfo import ZoneInfo
        class Logger:
            def error(self, *args): pass
        stamp = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
        db = AsyncMock(side_effect=[TimeoutError(), [], [{"last_run_at": stamp, "last_result": "OK"}]])
        namespace = {"sb_h": db, "logger": Logger(), "datetime": datetime, "KST": ZoneInfo("Asia/Seoul")}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<guard>", "exec"), namespace)
        async def scenario():
            guard = namespace["already_ran_today_kst"]
            blocked = await guard("calc_daily_snapshot")
            allowed_after_recovery = await guard("calc_daily_snapshot")
            blocked_after_success = await guard("calc_daily_snapshot")
            return blocked, allowed_after_recovery, blocked_after_success
        self.assertEqual(asyncio.run(scenario()), (True, False, True))
        self.assertEqual(db.await_count, 3)

    def test_today_failed_attempt_is_retryable(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        stamp = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
        self.assertFalse(self._guard([{"last_run_at": stamp, "last_result": "FAIL: timeout"}]))

    def test_today_unknown_result_remains_fail_closed(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        stamp = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
        self.assertTrue(self._guard([{"last_run_at": stamp, "last_result": ""}]))

    def test_failed_query_blocks_duplicate(self):
        self.assertTrue(self._guard(None))

    def test_network_error_blocks_duplicate(self):
        self.assertTrue(self._guard(None, RuntimeError("network unavailable")))

    def test_valid_empty_query_allows_first_run(self):
        self.assertFalse(self._guard([]))

    def test_missing_timestamp_blocks_duplicate(self):
        self.assertTrue(self._guard([{"last_run_at": None}]))

    def test_malformed_timestamp_blocks_duplicate(self):
        self.assertTrue(self._guard([{"last_run_at": "not-a-date"}]))

    def test_invalid_response_blocks_duplicate(self):
        self.assertTrue(self._guard({"error": "bad response"}))


if __name__ == "__main__":
    unittest.main()
