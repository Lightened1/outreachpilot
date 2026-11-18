"""REST endpoints. Handlers stay thin: validation is done by the schemas, behaviour by services."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, Query, Response, status

from app import __version__
from app.api.deps import get_container, get_prospect_service
from app.core.enums import ProspectStatus
from app.schemas import (
    ApprovalRequest,
    AuditEventRead,
    ErrorResponse,
    GenerateMessageRequest,
    HealthResponse,
    ImportReport,
    ImportRequest,
    MessageRead,
    ProspectCreate,
    ProspectDetail,
    ProspectList,
    ProspectRead,
    ProspectUpdate,
    StatsResponse,
    TaskRead,
)
from app.services.container import Container
from app.services.prospects import ProspectService, ProspectSort

ERRORS = {code: {"model": ErrorResponse} for code in (404, 409, 422)}

health_router = APIRouter(tags=["health"])
prospects_router = APIRouter(prefix="/prospects", tags=["prospects"], responses=ERRORS)
tasks_router = APIRouter(prefix="/tasks", tags=["tasks"], responses=ERRORS)
stats_router = APIRouter(tags=["health"])


@health_router.get("/health", response_model=HealthResponse, summary="Service health")
async def health(container: Container = Depends(get_container)) -> HealthResponse:
    db_ok = await container.db.ping()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        app=container.settings.app_name,
        version=__version__,
        database="ok" if db_ok else "unavailable",
        config=container.settings.public_summary(),
        tasks=container.tasks.stats(),
        time=datetime.now(UTC),
    )


@stats_router.get("/stats", response_model=StatsResponse, summary="Prospect counts by status and task load")
async def stats(
    container: Container = Depends(get_container), service: ProspectService = Depends(get_prospect_service)
) -> StatsResponse:
    by_status = await service.counts_by_status()
    return StatsResponse(total=sum(by_status.values()), by_status=by_status, tasks=container.tasks.stats())


# ---- prospects ------------------------------------------------------------------------------------
@prospects_router.post(
    "",
    response_model=ProspectDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Import a prospect (validated, de-duplicated)",
)
async def create_prospect(payload: ProspectCreate, service: ProspectService = Depends(get_prospect_service)):
    prospect = await service.create(payload)
    return await service.detail(prospect)


@prospects_router.post(
    "/import",
    response_model=ImportReport,
    summary="Import prospects from CSV (per-row report)",
    description="Rows are validated and de-duplicated one by one; invalid rows and duplicates are reported "
    "and skipped, valid rows are created. Up to 1000 rows per request.",
)
async def import_prospects(payload: ImportRequest, service: ProspectService = Depends(get_prospect_service)):
    return await service.import_csv(payload.csv)


@prospects_router.get("", response_model=ProspectList, summary="List prospects")
async def list_prospects(
    status_filter: list[ProspectStatus] | None = Query(
        None, alias="status", description="Repeat to match several statuses, e.g. ?status=failed&status=blocked"
    ),
    q: str | None = Query(None, max_length=100, description="Search name, company or job title"),
    sort: ProspectSort = Query("id", description="id, -id (newest first) or -updated (recently changed first)"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    service: ProspectService = Depends(get_prospect_service),
) -> ProspectList:
    items, total = await service.list(statuses=status_filter, q=q, sort=sort, limit=limit, offset=offset)
    return ProspectList(items=[ProspectRead.model_validate(p) for p in items], total=total, limit=limit, offset=offset)


@prospects_router.get("/{prospect_id}", response_model=ProspectDetail, summary="Get a prospect")
async def get_prospect(prospect_id: int, service: ProspectService = Depends(get_prospect_service)):
    return await service.detail(await service.get(prospect_id))


@prospects_router.patch("/{prospect_id}", response_model=ProspectDetail, summary="Update prospect fields")
async def update_prospect(
    prospect_id: int, payload: ProspectUpdate, service: ProspectService = Depends(get_prospect_service)
):
    return await service.detail(await service.update(prospect_id, payload))


@prospects_router.delete(
    "/{prospect_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a prospect, its messages and audit trail",
)
async def delete_prospect(prospect_id: int, service: ProspectService = Depends(get_prospect_service)) -> Response:
    await service.delete(prospect_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@prospects_router.get(
    "/{prospect_id}/messages", response_model=list[MessageRead], summary="All message versions, newest first"
)
async def prospect_messages(prospect_id: int, service: ProspectService = Depends(get_prospect_service)):
    return [MessageRead.model_validate(m) for m in await service.messages(prospect_id)]


@prospects_router.get("/{prospect_id}/events", response_model=list[AuditEventRead], summary="Audit trail")
async def prospect_events(prospect_id: int, service: ProspectService = Depends(get_prospect_service)):
    return await service.events(prospect_id)


@prospects_router.get("/{prospect_id}/tasks", response_model=list[TaskRead], summary="Tasks for a prospect")
async def prospect_tasks(
    prospect_id: int,
    service: ProspectService = Depends(get_prospect_service),
    container: Container = Depends(get_container),
):
    await service.get(prospect_id)
    return [TaskRead.from_record(r) for r in await container.tasks.list(prospect_id=prospect_id)]


@prospects_router.post(
    "/{prospect_id}/research",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Research the prospect's profile in the browser (background task)",
)
async def research(prospect_id: int, container: Container = Depends(get_container)):
    return TaskRead.from_record(await container.workflow.start_research(prospect_id))


@prospects_router.post(
    "/{prospect_id}/generate-message",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate a personalised message for approval (background task)",
)
async def generate_message(
    prospect_id: int, payload: GenerateMessageRequest | None = Body(None), container: Container = Depends(get_container)
):
    return TaskRead.from_record(await container.workflow.start_generation(prospect_id, payload))


@prospects_router.post(
    "/{prospect_id}/approve",
    response_model=ProspectDetail,
    summary="Approve or reject the pending message (human decision)",
)
async def approve(
    prospect_id: int,
    payload: ApprovalRequest,
    container: Container = Depends(get_container),
    service: ProspectService = Depends(get_prospect_service),
):
    await container.workflow.decide(prospect_id, payload)
    return await service.detail(await service.get(prospect_id))


@prospects_router.post(
    "/{prospect_id}/execute",
    response_model=TaskRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send the approved message through the demo portal (background task)",
)
async def execute(prospect_id: int, container: Container = Depends(get_container)):
    return TaskRead.from_record(await container.workflow.start_execution(prospect_id))


# ---- tasks ------------------------------------------------------------------------------------------
@tasks_router.get("/{task_id}", response_model=TaskRead, summary="Task status, attempts and history")
async def get_task(task_id: str, container: Container = Depends(get_container)):
    return TaskRead.from_record(await container.tasks.get(task_id))


@tasks_router.get("", response_model=list[TaskRead], summary="Recent tasks")
async def list_tasks(
    prospect_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    container: Container = Depends(get_container),
):
    return [TaskRead.from_record(r) for r in await container.tasks.list(prospect_id=prospect_id, limit=limit)]


@tasks_router.post("/{task_id}/cancel", response_model=TaskRead, summary="Cancel a queued or running task")
async def cancel_task(task_id: str, container: Container = Depends(get_container)):
    await container.tasks.get(task_id)
    return TaskRead.from_record(await container.workflow.cancel(task_id))
