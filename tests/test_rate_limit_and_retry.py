"""Rate limiting, retry classification and exponential backoff."""

from __future__ import annotations

import asyncio
import random
import time

import pytest

from app.core.errors import (
    AccessBlockedError,
    AutomationTimeoutError,
    LLMAuthError,
    LLMUnavailableError,
    NavigationError,
    RobotsDisallowedError,
    is_retryable,
)
from app.core.rate_limit import KeyedRateLimiter, TokenBucket
from app.core.retry import RetryPolicy, retry_async

pytestmark = pytest.mark.anyio


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# ---- token bucket --------------------------------------------------------------------------------
async def test_bucket_allows_burst_then_waits() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, capacity=3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        assert await bucket.acquire() == 0  # burst
    waited = await bucket.acquire()
    assert waited == pytest.approx(0.5)  # 1 token at 2 tokens/s
    assert clock.sleeps == [pytest.approx(0.5)]


async def test_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, capacity=2, clock=clock, sleep=clock.sleep)
    assert bucket.try_acquire() and bucket.try_acquire()
    assert not bucket.try_acquire()
    clock.now += 1.0
    assert bucket.try_acquire()


async def test_bucket_never_exceeds_capacity() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=10.0, capacity=2, clock=clock, sleep=clock.sleep)
    clock.now += 100
    assert bucket.available == 2


async def test_bucket_limits_real_throughput() -> None:
    bucket = TokenBucket(rate=20.0, capacity=1)
    start = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(6)))
    assert time.monotonic() - start >= 0.24  # 5 waits of 50 ms after the first token


async def test_keyed_limiter_isolates_hosts() -> None:
    clock = FakeClock()
    limiter = KeyedRateLimiter(rate=1.0, capacity=1, clock=clock, sleep=clock.sleep)
    assert await limiter.acquire("a.example") == 0
    assert await limiter.acquire("b.example") == 0  # separate budget
    assert await limiter.acquire("a.example") == pytest.approx(1.0)


def test_bucket_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1)
    with pytest.raises(ValueError):
        TokenBucket(rate=1, capacity=0)


# ---- classification --------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc, retryable",
    [
        (AutomationTimeoutError("t"), True),
        (NavigationError("n"), True),
        (LLMUnavailableError("u"), True),
        (TimeoutError(), True),
        (ConnectionError(), True),
        (AccessBlockedError("b"), False),
        (RobotsDisallowedError("r"), False),
        (LLMAuthError("a"), False),
        (ValueError("bug"), False),
    ],
)
def test_retry_classification(exc: BaseException, retryable: bool) -> None:
    assert is_retryable(exc) is retryable


# ---- backoff ------------------------------------------------------------------------------------------
def test_backoff_is_exponential_and_capped() -> None:
    policy = RetryPolicy(max_attempts=6, base_delay=1.0, max_delay=5.0, multiplier=2.0, jitter=0.0)
    assert [policy.delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_jitter_stays_within_bounds() -> None:
    policy = RetryPolicy(base_delay=2.0, jitter=0.25)
    rng = random.Random(7)
    delays = [policy.delay_for(1, rng) for _ in range(200)]
    assert all(1.5 <= d <= 2.5 for d in delays)
    assert len({round(d, 6) for d in delays}) > 50


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(jitter=1.5)


async def test_retry_succeeds_after_transient_failures() -> None:
    clock, calls = FakeClock(), []

    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise NavigationError("503")
        return "ok"

    seen: list[int] = []
    result = await retry_async(
        flaky,
        RetryPolicy(max_attempts=4, base_delay=1.0, jitter=0.0),
        sleep=clock.sleep,
        on_retry=lambda attempt, exc, delay: seen.append(attempt),
    )
    assert result == "ok" and len(calls) == 3
    assert clock.sleeps == [1.0, 2.0]
    assert seen == [1, 2]


async def test_retry_stops_immediately_on_non_retryable_error() -> None:
    clock, calls = FakeClock(), []

    async def blocked() -> None:
        calls.append(1)
        raise AccessBlockedError("429")

    with pytest.raises(AccessBlockedError):
        await retry_async(blocked, RetryPolicy(max_attempts=5), sleep=clock.sleep)
    assert len(calls) == 1 and clock.sleeps == []


async def test_retry_is_bounded() -> None:
    clock, calls = FakeClock(), []

    async def always_down() -> None:
        calls.append(1)
        raise LLMUnavailableError("503")

    with pytest.raises(LLMUnavailableError):
        await retry_async(always_down, RetryPolicy(max_attempts=3, jitter=0.0), sleep=clock.sleep)
    assert len(calls) == 3 and len(clock.sleeps) == 2


async def test_retry_does_not_swallow_cancellation() -> None:
    async def slow() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(retry_async(slow, RetryPolicy(max_attempts=3)))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
