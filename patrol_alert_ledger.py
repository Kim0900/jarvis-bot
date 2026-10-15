"""Task #198 (stage A / 1st step): burst-proof, fail-closed patrol alert dispatch.

Design: docs/design/task198_v09.md (CASSANDRA 5298 PASS, MAGI WO-20261015-02).

What this module guarantees (and, deliberately, what it does not):

* An alert is published only when the cooldown ledger (``scheduler_status`` rows named
  ``patrol_alert::<key>`` / ``patrol_meta::<key>``) was read with a trustworthy answer.
* Every ledger transition is a compare-and-swap on the typed ``last_run_at`` column and
  always writes a NEW ``last_run_at``, so of two racing runs only one wins a claim.
* The winner inserts the PATROL_ALERT event at most once per claim: an insert whose result
  is unknown is never repeated (state UNKNOWN, a short notice is sent instead).
* Telegram is sent per alert and per recipient.  Only a *definite* non-delivery is retried
  (at most 2 attempts in total); an ambiguous result is never resent within a claim.
* One run handles at most ``MAX_ALERTS_PER_RUN`` alerts and starts no new alert after
  ``RUN_DEADLINE_SECONDS``; the rest is simply detected again by the next run (the patrol
  is a stateless detector).

NOT guaranteed: exactly-once delivery across instances (there is no database uniqueness
constraint), lossless delivery after a crash (an alert whose Telegram result is unknown
stays unconfirmed until its cooldown expires), process-death monitoring (stage B).

All I/O goes through an ``io`` object so the state machine is tested without a network.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

NS_ALERT = "patrol_alert"
NS_META = "patrol_meta"
STATE_KEY_MAX = 240

GRACE_SECONDS = 600            # an in-flight state older than this is considered abandoned
UNKNOWN_RETRY_SECONDS = 900    # an UNKNOWN alert may be claimed again after this
MAX_TG_ATTEMPTS = 2            # total Telegram attempts per claim (definite failures only)
MAX_RECOVERY_ATTEMPTS = 3
MAX_ALERTS_PER_RUN = 8
RUN_DEADLINE_SECONDS = 120.0
LOCAL_META_COOLDOWN_SECONDS = 1800
VALUE_LIMIT = 900
H_LIMIT = 300

CLAIMED = "CLAIMED"
EVENT = "EVENT"
TG_SENDING = "TG_SENDING"
TG_ACKED = "TG_ACKED"
TG_FAILED = "TG_FAILED"
TG_GIVEUP = "TG_GIVEUP"
TG_PARTIAL = "TG_PARTIAL"
TG_UNCONFIRMED = "TG_UNCONFIRMED"
UNKNOWN = "UNKNOWN"
KNOWN_STATES = {CLAIMED, EVENT, TG_SENDING, TG_ACKED, TG_FAILED, TG_GIVEUP,
                TG_PARTIAL, TG_UNCONFIRMED, UNKNOWN}
TERMINAL_STATES = {TG_ACKED, TG_GIVEUP, TG_PARTIAL, TG_UNCONFIRMED}

# write outcomes (same literals as bot_v5_legacy.WRITE_*)
WIN = "WIN"
LOSE = "LOSE"
AMBIGUOUS = "AMBIGUOUS"
UNRESOLVED = "UNRESOLVED"

# telegram per-recipient results (same literals as bot_v5_legacy.TG_*)
T_OK = "ok"
T_FAIL = "fail"
T_AMB = "amb"

NON_LIVE_TASK_STATUSES = {"COMPLETED", "CLOSED", "CANCELLED"}


class LedgerParseError(ValueError):
    pass


# ───────────────────────────── ledger value ─────────────────────────────
@dataclass(frozen=True)
class LedgerValue:
    state: str
    c: int                      # claim epoch (cooldown clock)
    token: str = ""
    ra: Optional[int] = None    # retry-after epoch (UNKNOWN only)
    e: Optional[int] = None     # event id
    a: int = 0                  # Telegram attempts made
    r: int = 0                  # recovery attempts made
    tg: dict = field(default_factory=dict)   # recipient hash -> ok|fail|amb
    h: str = ""                 # alert text prefix (<= H_LIMIT)


def _clean_h(text: str) -> str:
    return (text or "").replace("|", "/").replace("\n", " ").replace("\r", " ")[:H_LIMIT]


def format_value(v: LedgerValue) -> str:
    tg = ",".join(f"{k}:{val}" for k, val in sorted(v.tg.items()))
    head = (f"S2|{v.state}|c={v.c}|ra={'' if v.ra is None else v.ra}|t={v.token}|"
            f"e={'' if v.e is None else v.e}|a={v.a}|r={v.r}|tg={tg}|h=")
    out = head + _clean_h(v.h)
    return out[:VALUE_LIMIT]


def parse_value(text: str) -> LedgerValue:
    """Parse an ``S2|`` value; anything malformed raises LedgerParseError (fail closed)."""
    if not isinstance(text, str) or not text.startswith("S2|"):
        raise LedgerParseError("not an S2 value")
    parts = text.split("|", 9)
    if len(parts) != 10:
        raise LedgerParseError("wrong field count")
    _, state, c, ra, t, e, a, r, tg, h = parts
    if state not in KNOWN_STATES:
        raise LedgerParseError(f"unknown state {state!r}")

    def field_of(raw: str, name: str) -> str:
        if not raw.startswith(name + "="):
            raise LedgerParseError(f"missing {name}")
        return raw[len(name) + 1:]

    def to_int(raw: str, name: str, optional=False):
        body = field_of(raw, name)
        if body == "" and optional:
            return None
        if not (body.isascii() and body.isdigit()):
            raise LedgerParseError(f"bad {name}")
        return int(body)

    tg_body = field_of(tg, "tg")
    tgd = {}
    if tg_body:
        for item in tg_body.split(","):
            k, sep, val = item.partition(":")
            if not sep or not k or val not in (T_OK, T_FAIL, T_AMB):
                raise LedgerParseError("bad tg")
            tgd[k] = val
    return LedgerValue(
        state=state,
        c=to_int(c, "c"),
        ra=to_int(ra, "ra", optional=True),
        token=field_of(t, "t"),
        e=to_int(e, "e", optional=True),
        a=to_int(a, "a"),
        r=to_int(r, "r"),
        tg=tgd,
        h=field_of(h, "h"),
    )


# ───────────────────────────── time helpers ─────────────────────────────
def parse_ts(raw) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt


# ───────────────────────────── data classes ─────────────────────────────
@dataclass
class Candidate:
    key: str
    detail: str
    severity: str = "WARN"
    task_id: Optional[int] = None
    cooldown_seconds: int = 7200
    namespace: str = NS_ALERT
    write_event: bool = True

    @property
    def state_key(self) -> str:
        return f"{self.namespace}::{self.key}"[:STATE_KEY_MAX]

    @property
    def event_detail(self) -> str:
        return f"[{self.severity}] {self.detail}"[:3400]


@dataclass
class LedgerRow:
    state_key: str
    last_run_at: Optional[str]      # raw string exactly as the server returned it
    last_result: Optional[str]


@dataclass
class Report:
    emitted: list = field(default_factory=list)      # events inserted
    sent: list = field(default_factory=list)         # state keys with a TG_ACKED result
    skipped: list = field(default_factory=list)      # (state_key, reason)
    deferred: list = field(default_factory=list)     # state keys not reached (cap/deadline)
    notes: list = field(default_factory=list)        # (state_key, what) for diagnostics
    ledger_unreadable: bool = False


# module-level (per process) holds: ambiguous claims and the DB-less meta cooldown
_LOCAL_HOLD: dict = {}
_LOCAL_META: dict = {}


def reset_local_state() -> None:
    _LOCAL_HOLD.clear()
    _LOCAL_META.clear()


# ───────────────────────────── the dispatcher ─────────────────────────────
class AlertDispatcher:
    """Runs the state machine.  ``io`` must provide (all async unless noted):

    read_ledger() -> {state_key: LedgerRow}                 raises on an untrusted answer
    create_row(state_key, value, last_run_at) -> (outcome, row|None)   POST ignore-duplicates
    cas_row(state_key, expected_last_run_at, value, last_run_at) -> (outcome, row|None)
    read_row(state_key) -> LedgerRow | None                 raises on an untrusted answer
    insert_event(task_id, detail) -> (True, event_id) | (False, None)  False = result unknown
    read_event(event_id) -> str | None                      None = certainly absent; raises if unknown
    task_statuses(ids) -> {task_id: status}                 raises on an untrusted answer
    send(text, only_hashes) -> {hash: ok|fail|amb}
    recipient_hashes() -> [hash]                            (sync)
    now() -> aware datetime                                 (sync)
    monotonic() -> float                                    (sync)
    """

    def __init__(self, io, *, max_alerts: int = MAX_ALERTS_PER_RUN,
                 deadline_seconds: float = RUN_DEADLINE_SECONDS):
        self.io = io
        self.max_alerts = max_alerts
        self.deadline_seconds = deadline_seconds
        self._token = uuid.uuid4().hex[:10]
        self._seq = 0
        self._task_status_cache: Optional[dict] = None
        self._task_status_failed = False
        self._all_candidates: list = []

    # ---- small helpers -------------------------------------------------
    def _next_token(self) -> str:
        self._seq += 1
        return f"{self._token}.{self._seq}"

    def _now_epoch(self) -> int:
        return int(self.io.now().timestamp())

    def _fresh_last_run_at(self, previous_raw: Optional[str]) -> str:
        now = self.io.now()
        prev = parse_ts(previous_raw)
        if prev is not None and now <= prev:
            now = prev + timedelta(microseconds=1)
        return now.isoformat()

    # ---- task link rule (MAGI WO-20261014-01) ----------------------------
    async def _resolve_task_id(self, cand: Candidate) -> Optional[int]:
        """A task id is kept only if that task exists and is not COMPLETED/CLOSED/CANCELLED;
        a missing, finished or unreadable target gives NULL (never #160 / never a guess)."""
        tid = cand.task_id
        if tid is None:
            return None
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            return None
        if self._task_status_cache is None and not self._task_status_failed:
            ids = sorted({int(c.task_id) for c in self._all_candidates
                          if c.task_id is not None and str(c.task_id).lstrip("-").isdigit()})
            try:
                self._task_status_cache = await self.io.task_statuses(ids) if ids else {}
            except Exception as exc:  # unknown -> NULL for everything
                logger.warning(f"patrol task lookup failed: {type(exc).__name__}")
                self._task_status_failed = True
        if self._task_status_failed or self._task_status_cache is None:
            return None
        status = self._task_status_cache.get(tid)
        if status is None or str(status).upper() in NON_LIVE_TASK_STATUSES:
            return None
        return tid

    # ---- guarded ledger write ----------------------------------------------
    async def _write(self, state_key: str, expected_raw, value: LedgerValue, *, new_row: bool = False):
        """One transition.  Returns (WIN, raw_last_run_at, text) | (LOSE, None, None) |
        (UNRESOLVED, None, None).  An ambiguous answer is resolved by ONE strict re-read:
        if the row now holds exactly our value the write landed; if it is unchanged the write
        is repeated once; anything else is UNRESOLVED (the caller stops, never guesses)."""
        text = format_value(value)
        had_ambiguous = False
        for attempt in (1, 2):
            last = self._fresh_last_run_at(expected_raw if not new_row else None)
            if new_row:
                outcome, row = await self.io.create_row(value_key(self, state_key), text, last)
            else:
                outcome, row = await self.io.cas_row(state_key, expected_raw, text, last)
            if outcome == WIN:
                return WIN, _row_last_run_at(row, last), text
            if outcome == LOSE:
                if not had_ambiguous:
                    return LOSE, None, None
                # an earlier attempt of OURS was ambiguous and may have committed after our
                # re-read: a LOSE now can be our own write -> look at the row before deciding
                try:
                    current = await self.io.read_row(state_key)
                except Exception:
                    return UNRESOLVED, None, None
                if current is not None and current.last_result == text:
                    return WIN, current.last_run_at, text
                return LOSE, None, None
            had_ambiguous = True
            # AMBIGUOUS: look at the row
            try:
                current = await self.io.read_row(state_key)
            except Exception:
                return UNRESOLVED, None, None
            if current is not None and current.last_result == text:
                return WIN, current.last_run_at, text
            unchanged = (current is None and new_row) or (
                current is not None and not new_row and current.last_run_at == expected_raw)
            if unchanged and attempt == 1:
                continue
            return UNRESOLVED, None, None
        return UNRESOLVED, None, None

    # ---- decisions ------------------------------------------------------------
    def _decide(self, cand: Candidate, row: Optional[LedgerRow]):
        """('skip', why) | ('new', None) | ('claim', raw) | ('recover_claimed'|'recover_event'|
        'recover_sending'|'retry_failed', (value, raw)) | ('bad', why)"""
        now = self.io.now()
        if row is None:
            return "new", None
        text = row.last_result or ""
        last = parse_ts(row.last_run_at)
        if not text.startswith("S2|"):
            # legacy plain-text row: the old cooldown rule on last_run_at
            if last is None:
                return "claim", row.last_run_at          # unreadable time -> one fresh claim
            if (now - last).total_seconds() < cand.cooldown_seconds:
                return "skip", "legacy_cooldown"
            return "claim", row.last_run_at
        try:
            val = parse_value(text)
        except (LedgerParseError, ValueError):
            # last_run_at only moves when a transition is written, so an unreadable state that is
            # older than the cooldown is safe to replace; a younger one is held and reported
            if last is not None and (now - last).total_seconds() >= cand.cooldown_seconds:
                return "claim", row.last_run_at
            return "bad", "unparseable_state"
        if last is None:
            return "bad", "unparseable_last_run_at"
        now_epoch = int(now.timestamp())
        if now_epoch - val.c >= cand.cooldown_seconds:
            return "claim", row.last_run_at                 # any state: cooldown over
        age = (now - last).total_seconds()
        if val.state == UNKNOWN:
            if val.ra is not None and now_epoch >= val.ra:
                return "claim", row.last_run_at
            return "skip", "unknown_waiting"
        if val.state in TERMINAL_STATES:
            return "skip", "cooldown"
        if val.state == TG_FAILED:
            return "retry_failed", (val, row.last_run_at)
        if age <= GRACE_SECONDS:
            return "skip", "in_flight"
        if val.state == CLAIMED:
            return "recover_claimed", (val, row.last_run_at)
        if val.state == EVENT:
            return "recover_event", (val, row.last_run_at)
        if val.state == TG_SENDING:
            return "recover_sending", (val, row.last_run_at)
        return "bad", "unhandled_state"

    # ---- main entry --------------------------------------------------------------
    async def dispatch(self, candidates: list) -> Report:
        report = Report()
        # one candidate per ledger key; CRITICAL first, detection order otherwise
        seen = set()
        ordered = []
        for cand in candidates:
            if cand.state_key in seen:
                continue
            seen.add(cand.state_key)
            ordered.append(cand)
        ordered.sort(key=lambda c: 0 if str(c.severity).upper() == "CRITICAL" else 1)
        self._all_candidates = ordered
        if not ordered:
            return report
        try:
            ledger = await self.io.read_ledger()
        except Exception as exc:
            report.ledger_unreadable = True
            logger.error(f"patrol ledger unreadable: {type(exc).__name__}: {exc}")
            for c in ordered:
                report.skipped.append((c.state_key, "ledger_unreadable"))
            plain = [c for c in ordered if c.namespace != NS_META]
            if plain:
                await self._local_meta_notice(
                    "ledger_unreadable",
                    f"⚠️ 순라 경보 장부를 읽지 못해 경보 {len(plain)}건을 보류했습니다 "
                    f"(조건이 지속되면 장부가 복구된 뒤 다음 실행에서 다시 감지됩니다).")
            for c in ordered:
                if c.namespace == NS_META:      # meta alerts: process-local cooldown only
                    await self._local_meta_notice(c.state_key, c.event_detail[:300])
            return report

        started = self.io.monotonic()
        handled = 0
        bad_keys: list = []
        for idx, cand in enumerate(ordered):
            row = ledger.get(cand.state_key)
            hold_until = _LOCAL_HOLD.get(cand.state_key)
            if hold_until is not None and self.io.monotonic() < hold_until:
                report.skipped.append((cand.state_key, "local_hold"))
                continue
            try:
                plan, arg = self._decide(cand, row)
            except Exception as exc:          # a hand-edited row must never stop the run
                logger.error(f"patrol decide failed for {cand.state_key}: {type(exc).__name__}")
                plan, arg = "bad", "decide_error"
            if plan == "skip":
                report.skipped.append((cand.state_key, arg))
                continue
            if plan == "bad":
                report.skipped.append((cand.state_key, arg))
                bad_keys.append(cand.state_key)
                continue
            if handled >= self.max_alerts or (self.io.monotonic() - started) >= self.deadline_seconds:
                report.deferred.extend(c.state_key for c in ordered[idx:]
                                       if c.state_key not in {k for k, _ in report.skipped})
                break
            handled += 1
            try:
                await self._handle(plan, arg, cand, row, report)
            except Exception as exc:  # one alert must never stop the others
                logger.error(f"patrol alert {cand.state_key} failed: {type(exc).__name__}: {exc}")
                report.notes.append((cand.state_key, f"exception:{type(exc).__name__}"))
        if bad_keys:                           # one local, rate-limited notice for all of them
            await self._local_meta_notice(
                "bad_state",
                f"⚠️ 순라 장부 상태를 해석하지 못해 경보 {len(bad_keys)}건을 보류했습니다: "
                + ", ".join(k[:60] for k in bad_keys[:3]))
        return report

    # ---- per-alert flows --------------------------------------------------------------
    async def _handle(self, plan, arg, cand: Candidate, row, report: Report):
        sk = cand.state_key
        if plan in ("new", "claim"):
            await self._claim_and_send(cand, report, expected_raw=arg, new_row=(plan == "new"))
        elif plan == "recover_claimed":
            val, raw = arg
            await self._to_unknown(cand, val, raw, report, why="claimed_abandoned")
        elif plan == "recover_sending":
            val, raw = arg
            w, _, _ = await self._write(sk, raw, replace(val, state=TG_UNCONFIRMED))
            report.notes.append((sk, f"sending_abandoned->{TG_UNCONFIRMED}:{w}"))
        elif plan in ("recover_event", "retry_failed"):
            val, raw = arg
            await self._resume_send(cand, val, raw, report, retry=(plan == "retry_failed"))

    async def _claim_and_send(self, cand: Candidate, report: Report, *, expected_raw, new_row: bool):
        sk = cand.state_key
        now_epoch = self._now_epoch()
        token = self._next_token()
        claimed = LedgerValue(state=CLAIMED, c=now_epoch, token=token, h=cand.event_detail)
        w, raw, _ = await self._write(sk, expected_raw, claimed, new_row=new_row)
        if w == LOSE:
            report.skipped.append((sk, "lost_claim"))
            return
        if w == UNRESOLVED:
            # we do not know whether we own the claim: never insert; hold locally
            _LOCAL_HOLD[sk] = self.io.monotonic() + min(cand.cooldown_seconds, LOCAL_META_COOLDOWN_SECONDS)
            report.notes.append((sk, "claim_unresolved"))
            await self._notify_unknown(cand, "경보 확보 결과 불명")
            return
        current = claimed
        if cand.write_event:
            tid = await self._resolve_task_id(cand)
            detail = (cand.event_detail + f" (claim={token})")[:3500]
            ok, event_id = await self.io.insert_event(tid, detail)
            if not ok:
                await self._to_unknown(cand, current, raw, report, why="insert_unknown")
                return
            report.emitted.append({"key": cand.key, "detail": cand.detail, "severity": cand.severity,
                                   "task_id": tid, "event_id": event_id})
            current = replace(current, state=EVENT, e=event_id, token=self._next_token())
            w, raw, _ = await self._write(sk, raw, current)
            if w != WIN:
                report.notes.append((sk, f"event_state_not_recorded:{w}"))
                return
        await self._send_stage(cand, current, raw, report, text=_alert_text(cand.event_detail), only=None)

    async def _to_unknown(self, cand: Candidate, val: LedgerValue, raw, report: Report, *, why: str):
        sk = cand.state_key
        unknown = replace(val, state=UNKNOWN, ra=self._now_epoch() + UNKNOWN_RETRY_SECONDS,
                          token=self._next_token())
        w, _, _ = await self._write(sk, raw, unknown)
        report.notes.append((sk, f"unknown:{why}:{w}"))
        if w == WIN:
            await self._notify_unknown(cand, why)

    async def _notify_unknown(self, cand: Candidate, why: str):
        text = (f"⚠️ 순라 경보 확정 불가({why}) — 이벤트 기록 결과를 알 수 없어 다시 기록하지 않았습니다.\n"
                f"{cand.event_detail[:300]}")
        try:
            await self.io.send(text, None)
        except Exception as exc:
            logger.error(f"patrol unknown-notice failed: {type(exc).__name__}")

    async def _resume_send(self, cand: Candidate, val: LedgerValue, raw, report: Report, *, retry: bool):
        sk = cand.state_key
        if retry and val.a >= MAX_TG_ATTEMPTS:
            w, _, _ = await self._write(sk, raw, replace(val, state=TG_GIVEUP, token=self._next_token()))
            report.notes.append((sk, f"giveup:{w}"))
            return
        body = None
        if val.e is not None and cand.write_event:
            try:
                body = await self.io.read_event(val.e)
            except Exception:
                body = False                                   # unknown
            if body is None:                                   # certainly absent
                await self._to_unknown(cand, val, raw, report, why="event_missing")
                return
            if body is False:
                if val.r + 1 >= MAX_RECOVERY_ATTEMPTS:
                    await self._to_unknown(cand, replace(val, r=val.r + 1), raw, report, why="event_unreadable")
                else:
                    w, _, _ = await self._write(sk, raw, replace(val, r=val.r + 1, token=self._next_token()))
                    report.notes.append((sk, f"event_unreadable_r+1:{w}"))
                return
            text = _alert_text(body.split(" (claim=")[0])
        else:
            text = _alert_text(cand.event_detail)
        only = None
        if retry:
            only = {h for h, st in val.tg.items() if st == T_FAIL}
            if not only:
                w, _, _ = await self._write(sk, raw, replace(val, state=TG_GIVEUP, token=self._next_token()))
                report.notes.append((sk, f"giveup_nothing_to_retry:{w}"))
                return
        await self._send_stage(cand, val, raw, report, text=text, only=only)

    async def _send_stage(self, cand: Candidate, val: LedgerValue, raw, report: Report, *, text: str, only):
        sk = cand.state_key
        sending = replace(val, state=TG_SENDING, token=self._next_token())
        w, raw2, _ = await self._write(sk, raw, sending)
        if w != WIN:
            report.notes.append((sk, f"sending_not_recorded:{w}"))
            return                                              # no send without a recorded owner
        try:
            results = await self.io.send(text, only)
        except Exception as exc:                                # defensive: send() should not raise
            logger.error(f"patrol send raised {type(exc).__name__}")
            results = {h: T_AMB for h in (only if only is not None else self.io.recipient_hashes())}
        tg = dict(val.tg)
        tg.update(results)
        wanted = list(self.io.recipient_hashes())
        for h in wanted:
            tg.setdefault(h, T_FAIL)                            # configured but never attempted
        attempts = val.a + 1
        has_fail = any(v == T_FAIL for v in tg.values())
        has_amb = any(v == T_AMB for v in tg.values())
        any_ok = any(v == T_OK for v in tg.values())
        if not tg:
            state = TG_GIVEUP                                   # nobody to send to
        elif not has_fail and not has_amb:
            state = TG_ACKED
        elif has_fail and attempts < MAX_TG_ATTEMPTS:
            state = TG_FAILED
        elif has_fail:
            state = TG_GIVEUP
        else:
            state = TG_PARTIAL if any_ok else TG_UNCONFIRMED
        final = replace(sending, state=state, a=attempts, tg=tg, token=self._next_token())
        w, _, _ = await self._write(sk, raw2, final)
        if w == WIN and state == TG_ACKED:
            report.sent.append(sk)
        report.notes.append((sk, f"result:{state}:{w}"))

    # ---- DB-less notices -----------------------------------------------------------------
    async def _local_meta_notice(self, key: str, text: str):
        now = self.io.monotonic()
        until = _LOCAL_META.get(key)
        if until is not None and now < until:
            return
        _LOCAL_META[key] = now + LOCAL_META_COOLDOWN_SECONDS
        try:
            await self.io.send(text, None)
        except Exception as exc:
            logger.error(f"patrol local notice failed: {type(exc).__name__}")


def value_key(dispatcher: AlertDispatcher, state_key: str) -> str:
    """create_row takes the state key; kept as a function so tests can see the call shape."""
    return state_key


def _row_last_run_at(row, fallback: str) -> str:
    """The raw ``last_run_at`` the SERVER reports for the row we just wrote (it may be
    formatted differently from what we sent; the next compare-and-swap must use it)."""
    if isinstance(row, list) and len(row) == 1:          # sb_write_strict returns [row]
        row = row[0]
    if isinstance(row, dict) and isinstance(row.get("last_run_at"), str):
        return row["last_run_at"]
    if isinstance(row, LedgerRow) and row.last_run_at:
        return row.last_run_at
    return fallback


def _alert_text(event_detail: str) -> str:
    return f"🚨 검누리 순라 경보\n{event_detail[:900]}\n공식 원장: Supabase magi_task_events"
