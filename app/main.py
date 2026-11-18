"""FastAPI application factory.

Run with ``uvicorn app.main:app`` (configuration from the environment / ``.env``). The operator
console is served at ``/`` and the interactive API docs at ``/docs``.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.deps import require_api_key
from app.api.routes import health_router, prospects_router, stats_router, tasks_router
from app.config import Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, request_id_var
from app.services.container import Container

log = get_logger(__name__)

UI_DIR = Path(__file__).parent / "ui"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

DESCRIPTION = """
AI-assisted outreach workflow with **browser automation** (Playwright) and **mandatory human approval**.

Workflow: import -> validate -> research (browser) -> generate message (LLM) -> **human approval** ->
execute (browser) -> results saved. Slow steps run as background tasks; poll `GET /tasks/{id}`.

When `API_KEY` is configured, every endpoint except `/health` requires the `X-API-Key` header
(use the **Authorize** button). The operator console is at [`/`](/).
"""


def create_app(settings: Settings | None = None, *, container_factory=None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        factory = container_factory or Container.create
        app.state.container = await factory(settings)
        try:
            yield
        finally:
            await app.state.container.shutdown()

    app = FastAPI(title="OutreachPilot API", version=__version__, description=DESCRIPTION, lifespan=lifespan)

    protected = [Depends(require_api_key)]
    app.include_router(health_router)
    app.include_router(stats_router, dependencies=protected)
    app.include_router(prospects_router, dependencies=protected)
    app.include_router(tasks_router, dependencies=protected)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "X-API-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path == "/" or request.url.path.startswith("/ui"):
            response.headers.setdefault("Content-Security-Policy", _UI_CSP)
        if request.url.path != "/health":
            log.info(
                "http.request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": elapsed_ms,
                    "request_id": request_id,
                },
            )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("request.failed", extra={"path": request.url.path, "code": exc.code})
        return JSONResponse(status_code=exc.status_code, content={"error": exc.to_dict()})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_error",
                    "message": "The request is invalid.",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("request.unhandled_error", extra={"path": request.url.path})
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "An unexpected error occurred.", "details": {}}},
        )

    @app.get("/", include_in_schema=False)
    async def console() -> RedirectResponse:
        return RedirectResponse("/ui/", status_code=307)

    app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
    return app


app = create_app()
