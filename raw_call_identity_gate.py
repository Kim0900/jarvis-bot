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
