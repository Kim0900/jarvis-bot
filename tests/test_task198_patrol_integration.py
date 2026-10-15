"""Task #198 stage 1: the real patrol code + real strict HTTP helpers against an in-process
PostgREST stand-in (tests/fake_postgrest.py).  Only the database and Telegram are replaced."""
import asyncio
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

import bot_v5_legacy as legacy  # noqa: E402
import patrol_alert_ledger as pal  # noqa: E402
from fake_postgrest import FakePostgREST  # noqa: E402

UTC = timezone.utc
JOBS = list(legacy._PATROL_SCHEDULER_MAX_AGE_SECONDS)


def iso(dt):
    return dt.astimezone(UTC).isoformat()


class Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakePostgREST().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        self._reset()

    def _reset(self):
        for p in getattr(self, "patches", []):
            p.stop()
        s = self.srv
        s.tables.clear()
        s.fail_mode = None
        s.fail_tables = None
        s.fail_methods = None
        s.get_quirk = None
        s.fail_body_contains = {}
        s.fail_query_contains = {}
        s.write_faults = []
        s.write_log = []
        s.rpc = {}
        s.max_rows = None
        s.range_quirk = None
        s._range_calls = 0
        s.requests.clear()
        pal.reset_local_state()
        self.sent = []
        self.send_results = None
        self.patches = [
            patch.object(legacy, "SUPABASE_URL", s.url),
            patch.object(legacy, "SUPABASE_INTERNAL_RPC_SECRET", "secret"),
            patch.object(legacy, "telegram_recipients", lambda: [("aa", "1"), ("bb", "2")]),
            patch.object(legacy, "send_telegram_checked", self._fake_send),
        ]
        for p in self.patches:
            p.start()
        self.now = datetime.now(UTC)
        s.tables["scheduler_status"] = [
            {"job_name": j, "last_run_at": s._norm_ts(iso(self.now - timedelta(minutes=5))), "last_result": "OK"}
            for j in JOBS
        ]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.patches = []

    async def _fake_send(self, text, only_hashes=None):
        self.sent.append((text, None if only_hashes is None else set(only_hashes)))
        if self.send_results:
            return dict(self.send_results.pop(0))
        targets = ["aa", "bb"] if only_hashes is None else [h for h in ("aa", "bb") if h in only_hashes]
        return {h: "ok" for h in targets}

    # --- helpers
    def fail_jobs(self, names, result="FAIL: boom"):
        for row in self.srv.tables["scheduler_status"]:
            if row["job_name"] in names:
                row["last_result"] = result

    def events(self, event_type="PATROL_ALERT"):
        return [e for e in self.srv.tables.get("magi_task_events", []) if e.get("event_type") == event_type]

    def ledger(self):
        return {r["job_name"]: r for r in self.srv.tables["scheduler_status"]
                if r["job_name"].startswith(("patrol_alert::", "patrol_meta::"))}

    def ledger_value(self, key):
        return pal.parse_value(self.ledger()[key]["last_result"])

    def alert_sends(self):
        return [s for s in self.sent if s[0].startswith("🚨")]

    async def patrol(self):
        return await legacy.run_geomnuri_patrol_once()

    def heartbeat(self):
        rows = [r for r in self.srv.tables["scheduler_status"] if r["job_name"] == "geomnuri_patrol"]
        return rows[0]["last_result"] if rows else None


class PatrolIntegrationTests(Base):
    async def test_healthy_run_is_silent(self):
        res = await self.patrol()
        self.assertEqual(res["alerts"], [])
        self.assertEqual((self.events(), self.sent), ([], []))
        self.assertEqual(self.heartbeat(), "alerts=0 findings=0")

    async def test_scheduler_failures_alert_once_then_stay_quiet(self):
        names = JOBS[:4]
        self.fail_jobs(names)
        await self.patrol()
        evs = self.events()
        self.assertEqual(len(evs), 4)
        for e in evs:
            self.assertEqual((e["actor"], e["task_id"]), ("GEOMNURI_PATROL", None))
            self.assertTrue(e["detail"].startswith("[CRITICAL]"))
        self.assertEqual(len(self.alert_sends()), 4)
        for n in names:
            self.assertEqual(self.ledger_value(f"patrol_alert::scheduler_fail::{n}").state, pal.TG_ACKED)
        await self.patrol()
        await self.patrol()
        self.assertEqual(len(self.events()), 4)
        self.assertEqual(len(self.alert_sends()), 4)

    async def test_burst_is_capped_at_eight_per_run_and_finishes_next_run(self):
        names = JOBS[:12]
        self.fail_jobs(names)
        await self.patrol()
        self.assertEqual(len(self.events()), 8)
        self.assertIn("deferred=4", self.heartbeat())
        await self.patrol()
        self.assertEqual(len(self.events()), 12)
        self.assertEqual(len({e["detail"] for e in self.events()}), 12)
        await self.patrol()
        self.assertEqual(len(self.events()), 12)

    async def test_scheduler_alert_cooldown_is_six_hours(self):
        job = JOBS[0]
        self.fail_jobs([job])
        key = f"patrol_alert::scheduler_fail::{job}"
        self.srv.tables["scheduler_status"].append(
            {"job_name": key, "last_run_at": self.srv._norm_ts(iso(self.now - timedelta(hours=2))),
             "last_result": "[CRITICAL] older"})
        await self.patrol()
        self.assertEqual(self.events(), [])
        self.ledger()[key]["last_run_at"] = self.srv._norm_ts(iso(self.now - timedelta(hours=7)))
        await self.patrol()
        self.assertEqual(len(self.events()), 1)

    async def test_detection_read_failure_is_a_self_failure_alert_not_a_clean_bill(self):
        self.srv.fail_query_contains = {"scheduler_status": "=in."}
        res = await self.patrol()
        keys = [f["key"] for f in res["findings"]]
        self.assertEqual(keys, ["patrol_internal_scheduler_check"])
        evs = self.events()
        self.assertEqual(len(evs), 1)
        self.assertIn("점검 자체 실패", evs[0]["detail"])
        self.assertNotIn("기록 없음", evs[0]["detail"])

    async def test_other_detection_failures_are_self_failures(self):
        self.srv.fail_query_contains = {"magi_tasks": "status", "call_image_ingestions": "limit"}
        self.srv.rpc = {"get_kakao_daily_completeness_v1": {"not": "a list"}}
        res = await self.patrol()
        keys = {f["key"] for f in res["findings"]}
        self.assertEqual(keys, {"patrol_internal_task_check", "patrol_internal_ingestion_check",
                                "patrol_internal_kakao_completeness_check",
                                "patrol_internal_hold_metadata_check"})

    async def test_ledger_unreadable_blocks_alerts_with_one_notice(self):
        self.fail_jobs(JOBS[:3])
        self.srv.fail_query_contains = {"scheduler_status": "like."}
        await self.patrol()
        await self.patrol()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.alert_sends(), [])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("ledger_unreadable", self.heartbeat())

    async def test_ledger_larger_than_one_page_is_fully_read(self):
        self.srv.max_rows = 1000
        job = JOBS[0]
        self.fail_jobs([job])
        fresh = self.srv._norm_ts(iso(self.now - timedelta(minutes=1)))
        self.srv.tables["scheduler_status"] += [
            {"job_name": f"patrol_alert::fill-{i:04d}", "last_run_at": fresh, "last_result": "[WARN] x"}
            for i in range(1200)]
        self.srv.tables["scheduler_status"].append(
            {"job_name": f"patrol_alert::scheduler_fail::{job}", "last_run_at": fresh, "last_result": "[CRITICAL] x"})
        await self.patrol()
        self.assertEqual(self.events(), [])           # row sorted after the first 1000 was seen

    async def test_expired_row_beyond_first_page_still_re_alerts(self):
        # a truncated ledger read would call this row "new", lose the insert on the unique key and
        # never alert again
        self.srv.max_rows = 1000
        job = JOBS[0]
        self.fail_jobs([job])
        fresh = self.srv._norm_ts(iso(self.now - timedelta(minutes=1)))
        self.srv.tables["scheduler_status"] += [
            {"job_name": f"patrol_alert::fill-{i:04d}", "last_run_at": fresh, "last_result": "[WARN] x"}
            for i in range(1200)]
        self.srv.tables["scheduler_status"].append(
            {"job_name": f"patrol_alert::scheduler_fail::{job}",
             "last_run_at": self.srv._norm_ts(iso(self.now - timedelta(days=3))), "last_result": "[CRITICAL] old"})
        await self.patrol()
        self.assertEqual(len(self.events()), 1)

    async def test_untrustworthy_ledger_pages_fail_closed(self):
        self.srv.max_rows = 1000
        self.fail_jobs(JOBS[:2])
        fresh = self.srv._norm_ts(iso(self.now - timedelta(minutes=1)))
        self.srv.tables["scheduler_status"] += [
            {"job_name": f"patrol_alert::fill-{i:04d}", "last_run_at": fresh, "last_result": "[WARN] x"}
            for i in range(1100)]
        quirks = ["missing", "garbled", "total_changes", "total_understated", "no_page_progress", "overlap",
                  "forged_range", "star_nonempty", "row_nondict", "key_missing", "key_none", "key_number",
                  "key_empty", "key_bool", "key_duplicate", "range_on_empty"]
        for quirk in quirks:
            with self.subTest(quirk=quirk):
                self.srv._range_calls = 0
                self.srv.range_quirk = quirk
                pal.reset_local_state()
                self.sent.clear()
                res = await self.patrol()
                self.assertEqual(self.events(), [], quirk)
                self.assertEqual(self.alert_sends(), [], quirk)
                self.assertIn("ledger_unreadable", self.heartbeat(), quirk)

    async def test_database_collation_order_is_not_compared_in_python(self):
        self.srv.range_quirk = "collation"
        fresh = self.srv._norm_ts(iso(self.now - timedelta(minutes=1)))
        self.srv.tables["scheduler_status"] += [
            {"job_name": "patrol_alert::B-upper", "last_run_at": fresh, "last_result": "[WARN] x"},
            {"job_name": "patrol_alert::a-lower", "last_run_at": fresh, "last_result": "[WARN] x"},
            {"job_name": "patrol_alert::C-upper", "last_run_at": fresh, "last_result": "[WARN] x"}]
        self.fail_jobs([JOBS[0]])
        await self.patrol()
        self.assertEqual(len(self.events()), 1)

    async def test_duplicate_ledger_row_fails_closed(self):
        row = {"job_name": "patrol_alert::dup", "last_run_at": self.srv._norm_ts(iso(self.now)), "last_result": "x"}
        self.srv.tables["scheduler_status"] += [dict(row), dict(row)]
        self.fail_jobs([JOBS[0]])
        await self.patrol()
        self.assertEqual(self.events(), [])

    async def test_two_simultaneous_runs_alert_once(self):
        self.fail_jobs(JOBS[:3])
        await asyncio.gather(self.patrol(), self.patrol(), self.patrol())
        self.assertEqual(len(self.events()), 3)
        self.assertEqual(len(self.alert_sends()), 3)

    async def test_three_simultaneous_runs_claim_an_expired_alert_once(self):
        names = JOBS[:2]
        self.fail_jobs(names)
        for n in names:
            self.srv.tables["scheduler_status"].append(
                {"job_name": f"patrol_alert::scheduler_fail::{n}",
                 "last_run_at": self.srv._norm_ts(iso(self.now - timedelta(days=3))), "last_result": "[CRITICAL] old"})
        await asyncio.gather(self.patrol(), self.patrol(), self.patrol())
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(len(self.alert_sends()), 2)

    async def test_claim_committed_but_response_lost_continues_once(self):
        for mode in ("commit_503", "commit_reset", "commit_empty", "commit_badjson", "commit_dict", "commit_two_rows"):
            with self.subTest(mode=mode):
                self._reset()
                self.fail_jobs([JOBS[0]])
                self.srv.write_faults = [{"method": "POST", "table": "scheduler_status", "contains": "patrol_alert",
                                          "mode": mode, "times": 1}]
                await self.patrol()
                self.assertEqual(len(self.events()), 1, mode)
                self.assertEqual(len(self.alert_sends()), 1, mode)

    async def test_claim_dropped_before_commit_is_retried_once(self):
        self.fail_jobs([JOBS[0]])
        self.srv.write_faults = [{"method": "POST", "table": "scheduler_status", "contains": "patrol_alert",
                                  "mode": "drop_503", "times": 1}]
        await self.patrol()
        self.assertEqual(len(self.events()), 1)

    async def test_claim_that_commits_late_cannot_duplicate(self):
        self.fail_jobs([JOBS[0]])
        self.srv.write_faults = [{"method": "POST", "table": "scheduler_status", "contains": "patrol_alert",
                                  "mode": "late_commit", "delay": 0.3, "times": 1}]
        await self.patrol()
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.ledger_value(f"patrol_alert::scheduler_fail::{JOBS[0]}").state, pal.TG_ACKED)

    async def test_cas_that_commits_late_cannot_duplicate(self):
        job = JOBS[0]
        self.fail_jobs([job])
        key = f"patrol_alert::scheduler_fail::{job}"
        self.srv.tables["scheduler_status"].append(
            {"job_name": key, "last_run_at": self.srv._norm_ts(iso(self.now - timedelta(days=2))),
             "last_result": "[CRITICAL] old"})
        self.srv.write_faults = [{"method": "PATCH", "table": "scheduler_status", "mode": "late_commit",
                                  "delay": 0.3, "times": 1}]
        await self.patrol()
        await asyncio.sleep(0.6)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.ledger_value(key).state, pal.TG_ACKED)

    async def test_event_insert_with_lost_response_is_never_repeated(self):
        for mode in ("commit_503", "commit_reset", "drop_503", "commit_empty", "commit_two_rows", "commit_dict"):
            with self.subTest(mode=mode):
                self._reset()
                self.fail_jobs([JOBS[0]])
                self.srv.write_faults = [{"method": "POST", "table": "magi_task_events", "mode": mode, "times": 1}]
                await self.patrol()
                key = f"patrol_alert::scheduler_fail::{JOBS[0]}"
                self.assertEqual(self.ledger_value(key).state, pal.UNKNOWN, mode)
                self.assertLessEqual(len(self.events()), 1, mode)
                self.assertEqual(self.alert_sends(), [], mode)
                self.assertEqual(len(self.sent), 1, mode)           # one short notice
                for _ in range(3):
                    await self.patrol()
                self.assertLessEqual(len(self.events()), 1, mode)
                self.assertEqual(len(self.sent), 1, mode)

    async def test_task_link_rule_against_real_lookup(self):
        t = self.srv.tables
        t["magi_tasks"] = [
            {"task_id": 5, "status": "IN_PROGRESS", "priority": "P0", "title": "live", "owner_agent": "x",
             "updated_at": iso(self.now - timedelta(hours=10))},
            {"task_id": 6, "status": "COMPLETED", "priority": "P2", "title": "done", "owner_agent": "x",
             "updated_at": iso(self.now)},
            {"task_id": 7, "status": "CANCELLED", "priority": "P2", "title": "gone", "owner_agent": "x",
             "updated_at": iso(self.now)},
        ]
        t["magi_task_events"] = []
        for tid in (6, 7, 9):
            for i in range(8):
                t["magi_task_events"].append({"event_id": tid * 100 + i, "task_id": tid, "event_type": "X",
                                              "actor": "자동에이전트", "detail": "d", "domain": "OPS",
                                              "created_at": iso(self.now)})
        await self.patrol()
        by_task = {e["detail"].split(":")[0]: e["task_id"] for e in self.events()}
        stale = [e for e in self.events() if "장기점유" in e["detail"]]
        self.assertEqual([e["task_id"] for e in stale], [5])
        loops = [e for e in self.events() if "반복루프" in e["detail"]]
        self.assertEqual(len(loops), 3)
        self.assertEqual([e["task_id"] for e in loops], [None, None, None])
        self.assertNotIn(160, [e["task_id"] for e in self.events()])

    async def test_kakao_key_is_normalised_and_counts_do_not_realert(self):
        row = {"completeness_status": "MISMATCH", "page_date": "2026-10-01", "expected_count": 5,
               "observed_count": 4, "missing_count": 1, "extra_count": 0}
        self.srv.rpc = {"get_kakao_daily_completeness_v1": [row]}
        await self.patrol()
        self.assertEqual(len(self.events()), 1)
        self.assertIn("patrol_alert::kakao_completeness::2026-10-01", self.ledger())
        self.srv.rpc = {"get_kakao_daily_completeness_v1": [dict(row, observed_count=3, missing_count=2)]}
        await self.patrol()
        self.assertEqual(len(self.events()), 1)

    async def test_loop_error_goes_through_the_ledger(self):
        await legacy._patrol_report_loop_error(RuntimeError(""))
        await legacy._patrol_report_loop_error(RuntimeError(""))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("RuntimeError", self.sent[0][0])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.ledger_value("patrol_meta::loop_error").state, pal.TG_ACKED)

    async def test_loop_error_with_unreadable_ledger_is_local_and_limited(self):
        self.srv.fail_query_contains = {"scheduler_status": "like."}
        for _ in range(3):
            await legacy._patrol_report_loop_error(ValueError("x"))
        self.assertEqual(len(self.sent), 1)

    async def test_telegram_partial_failure_retries_only_the_failed_recipient(self):
        self.send_results = [{"aa": "ok", "bb": "fail"}]
        self.fail_jobs([JOBS[0]])
        await self.patrol()
        key = f"patrol_alert::scheduler_fail::{JOBS[0]}"
        self.assertEqual(self.ledger_value(key).state, pal.TG_FAILED)
        await self.patrol()
        self.assertEqual(self.sent[-1][1], {"bb"})
        self.assertEqual(self.ledger_value(key).state, pal.TG_ACKED)
        self.assertEqual(len(self.events()), 1)

    async def test_heartbeat_is_written_even_when_everything_is_blocked(self):
        self.srv.fail_query_contains = {"scheduler_status": "like."}
        self.fail_jobs([JOBS[0]])
        await self.patrol()
        self.assertTrue(self.heartbeat().startswith("alerts=0 findings=1"))

    def test_empty_exception_text_uses_class_name(self):
        self.assertEqual(legacy._patrol_exc_text(Exception()), "Exception")
        self.assertEqual(legacy._patrol_exc_text(KeyError("k")), "'k'")
        self.assertEqual(legacy._patrol_exc_text(ValueError("a" * 900)), "a" * 300)


class StrictWriteTests(Base):
    async def test_outcomes(self):
        t = self.srv.tables
        t["scheduler_status"].append({"job_name": "k", "last_run_at": "2026-01-01T00:00:00.000000+00:00",
                                      "last_result": "a"})
        w = legacy.sb_write_strict
        out, rows = await w("PATCH", "scheduler_status", json_body={"last_result": "b"},
                            params={"job_name": "eq.k", "last_run_at": "eq.2026-01-01T00:00:00.000000+00:00"})
        self.assertEqual((out, len(rows)), ("WIN", 1))
        out, rows = await w("PATCH", "scheduler_status", json_body={"last_result": "c"},
                            params={"job_name": "eq.k", "last_run_at": "eq.2000-01-01T00:00:00.000000+00:00"})
        self.assertEqual((out, rows), ("LOSE", []))
        out, rows = await w("PATCH", "scheduler_status", json_body={"last_result": "c"},
                            params={"job_name": "eq.nope", "last_run_at": "is.null"})
        self.assertEqual(out, "LOSE")
        out, _ = await w("POST", "scheduler_status", json_body={"job_name": "k", "last_result": "z"},
                         params={"on_conflict": "job_name"}, extra_prefer="resolution=ignore-duplicates")
        self.assertEqual(out, "LOSE")
        self.assertEqual([r["last_result"] for r in t["scheduler_status"] if r["job_name"] == "k"], ["b"])
        out, rows = await w("POST", "scheduler_status", json_body={"job_name": "n", "last_result": "z"},
                            params={"on_conflict": "job_name"}, extra_prefer="resolution=ignore-duplicates")
        self.assertEqual((out, len(rows)), ("WIN", 1))

    async def test_everything_else_is_ambiguous(self):
        w = legacy.sb_write_strict
        for mode in ("commit_503", "commit_reset", "drop_503", "drop_reset", "commit_empty", "commit_badjson",
                     "commit_dict", "commit_two_rows"):
            with self.subTest(mode=mode):
                self.srv.write_faults = [{"method": "POST", "table": "scheduler_status", "mode": mode, "times": 1}]
                out, rows = await w("POST", "scheduler_status", json_body={"job_name": f"m-{mode}", "last_result": "z"})
                self.assertEqual((out, rows), ("AMBIGUOUS", None))
        with patch.object(legacy, "SUPABASE_URL", "http://127.0.0.1:1"):
            out, _ = await w("POST", "scheduler_status", json_body={"job_name": "x"}, timeout=1.0)
            self.assertEqual(out, "AMBIGUOUS")

    async def test_rpc_strict(self):
        self.srv.rpc = {"ok": [{"a": 1}], "bad": {"x": 1}}
        self.assertEqual(await legacy.sb_rpc_strict("rpc/ok", {}), [{"a": 1}])
        with self.assertRaises(legacy.SupabaseReadError):
            await legacy.sb_rpc_strict("rpc/bad", {})
        self.srv.fail_mode = "503"
        with self.assertRaises(legacy.SupabaseReadError):
            await legacy.sb_rpc_strict("rpc/ok", {})


class KeyedReaderTests(Base):
    def seed(self, n):
        self.srv.tables["scheduler_status"] = [
            {"job_name": f"patrol_alert::j{i:05d}", "last_run_at": None, "last_result": "x"} for i in range(n)]

    async def read(self):
        return await legacy.sb_get_all_strict(
            "scheduler_status", {"job_name": "like.patrol_alert::*"}, key_col="job_name", key_type=str)

    async def test_row_counts(self):
        self.srv.max_rows = 1000
        for n in (0, 1, 999, 1000, 1001, 2500):
            with self.subTest(n=n):
                self.seed(n)
                rows = await self.read()
                self.assertEqual(len(rows), n)
                self.assertEqual(len({r["job_name"] for r in rows}), n)

    async def test_bad_keys_and_headers_raise(self):
        self.srv.max_rows = 1000
        self.seed(1500)
        for quirk in ("key_missing", "key_none", "key_number", "key_empty", "key_bool", "key_duplicate",
                      "missing", "garbled", "total_changes", "total_understated", "no_page_progress", "overlap",
                      "forged_range", "star_nonempty", "row_nondict", "range_on_empty"):
            with self.subTest(quirk=quirk):
                self.srv._range_calls = 0
                self.srv.range_quirk = quirk
                with self.assertRaises(legacy.SupabaseReadError):
                    await self.read()

    async def test_server_collation_is_accepted(self):
        self.srv.range_quirk = "collation"
        self.srv.tables["scheduler_status"] = [
            {"job_name": n, "last_run_at": None, "last_result": "x"}
            for n in ("patrol_alert::Zed", "patrol_alert::alpha", "patrol_alert::Beta")]
        self.assertEqual(len(await self.read()), 3)

    async def test_raw_calls_integer_path_unchanged(self):
        self.srv.tables["raw_calls"] = [{"id": i} for i in (3, 1, 2)]
        rows = await legacy.sb_get_all_strict("raw_calls", {})
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])
        self.srv.range_quirk = "ids_reversed"
        with self.assertRaises(legacy.SupabaseReadError):
            await legacy.sb_get_all_strict("raw_calls", {})


class TelegramClassificationTests(unittest.IsolatedAsyncioTestCase):
    def test_classification_table(self):
        import httpx
        from telegram import error as te

        def chained(exc, cause):
            exc.__cause__ = cause
            return exc

        cases = [
            (te.BadRequest("chat not found"), "fail"),
            (te.Forbidden("blocked"), "fail"),
            (te.InvalidToken(), "fail"),
            (te.ChatMigrated(5), "fail"),
            (te.RetryAfter(3), "fail"),
            (chained(te.NetworkError("x"), httpx.ConnectError("refused")), "fail"),
            (chained(te.TimedOut("x"), httpx.ConnectTimeout("t")), "fail"),
            (chained(te.TimedOut("x"), httpx.PoolTimeout("t")), "fail"),
            (chained(te.TimedOut("x"), httpx.ReadTimeout("t")), "amb"),
            (chained(te.TimedOut("x"), httpx.WriteTimeout("t")), "amb"),
            (chained(te.NetworkError("x"), httpx.RemoteProtocolError("p")), "amb"),
            (te.NetworkError("no cause"), "amb"),
            (te.TelegramError("generic"), "amb"),
            (RuntimeError("anything"), "amb"),
        ]
        for exc, want in cases:
            with self.subTest(exc=repr(exc)):
                self.assertEqual(legacy.classify_telegram_error(exc), want)

    async def test_send_checked_per_recipient_and_only(self):
        from telegram import error as te

        calls = []

        class FakeBot:
            def __init__(self, token):
                pass

            async def send_message(self, chat_id, text):
                calls.append(chat_id)
                if chat_id == "2":
                    raise te.Forbidden("blocked")

        envs = {"ALLOWED_CHAT_ID": "1", "ALLOWED_CHAT_ID2": "2"}
        with patch.dict(os.environ, envs), patch("telegram.Bot", FakeBot):
            res = await legacy.send_telegram_checked("hi")
            h1, h2 = (h for h, _ in legacy.telegram_recipients())
            self.assertEqual(res, {h1: "ok", h2: "fail"})
            calls.clear()
            res = await legacy.send_telegram_checked("hi", {h2})
            self.assertEqual((res, calls), ({h2: "fail"}, ["2"]))
        with patch.dict(os.environ, {"ALLOWED_CHAT_ID": "", "ALLOWED_CHAT_ID2": ""}):
            self.assertEqual(await legacy.send_telegram_checked("hi"), {})


if __name__ == "__main__":
    unittest.main()
