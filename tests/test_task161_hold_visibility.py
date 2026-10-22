"""Task #161 evidence (CASSANDRA 4932 PARTIAL PASS: 'E2E response, old-event recovery outside the global 20, 6 h cooldown fault injection,
regression after redeploy were not run independently').

NO PRODUCTION ACCESS.  The real code (HealthHandler /mcp/ops_snapshot, build_agent_context_snapshot, run_geomnuri_patrol_once / _patrol_emit_once)
runs against an in-process PostgREST stand-in on 127.0.0.1 (tests/fake_postgrest.py, extended here with the `not.in` filter and multi-column order
that these queries use - the shipped stand-in ignores both).  The bot code is NOT modified by this change.

Tests marked FINDING record behaviour that the code has TODAY and that is questionable; they pin the current behaviour so the finding is visible and
a future fix must change the test on purpose.  They are not claims that the behaviour is right.
"""
import asyncio
import json
import os
import sys
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ["MCP_API_KEY"] = "test-mcp-key"

import bot_v5_legacy as legacy  # noqa: E402
from fake_postgrest import FakePostgREST  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


class Fake(FakePostgREST):
    """adds what the #161 queries use and the shipped stand-in silently ignores: `not.in.(..)` and `order=a.asc,b.desc`"""

    @staticmethod
    def _match(value, op, arg):
        if op == "not":
            inner_op, _, inner_arg = arg.partition(".")
            return not FakePostgREST._match(value, inner_op, inner_arg) if value is not None else True
        return FakePostgREST._match(value, op, arg)

    def _select(self, rows, params):
        out = self._filter(rows, params)
        qp = dict(params)
        for spec in reversed([s for s in (qp.get("order") or "").split(",") if s]):
            col, _, direction = spec.partition(".")
            out.sort(key=lambda r: (r.get(col) is None, r.get(col) if isinstance(r.get(col), (int, float)) else str(r.get(col))),
                     reverse=(direction == "desc"))
        if qp.get("limit"):
            out = out[: int(qp["limit"])]
        sel = qp.get("select")
        if sel and sel != "*":
            cols = [c.strip() for c in sel.split(",")]
            out = [{c: r.get(c) for c in cols} for r in out]
        return [dict(r) for r in out]


def ts(minutes_ago):
    return (datetime(2026, 10, 22, 12, 0, tzinfo=KST) - timedelta(minutes=minutes_ago)).isoformat()


def hold(tid, **kw):
    t = {"task_id": tid, "title": f"hold {tid}", "status": "HOLD", "priority": "P1", "owner_agent": "마기", "support_agents": [],
         "blocked_reason": "대표 결정 대기", "waiting_for": "대표", "next_action": "결정 후 재개", "updated_at": ts(100000), "domain": "system",
         "verification_status": None, "architect_decision_required": False}
    t.update(kw)
    return t


def ev(eid, tid, minutes_ago, typ="NOTE", detail="x"):
    return {"event_id": eid, "task_id": tid, "event_type": typ, "actor": "t", "detail": detail, "created_at": ts(minutes_ago)}


class Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Fake().start()
        cls._patch = patch.object(legacy, "SUPABASE_URL", cls.srv.url)
        cls._patch.start()
        cls.http = HTTPServer(("127.0.0.1", 0), legacy.HealthHandler)
        threading.Thread(target=cls.http.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.http.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls._patch.stop(); cls.srv.stop()

    def setUp(self):
        s = self.srv
        s.tables.clear(); s.fail_mode = None; s.fail_tables = None; s.fail_methods = None; s.get_quirk = None; s.requests.clear()

    def snapshot(self, key="test-mcp-key"):
        req = urllib.request.Request(self.base + "/mcp/ops_snapshot", data=b"{}", method="POST", headers={"X-MCP-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class OpsSnapshotE2E(Base):
    def seed_four_holds(self):
        """HOLD #68/#114/#148/#149 like the production backlog; each one's latest event is OLDER than 60 newer events of other tasks"""
        self.srv.tables["magi_tasks"] = [
            hold(68, priority="P2", updated_at=ts(500000)), hold(114), hold(148, waiting_for=""), hold(149, next_action=None),
            {**hold(900), "status": "IN_PROGRESS", "title": "active", "blocked_reason": None}, {**hold(901), "status": "COMPLETED"}, {**hold(902), "status": "CLOSED"},
        ]
        evs = [ev(3553, 68, 90000, "MAGI_DECISION"), ev(3500, 68, 99000), ev(3601, 114, 80000, "HOLD_REASON"), ev(3700, 148, 70000), ev(3800, 149, 60000)]
        evs += [ev(5000 + i, 999, i + 1) for i in range(60)]                      # 60 newer events on another task push the HOLD events out of any "recent 20"
        self.srv.tables["magi_task_events"] = evs

    def test_h1_old_events_outside_the_global_20_are_recovered_per_task(self):
        self.seed_four_holds()
        code, js = self.snapshot()
        self.assertEqual(code, 200, js)
        recent_ids = {e["event_id"] for e in js["recent_events"]}
        self.assertEqual(len(js["recent_events"]), 20)
        self.assertTrue(recent_ids.isdisjoint({3553, 3601, 3700, 3800}), "precondition: the HOLD events are NOT in the global recent 20")
        hb = {t["task_id"]: t for t in js["hold_backlog"]}
        self.assertEqual(set(hb), {68, 114, 148, 149}, "only HOLD tasks; COMPLETED/CLOSED/IN_PROGRESS excluded")
        self.assertEqual({k: v["latest_event"]["event_id"] for k, v in hb.items()}, {68: 3553, 114: 3601, 148: 3700, 149: 3800},
                         "task 68 -> 3553 (the newest of its two events), recovered although far outside the recent-20 window")
        self.assertEqual(hb[68]["latest_event"]["event_type"], "MAGI_DECISION")

    def test_h2_metadata_completeness_is_computed_per_field(self):
        self.seed_four_holds()
        _, js = self.snapshot()
        hb = {t["task_id"]: t for t in js["hold_backlog"]}
        self.assertEqual({k: v["metadata_complete"] for k, v in hb.items()}, {68: True, 114: True, 148: False, 149: False})
        self.assertEqual(js["summary"]["hold_backlog"], 4)
        self.assertEqual(js["summary"]["hold_metadata_incomplete"], 2)
        self.assertEqual(len(js["blockers"]), 4)

    def test_h3_endpoint_is_read_only_and_requires_the_key(self):
        self.seed_four_holds()
        before = json.dumps(self.srv.tables, sort_keys=True)
        self.snapshot()
        self.assertEqual(before, json.dumps(self.srv.tables, sort_keys=True), "ops_snapshot changed stored data")
        self.assertEqual({m for m, _ in self.srv.requests}, {"GET"}, "ops_snapshot issued a non-GET request")
        code, js = self.snapshot(key="wrong")
        self.assertEqual(code, 401)

    def test_h4_agent_context_carries_the_same_per_task_recovery(self):
        self.seed_four_holds()
        ctx = asyncio.run(legacy.build_agent_context_snapshot("CASPER"))
        hb = {t["task_id"]: t for t in ctx["hold_backlog"]}
        self.assertEqual({k: v["latest_event"]["event_id"] for k, v in hb.items()}, {68: 3553, 114: 3601, 148: 3700, 149: 3800})
        self.assertEqual({k: v["metadata_complete"] for k, v in hb.items()}, {68: True, 114: True, 148: False, 149: False})

    def test_h5_finding_a_noisy_hold_task_can_push_a_quiet_one_out_of_the_limit_500(self):
        """FINDING F161-1: the per-task recovery is ONE query for all HOLD tasks, `order=created_at.desc&limit=500`.  Task 700 (noisy, e.g. a
        PATROL_ALERT stream) has 600 events newer than the only event of quiet HOLD task 701 -> 701 gets latest_event=None.  The recovery that
        fixes 'a global limit hides old events' re-creates the same failure with a different limit.  Pinned as current behaviour; fix = one
        query per task (limit=1) or a per-task window."""
        self.srv.tables["magi_tasks"] = [hold(700), hold(701)]
        self.srv.tables["magi_task_events"] = [ev(10_000 + i, 700, i + 1, "PATROL_ALERT") for i in range(600)] + [ev(42, 701, 90000, "MAGI_DECISION")]
        _, js = self.snapshot()
        hb = {t["task_id"]: t for t in js["hold_backlog"]}
        self.assertEqual(hb[700]["latest_event"]["event_id"], 10_000)
        self.assertIsNone(hb[701]["latest_event"], "FINDING F161-1 (pinned): quiet HOLD task loses its latest event behind 600 newer events of another HOLD task")
        ctx = asyncio.run(legacy.build_agent_context_snapshot("CASPER"))
        self.assertIsNone({t["task_id"]: t for t in ctx["hold_backlog"]}[701]["latest_event"], "same in agent_context")

    def test_h6_finding_a_failed_event_read_degrades_silently_to_latest_event_none(self):
        """FINDING F161-2: sb_select swallows errors (returns []).  If only the per-task event read fails, hold_backlog is still returned with
        success=true and latest_event=None for every task - indistinguishable from 'HOLD task without events'."""
        self.seed_four_holds()
        self.srv.fail_mode = "503"; self.srv.fail_tables = {"magi_task_events"}
        code, js = self.snapshot()
        self.assertEqual(code, 200)
        self.assertTrue(js["success"])
        self.assertTrue(all(t["latest_event"] is None for t in js["hold_backlog"]), "FINDING F161-2 (pinned)")
        self.assertEqual(js["summary"]["hold_backlog"], 4)

    def test_h7_tasks_read_failure_is_an_empty_backlog_with_success_true(self):
        """FINDING F161-2b: same family - if the magi_tasks read fails the snapshot says success=true with 0 tasks and 0 HOLD."""
        self.seed_four_holds()
        self.srv.fail_mode = "503"; self.srv.fail_tables = {"magi_tasks"}
        code, js = self.snapshot()
        self.assertEqual((code, js["success"], js["summary"]["hold_backlog"], js["summary"]["active_total"]), (200, True, 0, 0), "FINDING F161-2b (pinned)")


class PatrolHoldCooldown(Base):
    """run_geomnuri_patrol_once, HOLD-metadata check (6 h cooldown per task), fault injection."""

    def setUp(self):
        super().setUp()
        self.srv.tables["magi_tasks"] = [hold(148, waiting_for=""), hold(149, next_action=None), hold(114)]
        self._tg = patch.object(legacy, "send_telegram_broadcast", AsyncMock(return_value={"sent": 1, "failed": 0}))
        self._tg.start()

    def tearDown(self):
        self._tg.stop()

    def run_patrol(self):
        return asyncio.run(legacy.run_geomnuri_patrol_once())

    def hold_alerts(self):
        return [e for e in self.srv.tables.get("magi_task_events", []) if e.get("event_type") == "PATROL_ALERT" and "HOLD Task 재개조건" in (e.get("detail") or "")]

    def test_p1_incomplete_hold_alerts_once_per_task_then_is_suppressed_for_6h(self):
        self.run_patrol()
        first = self.hold_alerts()
        self.assertEqual(sorted(e["task_id"] for e in first), [148, 149], "only incomplete HOLD tasks alert; #114 (complete) does not")
        self.assertIn("missing=waiting_for", [e["detail"] for e in first if e["task_id"] == 148][0])
        self.assertIn("missing=next_action", [e["detail"] for e in first if e["task_id"] == 149][0])
        self.run_patrol(); self.run_patrol()
        self.assertEqual(len(self.hold_alerts()), 2, "repeat patrols within the cooldown add nothing")

    def test_p2_cooldown_boundary_5h59_suppressed_6h01_alerts_again(self):
        self.run_patrol()
        status = self.srv.tables["scheduler_status"]
        key148 = next(r for r in status if r["job_name"].startswith("patrol_alert::hold_metadata_incomplete::148"))
        key149 = next(r for r in status if r["job_name"].startswith("patrol_alert::hold_metadata_incomplete::149"))
        now = datetime.now(KST)
        key148["last_run_at"] = (now - timedelta(hours=5, minutes=59)).isoformat()
        key149["last_run_at"] = (now - timedelta(hours=6, minutes=1)).isoformat()
        self.run_patrol()
        after = self.hold_alerts()
        self.assertEqual(sorted(e["task_id"] for e in after), [148, 149, 149], "149 (6 h 01 min) alerts again, 148 (5 h 59 min) does not")

    def test_p3_alert_never_changes_task_state(self):
        before = json.dumps(self.srv.tables["magi_tasks"], sort_keys=True)
        self.run_patrol(); self.run_patrol()
        self.assertEqual(before, json.dumps(self.srv.tables["magi_tasks"], sort_keys=True), "patrol modified magi_tasks")
        self.assertFalse([1 for m, t in self.srv.requests if m in ("PATCH",) and t == "magi_tasks"], "patrol PATCHed magi_tasks (no auto-resume / status change allowed)")
        self.assertEqual({t["status"] for t in self.srv.tables["magi_tasks"]}, {"HOLD"})

    def test_p4_finding_cooldown_state_read_failure_fails_open_and_duplicates(self):
        """FINDING F161-3: `_patrol_emit_once` reads the cooldown row inside try/except -> rows=[] on error.  If only scheduler_status GETs fail
        (writes still work) every patrol re-emits the HOLD alert (no cooldown) - alert spam at the patrol period (5 min)."""
        self.run_patrol()
        n1 = len(self.hold_alerts())
        self.srv.fail_mode = "503"; self.srv.fail_tables = {"scheduler_status"}; self.srv.fail_methods = {"GET"}
        self.run_patrol(); self.run_patrol()
        self.assertGreater(len(self.hold_alerts()), n1, "FINDING F161-3 (pinned): the cooldown is lost when its state cannot be read")

    def test_p5_finding_unparsable_cooldown_timestamp_fails_open(self):
        """FINDING F161-3b: a garbage last_run_at makes the age check fall through to 'emit'."""
        self.run_patrol()
        for r in self.srv.tables["scheduler_status"]:
            if r["job_name"].startswith("patrol_alert::hold_metadata_incomplete"):
                r["last_run_at"] = "not-a-time"
        n1 = len(self.hold_alerts())
        self.run_patrol()
        self.assertEqual(len(self.hold_alerts()), n1 + 2, "FINDING F161-3b (pinned)")

    def test_p6_hold_read_failure_is_silent_fail_open(self):
        """FINDING F161-2c: with magi_tasks unreadable the HOLD check sees [] and reports nothing - no 'check itself failed' alert (sb_select swallows)."""
        self.srv.fail_mode = "503"; self.srv.fail_tables = {"magi_tasks"}
        res = self.run_patrol()
        self.assertEqual(self.hold_alerts(), [])
        self.assertFalse([f for f in res.get("findings", []) if "HOLD 메타데이터 점검 자체 실패" in f.get("detail", "")] if isinstance(res, dict) else [], "pinned: no self-failure alert for the HOLD check")

    def test_p7_write_failure_does_not_hide_the_finding_and_does_not_burn_the_cooldown(self):
        """event insert fails (503) -> no PATROL_ALERT row, no cooldown row written -> next healthy patrol alerts (nothing lost)."""
        self.srv.fail_mode = "503"; self.srv.fail_tables = {"magi_task_events", "scheduler_status"}; self.srv.fail_methods = {"POST"}
        self.run_patrol()
        self.assertEqual(self.hold_alerts(), [])
        self.srv.fail_mode = None; self.srv.fail_tables = None; self.srv.fail_methods = None
        self.run_patrol()
        self.assertEqual(sorted(e["task_id"] for e in self.hold_alerts()), [148, 149])


if __name__ == "__main__":
    unittest.main()
