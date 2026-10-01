import unittest

from daily_operation_report_v1 import build_daily_operation_report_v1


class DailyOperationReportV1Tests(unittest.TestCase):
    def test_calendar_day_summary_and_hourly_extremes(self):
        rows = [
            {
                "id": 1, "날짜": "2026-07-15", "배차시각": "19:00", "하차시각": "19:10",
                "요금": 5000, "canonical_platform": "KAKAO",
                "data_source": "drive_ocr_tesseract", "canonical_weak_candidate_count": 0,
            },
            {
                "id": 2, "날짜": "2026-07-15", "배차시각": "19:18", "하차시각": "19:30",
                "요금": 7000, "canonical_platform": "KAKAO",
                "data_source": "drive_ocr_tesseract", "canonical_weak_candidate_count": 0,
            },
            {
                "id": 3, "날짜": "2026-07-15", "배차시각": "21:00", "하차시각": "21:12",
                "요금": 10000, "canonical_platform": "UBER",
                "data_source": "app_ocr_individual", "canonical_weak_candidate_count": 1,
            },
        ]
        report = build_daily_operation_report_v1("2026-07-15", rows, [])
        self.assertEqual(report["total_count"], 3)
        self.assertEqual(report["total_revenue"], 22000)
        self.assertEqual(report["platform"]["KAKAO"], {"count": 2, "revenue": 12000})
        self.assertEqual(report["highest_revenue_hour"]["hour"], "19:00")
        self.assertEqual(report["lowest_revenue_hour"]["hour"], "21:00")
        self.assertEqual(report["gap"]["min_gap_min"], 8)
        self.assertEqual(report["gap"]["continuous_call_gap_count"], 1)
        self.assertEqual(report["weak_identity_candidate_rows"], 1)

    def test_s700_unmatched_is_candidate_only(self):
        report = build_daily_operation_report_v1(
            "2026-07-15",
            [],
            [
                {"match_status": "UNMATCHED", "fare": 7700},
                {"match_status": "MATCHED", "fare": 8000},
            ],
        )
        self.assertTrue(report["roaming_candidate"]["candidate_only"])
        self.assertEqual(report["roaming_candidate"]["count"], 1)
        self.assertEqual(report["roaming_candidate"]["revenue"], 7700)

    def test_missing_times_marks_gap_partial(self):
        rows = [
            {
                "id": 1, "날짜": "2026-07-15", "배차시각": "19:00", "하차시각": None,
                "요금": 5000, "canonical_platform": "KAKAO",
                "data_source": "drive_ocr_tesseract",
            }
        ]
        report = build_daily_operation_report_v1("2026-07-15", rows, [])
        self.assertTrue(report["gap"]["partial"])
        self.assertEqual(report["gap"]["missing_time_trip_count"], 1)


if __name__ == "__main__":
    unittest.main()
