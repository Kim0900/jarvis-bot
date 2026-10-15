"""Task #198 stage 1: state-machine tests for patrol_alert_ledger (pure, in-memory atomic IO)."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import patrol_alert_ledger as pal
from patrol_alert_ledger import (
    AlertDispatcher, Candidate, LedgerRow, LedgerValue, format_value, parse_value,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 15, 2, 0, 0, tzinfo=UTC)


def norm(dt_or_raw):
    dt = dt_or_raw if isinstance(dt_or_raw, datetime) else datetime.fromisoformat(str(dt_or_raw).replace("Z", "+00:00"))
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"


class MemIO:
    """Atomic in-memory stand-in for the database + Telegram with fault injection."""

    def __init__(self):
        self.t = T0
        self.mono = 0.0
        self.rows = {}              # key -> [last_run_at, last_result]
        self.events = {}            # id -> (task_id, detail)
        self.sent = []              # (text, only)
        self.send_script = []       # each: dict | Exception
        self.send_cost = 0.0
        self.recipients = ["aa", "bb"]
        self.faults = []            # (op, mode)
        self.late = []
        self.ledger_error = False
        self.read_row_error = False
        self.read_event_error = False
        self.task_error = False
        self.task_status = {}
        self.task_lookups = 0
        self.insert_raises = False
        self.cas_log = []
        self.after_read_row = None

    # --- environment
    def now(self):
        return self.t

    def monotonic(self):
        return self.mono

    def recipient_hashes(self):
        return list(self.recipients)

    # --- helpers
    def put(self, key, text, age=0, lra="auto"):
        self.rows[key] = [norm(self.t - timedelta(seconds=age)) if lra == "auto" else lra, text]

    def put_s2(self, key, value: LedgerValue, age=0):
        self.put(key, format_value(value), age=age)

    def value(self, key):
        return parse_value(self.rows[key][1])

    def epoch(self, delta=0):
        return int(self.t.timestamp()) + delta

    def _pop(self, op):
        for i, (o, m) in enumerate(self.faults):
            if o == op:
                self.faults.pop(i)
                return m
        return None

    def _row(self, key):
        return {"job_name": key, "last_run_at": self.rows[key][0], "last_result": self.rows[key][1]}

    # --- ledger
    async def read_ledger(self):
        await asyncio.sleep(0)
        if self.ledger_error:
            raise RuntimeError("ledger unreadable")
        return {k: LedgerRow(k, v[0], v[1]) for k, v in self.rows.items()}

    async def read_row(self, key):
        await asyncio.sleep(0)
        if self.read_row_error:
            raise RuntimeError("reread failed")
        hook, self.after_read_row = self.after_read_row, None
        snapshot = None if key not in self.rows else LedgerRow(key, *self.rows[key])
        if hook:
            hook()                       # e.g. the earlier, "lost" write commits right after our look
        return snapshot

    async def create_row(self, key, text, lra):
        await asyncio.sleep(0)
        mode = self._pop("create")
        if mode == "drop":
            return pal.AMBIGUOUS, None
        if mode == "late":
            self.late.append(("create", key, None, text, lra))
            return pal.AMBIGUOUS, None
        if key in self.rows:
            return pal.LOSE, []
        self.rows[key] = [norm(lra), text]
        return (pal.AMBIGUOUS, None) if mode == "commit" else (pal.WIN, [self._row(key)])

    async def cas_row(self, key, expected, text, lra):
        await asyncio.sleep(0)
        self.cas_log.append((key, expected, lra))
        mode = self._pop("cas")
        if mode == "drop":
            return pal.AMBIGUOUS, None
        if mode == "late":
            self.late.append(("cas", key, expected, text, lra))
            return pal.AMBIGUOUS, None
        if key not in self.rows or self.rows[key][0] != expected:
            return pal.LOSE, []
        self.rows[key] = [norm(lra), text]
        return (pal.AMBIGUOUS, None) if mode == "commit" else (pal.WIN, [self._row(key)])

    def flush_late(self):
        for kind, key, expected, text, lra in self.late:
            if kind == "create":
                self.rows.setdefault(key, [norm(lra), text])
            elif key in self.rows and self.rows[key][0] == expected:
                self.rows[key] = [norm(lra), text]
        self.late.clear()

    # --- events / tasks
    async def insert_event(self, task_id, detail):
        await asyncio.sleep(0)
        if self.insert_raises:
            raise RuntimeError("insert exploded")
        mode = self._pop("insert")
        if mode == "drop":
            return False, None
        eid = len(self.events) + 1
        self.events[eid] = (task_id, detail)
        if mode == "commit":
            return False, None
        return True, eid

    async def read_event(self, eid):
        await asyncio.sleep(0)
        if self.read_event_error:
            raise RuntimeError("event read failed")
        ev = self.events.get(eid)
        return None if ev is None else ev[1]

    async def task_statuses(self, ids):
        self.task_lookups += 1
        if self.task_error:
            raise RuntimeError("task lookup failed")
        return {i: self.task_status[i] for i in ids if i in self.task_status}

    # --- telegram
    async def send(self, text, only):
        await asyncio.sleep(0)
        self.mono += self.send_cost
        self.sent.append((text, None if only is None else set(only)))
        if self.send_script:
            item = self.send_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return dict(item)
        targets = self.recipients if only is None else [h for h in self.recipients if h in only]
        return {h: pal.T_OK for h in targets}


def cand(key="k1", severity="WARN", cooldown=3600, **kw):
    return Candidate(key=key, detail=f"detail of {key}", severity=severity, cooldown_seconds=cooldown, **kw)


def sk(key):
    return f"patrol_alert::{key}"


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pal.reset_local_state()
        self.io = MemIO()

    def d(self, **kw):
        return AlertDispatcher(self.io, **kw)

    async def run_once(self, cands, **kw):
        return await self.d(**kw).dispatch(cands)

    def alert_sends(self):
        return [s for s in self.io.sent if s[0].startswith("🚨")]


class ValueFormatTests(unittest.TestCase):
    def test_roundtrip_with_pipe_and_length(self):
        v = LedgerValue(state=pal.TG_FAILED, c=1760000000, token="t.1", e=7, a=1, r=2,
                        tg={"aa": "ok", "bb": "fail"}, h="x|y" * 400)
        text = format_value(v)
        self.assertLessEqual(len(text), 900)
        back = parse_value(text)
        self.assertEqual((back.state, back.c, back.e, back.a, back.r, back.tg), (v.state, v.c, 7, 1, 2, v.tg))
        self.assertNotIn("|", back.h)

    def test_optional_fields_roundtrip(self):
        back = parse_value(format_value(LedgerValue(state=pal.CLAIMED, c=5, token="t")))
        self.assertIsNone(back.ra)
        self.assertIsNone(back.e)

    def test_malformed_values_raise(self):
        good = format_value(LedgerValue(state=pal.EVENT, c=5, token="t", e=1))
        bad = [
            "", "plain text", "S2|", "S2|EVENT|c=5", good.replace("EVENT", "WHATEVER"),
            good.replace("c=5", "c=x"), good.replace("c=5", "c=-5"), good.replace("|a=0|", "|a=|"),
            good.replace("|r=0|", "|r=z|"), good.replace("|tg=|", "|tg=aa:maybe|"),
            good.replace("|tg=|", "|tg=aa|"), good.replace("|t=t|", "|x=t|"), good.replace("|e=1|", "|e=one|"),
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(pal.LedgerParseError):
                    parse_value(text)


class HappyPathTests(Base):
    async def test_new_alert_claim_event_send_acked(self):
        rep = await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)
        self.assertEqual(self.io.value(sk("k1")).e, 1)
        self.assertEqual(rep.sent, [sk("k1")])
        self.assertEqual(len(rep.emitted), 1)
        self.assertIn("claim=", self.io.events[1][1])

    async def test_cas_chain_uses_server_representation(self):
        await self.run_once([cand()])
        # every CAS expected value is a string exactly as the (normalising) server stored it
        for _key, expected, _new in self.io.cas_log:
            self.assertRegex(expected, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")

    async def test_last_run_at_strictly_changes_on_every_transition(self):
        seen = []
        orig = self.io.cas_row

        async def spy(key, expected, text, lra):
            seen.append((expected, norm(lra)))
            return await orig(key, expected, text, lra)

        self.io.cas_row = spy
        await self.run_once([cand()])
        self.assertTrue(seen)
        for expected, new in seen:
            self.assertNotEqual(expected, new)

    async def test_second_run_inside_cooldown_is_silent(self):
        await self.run_once([cand()])
        self.io.t += timedelta(seconds=300)
        rep = await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)
        self.assertEqual(rep.skipped, [(sk("k1"), "cooldown")])

    async def test_cooldown_boundary(self):
        await self.run_once([cand(cooldown=100)])
        self.io.t += timedelta(seconds=99)
        await self.run_once([cand(cooldown=100)])
        self.assertEqual(len(self.io.events), 1)
        self.io.t += timedelta(seconds=1)
        await self.run_once([cand(cooldown=100)])
        self.assertEqual(len(self.io.events), 2)

    async def test_duplicate_candidate_keys_collapse(self):
        await self.run_once([cand("a"), cand("a"), cand("b")])
        self.assertEqual(len(self.io.events), 2)

    async def test_no_candidates_never_touches_ledger(self):
        self.io.ledger_error = True
        rep = await self.run_once([])
        self.assertFalse(rep.ledger_unreadable)
        self.assertEqual(self.io.sent, [])

    async def test_meta_without_event(self):
        c = Candidate(key="loop_error", detail="boom", severity="CRITICAL", namespace=pal.NS_META,
                      write_event=False, cooldown_seconds=100)
        await self.run_once([c])
        self.assertEqual(self.io.events, {})
        self.assertEqual(len(self.io.sent), 1)
        self.assertEqual(self.io.value("patrol_meta::loop_error").state, pal.TG_ACKED)
        await self.run_once([c])
        self.assertEqual(len(self.io.sent), 1)


class LegacyRowTests(Base):
    async def test_legacy_row_within_cooldown_skips(self):
        self.io.put(sk("k1"), "[WARN] old text", age=100)
        await self.run_once([cand(cooldown=3600)])
        self.assertEqual(self.io.events, {})

    async def test_legacy_row_after_cooldown_claims_and_becomes_s2(self):
        self.io.put(sk("k1"), "[WARN] old text", age=4000)
        await self.run_once([cand(cooldown=3600)])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)

    async def test_legacy_row_with_null_time_claims_once(self):
        self.io.rows[sk("k1")] = [None, "[WARN] old text"]
        await self.run_once([cand()])
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)

    async def test_unparseable_s2_state_is_held_not_overwritten(self):
        self.io.put(sk("k1"), "S2|BROKEN|garbage", age=10)
        rep = await self.run_once([cand()])
        self.assertEqual(self.io.events, {})
        self.assertEqual(rep.skipped, [(sk("k1"), "unparseable_state")])
        self.assertEqual(self.io.rows[sk("k1")][1], "S2|BROKEN|garbage")
        self.assertEqual(len(self.io.sent), 1)                       # one local notice
        await self.run_once([cand()])
        self.assertEqual(len(self.io.sent), 1)                       # not repeated


class RaceTests(Base):
    async def test_two_runs_claim_a_new_alert_once(self):
        a, b = self.d(), self.d()
        await asyncio.gather(a.dispatch([cand()]), b.dispatch([cand()]))
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)

    async def test_many_runs_claim_a_new_alert_once(self):
        runs = [self.d().dispatch([cand("a"), cand("b")]) for _ in range(6)]
        await asyncio.gather(*runs)
        self.assertEqual(len(self.io.events), 2)
        self.assertEqual(len(self.alert_sends()), 2)

    async def test_two_runs_claim_an_expired_alert_once(self):
        self.io.put(sk("k1"), "[WARN] old", age=99999)
        await asyncio.gather(self.d().dispatch([cand()]), self.d().dispatch([cand()]))
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)

    async def test_failed_retry_is_claimed_once(self):
        self.io.send_script = [{"aa": pal.T_OK, "bb": pal.T_FAIL}]
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_FAILED)
        self.io.sent.clear()
        await asyncio.gather(self.d().dispatch([cand()]), self.d().dispatch([cand()]))
        self.assertEqual(len(self.alert_sends()), 1)
        self.assertEqual(len(self.io.events), 1)


class AmbiguousWriteTests(Base):
    async def test_ambiguous_create_that_committed_proceeds_once(self):
        self.io.faults = [("create", "commit")]
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)

    async def test_ambiguous_create_that_was_dropped_is_retried_once(self):
        self.io.faults = [("create", "drop")]
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)

    async def test_ambiguous_create_committing_late_never_duplicates(self):
        self.io.faults = [("create", "late")]
        await self.run_once([cand()])
        self.io.flush_late()
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)

    async def test_ambiguous_cas_committed_proceeds(self):
        self.io.put(sk("k1"), "[WARN] old", age=99999)
        self.io.faults = [("cas", "commit")]
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)

    async def test_ambiguous_cas_late_commit_never_duplicates(self):
        self.io.put(sk("k1"), "[WARN] old", age=99999)
        self.io.faults = [("cas", "late")]
        await self.run_once([cand()])
        self.io.flush_late()
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)

    async def test_unresolvable_claim_inserts_nothing_and_holds(self):
        self.io.put(sk("k1"), "[WARN] old", age=99999)
        self.io.faults = [("cas", "commit")]
        self.io.read_row_error = True
        rep = await self.run_once([cand()])
        self.assertEqual(self.io.events, {})
        self.assertEqual(self.alert_sends(), [])
        self.assertIn((sk("k1"), "claim_unresolved"), rep.notes)
        self.assertEqual(len(self.io.sent), 1)
        # the same process does not try again while the hold lasts, even though the ledger says "claim"
        self.io.read_row_error = False
        self.io.rows[sk("k1")] = ["2000-01-01T00:00:00.000000+00:00", "[WARN] old"]
        rep = await self.run_once([cand()])
        self.assertEqual(self.io.events, {})
        self.assertEqual(rep.skipped, [(sk("k1"), "local_hold")])

    async def test_state_write_not_recorded_means_no_send(self):
        self.io.faults = [("cas", "drop"), ("cas", "drop")]    # EVENT -> TG_SENDING never lands
        self.io.read_row_error = False
        await self.run_once([cand()])
        # CLAIMED landed, EVENT write failed twice -> no Telegram for the alert
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(self.alert_sends(), [])


class LateCommitDeliveryTests(Base):
    """A write whose answer was lost may commit just AFTER our re-read; the alert must still be delivered."""

    async def test_late_cas_commit_after_reread_is_not_mistaken_for_a_lost_race(self):
        self.io.put(sk("k1"), "[WARN] old", age=99999)
        self.io.faults = [("cas", "late")]
        self.io.after_read_row = self.io.flush_late
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)

    async def test_late_create_commit_after_reread_is_not_mistaken_for_a_lost_race(self):
        self.io.faults = [("create", "late")]
        self.io.after_read_row = self.io.flush_late
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)

    async def test_late_commit_of_the_sending_state_still_sends(self):
        # fault the 3rd transition (CLAIMED, EVENT ok; TG_SENDING ambiguous+late)
        orig = self.io.cas_row
        n = {"i": 0}

        async def counting(key, expected, text, lra):
            n["i"] += 1
            if n["i"] == 2:
                self.io.faults.append(("cas", "late"))
                self.io.after_read_row = self.io.flush_late
            return await orig(key, expected, text, lra)

        self.io.cas_row = counting
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)


class HandEditedRowTests(Base):
    async def test_unparseable_state_older_than_cooldown_is_replaced(self):
        self.io.put(sk("k1"), "S2|BROKEN|garbage", age=4000)
        await self.run_once([cand(cooldown=3600)])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)

    async def test_unicode_digits_are_rejected_not_crashing(self):
        good = format_value(LedgerValue(state=pal.EVENT, c=5, token="t", e=1))
        with self.assertRaises(pal.LedgerParseError):
            parse_value(good.replace("c=5", "c=²"))
        self.io.put(sk("k1"), good.replace("c=5", "c=²"), age=10)
        self.io.put(sk("k2"), "[WARN] old", age=99999)
        await self.run_once([cand("k1"), cand("k2")])
        self.assertEqual(len(self.io.events), 1)               # k2 still handled

    async def test_many_bad_rows_give_one_notice(self):
        for i in range(5):
            self.io.put(sk(f"b{i}"), "S2|BROKEN", age=10)
        await self.run_once([cand(f"b{i}") for i in range(5)])
        self.assertEqual(len(self.io.sent), 1)


class InsertUnknownTests(Base):
    async def test_insert_committed_but_unknown_is_never_repeated(self):
        self.io.faults = [("insert", "commit")]
        rep = await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        val = self.io.value(sk("k1"))
        self.assertEqual(val.state, pal.UNKNOWN)
        self.assertEqual(val.ra, self.io.epoch(pal.UNKNOWN_RETRY_SECONDS))
        self.assertEqual(len(self.io.sent), 1)                      # one notice
        self.assertIn("detail of k1", self.io.sent[0][0])
        self.assertLessEqual(len(self.io.sent[0][0]), 600)
        for _ in range(5):
            self.io.t += timedelta(seconds=120)
            await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.io.sent), 1)

    async def test_insert_dropped_but_unknown_is_never_repeated(self):
        self.io.faults = [("insert", "drop")]
        await self.run_once([cand()])
        self.assertEqual(self.io.events, {})
        self.assertEqual(self.io.value(sk("k1")).state, pal.UNKNOWN)
        self.io.t += timedelta(seconds=60)
        await self.run_once([cand()])
        self.assertEqual(self.io.events, {})

    async def test_unknown_is_claimable_after_retry_after(self):
        self.io.faults = [("insert", "drop")]
        await self.run_once([cand()])
        self.io.t += timedelta(seconds=pal.UNKNOWN_RETRY_SECONDS + 1)
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)

    async def test_one_failing_alert_does_not_stop_the_others(self):
        calls = {"n": 0}
        orig = self.io.insert_event

        async def flaky(task_id, detail):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return await orig(task_id, detail)

        self.io.insert_event = flaky
        rep = await self.run_once([cand("a"), cand("b")])
        self.assertEqual(len(self.io.events), 1)
        self.assertTrue(any(n[1].startswith("exception:") for n in rep.notes))


class CrashRecoveryTests(Base):
    def seed(self, state, age, **kw):
        base = dict(state=state, c=self.io.epoch(-age), token="old", h="detail of k1")
        base.update(kw)
        self.io.put_s2(sk("k1"), LedgerValue(**base), age=age)

    async def test_claimed_within_grace_waits(self):
        self.seed(pal.CLAIMED, 60)
        await self.run_once([cand()])
        self.assertEqual((self.io.events, self.io.sent), ({}, []))

    async def test_claimed_abandoned_becomes_unknown_without_insert(self):
        self.seed(pal.CLAIMED, pal.GRACE_SECONDS + 5)
        await self.run_once([cand()])
        self.assertEqual(self.io.events, {})
        self.assertEqual(self.io.value(sk("k1")).state, pal.UNKNOWN)

    async def test_event_abandoned_resumes_send_with_stored_event(self):
        self.io.events[7] = (None, "[WARN] stored text (claim=old)")
        self.seed(pal.EVENT, pal.GRACE_SECONDS + 5, e=7)
        await self.run_once([cand()])
        self.assertEqual(len(self.io.events), 1)
        self.assertEqual(len(self.alert_sends()), 1)
        self.assertIn("stored text", self.alert_sends()[0][0])
        self.assertNotIn("claim=", self.alert_sends()[0][0])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)

    async def test_event_abandoned_but_event_missing_is_unknown(self):
        self.seed(pal.EVENT, pal.GRACE_SECONDS + 5, e=99)
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.UNKNOWN)
        self.assertEqual(self.io.events, {})

    async def test_event_unreadable_counts_recoveries_then_unknown(self):
        self.io.events[7] = (None, "[WARN] x")
        self.io.read_event_error = True
        self.seed(pal.EVENT, pal.GRACE_SECONDS + 5, e=7)
        for expected_r in (1, 2):
            await self.run_once([cand()])
            val = self.io.value(sk("k1"))
            self.assertEqual((val.state, val.r), (pal.EVENT, expected_r))
            self.io.t += timedelta(seconds=pal.GRACE_SECONDS + 5)
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.UNKNOWN)
        self.assertEqual(self.alert_sends(), [])

    async def test_sending_abandoned_is_unconfirmed_and_never_resent(self):
        self.seed(pal.TG_SENDING, pal.GRACE_SECONDS + 5, e=1)
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_UNCONFIRMED)
        self.assertEqual(self.io.sent, [])
        self.io.t += timedelta(seconds=300)
        await self.run_once([cand()])
        self.assertEqual(self.io.sent, [])

    async def test_sending_within_grace_waits(self):
        self.seed(pal.TG_SENDING, 30, e=1)
        await self.run_once([cand()])
        self.assertEqual(self.io.sent, [])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_SENDING)

    async def test_every_in_flight_state_is_reclaimable_after_cooldown(self):
        for state in (pal.CLAIMED, pal.EVENT, pal.TG_SENDING, pal.TG_FAILED, pal.UNKNOWN, pal.TG_UNCONFIRMED):
            with self.subTest(state=state):
                self.setUp()
                self.seed(state, 3700, e=1)
                await self.run_once([cand(cooldown=3600)])
                self.assertEqual(len(self.io.events), 1)
                self.assertEqual(self.io.value(sk("k1")).state, pal.TG_ACKED)

    async def test_terminal_states_hold_until_cooldown(self):
        for state in (pal.TG_ACKED, pal.TG_GIVEUP, pal.TG_PARTIAL, pal.TG_UNCONFIRMED):
            with self.subTest(state=state):
                self.setUp()
                self.seed(state, 120, e=1)
                await self.run_once([cand(cooldown=3600)])
                self.assertEqual((self.io.events, self.io.sent), ({}, []))


class TelegramRuleTests(Base):
    async def test_definite_failure_retries_only_failed_recipient_then_acks(self):
        self.io.send_script = [{"aa": pal.T_OK, "bb": pal.T_FAIL}]
        await self.run_once([cand()])
        val = self.io.value(sk("k1"))
        self.assertEqual((val.state, val.a), (pal.TG_FAILED, 1))
        self.io.t += timedelta(seconds=300)
        await self.run_once([cand()])
        self.assertEqual(self.io.sent[-1][1], {"bb"})
        val = self.io.value(sk("k1"))
        self.assertEqual((val.state, val.a), (pal.TG_ACKED, 2))
        self.assertEqual(len(self.io.events), 1)

    async def test_second_definite_failure_gives_up(self):
        self.io.send_script = [{"aa": pal.T_FAIL, "bb": pal.T_OK}, {"aa": pal.T_FAIL}]
        await self.run_once([cand()])
        self.io.t += timedelta(seconds=300)
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_GIVEUP)
        n = len(self.io.sent)
        self.io.t += timedelta(seconds=300)
        await self.run_once([cand()])
        self.assertEqual(len(self.io.sent), n)

    async def test_ambiguous_result_is_never_resent(self):
        self.io.send_script = [{"aa": pal.T_OK, "bb": pal.T_AMB}]
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_PARTIAL)
        for _ in range(3):
            self.io.t += timedelta(seconds=300)
            await self.run_once([cand()])
        self.assertEqual(len(self.io.sent), 1)

    async def test_all_ambiguous_is_unconfirmed_and_not_resent(self):
        self.io.send_script = [{"aa": pal.T_AMB, "bb": pal.T_AMB}]
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_UNCONFIRMED)
        self.io.t += timedelta(seconds=300)
        await self.run_once([cand()])
        self.assertEqual(len(self.io.sent), 1)

    async def test_fail_plus_ambiguous_retries_only_the_definite_failure(self):
        self.io.send_script = [{"aa": pal.T_FAIL, "bb": pal.T_AMB}]
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_FAILED)
        self.io.t += timedelta(seconds=300)
        await self.run_once([cand()])
        self.assertEqual(self.io.sent[-1][1], {"aa"})
        val = self.io.value(sk("k1"))
        self.assertEqual(val.tg["bb"], pal.T_AMB)

    async def test_send_exception_counts_as_ambiguous(self):
        self.io.send_script = [RuntimeError("socket died")]
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_UNCONFIRMED)
        self.assertEqual(len(self.io.sent), 1)

    async def test_missing_recipient_result_is_a_definite_failure(self):
        self.io.send_script = [{"aa": pal.T_OK}]
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_FAILED)

    async def test_no_recipients_gives_up_without_loop(self):
        self.io.recipients = []
        self.io.send_script = [{}]
        await self.run_once([cand()])
        self.assertEqual(self.io.value(sk("k1")).state, pal.TG_GIVEUP)

    async def test_failed_alert_whose_condition_vanished_is_never_retried(self):
        self.io.send_script = [{"aa": pal.T_FAIL, "bb": pal.T_FAIL}]
        await self.run_once([cand()])
        for _ in range(3):
            self.io.t += timedelta(seconds=300)
            await self.run_once([])
        self.assertEqual(len(self.io.sent), 1)

    async def test_retry_event_text_comes_from_the_stored_event(self):
        self.io.send_script = [{"aa": pal.T_FAIL, "bb": pal.T_OK}]
        await self.run_once([cand()])
        first = self.io.sent[0][0]
        self.io.t += timedelta(seconds=300)
        await self.run_once([Candidate(key="k1", detail="CHANGED", cooldown_seconds=3600)])
        self.assertEqual(self.io.sent[1][0], first)


class LimitTests(Base):
    async def test_per_run_cap_and_deferral(self):
        cands = [cand(f"k{i}") for i in range(12)]
        rep = await self.run_once(cands)
        self.assertEqual(len(self.io.events), 8)
        self.assertEqual(len(rep.deferred), 4)
        # deferred alerts are simply detected again next run
        self.io.t += timedelta(seconds=300)
        rep = await self.run_once(cands)
        self.assertEqual(len(self.io.events), 12)

    async def test_critical_first_even_when_detected_last(self):
        cands = [cand(f"w{i}") for i in range(9)] + [cand(f"c{i}", severity="CRITICAL") for i in range(4)]
        await self.run_once(cands)
        done = {d.split(" ")[3] for _t, d in self.io.events.values()}
        self.assertTrue({f"c{i}" for i in range(4)} <= done)

    async def test_first_four_critical_scheduler_failures_preserved(self):
        jobs = ["run_magi_auto_review_once", "notify_pending_simple_tasks", "dual_verify_7day_average",
                "ask_operated_status_telegram"]
        cands = [cand(f"scheduler_fail::{j}", severity="CRITICAL") for j in jobs]
        cands += [cand(f"ingestion_failed::s{i}") for i in range(10)]
        await self.run_once(cands)
        texts = [d for _t, d in self.io.events.values()]
        for j in jobs:
            self.assertTrue(any(j in t for t in texts), j)

    async def test_deadline_stops_new_alerts(self):
        self.io.send_cost = 50.0
        rep = await self.run_once([cand(f"k{i}") for i in range(10)], deadline_seconds=120.0)
        self.assertEqual(len(self.io.events), 3)                    # starts at 0, 50, 100; 150 >= 120 stops
        self.assertEqual(len(rep.deferred), 7)

    async def test_one_message_per_alert(self):
        await self.run_once([cand(f"k{i}") for i in range(5)])
        self.assertEqual(len(self.alert_sends()), 5)


class LedgerUnreadableTests(Base):
    async def test_unreadable_ledger_blocks_everything_with_one_local_notice(self):
        self.io.ledger_error = True
        rep = await self.run_once([cand("a"), cand("b")])
        self.assertTrue(rep.ledger_unreadable)
        self.assertEqual(self.io.events, {})
        self.assertEqual(len(self.io.sent), 1)
        self.assertEqual(self.alert_sends(), [])
        await self.run_once([cand("a"), cand("b")])
        self.assertEqual(len(self.io.sent), 1)                      # local 30-minute cooldown
        self.io.mono += pal.LOCAL_META_COOLDOWN_SECONDS + 1
        await self.run_once([cand("a")])
        self.assertEqual(len(self.io.sent), 2)

    async def test_unreadable_ledger_loop_error_is_local_and_rate_limited(self):
        self.io.ledger_error = True
        c = Candidate(key="loop_error", detail="boom", severity="CRITICAL", namespace=pal.NS_META,
                      write_event=False)
        for _ in range(4):
            await self.run_once([c])
        self.assertEqual(len(self.io.sent), 1)
        self.assertIn("boom", self.io.sent[0][0])
        self.assertEqual(self.io.events, {})


class TaskLinkTests(Base):
    async def test_task_link_rule(self):
        self.io.task_status = {5: "IN_PROGRESS", 6: "COMPLETED", 7: "CANCELLED", 8: "CLOSED", 10: "HOLD"}
        cands = [cand(f"t{i}", task_id=i) for i in (5, 6, 7, 8, 9, 10)] + [cand("none")]
        await self.run_once(cands)
        by_key = {d.split(" ")[3]: t for t, d in self.io.events.values()}
        self.assertEqual(by_key["t5"], 5)
        self.assertEqual(by_key["t10"], 10)
        for k in ("t6", "t7", "t8", "t9", "none"):
            self.assertIsNone(by_key[k], k)
        self.assertEqual(self.io.task_lookups, 1)

    async def test_lookup_failure_gives_null_not_a_guess(self):
        self.io.task_error = True
        await self.run_once([cand("t5", task_id=5), cand("t6", task_id=6)])
        self.assertEqual([t for t, _d in self.io.events.values()], [None, None])
        self.assertEqual(self.io.task_lookups, 1)

    async def test_never_defaults_to_task_160(self):
        self.io.task_status = {160: "IN_PROGRESS"}
        await self.run_once([cand("x")])
        self.assertIsNone(self.io.events[1][0])


if __name__ == "__main__":
    unittest.main()
