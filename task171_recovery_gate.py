"""Task #171 one-source controlled recovery gate.

This module does not process or write data. It only decides whether a single,
explicitly configured terminal image source may receive one automatic retry and
whether layout-primary may be used for that same source.

The retry override is fail-closed and tied to the ledger timestamp observed
before enabling the gate. Any processing attempt changes updated_at, making the
override ineligible for subsequent automatic retries.
"""
from __future__ import annotations

import os
from datetime import datetime

ENV_SOURCE_ID = "TASK171_RECOVERY_SOURCE_ID"
ENV_MAX_LEDGER_UPDATED_AT = "TASK171_RECOVERY_MAX_LEDGER_UPDATED_AT"
ENV_LAYOUT_PRIMARY = "TASK171_RECOVERY_LAYOUT_PRIMARY"


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def should_force_retry(
    source_id: str | None,
    prior_state: dict | None,
    *,
    environ: dict[str, str] | None = None,
) -> bool:
    """Allow one automatic retry only for the exact pre-authorized ledger state."""
    env = os.environ if environ is None else environ
    expected_source = str(env.get(ENV_SOURCE_ID) or "").strip()
    cutoff_text = str(env.get(ENV_MAX_LEDGER_UPDATED_AT) or "").strip()

    if not expected_source or source_id != expected_source:
        return False
    if not prior_state or str(prior_state.get("status") or "").upper() != "FAILED":
        return False
    if not str(prior_state.get("last_error") or "").startswith("TERMINAL:"):
        return False

    current = _parse_timestamp(prior_state.get("updated_at"))
    cutoff = _parse_timestamp(cutoff_text)
    if current is None or cutoff is None:
        return False

    try:
        return current <= cutoff
    except TypeError:
        return False


def source_primary_enabled(
    source_id: str | None,
    *,
    environ: dict[str, str] | None = None,
) -> bool:
    """Return True for global PRIMARY or the exact controlled-recovery source."""
    env = os.environ if environ is None else environ
    if str(env.get("DAILY_HISTORY_LAYOUT_PRIMARY_ENABLED") or "").lower() == "true":
        return True

    expected_source = str(env.get(ENV_SOURCE_ID) or "").strip()
    recovery_primary = str(env.get(ENV_LAYOUT_PRIMARY) or "").lower() == "true"
    return bool(recovery_primary and expected_source and source_id == expected_source)
