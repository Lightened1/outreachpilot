from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.core.enums import ApprovalDecision, Tone


class GenerateMessageRequest(BaseModel):
    tone: Tone = Tone.PROFESSIONAL
    extra_instructions: str | None = Field(
        default=None,
        max_length=500,
        description="Optional style guidance, e.g. 'mention we can share a case study'. It cannot add facts "
        "about the prospect.",
    )


class ApprovalRequest(BaseModel):
    decision: ApprovalDecision
    reviewer: str = Field(..., min_length=1, max_length=120, examples=["alex@northwind.example"])
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason_for_reject(self) -> ApprovalRequest:
        if self.decision is ApprovalDecision.REJECT and not (self.reason and self.reason.strip()):
            raise ValueError("a reason is required when rejecting a message")
        return self


class ResearchResult(BaseModel):
    """Facts extracted from the prospect's profile page. Only these may be used in messages."""

    name: str
    headline: str | None = None
    company: str | None = None
    location: str | None = None
    about: str | None = None
    recent_activity: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    profile_url: str
    screenshot: str | None = None

    def facts(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(exclude={"screenshot", "profile_url"}).items() if v}


class ExecutionResult(BaseModel):
    confirmation_id: str
    recipient: str
    sent_subject: str
    screenshot: str | None = None
    page_url: str
    duplicate: bool = False
