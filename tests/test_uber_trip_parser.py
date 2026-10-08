import unittest

from uber_trip_parser import (
    extract_focused_uber_fare_amounts,
    looks_like_uber_trip_detail,
    parse_uber_trip_detail_text,
    reconcile_compact_fare_with_probe,
    validate_uber_trip_detail,
)


COMPACT_SAMPLE = """운행 세부사항
일반 콜 · 2026. 9. 15. · PM 11:57
₩18,600
시간
23분 14초
거리
11.81 km
대구광역시 중구 남산동 KR
대구광역시 북구 구암동 KR
수입 3포인트
직접 결제
"""

LEGACY_SAMPLE = """운행 세부사항
2026. 9. 15. PM 11:57
23분 14초
11.81 km
대구광역시 중구 남산동 KR
대구광역시 북구 구암동 KR
순수익
요금
₩18,600
정산
-₩18,600
"""


class UberTripParserV2Tests(unittest.TestCase):
    def test_compact_screen_signature_without_net_earnings(self):
        self.assertTrue(looks_like_uber_trip_detail(COMPACT_SAMPLE))

    def test_compact_screen_parses_visible_fields(self):
        parsed = parse_uber_trip_detail_text(COMPACT_SAMPLE)
        self.assertEqual(parsed["ui_variant"], "compact_detail_v2")
        self.assertEqual(parsed["날짜"], "2026-09-15")
        self.assertEqual(parsed["배차시각"], "23:57")
        self.assertEqual(parsed["요금"], 18600)
        self.assertEqual(parsed["요금근거"], "COMPACT_HEADER_UNIQUE_CURRENCY")
        self.assertEqual(parsed["운행시간_초"], 1394)
        self.assertEqual(parsed["거리_km"], 11.81)
        self.assertEqual(parsed["출발지"], "대구광역시 중구 남산동")
        self.assertEqual(parsed["도착지"], "대구광역시 북구 구암동")
        self.assertEqual(parsed["결제방식"], "직접")
        self.assertEqual(parsed["결제수단"], "직접결제")
        self.assertEqual(validate_uber_trip_detail(parsed), {"ok": True})

    def test_compact_header_multiple_currency_values_fail_closed(self):
        sample = COMPACT_SAMPLE.replace(
            "₩18,600\n시간",
            "₩18,600\n₩17,900\n시간",
        )
        parsed = parse_uber_trip_detail_text(sample)
        gate = validate_uber_trip_detail(parsed)
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["error_code"], "UBER_COMPACT_FARE_AMBIGUOUS")
        self.assertNotIn("요금", parsed)

    def test_compact_missing_destination_fails_closed(self):
        sample = COMPACT_SAMPLE.replace("대구광역시 북구 구암동 KR\n", "")
        parsed = parse_uber_trip_detail_text(sample)
        gate = validate_uber_trip_detail(parsed)
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["error_code"], "UBER_COMPACT_REQUIRED_FIELD_MISSING")

    def test_legacy_screen_remains_supported(self):
        self.assertTrue(looks_like_uber_trip_detail(LEGACY_SAMPLE))
        parsed = parse_uber_trip_detail_text(LEGACY_SAMPLE)
        self.assertEqual(parsed["ui_variant"], "legacy_detail")
        self.assertEqual(parsed["요금"], 18600)
        self.assertEqual(parsed["요금근거"], "LEGACY_FARE_LABEL")
        self.assertNotIn("요금_정산액_참고", parsed)


    def test_legacy_two_token_signature_is_not_sufficient(self):
        text = """운행 세부사항
순수익
요금
₩18,600
"""
        self.assertFalse(looks_like_uber_trip_detail(text))

    def test_malformed_legacy_fails_strict_save_gate(self):
        text = """운행 세부사항
순수익
2026. 9. 15. PM 11:57
요금
₩18,600
"""
        parsed = parse_uber_trip_detail_text(text)
        self.assertEqual(parsed["ui_variant"], "legacy_detail")
        gate = validate_uber_trip_detail(parsed)
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["error_code"], "UBER_LEGACY_REQUIRED_FIELD_MISSING")
        self.assertIn("출발지", gate["message"])
        self.assertIn("도착지", gate["message"])

    def test_generic_detail_page_is_not_misclassified(self):
        text = "운행 세부사항\n2026. 9. 15. PM 11:57\n11.81 km"
        self.assertFalse(looks_like_uber_trip_detail(text))

    def test_kor_korean_address_variant(self):
        text = """운행 세부사항
일반 콜 · 2026. 10. 3. · PM 8:36
₩4,800
시간
4분 46초
거리
1.69 km
대구광역시 북구 침산동 KOR
대구광역시 북구 산격동 KOR
3포인트 수익을 달성했습니다
직접 결제
"""
        self.assertTrue(looks_like_uber_trip_detail(text))
        parsed = parse_uber_trip_detail_text(text)
        self.assertEqual(parsed["날짜"], "2026-10-03")
        self.assertEqual(parsed["배차시각"], "20:36")
        self.assertEqual(parsed["요금"], 4800)
        self.assertEqual(parsed["출발지"], "대구광역시 북구 침산동")
        self.assertEqual(parsed["도착지"], "대구광역시 북구 산격동")
        self.assertEqual(parsed["주소표기"], "KOREAN_OR_MIXED")
        self.assertEqual(parsed["결제수단"], "직접결제")
        self.assertEqual(validate_uber_trip_detail(parsed), {"ok": True})

    def test_task190_focused_probe_corrects_duplicated_leading_digit(self):
        whole = """운행 세부사항
일반 콜 · 2026. 10. 3. · PM 8:36
₩44,800
시간
4분 46초
거리
1.69 km
대구광역시 북구 침산동 KOR
대구광역시 북구 산격동 KOR
3포인트 수익을 달성했습니다
직접 결제
"""
        parsed = parse_uber_trip_detail_text(whole)
        self.assertEqual(parsed["요금"], 44800)
        reconciled = reconcile_compact_fare_with_probe(
            parsed,
            "2026. 10. 3. PM 8:36\n¥#4,800\n",
        )
        self.assertTrue(reconciled["ok"])
        self.assertEqual(reconciled["probe_status"], "CORRECTED")
        self.assertEqual(reconciled["parsed"]["요금"], 4800)
        self.assertEqual(reconciled["parsed"]["요금_whole_ocr_참고"], 44800)
        self.assertEqual(
            reconciled["parsed"]["요금근거"],
            "COMPACT_HEADER_FOCUSED_REOCR",
        )
        self.assertEqual(validate_uber_trip_detail(reconciled["parsed"]), {"ok": True})

    def test_task190_focused_probe_agreement_keeps_amount(self):
        parsed = parse_uber_trip_detail_text(COMPACT_SAMPLE)
        reconciled = reconcile_compact_fare_with_probe(
            parsed,
            "2026. 9. 15. PM 11:57\n₩18,600\n",
        )
        self.assertTrue(reconciled["ok"])
        self.assertEqual(reconciled["probe_status"], "AGREED")
        self.assertEqual(reconciled["parsed"]["요금"], 18600)
        self.assertNotIn("요금_whole_ocr_참고", reconciled["parsed"])

    def test_task190_focused_probe_ambiguous_fails_closed(self):
        parsed = parse_uber_trip_detail_text(COMPACT_SAMPLE)
        reconciled = reconcile_compact_fare_with_probe(
            parsed,
            "₩18,600\n17,900\n",
        )
        self.assertFalse(reconciled["ok"])
        self.assertEqual(
            reconciled["error_code"],
            "UBER_FOCUSED_FARE_AMBIGUOUS",
        )

    def test_task190_probe_excludes_datetime_and_metrics(self):
        amounts = extract_focused_uber_fare_amounts(
            "2026. 10. 3. PM 8:36\n4,800\n01:41\n1.69 km\n4분 46초\n"
        )
        self.assertEqual(amounts, [4800])

    def test_task190_no_probe_evidence_preserves_compatibility(self):
        parsed = parse_uber_trip_detail_text(COMPACT_SAMPLE)
        reconciled = reconcile_compact_fare_with_probe(parsed, "운행 세부사항\n")
        self.assertTrue(reconciled["ok"])
        self.assertEqual(reconciled["probe_status"], "NO_EVIDENCE")
        self.assertEqual(reconciled["parsed"]["요금"], 18600)

    def test_foreign_rider_english_kor_addresses_without_currency_glyph(self):
        text = """운행 세부사항
XL · 2026. 10. 4. · AM 5:20
12,805
시간
22분 59초
거리
9.57 km
Daegu Suseong District KOR
Daegu Dong-gu Jijeo-dong KOR
3포인트 수익을 달성했습니다
"""
        self.assertTrue(looks_like_uber_trip_detail(text))
        parsed = parse_uber_trip_detail_text(text)
        self.assertEqual(parsed["요금"], 12805)
        self.assertEqual(parsed["요금근거"], "COMPACT_HEADER_UNIQUE_BARE_AMOUNT")
        self.assertEqual(validate_uber_trip_detail(parsed), {"ok": True})

    def test_foreign_rider_xl_period_thousands_fallback(self):
        text = """운행 세부사항
XL · 2026. 10. 4. · AM 5:20
12.805
시간
22분 59초
거리
9.57 km
Daegu Suseong District KOR
Daegu Dong-gu Jijeo-dong KOR
"""
        parsed = parse_uber_trip_detail_text(text)
        self.assertEqual(parsed["요금"], 12805)
        self.assertEqual(parsed["요금근거"], "COMPACT_HEADER_UNIQUE_BARE_AMOUNT")

    def test_bare_amount_fallback_does_not_use_later_distance_or_points(self):
        text = """운행 세부사항
XL · 2026. 10. 4. · AM 5:20
시간
22분 59초
거리
9.57 km
Daegu Suseong District KOR
Daegu Dong-gu Jijeo-dong KOR
3포인트 수익을 달성했습니다
"""
        parsed = parse_uber_trip_detail_text(text)
        self.assertNotIn("요금", parsed)
        self.assertIn("요금 파싱실패", parsed["parse_errors"])

    def test_foreign_rider_english_kor_addresses(self):
        text = """운행 세부사항
XL · 2026. 10. 4. · AM 5:20
₩12,805
시간
22분 59초
거리
9.57 km
Daegu Suseong District KOR
Daegu Dong-gu Jijeo-dong KOR
3포인트 수익을 달성했습니다
"""
        self.assertTrue(looks_like_uber_trip_detail(text))
        parsed = parse_uber_trip_detail_text(text)
        self.assertEqual(parsed["날짜"], "2026-10-04")
        self.assertEqual(parsed["배차시각"], "05:20")
        self.assertEqual(parsed["요금"], 12805)
        self.assertEqual(parsed["운행시간_분"], 23.0)
        self.assertEqual(parsed["거리_km"], 9.57)
        self.assertEqual(parsed["출발지"], "Daegu Suseong District")
        self.assertEqual(parsed["도착지"], "Daegu Dong-gu Jijeo-dong")
        self.assertEqual(parsed["주소표기"], "ENGLISH")
        self.assertEqual(validate_uber_trip_detail(parsed), {"ok": True})


if __name__ == "__main__":
    unittest.main()
