"""Application configuration.

Settings come from environment variables (optionally loaded from a ``.env`` file) and are
validated once at startup; an invalid configuration stops the app with a clear message
instead of failing later in the middle of a task.

LLM mode:
* ``mock``   - deterministic local generator; no network, no key (default for tests/demo)
* ``openai`` - any OpenAI-compatible ``/chat/completions`` endpoint; requires ``OPENAI_API_KEY``
* ``auto``   - ``openai`` when a key is present, otherwise ``mock`` (with a warning)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator

from app.core.errors import ConfigurationError

ENV_PREFIX = ""  # variables are read without a prefix, e.g. DATABASE_URL


class Settings(BaseModel):
    model_config = {"frozen": True, "extra": "ignore"}

    # general
    app_name: str = "OutreachPilot"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    # HTTP / security
    api_key: SecretStr | None = None  # required in production; sent as the X-API-Key header
    cors_origins: list[str] = Field(default_factory=list)  # extra browser origins allowed to call the API

    # storage
    database_url: str = "sqlite+aiosqlite:///./data/outreachpilot.db"
    artifacts_dir: Path = Path("artifacts")
    browser_session_dir: Path = Path(".sessions")

    # LLM
    llm_mode: Literal["auto", "mock", "openai"] = "auto"
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_json_mode: bool = True
    llm_timeout_s: float = Field(30.0, gt=0, le=300)
    llm_requests_per_minute: float = Field(30.0, gt=0)
    message_max_words: int = Field(130, ge=30, le=400)
    message_validation_attempts: int = Field(3, ge=1, le=10)

    # sender profile used in generated messages (facts about *us*, never about the prospect)
    sender_name: str = "Alex Morgan"
    sender_role: str = "Solutions Engineer"
    sender_company: str = "Northwind Analytics"
    sender_value_prop: str = "we help operations teams automate repetitive reporting work"

    # demo portal / browser automation
    demo_portal_url: str = "http://127.0.0.1:8010"
    demo_username: str = "demo"
    demo_password: SecretStr = SecretStr("demo-password")
    allowed_automation_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "demo"])
    respect_robots_txt: bool = True
    browser_headless: bool = True
    browser_channel: str | None = None  # e.g. "chrome" to use an installed Chrome instead of bundled Chromium
    browser_slow_mo_ms: int = Field(0, ge=0, le=5_000)  # slow down actions for screen recordings
    browser_timeout_ms: int = Field(8_000, ge=500, le=120_000)
    navigation_timeout_ms: int = Field(15_000, ge=500, le=120_000)
    browser_max_pages: int = Field(2, ge=1, le=16)
    persist_browser_session: bool = True
    browser_trace_on_failure: bool = True  # save a Playwright trace (.zip) when a browser job fails
    navigations_per_minute: float = Field(60.0, gt=0)
    navigation_burst: int = Field(5, ge=1)

    # task processing
    task_max_concurrency: int = Field(2, ge=1, le=32)
    task_max_attempts: int = Field(3, ge=1, le=10)
    task_timeout_s: float = Field(120.0, gt=0)
    retry_base_delay_s: float = Field(1.0, ge=0)
    retry_max_delay_s: float = Field(20.0, ge=0)

    @field_validator("database_url")
    @classmethod
    def _check_db(cls, v: str) -> str:
        if not v.startswith("sqlite"):
            raise ValueError("only SQLite URLs are supported (e.g. sqlite+aiosqlite:///./data/app.db)")
        if v.startswith("sqlite://") and "+aiosqlite" not in v:
            v = v.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return v

    @field_validator("demo_portal_url", "openai_base_url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"'{v}' is not an http(s) URL")
        return v.rstrip("/")

    @field_validator("allowed_automation_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [h.strip().lower() for h in v.split(",") if h.strip()]
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = [o.strip().rstrip("/") for o in v.split(",") if o.strip()]
        return v

    @field_validator("browser_channel", mode="before")
    @classmethod
    def _blank_channel_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("openai_api_key", "api_key", mode="before")
    @classmethod
    def _blank_key_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("api_key")
    @classmethod
    def _strong_api_key(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and len(v.get_secret_value()) < 16:
            raise ValueError(
                "API_KEY must be at least 16 characters "
                "(generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))')"
            )
        return v

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        if self.environment == "production" and self.api_key is None:
            raise ValueError("API_KEY is required when ENVIRONMENT=production")
        if self.llm_mode == "openai" and self.openai_api_key is None:
            raise ValueError("LLM_MODE=openai requires OPENAI_API_KEY (use LLM_MODE=auto or mock to run without one)")
        if self.retry_max_delay_s < self.retry_base_delay_s:
            raise ValueError("RETRY_MAX_DELAY_S must be >= RETRY_BASE_DELAY_S")
        demo_host = urlparse(self.demo_portal_url).hostname or ""
        if demo_host.lower() not in self.allowed_automation_hosts:
            raise ValueError(f"DEMO_PORTAL_URL host '{demo_host}' must be listed in ALLOWED_AUTOMATION_HOSTS")
        return self

    # ---- derived values ----------------------------------------------------------------------
    @property
    def effective_llm_mode(self) -> Literal["mock", "openai"]:
        if self.llm_mode == "auto":
            return "openai" if self.openai_api_key else "mock"
        return self.llm_mode

    @property
    def session_state_file(self) -> Path:
        return self.browser_session_dir / "demo_portal_state.json"

    def public_summary(self) -> dict[str, Any]:
        """Configuration safe to expose on /health (no secrets)."""
        return {
            "environment": self.environment,
            "auth": "api_key" if self.api_key else "none",
            "llm_mode": self.effective_llm_mode,
            "llm_model": self.openai_model if self.effective_llm_mode == "openai" else "mock",
            "demo_portal_url": self.demo_portal_url,
            "browser_headless": self.browser_headless,
            "task_max_concurrency": self.task_max_concurrency,
            "task_max_attempts": self.task_max_attempts,
        }

    # ---- loading -----------------------------------------------------------------------------
    @classmethod
    def from_env(
        cls, env: dict[str, str] | None = None, *, env_file: str | Path | None = ".env", **overrides: Any
    ) -> Settings:
        """Build settings from environment variables (upper-case field names)."""
        values: dict[str, Any] = {}
        if env is None:
            if env_file and Path(env_file).is_file():
                from dotenv import dotenv_values

                values.update({k.lower(): v for k, v in dotenv_values(env_file).items() if v is not None})
            env = dict(os.environ)
        for name in cls.model_fields:
            key = (ENV_PREFIX + name).upper()
            if key in env:
                values[name] = env[key]
        values.update(overrides)
        try:
            return cls(**values)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'settings'}: {e['msg']}" for e in exc.errors()
            )
            raise ConfigurationError(f"Invalid configuration: {problems}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
