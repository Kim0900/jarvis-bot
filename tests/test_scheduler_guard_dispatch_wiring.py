"""Verify the production scheduler actually invokes the persistent DB guard."""
import ast
from pathlib import Path
import unittest


class ProductionSchedulerWiringTests(unittest.TestCase):
    def test_four_am_jobs_are_guarded_at_dispatch(self):
        source = Path(__file__).resolve().parents[1].joinpath("bot_v5_legacy.py").read_text()
        tree = ast.parse(source)
        scheduler = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fish_scheduler")
        jobs = {"recalc_daily_summary_totals", "recalc_7day_average", "calc_daily_snapshot", "check_daily_ingestion_detail"}
        # Verify each production job is in the guarded dispatch tuple.
        guarded = []
        for node in ast.walk(scheduler):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "daily_jobs" for t in node.targets):
                for item in node.value.elts:
                    if isinstance(item.elts[0], ast.Constant):
                        guarded.append(item.elts[0].value)
        self.assertEqual(set(guarded), jobs)
        self.assertIn("last_kpi7day = now.day", source)
        self.assertIn("mark_scheduler_run_required(job_name, result)", source)
        # The actual dispatch loop must check the DB before calling the job.
        loops = [n for n in ast.walk(scheduler) if isinstance(n, ast.For) and isinstance(n.target, ast.Tuple) and any(isinstance(t, ast.Name) and t.id == "job_name" for t in n.target.elts)]
        self.assertTrue(any(
            any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "already_ran_today_kst" for n in ast.walk(loop.body[0]))
            and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "job_func" for n in ast.walk(loop))
            for loop in loops
        ))


if __name__ == "__main__":
    unittest.main()
