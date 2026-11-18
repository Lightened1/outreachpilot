"""Asynchronous Playwright browser engine.

One browser process is shared; every job gets its own short-lived browser *context* (isolated
cookies and storage) so concurrent jobs cannot interfere. The number of open pages is bounded
by a semaphore. When ``persist_browser_session`` is on, the context's cookies/local storage are
saved to a state file after each job and loaded into the next one, so the demo login survives
restarts (the file lives in ``.sessions/``, which is git-ignored).

Every navigation goes through ``goto``, which enforces, in order:

1. the host is in ``ALLOWED_AUTOMATION_HOSTS``;
2. robots.txt allows the URL;
3. the per-host rate limit;
4. the navigation timeout;
5. response-status classification (403/429 -> blocked, 404 -> not found, 5xx -> retryable);
6. detection of challenge/"unusual activity" pages, which stop the run instead of being bypassed.

The engine sends an honest user agent and never uses stealth plugins, fingerprint spoofing or
CAPTCHA solving.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response, async_playwright

from app.automation.errors import translate_errors
from app.automation.robots import RobotsPolicy
from app.config import Settings
from app.core.errors import (
    AccessBlockedError,
    AppError,
    ConfigurationError,
    HostNotAllowedError,
    NavigationError,
    PageNotFoundError,
)
from app.core.logging import get_logger
from app.core.rate_limit import KeyedRateLimiter

log = get_logger(__name__)

USER_AGENT = "OutreachPilot/1.0 (+https://github.com/lightened1/outreachpilot; demo automation)"
CHALLENGE_TEXT = re.compile(
    r"captcha|unusual activity|verify you are (a )?human|access (is )?(temporarily )?restricted", re.IGNORECASE
)


class BrowserEngine:
    def __init__(
        self, settings: Settings, *, rate_limiter: KeyedRateLimiter | None = None, robots: RobotsPolicy | None = None
    ) -> None:
        self.settings = settings
        self.rate_limiter = rate_limiter or KeyedRateLimiter.per_minute(
            settings.navigations_per_minute, settings.navigation_burst
        )
        self.robots = robots or RobotsPolicy(USER_AGENT, enabled=settings.respect_robots_txt)
        self._pages = asyncio.Semaphore(settings.browser_max_pages)
        self._start_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.screenshot_dir = Path(settings.artifacts_dir) / "screenshots"
        self.trace_dir = Path(settings.artifacts_dir) / "traces"

    # ---- lifecycle ----------------------------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def start(self) -> None:
        async with self._start_lock:
            if self.started:
                return
            if sys.platform == "win32" and not isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
                # uvicorn --reload on Windows uses a selector loop, which cannot start subprocesses.
                raise ConfigurationError(
                    "Playwright cannot start a browser on Windows with this event loop. Start the API "
                    "without --reload: uvicorn app.main:app --port 8000"
                )
            with translate_errors("launch the browser"):
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.settings.browser_headless,
                    channel=self.settings.browser_channel or None,
                    slow_mo=self.settings.browser_slow_mo_ms or None,
                )
            log.info(
                "browser.started", extra={"headless": self.settings.browser_headless, "version": self._browser.version}
            )

    async def stop(self) -> None:
        async with self._start_lock:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:  # noqa: BLE001 - shutting down anyway
                    log.warning("browser.close_failed")
            if self._playwright is not None:
                await self._playwright.stop()
            self._browser, self._playwright = None, None
            log.info("browser.stopped")

    async def __aenter__(self) -> BrowserEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # ---- pages --------------------------------------------------------------------------------
    @asynccontextmanager
    async def new_page(self, label: str = "browser-job") -> AsyncIterator[Page]:
        """Open an isolated context + page; always closed (and the session saved) afterwards.

        With ``BROWSER_TRACE_ON_FAILURE`` a Playwright trace is recorded and kept only when the job
        fails; its path is added to the error details (open it with ``playwright show-trace``).
        """
        async with self._pages:
            await self.start()
            assert self._browser is not None
            state_file = self.settings.session_state_file
            storage_state = str(state_file) if self.settings.persist_browser_session and state_file.is_file() else None
            with translate_errors("open a browser context"):
                context: BrowserContext = await self._browser.new_context(
                    user_agent=USER_AGENT, storage_state=storage_state, viewport={"width": 1280, "height": 900}
                )
            context.set_default_timeout(self.settings.browser_timeout_ms)
            context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)
            tracing = self.settings.browser_trace_on_failure
            if tracing:
                try:
                    await context.tracing.start(screenshots=True, snapshots=True)
                except Exception:  # noqa: BLE001 - tracing is diagnostics only
                    tracing = False
            try:
                page = await context.new_page()
                yield page
            except Exception as exc:
                if tracing:
                    tracing = False
                    path = await self._save_trace(context, label)
                    if path and isinstance(exc, AppError):
                        exc.details.setdefault("trace", path)
                raise
            finally:
                if tracing:  # the job succeeded (or was cancelled): discard the trace
                    with contextlib.suppress(Exception):
                        await context.tracing.stop()
                if self.settings.persist_browser_session:
                    await self._save_state(context)
                await context.close()

    async def _save_trace(self, context: BrowserContext, label: str, keep: int = 50) -> str | None:
        try:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60] or "job"
            path = self.trace_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S%f}-{slug}.zip"
            await context.tracing.stop(path=str(path))
            for old in sorted(self.trace_dir.glob("*.zip"))[:-keep]:  # bounded disk use
                old.unlink(missing_ok=True)
            log.info("browser.trace_saved", extra={"path": str(path)})
            return str(path)
        except Exception:  # noqa: BLE001 - never mask the original failure
            log.warning("browser.trace_failed")
            return None

    async def _save_state(self, context: BrowserContext) -> None:
        async with self._state_lock:
            try:
                path = self.settings.session_state_file
                path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(path))
            except Exception:  # noqa: BLE001 - losing the session only costs a re-login
                log.warning("browser.session_save_failed")

    # ---- guarded navigation -------------------------------------------------------------------
    def check_host(self, url: str) -> str:
        host = (urlsplit(url).hostname or "").lower()
        if host not in self.settings.allowed_automation_hosts:
            raise HostNotAllowedError(
                f"Automation is not permitted on host '{host}'. Add it to ALLOWED_AUTOMATION_HOSTS only if "
                "you are authorised to automate that site.",
                details={"url": url, "host": host},
            )
        return host

    async def goto(self, page: Page, url: str) -> Response | None:
        host = self.check_host(url)

        async def fetch(robots_url: str) -> tuple[int, str]:
            with translate_errors("fetch robots.txt", url=robots_url):
                resp = await page.request.get(
                    robots_url, timeout=self.settings.navigation_timeout_ms, fail_on_status_code=False
                )
                return resp.status, await resp.text()

        await self.robots.check(url, fetch)
        waited = await self.rate_limiter.acquire(host)
        if waited > 0:
            log.info("rate_limit.waited", extra={"host": host, "waited_s": round(waited, 3)})
        log.info("browser.navigate", extra={"url": url})
        with translate_errors("navigate", url=url):
            response = await page.goto(url, wait_until="domcontentloaded")
        await self.raise_for_page(page, response, url)
        return response

    async def raise_for_page(self, page: Page, response: Response | None, url: str) -> None:
        status = response.status if response is not None else 200
        if status in (403, 429) or await self._looks_like_challenge(page):
            raise AccessBlockedError(
                "The site is restricting access (rate limit or verification challenge). The automation stopped "
                "and did not try to bypass it.",
                details={"url": url, "status": status},
            )
        if status == 404:
            raise PageNotFoundError("The page does not exist.", details={"url": url, "status": status})
        if status >= 500:
            raise NavigationError("The site returned a server error.", details={"url": url, "status": status})

    @staticmethod
    async def _looks_like_challenge(page: Page) -> bool:
        try:
            if await page.get_by_test_id("access-blocked").count() > 0:
                return True
            title = await page.title()
            return bool(CHALLENGE_TEXT.search(title))
        except Exception:  # noqa: BLE001 - page may be navigating; treat as not a challenge
            return False

    # ---- artifacts ----------------------------------------------------------------------------
    async def screenshot(self, page: Page, label: str) -> str:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60] or "page"
        path = self.screenshot_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%S%f}-{slug}.png"
        with translate_errors("capture a screenshot"):
            await page.screenshot(path=str(path), full_page=True)
        return str(path)
