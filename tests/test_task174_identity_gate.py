import unittest

from raw_call_identity_gate import partition_raw_call_payloads, persist_raw_call_batch


def row(**kw):
    base = {
        "id": 1,
        "날짜": "2026-10-01",
        "배차시각": "20:00",
        "하차시각": "20:10",
        "출발지": "대구 수성구 A",
        "도착지": "대구 중구 B",
        "요금": 7000,
        "콜유형": "카카오T",
        "raw_row_type": "trip",
        "source_id": "old-source",
        "data_source": "drive_ocr_layout_v1",
    }
    base.update(kw)
    return base


class Task174IdentityGateTests(unittest.TestCase):
    def test_strong_full_interval_is_duplicate_skipped(self):
        existing = [row()]
        candidate = row(id=None, source_id="new-copy", data_source="drive_ocr_tesseract")
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(len(result["novel"]), 0)
        self.assertEqual(len(result["weak"]), 0)
        self.assertEqual(result["duplicate_skipped"][0]["hits"][0]["strength"], "STRONG_FULL_INTERVAL")

    def test_strong_partial_address_is_duplicate_skipped(self):
        existing = [row(하차시각=None)]
        candidate = row(id=None, 하차시각="20:11", source_id="new-copy")
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(len(result["novel"]), 0)
        self.assertEqual(result["duplicate_skipped"][0]["hits"][0]["strength"], "STRONG_PARTIAL_ADDRESS")

    def test_time_only_match_is_quarantined(self):
        existing = [row(하차시각=None, 출발지=None, 도착지=None)]
        candidate = row(
            id=None,
            하차시각="20:12",
            출발지="다른 출발지",
            도착지="다른 도착지",
            source_id="new-copy",
        )
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(len(result["novel"]), 0)
        self.assertEqual(len(result["duplicate_skipped"]), 0)
        self.assertEqual(result["weak"][0]["hits"][0]["strength"], "WEAK_TIME_ONLY")

    def test_uber_null_fare_time_overlap_is_quarantined(self):
        # Exact production Golden shape:
        # existing raw id=1557 had only date/start/platform, fare=NULL.
        # Replaying the same source with the improved parser recovered fare and
        # addresses. The replay must still be WEAK_TIME_ONLY, never novel.
        existing = [row(
            날짜="2026-09-30",
            배차시각="21:05",
            하차시각=None,
            출발지=None,
            도착지=None,
            콜유형="우버",
            요금=None,
            raw_row_type="unclassified",
        )]
        candidate = row(
            id=None,
            날짜="2026-09-30",
            배차시각="21:05",
            하차시각=None,
            출발지="대구광역시 동구 신천동",
            도착지="경상북도 칠곡군 지천면 송정리",
            콜유형="우버",
            요금=27700,
            source_id="new-copy",
            data_source="drive_ocr_tesseract",
        )
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(len(result["novel"]), 0)
        self.assertEqual(len(result["duplicate_skipped"]), 0)
        self.assertEqual(len(result["weak"]), 1)
        self.assertEqual(result["weak"][0]["hits"][0]["strength"], "WEAK_TIME_ONLY")

    def test_missing_fare_never_becomes_strong(self):
        existing = [row(요금=None)]
        candidate = row(id=None, source_id="new-copy")
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(len(result["novel"]), 0)
        self.assertEqual(len(result["duplicate_skipped"]), 0)
        self.assertEqual(len(result["weak"]), 1)
        self.assertEqual(result["weak"][0]["hits"][0]["strength"], "WEAK_TIME_ONLY")

    def test_known_different_fares_are_not_identity_match(self):
        existing = [row(요금=7000)]
        candidate = row(id=None, 요금=8000, source_id="new-copy")
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(result["novel"], [candidate])
        self.assertEqual(result["duplicate_skipped"], [])
        self.assertEqual(result["weak"], [])

    def test_meter_receipt_style_time_only_identity_is_quarantined(self):
        existing = [row(하차시각=None, 출발지=None, 도착지=None)]
        candidate = {
            "날짜": "2026-10-01",
            "배차시각": "20:00",
            "하차시각": None,
            "출발지": None,
            "도착지": None,
            "요금": 7000,
            "콜유형": "카카오T",
            "source_id": "new-copy",
            "data_source": "drive_ocr_tesseract",
        }
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(len(result["novel"]), 0)
        self.assertEqual(len(result["weak"]), 1)

    def test_unrelated_trip_is_novel(self):
        existing = [row()]
        candidate = row(
            id=None,
            배차시각="21:00",
            하차시각="21:15",
            요금=11000,
            source_id="new-copy",
        )
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(result["novel"], [candidate])
        self.assertEqual(result["duplicate_skipped"], [])
        self.assertEqual(result["weak"], [])

    def test_same_source_rows_are_ignored(self):
        existing = [row(source_id="retry-source")]
        candidate = row(id=None, source_id="retry-source")
        result = partition_raw_call_payloads(existing, [candidate], "retry-source")
        self.assertEqual(result["novel"], [candidate])

    def test_daily_total_is_not_trip_identity(self):
        existing = [row(raw_row_type="daily_total")]
        candidate = row(id=None, source_id="new-copy")
        result = partition_raw_call_payloads(existing, [candidate], "new-copy")
        self.assertEqual(result["novel"], [candidate])


if __name__ == "__main__":
    unittest.main()
