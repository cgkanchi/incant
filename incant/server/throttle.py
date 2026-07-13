"""In-memory, per-client-IP throttling of FAILED bearer authentications.

A brute-force / credential-stuffing brake: an IP that racks up `limit` failed auths
within `window` seconds is refused with 429 (Retry-After) until the sliding window
drains. Successful auth is never counted and never throttled. State is process-local
(no shared store) — good enough as a per-node brake in front of the real defense,
which is that keys are high-entropy and unguessable.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict


class AuthThrottler:
    def __init__(self) -> None:
        self._fails: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        # Injectable clock (monotonic) so tests can drive window expiry deterministically.
        self._now = time.monotonic

    def _prune(self, dq: Deque[float], now: float, window: float) -> None:
        cutoff = now - window
        while dq and dq[0] <= cutoff:
            dq.popleft()

    def retry_after(self, ip: str, limit: int, window: float) -> float | None:
        """If `ip` is currently over the limit, seconds until it drops back under
        (>=1); otherwise None. limit<=0 disables throttling."""
        if limit <= 0:
            return None
        now = self._now()
        with self._lock:
            dq = self._fails.get(ip)
            if dq is None:
                return None
            self._prune(dq, now, window)
            if not dq:
                self._fails.pop(ip, None)
                return None
            if len(dq) >= limit:
                return max(1.0, window - (now - dq[0]))
            return None

    def record_failure(self, ip: str, window: float) -> None:
        now = self._now()
        with self._lock:
            dq = self._fails[ip]
            dq.append(now)
            self._prune(dq, now, window)
