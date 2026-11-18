"""SQLite-backed implementation of ``app.core.tasks.TaskStore``."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import ProspectStatus, TaskStatus
from app.core.tasks import TaskEvent, TaskRecord, utcnow
from app.core.workflow import BUSY_STATES
from app.models import AuditEvent, Prospect, TaskRow


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _to_record(row: TaskRow) -> TaskRecord:
    return TaskRecord(
        id=row.id,
        type=row.type,
        prospect_id=row.prospect_id,
        status=TaskStatus(row.status),
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        created_at=_aware(row.created_at) or utcnow(),
        started_at=_aware(row.started_at),
        finished_at=_aware(row.finished_at),
        result=row.result,
        error=row.error,
        history=[TaskEvent.model_validate(e) for e in (row.history or [])],
    )


class SqlTaskStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def save(self, record: TaskRecord) -> None:
        async with self._sessions() as session:
            row = await session.get(TaskRow, record.id)
            if row is None:
                row = TaskRow(id=record.id)
                session.add(row)
            row.type = record.type
            row.prospect_id = record.prospect_id
            row.status = record.status.value
            row.attempts = record.attempts
            row.max_attempts = record.max_attempts
            row.created_at = record.created_at
            row.started_at = record.started_at
            row.finished_at = record.finished_at
            row.result = record.result
            row.error = record.error
            row.history = [e.model_dump(mode="json") for e in record.history]
            await session.commit()

    async def get(self, task_id: str) -> TaskRecord | None:
        async with self._sessions() as session:
            row = await session.get(TaskRow, task_id)
            return _to_record(row) if row else None

    async def list(self, *, prospect_id: int | None = None, limit: int = 50) -> list[TaskRecord]:
        async with self._sessions() as session:
            stmt = select(TaskRow).order_by(TaskRow.created_at.desc()).limit(limit)
            if prospect_id is not None:
                stmt = stmt.where(TaskRow.prospect_id == prospect_id)
            return [_to_record(r) for r in (await session.scalars(stmt)).all()]

    async def recover_interrupted(self) -> int:
        """Mark work that was in flight when the process stopped as failed, so nothing stays
        'running' forever and users can retry explicitly. Returns the number of tasks fixed."""
        now = datetime.now(UTC)
        error = {
            "code": "interrupted",
            "message": "The service restarted while this task was running.",
            "details": {},
            "retryable": True,
        }
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(TaskRow).where(
                        TaskRow.status.in_(
                            [TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.RETRYING.value]
                        )
                    )
                )
            ).all()
            for row in rows:
                row.status = TaskStatus.FAILED.value
                row.finished_at = now
                row.error = error
                row.history = [
                    *(row.history or []),
                    TaskEvent(
                        status=TaskStatus.FAILED, attempt=row.attempts, message="interrupted by restart", error=error
                    ).model_dump(mode="json"),
                ]
            prospects = (await session.scalars(select(Prospect).where(Prospect.status.in_(list(BUSY_STATES))))).all()
            for p in prospects:
                session.add(
                    AuditEvent(
                        prospect_id=p.id, event_type="workflow.interrupted", detail={"from_status": p.status.value}
                    )
                )
                p.status = ProspectStatus.FAILED
                p.last_error = {**error, "stage": "restart"}
            await session.commit()
            return len(rows)
