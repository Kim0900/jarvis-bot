"""Shared canonical identity / precedence policy for raw_calls v1.

Purpose:
- One deterministic duplicate policy shared by Task #164 ingestion and Task #27 reporting.
- Preserve every raw provenance row; canonicalization is selection-only.
- Auto-collapse only STRONG duplicate evidence. WEAK candidates remain visible for review.
"""

from __future__ import annotations

IMAGE_BACKED_SOURCES = {
    "drive_ocr_layout_v1",
    "drive_ocr_tesseract",
}
_KAKAO_SOURCES = IMAGE_BACKED_SOURCES | {"app_ocr_individual"}


def _norm_text(value):
    return "".join(str(value or "").replace("·", "").replace(".", "").split()).lower()


def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def platform_key(row):
    call_type = _norm_text(row.get("콜유형"))
    if "카카오" in call_type:
        return "KAKAO"
    if "우버" in call_type:
        return "UBER"
    if "배회" in call_type:
        return "ROAM"
    if not call_type and row.get("data_source") in _KAKAO_SOURCES:
        return "KAKAO"
    return "UNKNOWN"


def evidence_rank(row, completed_source_ids=None):
    """Higher wins when two rows are STRONG duplicates."""
    completed_source_ids = completed_source_ids or set()
    source = row.get("data_source")
    verified = row.get("verify_status") == "verified"
    source_id = row.get("source_id")

    if source and source.startswith("manual_correction") and verified:
        return 600
    if source == "drive_ocr_layout_v1" and source_id in completed_source_ids:
        return 550
    if source == "drive_ocr_tesseract" and source_id in completed_source_ids:
        return 500
    if source == "app_ocr_individual":
        return 450
    if source == "argos_reconstructed" and verified:
        return 400
    if verified:
        return 350
    if source_id:
        return 300
    if row.get("status") == "confirmed":
        return 200
    return 100


def identity_strength(a, b):
    """Return STRONG_FULL_INTERVAL / STRONG_PARTIAL_ADDRESS / WEAK_TIME_ONLY / None."""
    if str(a.get("날짜")) != str(b.get("날짜")):
        return None
    if platform_key(a) != platform_key(b):
        return None

    a_fare = _int_or_none(a.get("요금"))
    b_fare = _int_or_none(b.get("요금"))
    fare_missing = a_fare is None or b_fare is None

    # Task #174 production Golden finding:
    # A legacy weak row can have fare=NULL while a replay of the same source is
    # parsed more completely and recovers a real fare. Missing fare must never
    # become STRONG evidence, but matching time evidence must still quarantine
    # the replay instead of treating it as novel.
    if not fare_missing and a_fare != b_fare:
        return None

    a_start, a_end = a.get("배차시각"), a.get("하차시각")
    b_start, b_end = b.get("배차시각"), b.get("하차시각")

    if fare_missing:
        any_time_overlap = bool(
            (a_start and a_start in (b_start, b_end))
            or (a_end and a_end in (b_start, b_end))
            or (b_start and b_start in (a_start, a_end))
            or (b_end and b_end in (a_start, a_end))
        )
        return "WEAK_TIME_ONLY" if any_time_overlap else None

    if a_start and a_end and b_start and b_end:
        if a_start == b_start and a_end == b_end:
            return "STRONG_FULL_INTERVAL"
        return None

    a_from, a_to = _norm_text(a.get("출발지")), _norm_text(a.get("도착지"))
    b_from, b_to = _norm_text(b.get("출발지")), _norm_text(b.get("도착지"))
    same_addresses = bool(a_from and a_to and a_from == b_from and a_to == b_to)

    same_position_time = (
        (a_start and b_start and a_start == b_start)
        or (a_end and b_end and a_end == b_end)
    )
    if same_addresses and same_position_time:
        return "STRONG_PARTIAL_ADDRESS"

    any_time_overlap = bool(
        (a_start and a_start in (b_start, b_end))
        or (a_end and a_end in (b_start, b_end))
        or (b_start and b_start in (a_start, a_end))
        or (b_end and b_end in (a_start, a_end))
    )
    if any_time_overlap:
        return "WEAK_TIME_ONLY"
    return None


def is_strong_duplicate(a, b):
    return identity_strength(a, b) in {
        "STRONG_FULL_INTERVAL",
        "STRONG_PARTIAL_ADDRESS",
    }


def select_canonical(rows, completed_source_ids=None):
    """Return canonical rows plus diagnostics; input rows are never mutated."""
    completed_source_ids = completed_source_ids or set()
    selected = []
    suppressed = []
    weak = []

    for row in rows:
        stronger = None
        weak_hits = []
        for other in rows:
            if other is row or other.get("id") == row.get("id"):
                continue
            strength = identity_strength(row, other)
            if strength == "WEAK_TIME_ONLY":
                weak_hits.append(other.get("id"))
                continue
            if strength not in {"STRONG_FULL_INTERVAL", "STRONG_PARTIAL_ADDRESS"}:
                continue
            row_key = (evidence_rank(row, completed_source_ids), int(row.get("id") or 0))
            other_key = (evidence_rank(other, completed_source_ids), int(other.get("id") or 0))
            if other_key > row_key:
                stronger = {
                    "row_id": row.get("id"),
                    "winner_id": other.get("id"),
                    "strength": strength,
                }
                break

        if stronger:
            suppressed.append(stronger)
        else:
            selected.append(row)
        if weak_hits:
            weak.append({"row_id": row.get("id"), "candidate_ids": sorted(set(weak_hits))})

    return {
        "rows": selected,
        "suppressed": suppressed,
        "weak_candidates": weak,
    }
