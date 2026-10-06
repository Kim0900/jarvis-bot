"""Task #174: cross-source raw_calls identity gate.

This module deliberately does not invent identity semantics. It only classifies
candidates using canonical_identity_v1.identity_strength(), which is the single
approved identity policy for Task #174.

Actions:
- STRONG_FULL_INTERVAL / STRONG_PARTIAL_ADDRESS -> duplicate_skipped
- WEAK_TIME_ONLY -> quarantine (no automatic write)
- no match -> novel
"""

from __future__ import annotations

from canonical_identity_v1 import identity_strength

STRONG_STRENGTHS = {
    "STRONG_FULL_INTERVAL",
    "STRONG_PARTIAL_ADDRESS",
}


def partition_raw_call_payloads(existing_rows, payloads, source_id=None):
    """Partition candidate payloads without mutating input rows.

    Rows from the same source_id are ignored because source-level retry handling
    owns that lifecycle. Cross-source copies must still be compared.
    """
    novel = []
    duplicate_skipped = []
    weak = []

    for index, payload in enumerate(payloads or []):
        strong_hits = []
        weak_hits = []

        for row in existing_rows or []:
            if source_id and row.get("source_id") == source_id:
                continue
            if row.get("raw_row_type") == "daily_total":
                continue

            strength = identity_strength(row, payload)
            if strength in STRONG_STRENGTHS:
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
            duplicate_skipped.append({
                "payload_index": index,
                "fare": payload.get("요금"),
                "hits": strong_hits,
            })
        elif weak_hits:
            weak.append({
                "payload_index": index,
                "fare": payload.get("요금"),
                "hits": weak_hits,
            })
        else:
            novel.append(payload)

    return {
        "novel": novel,
        "duplicate_skipped": duplicate_skipped,
        "weak": weak,
    }


async def persist_raw_call_batch(
    payloads,
    source_id,
    *,
    select_rows,
    bulk_insert,
    calc_service_date,
    validate_call_payload,
):
    """Prepare, classify, and atomically write a Drive-backed raw_calls batch.

    select_rows(date_value) must return existing raw_calls for that calendar date.
    bulk_insert(rows) must insert the supplied list as one database operation.
    """
    prepared = []
    for original in payloads or []:
        payload = dict(original)
        if source_id:
            payload["source_id"] = source_id
        payload.update(calc_service_date(payload.get("날짜"), payload.get("배차시각")))
        valid, reason = validate_call_payload(payload)
        if not valid:
            payload["raw_row_type"] = "unclassified"
            payload["비고"] = (payload.get("비고") or "") + f" [검증실패: {reason}]"
        prepared.append(payload)

    if not prepared:
        return {
            "ok": True,
            "inserted_count": 0,
            "duplicate_skipped_count": 0,
            "covered_count": 0,
            "duplicate_skipped": [],
        }

    to_insert = prepared
    duplicate_skipped = []

    if source_id:
        existing_rows = []
        dates = sorted({str(p.get("날짜")) for p in prepared if p.get("날짜")})
        for date_value in dates:
            rows = await select_rows(date_value)
            if rows is None:
                raise RuntimeError(f"RAW_CALL_IDENTITY_LOOKUP_FAILED:{date_value}")
            existing_rows.extend(list(rows))

        partitioned = partition_raw_call_payloads(
            existing_rows,
            prepared,
            source_id=source_id,
        )
        if partitioned["weak"]:
            return {
                "ok": False,
                "quarantine": True,
                "error_code": "RAW_CALL_IDENTITY_AMBIGUOUS",
                "inserted_count": 0,
                "duplicate_skipped_count": len(partitioned["duplicate_skipped"]),
                "covered_count": len(prepared),
                "weak_count": len(partitioned["weak"]),
                "weak_candidates": partitioned["weak"][:20],
                "duplicate_skipped": partitioned["duplicate_skipped"][:20],
            }

        to_insert = partitioned["novel"]
        duplicate_skipped = partitioned["duplicate_skipped"]

    inserted = []
    if to_insert:
        inserted = await bulk_insert(to_insert)
        if not isinstance(inserted, list) or len(inserted) != len(to_insert):
            raise RuntimeError(
                "RAW_CALL_BATCH_INSERT_MISMATCH:"
                f"expected={len(to_insert)} "
                f"actual={len(inserted) if isinstance(inserted, list) else 'non_list'}"
            )

    return {
        "ok": True,
        "inserted_count": len(to_insert),
        "duplicate_skipped_count": len(duplicate_skipped),
        "covered_count": len(prepared),
        "duplicate_skipped": duplicate_skipped[:20],
    }
