"""Structured logging with secret redaction.

Use ``get_logger(__name__)`` and pass structured fields through ``extra``:

    log.info("task.started", extra={"task_id": task.id, "task_type": task.type})

The JSON formatter emits one object per line; the console formatter prints ``event key=value``.
A redaction filter masks API keys, bearer tokens, passwords and e-mail addresses in both the
message and the extra fields, so secrets never reach log files.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}

_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"(?i)(api[_-]?key|password|passwd|secret|token)(\"?\s*[:=]\s*\"?)([^\s\",&]+)"), r"\1\2***"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "***@***"),
]
_SENSITIVE_KEYS = {"api_key", "openai_api_key", "password", "demo_password", "authorization", "token", "secret"}


def redact(value: Any) -> Any:
    """Mask secrets in strings and (recursively) in dicts and lists."""
    if isinstance(value, str):
        for pattern, repl in _SECRET_PATTERNS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: ("***" if str(k).lower() in _SENSITIVE_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


# Set per HTTP request by the API middleware; copied into background tasks started by that request.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        request_id = request_id_var.get()
        if request_id and not hasattr(record, "request_id"):
            record.request_id = request_id
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            record.args = (
                tuple(redact(a) for a in record.args) if isinstance(record.args, tuple) else redact(record.args)
            )
        for key, val in list(vars(record).items()):
            if key not in _STANDARD_ATTRS:
                setattr(record, key, "***" if key.lower() in _SENSITIVE_KEYS else redact(val))
        return True


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        fields = " ".join(f"{k}={v}" for k, v in _extra_fields(record).items())
        line = f"{ts} {record.levelname:<7} {record.name}: {record.getMessage()}" + (f" | {fields}" if fields else "")
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def configure_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """Configure the root ``outreachpilot`` logger. Safe to call more than once."""
    root = logging.getLogger("outreachpilot")
    root.setLevel(level.upper())
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else ConsoleFormatter())
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``outreachpilot`` namespace."""
    if not name.startswith("outreachpilot"):
        name = "outreachpilot." + name.removeprefix("app.")
    return logging.getLogger(name)
