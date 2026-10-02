#!/usr/bin/env python3
"""Read-only Task #164 OCR.space overlay PoC.

This script:
1) split2-crops the image with the production 10% overlap,
2) requests OCR.space Engine2 with isOverlayRequired=true,
3) maps coordinates back to original-image normalized space,
4) re-OCRs only incomplete cards within the eight-call hard cap,
5) derives count/sum from the OCR header and validates all card totals,
6) prints JSON only. It performs no DB/Drive writes.

Use OCR_SPACE_API_KEY in the environment or --prompt-key for a local hidden prompt.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from io import BytesIO
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daily_history_layout_parser import (
    _fare_candidates,
    _looks_like_address,
    build_card_layout,
    extract_header_date,
    plan_missing_anchor_rescue,
    extract_header_totals,
    ocrspace_overlay_to_lines,
)

OCR_SPACE_URL = "https://api.ocr.space/parse/image"
MAX_WIDTH = 800
TIMEOUT_SEC = 25


def resize(img: Image.Image) -> Image.Image:
    if img.width <= MAX_WIDTH:
        return img
    scale = MAX_WIDTH / img.width
    return img.resize((MAX_WIDTH, max(1, int(img.height * scale))), Image.LANCZOS)


def jpeg_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    resize(img).convert("RGB").save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def ocr_overlay(img: Image.Image, api_key: str) -> dict:
    payload = urllib.parse.urlencode({
        "base64Image": "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes(img)).decode(),
        "language": "kor",
        "isOverlayRequired": "true",
        "OCREngine": "2",
        "scale": "true",
    }).encode()
    req = urllib.request.Request(
        OCR_SPACE_URL,
        data=payload,
        headers={"apikey": api_key, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        result = json.loads(resp.read().decode())
    if result.get("IsErroredOnProcessing"):
        raise RuntimeError(f"OCRSPACE_ERROR:{result.get('ErrorMessage')}")
    parsed = result.get("ParsedResults") or []
    if not parsed or str(parsed[0].get("FileParseExitCode")) != "1":
        raise RuntimeError("OCRSPACE_PARSE_FAILED")
    return result


def reconcile_reocr(card: dict, lines: list[dict]) -> bool:
    """Accept a selective OCR result only when it uniquely resolves missing fields."""
    texts = [str(line.get("text") or "") for line in lines]
    fares = [fare for text in texts for fare in _fare_candidates(text)]
    if len(set(fares)) != 1:
        return False
    if "MISSING_FARE" in card["reocr_reasons"]:
        card["fare"] = fares[0]
    elif card["fare"] != fares[0]:
        return False

    if any(reason.startswith("ADDRESS_LINES_") for reason in card["reocr_reasons"]):
        addresses = [text for text in texts if _looks_like_address(text)]
        if len(addresses) != 2 or len(set(addresses)) != 2:
            return False
        card["address_lines"] = addresses
    card["reocr_reasons"] = []
    return True


def verify_layout(layout: dict, expected_count: int, expected_sum: int,
                  expected_direct_count: int | None, actual_calls: int) -> dict:
    """Final count/sum gate; direct count is only an optional independent assertion."""
    layout["actual_total_ocr_calls"] = actual_calls
    if not layout.get("ok"):
        return layout
    cards = layout["cards"]
    error = None
    if actual_calls > layout["max_total_ocr_calls"]:
        error = "LAYOUT_REOCR_BUDGET_EXCEEDED"
    elif len(cards) != expected_count:
        error = "LAYOUT_CARD_COUNT_MISMATCH"
    elif any(card["reocr_reasons"] or card["fare"] is None
             or len(card["address_lines"]) != 2 for card in cards):
        error = "LAYOUT_CARD_UNRESOLVED"
    elif sum(card["fare"] for card in cards) != expected_sum:
        error = "LAYOUT_FARE_SUM_MISMATCH"
    elif expected_direct_count is not None and sum(
        card["payment"] == "직접" for card in cards
    ) != expected_direct_count:
        error = "LAYOUT_DIRECT_PAYMENT_COUNT_MISMATCH"
    if error:
        layout.update(ok=False, status="FAIL_CLOSED", error_code=error)
    else:
        layout.update(ok=True, status="COMPLETE_LAYOUT_VALIDATED", error_code=None)
    layout["observed_fare_sum"] = sum(card["fare"] or 0 for card in cards)
    layout["observed_direct_count"] = sum(card["payment"] == "직접" for card in cards)
    layout["observed_payment_unknown_count"] = sum(
        card["payment"] == "미확인" for card in cards
    )
    layout["payment_semantics"] = "POSITIVE_DIRECT_EVIDENCE_ONLY"
    layout["direct_count_source"] = "CARD_LABELS"
    layout["direct_count_independently_verified"] = expected_direct_count is not None
    return layout



def _shadow_norm(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch not in " \t\r\n•●○·ㆍ")


def build_shadow_comparison(layout: dict, legacy_text: str) -> dict:
    """Compare layout output with the legacy text parser using the same base OCR responses.

    Diagnostic only: the legacy production fare-probe repair is intentionally not invoked.
    No raw addresses are emitted; only the names of mismatched fields are reported.
    """
    from daily_history_parser import parse_daily_history_text, validate_daily_history_document

    legacy = parse_daily_history_text(legacy_text)
    legacy_validation = validate_daily_history_document(legacy)
    legacy_map = {str(item.get("탑승시각") or ""): item for item in legacy.get("items") or []}
    layout_map = {str(card.get("start_time") or ""): card for card in layout.get("cards") or []}
    mismatches = []
    for start in sorted(set(legacy_map) | set(layout_map)):
        old = legacy_map.get(start)
        new = layout_map.get(start)
        fields = []
        if old is None:
            fields.append("missing_in_legacy")
        elif new is None:
            fields.append("missing_in_layout")
        else:
            if str(old.get("하차시각") or "") != str(new.get("end_time") or ""):
                fields.append("end_time")
            addresses = new.get("address_lines") or []
            if len(addresses) < 2:
                fields.append("layout_address_count")
            else:
                if _shadow_norm(old.get("출발지")) != _shadow_norm(addresses[0]):
                    fields.append("origin")
                if _shadow_norm(old.get("도착지")) != _shadow_norm(addresses[1]):
                    fields.append("destination")
            if int(old.get("요금") or 0) != int(new.get("fare") or 0):
                fields.append("fare")
            # Payment is not treated as an auto-vs-direct symmetric field.
            # Only positive direct-payment evidence is comparable; absence is unknown.
            old_direct = str(old.get("결제방식") or "") == "직접"
            new_direct = str(new.get("payment") or "") == "직접"
            if old_direct != new_direct:
                fields.append("payment_direct_disagreement")
        if fields:
            mismatches.append({"start_time": start, "fields": fields})

    return {
        "mode": "SAME_BASE_OCR_TEXT_NO_LEGACY_FARE_PROBE",
        "legacy_item_count": len(legacy_map),
        "layout_item_count": len(layout_map),
        "legacy_validation_ok": bool(legacy_validation.get("ok")),
        "legacy_parse_error_count": len(legacy.get("parse_errors") or []),
        "payment_comparison": "POSITIVE_DIRECT_EVIDENCE_ONLY",
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    # Optional assertions come from a separately reviewed Golden image, never from
    # the production parser. Runtime count/sum always come from OCR header evidence.
    ap.add_argument("--expected-count", type=int)
    ap.add_argument("--expected-sum", type=int)
    ap.add_argument("--expected-direct-count", type=int)
    ap.add_argument("--expected-date",
                    help="Optional independently reviewed YYYY-MM-DD assertion.")
    ap.add_argument("--prompt-key", action="store_true",
                    help="Read OCR API key from a hidden local prompt; do not save it.")
    ap.add_argument("--shadow-legacy", action="store_true",
                    help="Compare against legacy parser using the exact same base OCR text.")
    ap.add_argument("--latency-probe-extra-cards", type=int, default=0,
                    help="Diagnostic only: no-op OCR on N card crops (0..6) to measure serial latency.")
    args = ap.parse_args()
    if not 0 <= args.latency_probe_extra_cards <= 6:
        print(json.dumps({"ok": False, "error_code": "LATENCY_PROBE_RANGE_INVALID"}))
        return 2

    key = os.getenv("OCR_SPACE_API_KEY")
    if not key and args.prompt_key:
        key = getpass.getpass("OCR_SPACE_API_KEY: ").strip()
    if not key:
        print(json.dumps({"ok": False, "error_code": "OCR_SPACE_API_KEY_MISSING"}))
        return 2

    img = Image.open(args.image).convert("RGB")
    ow, oh = img.size
    split = [
        (1, (0, 0, ow, int(oh * 0.55))),
        (2, (0, int(oh * 0.45), ow, oh)),
    ]
    all_lines = []
    durations = []
    wall_durations = []
    legacy_chunks = []
    actual_calls = 0
    run_started = time.monotonic()
    for chunk_index, crop_box in split:
        crop = img.crop(crop_box)
        submitted = resize(crop)
        call_started = time.monotonic()
        result = ocr_overlay(crop, key)
        wall_durations.append(round((time.monotonic() - call_started) * 1000))
        actual_calls += 1
        parsed = result["ParsedResults"][0]
        legacy_chunks.append(
            f"---MAGI_OCR_CHUNK_{chunk_index}---\n{parsed.get('ParsedText') or ''}"
        )
        durations.append(result.get("ProcessingTimeInMilliseconds"))
        all_lines.extend(ocrspace_overlay_to_lines(
            parsed,
            chunk_index=chunk_index,
            crop_box=crop_box,
            submitted_size=submitted.size,
            original_size=img.size,
        ))

    header = extract_header_totals(all_lines)
    if not header["ok"]:
        print(json.dumps({
            "ok": False, "status": "FAIL_CLOSED", "error_code": header["error_code"],
            "actual_total_ocr_calls": actual_calls,
        }))
        return 3

    service_date = extract_header_date(all_lines)
    if not service_date["ok"]:
        print(json.dumps({
            "ok": False, "status": "FAIL_CLOSED",
            "error_code": service_date["error_code"],
            "actual_total_ocr_calls": actual_calls,
        }))
        return 3

    if ((args.expected_count is not None and
         header["expected_count"] != args.expected_count) or
        (args.expected_sum is not None and
         header["expected_sum"] != args.expected_sum)):
        print(json.dumps({
            "ok": False, "status": "FAIL_CLOSED",
            "error_code": "LAYOUT_HEADER_ASSERTION_MISMATCH",
            "header": header, "actual_total_ocr_calls": actual_calls,
        }))
        return 3
    if args.expected_date is not None and service_date["date"] != args.expected_date:
        print(json.dumps({
            "ok": False, "status": "FAIL_CLOSED",
            "error_code": "LAYOUT_DATE_ASSERTION_MISMATCH",
            "date": service_date, "actual_total_ocr_calls": actual_calls,
        }))
        return 3

    anchor_rescue = plan_missing_anchor_rescue(
        all_lines,
        original_size=img.size,
        expected_count=header["expected_count"],
    )
    anchor_rescue_calls = 0
    if (
        anchor_rescue.get("ok")
        and anchor_rescue.get("missing_anchor_count", 0) > 0
    ):
        for rescue in anchor_rescue.get("crops") or []:
            if actual_calls >= 8:
                break
            box = rescue["crop_px"]
            crop_box = (box["left"], box["top"], box["right"], box["bottom"])
            crop = img.crop(crop_box)
            submitted = resize(crop)
            call_started = time.monotonic()
            result = ocr_overlay(crop, key)
            wall_durations.append(round((time.monotonic() - call_started) * 1000))
            actual_calls += 1
            anchor_rescue_calls += 1
            durations.append(result.get("ProcessingTimeInMilliseconds"))
            all_lines.extend(ocrspace_overlay_to_lines(
                result["ParsedResults"][0],
                chunk_index=actual_calls,
                crop_box=crop_box,
                submitted_size=submitted.size,
                original_size=img.size,
            ))

    layout = build_card_layout(
        all_lines,
        original_size=img.size,
        expected_count=header["expected_count"],
        base_ocr_calls=actual_calls,
    )
    layout["anchor_rescue"] = {
        "attempted": bool(anchor_rescue_calls),
        "calls": anchor_rescue_calls,
        "plan": anchor_rescue,
    }
    if layout["ok"]:
        for card in layout["cards"]:
            if not card["reocr_reasons"]:
                continue
            if actual_calls >= layout["max_total_ocr_calls"]:
                layout.update(ok=False, status="FAIL_CLOSED",
                              error_code="LAYOUT_REOCR_BUDGET_EXCEEDED")
                break
            address_problem = any(
                reason.startswith("ADDRESS_LINES_") for reason in card["reocr_reasons"]
            )
            box = card["full_crop_px"] if address_problem else card["fare_crop_px"]
            crop_box = (box["left"], box["top"], box["right"], box["bottom"])
            crop = img.crop(crop_box)
            submitted = resize(crop)
            call_started = time.monotonic()
            result = ocr_overlay(crop, key)
            wall_durations.append(round((time.monotonic() - call_started) * 1000))
            actual_calls += 1
            durations.append(result.get("ProcessingTimeInMilliseconds"))
            lines = ocrspace_overlay_to_lines(
                result["ParsedResults"][0],
                chunk_index=actual_calls,
                crop_box=crop_box,
                submitted_size=submitted.size,
                original_size=img.size,
            )
            if not reconcile_reocr(card, lines):
                layout.update(ok=False, status="FAIL_CLOSED",
                              error_code="LAYOUT_CARD_REOCR_UNRESOLVED")
                break
    if layout.get("ok") and args.latency_probe_extra_cards:
        requested = args.latency_probe_extra_cards
        if actual_calls + requested > layout["max_total_ocr_calls"]:
            layout.update(ok=False, status="FAIL_CLOSED",
                          error_code="LATENCY_PROBE_BUDGET_EXCEEDED")
        else:
            for card in layout["cards"][:requested]:
                box = card["full_crop_px"]
                crop_box = (box["left"], box["top"], box["right"], box["bottom"])
                crop = img.crop(crop_box)
                call_started = time.monotonic()
                probe_result = ocr_overlay(crop, key)
                wall_durations.append(round((time.monotonic() - call_started) * 1000))
                actual_calls += 1
                durations.append(probe_result.get("ProcessingTimeInMilliseconds"))

    layout = verify_layout(
        layout, header["expected_count"], header["expected_sum"],
        args.expected_direct_count, actual_calls,
    )
    layout["header"] = header
    layout["date"] = service_date["date"]
    layout["date_source"] = service_date["source"]
    layout["ocrspace_duration_ms"] = durations
    layout["ocr_call_wall_ms"] = wall_durations
    layout["total_wall_duration_ms"] = round((time.monotonic() - run_started) * 1000)
    layout["diagnostic_latency_probe_extra_calls"] = args.latency_probe_extra_cards
    if args.shadow_legacy:
        layout["legacy_shadow"] = build_shadow_comparison(
            layout, "\n".join(legacy_chunks)
        )
    layout["source_image"] = Path(args.image).name
    print(json.dumps(layout, ensure_ascii=False, indent=2))
    return 0 if layout.get("ok") else 3


if __name__ == "__main__":
    raise SystemExit(main())
