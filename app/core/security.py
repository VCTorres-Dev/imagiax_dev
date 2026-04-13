"""
app.core.security — Rate limiting and brute-force protection.

Extracted from servidor_api_orthanc_v1.py (_RateLimiter class).

Usage:
    from app.core.security import RateLimiter

    _analysis_limiter = RateLimiter(max_requests=10, window_seconds=60)

    # In a middleware or dependency:
    if not _analysis_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes.")
"""

from __future__ import annotations

import collections
import time
from typing import Dict


class RateLimiter:
    """Simple in-process sliding-window rate limiter keyed by an arbitrary string (IP, user_id, etc.).

    Thread-safety note: This implementation uses a plain dict + deque.
    It is safe for use in a single-process asyncio server (no thread contention).
    For multi-process deployments, use a shared backend (Redis, etc.) instead.

    Args:
        max_requests: Maximum number of requests allowed within window_seconds.
        window_seconds: Length of the sliding window in seconds.
    """

    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: Dict[str, collections.deque] = {}
        self._last_cleanup: float = 0.0

    def check(self, key: str) -> bool:
        """Return True if the request is within the limit, False if it should be rejected.

        Side effect: records this request attempt against key.
        """
        now = time.monotonic()

        # Evict fully-empty buckets every 5 minutes to bound memory usage.
        # Prevents bots from accumulating thousands of stale keys.
        if now - self._last_cleanup > 300:
            self._hits = {k: v for k, v in self._hits.items() if v}
            self._last_cleanup = now

        q = self._hits.setdefault(key, collections.deque())

        # Remove timestamps outside the current window
        while q and q[0] < now - self._window:
            q.popleft()

        if len(q) >= self._max:
            return False

        q.append(now)
        return True

    def reset(self, key: str) -> None:
        """Clear all recorded hits for key (useful after successful login, etc.)."""
        self._hits.pop(key, None)
