from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.core.tasks import TaskEvent, TaskRecord


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    database: str
    config: dict[str, Any]
    tasks: dict[str, int]
    time: datetime


class TaskRead(TaskRecord):
    """API view of a background task, including its attempt history."""

    @classmethod
    def from_record(cls, record: TaskRecord) -> TaskRead:
        return cls.model_validate(record.model_dump())


__all__ = ["ErrorBody", "ErrorResponse", "HealthResponse", "TaskEvent", "TaskRead"]
