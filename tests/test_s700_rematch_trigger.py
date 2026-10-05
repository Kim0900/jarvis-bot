import threading
import unittest

from s700_rematch_trigger import CoalescingDrain


class CoalescingDrainTests(unittest.TestCase):
    def test_request_during_active_pass_forces_trailing_pass(self):
        gate = CoalescingDrain("test-s700-drain")
        first_started = threading.Event()
        release_first = threading.Event()
        calls = []

        def run_once():
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                first_started.set()
                self.assertTrue(release_first.wait(1.0))

        gate.request(run_once)
        self.assertTrue(first_started.wait(1.0))

        # This request occurs while the first pass owns the lock. It must not be
        # discarded: the active worker must drain one more pass afterward.
        gate.request(run_once)
        release_first.set()

        self.assertTrue(gate.wait_idle(2.0))
        self.assertEqual(calls, [1, 2])

    def test_single_request_runs_once(self):
        gate = CoalescingDrain("test-s700-single")
        calls = []
        gate.request(lambda: calls.append(1))
        self.assertTrue(gate.wait_idle(1.0))
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
