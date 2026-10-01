"""Task #164 image-ingestion retry policy.

Deterministic content/parser failures are terminal for the same immutable Drive
source_id. Transient provider/server failures remain retryable. A manual caller
may explicitly force retry after code/parser improvements.
"""

TERMINAL_PREFIX = "TERMINAL:"
RETRYABLE_PREFIX = "RETRYABLE:"

_TERMINAL_ERRORS = {
    "형식판별실패",
    "UBER_FARE_MISMATCH",
    "UBER_COMPACT_REQUIRED_FIELD_MISSING",
    "UBER_COMPACT_FARE_AMBIGUOUS",
}

_TERMINAL_ERROR_CODES = {
    "DAILY_HISTORY_COUNT_MISMATCH",
    "DAILY_HISTORY_ANCHOR_MISMATCH",
    "DAILY_HISTORY_AMOUNT_MISMATCH",
    "DAILY_HISTORY_DATE_MISSING",
    "DAILY_HISTORY_DATE_INVALID",
    "LAYOUT_CARD_COUNT_MISMATCH",
    "LAYOUT_HEADER_MISSING_OR_AMBIGUOUS",
    "LAYOUT_PRIMARY_FAIL_CLOSED",
    "LAYOUT_INDEPENDENT_HEADER_DISAGREEMENT",
    "LAYOUT_KAKAO_DUPLICATE_QUARANTINE",
    "LAYOUT_KAKAO_IDENTITY_AMBIGUOUS",
}


def is_terminal_last_error(last_error):
    return str(last_error or "").startswith(TERMINAL_PREFIX)


def mark_terminal(message):
    text = str(message or "UNKNOWN_TERMINAL_IMAGE_FAILURE")
    return text if text.startswith(TERMINAL_PREFIX) else TERMINAL_PREFIX + text


def mark_retryable(message):
    text = str(message or "UNKNOWN_RETRYABLE_IMAGE_FAILURE")
    return text if text.startswith(RETRYABLE_PREFIX) else RETRYABLE_PREFIX + text


def classify_result_failure(result):
    """Return 'terminal' or 'retryable' for a completed parser decision."""
    result = result or {}
    if result.get("error_stage") == "daily_history_validation":
        return "terminal"
    code = str(result.get("error_code") or "")
    if code in _TERMINAL_ERROR_CODES:
        return "terminal"
    error = str(result.get("error") or "")
    if error in _TERMINAL_ERRORS:
        return "terminal"
    # A parser reached a stable format and rejected the immutable content.
    if result.get("format") in {
        "daily_history",
        "uber_trip_detail",
        "kakao_trip_detail",
        "meter_receipt",
    }:
        return "terminal"
    return "terminal" if error == "형식판별실패" else "retryable"
