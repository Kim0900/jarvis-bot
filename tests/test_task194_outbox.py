"""Task194 outbox isolated tests. No Telegram or production Supabase network."""
import asyncio
import os
import types
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

import task194_outbox as box
import scheduler_daily_jobs as sdj


class FakeLegacy:
    def __init__(self):
        self.original_sends = []
        self.TELEGRAM_TOKEN = "fake-token"
        self.logger = types.SimpleNamespace(exception=lambda *a, **k: None)
        self.SUPABASE_URL = "http://127.0.0.1:1"
        self.HEADERS_SB = {"apikey": "fake", "Authorization": "Bearer fake"}
        self.send_telegram_broadcast = self.original_send

    def today_kst(self):
        return date(2026, 10, 13)

    async def original_send(self, text):
        self.original_sends.append(text)
        return {"sent": 1, "failed": 0}

    async def recalc_7day_average(self):
        # Real legacy function swallows Telegram errors; wrapper must surface them.
        try:
            await self.send_telegram_broadcast("7-day critical alert")
        except Exception:
            pass
        return {"status": "CRITICAL"}

    async def check_daily_ingestion_detail(self):
        sent = await self.send_telegram_broadcast("ingestion gap alert")
        if sent["sent"] < 1:
            raise RuntimeError("no recipient")
        return {"sent": sent["sent"]}


class OutboxTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flags = patch.dict(os.environ, {
            "TASK194_OUTBOX_ENABLED": "1", "ALLOWED_CHAT_ID": "123",
            "ALLOWED_CHAT_ID2": "456",
        })
        self.flags.start()

    def tearDown(self):
        self.flags.stop()

    def test_dedupe_keys_stable_but_recipient_scoped(self):
        a = box.dedupe_key("job", "2026-10-12", "123")
        self.assertEqual(a, box.dedupe_key("job", "2026-10-12", "123"))
        self.assertNotEqual(a, box.dedupe_key("job", "2026-10-12", "456"))
        self.assertNotEqual(a, box.dedupe_key("job", "2026-10-13", "123"))

    def test_outbox_enqueue_is_not_pre_send_claimed(self):
        self.assertFalse(sdj.needs_pre_send_claim("check_daily_ingestion_detail"))
        with patch.dict(os.environ, {"TASK194_OUTBOX_ENABLED": "0"}):
            self.assertTrue(sdj.needs_pre_send_claim("check_daily_ingestion_detail"))
        self.assertFalse(sdj.needs_pre_send_claim("recalc_7day_average"))

    async def test_enqueue_per_recipient_uses_conflict_ignore(self):
        calls = []
        async def fake_post(_legacy, path, payload, *, prefer=None):
            calls.append((path, payload, prefer))
            return []  # Already queued: no duplicate or state reset.
        with patch.object(box, "_post", side_effect=fake_post):
            count = await box.enqueue(FakeLegacy(), "recalc_7day_average", "2026-10-13", "WARNING")
            again = await box.enqueue(FakeLegacy(), "recalc_7day_average", "2026-10-13", "WARNING")
        self.assertEqual((count, again), (2, 2))
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({c[1]["dedupe_key"] for c in calls}), 2)
        for path, payload, prefer in calls:
            self.assertEqual(path, "task194_notification_outbox?on_conflict=dedupe_key")
            self.assertEqual(prefer, "resolution=ignore-duplicates,return=representation")
            self.assertEqual(payload["state"], "PENDING")
            self.assertIn("[알림 ID:", payload["body"])

    async def test_failed_enqueue_propagates_to_retry(self):
        with patch.object(box, "_post", side_effect=RuntimeError("DB 503")):
            with self.assertRaisesRegex(RuntimeError, "DB 503"):
                await box.enqueue(FakeLegacy(), "recalc_7day_average", "2026-10-13", "warning")

    async def test_only_scoped_job_alerts_are_queued(self):
        legacy = FakeLegacy()
        with patch.object(box, "enqueue", new_callable=AsyncMock, return_value=2) as enqueue:
            box.install_broadcast_routing(legacy)
            result = await legacy.check_daily_ingestion_detail()
            self.assertEqual(result["sent"], 2)
            result = await legacy.recalc_7day_average()
            self.assertEqual(result["status"], "CRITICAL")
            self.assertEqual(enqueue.await_count, 2)
            self.assertEqual(enqueue.await_args_list[0].args[1:3],
                             ("check_daily_ingestion_detail", date(2026, 10, 12)))
            self.assertEqual(enqueue.await_args_list[1].args[1:3],
                             ("recalc_7day_average", date(2026, 10, 13)))
            await legacy.send_telegram_broadcast("unrelated alert")
            self.assertEqual(legacy.original_sends, ["unrelated alert"])

    async def test_7day_swallowed_failure_becomes_real_job_failure(self):
        legacy = FakeLegacy()
        with patch.object(box, "enqueue", new_callable=AsyncMock,
                          side_effect=RuntimeError("DB 503")):
            box.install_broadcast_routing(legacy)
            with self.assertRaisesRegex(RuntimeError, "Task194 outbox enqueue failed"):
                await legacy.recalc_7day_average()
            self.assertFalse(legacy.original_sends)

    async def test_claim_ack_stores_message_id(self):
        legacy = FakeLegacy()
        row = {"id": 21, "lease_token": "test-lease", "chat_id": "123",
               "body": "critical [알림 ID: abc]"}
        calls = []
        async def fake_post(_legacy, path, payload, **kwargs):
            calls.append((path, payload))
            if path.endswith("claim_notifications"):
                return [row]
            return True
        class FakeBot:
            def __init__(self, token): self.token = token
            async def send_message(self, chat_id, text):
                return types.SimpleNamespace(message_id=902)
        with patch.object(box, "_post", side_effect=fake_post), patch("telegram.Bot", FakeBot):
            result = await box.dispatch_once(legacy)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(calls[-1][0], "rpc/task194_ack_notification")
        self.assertEqual(calls[-1][1]["p_message_id"], 902)

    async def test_telegram_uncertain_delivery_is_recorded(self):
        legacy = FakeLegacy()
        row = {"id": 21, "lease_token": "test-lease", "chat_id": "123", "body": "critical"}
        calls = []
        async def fake_post(_legacy, path, payload, **kwargs):
            calls.append(path)
            return [row] if path.endswith("claim_notifications") else True
        class TimeoutBot:
            def __init__(self, token): pass
            async def send_message(self, chat_id, text): raise TimeoutError("response unknown")
        with patch.object(box, "_post", side_effect=fake_post), patch("telegram.Bot", TimeoutBot):
            result = await box.dispatch_once(legacy)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(calls, ["rpc/task194_claim_notifications", "rpc/task194_release_notification"])

    async def test_ack_write_failure_cannot_be_reported_sent(self):
        legacy = FakeLegacy()
        row = {"id": 21, "lease_token": "test-lease", "chat_id": "123", "body": "critical"}
        async def fake_post(_legacy, path, payload, **kwargs):
            if path.endswith("claim_notifications"): return [row]
            raise RuntimeError("DB status write 503")
        class FakeBot:
            def __init__(self, token): pass
            async def send_message(self, chat_id, text): return types.SimpleNamespace(message_id=6)
        with patch.object(box, "_post", side_effect=fake_post), patch("telegram.Bot", FakeBot):
            with self.assertRaisesRegex(RuntimeError, "status write"):
                await box.dispatch_once(legacy)


if __name__ == "__main__":
    unittest.main()
