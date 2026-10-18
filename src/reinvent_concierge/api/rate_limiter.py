"""Token bucket rate limiter for outgoing AWS Events API requests."""

import asyncio
import time
from typing import Optional


class TokenBucketRateLimiter:
    """Allows ``rate_per_second`` requests per one-second window.

    The (N+1)th request in a window waits until the next window starts.
    """

    def __init__(self, rate_per_second: int = 5):
        if rate_per_second < 1:
            raise ValueError("rate_per_second must be at least 1")

        self.rate_per_second = rate_per_second
        self.tokens = rate_per_second
        self.window_started = self._now()
        # Created on first use so the lock binds to the running event loop.
        self._lock: Optional[asyncio.Lock] = None

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """Take one token, waiting for the next window if necessary.

        Args:
            timeout: Maximum seconds to wait; None waits indefinitely

        Returns:
            True if a token was acquired, False on timeout
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        started = self._now()

        while True:
            async with self._lock:
                now = self._now()
                self._refill_tokens(now)
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True

                wait_time = self.window_started + 1.0 - now
                if timeout is not None:
                    remaining = timeout - (now - started)
                    if remaining <= 0:
                        return False
                    wait_time = min(wait_time, remaining)

            await asyncio.sleep(wait_time)

    def _refill_tokens(self, now: float) -> None:
        """Restore full capacity at each one-second boundary."""
        elapsed = now - self.window_started
        if elapsed >= 1.0:
            self.window_started += int(elapsed)
            self.tokens = self.rate_per_second

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    async def __aenter__(self) -> "TokenBucketRateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    def get_current_tokens(self) -> int:
        """Current token count (for monitoring)."""
        self._refill_tokens(self._now())
        return self.tokens
