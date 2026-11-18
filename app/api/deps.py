from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationError
from app.services.container import Container
from app.services.prospects import ProspectService


def get_container(request: Request) -> Container:
    return request.app.state.container


async def get_session(container: Container = Depends(get_container)) -> AsyncIterator[AsyncSession]:
    async with container.db.session_factory() as session:
        yield session


def get_prospect_service(session: AsyncSession = Depends(get_session)) -> ProspectService:
    return ProspectService(session)


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key", description="Required when API_KEY is configured"),
) -> None:
    """Reject the request unless it carries the configured API key (no-op when API_KEY is unset)."""
    expected = request.app.state.container.settings.api_key
    if expected is None:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.get_secret_value().encode()):
        raise AuthenticationError("A valid X-API-Key header is required.")
