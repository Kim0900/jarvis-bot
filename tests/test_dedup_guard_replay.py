"""Dedup-guard replay on the real Drive ingestion write path (pre-condition B6 for resuming uploads).

Real code under test: ``bot_v5_legacy._persist_raw_call_batch`` -> ``raw_call_identity_gate``
-> real ``sb_h`` HTTP stack.  Only the database is replaced by the in-process PostgREST
stand-in.  Seed rows are copied (fields only; Drive file ids replaced) from production
raw_calls for business date 2026-10-09 (read-only query, 2026-10-13).

Questions answered, one test each:
  D1 same trips re-uploaded under a NEW source id            -> nothing inserted
  D2 overlapping screenshot (some old, some new trips)       -> only the new trips inserted
  D3 lookup degraded (503 / reset / empty 200 / garbled 200) -> fail closed, nothing inserted
  D4 two sources carrying the same trips processed at once   -> exactly one copy stored
  D5 asymmetric NULL fare replay                              -> quarantined, nothing inserted
  D6 the same trip twice inside one batch                     -> stored once
  D7 genuinely different trips                                -> all inserted (guard not over-blocking)
"""
import asyncio
import os
import sys
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
from fake_postgrest import FakePostgREST  # noqa: E402

DAY = "2026-10-09"
# (배차, 하차, 출발, 도착, 요금, 콜유형, data_source, source)
KAKAO = [
    ("00:32", "00:42", "• 대구 수성구 두산동", "• 대구 동구 신천 4 동", 10700),
    ("01:29", "01:35", "• 대구 동구 신암 3 동", "• 대구 수성구 만촌 1 동", 7900),
    ("01:45", "01:55", "• 대구 동구 신암 1 동", "• 대구 중구 남산 2 동", 9500),
    ("02:37", "03:08", "• 대구 동구 효목 1 동", "• 대구 중구 성내 1 동", 14400),
    ("03:35", "03:40", "• 대구 수성구 범어 3 동", "• 대구 동구 신암 4 동", 6800),
    ("19:01", "19:11", "• 대구 동구 신암 2 동", "• 대구 북구 칠성동", 6200),
    ("19:51", "20:00", "• 대구 북구 무태조야동", "• 대구 북구 산격 2 동", 6700),
    ("20:34", "20:44", "• 대구 수성구 범어 3 동", "• 대구 수성구 두산동", 7300),
]
NEW_TRIPS = [
    ("23:50", "23:58", "• 대구 중구 삼덕동", "• 대구 동구 신천 1.2 동", 6100),
    ("23:59", "00:20", "• 대구 동구 신천 1.2 동", "• 대구 수성구 범어 1 동", 8800),
]


def payload(t, source_data="drive_ocr_layout_v1"):
    return {"날짜": DAY, "배차시각": t[0], "하차시각": t[1], "출발지": t[2], "도착지": t[3],
            "요금": t[4], "콜유형": "카카오T", "data_source": source_data}


def seed_rows(start_id=1000):
    rows = []
    for i, t in enumerate(KAKAO):
        rows.append({**payload(t), "id": start_id + i, "source_id": "SRC_OLD",
                     "verify_status": "unverified", "raw_row_type": "trip"})
    return rows


class DedupGuardReplay(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = FakePostgREST().start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def setUp(self):
        self.srv.tables.clear()
        self.srv.fail_mode = None
        self.srv.fail_tables = None
        self.srv.fail_methods = None
        self.srv.get_quirk = None
        self.srv.fail_body_contains = {}
        self.srv.requests.clear()
        self.srv.tables["raw_calls"] = seed_rows()
        self.p = patch.object(legacy, "SUPABASE_URL", self.srv.url)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def count(self):
        return len(self.srv.tables["raw_calls"])

    async def ingest(self, trips, source_id):
        return await legacy._persist_raw_call_batch([payload(t) for t in trips], source_id=source_id)

    # D1
    async def test_d1_same_trips_new_source_id_inserts_nothing(self):
        res = await self.ingest(KAKAO, "SRC_COPY")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["inserted_count"], 0)
        self.assertEqual(res["duplicate_skipped_count"], len(KAKAO))
        self.assertEqual(self.count(), len(KAKAO))

    # D2
    async def test_d2_overlap_inserts_only_new_trips(self):
        res = await self.ingest(KAKAO[:3] + NEW_TRIPS, "SRC_OVERLAP")
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["inserted_count"], res["duplicate_skipped_count"]), (len(NEW_TRIPS), 3))
        self.assertEqual(self.count(), len(KAKAO) + len(NEW_TRIPS))
        stored = {(r["배차시각"], r["요금"]) for r in self.srv.tables["raw_calls"]}
        for t in NEW_TRIPS:
            self.assertIn((t[0], t[4]), stored)

    # D3
    async def test_d3_degraded_lookup_fails_closed(self):
        cases = {
            "http503": dict(fail_mode="503"),
            "reset": dict(fail_mode="reset"),
            "empty200": dict(get_quirk="empty"),
            "garbled200": dict(get_quirk="badjson"),
            "dict200": dict(get_quirk="dict"),
        }
        for label, knobs in cases.items():
            with self.subTest(case=label):
                self.srv.tables["raw_calls"] = seed_rows()
                self.srv.fail_mode = self.srv.get_quirk = None
                self.srv.fail_tables = {"raw_calls"}
                self.srv.fail_methods = {"GET"}
                for k, v in knobs.items():
                    setattr(self.srv, k, v)
                with self.assertRaises(Exception):
                    await self.ingest(KAKAO + NEW_TRIPS, "SRC_DEGRADED")
                self.srv.fail_mode = self.srv.get_quirk = None
                self.assertEqual(self.count(), len(KAKAO), "nothing may be written when dedup state is unknown")

    # D4
    async def test_d4_concurrent_sources_same_trips_store_one_copy(self):
        self.srv.tables["raw_calls"] = []
        results = await asyncio.gather(
            self.ingest(NEW_TRIPS, "SRC_X"), self.ingest(NEW_TRIPS, "SRC_Y"))
        self.assertTrue(all(r["ok"] for r in results), results)
        self.assertEqual(self.count(), len(NEW_TRIPS))

    # D5
    async def test_d5_asymmetric_null_fare_is_quarantined(self):
        replay = dict(payload(KAKAO[0]))
        replay["요금"] = None
        res = await legacy._persist_raw_call_batch([replay], source_id="SRC_NULLFARE")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("quarantine"))
        self.assertEqual(self.count(), len(KAKAO))

    # D6
    async def test_d6_same_trip_twice_in_one_batch_stored_once(self):
        res = await self.ingest([NEW_TRIPS[0], NEW_TRIPS[0]], "SRC_TWICE")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.count(), len(KAKAO) + 1)

    # D7
    async def test_d7_different_trips_are_not_over_blocked(self):
        res = await self.ingest(NEW_TRIPS, "SRC_NEW")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["inserted_count"], len(NEW_TRIPS))
        self.assertEqual(res["duplicate_skipped_count"], 0)
        self.assertEqual(self.count(), len(KAKAO) + len(NEW_TRIPS))


if __name__ == "__main__":
    unittest.main()
