from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.enums import MessageStatus, ProspectStatus, Tone
from app.core.errors import ValidationFailedError

_NAME_RE = re.compile(r"^[^\W\d_]+(?:(?:[ '\-]|\. ?)[^\W\d_]+)*\.?$", re.UNICODE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = _CONTROL_RE.sub("", unicodedata.normalize("NFC", value))
    value = re.sub(r"[ \t]+", " ", value).strip()
    return value or None


def normalize_url(url: str) -> str:
    """Canonical form used for duplicate detection: lower-case host, no www., query, fragment
    or trailing slash."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port:
        host = f"{host}:{parts.port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def dedupe_key(name: str, company: str | None, source_url: str | None) -> str:
    """Identity of a prospect. A profile URL identifies a person on its own; otherwise we use
    name + company (case-, accent- and whitespace-insensitive)."""
    if source_url:
        return "url:" + normalize_url(source_url)

    def norm(s: str | None) -> str:
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        return re.sub(r"\s+", " ", s).strip().lower()

    return f"name:{norm(name)}|company:{norm(company)}"


class _ProspectFields(BaseModel):
    @field_validator("name", "job_title", "company", "notes", mode="before", check_fields=False)
    @classmethod
    def _clean(cls, v: Any) -> Any:
        return _clean_text(v) if isinstance(v, str) else v

    @field_validator("name", check_fields=False)
    @classmethod
    def _valid_name(cls, v: str | None) -> str | None:
        if v is not None and not _NAME_RE.match(v):
            raise ValueError("name may contain letters, spaces, apostrophes, hyphens and periods only")
        return v

    @field_validator("source_url", mode="before", check_fields=False)
    @classmethod
    def _valid_url(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        if not isinstance(v, str):
            raise ValueError("source_url must be a string")
        parts = urlsplit(v.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("source_url must be an absolute http(s) URL")
        return v.strip()


class ProspectCreate(_ProspectFields):
    name: str = Field(..., min_length=2, max_length=120, examples=["Priya Raman"])
    job_title: str | None = Field(default=None, max_length=120, examples=["Head of Operations"])
    company: str | None = Field(default=None, max_length=120, examples=["Bluefin Logistics"])
    source_url: str | None = Field(default=None, max_length=500, examples=["http://127.0.0.1:8010/people/1"])
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _identity_present(self) -> ProspectCreate:
        if not self.source_url and not self.company:
            raise ValueError("provide a source_url or a company so the prospect can be identified")
        return self

    @property
    def dedupe_key(self) -> str:
        return dedupe_key(self.name, self.company, self.source_url)


class ProspectUpdate(_ProspectFields):
    """Partial update. Status is not editable here: it only changes through the workflow."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    job_title: str | None = Field(default=None, max_length=120)
    company: str | None = Field(default=None, max_length=120)
    source_url: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    version: int
    tone: Tone
    subject: str
    body: str
    facts_used: list[str]
    model: str
    generation_attempts: int
    status: MessageStatus
    reviewer: str | None = None
    review_reason: str | None = None
    reviewed_at: datetime | None = None
    sent_at: datetime | None = None
    execution_result: dict[str, Any] | None = None
    created_at: datetime


class ProspectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    job_title: str | None
    company: str | None
    source_url: str | None
    notes: str | None
    status: ProspectStatus
    research_data: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class ProspectDetail(ProspectRead):
    latest_message: MessageRead | None = None
    allowed_next_statuses: list[str] = []


class ProspectList(BaseModel):
    items: list[ProspectRead]
    total: int
    limit: int
    offset: int


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: str | None
    event_type: str
    actor: str
    detail: dict[str, Any]
    created_at: datetime


# ---- bulk import ------------------------------------------------------------------------------
IMPORT_MAX_ROWS = 1000
IMPORT_COLUMN_ALIASES = {
    "name": "name",
    "full_name": "name",
    "job_title": "job_title",
    "title": "job_title",
    "role": "job_title",
    "company": "company",
    "organization": "company",
    "source_url": "source_url",
    "url": "source_url",
    "profile_url": "source_url",
    "notes": "notes",
}


@dataclass
class ParsedImportRow:
    line: int
    name: str | None
    payload: ProspectCreate | None
    errors: list[str]


def _header_key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def parse_import_csv(text: str) -> list[ParsedImportRow]:
    """Parse and validate CSV rows without touching the database (blank lines are skipped)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    headers = {h: IMPORT_COLUMN_ALIASES.get(_header_key(h)) for h in (reader.fieldnames or []) if h}
    if "name" not in headers.values():
        raise ValidationFailedError(
            "The CSV needs a header row with at least a 'name' column.", details={"columns": list(headers)}
        )
    parsed: list[ParsedImportRow] = []
    for raw in reader:
        data = {
            field: value.strip()
            for header, field in headers.items()
            if field and isinstance(value := raw.get(header), str) and value.strip()
        }
        if not data:
            continue
        if len(parsed) >= IMPORT_MAX_ROWS:
            raise ValidationFailedError(
                f"The CSV has more than {IMPORT_MAX_ROWS} rows; split it into smaller files.",
                details={"max_rows": IMPORT_MAX_ROWS},
            )
        try:
            payload = ProspectCreate.model_validate(data)
        except ValidationError as exc:
            errors = [f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in exc.errors()]
            parsed.append(ParsedImportRow(reader.line_num, data.get("name"), None, errors))
            continue
        parsed.append(ParsedImportRow(reader.line_num, payload.name, payload, []))
    return parsed


class ImportRequest(BaseModel):
    csv: str = Field(
        min_length=1,
        max_length=1_000_000,
        description="CSV text with a header row. Recognised columns: name, job_title (or title), company, "
        "source_url (or url), notes. Other columns are ignored.",
        examples=[
            "name,job_title,company,source_url\nPriya Raman,Head of Operations,Bluefin Logistics,"
            "http://127.0.0.1:8010/people/1\n"
        ],
    )


class ImportRowResult(BaseModel):
    row: int = Field(description="Line number in the file (the header is line 1).")
    status: Literal["created", "duplicate", "invalid"]
    prospect_id: int | None = None
    name: str | None = None
    errors: list[str] = []


class ImportReport(BaseModel):
    created: int
    duplicates: int
    invalid: int
    rows: list[ImportRowResult]


class StatsResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    tasks: dict[str, int]
