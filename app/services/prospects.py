"""Prospect CRUD with validation, duplicate detection and an audit trail."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ProspectStatus
from app.core.errors import DuplicateProspectError, InvalidTransitionError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.core.workflow import BUSY_STATES, allowed_next, ensure_transition
from app.models import AuditEvent, OutreachMessage, Prospect
from app.schemas.prospect import (
    ImportReport,
    ImportRowResult,
    MessageRead,
    ProspectCreate,
    ProspectDetail,
    ProspectRead,
    ProspectUpdate,
    dedupe_key,
    parse_import_csv,
)

log = get_logger(__name__)

ProspectSort = Literal["id", "-id", "-updated"]
_SORTS = {"id": Prospect.id.asc(), "-id": Prospect.id.desc(), "-updated": Prospect.updated_at.desc()}


def record_event(
    session: AsyncSession,
    prospect_id: int | None,
    event_type: str,
    *,
    actor: str = "system",
    task_id: str | None = None,
    **detail: Any,
) -> None:
    """Append an audit event to the current unit of work (committed with it)."""
    session.add(AuditEvent(prospect_id=prospect_id, task_id=task_id, event_type=event_type, actor=actor, detail=detail))


class ProspectService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, prospect_id: int) -> Prospect:
        prospect = await self.session.get(Prospect, prospect_id)
        if prospect is None:
            raise NotFoundError(f"Prospect {prospect_id} not found.", details={"prospect_id": prospect_id})
        return prospect

    async def find_duplicate(self, key: str, *, exclude_id: int | None = None) -> Prospect | None:
        stmt = select(Prospect).where(Prospect.dedupe_key == key)
        if exclude_id is not None:
            stmt = stmt.where(Prospect.id != exclude_id)
        return (await self.session.scalars(stmt)).first()

    async def create(self, data: ProspectCreate, *, actor: str = "api") -> Prospect:
        key = data.dedupe_key
        existing = await self.find_duplicate(key)
        if existing is not None:
            raise DuplicateProspectError(
                "A prospect with the same identity already exists.",
                details={"existing_id": existing.id, "dedupe_key": key},
            )
        prospect = Prospect(**data.model_dump(), dedupe_key=key, status=ProspectStatus.IMPORTED)
        self.session.add(prospect)
        try:
            await self.session.flush()
        except IntegrityError as exc:  # a concurrent insert won the race
            await self.session.rollback()
            dup = await self.find_duplicate(key)
            raise DuplicateProspectError(
                "A prospect with the same identity already exists.",
                details={"existing_id": dup.id if dup else None, "dedupe_key": key},
            ) from exc
        record_event(
            self.session,
            prospect.id,
            "prospect.imported",
            actor=actor,
            fields=sorted(k for k, v in data.model_dump().items() if v),
        )
        # Import -> Validate: the payload passed schema validation; record the business checks too.
        prospect.status = ensure_transition(prospect.status, ProspectStatus.VALIDATED)
        record_event(
            self.session, prospect.id, "prospect.validated", actor="system", research_ready=bool(prospect.source_url)
        )
        await self.session.commit()
        log.info("prospect.created", extra={"prospect_id": prospect.id})
        return prospect

    async def list(
        self,
        *,
        status: ProspectStatus | None = None,
        statuses: list[ProspectStatus] | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: ProspectSort = "id",
    ) -> tuple[list[Prospect], int]:
        stmt = select(Prospect)
        if status is not None:
            stmt = stmt.where(Prospect.status == status)
        if statuses:
            stmt = stmt.where(Prospect.status.in_(statuses))
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(
                or_(Prospect.name.ilike(like), Prospect.company.ilike(like), Prospect.job_title.ilike(like))
            )
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        order = (_SORTS[sort], Prospect.id.desc()) if sort == "-updated" else (_SORTS[sort],)
        rows = (await self.session.scalars(stmt.order_by(*order).limit(limit).offset(offset))).all()
        return list(rows), int(total or 0)

    async def update(self, prospect_id: int, data: ProspectUpdate, *, actor: str = "api") -> Prospect:
        prospect = await self.get(prospect_id)
        if prospect.status in BUSY_STATES:
            raise InvalidTransitionError(
                "The prospect is being processed by a background task; try again when it finishes.",
                details={"status": prospect.status.value},
            )
        changes = data.model_dump(exclude_unset=True)
        if "name" in changes and changes["name"] is None:
            changes.pop("name")  # name is required; ignore an explicit null
        if not changes:
            return prospect
        merged = {f: changes.get(f, getattr(prospect, f)) for f in ("name", "company", "source_url")}
        if not merged["source_url"] and not merged["company"]:
            raise ValidationFailedError("A prospect needs a source_url or a company.")
        key = dedupe_key(merged["name"], merged["company"], merged["source_url"])
        if key != prospect.dedupe_key:
            existing = await self.find_duplicate(key, exclude_id=prospect.id)
            if existing is not None:
                raise DuplicateProspectError(
                    "Another prospect already has this identity.",
                    details={"existing_id": existing.id, "dedupe_key": key},
                )
            prospect.dedupe_key = key
        for field, value in changes.items():
            setattr(prospect, field, value)
        record_event(self.session, prospect.id, "prospect.updated", actor=actor, fields=sorted(changes))
        await self.session.commit()
        return prospect

    async def delete(self, prospect_id: int, *, actor: str = "api") -> None:
        """Delete a prospect with its messages and audit trail. Refused while a task is working on it."""
        prospect = await self.get(prospect_id)
        if prospect.status in BUSY_STATES:
            raise InvalidTransitionError(
                "The prospect is being processed by a background task; cancel the task or wait for it to finish.",
                details={"status": prospect.status.value},
            )
        await self.session.execute(sql_delete(AuditEvent).where(AuditEvent.prospect_id == prospect_id))
        await self.session.delete(prospect)
        await self.session.commit()
        log.info("prospect.deleted", extra={"prospect_id": prospect_id, "actor": actor})

    async def messages(self, prospect_id: int) -> list[OutreachMessage]:
        """Every message version for a prospect, newest first."""
        await self.get(prospect_id)
        stmt = (
            select(OutreachMessage)
            .where(OutreachMessage.prospect_id == prospect_id)
            .order_by(OutreachMessage.version.desc(), OutreachMessage.id.desc())
        )
        return list((await self.session.scalars(stmt)).all())

    async def counts_by_status(self) -> dict[str, int]:
        rows = (await self.session.execute(select(Prospect.status, func.count()).group_by(Prospect.status))).all()
        counts = {s.value: 0 for s in ProspectStatus}
        counts.update({status.value: int(n) for status, n in rows})
        return counts

    async def import_csv(self, text: str, *, actor: str = "api") -> ImportReport:
        """Create prospects from CSV text. Each row is validated and de-duplicated on its own."""
        rows: list[ImportRowResult] = []
        for parsed in parse_import_csv(text):
            if parsed.payload is None:
                rows.append(ImportRowResult(row=parsed.line, status="invalid", name=parsed.name, errors=parsed.errors))
                continue
            try:
                prospect = await self.create(parsed.payload, actor=actor)
            except DuplicateProspectError as exc:
                rows.append(
                    ImportRowResult(
                        row=parsed.line,
                        status="duplicate",
                        name=parsed.payload.name,
                        prospect_id=exc.details.get("existing_id"),
                        errors=[exc.message],
                    )
                )
                continue
            rows.append(ImportRowResult(row=parsed.line, status="created", name=prospect.name, prospect_id=prospect.id))
        report = ImportReport(
            created=sum(r.status == "created" for r in rows),
            duplicates=sum(r.status == "duplicate" for r in rows),
            invalid=sum(r.status == "invalid" for r in rows),
            rows=rows,
        )
        log.info(
            "prospects.imported",
            extra={"created": report.created, "duplicates": report.duplicates, "invalid": report.invalid},
        )
        return report

    async def latest_message(self, prospect_id: int) -> OutreachMessage | None:
        stmt = (
            select(OutreachMessage)
            .where(OutreachMessage.prospect_id == prospect_id)
            .order_by(OutreachMessage.id.desc())
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def events(self, prospect_id: int) -> list[AuditEvent]:
        await self.get(prospect_id)
        stmt = select(AuditEvent).where(AuditEvent.prospect_id == prospect_id).order_by(AuditEvent.id)
        return list((await self.session.scalars(stmt)).all())

    async def detail(self, prospect: Prospect) -> ProspectDetail:
        latest = await self.latest_message(prospect.id)
        base = ProspectRead.model_validate(prospect).model_dump()
        return ProspectDetail(
            **base,
            latest_message=MessageRead.model_validate(latest) if latest else None,
            allowed_next_statuses=allowed_next(prospect.status),
        )
