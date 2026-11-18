"""Domain enumerations shared by the models, schemas, services and API."""

from __future__ import annotations

from enum import Enum


class ProspectStatus(str, Enum):
    IMPORTED = "imported"
    VALIDATED = "validated"
    RESEARCHING = "researching"
    RESEARCHED = "researched"
    GENERATING = "generating"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class MessageStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    SENT = "sent"


class TaskType(str, Enum):
    RESEARCH = "research"
    GENERATE_MESSAGE = "generate_message"
    EXECUTE = "execute"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED)


class Tone(str, Enum):
    PROFESSIONAL = "professional"
    FRIENDLY = "friendly"
    CONCISE = "concise"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
