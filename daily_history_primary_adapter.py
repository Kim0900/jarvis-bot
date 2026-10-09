"""Task #164 production primary cutover adapter."""

from __future__ import annotations

from datetime import date as _date

from canonical_identity_v1 import identity_strength, platform_key

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



def _resolve_card_date(header_date_value, date_hint):
    """Resolve an explicit M/D card prefix against the independently parsed header date.

    Kakao daily-history pages may include next-calendar-day trips on the prior
    service-day page. Only the header date itself or the immediately following
    calendar day is accepted. Anything else fails closed.
    """
    try:
        header_date = _date.fromisoformat(str(header_date_value))
    except (TypeError, ValueError):
        return None

    if not date_hint:
        return header_date.isoformat()

    month = _int_or_none(date_hint.get("month") if isinstance(date_hint, dict) else None)
    day = _int_or_none(date_hint.get("day") if isinstance(date_hint, dict) else None)
    if month is None or day is None:
        return None

    candidates = []
    for year in (header_date.year - 1, header_date.year, header_date.year + 1):
        try:
            candidates.append(_date(year, month, day))
        except ValueError:
            continue

    allowed = [
        candidate for candidate in candidates
        if 0 <= (candidate - header_date).days <= 1
    ]
    if len(allowed) != 1:
        return None
    return allowed[0].isoformat()

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


def find_kakao_overlap_candidates(rows, payloads, source_id):
    """Shared v1 identity policy: STRONG duplicates and WEAK ambiguity candidates."""
    hits = []
    for row in rows or []:
        if row.get("source_id") == source_id:
            continue
        if row.get("raw_row_type") == "daily_total":
            continue
        if platform_key(row) != "KAKAO":
            continue
        for payload in payloads:
            candidate = dict(payload)
            candidate.setdefault("콜유형", "카카오T")
            strength = identity_strength(row, candidate)
            if strength:
                hits.append({
                    "existing_id": row.get("id"),
                    "existing_source_id": row.get("source_id"),
                    "fare": _int_or_none(row.get("요금")),
                    "strength": strength,
                })
                break
    return hits



def partition_kakao_payloads(rows, payloads, source_id):
    """Partition a validated layout batch by shared canonical identity.

    STRONG identity means the trip is already represented by another source and
    is skipped, not duplicated. WEAK_TIME_ONLY is ambiguous and blocks the whole
    batch. This keeps raw provenance append-only without creating known duplicate
    trips when a full daily-history screenshot overlaps earlier partial uploads.
    """
    novel = []
    covered = []
    weak = []
    for index, payload in enumerate(payloads):
        candidate = dict(payload)
        candidate.setdefault("콜유형", "카카오T")
        strong_hits = []
        weak_hits = []
        for row in rows or []:
            if row.get("source_id") == source_id:
                continue
            if row.get("raw_row_type") == "daily_total":
                continue
            if platform_key(row) != "KAKAO":
                continue
            strength = identity_strength(row, candidate)
            if strength and str(strength).startswith("STRONG_"):
                strong_hits.append({
                    "existing_id": row.get("id"),
                    "existing_source_id": row.get("source_id"),
                    "strength": strength,
                })
            elif strength == "WEAK_TIME_ONLY":
                weak_hits.append({
                    "existing_id": row.get("id"),
                    "existing_source_id": row.get("source_id"),
                    "strength": strength,
                })
        if strong_hits:
            covered.append({
                "payload_index": index,
                "fare": _int_or_none(payload.get("요금")),
                "hits": strong_hits,
            })
        elif weak_hits:
            weak.append({
                "payload_index": index,
                "fare": _int_or_none(payload.get("요금")),
                "hits": weak_hits,
            })
        else:
            novel.append(payload)
    return {"novel": novel, "covered": covered, "weak": weak}

def layout_to_daily_history(layout):
    header = layout.get("header") or {}
    cards = layout.get("cards") or []
    header_date = layout.get("date")
    items = []
    for card in cards:
        addresses = list(card.get("address_lines") or [])
        while len(addresses) < 2:
            addresses.append(None)
        card_date = _resolve_card_date(header_date, card.get("date_hint"))
        if card_date is None:
            return {
                "format": "daily_history",
                "날짜": header_date,
                "표시건수": _int_or_none(header.get("expected_count")),
                "표시금액": _int_or_none(header.get("expected_sum")),
                "items": [],
                "error_code": "LAYOUT_CARD_DATE_OUT_OF_RANGE",
                "error_card_index": card.get("card_index"),
            }
        items.append({
            "날짜": card_date,
            "탑승시각": card.get("start_time"),
            "하차시각": card.get("end_time"),
            "출발지": addresses[0],
            "도착지": addresses[1],
            "요금": _int_or_none(card.get("fare")),
            "결제방식": "직접" if card.get("payment") == "직접" else "미확인",
        })
    return {
        "format": "daily_history",
        "날짜": header_date,
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
    if parsed.get("error_code"):
        return {"ok": False, "error_code": parsed.get("error_code")}
    date_value = parsed.get("날짜")
    if not date_value:
        return {"ok": False, "error_code": "LAYOUT_PRIMARY_DATE_REQUIRED"}

    existing_source = await select_rows({"source_id": f"eq.{source_id}", "limit": "1"})
    if existing_source:
        return {"ok": False, "error_code": "LAYOUT_PRIMARY_SOURCE_ROWS_ALREADY_EXIST"}

    payloads = []
    for item in parsed.get("items") or []:
        item_date = item.get("날짜") or date_value
        payload = {
            "날짜": item_date,
            "배차시각": item.get("탑승시각"),
            "하차시각": item.get("하차시각"),
            "출발지": item.get("출발지"),
            "도착지": item.get("도착지"),
            "요금": item.get("요금"),
            "콜유형": "카카오T",
            "비고": "직접결제" if item.get("결제방식") == "직접" else None,
            "data_source": "drive_ocr_layout_v1",
            "raw_row_type": "trip",
            "source_id": source_id,
        }
        # Task180: keep the independently parsed page-header date separate.\n        # Enable only after the nullable column migration.\n        import os\n        if os.getenv("TASK180_LEDGER_DATE_ENABLED", "").lower() == "true":\n            payload["ledger_date"] = date_value\n        payload.update(calc_service_date(payload["날짜"], payload["배차시각"]))
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

    date_rows = []
    for candidate_date in sorted({str(p.get("날짜")) for p in payloads if p.get("날짜")}):
        rows = await select_rows({"날짜": f"eq.{candidate_date}", "limit": "500"})
        if rows:
            date_rows.extend(rows)
    partitioned = partition_kakao_payloads(date_rows, payloads, source_id)
    weak = partitioned["weak"]
    if weak:
        return {
            "ok": False,
            "quarantine": True,
            "error_code": "LAYOUT_KAKAO_IDENTITY_AMBIGUOUS",
            "overlap_count": len(weak),
            "overlap_candidates": weak[:20],
        }

    to_insert = partitioned["novel"]
    covered = partitioned["covered"]
    if len(to_insert) + len(covered) != expected:
        return {
            "ok": False,
            "error_code": "LAYOUT_PRIMARY_COVERAGE_COUNT_MISMATCH",
        }

    try:
        inserted = [] if not to_insert else await bulk_insert(to_insert)
        if not isinstance(inserted, list) or len(inserted) != len(to_insert):
            raise RuntimeError("LAYOUT_PRIMARY_BULK_INSERT_MISMATCH")
        await mark_completed(len(to_insert))
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
        "saved_count": len(to_insert),
        "covered_count": len(payloads),
        "duplicate_skipped_count": len(covered),
        "duplicate_skipped": covered[:20],
        "date": date_value,
        "displayed_count": expected,
        "displayed_amount": expected_sum,
    }
