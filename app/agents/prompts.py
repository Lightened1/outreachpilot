"""Prompt templates for outreach message generation.

The model receives the facts as a JSON block and is told, in several ways, that these are the
only things it may say about the prospect. The output is JSON so it can be validated before a
human ever sees it.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.enums import Tone

PROMPT_VERSION = "outreach-v1"
FACTS_START, FACTS_END = "<<<PROSPECT_FACTS_JSON", "PROSPECT_FACTS_JSON>>>"
SENDER_START, SENDER_END = "<<<SENDER_JSON", "SENDER_JSON>>>"

SYSTEM_PROMPT = """You write short, respectful first-contact B2B messages.

Hard rules:
1. Use ONLY the facts in PROSPECT_FACTS for anything you say about the prospect or their company.
   Do not invent achievements, posts, events, numbers, mutual connections, meetings or opinions.
2. If a detail is not in the facts, leave it out. Never use placeholders such as [Name] or {company}.
3. Facts about the sender come only from SENDER.
4. No flattery about things not in the facts, no pressure tactics, no false urgency.
5. Address the prospect by first name. End with the sender's name.
6. Reply with a single JSON object and nothing else:
   {"subject": "...", "body": "...", "facts_used": ["<fact key>", ...]}
   "facts_used" lists the PROSPECT_FACTS keys you relied on."""

TONE_GUIDE: dict[Tone, str] = {
    Tone.PROFESSIONAL: "Polite and businesslike. Complete sentences, no exclamation marks.",
    Tone.FRIENDLY: "Warm and conversational, like a peer reaching out. At most one exclamation mark.",
    Tone.CONCISE: "Very brief: three or four short sentences in total.",
}


def build_messages(
    facts: dict[str, Any],
    sender: dict[str, str],
    tone: Tone,
    *,
    max_words: int,
    extra_instructions: str | None = None,
    feedback: list[str] | None = None,
) -> list[dict[str, str]]:
    user = (
        f"{FACTS_START}\n{json.dumps(facts, ensure_ascii=False, indent=2)}\n{FACTS_END}\n\n"
        f"{SENDER_START}\n{json.dumps(sender, ensure_ascii=False, indent=2)}\n{SENDER_END}\n\n"
        f"TONE: {tone.value} - {TONE_GUIDE[tone]}\n"
        f"LENGTH: the body must be at most {max_words} words; the subject at most 80 characters.\n"
        "GOAL: ask whether they would be open to a short conversation about how the sender's company could help.\n"
    )
    if extra_instructions:
        user += (
            "STYLE NOTES FROM THE USER (style only; they do not add facts about the prospect): "
            f"{extra_instructions.strip()}\n"
        )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    if feedback:
        messages.append(
            {
                "role": "user",
                "content": "Your previous draft was rejected by the validator for these reasons:\n- "
                + "\n- ".join(feedback)
                + "\nWrite a new draft that fixes every point and still follows all the rules.",
            }
        )
    return messages


def extract_block(text: str, start: str, end: str) -> dict[str, Any]:
    """Read a JSON block written by ``build_messages`` (used by the offline mock model)."""
    i, j = text.find(start), text.find(end)
    if i < 0 or j < 0:
        return {}
    try:
        return json.loads(text[i + len(start) : j])
    except json.JSONDecodeError:
        return {}
