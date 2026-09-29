import unittest

from daily_history_parser import (
    extract_fare_probe_amounts,
    parse_daily_history_text,
    validate_daily_history_document,
)

GOLDEN_10 = """일별 운행 이력
2026년 9월 28일(월) 10건
실시간 운행
10건 / 70,400원
22:32 - 22:40 실시간 >
• 대구 남구 대명3동
○ 대구 서구 평리3동
결제 취소하기
6,500원
21:55 - 22:16 실시간 >
• 대구 동구 신천4동
○ 대구 남구 대명1동
직접결제
11,200원
21:40 - 21:51 실시간
대구 남구 대명2동
대구 동구 신천4동
9,200원
21:22 - 21:34 실시간
대구 중구 삼덕동
대구 남구 대명2동
7,100원
20:55 - 21:03 실시간
대구 북구 침산2동
대구 중구 성내1동
6,000원
20:39 - 20:48 실시간
대구 동구 신암4동
대구 북구 침산2동
6,900원
20:11 - 20:20 실시간
대구 수성구 만촌1동
대구 동구 해안동
7,200원
19:56 - 20:02 실시간
대구 수성구 만촌2동
대구 수성구 만촌1동
5,600원
19:48 - 19:52 실시간
대구 수성구 범어1동
대구 수성구 범어4동
5,000원
19:38 - 19:45 실시간
대구 수성구 범어3동
대구 수성구 범어1동
5,700원
"""

ONE_ROW = """일별 운행 이력
2026년 9월 29일(화) 1건
실시간 운행 1건 / 7,700원
00:05 - 00:11 실시간 >
대구 수성구 범어2동
대구 동구 신천3동
7,700원
"""

class TestDailyHistoryParser(unittest.TestCase):
    def test_golden_10_rows(self):
        p = parse_daily_history_text(GOLDEN_10)
        self.assertEqual(p["날짜"], "2026-09-28")
        self.assertEqual(p["표시건수"], 10)
        self.assertEqual(p["표시금액"], 70400)
        self.assertEqual(p["time_anchor_count"], 10)
        self.assertEqual(len(p["items"]), 10)
        self.assertEqual(sum(x["요금"] for x in p["items"]), 70400)
        self.assertEqual(p["items"][1]["결제방식"], "직접")
        self.assertTrue(validate_daily_history_document(p)["ok"])

    def test_one_row(self):
        p = parse_daily_history_text(ONE_ROW)
        self.assertEqual(len(p["items"]), 1)
        self.assertTrue(validate_daily_history_document(p)["ok"])

    def test_partial_extraction_is_fail_closed(self):
        p = parse_daily_history_text(GOLDEN_10)
        p["items"] = p["items"][:1]
        v = validate_daily_history_document(p)
        self.assertFalse(v["ok"])
        self.assertEqual(v["error_code"], "DAILY_HISTORY_COUNT_MISMATCH")

    def test_missing_date_is_fail_closed(self):
        p = parse_daily_history_text(ONE_ROW.replace("2026년 9월 29일(화)", "날짜 미인식"))
        v = validate_daily_history_document(p)
        self.assertFalse(v["ok"])
        self.assertEqual(v["error_code"], "DAILY_HISTORY_DATE_MISSING")


    def test_split_overlap_dedup(self):
        top = """일별 운행 이력
2026년 9월 28일(월) 2건
실시간 운행 2건 / 17,700원
21:55 - 22:16 실시간
대구 동구 신천4동
대구 남구 대명1동
직접결제
11,200원
21:40 - 21:51 실시간
대구 남구 대명2동
대구 동구 신천4동
9,200원
"""
        bottom = """21:40 - 21:51 실시간
대구 남구 대명2동
대구 동구 신천4동
9,200원
"""
        p = parse_daily_history_text(
            "---MAGI_OCR_CHUNK_1---\n" + top +
            "---MAGI_OCR_CHUNK_2---\n" + bottom
        )
        self.assertEqual(p["time_anchor_count"], 2)
        self.assertEqual(len(p["items"]), 2)
        self.assertEqual(p["items"][0]["결제방식"], "직접")

    def test_split_prefers_complete_duplicate(self):
        top = """일별 운행 이력
2026년 9월 29일(화) 1건
실시간 운행 1건 / 7,700원
00:05 - 00:11 실시간
대구 수성구 범어2동
"""
        bottom = """00:05 - 00:11 실시간
대구 수성구 범어2동
대구 동구 신천3동
7,700원
"""
        p = parse_daily_history_text(
            "---MAGI_OCR_CHUNK_1---\n" + top +
            "---MAGI_OCR_CHUNK_2---\n" + bottom
        )
        self.assertEqual(p["time_anchor_count"], 1)
        self.assertEqual(len(p["items"]), 1)
        self.assertTrue(validate_daily_history_document(p)["ok"])

    def test_fare_probe_excludes_header_total(self):
        text = "70,400원\n6,500원\n11,200원\n9,200원"
        self.assertEqual(
            extract_fare_probe_amounts(text, displayed_amount=70400),
            [6500, 11200, 9200],
        )

    def test_fare_probe_dedupes_chunk_boundary(self):
        text = (
            "---MAGI_OCR_CHUNK_1---\n"
            "6,500원\n11,200원\n9,200원\n7,100원\n"
            "---MAGI_OCR_CHUNK_2---\n"
            "7,100원\n6,000원\n6,900원\n7,200원\n5,600원\n5,000원\n5,700원"
        )
        self.assertEqual(
            extract_fare_probe_amounts(text, displayed_amount=70400),
            [6500, 11200, 9200, 7100, 6000, 6900, 7200, 5600, 5000, 5700],
        )

    def test_amount_mismatch_is_fail_closed(self):
        p = parse_daily_history_text(ONE_ROW.replace("7,700원\n00:05", "8,000원\n00:05", 1))
        v = validate_daily_history_document(p)
        self.assertFalse(v["ok"])
        self.assertEqual(v["error_code"], "DAILY_HISTORY_AMOUNT_MISMATCH")

if __name__ == "__main__":
    unittest.main()
