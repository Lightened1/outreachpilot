"""Offline, deterministic stand-in for an LLM.

It reads the facts block from the prompt and writes a message from templates, using only those
facts, so the whole workflow runs with no API key. For tests it can also replay a ``script`` of
canned replies or exceptions, which makes failure handling easy to exercise.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable

from app.agents.llm import ChatMessages, LLMResponse
from app.agents.prompts import FACTS_END, FACTS_START, SENDER_END, SENDER_START, extract_block


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


class MockLLMClient:
    name = "mock-llm"

    def __init__(self, script: Iterable[str | BaseException] | None = None) -> None:
        self._script: deque[str | BaseException] = deque(script or [])
        self.calls: list[ChatMessages] = []

    async def complete(
        self, messages: ChatMessages, *, temperature: float = 0.4, json_mode: bool = True
    ) -> LLMResponse:
        self.calls.append(messages)
        if self._script:
            item = self._script.popleft()
            if isinstance(item, BaseException):
                raise item
            return LLMResponse(content=item, model=self.name)
        return LLMResponse(content=json.dumps(self._compose(messages)), model=self.name)

    async def aclose(self) -> None:
        return None

    # ---- template generation -------------------------------------------------------------------
    @staticmethod
    def _compose(messages: ChatMessages) -> dict[str, object]:
        prompt = "\n".join(m["content"] for m in messages if m["role"] == "user")
        facts = extract_block(prompt, FACTS_START, FACTS_END)
        sender = extract_block(prompt, SENDER_START, SENDER_END)
        tone = "professional"
        for line in prompt.splitlines():
            if line.startswith("TONE:"):
                tone = line.split(":", 1)[1].strip().split(" ", 1)[0]

        first = str(facts.get("name", "there")).split()[0]
        company = facts.get("company")
        headline = facts.get("headline") or facts.get("job_title")
        activity = (facts.get("recent_activity") or [None])[0]
        used: list[str] = ["name"]

        if activity:
            hook = f"I read that you {_lower_first(str(activity).rstrip('.'))}."
            used.append("recent_activity")
        elif headline and company:
            hook = f"I noticed you work as {headline} at {company}."
            used += ["headline" if "headline" in facts else "job_title", "company"]
        elif company:
            hook = f"I noticed you are at {company}."
            used.append("company")
        else:
            hook = "I came across your profile."

        bridge_uses_company = bool(company) and "company" not in used
        if bridge_uses_company:
            bridge = f"It sounds relevant to the work your team does at {company}."
        else:
            bridge = "It sounds close to the problems we spend our time on."

        s_name = sender.get("name", "Alex")
        intro = (
            f"I'm {s_name}, {sender.get('role', 'a solutions engineer')} at {sender.get('company', 'our team')}; "
            f"{sender.get('value_prop', 'we help teams automate repetitive work')}."
        )
        if tone != "concise" and bridge_uses_company:
            used.append("company")
        if tone == "friendly":
            body = (
                f"Hi {first}!\n\n{hook} {bridge}\n\n{intro} If it is useful, I would be glad to share what has "
                f"worked for similar teams. Would a short chat be worth it?\n\nThanks,\n{s_name}"
            )
            subject = "A quick hello"
        elif tone == "concise":
            body = f"Hi {first},\n\n{hook} {intro} Open to a short call?\n\nBest,\n{s_name}"
            subject = "Short question"
        else:
            body = (
                f"Hi {first},\n\n{hook} {bridge}\n\n{intro} Would you be open to a short call to compare "
                f"notes?\n\nBest regards,\n{s_name}"
            )
            subject = f"Question about {company}" if company else "A short question"
            if company and "company" not in used:
                used.append("company")
        return {"subject": subject, "body": body, "facts_used": used}
