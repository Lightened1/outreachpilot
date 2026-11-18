"""Application error hierarchy.

Every error carries a stable ``code``, an HTTP ``status_code`` for the API layer and a
``retryable`` flag that the retry logic and the task manager use to decide whether another
attempt can help. Keeping the classification on the error itself means callers never need
to pattern-match on messages.
"""

from __future__ import annotations

import asyncio
from typing import Any


class AppError(Exception):
    """Base class for all expected application errors."""

    code: str = "app_error"
    status_code: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


# --- configuration -------------------------------------------------------------------------
class ConfigurationError(AppError):
    code = "configuration_error"
    status_code = 500


class AuthenticationError(AppError):
    code = "unauthorized"
    status_code = 401


# --- domain / API ----------------------------------------------------------------------------
class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class DuplicateProspectError(AppError):
    code = "duplicate_prospect"
    status_code = 409


class InvalidTransitionError(AppError):
    code = "invalid_transition"
    status_code = 409


class ValidationFailedError(AppError):
    code = "validation_failed"
    status_code = 422


# --- browser automation ------------------------------------------------------------------------
class AutomationError(AppError):
    code = "automation_error"
    status_code = 502


class AutomationTimeoutError(AutomationError):
    """A page, element or navigation did not respond in time. Usually transient."""

    code = "automation_timeout"
    retryable = True


class NavigationError(AutomationError):
    """Network-level failure or a 5xx response. Usually transient."""

    code = "navigation_error"
    retryable = True


class ElementNotFoundError(AutomationError):
    """The page loaded but an expected element is missing: the page layout is not what we expect."""

    code = "element_not_found"


class AccessBlockedError(AutomationError):
    """The site refused access (403/429, a challenge page, a login wall we must not pass).

    Never retried automatically: the correct behaviour is to stop and report to a human.
    """

    code = "access_blocked"
    status_code = 403


class RobotsDisallowedError(AutomationError):
    code = "robots_disallowed"
    status_code = 403


class HostNotAllowedError(AutomationError):
    code = "host_not_allowed"
    status_code = 403


class PageNotFoundError(AutomationError):
    code = "page_not_found"
    status_code = 404


class LoginFailedError(AutomationError):
    """The configured credentials were rejected. Retrying would not help."""

    code = "login_failed"
    status_code = 401


class DataMismatchError(AutomationError):
    """The page we reached does not belong to the prospect we were asked to research."""

    code = "data_mismatch"
    status_code = 409


# --- LLM ----------------------------------------------------------------------------------------
class LLMError(AppError):
    code = "llm_error"
    status_code = 502


class LLMUnavailableError(LLMError):
    """Rate limited, 5xx or network failure from the LLM provider."""

    code = "llm_unavailable"
    retryable = True


class LLMAuthError(LLMError):
    code = "llm_auth_error"
    status_code = 500


class MessageValidationError(LLMError):
    """The model kept producing drafts that fail validation (invented facts, placeholders...)."""

    code = "message_validation_failed"
    status_code = 422


# --- tasks --------------------------------------------------------------------------------------
class TaskCancelledError(AppError):
    code = "task_cancelled"
    status_code = 409


def is_retryable(exc: BaseException) -> bool:
    """Classify any exception as retryable (transient) or not.

    Unknown exceptions are treated as non-retryable: retrying a bug only hides it.
    """
    if isinstance(exc, AppError):
        return exc.retryable
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True
    return False


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Serializable description of an exception for task records and API responses."""
    if isinstance(exc, AppError):
        return {**exc.to_dict(), "retryable": exc.retryable}
    return {
        "code": type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
        "details": {},
        "retryable": is_retryable(exc),
    }
