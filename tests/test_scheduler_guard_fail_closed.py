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
