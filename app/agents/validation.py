"""Validation of generated drafts before a human sees them.

A draft is rejected if it:

* is not valid JSON with ``subject``, ``body`` and ``facts_used``;
* is too long or too short, or does not greet the prospect by first name;
* contains template placeholders (``[Name]``, ``{company}``, ``<insert>``...);
* cites fact keys that do not exist;
* mentions a number, link or proper noun that appears nowhere in the facts, the sender profile
  or the user's own instructions (a cheap but effective guard against invented details);
* claims shared history or events we cannot know ("as we discussed", "congratulations on...").

The proper-noun check is a heuristic: it errs on the side of rejecting, and the agent then asks
the model to rewrite with the reasons attached.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

_PLACEHOLDER = re.compile(r"\[[^\]]{1,40}\]|\{\{?[^{}]{1,40}\}?\}|<[A-Za-z _]{1,30}>|\b(?:INSERT|TBD|XXX|TODO)\b")
_NUMBER = re.compile(r"\d[\d,.]*%?")
_URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CAPITALISED = re.compile(r"\b[A-Z][A-Za-z0-9'’&\-]*")
_UNSUPPORTED_CLAIMS = [
    (
        re.compile(
            r"\b(as we discussed|our (last )?(call|conversation|meeting|chat)|we met|great (meeting|chatting))\b", re.I
        ),
        "claims a previous interaction that is not in the facts",
    ),
    (
        re.compile(r"\b(congrat\w*|promotion|new role|recently joined|anniversary)\b", re.I),
        "congratulates or mentions a career event that is not in the facts",
    ),
    (
        re.compile(r"\b(mutual (connection|friend)s?|we both know|introduced me)\b", re.I),
        "claims a mutual connection that is not in the facts",
    ),
]
_ACTIVITY_CLAIM = re.compile(r"\b(your|recent) (recent )?(post|article|talk|panel|write-up|webinar)\b", re.I)

COMMON_WORDS = frozenset(
    """
a about after again all also am an and any are as at be best both but by can could dear do does for from
glad good happy have hello hey hi how i i'd i'll i'm i've if in is it it's just kind let me more most my
no not of on open or our quick regards short so some sounds thank thanks that the their them then there
these they this to warm we we'd we're were what when which while who why will with would yes you your
you're question questions idea ideas note hello hope cheers sincerely team monday tuesday wednesday thursday
friday saturday sunday january february march april may june july august september october november december
worth curious looking interested keen free noticed saw read came feel given since because reaching wanted
wondering love great nice really seems sounded happy liked enjoyed please ps p.s. fyi
""".split()
)


class Draft(BaseModel):
    subject: str = Field(min_length=3, max_length=80)
    body: str = Field(min_length=40, max_length=2000)
    facts_used: list[str] = Field(default_factory=list)


def parse_draft(content: str) -> tuple[Draft | None, list[str]]:
    """Parse the model output (tolerating code fences or text around the JSON object)."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, ["the reply was not a JSON object"]
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        return None, [f"the reply was not valid JSON ({exc.msg})"]
    try:
        return Draft.model_validate(data), []
    except ValidationError as exc:
        return None, [f"field '{'.'.join(map(str, e['loc']))}': {e['msg']}" for e in exc.errors()]


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return str(value) if value is not None else ""


def validate_draft(
    draft: Draft,
    *,
    facts: dict[str, Any],
    sender: dict[str, str],
    max_words: int,
    extra_instructions: str | None = None,
) -> list[str]:
    problems: list[str] = []
    text = f"{draft.subject}\n{draft.body}"
    allowed_text = " ".join([_flatten(facts), _flatten(sender), extra_instructions or ""])
    allowed_lower = allowed_text.lower()
    allowed_words = set(re.findall(r"[a-z0-9'’&\-]+", allowed_lower))

    words = len(re.findall(r"\S+", draft.body))
    if words > max_words:
        problems.append(f"the body has {words} words; the limit is {max_words}")
    if words < 15:
        problems.append("the body is too short to be a meaningful message")

    first_name = str(facts.get("name", "")).split()[0] if facts.get("name") else ""
    if first_name and first_name.lower() not in draft.body.lower():
        problems.append(f"the message does not address the prospect by first name ('{first_name}')")

    if _PLACEHOLDER.search(text):
        problems.append(f"contains a template placeholder: '{_PLACEHOLDER.search(text).group(0)}'")  # type: ignore[union-attr]

    unknown_keys = sorted(set(draft.facts_used) - set(facts))
    if unknown_keys:
        problems.append(f"facts_used lists keys that do not exist: {', '.join(unknown_keys)}")

    for number in {n.rstrip(".,") for n in _NUMBER.findall(text)}:
        if number and number not in allowed_text:
            problems.append(f"mentions the number '{number}', which is not in the facts")

    for url in _URL.findall(text):
        if url.rstrip(".,)").lower() not in allowed_lower:
            problems.append(f"contains a link that is not in the facts: {url}")

    unsupported = []
    for token in _CAPITALISED.findall(text):
        word = token.lower().strip("'’-")
        if not word or word in COMMON_WORDS or word in allowed_words:
            continue
        if word.endswith(("'s", "’s")) and word[:-2] in allowed_words:
            continue
        unsupported.append(token)
    if unsupported:
        problems.append("mentions names or terms that are not in the facts: " + ", ".join(sorted(set(unsupported))))

    for pattern, reason in _UNSUPPORTED_CLAIMS:
        match = pattern.search(text)
        if match and match.group(0).lower() not in allowed_lower:
            problems.append(f"{reason} ('{match.group(0)}')")
    if _ACTIVITY_CLAIM.search(text) and not facts.get("recent_activity"):
        problems.append("refers to a post or talk, but the facts contain no recent activity")
    return problems
