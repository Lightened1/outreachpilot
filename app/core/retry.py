"""Bounded retries with exponential backoff and jitter.

Only errors classified as retryable (see ``app.core.errors.is_retryable``) are retried; anything
else is raised immediately. The number of attempts is always bounded.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.core.errors import is_retryable
from app.core.logging import get_logger

T = TypeVar("T")
log = get_logger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.1  # +/- fraction of the delay

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay < 0 or self.max_delay < 0:
            raise ValueError("delays must be non-negative")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")
        if not 0 <= self.jitter < 1:
            raise ValueError("jitter must be in [0, 1)")

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay to wait after failed attempt number ``attempt`` (1-based)."""
        delay = min(self.max_delay, self.base_delay * self.multiplier ** (attempt - 1))
        if self.jitter and delay:
            r = (rng or random).uniform(-self.jitter, self.jitter)
            delay = max(0.0, delay * (1 + r))
        return delay


RetryCallback = Callable[[int, BaseException, float], Awaitable[None] | None]


async def retry_async(
    func: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    classify: Callable[[BaseException], bool] = is_retryable,
    on_retry: RetryCallback | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``func`` until it succeeds, a non-retryable error occurs or attempts run out.

    ``on_retry(attempt, exc, delay)`` is called before each backoff sleep. Cancellation is
    never swallowed.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await func()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt >= policy.max_attempts or not classify(exc):
                raise
            delay = policy.delay_for(attempt)
            log.warning(
                "retry.scheduled", extra={"attempt": attempt, "delay_s": round(delay, 3), "error": type(exc).__name__}
            )
            if on_retry is not None:
                result = on_retry(attempt, exc, delay)
                if asyncio.iscoroutine(result):
                    await result
            await sleep(delay)
