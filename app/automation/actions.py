"""Reusable page operations, all addressed by ``data-testid`` and all with explicit timeouts.

Each helper converts Playwright failures to application errors, so callers deal with one error
hierarchy only.
"""

from __future__ import annotations

from playwright.async_api import Page, Response

from app.automation.errors import translate_errors
from app.core.errors import AutomationTimeoutError, ElementNotFoundError


async def wait_visible(page: Page, test_id: str, *, timeout_ms: float | None = None) -> None:
    """Wait for an element; a missing element means the page is not what we expected."""
    try:
        with translate_errors(f"find '{test_id}'", test_id=test_id):
            await page.get_by_test_id(test_id).first.wait_for(state="visible", timeout=timeout_ms)
    except AutomationTimeoutError as exc:
        raise ElementNotFoundError(
            f"Expected element '{test_id}' did not appear on {page.url}.", details={"test_id": test_id, "url": page.url}
        ) from exc


async def text_of(page: Page, test_id: str, *, required: bool = True) -> str | None:
    locator = page.get_by_test_id(test_id)
    with translate_errors(f"read '{test_id}'", test_id=test_id):
        if await locator.count() == 0:
            if required:
                raise ElementNotFoundError(
                    f"Element '{test_id}' is missing on {page.url}.", details={"test_id": test_id, "url": page.url}
                )
            return None
        text = (await locator.first.inner_text()).strip()
    return text or None


async def texts_of(page: Page, test_id: str) -> list[str]:
    with translate_errors(f"read all '{test_id}'", test_id=test_id):
        values = await page.get_by_test_id(test_id).all_inner_texts()
    return [v.strip() for v in values if v.strip()]


async def fill(page: Page, test_id: str, value: str) -> None:
    with translate_errors(f"fill '{test_id}'", test_id=test_id):
        await page.get_by_test_id(test_id).fill(value)


async def set_hidden_value(page: Page, test_id: str, value: str) -> None:
    with translate_errors(f"set hidden field '{test_id}'", test_id=test_id):
        await page.get_by_test_id(test_id).evaluate("(el, v) => { el.value = v; }", value)


async def submit_and_wait(page: Page, button_test_id: str) -> Response | None:
    """Click a submit button, wait for the resulting navigation and return its response."""
    with translate_errors(f"submit via '{button_test_id}'", test_id=button_test_id):
        async with page.expect_navigation(wait_until="domcontentloaded") as nav:
            await page.get_by_test_id(button_test_id).click()
        return await nav.value
