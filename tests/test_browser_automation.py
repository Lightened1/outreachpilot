"""Real Playwright automation against the local demo portal (Chromium, headless)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.automation.browser import BrowserEngine
from app.automation.portal import DemoPortalClient, names_match
from app.config import Settings
from app.core.errors import (
    AccessBlockedError,
    AutomationTimeoutError,
    DataMismatchError,
    HostNotAllowedError,
    LoginFailedError,
    NavigationError,
    PageNotFoundError,
    RobotsDisallowedError,
    is_retryable,
)
from app.core.retry import RetryPolicy, retry_async

pytestmark = [pytest.mark.anyio, pytest.mark.browser]


@pytest.fixture
async def portal(settings: Settings):
    async with BrowserEngine(settings) as engine:
        yield DemoPortalClient(engine, settings)


async def test_research_extracts_structured_profile(portal: DemoPortalClient, settings: Settings) -> None:
    result = await portal.research(f"{settings.demo_portal_url}/people/1", expected_name="Priya Raman")
    assert result.name == "Priya Raman"
    assert result.headline == "Head of Operations" and result.company == "Bluefin Logistics"
    assert result.location == "Rotterdam, Netherlands"
    assert result.recent_activity[0].startswith("Wrote about moving weekly warehouse reports")
    assert result.skills == ["Operations management", "Lean", "Warehouse planning"]
    assert result.screenshot and Path(result.screenshot).is_file()
    assert set(result.facts()) >= {"name", "headline", "company", "about", "recent_activity", "skills"}


async def test_login_happens_once_and_session_is_persisted(portal: DemoPortalClient, settings: Settings) -> None:
    await portal.research(f"{settings.demo_portal_url}/people/2", expected_name="Marcus Oyelaran")
    assert settings.session_state_file.is_file()
    # A brand-new browser process reuses the saved session: no login form this time.
    async with BrowserEngine(settings) as engine:
        async with engine.new_page() as page:
            await engine.goto(page, f"{settings.demo_portal_url}/people/3")
            assert "/login" not in page.url
            assert await page.get_by_test_id("person-name").inner_text() == "Elena Varga"


async def test_search_form_interaction(portal: DemoPortalClient, settings: Settings) -> None:
    engine = portal.engine
    async with engine.new_page() as page:
        await portal._open(page, f"{settings.demo_portal_url}/people")
        await page.get_by_test_id("search-input").fill("kestrel")
        async with page.expect_navigation():
            await page.get_by_test_id("search-submit").click()
        rows = await page.get_by_test_id("person-row").all_inner_texts()
        assert len(rows) == 1 and "Elena Varga" in rows[0]


async def test_slow_page_times_out_as_retryable(portal: DemoPortalClient, settings: Settings) -> None:
    start = time.monotonic()
    with pytest.raises(AutomationTimeoutError) as exc:
        await portal.research(f"{settings.demo_portal_url}/people/4", expected_name="Tomás Ferreira")
    assert is_retryable(exc.value)
    assert time.monotonic() - start < 4  # navigation timeout is 2.5 s in the test settings


async def test_blocked_page_stops_without_retry(portal: DemoPortalClient, settings: Settings) -> None:
    attempts = []

    async def run() -> None:
        attempts.append(1)
        await portal.research(f"{settings.demo_portal_url}/people/5", expected_name="Hana Sato")

    with pytest.raises(AccessBlockedError) as exc:
        await retry_async(run, RetryPolicy(max_attempts=3, base_delay=0.01))
    assert exc.value.details["status"] == 429
    assert attempts == [1]  # never retried: a challenge page must be handled by a human


async def test_transient_503_is_retried_until_success(portal: DemoPortalClient, settings: Settings) -> None:
    errors: list[str] = []
    result = await retry_async(
        lambda: portal.research(f"{settings.demo_portal_url}/people/6", expected_name="Daniel Kim"),
        RetryPolicy(max_attempts=4, base_delay=0.01, jitter=0.0),
        on_retry=lambda attempt, exc, delay: errors.append(type(exc).__name__),
    )
    assert result.name == "Daniel Kim"
    assert errors == ["NavigationError", "NavigationError"]


async def test_robots_txt_is_respected(portal: DemoPortalClient, settings: Settings) -> None:
    with pytest.raises(RobotsDisallowedError):
        await portal.research(f"{settings.demo_portal_url}/private/people/7", expected_name="Sofia Lindqvist")


async def test_hosts_outside_allow_list_are_refused(portal: DemoPortalClient) -> None:
    with pytest.raises(HostNotAllowedError):
        await portal.research("https://www.linkedin.com/in/someone", expected_name="Some One")


async def test_wrong_person_is_detected(portal: DemoPortalClient, settings: Settings) -> None:
    with pytest.raises(DataMismatchError):
        await portal.research(f"{settings.demo_portal_url}/people/3", expected_name="Priya Raman")


async def test_missing_page_is_reported_with_a_trace(portal: DemoPortalClient, settings: Settings) -> None:
    with pytest.raises(PageNotFoundError) as info:
        await portal.research(f"{settings.demo_portal_url}/people/999", expected_name="No One")
    trace = Path(info.value.details["trace"])  # failed jobs keep a Playwright trace for debugging
    assert trace.is_file() and trace.suffix == ".zip" and "research-no-one" in trace.name


async def test_successful_jobs_do_not_keep_traces(portal: DemoPortalClient, settings: Settings) -> None:
    await portal.research(f"{settings.demo_portal_url}/people/2", expected_name="Marcus Oyelaran")
    traces = Path(settings.artifacts_dir) / "traces"
    assert not any(traces.glob("*marcus*")) if traces.exists() else True


async def test_unreachable_site_is_retryable(settings: Settings) -> None:
    dead = settings.model_copy(update={"demo_portal_url": "http://127.0.0.1:9"})
    async with BrowserEngine(dead) as engine:
        with pytest.raises(NavigationError):
            await DemoPortalClient(engine, dead).research("http://127.0.0.1:9/people/1")


async def test_bad_credentials_fail_cleanly(settings: Settings) -> None:
    from pydantic import SecretStr

    bad = settings.model_copy(update={"demo_password": SecretStr("wrong"), "persist_browser_session": False})
    async with BrowserEngine(bad) as engine:
        with pytest.raises(LoginFailedError):
            await DemoPortalClient(engine, bad).research(f"{settings.demo_portal_url}/people/1")


async def test_send_message_is_idempotent(portal: DemoPortalClient, settings: Settings, demo) -> None:
    url = f"{settings.demo_portal_url}/people/1"
    first = await portal.send_message(
        url,
        subject="Reporting automation",
        body="Hi Priya, a short note.",
        idempotency_key="msg-1",
        expected_name="Priya Raman",
    )
    assert first.confirmation_id.startswith("MSG-") and not first.duplicate
    assert Path(first.screenshot).is_file()
    again = await portal.send_message(
        url,
        subject="Reporting automation",
        body="Hi Priya, a short note.",
        idempotency_key="msg-1",
        expected_name="Priya Raman",
    )
    assert again.duplicate and again.confirmation_id == first.confirmation_id
    outbox = demo.outbox()
    assert len(outbox) == 1 and outbox[0]["recipient"] == "Priya Raman"


async def test_send_refuses_wrong_recipient(portal: DemoPortalClient, settings: Settings, demo) -> None:
    with pytest.raises(DataMismatchError):
        await portal.send_message(
            f"{settings.demo_portal_url}/people/2",
            subject="Hi",
            body="Hello there friend.",
            idempotency_key="k",
            expected_name="Priya Raman",
        )
    assert demo.outbox() == []


async def test_page_pool_is_bounded(settings: Settings) -> None:
    import asyncio

    limited = settings.model_copy(update={"browser_max_pages": 1})
    async with BrowserEngine(limited) as engine:
        portal = DemoPortalClient(engine, limited)
        open_pages, peak = 0, 0
        original = engine.new_page

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def counting(label: str = "job"):
            nonlocal open_pages, peak
            async with original(label) as page:
                open_pages += 1
                peak = max(peak, open_pages)
                try:
                    yield page
                finally:
                    open_pages -= 1

        engine.new_page = counting  # type: ignore[method-assign]
        await asyncio.gather(*(portal.research(f"{limited.demo_portal_url}/people/{i}") for i in (1, 2, 3)))
        assert peak == 1


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("Tomás Ferreira", "Tomas Ferreira", True),
        ("priya raman", "Priya  Raman", True),
        ("Priya Raman", "Priya R. Raman", True),
        ("Priya Raman", "Elena Varga", False),
    ],
)
def test_name_matching(a: str, b: str, same: bool) -> None:
    assert names_match(a, b) is same
