"""Regression for retired Slack patrol cleanup (#204)."""
import ast
import unittest
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "bot_v5_legacy.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


class Task204PatrolRegression(unittest.TestCase):
    def test_retired_job_not_referenced_by_patrol(self):
        patrol = next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == "run_geomnuri_patrol_once")
        source = ast.get_source_segment(SOURCE, patrol)
        self.assertNotIn("magi_slack_control", source)
        self.assertNotIn("control_classifier_underposting", source)
        self.assertNotIn("patrol_internal_control_check", source)

    def test_normal_alerts_and_shared_classifier_preserved(self):
        self.assertIn("scheduler_fail::{job_name}", SOURCE)
        self.assertIn("patrol_internal_scheduler_check", SOURCE)
        self.assertIn("task_stale::{tid}", SOURCE)
        self.assertIn("patrol_internal_ingestion_check", SOURCE)
        self.assertTrue(any(isinstance(n, ast.FunctionDef) and
                            n.name == "_classify_control_event" for n in TREE.body))
        self.assertNotIn("GEOMNURI_PATROL_CONTROL_TASK_ID", SOURCE)


if __name__ == "__main__":
    unittest.main()
