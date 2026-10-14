"""Task #206 hardening of the existing dedup guards (CASSANDRA 5126/5252 design PASS).

Real code under test: ``bot_v5_legacy._persist_raw_call_batch`` / ``sb_get_all_strict``,
``raw_call_identity_gate``, ``daily_history_primary_adapter.persist_layout_primary`` and
``task164_primary_patch.strict_select_raw_calls``.  Only the database is replaced by the
in-process PostgREST stand-in (which emulates ``db-max-rows`` paging and Content-Range).

Covered: strict+complete lookup (501/1001/2500 rows, 200/206, missing/garbled header,
changing total, empty page, outage), source_id required, in-batch STRONG duplicate ->
quarantine with zero writes (gate and layout-primary), WEAK in-batch pairs NOT newly
blocked, single-process write lock (same loop, two threads, timeout, release on error),
layout-primary lookups fail closed and see rows beyond the old 500-row cap.
"""
import asyncio
import os
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from unittest.mock import patch  # noqa: E402

import bot_v5_legacy as legacy  # noqa: E402
import raw_call_identity_gate as gate  # noqa: E402
import task164_primary_patch as primary_patch  # noqa: E402
from daily_history_primary_adapter import persist_layout_primary  # noqa: E402
from fake_postgrest import FakePostgREST  # noqa: E402

DAY = "2026-10-09"


def trip(start, end, fare, frm="• 대구 A 동", to="• 대구 B 동", **extra):
    row = {"날짜": DAY, "배차시각": start, "하차시각": end, "출발지": frm, "도착지": to,
           "요금": fare, "콜유형": "카카오T", "data_source": "drive_ocr_layout_v1"}
    row.update(extra)
    return row


def filler_rows(n, start_id=10000):
    """n unrelated existing rows on DAY (distinct fares/times so nothing matches)."""
    return [{"id": start_id + i, "날짜": DAY, "배차시각": f"{(i // 60) % 24:02d}:{i % 60:02d}",
             "하차시각": f"{(i // 60) % 24:02d}:{i % 60:02d}", "요금": 100000 + i, "콜유형": "우버",
             "source_id": "SRC_FILL", "raw_row_type": "trip"} for i in range(n)]


class Base(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakePostgREST().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        s = self.srv
        s.tables.clear()
        s.fail_mode = s.fail_tables = s.fail_methods = s.get_quirk = None
        s.fail_body_contains = {}
        s.max_rows = None
        s.range_quirk = None
        s._range_calls = 0
        s.requests.clear()
        s.tables["raw_calls"] = []
        self.p = patch.object(legacy, "SUPABASE_URL", s.url)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def count(self):
        return len(self.srv.tables["raw_calls"])

    def writes(self):
        return [r for r in self.srv.requests if r[0] in ("POST", "PATCH")]


class StrictPagedRead(Base):
    async def test_reads_all_rows_across_pages_with_db_max_rows_cap(self):
        for n in (0, 1, 501, 1000, 1001, 2500):
            with self.subTest(rows=n):
                self.srv.tables["raw_calls"] = filler_rows(n)
                self.srv.max_rows = 1000
                self.srv._range_calls = 0
                got = await legacy.sb_get_all_strict("raw_calls", {"날짜": f"eq.{DAY}"})
                self.assertEqual(len(got), n)
                self.assertEqual([r["id"] for r in got], sorted(r["id"] for r in got))

    async def test_server_cap_smaller_than_page_still_complete(self):
        self.srv.tables["raw_calls"] = filler_rows(130)
        self.srv.max_rows = 50  # server shortens every page (206) below our page size
        got = await legacy.sb_get_all_strict("raw_calls", {"날짜": f"eq.{DAY}"})
        self.assertEqual(len(got), 130)

    async def test_untrustworthy_answers_raise(self):
        cases = {
            "http503": dict(fail_mode="503"),
            "reset": dict(fail_mode="reset"),
            "empty200": dict(get_quirk="empty"),
            "garbled200": dict(get_quirk="badjson"),
            "dict200": dict(get_quirk="dict"),
            "header_missing": dict(range_quirk="missing"),
            "header_garbled": dict(range_quirk="garbled"),
            "total_changes": dict(range_quirk="total_changes"),
            "empty_page_before_total": dict(range_quirk="no_page_progress"),
            "more_rows_than_total": dict(range_quirk="total_understated"),
            "rows_shift_between_pages": dict(range_quirk="overlap"),
        }
        for label, knobs in cases.items():
            with self.subTest(case=label):
                self.setUp_case(knobs)
                with self.assertRaises(legacy.SupabaseReadError):
                    await legacy.sb_get_all_strict("raw_calls", {"날짜": f"eq.{DAY}"}, page_size=100)

    def setUp_case(self, knobs):
        s = self.srv
        s.fail_mode = s.fail_tables = s.fail_methods = s.get_quirk = s.range_quirk = None
        s._range_calls = 0
        s.max_rows = None
        s.tables["raw_calls"] = filler_rows(250)
        for k, v in knobs.items():
            setattr(s, k, v)
        if "fail_mode" in knobs:
            s.fail_tables = {"raw_calls"}
            s.fail_methods = {"GET"}

    async def test_too_many_pages_raises(self):
        self.srv.tables["raw_calls"] = filler_rows(50)
        with self.assertRaises(legacy.SupabaseReadError):
            await legacy.sb_get_all_strict("raw_calls", {"날짜": f"eq.{DAY}"}, page_size=10, max_pages=3)


class RepairPath(Base):
    async def test_same_source_probe_is_strict(self):
        parsed_repair = {"날짜": DAY, "items": [{"탑승시각": "10:00", "하차시각": "10:10", "요금": 5000,
                                              "출발지": "• 대구 A 동", "도착지": "• 대구 B 동", "결제방식": "미확인"}]}
        for quirk in ("empty", "badjson", "dict"):
            with self.subTest(quirk=quirk):
                self.srv.get_quirk = quirk
                with self.assertRaises(Exception):
                    await legacy._save_repaired_daily_history_batch(parsed_repair, "SRC_REPAIR")
                self.srv.get_quirk = None
                self.assertEqual(self.count(), 0)

    async def test_repair_with_healthy_reads_inserts(self):
        parsed_repair = {"날짜": DAY, "items": [{"탑승시각": "10:00", "하차시각": "10:10", "요금": 5000,
                                              "출발지": "• 대구 A 동", "도착지": "• 대구 B 동", "결제방식": "미확인"}]}
        res = await legacy._save_repaired_daily_history_batch(parsed_repair, "SRC_REPAIR")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.count(), 1)


class GatePath(Base):
    async def ingest(self, trips, source_id="SRC_NEW"):
        return await legacy._persist_raw_call_batch(trips, source_id=source_id)

    async def test_existing_trip_beyond_first_page_is_found(self):
        existing = {**trip("23:50", "23:58", 6100), "id": 99999, "source_id": "SRC_OLD",
                    "raw_row_type": "trip"}
        self.srv.tables["raw_calls"] = filler_rows(1500) + [existing]
        self.srv.max_rows = 1000
        res = await self.ingest([trip("23:50", "23:58", 6100)], "SRC_COPY")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["inserted_count"], 0)
        self.assertEqual(res["duplicate_skipped_count"], 1)
        self.assertEqual(self.count(), 1501)

    async def test_source_id_required_before_any_request(self):
        for sid in (None, ""):
            with self.subTest(source_id=sid):
                self.srv.requests.clear()
                with self.assertRaises(RuntimeError) as cm:
                    await self.ingest([trip("10:00", "10:10", 5000)], sid)
                self.assertIn("RAW_CALL_SOURCE_ID_REQUIRED", str(cm.exception))
                self.assertEqual(self.srv.requests, [])
                self.assertEqual(self.count(), 0)

    async def test_empty_batch_without_source_id_is_still_a_noop(self):
        res = await legacy._persist_raw_call_batch([], source_id=None)
        self.assertTrue(res["ok"])
        self.assertEqual(self.srv.requests, [])

    async def test_in_batch_strong_duplicate_quarantines_whole_batch(self):
        batch = [trip("10:00", "10:10", 5000), trip("11:00", "11:10", 6000),
                 trip("10:00", "10:10", 5000)]
        res = await self.ingest(batch, "SRC_TWICE")
        self.assertFalse(res["ok"])
        self.assertTrue(res["quarantine"])
        self.assertEqual(res["error_code"], "RAW_CALL_IN_BATCH_DUPLICATE")
        self.assertEqual(res["inserted_count"], 0)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.writes(), [])

    async def test_in_batch_partial_address_strong_duplicate_quarantines(self):
        a = trip(None, "10:10", 5000)
        b = trip(None, "10:10", 5000)
        res = await self.ingest([a, b], "SRC_PARTIAL")
        self.assertEqual(res["error_code"], "RAW_CALL_IN_BATCH_DUPLICATE")
        self.assertEqual(self.count(), 0)

    async def test_distinct_trips_and_weak_pairs_are_not_blocked(self):
        # back-to-back trips (drop-off 10:10 == next pick-up 10:10), different fares: NOT duplicates
        batch = [trip("10:00", "10:10", 5000), trip("10:10", "10:20", 6000),
                 trip("12:00", "12:10", 7000)]
        res = await self.ingest(batch, "SRC_DISTINCT")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["inserted_count"], 3)
        self.assertEqual(self.count(), 3)

    async def test_in_batch_weak_pair_is_not_newly_blocked(self):
        # one fare missing + a shared boundary minute is WEAK against each other; in-batch WEAK is ignored
        batch = [trip("10:00", "10:10", None), trip("10:10", "10:20", 6000)]
        res = await self.ingest(batch, "SRC_WEAKPAIR")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.count(), 2)

    async def test_lookup_failure_writes_nothing(self):
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"raw_calls"}
        self.srv.fail_methods = {"GET"}
        with self.assertRaises(Exception):
            await self.ingest([trip("10:00", "10:10", 5000)], "SRC_X")
        self.srv.fail_mode = None
        self.assertEqual(self.count(), 0)

    async def test_header_fault_writes_nothing(self):
        for quirk in ("missing", "garbled", "total_changes", "no_page_progress", "total_understated", "overlap"):
            with self.subTest(quirk=quirk):
                self.srv.tables["raw_calls"] = filler_rows(2300)
                self.srv.range_quirk = quirk
                self.srv._range_calls = 0
                before = self.count()
                with self.assertRaises(Exception):
                    await self.ingest([trip("10:00", "10:10", 5000)], "SRC_H")
                self.assertEqual(self.count(), before)


class WriteLock(Base):
    async def test_same_loop_concurrent_sources_same_trips_store_one_copy(self):
        batch = [trip("23:50", "23:58", 6100)]
        results = await asyncio.gather(
            legacy._persist_raw_call_batch([dict(batch[0])], source_id="SRC_X"),
            legacy._persist_raw_call_batch([dict(batch[0])], source_id="SRC_Y"))
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(self.count(), 1)

    def test_two_threads_each_with_own_event_loop_store_one_copy(self):
        # the production shape: each request does asyncio.run on its own thread
        self.srv.tables["raw_calls"] = []
        barrier = threading.Barrier(2)
        out = {}

        def worker(sid):
            barrier.wait()
            out[sid] = asyncio.run(legacy._persist_raw_call_batch(
                [trip("23:50", "23:58", 6100)], source_id=sid))

        threads = [threading.Thread(target=worker, args=(s,)) for s in ("SRC_T1", "SRC_T2")]
        with patch.object(legacy, "SUPABASE_URL", self.srv.url):
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(r["ok"] for r in out.values()), out)
        self.assertEqual(self.count(), 1)

    async def test_lock_timeout_fails_closed_without_writes(self):
        self.assertTrue(gate._WRITE_LOCK.acquire(blocking=False))
        try:
            with patch.object(gate, "WRITE_LOCK_TIMEOUT_SECONDS", 0.1):
                with self.assertRaises(RuntimeError) as cm:
                    await legacy._persist_raw_call_batch([trip("10:00", "10:10", 5000)], source_id="S")
            self.assertIn("RAW_CALL_WRITE_LOCK_TIMEOUT", str(cm.exception))
        finally:
            gate._WRITE_LOCK.release()
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.srv.requests, [])

    async def test_lock_released_after_error_and_after_success(self):
        self.srv.fail_mode = "503"
        self.srv.fail_tables = {"raw_calls"}
        self.srv.fail_methods = {"GET"}
        with self.assertRaises(Exception):
            await legacy._persist_raw_call_batch([trip("10:00", "10:10", 5000)], source_id="S1")
        self.assertFalse(gate._WRITE_LOCK.locked())
        self.srv.fail_mode = None
        res = await legacy._persist_raw_call_batch([trip("10:00", "10:10", 5000)], source_id="S2")
        self.assertTrue(res["ok"])
        self.assertFalse(gate._WRITE_LOCK.locked())


def parsed(items, expected=None, total=None):
    return {"날짜": DAY, "표시건수": expected if expected is not None else len(items),
            "표시금액": total if total is not None else sum(i["요금"] for i in items),
            "items": items}


def item(start, end, fare, frm="• 대구 A 동", to="• 대구 B 동"):
    return {"탑승시각": start, "하차시각": end, "요금": fare, "출발지": frm, "도착지": to,
            "결제방식": "미확인"}


class LayoutPrimaryPath(Base):
    async def persist(self, parsed_value, source_id="SRC_LAYOUT"):
        completed = []

        async def select_rows(params):
            return await primary_patch.strict_select_raw_calls(legacy, params)

        async def bulk_insert(rows):
            return await legacy.sb_h("POST", "raw_calls", json=rows)

        async def mark_completed(n):
            completed.append(n)

        async def rollback(_sid):
            return 0

        res = await persist_layout_primary(
            parsed_value, source_id,
            select_rows=select_rows, bulk_insert=bulk_insert, mark_completed=mark_completed,
            rollback_source_rows=rollback, calc_service_date=legacy.calc_service_date,
            validate_call_payload=legacy.validate_call_payload)
        res["_completed"] = completed
        return res

    async def test_new_trips_are_inserted(self):
        res = await self.persist(parsed([item("10:00", "10:10", 5000), item("11:00", "11:10", 6000)]))
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["saved_count"], res["duplicate_skipped_count"]), (2, 0))
        self.assertEqual(self.count(), 2)

    async def test_lookup_degradation_fails_closed_with_zero_writes(self):
        cases = {
            "http503": dict(fail_mode="503"),
            "reset": dict(fail_mode="reset"),
            "empty200": dict(get_quirk="empty"),
            "garbled200": dict(get_quirk="badjson"),
            "dict200": dict(get_quirk="dict"),
        }
        for label, knobs in cases.items():
            with self.subTest(case=label):
                s = self.srv
                s.fail_mode = s.fail_tables = s.fail_methods = s.get_quirk = None
                s.tables["raw_calls"] = []
                for k, v in knobs.items():
                    setattr(s, k, v)
                if "fail_mode" in knobs:
                    s.fail_tables = {"raw_calls"}
                    s.fail_methods = {"GET"}
                with self.assertRaises(Exception):
                    await self.persist(parsed([item("10:00", "10:10", 5000)]), "SRC_DEG")
                s.fail_mode = s.get_quirk = None
                self.assertEqual(self.count(), 0)

    async def test_existing_trip_beyond_old_500_row_cap_is_covered(self):
        existing = {**trip("23:50", "23:58", 6100), "id": 99999, "source_id": "SRC_OLD",
                    "raw_row_type": "trip", "data_source": "drive_ocr_tesseract"}
        self.srv.tables["raw_calls"] = filler_rows(700) + [existing]
        self.srv.max_rows = 1000
        res = await self.persist(parsed([item("23:50", "23:58", 6100)]), "SRC_OVERLAP")
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["saved_count"], res["duplicate_skipped_count"]), (0, 1))
        self.assertEqual(self.count(), 701)

    async def test_in_batch_strong_duplicate_quarantines_with_zero_writes(self):
        res = await self.persist(parsed([item("10:00", "10:10", 5000), item("10:00", "10:10", 5000)]))
        self.assertFalse(res["ok"])
        self.assertTrue(res["quarantine"])
        self.assertEqual(res["error_code"], "LAYOUT_PRIMARY_IN_BATCH_DUPLICATE")
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.writes(), [])
        self.assertEqual(res["_completed"], [])

    async def test_source_id_required(self):
        res = await persist_layout_primary(
            parsed([item("10:00", "10:10", 5000)]), None,
            select_rows=None, bulk_insert=None, mark_completed=None, rollback_source_rows=None,
            calc_service_date=legacy.calc_service_date, validate_call_payload=legacy.validate_call_payload)
        self.assertEqual(res["error_code"], "LAYOUT_PRIMARY_SOURCE_ID_REQUIRED")

    async def test_one_row_existence_probe_is_strict(self):
        self.srv.get_quirk = "empty"
        with self.assertRaises(legacy.SupabaseReadError):
            await primary_patch.strict_select_raw_calls(legacy, {"source_id": "eq.X", "limit": "1"})


class SingleThreadedServerAssumption(unittest.TestCase):
    def test_request_server_is_not_threaded(self):
        """The operating decision for D4 relies on the request server handling one
        request at a time.  If someone switches to a threaded server this test fails so the
        decision is revisited deliberately (CASSANDRA 5252)."""
        src = (ROOT / "bot_v5_legacy.py").read_text(encoding="utf-8")
        self.assertIn('server = HTTPServer(("0.0.0.0", PORT), HealthHandler)', src)
        self.assertNotIn("ThreadingHTTPServer", src)


if __name__ == "__main__":
    unittest.main()
