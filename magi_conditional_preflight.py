"""Pure, fail-closed preflight rules for MAGI Tasks 180, 185 and 186.

No database writes, no mutation of historical records. This module is deliberately
separate from the production ingestion pipeline pending independent verification.
"""
from datetime import date
from typing import Iterable


def unique_page_date(evidence_dates: Iterable[str | None]) -> str | None:
    """Task 180: never guess a date, including when evidence is missing or mixed."""
    values = list(evidence_dates)
    if not values or any(value is None for value in values):
        return None
    parsed = set()
    for value in values:
        try:
            parsed.add(date.fromisoformat(value).isoformat())
        except (ValueError, TypeError):
            return None
    return next(iter(parsed)) if len(parsed) == 1 else None


def close_decision(*, kakao_pages_final: bool, s700_all_classified: bool,
                   gpx_coverage_ok: bool, unresolved_evidence: bool,
                   no_operation_confirmed: bool = False,
                   exception_approval: dict | None = None) -> str:
    """Task 185: a new evidence snapshot always gets a fresh decision.

    Exception approval is explicit, attributable, and distinct from COMPLETE.
    Caller must persist evidence snapshots and state transitions separately.
    """
    if no_operation_confirmed and not unresolved_evidence:
        return "NO_OPERATION"
    if (kakao_pages_final and s700_all_classified and gpx_coverage_ok
            and not unresolved_evidence):
        return "COMPLETE"
    if exception_approval and all(exception_approval.get(k) for k in
                                  ("approver", "approved_at", "reason", "evidence_id")):
        return "COMPLETE_EXCEPTION"
    return "NEEDS_CONFIRM"


def legacy_terminal_candidate(frame: dict) -> bool:
    """Task 186: eligibility *only*, not acceptance or decoder bypass.

    The caller MUST additionally run the original checksum, exact 98-byte ASCII,
    re-decode consistency, fare-copy, valid-time and duplicate checks.
    """
    return (frame.get("decoder_version", "").startswith("0.4")
            and frame.get("primary_state") == "1C"
            and frame.get("secondary_state") == "08"
            and frame.get("aux_flag") == "01")
