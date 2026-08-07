"""Per-token and per-patient rate limiting (BUILD_SPEC 4.5).

A fixed-window counter held in process memory.

**Known limitation, recorded in docs/decisions.md:** the counters are per-process, so with N API
workers the effective ceiling is N times the configured one. That is acceptable for a
single-instance demo deployment and is not acceptable for production. BUILD_SPEC asks for Redis
to be justified before adding it, and a shared counter store is the justification if this ever
runs multi-instance — the interface below is deliberately narrow so swapping the backing store
touches one class.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    #: Seconds until the current window rolls over; sent as Retry-After on a 429.
    retry_after_seconds: int


class FixedWindowRateLimiter:
    """Counts events per key per window."""

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window_seconds = window_seconds
        self._counters: dict[str, tuple[int, int]] = {}  # key -> (window_start, count)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int) -> RateLimitDecision:
        """Record one event against ``key`` and say whether it is allowed."""
        now = int(time.monotonic())
        window_start = now - (now % self._window_seconds)

        with self._lock:
            stored_start, count = self._counters.get(key, (window_start, 0))
            if stored_start != window_start:
                stored_start, count = window_start, 0

            allowed = count < limit
            if allowed:
                count += 1
            self._counters[key] = (stored_start, count)

        retry_after = max(1, (window_start + self._window_seconds) - now)
        return RateLimitDecision(
            allowed=allowed,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after_seconds=retry_after,
        )

    def reset(self) -> None:
        """Clear all counters. Used by tests so one test's traffic cannot fail another's."""
        with self._lock:
            self._counters.clear()


#: Process-wide limiter. One instance so per-token and per-patient keys share a window.
limiter = FixedWindowRateLimiter()
