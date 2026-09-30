import unittest

from daily_history_primary_adapter import (
    ACTION_FAIL_CLOSED,
    ACTION_FALLBACK_LEGACY,
    ACTION_USE_LAYOUT,
    classify_layout_result,
    date_has_conflicting_rows,
    independent_header_disagreements,
    layout_to_daily_history,
)


def sample_layout():
    return {
        "ok": True,
        "status": "COMPLETE_LAYOUT_VALIDATED",
        "date": "2026-09-28",
        "header": {"expected_count": 2, "expected_sum": 12000},
        "cards": [
            {"start_time": "20:00", "end_time": "20:10",
             "address_lines": ["출발 A", "도착 A"], "fare": 5000, "payment": "미확인"},
            {"start_time": "20:20", "end_time": "20:30",
             "address_lines": ["출발 B", "도착 B"], "fare": 7000, "payment": "직접"},
        ],
    }


class PrimaryPolicyTests(unittest.TestCase):
    def test_fallback_only_for_transport_or_5xx(self):
        self.assertEqual(classify_layout_result(None, {}, True), ACTION_FALLBACK_LEGACY)
        self.assertEqual(classify_layout_result(503, {}), ACTION_FALLBACK_LEGACY)
        self.assertEqual(classify_layout_result(422, {"ok": False}), ACTION_FAIL_CLOSED)
        self.assertEqual(classify_layout_result(200, sample_layout()), ACTION_USE_LAYOUT)

    def test_only_independent_header_values_disagree(self):
        layout = sample_layout()
        legacy = {"날짜": "2026-09-28", "표시건수": 2, "표시금액": 12000}
        self.assertEqual(independent_header_disagreements(layout, legacy), [])
        legacy["표시금액"] = 11900
        self.assertEqual(independent_header_disagreements(layout, legacy), ["header_sum"])

    def test_existing_trip_row_quarantines_but_daily_total_does_not(self):
        self.assertFalse(date_has_conflicting_rows(
            [{"raw_row_type": "daily_total", "source_id": None}], "src-new"))
        self.assertTrue(date_has_conflicting_rows(
            [{"raw_row_type": "trip", "source_id": None}], "src-new"))

    def test_positive_direct_only_translation(self):
        parsed = layout_to_daily_history(sample_layout())
        self.assertEqual(parsed["표시건수"], 2)
        self.assertEqual(parsed["표시금액"], 12000)
        self.assertEqual(parsed["items"][0]["결제방식"], "미확인")
        self.assertEqual(parsed["items"][1]["결제방식"], "직접")


if __name__ == "__main__":
    unittest.main()
