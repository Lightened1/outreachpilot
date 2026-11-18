"""Translate Playwright exceptions into the application's error hierarchy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.core.errors import AppError, AutomationError, AutomationTimeoutError, NavigationError

_NETWORK_MARKERS = ("net::ERR_", "NS_ERROR_", "Connection refused", "ECONNREFUSED", "ERR_CONNECTION")


@contextmanager
def translate_errors(action: str, **details: object) -> Iterator[None]:
    """Run a block of Playwright calls and convert failures to ``AppError`` subclasses.

    Timeouts and network errors become retryable; everything else is a non-retryable
    ``AutomationError`` carrying the action that failed.
    """
    try:
        yield
    except AppError:
        raise
    except PlaywrightTimeoutError as exc:
        raise AutomationTimeoutError(
            f"Timed out while trying to {action}.", details={**details, "playwright": str(exc).splitlines()[0]}
        ) from exc
    except PlaywrightError as exc:
        text = str(exc)
        first = text.splitlines()[0] if text else type(exc).__name__
        if any(m in text for m in _NETWORK_MARKERS):
            raise NavigationError(
                f"Network error while trying to {action}.", details={**details, "playwright": first}
            ) from exc
        raise AutomationError(
            f"Browser error while trying to {action}.", details={**details, "playwright": first}
        ) from exc
