"""Async rate limiting.

``TokenBucket`` allows ``rate`` operations per second on average with bursts up to ``capacity``.
``KeyedRateLimiter`` keeps one bucket per key (for example per website host), so automation
against one site never borrows another site's budget.

The clock and sleep functions are injectable, which keeps the tests fast and deterministic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.rate = rate
        self.capacity = capacity
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Take tokens without waiting. Returns False if not enough are available."""
        self._refill()
        if self._tokens >= tokens:
            self._tokens -= tokens
            return True
        return False

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until ``tokens`` are available, take them and return the time waited (seconds)."""
        if tokens > self.capacity:
            raise ValueError("cannot acquire more tokens than the bucket capacity")
        waited = 0.0
        async with self._lock:  # FIFO fairness between waiters
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                delay = (tokens - self._tokens) / self.rate
                await self._sleep(delay)
                waited += delay


class KeyedRateLimiter:
    """One token bucket per key, created on first use."""

    def __init__(self, rate: float, capacity: float, **bucket_kwargs: object) -> None:
        self.rate = rate
        self.capacity = capacity
        self._bucket_kwargs = bucket_kwargs
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            self._buckets[key] = TokenBucket(self.rate, self.capacity, **self._bucket_kwargs)  # type: ignore[arg-type]
        return self._buckets[key]

    async def acquire(self, key: str, tokens: float = 1.0) -> float:
        return await self.bucket(key).acquire(tokens)

    @classmethod
    def per_minute(cls, per_minute: float, burst: float, **kwargs: object) -> KeyedRateLimiter:
        return cls(rate=per_minute / 60.0, capacity=burst, **kwargs)
