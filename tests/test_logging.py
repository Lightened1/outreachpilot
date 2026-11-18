"""Structured logging never leaks secrets."""

from __future__ import annotations

import io
import json
import logging

from app.core.logging import JsonFormatter, RedactingFilter, configure_logging, get_logger, redact


def test_redact_masks_common_secrets() -> None:
    text = redact("key sk-abcdefghijklmnop Authorization: Bearer abc.def password=hunter2 mail ana@example.com")
    assert "sk-abcdefghijklmnop" not in text and "hunter2" not in text and "abc.def" not in text
    assert "ana@example.com" not in text


def test_redact_handles_nested_structures() -> None:
    data = redact({"openai_api_key": "sk-123456789012", "nested": [{"password": "x"}, "token=abc123"]})
    assert data == {"openai_api_key": "***", "nested": [{"password": "***"}, "token=***"]}


def test_json_logs_include_fields_and_are_redacted() -> None:
    configure_logging("INFO", json_logs=True)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    log = get_logger("app.tests")
    logging.getLogger("outreachpilot").addHandler(handler)
    try:
        log.info("llm.request", extra={"api_key": "sk-supersecret123", "task_id": "t1"})
    finally:
        logging.getLogger("outreachpilot").removeHandler(handler)
    record = json.loads(stream.getvalue().strip())
    assert record["event"] == "llm.request" and record["task_id"] == "t1"
    assert record["api_key"] == "***" and record["logger"] == "outreachpilot.tests"


def test_log_fields_never_shadow_logrecord_attributes() -> None:
    """logging raises KeyError for extra={"created": ...} and similar; catch that before it ships."""
    import logging
    import re
    from pathlib import Path

    reserved = set(vars(logging.LogRecord("n", 20, "p", 1, "m", (), None))) | {"message", "asctime"}
    offenders = []
    for path in Path(__file__).resolve().parents[1].joinpath("app").rglob("*.py"):
        for block in re.findall(r"extra=\{(.*?)\}", path.read_text(encoding="utf-8"), re.S):
            offenders += [f"{path.name}:{key}" for key in re.findall(r'"(\w+)"\s*:', block) if key in reserved]
    assert offenders == []
