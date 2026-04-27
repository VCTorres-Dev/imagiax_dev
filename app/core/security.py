"""
app.core.security — Rate limiting and brute-force protection.

Extracted from servidor_api_orthanc_v1.py (_RateLimiter class).

Usage:
    from app.core.security import RateLimiter

    _analysis_limiter = RateLimiter(max_requests=10, window_seconds=60)

    # In a middleware or dependency:
    if not _analysis_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Demasiadas solicitudes.")

NOTE: This in-process rate limiter is a SECONDARY DEFENSE LAYER.
Primary rate limiting is enforced by nginx (see nginx-rate-zones.conf).
With N workers, the effective limit is N × max_requests in the worst case
(each worker has its own in-process dict — there is no shared state between workers).

Limits here are intentionally set at ~50% of the nginx limit so that even
with 4 workers the combined ceiling (4×) stays close to 2× the nginx limit,
providing a meaningful safety net without being so tight it causes false positives.

Example alignment:
  nginx api_zone     = 30r/m  → in-process max_requests = 15  (4 workers ≈ 60r/m worst-case)
  nginx analisis_zone = 10r/m → in-process max_requests = 5   (4 workers ≈ 20r/m worst-case)
  nginx auth_zone    =  5r/m  → in-process max_requests = 3   (4 workers ≈ 12r/m worst-case)

For a true shared rate limit across workers, replace this with a Redis-backed
implementation (e.g. redis-py + sliding window Lua script, or slowapi + redis).
"""

from __future__ import annotations

import collections
import time
from typing import Dict


class RateLimiter:
    """Simple in-process sliding-window rate limiter keyed by an arbitrary string (IP, user_id, etc.).

    # NOTE: This is a secondary defense layer.
    # Primary rate limiting is done by nginx (see nginx-rate-zones.conf).
    # In a multi-worker deployment each worker maintains its own in-process dict,
    # so the effective limit across all workers is: workers × max_requests.
    # Set max_requests to roughly half the nginx zone rate so the combined
    # ceiling remains bounded even under worst-case multi-worker scenarios.

    Thread-safety note: This implementation uses a plain dict + deque.
    It is safe for use in a single-process asyncio server (no thread contention).
    For multi-process deployments, use a shared backend (Redis, etc.) instead.

    Args:
        max_requests: Maximum number of requests allowed within window_seconds.
                      Should be ≤ 50% of the corresponding nginx zone rate to keep
                      the multi-worker effective limit within 2× the nginx limit.
        window_seconds: Length of the sliding window in seconds.
    """

    def __init__(self, max_requests: int = 5, window_seconds: int = 60) -> None:
        # Default is 5r/m (half of the 10r/m nginx analisis_zone).
        # Callers should pass the rate that matches their nginx zone.
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
