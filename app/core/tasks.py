"""Background task manager built on asyncio.

* every submitted job gets a persistent ``TaskRecord`` (status, attempts, result, error, history);
* a semaphore bounds how many jobs run at once; queued jobs wait for a slot;
* retryable failures are retried with exponential backoff, up to ``max_attempts``; the slot is
  released while a job waits for its next attempt;
* each attempt can have a timeout;
* jobs can be cancelled individually, and ``shutdown`` cancels everything gracefully.

Storage is behind the small ``TaskStore`` protocol. ``InMemoryTaskStore`` is used in tests; the
application uses a SQL-backed store (``app.services.task_store``). Moving to Redis/Celery later
means implementing the same protocol and replacing the ``asyncio`` runner, without touching
the services that submit work.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.core.enums import TaskStatus
from app.core.errors import AppError, NotFoundError, TaskCancelledError, error_payload, is_retryable
from app.core.logging import get_logger
from app.core.retry import RetryPolicy

log = get_logger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


class TaskEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    status: TaskStatus
    attempt: int
    message: str
    error: dict[str, Any] | None = None


class TaskRecord(BaseModel):
    id: str
    type: str
    prospect_id: int | None = None
    status: TaskStatus = TaskStatus.QUEUED
    attempts: int = 0
    max_attempts: int
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    history: list[TaskEvent] = Field(default_factory=list)

    def add_event(self, status: TaskStatus, message: str, error: dict[str, Any] | None = None) -> None:
        self.status = status
        self.history.append(TaskEvent(status=status, attempt=self.attempts, message=message, error=error))


class TaskStore(Protocol):
    async def save(self, record: TaskRecord) -> None: ...
    async def get(self, task_id: str) -> TaskRecord | None: ...
    async def list(self, *, prospect_id: int | None = None, limit: int = 50) -> list[TaskRecord]: ...


class InMemoryTaskStore:
    def __init__(self) -> None:
        self._records: dict[str, TaskRecord] = {}

    async def save(self, record: TaskRecord) -> None:
        self._records[record.id] = record.model_copy(deep=True)

    async def get(self, task_id: str) -> TaskRecord | None:
        rec = self._records.get(task_id)
        return rec.model_copy(deep=True) if rec else None

    async def list(self, *, prospect_id: int | None = None, limit: int = 50) -> list[TaskRecord]:
        recs = [r for r in self._records.values() if prospect_id is None or r.prospect_id == prospect_id]
        recs.sort(key=lambda r: r.created_at, reverse=True)
        return [r.model_copy(deep=True) for r in recs[:limit]]


@dataclass
class TaskContext:
    """Passed to every job so it knows which task and attempt it is running."""

    task_id: str
    task_type: str
    attempt: int
    prospect_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


JobResult = dict[str, Any] | None
Job = Callable[[TaskContext], Awaitable[JobResult]]
SuccessHook = Callable[[TaskRecord, JobResult], Awaitable[None]]
FailureHook = Callable[[TaskRecord, BaseException], Awaitable[None]]


class TaskManager:
    def __init__(
        self,
        store: TaskStore,
        *,
        max_concurrency: int = 2,
        default_policy: RetryPolicy | None = None,
        default_timeout_s: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.store = store
        self.max_concurrency = max_concurrency
        self.default_policy = default_policy or RetryPolicy()
        self.default_timeout_s = default_timeout_s
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._running: dict[str, asyncio.Task[None]] = {}
        self._accepting = True
        self._active = 0

    # ---- public API ---------------------------------------------------------------------------
    async def submit(
        self,
        task_type: str,
        job: Job,
        *,
        prospect_id: int | None = None,
        policy: RetryPolicy | None = None,
        timeout_s: float | None = None,
        on_success: SuccessHook | None = None,
        on_failure: FailureHook | None = None,
    ) -> TaskRecord:
        if not self._accepting:
            raise AppError("Task manager is shutting down; no new tasks are accepted.")
        policy = policy or self.default_policy
        record = TaskRecord(
            id=uuid.uuid4().hex, type=task_type, prospect_id=prospect_id, max_attempts=policy.max_attempts
        )
        record.add_event(TaskStatus.QUEUED, "queued")
        await self.store.save(record)
        # The runner mutates ``record``; callers get a snapshot of the state at submission time.
        snapshot = record.model_copy(deep=True)
        runner = asyncio.create_task(
            self._run(
                record,
                job,
                policy,
                timeout_s if timeout_s is not None else self.default_timeout_s,
                on_success,
                on_failure,
            ),
            name=f"task-{task_type}-{record.id[:8]}",
        )
        self._running[record.id] = runner
        runner.add_done_callback(lambda _t, tid=record.id: self._running.pop(tid, None))
        log.info("task.submitted", extra={"task_id": record.id, "task_type": task_type, "prospect_id": prospect_id})
        return snapshot

    async def get(self, task_id: str) -> TaskRecord:
        record = await self.store.get(task_id)
        if record is None:
            raise NotFoundError(f"Task '{task_id}' not found.")
        return record

    async def list(self, *, prospect_id: int | None = None, limit: int = 50) -> list[TaskRecord]:
        return await self.store.list(prospect_id=prospect_id, limit=limit)

    async def cancel(self, task_id: str) -> TaskRecord:
        """Cancel a queued or running task. Cancelling a finished task is a no-op."""
        runner = self._running.get(task_id)
        if runner is not None and not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        return await self.get(task_id)

    async def wait(self, task_id: str, timeout: float | None = None) -> TaskRecord:
        """Wait for a task to finish (used by tests and the CLI walkthrough)."""
        runner = self._running.get(task_id)
        if runner is not None:
            await asyncio.wait_for(asyncio.shield(runner), timeout)
        return await self.get(task_id)

    def stats(self) -> dict[str, int]:
        return {"max_concurrency": self.max_concurrency, "active": self._active, "pending": len(self._running)}

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Stop accepting work, cancel queued/running tasks and wait for them to record it."""
        self._accepting = False
        runners = [t for t in self._running.values() if not t.done()]
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.wait(runners, timeout=timeout)
        log.info("task_manager.stopped", extra={"cancelled": len(runners)})

    # ---- internals ----------------------------------------------------------------------------
    async def _run(
        self,
        record: TaskRecord,
        job: Job,
        policy: RetryPolicy,
        timeout_s: float | None,
        on_success: SuccessHook | None,
        on_failure: FailureHook | None,
    ) -> None:
        try:
            while True:
                async with self._semaphore:
                    self._active += 1
                    try:
                        record.attempts += 1
                        if record.started_at is None:
                            record.started_at = utcnow()
                        record.add_event(TaskStatus.RUNNING, f"attempt {record.attempts} started")
                        await self.store.save(record)
                        ctx = TaskContext(
                            task_id=record.id,
                            task_type=record.type,
                            attempt=record.attempts,
                            prospect_id=record.prospect_id,
                        )
                        result: dict[str, Any] | None = None
                        try:
                            coro = job(ctx)
                            result = await (asyncio.wait_for(coro, timeout_s) if timeout_s else coro)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - classified below
                            if isinstance(exc, asyncio.TimeoutError) and timeout_s:
                                exc = TimeoutError(f"attempt exceeded {timeout_s:g}s")
                            failure: BaseException | None = exc
                        else:
                            failure = None
                    finally:
                        self._active -= 1

                if failure is None:
                    record.result = result or {}
                    record.error = None
                    # Apply the result first: a task only reports a terminal status once its effects
                    # are visible, so a client that sees "succeeded" never reads stale data.
                    await self._call_hook(on_success, record, record.result)
                    record.finished_at = utcnow()
                    record.add_event(TaskStatus.SUCCEEDED, "succeeded")
                    await self.store.save(record)
                    log.info("task.succeeded", extra={"task_id": record.id, "attempts": record.attempts})
                    return

                payload = error_payload(failure)
                if is_retryable(failure) and record.attempts < policy.max_attempts:
                    delay = policy.delay_for(record.attempts)
                    record.error = payload
                    record.add_event(
                        TaskStatus.RETRYING, f"attempt {record.attempts} failed; retrying in {delay:.2f}s", payload
                    )
                    await self.store.save(record)
                    log.warning(
                        "task.retrying",
                        extra={
                            "task_id": record.id,
                            "attempt": record.attempts,
                            "delay_s": round(delay, 3),
                            "error": payload["code"],
                        },
                    )
                    await self._sleep(delay)
                    continue

                record.error = payload
                reason = "non-retryable error" if not is_retryable(failure) else "retries exhausted"
                await self._call_hook(on_failure, record, failure)  # before the terminal status (see above)
                record.finished_at = utcnow()
                record.add_event(TaskStatus.FAILED, f"failed after {record.attempts} attempt(s): {reason}", payload)
                await self.store.save(record)
                log.error(
                    "task.failed",
                    extra={
                        "task_id": record.id,
                        "attempts": record.attempts,
                        "error": payload["code"],
                        "reason": reason,
                    },
                )
                return
        except asyncio.CancelledError:
            exc = TaskCancelledError("Task was cancelled.")
            record.error = error_payload(exc)
            await asyncio.shield(self._call_hook(on_failure, record, exc))
            record.finished_at = utcnow()
            record.add_event(TaskStatus.CANCELLED, "cancelled", record.error)
            await asyncio.shield(self.store.save(record))
            log.warning("task.cancelled", extra={"task_id": record.id})

    @staticmethod
    async def _call_hook(hook: Callable[..., Awaitable[None]] | None, *args: Any) -> None:
        if hook is None:
            return
        try:
            await hook(*args)
        except Exception:
            log.exception("task.hook_failed")
