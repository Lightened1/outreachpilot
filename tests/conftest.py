"""Shared fixtures.

* async tests use the ``anyio`` pytest plugin (``@pytest.mark.anyio``), so no extra plugin is needed;
* ``demo_server`` runs the demo portal with uvicorn in a background thread on a free port;
* ``settings`` points the app at that server and at temporary directories.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn

from app.config import Settings
from demo.app import DemoConfig, create_demo_app

HAS_API_STACK = all(importlib.util.find_spec(m) for m in ("fastapi", "sqlalchemy", "aiosqlite", "httpx"))
if os.getenv("REQUIRE_FULL_STACK") and not HAS_API_STACK:  # CI must run every test, never skip silently
    raise RuntimeError("REQUIRE_FULL_STACK is set but fastapi/sqlalchemy/aiosqlite/httpx are not installed")
needs_api_stack = pytest.mark.skipif(
    not HAS_API_STACK, reason="needs fastapi, sqlalchemy, aiosqlite and httpx (pip install -r requirements-dev.txt)"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DemoServer:
    def __init__(self, config: DemoConfig) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(
            uvicorn.Config(create_demo_app(config), host="127.0.0.1", port=self.port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("demo server did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    def reset(self) -> None:
        urllib.request.urlopen(urllib.request.Request(self.url + "/api/reset", method="POST"), timeout=5).read()

    def outbox(self) -> list[dict]:
        return json.loads(urllib.request.urlopen(self.url + "/api/outbox", timeout=5).read())


@pytest.fixture(scope="session")
def demo_server() -> Iterator[DemoServer]:
    server = DemoServer(DemoConfig(slow_delay_s=4.0))
    server.start()
    yield server
    server.stop()


@pytest.fixture
def demo(demo_server: DemoServer) -> DemoServer:
    demo_server.reset()
    return demo_server


@pytest.fixture
def settings(tmp_path: Path, demo: DemoServer) -> Settings:
    return Settings.from_env(
        {},
        env_file=None,
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        artifacts_dir=tmp_path / "artifacts",
        browser_session_dir=tmp_path / "sessions",
        demo_portal_url=demo.url,
        llm_mode="mock",
        browser_timeout_ms=2_500,
        navigation_timeout_ms=2_500,
        task_max_attempts=3,
        retry_base_delay_s=0.05,
        retry_max_delay_s=0.2,
        task_timeout_s=30,
        navigations_per_minute=600,
        navigation_burst=20,
    )
