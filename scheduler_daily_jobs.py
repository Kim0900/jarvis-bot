"""Task #194: single dispatch path for the four daily 04:00 statistics jobs.

Why this module exists
----------------------
On 2026-10-11 the four daily jobs were recorded as ``FAIL: `` (empty error text)
after a Render->Supabase outage.  Root causes (task #194 phase 1, CASSANDRA 4968):

* ``already_ran_today_kst`` treated *any* row stamped today as "ran", so a FAIL
  record blocked every retry that day.
* ``sb_select`` converts non-200 PostgREST answers into ``[]``; a guard built on it
  reads "no success recorded" during an outage and may run a job twice.
* ``str(httpx.TimeoutException())`` is empty, so the failure reason was lost.
* A retry loop existed only in the replaced ``bot_v5_legacy.fish_scheduler``; the
  production scheduler is ``sitecustomize.fish_scheduler``.  This module is the one
  place both the success guard and the retry rule live, and sitecustomize calls it.

Rules implemented
-----------------
1. Success state is read through ``legacy.sb_h`` and every ambiguous answer raises
   :class:`SchedulerStateUnknown`; the job is then NOT run (fail closed).
2. A job counts as done only if its row was written today (KST) with a non-empty
   result that does not start with ``FAIL``.
3. Failed or never-run jobs are retried inside a bounded window with a minimum
   interval between attempts.  A failed attempt is stored as
   ``FAIL: <ExceptionType>: <repr>`` (never empty).
4. Success and failure status writes use ``sb_upsert`` (raises on failure).  If the
   job ran but its success row could not be written, an idempotent job is retried
   on the next interval; ``check_daily_ingestion_detail`` sends a Telegram alert,
   so it is *not* re-run for the rest of the day (a duplicate alert is worse).
5. Missed-day catch-up for ``calc_daily_snapshot`` exists but is disabled unless
   ``TASK194_CATCHUP_DAYS`` > 0.  Enabling it rewrites historical rows and needs the
   architect's approval (task #194 phase 3).
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

DAILY_JOBS = (
    "recalc_daily_summary_totals",
    "recalc_7day_average",
    "calc_daily_snapshot",
    "check_daily_ingestion_detail",
)
# These jobs send external notifications; never repeat them on an uncertain status write.
NON_IDEMPOTENT_JOBS = frozenset({"check_daily_ingestion_detail"})

RESULT_MAX_LEN = 500


class SchedulerStateUnknown(RuntimeError):
    """scheduler_status could not be read or is ambiguous; callers must not run the job."""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def retry_interval_seconds() -> int:
    return max(_env_int("TASK194_RETRY_INTERVAL_SEC", 900), 60)


def window_hours() -> tuple[int, int]:
    """[start, end) KST hours in which the jobs may run or retry."""
    start = _env_int("TASK194_WINDOW_START_HOUR", 4)
    end = _env_int("TASK194_WINDOW_END_HOUR", 12)
    return start, max(end, start + 1)


def catchup_days() -> int:
    return max(_env_int("TASK194_CATCHUP_DAYS", 0), 0)


def format_failure(exc: BaseException) -> str:
    """Never return an empty reason: httpx timeouts stringify to ''."""
    text = f"FAIL: {type(exc).__name__}: {exc!r}"
    return text[:RESULT_MAX_LEN]


CLAIM_PREFIX = "CLAIMED"


def is_success_result(result: Any) -> bool:
    return (isinstance(result, str) and bool(result.strip())
            and not result.startswith("FAIL") and not result.startswith(CLAIM_PREFIX))


def is_claim_result(result: Any) -> bool:
    return isinstance(result, str) and result.startswith(CLAIM_PREFIX)


async def job_state_today(legacy: Any, job_name: str, now: datetime) -> str:
    """``"success"``, ``"claimed"`` or ``"none"`` for today's (KST) record of ``job_name``.

    The read goes through ``legacy.sb_get_strict`` (CASSANDRA 5067 F1): only HTTP 200
    with a non-empty JSON list is believed, so an empty/garbled 200 can never pass as
    "no row yet".  Raises :class:`SchedulerStateUnknown` for every untrusted answer:
    transport errors, non-200, empty/invalid/non-list body, duplicated or malformed
    rows, naive or future timestamps.
    """
    try:
        rows = await legacy.sb_get_strict("scheduler_status", {"job_name": f"eq.{job_name}"})
    except Exception as exc:  # transport level or untrusted body
        raise SchedulerStateUnknown(f"read failed: {type(exc).__name__}: {exc!r}") from exc
    if not isinstance(rows, list):
        raise SchedulerStateUnknown("read failed: non-list response")
    if not rows:
        return "none"  # a verified empty answer is a first run
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise SchedulerStateUnknown("ambiguous scheduler_status rows")
    row = rows[0]
    stamp = row.get("last_run_at")
    if not isinstance(stamp, str) or not stamp.strip():
        raise SchedulerStateUnknown("last_run_at missing")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SchedulerStateUnknown(f"last_run_at invalid: {exc!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchedulerStateUnknown("last_run_at has no timezone offset")
    parsed = parsed.astimezone(KST)
    if parsed > now + timedelta(minutes=5):
        raise SchedulerStateUnknown("last_run_at is in the future")
    if parsed.date() != now.date():
        return "none"
    result = row.get("last_result")
    # CASSANDRA 5072 F5: scheduler_status.last_result is nullable.  A row stamped today
    # whose result is null / blank / not a string is ambiguous, not "never ran": running
    # again could repeat a notification.  Explicit FAIL rows keep their retry semantics.
    if not isinstance(result, str) or not result.strip():
        raise SchedulerStateUnknown("last_result missing, null or blank")
    if is_claim_result(result):
        return "claimed"
    return "success" if is_success_result(result) else "none"


async def succeeded_today(legacy: Any, job_name: str, now: datetime) -> bool:
    return (await job_state_today(legacy, job_name, now)) == "success"


class DailyJobState:
    """In-process retry bookkeeping.  Success itself is always decided from the DB."""

    def __init__(self) -> None:
        self.last_attempt: dict[tuple[str, str], float] = {}
        self.attempts: dict[tuple[str, str], int] = {}
        self.uncertain_done: set[tuple[str, str]] = set()
        # Success confirmed from the DB today; avoids re-reading scheduler_status every tick.
        self.confirmed_done: set[tuple[str, str]] = set()

    def prune(self, today: date) -> None:
        keep = str(today)
        self.confirmed_done = {k for k in self.confirmed_done if k[0] == keep}
        self.last_attempt = {k: v for k, v in self.last_attempt.items() if k[0] == keep}
        self.attempts = {k: v for k, v in self.attempts.items() if k[0] == keep}
        self.uncertain_done = {k for k in self.uncertain_done if k[0] == keep}


async def _write_status(legacy: Any, job_name: str, result: str, now: datetime) -> None:
    await legacy.sb_upsert(
        "scheduler_status",
        {"job_name": job_name, "last_run_at": now.isoformat(), "last_result": result},
        on_conflict="job_name",
    )


async def run_daily_jobs(
    legacy: Any,
    now: datetime,
    state: DailyJobState,
    monotonic: Callable[[], float] = time.monotonic,
    jobs: Optional[dict[str, Callable[[], Awaitable[Any]]]] = None,
) -> dict[str, str]:
    """One scheduler tick.  Returns ``{job_name: outcome}`` for logging and tests.

    Outcomes: ``outside_window``, ``done``, ``state_unknown``, ``cooldown``,
    ``uncertain_done``, ``claim_failed``, ``ok``, ``failed``, ``ok_status_write_failed``.
    """
    outcomes: dict[str, str] = {}
    start_hour, end_hour = window_hours()
    state.prune(now.date())
    if not (start_hour <= now.hour < end_hour):
        return {name: "outside_window" for name in DAILY_JOBS}

    actions = jobs or {name: getattr(legacy, name) for name in DAILY_JOBS}
    interval = retry_interval_seconds()

    for name in DAILY_JOBS:
        key = (str(now.date()), name)
        if key in state.confirmed_done:
            outcomes[name] = "done"
            continue
        if key in state.uncertain_done:
            outcomes[name] = "uncertain_done"
            continue
        if monotonic() - state.last_attempt.get(key, -1e12) < interval:
            outcomes[name] = "cooldown"
            continue
        try:
            seen = await job_state_today(legacy, name, now)
            if seen == "success":
                state.confirmed_done.add(key)
                outcomes[name] = "done"
                continue
            if seen == "claimed" and name in NON_IDEMPOTENT_JOBS:
                # A previous process claimed the send and never recorded the outcome
                # (crash/restart or lost status write).  At-most-once: do not resend.
                legacy.logger.error("Task194 %s: claim found without outcome -> not re-run (delivery uncertain)", name)
                state.uncertain_done.add(key)
                outcomes[name] = "uncertain_done"
                continue
        except SchedulerStateUnknown as exc:
            legacy.logger.error("Task194 scheduler state unknown (%s): %s -> job not run", name, exc)
            outcomes[name] = "state_unknown"
            # A failed read is also an attempt: do not hammer a struggling database.
            state.last_attempt[key] = monotonic()
            continue

        state.last_attempt[key] = monotonic()
        state.attempts[key] = state.attempts.get(key, 0) + 1
        if name in NON_IDEMPOTENT_JOBS:
            # Durable claim BEFORE the external side effect (CASSANDRA 5067 F2).  If the
            # claim cannot be stored we do not send; if the process dies after sending,
            # the stored claim stops any re-send after restart.
            try:
                await _write_status(legacy, name, f"{CLAIM_PREFIX}: attempt={state.attempts[key]}", now)
            except Exception as exc:
                legacy.logger.error("Task194 claim write failed (%s): %r -> job not run", name, exc)
                outcomes[name] = "claim_failed"
                continue
        try:
            result = await actions[name]()
        except Exception as exc:
            detail = format_failure(exc)
            legacy.logger.error("Task194 job failed (%s) attempt=%s: %s", name, state.attempts[key], detail)
            try:
                await _write_status(legacy, name, detail, now)
            except Exception as status_exc:
                legacy.logger.error("Task194 failure status write failed (%s): %r", name, status_exc)
            outcomes[name] = "failed"
            continue

        success_value = str(result) if name == "check_daily_ingestion_detail" and result else "OK"
        try:
            await _write_status(legacy, name, success_value, now)
        except Exception as exc:
            legacy.logger.error("Task194 success status write failed (%s): %r", name, exc)
            if name in NON_IDEMPOTENT_JOBS:
                state.uncertain_done.add(key)
            outcomes[name] = "ok_status_write_failed"
            continue
        state.confirmed_done.add(key)
        outcomes[name] = "ok"

    await _maybe_catch_up(legacy, now, outcomes, actions)
    return outcomes


async def _maybe_catch_up(legacy: Any, now: datetime, outcomes: dict[str, str],
                          actions: dict[str, Callable[..., Awaitable[Any]]]) -> None:
    """Optional recompute of snapshot dates missed during an outage (flag off by default).

    Runs only after today's regular ``calc_daily_snapshot`` succeeded, so a failing
    database is never hit with a burst of historical recomputes.
    """
    days = catchup_days()
    if days <= 0 or outcomes.get("calc_daily_snapshot") not in ("ok", "done"):
        return
    try:
        dates = await missing_snapshot_dates(legacy, now, days)
    except SchedulerStateUnknown as exc:
        legacy.logger.error("Task194 catch-up skipped: %s", exc)
        return
    for day in dates:
        try:
            await actions["calc_daily_snapshot"](day)
            legacy.logger.info("Task194 catch-up recomputed daily_calc_snapshot %s", day)
        except Exception as exc:
            legacy.logger.error("Task194 catch-up stopped at %s: %s", day, format_failure(exc))
            return


async def missing_snapshot_dates(legacy: Any, now: datetime, max_days: int) -> list[str]:
    """Dates (oldest first) in the last ``max_days`` days before yesterday with no
    ``daily_calc_snapshot`` row.

    The look-back is by calendar window, not "after the newest row": once the regular
    run stores yesterday's row, the newest row is yesterday and a gap before it would
    be invisible.  ``sb_h`` failures raise :class:`SchedulerStateUnknown`, so an outage
    can never look like "everything is missing".
    """
    if max_days <= 0:
        return []
    yesterday = now.date() - timedelta(days=1)
    first = yesterday - timedelta(days=max_days)
    try:
        rows = await legacy.sb_get_strict(
            "daily_calc_snapshot",
            {"select": "calc_date", "calc_date": [f"gte.{first}", f"lt.{yesterday}"], "limit": "1000"},
        )
    except Exception as exc:
        raise SchedulerStateUnknown(f"snapshot read failed: {type(exc).__name__}: {exc!r}") from exc
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise SchedulerStateUnknown("snapshot read returned an unusable answer")
    present = {str(r.get("calc_date")) for r in rows}
    dates = []
    cursor = first
    while cursor < yesterday:
        if str(cursor) not in present:
            dates.append(str(cursor))
        cursor += timedelta(days=1)
    return dates
