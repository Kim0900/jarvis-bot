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

import asyncio
import threading
import time

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


# Task #206 (D4): keep the read-classify-insert sequence of two batches of the same
# process from interleaving.  The request server (plain ``HTTPServer``) already handles
# one ingestion request at a time, so this guard is defensive today; it is a
# threading.Lock (polled without blocking the event loop) because each request runs its
# own ``asyncio.run`` and an asyncio.Lock cannot be shared across loops.  It is a
# SINGLE-PROCESS guard only.  NOT covered (accepted, CASSANDRA 5252 / operating rule): two
# instances overlapping during a deploy (no upload around a deploy + a duplicate check
# before the first upload) and the Telegram-side writers that insert into raw_calls
# without this gate.
_WRITE_LOCK = threading.Lock()
WRITE_LOCK_TIMEOUT_SECONDS = 60.0


class raw_call_write_guard:
    def __init__(self, timeout: float | None = None):
        self._timeout = WRITE_LOCK_TIMEOUT_SECONDS if timeout is None else timeout

    async def __aenter__(self):
        deadline = time.monotonic() + self._timeout
        while not _WRITE_LOCK.acquire(blocking=False):
            if time.monotonic() >= deadline:
                raise RuntimeError("RAW_CALL_WRITE_LOCK_TIMEOUT")
            await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        _WRITE_LOCK.release()
        return False


def find_in_batch_strong_duplicates(payloads):
    """Pairs of payloads inside ONE batch that are STRONG duplicates of each other.

    Only STRONG evidence counts (same policy function as everything else); WEAK pairs
    are deliberately not flagged here so ordinary back-to-back trips or same-minute
    receipts are not newly blocked.  daily_total rows never take part.
    """
    pairs = []
    items = [p for p in (payloads or []) if p.get("raw_row_type") != "daily_total"]
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            strength = identity_strength(items[i], items[j])
            if strength in STRONG_STRENGTHS:
                pairs.append({
                    "first": _brief(items[i]),
                    "second": _brief(items[j]),
                    "strength": strength,
                })
    return pairs


def _brief(payload):
    return {
        "날짜": payload.get("날짜"),
        "배차시각": payload.get("배차시각"),
        "하차시각": payload.get("하차시각"),
        "요금": payload.get("요금"),
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
    if payloads and not source_id:
        # Task #206: without a source id the cross-source lookup below is skipped and the
        # whole batch would be inserted unchecked.  Refuse before anything is written.
        raise RuntimeError("RAW_CALL_SOURCE_ID_REQUIRED")

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

    in_batch = find_in_batch_strong_duplicates(prepared)
    if in_batch:
        # Task #206 / CASSANDRA 5126: a STRONG duplicate *inside* one batch is ambiguous
        # about which row is the true one, so quarantine the whole batch (0 writes)
        # instead of silently dropping a row.
        return {
            "ok": False,
            "quarantine": True,
            "error_code": "RAW_CALL_IN_BATCH_DUPLICATE",
            "inserted_count": 0,
            "duplicate_skipped_count": 0,
            "covered_count": len(prepared),
            "in_batch_duplicate_count": len(in_batch),
            "in_batch_duplicates": in_batch[:20],
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
