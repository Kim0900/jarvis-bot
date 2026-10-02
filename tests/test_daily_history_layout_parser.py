import unittest
import io
import json
import os
import tempfile
from unittest.mock import patch

from PIL import Image

from daily_history_layout_parser import (
    build_card_layout,
    extract_header_date,
    extract_header_totals,
    map_bbox_to_original,
    ocrspace_overlay_to_lines,
)
from scripts.daily_history_overlay_poc import (
    build_shadow_comparison,
    reconcile_reocr,
    verify_layout,
)
from scripts import daily_history_overlay_poc


def line(text, y0, y1=None, x0=0.04, x1=0.96, chunk=1):
    return {
        "chunk_index": chunk,
        "line_index": 0,
        "text": text,
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1 if y1 is not None else y0 + 0.005,
        "words": [],
    }


def synthetic_cards(count=10, missing_fares=0, overlap_duplicate=False):
    out = []
    gap = 0.05 if count >= 15 else 0.06
    start_y = 0.12 if count >= 15 else 0.15
    fares = [5000 + 100 * i for i in range(count)]
    for i in range(count):
        y = start_y + i * gap
        h1 = 22 - (i // 3)
        h2 = (40 - i) % 60
        out.append(line(f"{h1:02d}:{h2:02d} - {h1:02d}:{(h2+8)%60:02d} 실시간", y))
        out.append(line(f"대구 중구 {i+1}동 출발", y + 0.012))
        out.append(line(f"부산 해운대구 {i+1}동 도착", y + 0.025))
        if i >= missing_fares:
            out.append(line(f"결제 취소하기 {fares[i]:,}원", y + 0.042))
    if overlap_duplicate:
        # duplicate an anchor and its lines at essentially the same normalized y from chunk2
        dup = [dict(x, chunk_index=2) for x in out[:4]]
        out.extend(dup)
    return out



def golden_20260928_geometry_fixture():
    """Sanitized geometry fixture measured from the real 1080x7749 Golden image.

    Contains only time/fare/layout facts needed for parser regression; addresses are
    replaced with generic Korean placeholders.
    """
    y_px = [824, 1319, 1822, 2321, 2821, 3320, 3819, 4318, 4818, 5317]
    times = [
        ("22:32", "22:40"), ("21:55", "22:16"), ("21:40", "21:51"),
        ("21:22", "21:34"), ("20:55", "21:03"), ("20:39", "20:48"),
        ("20:11", "20:20"), ("19:56", "20:02"), ("19:48", "19:52"),
        ("19:38", "19:45"),
    ]
    fares = [6500, 11200, 9200, 7100, 6000, 6900, 7200, 5600, 5000, 5700]
    out = [
        line("2026년 9월 28일", 0.015),
        line("실시간 운행 10건 / 70,400원", 0.03),
    ]
    for i, ((start, end), fare, y) in enumerate(zip(times, fares, y_px), start=1):
        yn = y / 5740.0
        out.append(line(f"{start} - {end} 실시간", yn))
        out.append(line(f"대구 중구 {i}동", yn + 0.018))
        out.append(line(f"부산 해운대구 {i}동", yn + 0.036))
        if i == 2:
            out.append(line("직접 결제", yn + 0.050))
        out.append(line(f"{fare:,}원", yn + 0.061, x0=0.76, x1=0.96))
    # split2 overlap duplicate around card 5 with slightly different OCR text
    y5 = y_px[4] / 5740.0
    out.append(line("20:55 - 21:03 실시간", y5 + 0.0002, chunk=2))
    out.append(line("대구 중구 5 동", y5 + 0.0182, chunk=2))
    return out

class LayoutParserTests(unittest.TestCase):
    def test_coordinate_mapping_split_resize(self):
        mapped = map_bbox_to_original(
            (100, 50, 200, 40),
            crop_box=(0, 4500, 1080, 7749),
            submitted_size=(800, 2407),
            original_size=(1080, 7749),
        )
        self.assertAlmostEqual(mapped["x0"], 135/1080, places=4)
        self.assertGreater(mapped["y0"], 4500/7749)
        self.assertLessEqual(mapped["y1"], 1.0)

    def test_ocrspace_overlay_shape(self):
        parsed = {
            "TextOverlay": {
                "HasOverlay": True,
                "Lines": [{
                    "Words": [
                        {"WordText": "22:32", "Left": 10, "Top": 20, "Width": 60, "Height": 10},
                        {"WordText": "-", "Left": 75, "Top": 20, "Width": 10, "Height": 10},
                        {"WordText": "22:40", "Left": 90, "Top": 20, "Width": 60, "Height": 10},
                    ]
                }]
            }
        }
        lines = ocrspace_overlay_to_lines(
            parsed,
            chunk_index=1,
            crop_box=(0, 0, 1080, 4262),
            submitted_size=(800, 3156),
            original_size=(1080, 7749),
        )
        self.assertEqual(len(lines), 1)
        self.assertIn("22:32 - 22:40", lines[0]["text"])
        self.assertGreater(lines[0]["y0"], 0)

    def test_ocrspace_spaced_clock_tokens_anchor_card(self):
        rows = synthetic_cards(1)
        rows[0] = line("22 : 32 - 22 : 40 실시간", 0.15)
        result = build_card_layout(rows, original_size=(1080, 7749), expected_count=1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cards"][0]["start_time"], "22:32")
        self.assertEqual(result["cards"][0]["end_time"], "22:40")

    def test_invalid_spaced_clock_is_not_anchor(self):
        rows = synthetic_cards(1)
        rows[0] = line("25 : 32 - 22 : 40 실시간", 0.15)
        result = build_card_layout(rows, original_size=(1080, 7749), expected_count=1)
        self.assertEqual(result["error_code"], "LAYOUT_NO_TIME_ANCHORS")

    def test_overlap_duplicate_dedup(self):
        r = build_card_layout(
            synthetic_cards(10, 0, overlap_duplicate=True),
            original_size=(1080, 7749),
            expected_count=10,
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["detected_card_count"], 10)

    def test_overlap_geometry_dedupes_text_variation(self):
        rows = synthetic_cards(3, 0)
        # same original line from split2, slightly different OCR text
        rows.append(line("대구 중구 2동 출발", 0.15 + 0.06 + 0.012, chunk=2))
        r = build_card_layout(
            rows,
            original_size=(1080, 3000),
            expected_count=3,
        )
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["cards"][1]["address_lines"]), 2)

    def test_three_address_lines_requires_selective_reocr(self):
        rows = synthetic_cards(3, 0)
        rows.append(line("경북 경산시 중방동", 0.15 + 0.06 + 0.030, chunk=1))
        r = build_card_layout(
            rows,
            original_size=(1080, 3000),
            expected_count=3,
        )
        self.assertTrue(r["ok"])
        self.assertIn(2, r["reocr_card_indices"])
        self.assertIn("ADDRESS_LINES_AMBIGUOUS", r["cards"][1]["reocr_reasons"])

    def test_five_missing_cards_fit_budget(self):
        r = build_card_layout(
            synthetic_cards(10, 5),
            original_size=(1080, 7749),
            expected_count=10,
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "READY_FOR_SELECTIVE_REOCR")
        self.assertEqual(len(r["reocr_card_indices"]), 5)
        self.assertEqual(r["planned_total_ocr_calls"], 7)

    def test_seven_missing_cards_fail_budget(self):
        r = build_card_layout(
            synthetic_cards(10, 7),
            original_size=(1080, 7749),
            expected_count=10,
        )
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "LAYOUT_REOCR_BUDGET_EXCEEDED")
        self.assertEqual(r["planned_total_ocr_calls"], 9)

    def test_fifteen_cards_with_six_repairs_is_hard_cap_eight(self):
        r = build_card_layout(
            synthetic_cards(15, 6),
            original_size=(1080, 11000),
            expected_count=15,
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["detected_card_count"], 15)
        self.assertEqual(len(r["reocr_card_indices"]), 6)
        self.assertEqual(r["planned_total_ocr_calls"], 8)


    def test_real_golden_geometry_regression(self):
        fixture = golden_20260928_geometry_fixture()
        header = extract_header_totals(fixture)
        self.assertTrue(header["ok"])
        r = build_card_layout(
            fixture,
            original_size=(1080, 7749),
            expected_count=header["expected_count"],
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["detected_card_count"], 10)
        self.assertEqual(sum(card["fare"] or 0 for card in r["cards"]),
                         header["expected_sum"])
        self.assertEqual(
            [card["card_index"] for card in r["cards"] if card["payment"] == "직접"],
            [2],
        )
        self.assertEqual(
            [card["payment"] for card in r["cards"] if card["card_index"] != 2],
            ["미확인"] * 9,
        )
        self.assertEqual(
            [card["payment_evidence"] for card in r["cards"] if card["card_index"] != 2],
            ["NO_DIRECT_LABEL"] * 9,
        )
        self.assertEqual(r["reocr_card_indices"], [])
        self.assertEqual(r["planned_total_ocr_calls"], 2)

    def test_fare_anchor_fallback_recovers_one_missing_time_row(self):
        rows = [
            line("2026년 10월 1일", 0.015),
            line("실시간 운행 12건 / 66,600원", 0.03),
        ]
        rows.extend(synthetic_cards(12))
        # Remove only card 8's time line; all 12 fare rows remain visible.
        time_seen = 0
        filtered = []
        for row in rows:
            if " - " in str(row.get("text") or "") and "실시간" in str(row.get("text") or ""):
                time_seen += 1
                if time_seen == 8:
                    continue
            filtered.append(row)

        r = build_card_layout(
            filtered,
            original_size=(1080, 8000),
            expected_count=12,
            expected_sum=66600,
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["anchor_strategy"], "FARE_ANCHOR_FALLBACK")
        self.assertEqual(r["detected_time_anchor_count"], 11)
        self.assertEqual(r["detected_card_count"], 12)
        missing = [x for x in r["cards"] if "MISSING_TIME" in x["reocr_reasons"]]
        self.assertEqual(len(missing), 1)
        self.assertEqual(r["planned_total_ocr_calls"], 3)

        missing_card = missing[0]
        self.assertTrue(reconcile_reocr(
            missing_card,
            [line("19:20 - 19:29 실시간", 0.5)],
        ))
        result = verify_layout(r, 12, 66600, None, 3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "COMPLETE_LAYOUT_VALIDATED")

    def test_fare_anchor_fallback_rejects_wrong_sum(self):
        rows = synthetic_cards(12)
        # one missing time row but fare population sum does not agree with header
        del rows[4 * 7]
        r = build_card_layout(
            rows,
            original_size=(1080, 8000),
            expected_count=12,
            expected_sum=99999,
        )
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "LAYOUT_CARD_COUNT_MISMATCH")

    def test_expected_count_mismatch_fail_closed(self):
        r = build_card_layout(
            synthetic_cards(10, 0),
            original_size=(1080, 7749),
            expected_count=15,
        )
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_code"], "LAYOUT_CARD_COUNT_MISMATCH")

    def test_plan_with_missing_fare_cannot_pass_final_gate(self):
        r = build_card_layout(synthetic_cards(3, 1), original_size=(1080, 3000),
                              expected_count=3)
        self.assertTrue(r["ok"])
        result = verify_layout(r, 3, 15300, 0, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "LAYOUT_CARD_UNRESOLVED")

    def test_selective_reocr_then_sum_gate(self):
        r = build_card_layout(synthetic_cards(3, 1), original_size=(1080, 3000),
                              expected_count=3)
        self.assertTrue(reconcile_reocr(
            r["cards"][0], [line("결제 취소하기 5,000원", 0.19)]
        ))
        result = verify_layout(r, 3, 15300, 0, 3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "COMPLETE_LAYOUT_VALIDATED")
        self.assertEqual(result["observed_fare_sum"], 15300)

    def test_wrong_sum_and_direct_count_fail_closed(self):
        r = build_card_layout(synthetic_cards(3), original_size=(1080, 3000),
                              expected_count=3)
        self.assertEqual(
            verify_layout(r, 3, 99999, 0, 2)["error_code"],
            "LAYOUT_FARE_SUM_MISMATCH",
        )
        r = build_card_layout(synthetic_cards(3), original_size=(1080, 3000),
                              expected_count=3)
        self.assertEqual(
            verify_layout(r, 3, 15300, 1, 2)["error_code"],
            "LAYOUT_DIRECT_PAYMENT_COUNT_MISMATCH",
        )

    def test_reocr_ambiguous_fare_remains_unresolved(self):
        r = build_card_layout(synthetic_cards(3, 1), original_size=(1080, 3000),
                              expected_count=3)
        self.assertFalse(reconcile_reocr(
            r["cards"][0], [line("5,000원 6,000원", 0.19)]
        ))
        self.assertIn("MISSING_FARE", r["cards"][0]["reocr_reasons"])

    def test_header_date_korean_and_dotted_formats(self):
        rows = [line("2026년 9월 28일", 0.02)] + synthetic_cards(1)
        result = extract_header_date(rows)
        self.assertTrue(result["ok"])
        self.assertEqual(result["date"], "2026-09-28")
        self.assertEqual(result["source"], "OCR_OVERLAY_HEADER")

        rows = [line("2026. 9. 28.", 0.02)] + synthetic_cards(1)
        result = extract_header_date(rows)
        self.assertTrue(result["ok"])
        self.assertEqual(result["date"], "2026-09-28")

    def test_header_date_missing_invalid_or_ambiguous_fails_closed(self):
        self.assertEqual(
            extract_header_date(synthetic_cards(1))["error_code"],
            "LAYOUT_DATE_MISSING_OR_AMBIGUOUS",
        )
        rows = [line("2026년 2월 31일", 0.02)] + synthetic_cards(1)
        self.assertEqual(extract_header_date(rows)["error_code"], "LAYOUT_DATE_INVALID")

        rows = [
            line("2026년 9월 28일", 0.01),
            line("2026년 9월 29일", 0.03),
        ] + synthetic_cards(1)
        self.assertEqual(
            extract_header_date(rows)["error_code"],
            "LAYOUT_DATE_MISSING_OR_AMBIGUOUS",
        )

    def test_header_count_sum_from_overlay_above_first_card(self):
        rows = [line("실시간 운행", 0.03), line("10건 / 70,400원", 0.06)]
        rows.extend(synthetic_cards(10))
        header = extract_header_totals(rows)
        self.assertTrue(header["ok"])
        self.assertEqual((header["expected_count"], header["expected_sum"]), (10, 70400))
        self.assertEqual(header["source"], "OCR_OVERLAY_HEADER")

    def test_missing_or_conflicting_header_fails_closed(self):
        rows = [line("실시간 운행", 0.03)] + synthetic_cards(3)
        self.assertEqual(extract_header_totals(rows)["error_code"],
                         "LAYOUT_HEADER_MISSING_OR_AMBIGUOUS")
        rows[:1] = [line("3건 / 15,300원", 0.03),
                    line("4건 / 15,300원", 0.05)]
        self.assertEqual(extract_header_totals(rows)["error_code"],
                         "LAYOUT_HEADER_MISSING_OR_AMBIGUOUS")

    def test_direct_count_is_observed_without_prior_truth(self):
        r = build_card_layout(synthetic_cards(3), original_size=(1080, 3000),
                              expected_count=3)
        result = verify_layout(r, 3, 15300, None, 2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["observed_direct_count"], 0)
        self.assertEqual(result["observed_payment_unknown_count"], 3)
        self.assertEqual(result["payment_semantics"], "POSITIVE_DIRECT_EVIDENCE_ONLY")
        self.assertFalse(result["direct_count_independently_verified"])

    def test_ui_guidance_is_not_an_address_and_long_distance_is(self):
        rows = synthetic_cards(1)
        rows.append(line("실시간 운행 요금은 안내문구", 0.184))
        r = build_card_layout(rows, original_size=(1080, 3000), expected_count=1)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["cards"][0]["address_lines"]), 2)

    def test_same_input_legacy_shadow_match_without_raw_address_output(self):
        rows = [
            line("22:00 - 22:10 실시간", 0.15),
            line("대구 중구 동인동", 0.17),
            line("대구 수성구 범어동", 0.19),
            line("5,000원", 0.21, x0=0.76, x1=0.96),
        ]
        layout = build_card_layout(rows, original_size=(1080, 3000), expected_count=1)
        legacy_text = (
            "2026년 9월 28일\n"
            "실시간 운행 1건 / 5,000원\n"
            "22:00 - 22:10 실시간\n"
            "대구 중구 동인동\n"
            "대구 수성구 범어동\n"
            "5,000원\n"
        )
        result = build_shadow_comparison(layout, legacy_text)
        self.assertTrue(result["legacy_validation_ok"])
        self.assertEqual(result["mismatch_count"], 0)
        self.assertEqual(result["mode"], "SAME_BASE_OCR_TEXT_NO_LEGACY_FARE_PROBE")
        self.assertEqual(
            result["payment_comparison"],
            "POSITIVE_DIRECT_EVIDENCE_ONLY",
        )
        self.assertNotIn("동인동", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("범어동", json.dumps(result, ensure_ascii=False))

    def test_shadow_flags_direct_payment_disagreement_only(self):
        rows = [
            line("22:00 - 22:10 실시간", 0.15),
            line("대구 중구 동인동", 0.17),
            line("대구 수성구 범어동", 0.19),
            line("직접 결제", 0.205),
            line("5,000원", 0.21, x0=0.76, x1=0.96),
        ]
        layout = build_card_layout(rows, original_size=(1080, 3000), expected_count=1)
        legacy_text = (
            "2026년 9월 28일\n"
            "실시간 운행 1건 / 5,000원\n"
            "22:00 - 22:10 실시간\n"
            "대구 중구 동인동\n"
            "대구 수성구 범어동\n"
            "5,000원\n"
        )
        result = build_shadow_comparison(layout, legacy_text)
        self.assertEqual(result["mismatch_count"], 1)
        self.assertEqual(
            result["mismatches"][0]["fields"],
            ["payment_direct_disagreement"],
        )

    def test_runner_latency_probe_counts_noop_provider_calls(self):
        rows = [
            line("2026년 9월 28일", 0.015),
            line("실시간 운행 3건 / 15,300원", 0.03),
        ]
        rows.extend(synthetic_cards(3))
        response = {
            "ParsedResults": [{"FileParseExitCode": 1, "ParsedText": "base"}],
            "ProcessingTimeInMilliseconds": 10,
        }
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            Image.new("RGB", (1080, 3000), "white").save(tmp.name)
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"OCR_SPACE_API_KEY": "test-only"}),
                patch("sys.argv", ["poc", "--image", tmp.name,
                                   "--latency-probe-extra-cards", "2"]),
                patch.object(daily_history_overlay_poc, "ocr_overlay",
                             return_value=response) as ocr,
                patch.object(daily_history_overlay_poc, "ocrspace_overlay_to_lines",
                             side_effect=[rows, []]),
                patch("sys.stdout", output),
            ):
                code = daily_history_overlay_poc.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["actual_total_ocr_calls"], 4)
        self.assertEqual(result["diagnostic_latency_probe_extra_calls"], 2)
        self.assertEqual(len(result["ocr_call_wall_ms"]), 4)
        self.assertEqual(ocr.call_count, 4)

    def test_runner_uses_header_without_required_external_totals(self):
        rows = [
            line("2026년 9월 28일", 0.015),
            line("실시간 운행 3건 / 15,300원", 0.03),
        ]
        rows.extend(synthetic_cards(3))
        fake_response = {
            "ParsedResults": [{"FileParseExitCode": 1}],
            "ProcessingTimeInMilliseconds": 10,
        }
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            Image.new("RGB", (1080, 3000), "white").save(tmp.name)
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"OCR_SPACE_API_KEY": "test-only"}),
                patch("sys.argv", ["poc", "--image", tmp.name]),
                patch.object(daily_history_overlay_poc, "ocr_overlay",
                             return_value=fake_response),
                patch.object(daily_history_overlay_poc, "ocrspace_overlay_to_lines",
                             side_effect=[rows, []]),
                patch("sys.stdout", output),
            ):
                code = daily_history_overlay_poc.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["header"]["source"], "OCR_OVERLAY_HEADER")
        self.assertEqual(result["observed_fare_sum"], 15300)
        self.assertEqual(result["date"], "2026-09-28")
        self.assertEqual(result["date_source"], "OCR_OVERLAY_HEADER")
        self.assertFalse(result["direct_count_independently_verified"])

    def test_runner_expected_date_mismatch_fails_closed(self):
        rows = [
            line("2026년 9월 28일", 0.015),
            line("실시간 운행 3건 / 15,300원", 0.03),
        ]
        rows.extend(synthetic_cards(3))
        response = {"ParsedResults": [{"FileParseExitCode": 1}],
                    "ProcessingTimeInMilliseconds": 10}
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            Image.new("RGB", (1080, 3000), "white").save(tmp.name)
            output = io.StringIO()
            with (
                patch.dict(os.environ, {"OCR_SPACE_API_KEY": "test-only"}),
                patch("sys.argv", ["poc", "--image", tmp.name,
                                   "--expected-date", "2026-09-29"]),
                patch.object(daily_history_overlay_poc, "ocr_overlay",
                             return_value=response),
                patch.object(daily_history_overlay_poc, "ocrspace_overlay_to_lines",
                             side_effect=[rows, []]),
                patch("sys.stdout", output),
            ):
                code = daily_history_overlay_poc.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result["error_code"], "LAYOUT_DATE_ASSERTION_MISMATCH")

    def test_runner_hidden_key_prompt_does_not_require_env(self):
        rows = [
            line("2026. 9. 28.", 0.015),
            line("실시간 운행 3건 / 15,300원", 0.03),
        ]
        rows.extend(synthetic_cards(3))
        response = {"ParsedResults": [{"FileParseExitCode": 1}],
                    "ProcessingTimeInMilliseconds": 10}
        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
            Image.new("RGB", (1080, 3000), "white").save(tmp.name)
            output = io.StringIO()
            env = dict(os.environ)
            env.pop("OCR_SPACE_API_KEY", None)
            with (
                patch.dict(os.environ, env, clear=True),
                patch("sys.argv", ["poc", "--image", tmp.name, "--prompt-key"]),
                patch.object(daily_history_overlay_poc.getpass, "getpass",
                             return_value="local-secret") as prompt,
                patch.object(daily_history_overlay_poc, "ocr_overlay",
                             return_value=response) as ocr,
                patch.object(daily_history_overlay_poc, "ocrspace_overlay_to_lines",
                             side_effect=[rows, []]),
                patch("sys.stdout", output),
            ):
                code = daily_history_overlay_poc.main()
        self.assertEqual(code, 0)
        prompt.assert_called_once()
        self.assertEqual(ocr.call_args.args[1], "local-secret")
        self.assertNotIn("local-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()


# Task164 primary-cutover regression is imported here so the existing
# path-filtered workflow executes the new policy/persistence tests.
from tests.test_daily_history_primary_adapter import PrimaryPolicyTests
from tests.test_daily_history_primary_persistence import PersistenceTests

from tests.test_image_retry_policy import ImageRetryPolicyTests

from tests.test_canonical_identity_v1 import CanonicalIdentityV1Tests

from tests.test_canonical_access_v1 import CanonicalAccessV1Tests

from tests.test_canonical_runtime_audit import CanonicalRuntimeAuditTests

from tests.test_daily_operation_report_v1 import DailyOperationReportV1Tests

from tests.test_uber_trip_parser import UberTripParserV2Tests
