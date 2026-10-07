import unittest
from datetime import date

from kakao_monthly_history_parser import (
    looks_like_kakao_monthly_history,
    parse_kakao_monthly_history_text,
)


class KakaoMonthlyHistoryParserTests(unittest.TestCase):
    def test_october_control_totals(self):
        text = """
        운행 이력
        월별
        2026년 10월 60건
        실시간 운행 60건 / 예약 운행 0건
        6일 (화) 2건
        5일 (월) 14건
        4일 (일) 9건
        3일 (토) 15건
        2일 (금) 8건
        1일 (목) 12건
        """
        self.assertTrue(looks_like_kakao_monthly_history(text))
        parsed = parse_kakao_monthly_history_text(text, today=date(2026, 10, 7))
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["monthly_total"], 60)
        self.assertEqual(
            [(x["page_date"], x["expected_count"]) for x in parsed["items"]],
            [
                ("2026-10-01", 12),
                ("2026-10-02", 8),
                ("2026-10-03", 15),
                ("2026-10-04", 9),
                ("2026-10-05", 14),
                ("2026-10-06", 2),
            ],
        )

    def test_internal_missing_day_becomes_verified_zero(self):
        text = """
        월별
        2026년 10월 9건
        실시간 운행 9건
        3일 (토) 5건
        1일 (목) 4건
        """
        parsed = parse_kakao_monthly_history_text(text, today=date(2026, 10, 3))
        by_date = {x["page_date"]: x["expected_count"] for x in parsed["items"]}
        self.assertEqual(by_date["2026-10-02"], 0)

    def test_current_month_trailing_unseen_days_are_not_assumed_off(self):
        text = """
        월별
        2026년 10월 12건
        실시간 운행 12건
        6일 (화) 12건
        """
        parsed = parse_kakao_monthly_history_text(text, today=date(2026, 10, 7))
        self.assertEqual(parsed["control_through_day"], 6)
        self.assertEqual(len(parsed["items"]), 6)

    def test_previous_month_fills_trailing_days_as_zero(self):
        text = """
        월별
        2026년 9월 3건
        실시간 운행 3건
        28일 (월) 3건
        """
        parsed = parse_kakao_monthly_history_text(text, today=date(2026, 10, 7))
        self.assertEqual(parsed["control_through_day"], 30)
        self.assertEqual(parsed["items"][-1], {
            "page_date": "2026-09-30",
            "expected_count": 0,
        })

    def test_daily_history_is_not_monthly(self):
        text = "일별 운행 이력 2026년 10월 5일 14건 106,400원"
        self.assertFalse(looks_like_kakao_monthly_history(text))


if __name__ == "__main__":
    unittest.main()
