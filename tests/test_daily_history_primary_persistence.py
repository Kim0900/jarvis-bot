import unittest

from daily_history_primary_adapter import persist_layout_primary


def parsed():
    return {
        "날짜": "2026-09-28",
        "표시건수": 1,
        "표시금액": 5000,
        "items": [{
            "탑승시각": "20:00",
            "하차시각": "20:10",
            "출발지": "출발 A",
            "도착지": "도착 A",
            "요금": 5000,
            "결제방식": "미확인",
        }],
    }


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rollback_count = 0

    async def rollback(self, source_id):
        self.rollback_count += 1
        return 0

    def calc(self, date_value, time_value):
        return {"platform_date": date_value, "business_date": date_value}

    def validate(self, payload):
        return True, ""

    async def test_duplicate_source_blocks_before_write(self):
        async def select_rows(params):
            return [{"id": 1}] if "source_id" in params else []

        async def no_write(_):
            raise AssertionError("write must not run")

        result = await persist_layout_primary(
            parsed(), "src",
            select_rows=select_rows,
            bulk_insert=no_write,
            mark_completed=no_write,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertEqual(result["error_code"], "LAYOUT_PRIMARY_SOURCE_ROWS_ALREADY_EXIST")

    async def test_same_date_unrelated_platform_does_not_block(self):
        async def select_rows(params):
            if "source_id" in params:
                return []
            return [
                {"id": 2, "raw_row_type": "trip", "콜유형": "우버",
                 "배차시각": "20:00", "하차시각": "20:10", "요금": 5000},
                {"id": 3, "raw_row_type": "unclassified", "콜유형": "미분류",
                 "배차시각": "20:00", "요금": 5000},
            ]

        async def bulk(_):
            return [{"id": 100}]

        marks = []
        async def mark(count):
            marks.append(count)
            return {"ok": True}

        result = await persist_layout_primary(
            parsed(), "src",
            select_rows=select_rows,
            bulk_insert=bulk,
            mark_completed=mark,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(marks, [1])

    async def test_same_platform_identity_overlap_quarantines(self):
        async def select_rows(params):
            if "source_id" in params:
                return []
            return [{
                "id": 4,
                "raw_row_type": "trip",
                "콜유형": "카카오T",
                "날짜": "2026-09-28",
                "배차시각": "20:10",
                "하차시각": None,
                "요금": 5000,
                "source_id": None,
            }]

        async def no_write(_):
            raise AssertionError("write must not run")

        result = await persist_layout_primary(
            parsed(), "src",
            select_rows=select_rows,
            bulk_insert=no_write,
            mark_completed=no_write,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertTrue(result["quarantine"])
        self.assertEqual(result["error_code"], "LAYOUT_KAKAO_IDENTITY_AMBIGUOUS")
        self.assertEqual(result["overlap_count"], 1)

    async def test_strong_cross_source_duplicate_is_covered_not_reinserted(self):
        async def select_rows(params):
            if "source_id" in params:
                return []
            return [{
                "id": 77,
                "raw_row_type": "trip",
                "콜유형": "카카오T",
                "날짜": "2026-09-28",
                "배차시각": "20:00",
                "하차시각": "20:10",
                "출발지": "출발 A",
                "도착지": "도착 A",
                "요금": 5000,
                "source_id": "older-source",
            }]

        async def no_bulk(_):
            raise AssertionError("strong-covered payload must not be inserted")

        marks = []
        async def mark(count):
            marks.append(count)
            return {"ok": True}

        result = await persist_layout_primary(
            parsed(), "src-new",
            select_rows=select_rows,
            bulk_insert=no_bulk,
            mark_completed=mark,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["saved_count"], 0)
        self.assertEqual(result["covered_count"], 1)
        self.assertEqual(result["duplicate_skipped_count"], 1)
        self.assertEqual(marks, [0])
        self.assertEqual(self.rollback_count, 0)

    async def test_db_failure_rolls_back(self):
        async def select_rows(params):
            return []

        async def bulk(_):
            raise RuntimeError("db fail")

        async def mark(_):
            raise AssertionError("must not mark")

        result = await persist_layout_primary(
            parsed(), "src",
            select_rows=select_rows,
            bulk_insert=bulk,
            mark_completed=mark,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertEqual(result["error_code"], "LAYOUT_PRIMARY_PERSISTENCE_FAILED")
        self.assertEqual(self.rollback_count, 1)

    async def test_mark_failure_rolls_back(self):
        async def select_rows(params):
            return []

        async def bulk(_):
            return [{"id": 3}]

        async def mark(_):
            raise RuntimeError("mark fail")

        result = await persist_layout_primary(
            parsed(), "src",
            select_rows=select_rows,
            bulk_insert=bulk,
            mark_completed=mark,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertEqual(result["error_code"], "LAYOUT_PRIMARY_PERSISTENCE_FAILED")
        self.assertEqual(self.rollback_count, 1)

    async def test_primary_rows_persist_trip_contract(self):
        async def select_rows(params):
            return []

        captured = []
        async def bulk(rows):
            captured.extend(rows)
            return [{"id": 200 + i} for i, _ in enumerate(rows)]

        async def mark(_):
            return {"ok": True}

        result = await persist_layout_primary(
            parsed(), "src-trip-contract",
            select_rows=select_rows,
            bulk_insert=bulk,
            mark_completed=mark,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["raw_row_type"], "trip")
        self.assertEqual(captured[0]["data_source"], "drive_ocr_layout_v1")

    async def test_rollover_item_date_is_persisted_and_both_dates_are_scanned(self):
        rollover = {
            "날짜": "2026-10-05",
            "표시건수": 2,
            "표시금액": 21700,
            "items": [
                {
                    "날짜": "2026-10-06",
                    "탑승시각": "00:02",
                    "하차시각": "00:13",
                    "출발지": "출발 A",
                    "도착지": "도착 A",
                    "요금": 12900,
                    "결제방식": "미확인",
                },
                {
                    "날짜": "2026-10-05",
                    "탑승시각": "23:50",
                    "하차시각": "23:59",
                    "출발지": "출발 B",
                    "도착지": "도착 B",
                    "요금": 8800,
                    "결제방식": "미확인",
                },
            ],
        }
        selected_dates = []
        async def select_rows(params):
            if "source_id" in params:
                return []
            selected_dates.append(params["날짜"])
            return []

        captured = []
        async def bulk(rows):
            captured.extend(rows)
            return [{"id": 500 + i} for i, _ in enumerate(rows)]

        async def mark(_):
            return {"ok": True}

        result = await persist_layout_primary(
            rollover, "src-rollover",
            select_rows=select_rows,
            bulk_insert=bulk,
            mark_completed=mark,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            selected_dates,
            ["eq.2026-10-05", "eq.2026-10-06"],
        )
        self.assertEqual(
            [row["날짜"] for row in captured],
            ["2026-10-06", "2026-10-05"],
        )

    async def test_adapter_date_error_fails_before_write(self):
        bad = parsed()
        bad["error_code"] = "LAYOUT_CARD_DATE_OUT_OF_RANGE"

        async def no_select(_):
            raise AssertionError("select must not run")

        async def no_write(_):
            raise AssertionError("write must not run")

        result = await persist_layout_primary(
            bad, "src-bad-date",
            select_rows=no_select,
            bulk_insert=no_write,
            mark_completed=no_write,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertEqual(result["error_code"], "LAYOUT_CARD_DATE_OUT_OF_RANGE")

    async def test_success_marks_completed_without_rollback(self):
        async def select_rows(params):
            return []

        async def bulk(_):
            return [{"id": 4}]

        marks = []
        async def mark(count):
            marks.append(count)
            return {"ok": True}

        result = await persist_layout_primary(
            parsed(), "src",
            select_rows=select_rows,
            bulk_insert=bulk,
            mark_completed=mark,
            rollback_source_rows=self.rollback,
            calc_service_date=self.calc,
            validate_call_payload=self.validate,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(marks, [1])
        self.assertEqual(self.rollback_count, 0)


if __name__ == "__main__":
    unittest.main()
