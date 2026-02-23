from __future__ import annotations

import asyncio
import os
import time


class AsyncTokenBucketRateLimiter:
    """Process-wide async token bucket rate limiter."""

    def __init__(self, *, rate_per_sec: float, burst: int) -> None:
        self._rate_per_sec = max(0.1, float(rate_per_sec))
        self._capacity = max(1.0, float(burst))
        self._tokens = float(self._capacity)
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        need = max(0.1, float(tokens))
        while True:
            wait_for = 0.0
            async with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self._updated_at)
                self._updated_at = now
                self._tokens = min(
                    self._capacity, self._tokens + (elapsed * self._rate_per_sec)
                )

                if self._tokens >= need:
                    self._tokens -= need
                    return

                deficit = need - self._tokens
                wait_for = deficit / self._rate_per_sec
            await asyncio.sleep(wait_for)


_GLOBAL_LIMITER: AsyncTokenBucketRateLimiter | None = None


def get_global_rate_limiter() -> AsyncTokenBucketRateLimiter:
    global _GLOBAL_LIMITER
    if _GLOBAL_LIMITER is not None:
        return _GLOBAL_LIMITER

    # Safe defaults for KIS bursty paths. Tunable at runtime via env.
    rps = float(os.environ.get("KIS_API_RATE_LIMIT_RPS", "4.0") or 4.0)
    burst = int(float(os.environ.get("KIS_API_RATE_LIMIT_BURST", "4") or 4))
    _GLOBAL_LIMITER = AsyncTokenBucketRateLimiter(rate_per_sec=rps, burst=burst)
    return _GLOBAL_LIMITER
