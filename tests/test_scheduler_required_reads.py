"""Required PostgREST reads must never silently turn errors into zero KPI data."""
import ast
import asyncio
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import AsyncMock


class RequiredReadTests(unittest.TestCase):
    def _run(self, result):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("bot_v5_legacy.py").read_text())
        fn = next(x for x in tree.body if isinstance(x, ast.AsyncFunctionDef) and x.name == "sb_select_required")
        ns = {"sb_h": AsyncMock(return_value=result)}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<required_read>", "exec"), ns)
        return asyncio.run(ns["sb_select_required"]("raw_calls", {"limit": "1"}))

    def test_empty_data_is_valid(self):
        self.assertEqual(self._run([]), [])

    def test_data_is_preserved(self):
        self.assertEqual(self._run([{"id": 1}]), [{"id": 1}])

    def test_http_failure_blocks_kpi(self):
        with self.assertRaises(RuntimeError):
            self._run(None)

    def test_invalid_response_blocks_kpi(self):
        with self.assertRaises(RuntimeError):
            self._run({"error": "503"})

    def test_critical_kpi_paths_use_required_reads(self):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("bot_v5_legacy.py").read_text())
        for name in ("recalc_daily_summary_totals", "recalc_7day_average", "calc_daily_snapshot"):
            fn = next(x for x in tree.body if isinstance(x, ast.AsyncFunctionDef) and x.name == name)
            required = [x for x in ast.walk(fn) if isinstance(x, ast.Call) and isinstance(x.func, ast.Name) and x.func.id == "sb_select_required"]
            self.assertGreaterEqual(len(required), 1, name)


if __name__ == "__main__":
    unittest.main()
