"""Isolated scheduler-to-real-SQLite integration, no production credentials or writes."""
import ast
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from zoneinfo import ZoneInfo


class SchedulerDatabaseIntegration(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE scheduler_status(job_name TEXT PRIMARY KEY, last_run_at TEXT)")
        self.kst = ZoneInfo("Asia/Seoul")
        self.fail_reads = False
        self.executions = 0
        fn = next(n for n in ast.parse(Path("bot_v5_legacy.py").read_text()).body
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "already_ran_today_kst")
        class Logger:
            def error(self, *args, **kwargs): pass
        async def sb_h(method, table, params):
            if self.fail_reads:
                raise TimeoutError("simulated Supabase transport timeout")
            assert method == "GET" and table == "scheduler_status"
            job_name = params["job_name"].removeprefix("eq.")
            rows = self.conn.execute(
                "SELECT last_run_at FROM scheduler_status WHERE job_name=?", (job_name,)
            ).fetchall()
            return [{"last_run_at": r[0]} for r in rows]
        namespace = {"sb_h": sb_h, "logger": Logger(), "datetime": datetime, "KST": self.kst}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<real_guard>", "exec"), namespace)
        self.guard = namespace["already_ran_today_kst"]

    async def asyncTearDown(self):
        self.conn.close()

    async def scheduler_tick(self):
        # Actual repository guard + persistent SQL status + scheduler dispatch decision.
        if await self.guard("calc_daily_snapshot"):
            return False
        self.executions += 1
        self.conn.execute(
            "INSERT INTO scheduler_status(job_name,last_run_at) VALUES(?,?) "
            "ON CONFLICT(job_name) DO UPDATE SET last_run_at=excluded.last_run_at",
            ("calc_daily_snapshot", datetime.now(self.kst).isoformat()),
        )
        self.conn.commit()
        return True

    async def test_timeout_recovery_and_duplicate_prevention(self):
        self.fail_reads = True
        self.assertFalse(await self.scheduler_tick())
        self.assertEqual(self.executions, 0)
        self.fail_reads = False
        self.assertTrue(await self.scheduler_tick())
        self.assertFalse(await self.scheduler_tick())
        self.assertEqual(self.executions, 1)
        count = self.conn.execute("SELECT count(*) FROM scheduler_status").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_corrupt_sql_state_blocks_until_repaired(self):
        self.conn.execute(
            "INSERT INTO scheduler_status VALUES (?,?)",
            ("calc_daily_snapshot", "invalid timestamp"),
        )
        self.conn.commit()
        self.assertFalse(await self.scheduler_tick())
        self.assertEqual(self.executions, 0)
        self.conn.execute("DELETE FROM scheduler_status")
        self.conn.commit()
        self.assertTrue(await self.scheduler_tick())
        self.assertEqual(self.executions, 1)
