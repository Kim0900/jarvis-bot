"""Task #187 evidence (ingestion Telegram notifications).  No network: send_telegram_broadcast is replaced by a recording fake; the real
queue_ingestion_notification / worker thread / ingestion_notify.format_ingestion_message run.  Bot code is NOT modified.
FINDING tests pin TODAY's behaviour so the issue is visible; a fix must change them on purpose."""
import asyncio
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:1")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
import bot_v5_legacy as legacy  # noqa: E402
import ingestion_notify  # noqa: E402


class Recorder:
    def __init__(self):
        self.sent, self.mode, self.gate = [], None, None

    async def __call__(self, text):
        if self.gate is not None:
            await asyncio.get_running_loop().run_in_executor(None, self.gate.wait, 10)
        if self.mode == "raise":
            self.mode = None
            raise RuntimeError("telegram down")
        self.sent.append(text)
        return {"sent": 1, "failed": 0}


class T(unittest.TestCase):
    def setUp(self):
        legacy._INGEST_NOTIFY_SEEN.clear()
        self.rec = Recorder()
        p = patch.object(legacy, "send_telegram_broadcast", self.rec)
        p.start(); self.addCleanup(p.stop)

    def q(self, **kw):
        base = dict(stage="RECEIVED", kind="kakao", source_id="s1", file_name="a.txt")
        base.update(kw)
        return legacy.queue_ingestion_notification(**base)

    def drain(self):
        t = threading.Thread(target=legacy._INGEST_NOTIFY_QUEUE.join, daemon=True)
        t.start(); t.join(10)
        self.assertFalse(t.is_alive(), "queue did not drain in 10 s")

    def test_n1_sent_once_and_duplicate_suppressed(self):
        self.assertTrue(self.q()); self.assertFalse(self.q())
        self.drain()
        self.assertEqual(len(self.rec.sent), 1)

    def test_n2_telegram_failure_never_raises_and_worker_survives(self):
        self.rec.mode = "raise"
        self.assertTrue(self.q(source_id="a"))
        self.drain()
        self.assertEqual(self.rec.sent, [])
        self.assertTrue(self.q(source_id="b"))                       # worker still alive after the exception
        self.drain()
        self.assertEqual(len(self.rec.sent), 1)

    def test_n3_fifo_order_and_caller_not_blocked_by_slow_send(self):
        self.rec.gate = threading.Event()
        t0 = time.monotonic()
        for i in range(5):
            self.q(source_id=f"f{i}")
        self.assertLess(time.monotonic() - t0, 1.0, "enqueue blocked behind a stalled Telegram send")
        self.rec.gate.set(); self.drain()
        order = [next(i for i in range(5) if f"f{i}" in m) for m in self.rec.sent]
        self.assertEqual(order, [0, 1, 2, 3, 4])

    def test_n4_distinct_stages_and_sources_are_not_merged(self):
        self.q(stage="RECEIVED"); self.q(stage="COMPLETED", result={"format": "x"}); self.q(source_id="other")
        self.drain()
        self.assertEqual(len(self.rec.sent), 3)

    def test_n5_finding_format_exception_burns_the_dedupe_key(self):
        """FINDING F187-1: the key is added to the seen-set BEFORE the message is formatted.  If formatting raises once (bad input), the
        caller gets False, nothing is sent, and the SAME event can never be notified again in this process (retry returns False)."""
        with patch.object(ingestion_notify, "format_ingestion_message", side_effect=ValueError("bad input")):
            self.assertFalse(self.q(source_id="burn"))
        self.assertFalse(self.q(source_id="burn"), "FINDING F187-1 (pinned): retry with a valid formatter is suppressed")
        self.drain()
        self.assertEqual(self.rec.sent, [], "FINDING F187-1 (pinned): the notification is lost for the process lifetime")

    def test_n6_finding_errors_sharing_a_180_char_prefix_are_merged(self):
        """FINDING F187-2: the ERROR fingerprint is error[:180]; two different errors for one source with an identical 180-char prefix
        (e.g. a long path/URL prefix) collapse into one notification."""
        prefix = "E" * 180
        self.assertTrue(self.q(stage="ERROR", error=prefix + " cause=A"))
        self.assertFalse(self.q(stage="ERROR", error=prefix + " cause=B"), "FINDING F187-2 (pinned)")
        self.drain()
        self.assertEqual(len(self.rec.sent), 1)

    def test_n7_finding_dedupe_set_overflow_clears_and_allows_repeats(self):
        """FINDING F187-3: at >5000 keys the whole set is cleared; an event already notified is notified AGAIN (bounded memory, not exactly-once)."""
        self.assertTrue(self.q(source_id="dup")); self.drain()
        for i in range(5001):
            legacy._INGEST_NOTIFY_SEEN.add(("X", "k", str(i), ""))
        self.assertTrue(self.q(source_id="trigger"))                 # pushes size over 5000 -> clear
        self.assertTrue(self.q(source_id="dup"), "FINDING F187-3 (pinned): repeat after overflow")
        self.drain()
        self.assertEqual(sum("dup" in m for m in self.rec.sent), 2)

    def test_n8_finding_dedupe_is_per_process_so_a_restart_resends(self):
        """FINDING F187-4: seen-set is in memory; a restart (simulated by clear) re-notifies RECEIVED for the same source."""
        self.q(); self.drain()
        legacy._INGEST_NOTIFY_SEEN.clear()
        self.assertTrue(self.q()); self.drain()
        self.assertEqual(len(self.rec.sent), 2)


if __name__ == "__main__":
    unittest.main()
