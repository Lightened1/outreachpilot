"""Browser automation showcase (no database or API needed).

Starts the demo portal in-process (unless --portal-url is given) and drives it with Playwright:
login, search, profile extraction, failure handling (timeout, transient 503 with retries, blocked
page, robots.txt, host allow-list) and an idempotent form submission. Prints structured JSON
results and saves screenshots under ``artifacts/screenshots``.

    python -m demo.browser_demo                       # headless
    python -m demo.browser_demo --headed --slow-mo 400   # watch it work (good for screen recordings)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time
from typing import Any

import uvicorn

from app.automation import actions
from app.automation.browser import BrowserEngine
from app.automation.portal import DemoPortalClient
from app.config import Settings
from app.core.errors import AppError
from app.core.logging import configure_logging
from app.core.retry import RetryPolicy, retry_async
from demo.app import DemoConfig, create_demo_app


def start_portal() -> tuple[str, uvicorn.Server]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_demo_app(DemoConfig(slow_delay_s=8)), host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", server


def show(title: str, payload: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, default=str) if not isinstance(payload, str) else payload)


async def run(settings: Settings) -> None:
    base = settings.demo_portal_url
    async with BrowserEngine(settings) as engine:
        portal = DemoPortalClient(engine, settings)

        # 1. Navigate, log in, search and read a results table.
        async with engine.new_page() as page:
            await portal._open(page, f"{base}/people")
            await actions.fill(page, "search-input", "logistics")
            await actions.submit_and_wait(page, "search-submit")
            rows = await actions.texts_of(page, "person-row")
            show("Search results for 'logistics'", rows)
            shot = await engine.screenshot(page, "search-results")
            show("Screenshot", shot)

        # 2. Extract a profile as structured data.
        result = await portal.research(f"{base}/people/1", expected_name="Priya Raman")
        show("Structured research result", result.model_dump())

        # 3. Failure handling.
        async def attempt(label: str, coro_factory, policy: RetryPolicy | None = None) -> None:
            retries: list[str] = []
            try:
                if policy:
                    value = await retry_async(
                        coro_factory,
                        policy,
                        on_retry=lambda n, exc, d: retries.append(f"{type(exc).__name__} -> retry in {d:.2f}s"),
                    )
                else:
                    value = await coro_factory()
                show(label, {"outcome": "success", "retries": retries, "name": getattr(value, "name", None)})
            except AppError as exc:
                show(
                    label,
                    {
                        "outcome": "stopped",
                        "retries": retries,
                        "error": exc.code,
                        "retryable": exc.retryable,
                        "message": exc.message,
                    },
                )

        policy = RetryPolicy(max_attempts=3, base_delay=0.5, jitter=0.0)
        await attempt(
            "Transient 503s are retried with exponential backoff",
            lambda: portal.research(f"{base}/people/6", expected_name="Daniel Kim"),
            policy,
        )
        await attempt(
            "A slow page hits the timeout (retryable)",
            lambda: portal.research(f"{base}/people/4", expected_name="Tomás Ferreira"),
        )
        await attempt(
            "An access challenge stops the run and is never retried",
            lambda: portal.research(f"{base}/people/5", expected_name="Hana Sato"),
            policy,
        )
        await attempt(
            "robots.txt is respected",
            lambda: portal.research(f"{base}/private/people/7", expected_name="Sofia Lindqvist"),
        )
        await attempt(
            "Hosts outside the allow-list are refused",
            lambda: portal.research("https://www.linkedin.com/in/example", expected_name="Example User"),
        )

        # 4. Form interaction: send a message (idempotent).
        sent = await portal.send_message(
            f"{base}/people/2",
            subject="Hello from the demo",
            body="Hi Marcus, this is a demo message sent by OutreachPilot's browser "
            "automation to the local test portal.",
            idempotency_key="browser-demo-1",
            expected_name="Marcus Oyelaran",
        )
        again = await portal.send_message(
            f"{base}/people/2",
            subject="Hello from the demo",
            body="Hi Marcus, this is a demo message sent by OutreachPilot's browser "
            "automation to the local test portal.",
            idempotency_key="browser-demo-1",
            expected_name="Marcus Oyelaran",
        )
        show(
            "Form submission (first send, then a retry with the same idempotency key)",
            {"first": sent.model_dump(), "retry_was_duplicate": again.duplicate},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--portal-url", help="use an already running demo portal instead of starting one")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    parser.add_argument("--slow-mo", type=int, default=0, help="delay between browser actions in ms")
    args = parser.parse_args()

    configure_logging("WARNING")
    server = None
    url = args.portal_url
    if not url:
        url, server = start_portal()
        print(f"Demo portal running at {url}")
    settings = Settings.from_env(
        demo_portal_url=url,
        browser_headless=not args.headed,
        browser_slow_mo_ms=args.slow_mo,
        navigation_timeout_ms=4000,
        browser_timeout_ms=4000,
    )
    try:
        asyncio.run(run(settings))
    finally:
        if server is not None:
            server.should_exit = True


if __name__ == "__main__":
    main()
