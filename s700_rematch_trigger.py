"""Coalescing trailing-pass runner for S700 post-raw rematch.

Every request marks work pending. If a worker is already running, the caller
returns immediately, but the active worker performs another pass before it
becomes idle. This prevents a late raw_calls batch from being lost merely
because it arrived while a previous rematch was in progress.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class CoalescingDrain:
    def __init__(self, thread_name: str = "coalescing-drain"):
        self._lock = threading.Lock()
        self._pending = threading.Event()
        self._thread_name = thread_name

    def request(
        self,
        run_once: Callable[[], None],
        *,
        on_coalesced: Callable[[], None] | None = None,
        on_trailing: Callable[[], None] | None = None,
    ) -> None:
        self._pending.set()

        def _worker() -> None:
            if not self._lock.acquire(blocking=False):
                if on_coalesced is not None:
                    on_coalesced()
                return
            try:
                while True:
                    self._pending.clear()
                    run_once()
                    if not self._pending.is_set():
                        break
                    if on_trailing is not None:
                        on_trailing()
            finally:
                self._lock.release()
                # Close the small race where a request arrives after the final
                # pending check but immediately before lock release.
                if self._pending.is_set():
                    self.request(
                        run_once,
                        on_coalesced=on_coalesced,
                        on_trailing=on_trailing,
                    )

        threading.Thread(
            target=_worker,
            name=self._thread_name,
            daemon=True,
        ).start()

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Test/diagnostic helper. Production callers do not need to wait."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if not self._lock.locked() and not self._pending.is_set():
                return True
            time.sleep(0.005)
        return not self._lock.locked() and not self._pending.is_set()
