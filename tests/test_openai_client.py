"""OpenAI-compatible HTTP client against a mocked transport (no network, no key)."""

from __future__ import annotations

import json

import pytest

httpx = pytest.importorskip("httpx")

from app.agents.openai_client import OpenAICompatibleClient  # noqa: E402
from app.core.errors import LLMAuthError, LLMError, LLMUnavailableError  # noqa: E402

pytestmark = pytest.mark.anyio
MESSAGES = [{"role": "user", "content": "hi"}]


def client(handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="https://llm.example/v1",
        api_key="sk-test-secret",
        model="test-model",
        requests_per_minute=6000,
        transport=httpx.MockTransport(handler),
    )


async def test_success_parses_content_and_sends_expected_request() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [{"message": {"content": '{"a": 1}'}}],
                "usage": {"total_tokens": 42},
            },
        )

    resp = await client(handler).complete(MESSAGES)
    assert resp.content == '{"a": 1}' and resp.usage == {"total_tokens": 42}
    assert seen["auth"] == "Bearer sk-test-secret" and seen["path"] == "/v1/chat/completions"
    assert seen["body"]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "status, error, retryable",
    [
        (429, LLMUnavailableError, True),
        (500, LLMUnavailableError, True),
        (503, LLMUnavailableError, True),
        (401, LLMAuthError, False),
        (403, LLMAuthError, False),
        (400, LLMError, False),
    ],
)
async def test_http_errors_are_classified(status: int, error: type, retryable: bool) -> None:
    with pytest.raises(error) as exc:
        await client(lambda r: httpx.Response(status, json={"error": "x"})).complete(MESSAGES)
    assert exc.value.retryable is retryable


async def test_timeouts_and_network_errors_are_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    for handler in (timeout, refused):
        with pytest.raises(LLMUnavailableError):
            await client(handler).complete(MESSAGES)


async def test_malformed_body_is_an_error() -> None:
    with pytest.raises(LLMError):
        await client(lambda r: httpx.Response(200, json={"choices": []})).complete(MESSAGES)
