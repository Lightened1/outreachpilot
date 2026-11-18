"""Outreach message agent: prompt -> LLM -> parse -> validate -> (rewrite with feedback).

Transport failures (timeouts, 429/5xx) propagate as retryable errors so the task manager can
back off and try again. Drafts that keep failing validation end in a non-retryable
``MessageValidationError``: the problem is the content, and a human should look at it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.agents.llm import LLMClient
from app.agents.prompts import PROMPT_VERSION, build_messages
from app.agents.validation import parse_draft, validate_draft
from app.config import Settings
from app.core.enums import Tone
from app.core.errors import MessageValidationError
from app.core.logging import get_logger

log = get_logger(__name__)


class GeneratedMessage(BaseModel):
    subject: str
    body: str
    facts_used: list[str]
    tone: Tone
    model: str
    prompt_version: str = PROMPT_VERSION
    attempts: int


def build_facts(prospect: dict[str, Any], research: dict[str, Any] | None) -> dict[str, Any]:
    """Facts the model may use: researched profile data plus the CRM fields entered by a user.

    Internal notes are deliberately excluded; they may contain private context that should not
    be sent to a third-party model or copied into a message.
    """
    facts: dict[str, Any] = {"name": prospect["name"]}
    if prospect.get("job_title"):
        facts["job_title"] = prospect["job_title"]
    if prospect.get("company"):
        facts["company"] = prospect["company"]
    for key, value in (research or {}).items():
        if key in ("screenshot", "profile_url") or value in (None, "", []):
            continue
        facts[key] = value
    return facts


class MessageAgent:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    @property
    def sender(self) -> dict[str, str]:
        s = self.settings
        return {
            "name": s.sender_name,
            "role": s.sender_role,
            "company": s.sender_company,
            "value_prop": s.sender_value_prop,
        }

    async def generate(
        self, facts: dict[str, Any], *, tone: Tone = Tone.PROFESSIONAL, extra_instructions: str | None = None
    ) -> GeneratedMessage:
        feedback: list[str] = []
        history: list[list[str]] = []
        max_attempts = self.settings.message_validation_attempts
        for attempt in range(1, max_attempts + 1):
            messages = build_messages(
                facts,
                self.sender,
                tone,
                max_words=self.settings.message_max_words,
                extra_instructions=extra_instructions,
                feedback=feedback or None,
            )
            response = await self.llm.complete(messages, temperature=0.4 if attempt == 1 else 0.2)
            draft, problems = parse_draft(response.content)
            if draft is not None:
                problems = validate_draft(
                    draft,
                    facts=facts,
                    sender=self.sender,
                    max_words=self.settings.message_max_words,
                    extra_instructions=extra_instructions,
                )
            if draft is not None and not problems:
                log.info(
                    "agent.message_generated", extra={"attempts": attempt, "tone": tone.value, "model": response.model}
                )
                return GeneratedMessage(
                    subject=draft.subject.strip(),
                    body=draft.body.strip(),
                    facts_used=draft.facts_used,
                    tone=tone,
                    model=response.model,
                    attempts=attempt,
                )
            history.append(problems)
            feedback = problems
            log.warning("agent.draft_rejected", extra={"attempt": attempt, "problems": problems})
        raise MessageValidationError(
            f"The model did not produce a valid message in {max_attempts} attempt(s).",
            details={"attempts": [{"attempt": i + 1, "problems": p} for i, p in enumerate(history)]},
        )
