"""LLM client interface and factory.

Any object with an async ``complete(messages, temperature=..., json_mode=...)`` method can be
used, which keeps the agent independent of a vendor SDK. Two implementations ship:
``MockLLMClient`` (offline, deterministic) and ``OpenAICompatibleClient`` (HTTP).
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from app.config import Settings
from app.core.logging import get_logger

log = get_logger(__name__)

ChatMessages = list[dict[str, str]]


class LLMResponse(BaseModel):
    content: str
    model: str
    usage: dict[str, int] | None = None


class LLMClient(Protocol):
    name: str

    async def complete(
        self, messages: ChatMessages, *, temperature: float = 0.4, json_mode: bool = True
    ) -> LLMResponse: ...

    async def aclose(self) -> None: ...


def build_llm_client(settings: Settings) -> LLMClient:
    """Pick the client from configuration; a missing API key means mock mode, not a crash."""
    if settings.effective_llm_mode == "openai":
        from app.agents.openai_client import OpenAICompatibleClient

        assert settings.openai_api_key is not None
        log.info(
            "llm.client", extra={"mode": "openai", "model": settings.openai_model, "base_url": settings.openai_base_url}
        )
        return OpenAICompatibleClient(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
            timeout_s=settings.llm_timeout_s,
            requests_per_minute=settings.llm_requests_per_minute,
            json_mode=settings.openai_json_mode,
        )
    from app.agents.mock_llm import MockLLMClient

    if settings.llm_mode == "auto":
        log.warning("llm.mock_mode", extra={"reason": "OPENAI_API_KEY not set; using the offline mock generator"})
    return MockLLMClient()
