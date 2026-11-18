"""Client for any OpenAI-compatible ``/chat/completions`` endpoint (OpenAI, Azure-compatible
gateways, vLLM, Ollama's OpenAI API, LM Studio...).

Error handling:
* network errors, timeouts, HTTP 429 and 5xx -> ``LLMUnavailableError`` (retryable)
* HTTP 401/403 -> ``LLMAuthError`` (not retryable: fix the key)
* other 4xx or a malformed body -> ``LLMError`` (not retryable)
"""

from __future__ import annotations

from typing import Any

import httpx

from app.agents.llm import ChatMessages, LLMResponse
from app.core.errors import LLMAuthError, LLMError, LLMUnavailableError
from app.core.logging import get_logger
from app.core.rate_limit import TokenBucket

log = get_logger(__name__)


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
        requests_per_minute: float = 30.0,
        json_mode: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = model
        self.model = model
        self.json_mode = json_mode
        self._limiter = TokenBucket(rate=requests_per_minute / 60.0, capacity=max(1.0, min(5.0, requests_per_minute)))
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def complete(
        self, messages: ChatMessages, *, temperature: float = 0.4, json_mode: bool = True
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode and self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        await self._limiter.acquire()
        try:
            resp = await self._http.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError("The LLM request timed out.") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(
                "Could not reach the LLM endpoint.", details={"error": type(exc).__name__}
            ) from exc

        if resp.status_code in (401, 403):
            raise LLMAuthError("The LLM provider rejected the API key.", details={"status": resp.status_code})
        if resp.status_code == 429 or resp.status_code >= 500:
            raise LLMUnavailableError(
                "The LLM provider is rate limiting or unavailable.",
                details={"status": resp.status_code, "retry_after": resp.headers.get("retry-after")},
            )
        if resp.status_code >= 400:
            raise LLMError(
                "The LLM provider rejected the request.", details={"status": resp.status_code, "body": resp.text[:300]}
            )
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("The LLM response did not have the expected shape.") from exc
        usage = data.get("usage") or None
        log.info(
            "llm.completed", extra={"model": data.get("model", self.model), "tokens": (usage or {}).get("total_tokens")}
        )
        return LLMResponse(
            content=content,
            model=data.get("model", self.model),
            usage={k: v for k, v in (usage or {}).items() if isinstance(v, int)} or None,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
