"""Workflow orchestration: research -> generate -> approve/reject -> execute.

Every step validates the state transition, records an audit event and (for slow steps) submits a
background task. Task completion hooks apply the outcome: the success state, or ``failed`` /
``blocked`` with the error stored on the prospect. A per-prospect lock prevents two requests from
starting conflicting work at the same time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.message_agent import MessageAgent, build_facts
from app.automation.portal import DemoPortalClient
from app.config import Settings
from app.core.enums import ApprovalDecision, MessageStatus, ProspectStatus, TaskType
from app.core.errors import (
    AccessBlockedError,
    HostNotAllowedError,
    InvalidTransitionError,
    NotFoundError,
    RobotsDisallowedError,
    TaskCancelledError,
    ValidationFailedError,
    error_payload,
)
from app.core.logging import get_logger
from app.core.retry import RetryPolicy
from app.core.tasks import Job, TaskContext, TaskManager, TaskRecord
from app.core.workflow import ensure_transition
from app.models import OutreachMessage, Prospect
from app.schemas.workflow import ApprovalRequest, GenerateMessageRequest
from app.services.prospects import ProspectService, record_event

log = get_logger(__name__)

BLOCKING_ERRORS = (AccessBlockedError, RobotsDisallowedError, HostNotAllowedError)


class WorkflowService:
    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        tasks: TaskManager,
        portal: DemoPortalClient,
        agent: MessageAgent,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.tasks = tasks
        self.portal = portal
        self.agent = agent
        self.policy = RetryPolicy(
            max_attempts=settings.task_max_attempts,
            base_delay=settings.retry_base_delay_s,
            max_delay=settings.retry_max_delay_s,
        )
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, prospect_id: int) -> asyncio.Lock:
        return self._locks.setdefault(prospect_id, asyncio.Lock())

    # ---- shared plumbing ----------------------------------------------------------------------
    async def _begin(
        self,
        prospect_id: int,
        target: ProspectStatus,
        task_type: TaskType,
        job: Job,
        *,
        actor: str,
        success: Callable[[AsyncSession, Prospect, TaskRecord, dict[str, Any]], Awaitable[None]],
        precheck: Callable[[AsyncSession, Prospect], Awaitable[None]] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> TaskRecord:
        """Move the prospect into a busy state and submit the background job for it."""
        async with self._lock(prospect_id):
            async with self.sessions() as session:
                prospect = await ProspectService(session).get(prospect_id)
                previous = prospect.status
                ensure_transition(previous, target)  # state rules first (409), then step preconditions (422)
                if precheck is not None:
                    await precheck(session, prospect)
                prospect.status = target
                record_event(
                    session,
                    prospect_id,
                    f"{task_type.value}.requested",
                    actor=actor,
                    from_status=previous.value,
                    **(detail or {}),
                )
                await session.commit()
            try:
                record = await self.tasks.submit(
                    task_type.value,
                    job,
                    prospect_id=prospect_id,
                    policy=self.policy,
                    timeout_s=self.settings.task_timeout_s,
                    on_success=self._success_hook(prospect_id, target, task_type, success),
                    on_failure=self._failure_hook(prospect_id, target, task_type),
                )
            except Exception:
                async with self.sessions() as session:  # roll the state back if the task never started
                    prospect = await ProspectService(session).get(prospect_id)
                    prospect.status = previous
                    await session.commit()
                raise
            async with self.sessions() as session:
                record_event(session, prospect_id, "task.queued", task_id=record.id, task_type=task_type.value)
                await session.commit()
            return record

    def _success_hook(
        self,
        prospect_id: int,
        busy: ProspectStatus,
        task_type: TaskType,
        apply: Callable[[AsyncSession, Prospect, TaskRecord, dict[str, Any]], Awaitable[None]],
    ):
        async def hook(record: TaskRecord, result: dict[str, Any] | None) -> None:
            async with self.sessions() as session:
                prospect = await session.get(Prospect, prospect_id)
                if prospect is None or prospect.status != busy:
                    log.warning("workflow.stale_result", extra={"prospect_id": prospect_id, "task_id": record.id})
                    return
                prospect.last_error = None
                await apply(session, prospect, record, result or {})
                await session.commit()

        return hook

    def _failure_hook(self, prospect_id: int, busy: ProspectStatus, task_type: TaskType):
        async def hook(record: TaskRecord, exc: BaseException) -> None:
            async with self.sessions() as session:
                prospect = await session.get(Prospect, prospect_id)
                if prospect is None or prospect.status != busy:
                    return
                blocked = isinstance(exc, BLOCKING_ERRORS)
                target = ProspectStatus.BLOCKED if blocked else ProspectStatus.FAILED
                prospect.status = ensure_transition(prospect.status, target)
                prospect.last_error = {"stage": task_type.value, "task_id": record.id, **error_payload(exc)}
                outcome = "cancelled" if isinstance(exc, TaskCancelledError) else ("blocked" if blocked else "failed")
                record_event(
                    session,
                    prospect_id,
                    f"{task_type.value}.{outcome}",
                    task_id=record.id,
                    attempts=record.attempts,
                    error=error_payload(exc),
                )
                await session.commit()

        return hook

    # ---- research ------------------------------------------------------------------------------
    async def start_research(self, prospect_id: int, *, actor: str = "api") -> TaskRecord:
        async with self.sessions() as session:
            prospect = await ProspectService(session).get(prospect_id)
            name, url = prospect.name, prospect.source_url
        if not url:
            raise ValidationFailedError(
                "This prospect has no source_url to research.", details={"prospect_id": prospect_id}
            )
        # Fail fast, before queueing, if the host is not allowed for automation.
        self.portal.engine.check_host(url)

        async def job(ctx: TaskContext) -> dict[str, Any]:
            result = await self.portal.research(url, expected_name=name)
            return result.model_dump(mode="json")

        async def success(
            session: AsyncSession, prospect: Prospect, record: TaskRecord, result: dict[str, Any]
        ) -> None:
            prospect.research_data = result
            prospect.status = ensure_transition(prospect.status, ProspectStatus.RESEARCHED)
            record_event(
                session,
                prospect.id,
                "research.completed",
                task_id=record.id,
                attempts=record.attempts,
                facts=sorted(k for k, v in result.items() if v and k not in ("screenshot", "profile_url")),
                screenshot=result.get("screenshot"),
            )

        return await self._begin(
            prospect_id,
            ProspectStatus.RESEARCHING,
            TaskType.RESEARCH,
            job,
            actor=actor,
            success=success,
            detail={"url": url},
        )

    # ---- generation ----------------------------------------------------------------------------
    async def start_generation(
        self, prospect_id: int, request: GenerateMessageRequest | None = None, *, actor: str = "api"
    ) -> TaskRecord:
        request = request or GenerateMessageRequest()

        async def precheck(session: AsyncSession, prospect: Prospect) -> None:
            if not prospect.research_data:
                raise ValidationFailedError(
                    "Research the prospect before generating a message, so the message can only use verified facts.",
                    details={"prospect_id": prospect.id},
                )

        async def job(ctx: TaskContext) -> dict[str, Any]:
            async with self.sessions() as session:
                prospect = await ProspectService(session).get(prospect_id)
                facts = build_facts(
                    {"name": prospect.name, "job_title": prospect.job_title, "company": prospect.company},
                    prospect.research_data,
                )
            message = await self.agent.generate(facts, tone=request.tone, extra_instructions=request.extra_instructions)
            return message.model_dump(mode="json")

        async def success(
            session: AsyncSession, prospect: Prospect, record: TaskRecord, result: dict[str, Any]
        ) -> None:
            await session.execute(
                update(OutreachMessage)
                .where(
                    OutreachMessage.prospect_id == prospect.id,
                    OutreachMessage.status.in_(
                        [MessageStatus.PENDING_APPROVAL, MessageStatus.APPROVED, MessageStatus.REJECTED]
                    ),
                )
                .values(status=MessageStatus.SUPERSEDED)
            )
            count = len(
                (
                    await session.scalars(select(OutreachMessage.id).where(OutreachMessage.prospect_id == prospect.id))
                ).all()
            )
            message = OutreachMessage(
                prospect_id=prospect.id,
                version=count + 1,
                tone=request.tone,
                subject=result["subject"],
                body=result["body"],
                facts_used=result.get("facts_used", []),
                model=result["model"],
                generation_attempts=result.get("attempts", 1),
                status=MessageStatus.PENDING_APPROVAL,
            )
            session.add(message)
            await session.flush()
            prospect.status = ensure_transition(prospect.status, ProspectStatus.PENDING_APPROVAL)
            record_event(
                session,
                prospect.id,
                "message.generated",
                task_id=record.id,
                message_id=message.id,
                version=message.version,
                tone=request.tone.value,
                model=message.model,
                validation_attempts=message.generation_attempts,
                facts_used=message.facts_used,
            )

        return await self._begin(
            prospect_id,
            ProspectStatus.GENERATING,
            TaskType.GENERATE_MESSAGE,
            job,
            actor=actor,
            success=success,
            precheck=precheck,
            detail={"tone": request.tone.value},
        )

    # ---- human approval ------------------------------------------------------------------------
    async def decide(self, prospect_id: int, request: ApprovalRequest) -> Prospect:
        async with self._lock(prospect_id):
            async with self.sessions() as session:
                prospect = await ProspectService(session).get(prospect_id)
                if prospect.status != ProspectStatus.PENDING_APPROVAL:
                    raise InvalidTransitionError(
                        "There is no message waiting for approval.", details={"status": prospect.status.value}
                    )
                message = (
                    await session.scalars(
                        select(OutreachMessage)
                        .where(
                            OutreachMessage.prospect_id == prospect_id,
                            OutreachMessage.status == MessageStatus.PENDING_APPROVAL,
                        )
                        .order_by(OutreachMessage.id.desc())
                        .limit(1)
                    )
                ).first()
                if message is None:
                    raise NotFoundError("No pending message found for this prospect.")
                now = datetime.now(UTC)
                message.reviewer, message.review_reason, message.reviewed_at = request.reviewer, request.reason, now
                if request.decision is ApprovalDecision.APPROVE:
                    message.status = MessageStatus.APPROVED
                    prospect.status = ensure_transition(prospect.status, ProspectStatus.APPROVED)
                else:
                    message.status = MessageStatus.REJECTED
                    prospect.status = ensure_transition(prospect.status, ProspectStatus.REJECTED)
                record_event(
                    session,
                    prospect_id,
                    f"message.{'approved' if request.decision is ApprovalDecision.APPROVE else 'rejected'}",
                    actor=request.reviewer,
                    message_id=message.id,
                    version=message.version,
                    reason=request.reason,
                )
                await session.commit()
                return prospect

    # ---- execution ---------------------------------------------------------------------------
    async def start_execution(self, prospect_id: int, *, actor: str = "api") -> TaskRecord:
        async with self.sessions() as session:
            prospect = await ProspectService(session).get(prospect_id)
            name, url = prospect.name, prospect.source_url
            message = (
                await session.scalars(
                    select(OutreachMessage)
                    .where(OutreachMessage.prospect_id == prospect_id, OutreachMessage.status == MessageStatus.APPROVED)
                    .order_by(OutreachMessage.id.desc())
                    .limit(1)
                )
            ).first()
        if message is None:
            raise InvalidTransitionError(
                "Only an approved message can be executed. Generate a message and have a reviewer approve it first.",
                details={"prospect_id": prospect_id},
            )
        if not url:
            raise ValidationFailedError("This prospect has no source_url to send the message through.")
        self.portal.engine.check_host(url)
        message_id, subject, body = message.id, message.subject, message.body

        async def job(ctx: TaskContext) -> dict[str, Any]:
            result = await self.portal.send_message(
                url,
                subject=subject,
                body=body,
                idempotency_key=f"outreachpilot-message-{message_id}",
                expected_name=name,
            )
            return result.model_dump(mode="json")

        async def success(
            session: AsyncSession, prospect: Prospect, record: TaskRecord, result: dict[str, Any]
        ) -> None:
            msg = await session.get(OutreachMessage, message_id)
            assert msg is not None
            msg.status = MessageStatus.SENT
            msg.sent_at = datetime.now(UTC)
            msg.execution_result = result
            prospect.status = ensure_transition(prospect.status, ProspectStatus.COMPLETED)
            record_event(
                session,
                prospect.id,
                "message.sent",
                task_id=record.id,
                message_id=message_id,
                confirmation_id=result.get("confirmation_id"),
                duplicate=result.get("duplicate"),
                screenshot=result.get("screenshot"),
            )

        return await self._begin(
            prospect_id,
            ProspectStatus.EXECUTING,
            TaskType.EXECUTE,
            job,
            actor=actor,
            success=success,
            detail={"message_id": message_id},
        )

    async def cancel(self, task_id: str) -> TaskRecord:
        return await self.tasks.cancel(task_id)
