import unittest

from daily_history_primary_adapter import (
    ACTION_FAIL_CLOSED,
    ACTION_FALLBACK_LEGACY,
    ACTION_USE_LAYOUT,
    classify_layout_result,
    find_kakao_overlap_candidates,
    independent_header_disagreements,
    layout_to_daily_history,
)
from task164_primary_patch import _looks_like_daily_history


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

    def test_mixed_platform_same_date_does_not_block_kakao(self):
        existing = [
            {"id": 1, "raw_row_type": "trip", "콜유형": "우버",
             "배차시각": "20:00", "하차시각": "20:10", "요금": 5000},
            {"id": 2, "raw_row_type": "unclassified", "콜유형": "미분류",
             "배차시각": "20:20", "요금": 7000},
            {"id": 3, "raw_row_type": "daily_total", "콜유형": None,
             "배차시각": "00:00", "요금": 12000},
        ]
        payloads = [
            {"배차시각": "20:00", "하차시각": "20:10", "요금": 5000},
            {"배차시각": "20:20", "하차시각": "20:30", "요금": 7000},
        ]
        self.assertEqual(find_kakao_overlap_candidates(existing, payloads, "src-new"), [])

    def test_kakao_overlap_uses_fare_and_any_start_or_end_time(self):
        existing = [
            {"id": 10, "raw_row_type": "trip", "콜유형": "카카오T",
             "배차시각": "20:10", "하차시각": None, "요금": 5000},
        ]
        payloads = [
            {"배차시각": "20:00", "하차시각": "20:10", "요금": 5000},
        ]
        hits = find_kakao_overlap_candidates(existing, payloads, "src-new")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["existing_id"], 10)

    def test_kakao_same_date_without_identity_overlap_does_not_block(self):
        existing = [
            {"id": 11, "raw_row_type": "trip", "콜유형": "카카오T",
             "배차시각": "21:00", "하차시각": "21:10", "요금": 5000},
        ]
        payloads = [
            {"배차시각": "20:00", "하차시각": "20:10", "요금": 5000},
        ]
        self.assertEqual(find_kakao_overlap_candidates(existing, payloads, "src-new"), [])

    def test_layout_hint_no_longer_requires_legacy_heading(self):
        # OCR heading may be missing; repeated time anchors + header are enough.
        # Regression trigger after regex escaping fix.
        text = "10건 / 70,400원\n19:00 - 19:10\n19:20 - 19:30\n"
        self.assertTrue(_looks_like_daily_history(text))
        self.assertFalse(_looks_like_daily_history("배차 19:00\n최종 요금 7,000원"))

    def test_positive_direct_only_translation(self):
        parsed = layout_to_daily_history(sample_layout())
        self.assertEqual(parsed["표시건수"], 2)
        self.assertEqual(parsed["표시금액"], 12000)
        self.assertEqual(parsed["items"][0]["결제방식"], "미확인")
        self.assertEqual(parsed["items"][1]["결제방식"], "직접")
        self.assertEqual(parsed["items"][0]["날짜"], "2026-09-28")

    def test_rollover_card_date_overrides_header_date(self):
        layout = sample_layout()
        layout["date"] = "2026-10-05"
        layout["cards"][0]["date_hint"] = {
            "month": 10, "day": 6, "source": "TIME_LINE_PREFIX"
        }
        parsed = layout_to_daily_history(layout)
        self.assertNotIn("error_code", parsed)
        self.assertEqual(parsed["items"][0]["날짜"], "2026-10-06")
        self.assertEqual(parsed["items"][1]["날짜"], "2026-10-05")

    def test_year_rollover_card_date_is_next_calendar_day(self):
        layout = sample_layout()
        layout["date"] = "2026-12-31"
        layout["cards"][0]["date_hint"] = {
            "month": 1, "day": 1, "source": "TIME_LINE_PREFIX"
        }
        parsed = layout_to_daily_history(layout)
        self.assertEqual(parsed["items"][0]["날짜"], "2027-01-01")

    def test_out_of_range_card_date_fails_closed(self):
        layout = sample_layout()
        layout["date"] = "2026-10-05"
        layout["cards"][0]["date_hint"] = {
            "month": 10, "day": 8, "source": "TIME_LINE_PREFIX"
        }
        parsed = layout_to_daily_history(layout)
        self.assertEqual(parsed["error_code"], "LAYOUT_CARD_DATE_OUT_OF_RANGE")
        self.assertEqual(parsed["items"], [])


if __name__ == "__main__":
    unittest.main()
