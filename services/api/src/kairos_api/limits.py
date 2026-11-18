"""Rate limits for endpoints that start work (ADR-015).

A sliding window per client address, in memory: the API is one process for one user,
so this guards against a stuck client or script, not a distributed attack (that is
the job of the edge, in M8).
"""

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self, *, limit: int, per_seconds: float, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._limit = limit
        self._window = per_seconds
        self._now = now
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> float | None:
        """Record a hit; return ``None`` if allowed, else seconds until one is."""
        now = self._now()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return self._window - (now - hits[0])
        hits.append(now)
        return None
