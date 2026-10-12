"""Compare the same timeout stimulus against unmodified main and PR66."""
import ast
import asyncio
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock
from datetime import datetime
from zoneinfo import ZoneInfo


def run_guard(source, db_result=None, error=None):
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef) and n.name == "already_ran_today_kst")
    class Logger:
        def error(self, *args, **kwargs): pass
    namespace = {
        "sb_select": AsyncMock(return_value=db_result, side_effect=error),
        "sb_h": AsyncMock(return_value=db_result, side_effect=error),
        "logger": Logger(), "datetime": datetime, "KST": ZoneInfo("Asia/Seoul"),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<guard>", "exec"), namespace)
    return asyncio.run(namespace["already_ran_today_kst"]("calc_daily_snapshot"))


class BeforeAfterTests(unittest.TestCase):
    def test_same_timeout_fails_before_and_blocks_after(self):
        base = subprocess.run(
            ["git", "show", "origin/main:bot_v5_legacy.py"],
            check=True, capture_output=True, text=True,
        ).stdout
        patch = Path("bot_v5_legacy.py").read_text()
        self.assertFalse(run_guard(base, error=TimeoutError()))
        self.assertTrue(run_guard(patch, error=TimeoutError()))

    def test_same_invalid_response_fails_before_and_blocks_after(self):
        base = subprocess.run(
            ["git", "show", "origin/main:bot_v5_legacy.py"],
            check=True, capture_output=True, text=True,
        ).stdout
        patch = Path("bot_v5_legacy.py").read_text()
        # Legacy sb_select silently returns [] on invalid response; patched sb_h rejects dict.
        self.assertFalse(run_guard(base, db_result=[]))
        self.assertTrue(run_guard(patch, db_result={"error": "invalid"}))


if __name__ == "__main__":
    unittest.main()
