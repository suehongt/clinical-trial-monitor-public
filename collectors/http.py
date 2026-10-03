"""HTTP scheduling primitives shared by direct registry adapters."""
from __future__ import annotations

import threading
import time


class RequestPacer:
    """Enforce a thread-safe minimum interval between request starts.

    A bounded worker pool can overlap network latency while this pacer keeps
    outbound traffic polite and predictable for the upstream registry.
    """

    def __init__(self, min_interval: float = 0.0):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_start - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_start = now + self.min_interval
