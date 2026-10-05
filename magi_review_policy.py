"""Deterministic routing policy for MAGI verification.

External LLMs are advisory only. The control plane decides which authority owns
the verification before any provider call can occur.
"""
from __future__ import annotations


CASSANDRA_PREFIXES = (
    "PENDING_CASSANDRA",
    "CASSANDRA_",
)


def classify_magi_review_lane(task: dict) -> str:
    """Return one of: skip, cassandra, magi.

    - CASSANDRA owns tasks that explicitly require verification or already sit
      in a CASSANDRA state, including FINAL/HOLD variants.
    - MAGI owns the remaining unverified VERIFICATION tasks.
    - Completed/non-verification/already-verified rows are skipped.
    """
    if not isinstance(task, dict):
        return "skip"
    if str(task.get("status") or "") != "VERIFICATION":
        return "skip"
    if task.get("verified_by"):
        return "skip"

    verification_status = str(task.get("verification_status") or "").upper()
    if bool(task.get("verification_required")):
        return "cassandra"
    if any(verification_status.startswith(prefix) for prefix in CASSANDRA_PREFIXES):
        return "cassandra"

    return "magi"


def external_review_enabled(value: str | None) -> bool:
    """Explicit opt-in only. Default/empty/unknown values are disabled."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
