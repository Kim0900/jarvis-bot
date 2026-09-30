"""Task #164 production primary cutover adapter."""

from __future__ import annotations

LAYOUT_COMPLETE_STATUS = "COMPLETE_LAYOUT_VALIDATED"
ACTION_USE_LAYOUT = "USE_LAYOUT"
ACTION_FAIL_CLOSED = "FAIL_CLOSED"
ACTION_FALLBACK_LEGACY = "FALLBACK_LEGACY"

_KAKAO_SOURCES = {
    "app_ocr_individual",
    "drive_ocr_tesseract",
    "drive_ocr_layout_v1",
}


def classify_layout_result(http_status, payload, transport_error=False):
    if transport_error:
        return ACTION_FALLBACK_LEGACY
    if http_status is None:
        return ACTION_FAIL_CLOSED
    if http_status >= 500:
        return ACTION_FALLBACK_LEGACY
    payload = payload or {}
    if http_status == 200 and payload.get("ok") and payload.get("status") == LAYOUT_COMPLETE_STATUS:
        return ACTION_USE_LAYOUT
    return ACTION_FAIL_CLOSED


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _norm(value):
    return "".join(str(value or "").split()).lower()


def independent_header_disagreements(layout, legacy):
    legacy = legacy or {}
    out = []
    if legacy.get("날짜") and layout.get("date") and str(legacy.get("날짜")) != str(layout.get("date")):
        out.append("date")
    header = layout.get("header") or {}
    layout_count = _int_or_none(header.get("expected_count"))
    layout_sum = _int_or_none(header.get("expected_sum"))
    legacy_count = _int_or_none(legacy.get("표시건수"))
    legacy_sum = _int_or_none(legacy.get("표시금액"))
    if legacy_count is not None and layout_count is not None and legacy_count != layout_count:
        out.append("header_count")
    if legacy_sum is not None and layout_sum is not None and legacy_sum != layout_sum:
        out.append("header_sum")
    return out


def is_kakao_existing_row(row):
    if row.get("raw_row_type") == "daily_total":
        return False
    call_type = _norm(row.get("콜유형"))
    if any(x in call_type for x in ("우버", "배회", "미분류")):
        return False
    if "카카오" in call_type:
        return True
    return not call_type and row.get("data_source") in _KAKAO_SOURCES


def _time_set(row):
    return {
        str(value)
        for value in (row.get("배차시각"), row.get("하차시각"))
        if value
    }


def find_kakao_overlap_candidates(rows, payloads, source_id):
    """Return only strong same-platform duplicate candidates.

    Identity rule:
    - existing row must be Kakao (not Uber/roaming/unclassified)
    - fare must match exactly
    - at least one start/end clock value must overlap

    Using both start and end accommodates historical rows where the old
    app OCR stored the card end-time in the dispatch-time column.
    """
    hits = []
    for row in rows or []:
        if row.get("source_id") == source_id:
            continue
        if not is_kakao_existing_row(row):
            continue
        existing_fare = _int_or_none(row.get("요금"))
        existing_times = _time_set(row)
        if existing_fare is None or not existing_times:
            continue
        for payload in payloads:
            if existing_fare != _int_or_none(payload.get("요금")):
                continue
            if existing_times.intersection(_time_set(payload)):
                hits.append({
                    "existing_id": row.get("id"),
                    "existing_source_id": row.get("source_id"),
                    "fare": existing_fare,
                    "time_overlap": sorted(existing_times.intersection(_time_set(payload))),
                })
                break
    return hits


def layout_to_daily_history(layout):
    header = layout.get("header") or {}
    cards = layout.get("cards") or []
    items = []
    for card in cards:
        addresses = list(card.get("address_lines") or [])
        while len(addresses) < 2:
            addresses.append(None)
        items.append({
            "탑승시각": card.get("start_time"),
            "하차시각": card.get("end_time"),
            "출발지": addresses[0],
            "도착지": addresses[1],
            "요금": _int_or_none(card.get("fare")),
            "결제방식": "직접" if card.get("payment") == "직접" else "미확인",
        })
    return {
        "format": "daily_history",
        "날짜": layout.get("date"),
        "표시건수": _int_or_none(header.get("expected_count")),
        "표시금액": _int_or_none(header.get("expected_sum")),
        "items": items,
    }


async def persist_layout_primary(
    parsed,
    source_id,
    *,
    select_rows,
    bulk_insert,
    mark_completed,
    rollback_source_rows,
    calc_service_date,
    validate_call_payload,
):
    if not source_id:
        return {"ok": False, "error_code": "LAYOUT_PRIMARY_SOURCE_ID_REQUIRED"}
    date_value = parsed.get("날짜")
    if not date_value:
        return {"ok": False, "error_code": "LAYOUT_PRIMARY_DATE_REQUIRED"}

    existing_source = await select_rows({"source_id": f"eq.{source_id}", "limit": "1"})
    if existing_source:
        return {"ok": False, "error_code": "LAYOUT_PRIMARY_SOURCE_ROWS_ALREADY_EXIST"}

    payloads = []
    for item in parsed.get("items") or []:
        payload = {
            "날짜": date_value,
            "배차시각": item.get("탑승시각"),
            "하차시각": item.get("하차시각"),
            "출발지": item.get("출발지"),
            "도착지": item.get("도착지"),
            "요금": item.get("요금"),
            "콜유형": "카카오T",
            "비고": "직접결제" if item.get("결제방식") == "직접" else None,
            "data_source": "drive_ocr_layout_v1",
            "source_id": source_id,
        }
        payload.update(calc_service_date(payload["날짜"], payload["배차시각"]))
        valid, reason = validate_call_payload(payload)
        if not valid:
            return {
                "ok": False,
                "error_code": "LAYOUT_PRIMARY_ROW_VALIDATION_FAILED",
                "reason": reason,
            }
        payloads.append(payload)

    expected = _int_or_none(parsed.get("표시건수"))
    expected_sum = _int_or_none(parsed.get("표시금액"))
    if not payloads or expected is None or expected_sum is None:
        return {"ok": False, "error_code": "LAYOUT_PRIMARY_BATCH_METADATA_MISSING"}
    if len(payloads) != expected:
        return {"ok": False, "error_code": "LAYOUT_PRIMARY_COUNT_MISMATCH"}
    if sum(int(p.get("요금") or 0) for p in payloads) != expected_sum:
        return {"ok": False, "error_code": "LAYOUT_PRIMARY_SUM_MISMATCH"}

    date_rows = await select_rows({"날짜": f"eq.{date_value}", "limit": "500"})
    overlaps = find_kakao_overlap_candidates(date_rows, payloads, source_id)
    if overlaps:
        return {
            "ok": False,
            "quarantine": True,
            "error_code": "LAYOUT_KAKAO_OVERLAP_QUARANTINE",
            "overlap_count": len(overlaps),
            "overlap_candidates": overlaps[:20],
        }

    try:
        inserted = await bulk_insert(payloads)
        if not isinstance(inserted, list) or len(inserted) != len(payloads):
            raise RuntimeError("LAYOUT_PRIMARY_BULK_INSERT_MISMATCH")
        await mark_completed(len(payloads))
    except Exception as exc:
        try:
            await rollback_source_rows(source_id)
        finally:
            return {
                "ok": False,
                "error_code": "LAYOUT_PRIMARY_PERSISTENCE_FAILED",
                "exception_type": type(exc).__name__,
            }

    return {
        "ok": True,
        "saved_count": len(payloads),
        "date": date_value,
        "displayed_count": expected,
        "displayed_amount": expected_sum,
    }
