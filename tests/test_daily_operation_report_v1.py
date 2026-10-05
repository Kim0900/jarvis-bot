import unittest

from daily_operation_report_v1 import build_briefing_section_a_v1, build_daily_operation_report_v1, build_month_activity_v1


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


    def test_briefing_section_a_uses_canonical_report_contract(self):
        report = build_daily_operation_report_v1(
            "2026-10-04",
            [
                {
                    "id": 1, "날짜": "2026-10-04", "배차시각": "19:00", "하차시각": "19:10",
                    "요금": 6000, "canonical_platform": "KAKAO",
                    "data_source": "drive_ocr_layout_v1", "canonical_weak_candidate_count": 0,
                },
                {
                    "id": 2, "날짜": "2026-10-04", "배차시각": "20:00", "하차시각": "20:10",
                    "요금": 10000, "canonical_platform": "UBER",
                    "data_source": "drive_ocr_layout_v1", "canonical_weak_candidate_count": 1,
                },
            ],
            [{"match_status": "UNMATCHED", "fare": 7000}],
        )
        section = build_briefing_section_a_v1(report)
        self.assertEqual(section["source"], "daily_operation_report_v1")
        self.assertEqual(section["date_basis"], "calendar_day")
        self.assertEqual(section["calls"], 2)
        self.assertEqual(section["revenue"], 16000)
        self.assertEqual(section["avg_fare"], 8000)
        self.assertEqual(section["platform"]["kakao"], 1)
        self.assertEqual(section["platform"]["uber"], 1)
        self.assertEqual(section["platform"]["roam_confirmed"], 0)
        self.assertEqual(section["weak_identity_candidate_rows"], 1)
        self.assertEqual(section["roaming_candidate"]["count"], 1)
        self.assertTrue(section["roaming_candidate"]["candidate_only"])

    def test_briefing_section_a_fails_closed_on_wrong_version(self):
        with self.assertRaises(ValueError):
            build_briefing_section_a_v1({
                "version": "legacy",
                "date_basis": "calendar_day",
            })


    def test_month_activity_uses_calendar_days_and_operating_days(self):
        rows = [
            {"날짜": "2026-10-01"},
            {"날짜": "2026-10-01"},
            {"날짜": "2026-10-03"},
            {"날짜": "2026-10-06"},  # future relative to through-date; ignored
            {"날짜": "2026-09-30"},  # other month; ignored
        ]
        stats = build_month_activity_v1("2026-10-05", rows)
        self.assertEqual(stats["cumulative_calls"], 3)
        self.assertEqual(stats["calendar_days_elapsed"], 5)
        self.assertEqual(stats["calendar_avg_calls"], 0.6)
        self.assertEqual(stats["operating_days"], 2)
        self.assertEqual(stats["workday_avg_calls"], 1.5)


if __name__ == "__main__":
    unittest.main()
