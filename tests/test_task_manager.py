"""Background task manager: statuses, retries, failure reporting, concurrency, timeouts, cancellation."""

from __future__ import annotations

import asyncio

import pytest

from app.core.enums import TaskStatus
from app.core.errors import AccessBlockedError, AppError, NavigationError, NotFoundError
from app.core.retry import RetryPolicy
from app.core.tasks import InMemoryTaskStore, TaskContext, TaskManager, TaskRecord

pytestmark = pytest.mark.anyio
FAST = RetryPolicy(max_attempts=3, base_delay=0.01, max_delay=0.02, jitter=0.0)


def manager(**kwargs) -> TaskManager:
    return TaskManager(InMemoryTaskStore(), default_policy=FAST, **kwargs)


def statuses(record: TaskRecord) -> list[str]:
    return [e.status.value for e in record.history]


async def test_successful_task_records_result_and_history() -> None:
    tm = manager()
    done: list[dict] = []

    async def job(ctx: TaskContext) -> dict:
        return {"task": ctx.task_id, "attempt": ctx.attempt}

    async def on_success(record: TaskRecord, result: dict) -> None:
        done.append(result)

    rec = await tm.submit("demo", job, prospect_id=7, on_success=on_success)
    assert rec.status is TaskStatus.QUEUED
    final = await tm.wait(rec.id, timeout=5)
    assert final.status is TaskStatus.SUCCEEDED
    assert final.result == {"task": rec.id, "attempt": 1}
    assert final.attempts == 1 and final.prospect_id == 7
    assert statuses(final) == ["queued", "running", "succeeded"]
    assert final.started_at and final.finished_at and done == [final.result]


async def test_transient_failures_are_retried_with_history() -> None:
    tm, calls = manager(), []

    async def job(ctx: TaskContext) -> dict:
        calls.append(ctx.attempt)
        if ctx.attempt < 3:
            raise NavigationError("503 from site")
        return {"ok": True}

    final = await tm.wait((await tm.submit("flaky", job)).id, timeout=5)
    assert final.status is TaskStatus.SUCCEEDED and calls == [1, 2, 3]
    assert statuses(final) == ["queued", "running", "retrying", "running", "retrying", "running", "succeeded"]
    assert final.history[2].error["code"] == "navigation_error"
    assert final.error is None


async def test_non_retryable_error_fails_immediately_and_calls_hook() -> None:
    tm, failures = manager(), []

    async def job(ctx: TaskContext) -> None:
        raise AccessBlockedError("challenge page", details={"status": 429})

    async def on_failure(record: TaskRecord, exc: BaseException) -> None:
        failures.append(type(exc).__name__)

    final = await tm.wait((await tm.submit("blocked", job, on_failure=on_failure)).id, timeout=5)
    assert final.status is TaskStatus.FAILED and final.attempts == 1
    assert final.error["code"] == "access_blocked" and final.error["retryable"] is False
    assert "non-retryable" in final.history[-1].message
    assert failures == ["AccessBlockedError"]


async def test_retries_are_bounded() -> None:
    tm = manager()

    async def job(ctx: TaskContext) -> None:
        raise NavigationError("still down")

    final = await tm.wait((await tm.submit("down", job)).id, timeout=5)
    assert final.status is TaskStatus.FAILED and final.attempts == 3
    assert "retries exhausted" in final.history[-1].message


async def test_unexpected_exceptions_are_not_retried() -> None:
    tm = manager()

    async def job(ctx: TaskContext) -> None:
        raise ZeroDivisionError("bug")

    final = await tm.wait((await tm.submit("bug", job)).id, timeout=5)
    assert final.status is TaskStatus.FAILED and final.attempts == 1
    assert final.error["code"] == "ZeroDivisionError"


async def test_attempt_timeout_is_retryable() -> None:
    tm = manager(default_timeout_s=0.05)

    async def job(ctx: TaskContext) -> dict:
        if ctx.attempt == 1:
            await asyncio.sleep(1)
        return {"attempt": ctx.attempt}

    final = await tm.wait((await tm.submit("slow", job)).id, timeout=5)
    assert final.status is TaskStatus.SUCCEEDED and final.result == {"attempt": 2}
    assert final.history[2].error["code"] == "TimeoutError"


async def test_concurrency_limit_is_respected() -> None:
    tm = manager(max_concurrency=2)
    running, peak = 0, 0

    async def job(ctx: TaskContext) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    records = [await tm.submit("work", job) for _ in range(6)]
    for rec in records:
        assert (await tm.wait(rec.id, timeout=5)).status is TaskStatus.SUCCEEDED
    assert peak == 2


async def test_cancel_running_task() -> None:
    tm, failures = manager(), []
    started = asyncio.Event()

    async def job(ctx: TaskContext) -> None:
        started.set()
        await asyncio.sleep(10)

    async def on_failure(record: TaskRecord, exc: BaseException) -> None:
        failures.append(exc)

    rec = await tm.submit("long", job, on_failure=on_failure)
    await started.wait()
    final = await tm.cancel(rec.id)
    assert final.status is TaskStatus.CANCELLED
    assert final.error["code"] == "task_cancelled"
    assert len(failures) == 1


async def test_shutdown_cancels_everything_and_refuses_new_work() -> None:
    tm = manager(max_concurrency=1)

    async def job(ctx: TaskContext) -> None:
        await asyncio.sleep(10)

    recs = [await tm.submit("long", job) for _ in range(3)]  # 1 running, 2 queued
    await asyncio.sleep(0.02)
    await tm.shutdown(timeout=2)
    for rec in recs:
        assert (await tm.get(rec.id)).status is TaskStatus.CANCELLED
    with pytest.raises(AppError):
        await tm.submit("late", job)


async def test_unknown_task_is_not_found() -> None:
    with pytest.raises(NotFoundError):
        await manager().get("nope")


async def test_failing_hook_does_not_break_the_task() -> None:
    tm = manager()

    async def job(ctx: TaskContext) -> dict:
        return {}

    async def bad_hook(record: TaskRecord, result: dict) -> None:
        raise RuntimeError("hook bug")

    final = await tm.wait((await tm.submit("x", job, on_success=bad_hook)).id, timeout=5)
    assert final.status is TaskStatus.SUCCEEDED
