import unittest

from daily_operation_report_v1 import build_briefing_section_a_v1, build_daily_operation_report_v1, build_month_activity_v1, build_snapshot_briefing_metrics_v1, resolve_briefing_date_v1


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


    def test_resolve_briefing_date_v1(self):
        self.assertEqual(resolve_briefing_date_v1(None, "2026-10-05"), "2026-10-05")
        self.assertEqual(resolve_briefing_date_v1("오늘", "2026-10-05"), "2026-10-05")
        self.assertEqual(resolve_briefing_date_v1("어제", "2026-10-05"), "2026-10-04")
        self.assertEqual(resolve_briefing_date_v1("2026-10-04", "2026-10-05"), "2026-10-04")
        with self.assertRaises(ValueError):
            resolve_briefing_date_v1("10/4", "2026-10-05")


    def test_snapshot_briefing_metrics_v1(self):
        metrics = build_snapshot_briefing_metrics_v1(
            "2026-10-04",
            {
                "calc_date": "2026-10-04",
                "axis": "A",
                "call_count": 8,
                "avg_fare": "8575.00",
                "max_interval_min": "72.00",
                "unclassified_flag": False,
            },
            {
                "calc_date": "2026-10-04",
                "window_start": "2026-09-28",
                "window_end": "2026-10-04",
                "total_count": 48,
                "daily_average": "6.86",
                "status": "CRITICAL",
            },
        )
        self.assertEqual(metrics["source"], "daily_calc_snapshot+kpi_7day_snapshot")
        self.assertEqual(metrics["daily_call_count"], 8)
        self.assertEqual(metrics["daily_avg_fare"], 8575)
        self.assertEqual(metrics["daily_max_interval_min"], 72.0)
        self.assertEqual(metrics["kpi_7day_total"], 48)
        self.assertEqual(metrics["kpi_7day_avg"], 6.86)
        self.assertEqual(metrics["kpi_status"], "CRITICAL")

    def test_snapshot_briefing_metrics_fails_closed(self):
        with self.assertRaises(ValueError):
            build_snapshot_briefing_metrics_v1(
                "2026-10-04",
                {"calc_date": "2026-10-04", "axis": "B"},
                {"calc_date": "2026-10-04"},
            )


if __name__ == "__main__":
    unittest.main()
