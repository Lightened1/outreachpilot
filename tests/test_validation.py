"""Input validation, duplicate-key normalisation and startup configuration checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.enums import ApprovalDecision
from app.core.errors import ConfigurationError, ValidationFailedError
from app.schemas import ApprovalRequest, ProspectCreate, ProspectUpdate, dedupe_key, normalize_url
from app.schemas.prospect import IMPORT_MAX_ROWS, parse_import_csv


def test_prospect_fields_are_cleaned() -> None:
    p = ProspectCreate(
        name="  Priya   Raman ",
        job_title=" Head of Ops ",
        company="Bluefin",
        source_url=" http://127.0.0.1:8010/people/1 ",
        notes="  ",
    )
    assert p.name == "Priya Raman"
    assert p.job_title == "Head of Ops"
    assert p.notes is None
    assert p.source_url == "http://127.0.0.1:8010/people/1"


@pytest.mark.parametrize(
    "payload, field",
    [
        ({"name": "R2-D2", "company": "X"}, "name"),
        ({"name": "A", "company": "X"}, "name"),
        ({"name": "Ana Lima", "source_url": "ftp://example.com/x"}, "source_url"),
        ({"name": "Ana Lima", "source_url": "not a url"}, "source_url"),
        ({"name": "Ana Lima", "company": "X", "notes": "n" * 2001}, "notes"),
    ],
)
def test_invalid_prospects_are_rejected(payload: dict, field: str) -> None:
    with pytest.raises(ValidationError) as exc:
        ProspectCreate(**payload)
    assert field in str(exc.value)


def test_prospect_needs_url_or_company() -> None:
    with pytest.raises(ValidationError, match="source_url or a company"):
        ProspectCreate(name="Ana Lima")


@pytest.mark.parametrize("name", ["Tomás O'Neill-Ferreira", "Priya R. Raman", "J.R. Smith", "Zoë Saldaña", "Li Wei"])
def test_real_world_names_are_valid(name: str) -> None:
    assert ProspectCreate(name=name, company="Atlas").name == name


def test_update_cannot_set_status_directly() -> None:
    with pytest.raises(ValidationError):
        ProspectUpdate(status="completed")  # type: ignore[call-arg]


def test_url_normalisation_for_duplicates() -> None:
    assert normalize_url("HTTP://WWW.Example.COM/people/1/?utm=x#top") == "http://example.com/people/1"
    a = ProspectCreate(name="Ana Lima", source_url="http://example.com/people/1").dedupe_key
    b = ProspectCreate(name="Ana Lima", source_url="http://www.example.com/people/1/").dedupe_key
    assert a == b


def test_name_company_key_ignores_case_accents_and_spaces() -> None:
    assert dedupe_key("José  Núñez", "ACME Corp", None) == dedupe_key("jose nunez", "acme corp", None)
    assert dedupe_key("José Núñez", "ACME", None) != dedupe_key("José Núñez", "Other", None)


def test_rejection_requires_reason() -> None:
    with pytest.raises(ValidationError, match="reason"):
        ApprovalRequest(decision=ApprovalDecision.REJECT, reviewer="alex")
    assert ApprovalRequest(decision=ApprovalDecision.APPROVE, reviewer="alex").reason is None


# ---- configuration -----------------------------------------------------------------------------
def test_defaults_run_in_mock_mode_without_key() -> None:
    s = Settings.from_env({}, env_file=None)
    assert s.effective_llm_mode == "mock"
    assert "openai_api_key" not in str(s.public_summary())


def test_auto_mode_uses_openai_when_key_present() -> None:
    s = Settings.from_env({"OPENAI_API_KEY": "sk-test-0123456789"}, env_file=None)
    assert s.effective_llm_mode == "openai"
    assert "sk-test" not in repr(s)  # SecretStr never prints the key


@pytest.mark.parametrize(
    "env, message",
    [
        ({"LLM_MODE": "openai"}, "requires OPENAI_API_KEY"),
        ({"DEMO_PORTAL_URL": "https://www.linkedin.com"}, "ALLOWED_AUTOMATION_HOSTS"),
        ({"TASK_MAX_CONCURRENCY": "0"}, "task_max_concurrency"),
        ({"DATABASE_URL": "postgresql://x"}, "SQLite"),
        ({"RETRY_BASE_DELAY_S": "5", "RETRY_MAX_DELAY_S": "1"}, "RETRY_MAX_DELAY_S"),
        ({"ENVIRONMENT": "production"}, "API_KEY is required"),
        ({"API_KEY": "too-short"}, "at least 16 characters"),
    ],
)
def test_invalid_configuration_fails_at_startup(env: dict, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Settings.from_env(env, env_file=None)


def test_env_values_are_parsed() -> None:
    s = Settings.from_env(
        {
            "BROWSER_HEADLESS": "false",
            "ALLOWED_AUTOMATION_HOSTS": "127.0.0.1, demo ,localhost",
            "TASK_MAX_ATTEMPTS": "5",
        },
        env_file=None,
    )
    assert s.browser_headless is False
    assert s.allowed_automation_hosts == ["127.0.0.1", "demo", "localhost"]
    assert s.task_max_attempts == 5


def test_security_settings_are_parsed_and_never_exposed() -> None:
    s = Settings.from_env(
        {"ENVIRONMENT": "production", "API_KEY": "x" * 32, "CORS_ORIGINS": "https://ops.example.org/, http://a.test"},
        env_file=None,
    )
    assert s.cors_origins == ["https://ops.example.org", "http://a.test"]
    assert s.public_summary()["auth"] == "api_key"
    assert "x" * 32 not in repr(s) and "x" * 32 not in str(s.public_summary())


def test_csv_rows_are_parsed_with_header_aliases() -> None:
    text = (
        "\ufeffFull Name,Role,Organization,URL,Notes,Other\n"
        "Priya Raman,Head of Operations,Bluefin Logistics,http://127.0.0.1:8010/people/1,met at fair,x\n"
        ",,,,,\n"
        "R2-D2,Droid,Rebellion,,,\n"
        "Ana Lima,,,,,\n"
    )
    rows = parse_import_csv(text)
    assert [r.line for r in rows] == [2, 4, 5]
    first = rows[0].payload
    assert first is not None and first.job_title == "Head of Operations" and first.notes == "met at fair"
    assert rows[1].payload is None and "name" in rows[1].errors[0]
    assert rows[2].payload is None and any("source_url or a company" in e for e in rows[2].errors)


def test_csv_needs_a_name_column_and_has_a_row_limit() -> None:
    with pytest.raises(ValidationFailedError, match="name"):
        parse_import_csv("company,url\nAcme,http://x\n")
    big = "name,company\n" + "".join(f"Person Number,Co {i}\n" for i in range(IMPORT_MAX_ROWS + 1))
    with pytest.raises(ValidationFailedError, match="more than"):
        parse_import_csv(big)
