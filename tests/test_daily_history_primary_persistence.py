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

    async def test_existing_date_rows_quarantine(self):
        async def select_rows(params):
            if "source_id" in params:
                return []
            return [{"id": 2, "raw_row_type": "trip", "source_id": None}]

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
        self.assertEqual(result["error_code"], "LAYOUT_DATE_EXISTING_ROWS_QUARANTINE")

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
