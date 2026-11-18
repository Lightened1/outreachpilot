"""High-level automation of the demo prospect portal.

``research`` opens a profile and extracts structured facts; ``send_message`` fills and submits
the compose form for an *approved* message. Both log in when the portal redirects to its login
page, verify they are on the right person's page before doing anything, and capture a
screenshot as evidence.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from playwright.async_api import Page

from app.automation import actions
from app.automation.browser import BrowserEngine
from app.config import Settings
from app.core.errors import DataMismatchError, LoginFailedError, ValidationFailedError
from app.core.logging import get_logger
from app.schemas.workflow import ExecutionResult, ResearchResult

log = get_logger(__name__)


def _norm_name(name: str) -> list[str]:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch)).lower()
    return [t for t in re.split(r"[^a-z]+", name) if t]


def names_match(expected: str, actual: str) -> bool:
    """True if the first and last name tokens agree (accent- and case-insensitive)."""
    a, b = _norm_name(expected), _norm_name(actual)
    if not a or not b:
        return False
    return a[0] == b[0] and a[-1] == b[-1]


class DemoPortalClient:
    def __init__(self, engine: BrowserEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def _open(self, page: Page, url: str) -> None:
        """Navigate to ``url``, logging in first if the portal asks for it."""
        await self.engine.goto(page, url)
        if urlsplit(page.url).path.rstrip("/") == "/login":
            await self._login(page, url)

    async def _login(self, page: Page, target_url: str) -> None:
        log.info("portal.login", extra={"target": target_url})
        await actions.wait_visible(page, "login-form")
        await actions.fill(page, "login-username", self.settings.demo_username)
        await actions.fill(page, "login-password", self.settings.demo_password.get_secret_value())
        response = await actions.submit_and_wait(page, "login-submit")
        if urlsplit(page.url).path.rstrip("/") == "/login":
            raise LoginFailedError(
                "The demo portal rejected the configured credentials.",
                details={"status": response.status if response else None},
            )
        await self.engine.raise_for_page(page, response, page.url)

    def _check_person(self, expected_name: str | None, actual: str, url: str) -> None:
        if expected_name and not names_match(expected_name, actual):
            raise DataMismatchError(
                f"The page at {url} belongs to '{actual}', not '{expected_name}'. Nothing was saved.",
                details={"expected": expected_name, "found": actual, "url": url},
            )

    async def research(self, profile_url: str, *, expected_name: str | None = None) -> ResearchResult:
        async with self.engine.new_page(f"research-{expected_name or 'profile'}") as page:
            await self._open(page, profile_url)
            await actions.wait_visible(page, "person-name")
            name = await actions.text_of(page, "person-name") or ""
            self._check_person(expected_name, name, profile_url)
            result = ResearchResult(
                name=name,
                headline=await actions.text_of(page, "person-headline", required=False),
                company=await actions.text_of(page, "person-company", required=False),
                location=await actions.text_of(page, "person-location", required=False),
                about=await actions.text_of(page, "person-about", required=False),
                recent_activity=await actions.texts_of(page, "activity-item"),
                skills=await actions.texts_of(page, "skill-item"),
                profile_url=page.url,
                screenshot=await self.engine.screenshot(page, f"research-{name}"),
            )
            log.info("portal.research_done", extra={"profile_url": profile_url, "facts": sorted(result.facts().keys())})
            return result

    async def send_message(
        self, profile_url: str, *, subject: str, body: str, idempotency_key: str, expected_name: str
    ) -> ExecutionResult:
        compose_url = profile_url.rstrip("/") + "/message"
        async with self.engine.new_page(f"send-{expected_name or 'message'}") as page:
            await self._open(page, compose_url)
            await actions.wait_visible(page, "compose-form")
            recipient = await actions.text_of(page, "recipient-name") or ""
            self._check_person(expected_name, recipient, compose_url)
            await actions.fill(page, "compose-subject", subject)
            await actions.fill(page, "compose-body", body)
            # The same key on every retry makes the send idempotent: the portal will not deliver twice.
            await actions.set_hidden_value(page, "idempotency-key", idempotency_key)
            response = await actions.submit_and_wait(page, "send-button")
            await self.engine.raise_for_page(page, response, page.url)
            form_error = await actions.text_of(page, "compose-error", required=False)
            if form_error:
                raise ValidationFailedError(f"The portal rejected the message: {form_error}", details={"url": page.url})
            await actions.wait_visible(page, "sent-confirmation")
            result = ExecutionResult(
                confirmation_id=await actions.text_of(page, "confirmation-id") or "",
                recipient=await actions.text_of(page, "sent-recipient") or recipient,
                sent_subject=await actions.text_of(page, "sent-subject") or subject,
                duplicate=await page.get_by_test_id("duplicate-notice").count() > 0,
                page_url=page.url,
                screenshot=await self.engine.screenshot(page, f"sent-{recipient}"),
            )
            log.info(
                "portal.message_sent", extra={"confirmation_id": result.confirmation_id, "duplicate": result.duplicate}
            )
            return result
