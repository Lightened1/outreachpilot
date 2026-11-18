"""AI message generation with mocked LLM responses: grounding, validation, regeneration, failures."""

from __future__ import annotations

import json

import pytest

from app.agents.message_agent import MessageAgent, build_facts
from app.agents.mock_llm import MockLLMClient
from app.agents.prompts import FACTS_START, build_messages
from app.agents.validation import Draft, parse_draft, validate_draft
from app.config import Settings
from app.core.enums import Tone
from app.core.errors import LLMUnavailableError, MessageValidationError, is_retryable

pytestmark = pytest.mark.anyio

PROSPECT = {
    "name": "Priya Raman",
    "job_title": "Head of Operations",
    "company": "Bluefin Logistics",
    "notes": "CONFIDENTIAL: budget approved, champion is her boss",
}
RESEARCH = {
    "name": "Priya Raman",
    "headline": "Head of Operations",
    "company": "Bluefin Logistics",
    "location": "Rotterdam, Netherlands",
    "about": "Leads a 40-person operations team.",
    "recent_activity": ["Wrote about moving weekly warehouse reports from spreadsheets to dashboards"],
    "skills": ["Lean"],
    "profile_url": "http://127.0.0.1/people/1",
    "screenshot": "x.png",
}
SENDER = {
    "name": "Alex Morgan",
    "role": "Solutions Engineer",
    "company": "Northwind Analytics",
    "value_prop": "we help operations teams automate repetitive reporting work",
}


@pytest.fixture
def settings() -> Settings:  # overrides the browser fixture: no demo server needed here
    return Settings.from_env({}, env_file=None, message_validation_attempts=3)


@pytest.fixture
def facts() -> dict:
    return build_facts(PROSPECT, RESEARCH)


def draft(**kw) -> str:
    base = {
        "subject": "Question about Bluefin Logistics",
        "body": "Hi Priya,\n\nI read that you wrote about moving weekly warehouse reports from spreadsheets to "
        "dashboards. I'm Alex Morgan at Northwind Analytics; we help operations teams automate "
        "repetitive reporting work. Would you be open to a short call?\n\nBest,\nAlex Morgan",
        "facts_used": ["name", "recent_activity"],
    }
    base.update(kw)
    return json.dumps(base)


def test_internal_notes_and_artifacts_never_reach_the_model(facts: dict) -> None:
    assert "notes" not in facts and "screenshot" not in facts and "profile_url" not in facts
    prompt = json.dumps(build_messages(facts, SENDER, Tone.PROFESSIONAL, max_words=120))
    assert "CONFIDENTIAL" not in prompt and FACTS_START in prompt


@pytest.mark.parametrize("tone", list(Tone))
async def test_mock_generation_is_grounded_for_every_tone(settings: Settings, facts: dict, tone: Tone) -> None:
    msg = await MessageAgent(MockLLMClient(), settings).generate(facts, tone=tone)
    assert msg.attempts == 1 and msg.tone is tone and msg.model == "mock-llm"
    assert msg.body.startswith("Hi Priya")
    assert "Alex Morgan" in msg.body
    assert set(msg.facts_used) <= set(facts)
    assert (
        validate_draft(
            Draft(subject=msg.subject, body=msg.body, facts_used=msg.facts_used),
            facts=facts,
            sender=SENDER,
            max_words=settings.message_max_words,
        )
        == []
    )


async def test_concise_tone_is_shorter(settings: Settings, facts: dict) -> None:
    agent = MessageAgent(MockLLMClient(), settings)
    concise = await agent.generate(facts, tone=Tone.CONCISE)
    friendly = await agent.generate(facts, tone=Tone.FRIENDLY)
    assert len(concise.body.split()) < len(friendly.body.split())


async def test_minimal_facts_do_not_produce_invented_details(settings: Settings) -> None:
    facts = build_facts({"name": "Marcus Oyelaran", "company": "Tidewater Health"}, None)
    msg = await MessageAgent(MockLLMClient(), settings).generate(facts)
    assert "read that you" not in msg.body  # no activity in the facts, so no claim about a post
    assert msg.facts_used == ["name", "company"]


@pytest.mark.parametrize(
    "bad, expected",
    [
        (
            draft(
                body="Hi [First Name], I read your post about dashboards. Best, Alex Morgan, Northwind Analytics team"
            ),
            "placeholder",
        ),
        (
            draft(
                body="Hi Priya, your team cut reporting time by 35% last quarter, impressive work. Best, Alex Morgan "
                "from Northwind Analytics"
            ),
            "number '35%'",
        ),
        (
            draft(
                body="Hi Priya, I saw you speak at SaaStr Europe about warehouse dashboards. Best regards, "
                "Alex Morgan, Northwind Analytics"
            ),
            "SaaStr",
        ),
        (
            draft(
                body="Hi Priya, as we discussed on our call, here is the deck about reporting dashboards. "
                "Best regards, Alex Morgan"
            ),
            "previous interaction",
        ),
        (
            draft(
                body="Hi Priya, congratulations on the promotion! Would you be open to a short chat about reporting? "
                "Alex Morgan"
            ),
            "career event",
        ),
        (
            draft(
                body="Hello there, I read that you wrote about moving weekly warehouse reports to dashboards. Best, "
                "Alex Morgan"
            ),
            "first name",
        ),
        (draft(facts_used=["name", "funding_round"]), "funding_round"),
        (
            draft(
                body="Hi Priya, see https://evil.example/offer for the details about reporting automation. "
                "Best regards, Alex Morgan"
            ),
            "link",
        ),
    ],
)
def test_validator_rejects_invented_or_broken_content(facts: dict, bad: str, expected: str) -> None:
    parsed, problems = parse_draft(bad)
    assert parsed is not None and problems == []
    problems = validate_draft(parsed, facts=facts, sender=SENDER, max_words=130)
    assert any(expected in p for p in problems), problems


def test_validator_accepts_grounded_draft_and_user_instructions(facts: dict) -> None:
    parsed, _ = parse_draft(
        draft(
            body="Hi Priya, I read that you wrote about moving weekly warehouse reports from "
            "spreadsheets to dashboards. Happy to share our Q3 case study if useful. "
            "Best, Alex Morgan"
        )
    )
    assert parsed is not None
    # "Q3" is not a fact, but the user explicitly asked to mention the Q3 case study:
    assert (
        validate_draft(parsed, facts=facts, sender=SENDER, max_words=130, extra_instructions="offer our Q3 case study")
        == []
    )


def test_parser_handles_code_fences_and_rejects_garbage() -> None:
    fenced = "```json\n" + draft() + "\n```"
    assert parse_draft(fenced)[0] is not None
    assert parse_draft("Sure! Here you go:")[1] == ["the reply was not a JSON object"]
    assert "not valid JSON" in parse_draft("{not json}")[1][0]
    assert any("subject" in p for p in parse_draft(json.dumps({"body": "x" * 50}))[1])


def test_length_limit_is_enforced(facts: dict) -> None:
    parsed, _ = parse_draft(draft(body="Hi Priya, " + "really " * 200 + "Best, Alex Morgan"))
    assert parsed is not None
    assert any("words" in p for p in validate_draft(parsed, facts=facts, sender=SENDER, max_words=130))


async def test_agent_regenerates_with_validator_feedback(settings: Settings, facts: dict) -> None:
    invented = draft(body="Hi Priya, congrats on raising your Series B! Best regards, Alex Morgan, Northwind Analytics")
    llm = MockLLMClient([invented])  # first reply invents facts, then the template model answers
    msg = await MessageAgent(llm, settings).generate(facts)
    assert msg.attempts == 2
    feedback = llm.calls[1][-1]["content"]
    assert "rejected by the validator" in feedback and "Series" in feedback


async def test_agent_gives_up_after_bounded_attempts(settings: Settings, facts: dict) -> None:
    bad = draft(body="Hi [Name], we met at the conference. Best, Alex Morgan, Northwind Analytics team lead")
    llm = MockLLMClient([bad, bad, bad, bad])
    with pytest.raises(MessageValidationError) as exc:
        await MessageAgent(llm, settings).generate(facts)
    assert len(llm.calls) == 3
    assert not is_retryable(exc.value)  # content problem: a human should look, not a blind retry
    assert len(exc.value.details["attempts"]) == 3


async def test_llm_outage_propagates_as_retryable(settings: Settings, facts: dict) -> None:
    llm = MockLLMClient([LLMUnavailableError("503 from provider")])
    with pytest.raises(LLMUnavailableError) as exc:
        await MessageAgent(llm, settings).generate(facts)
    assert is_retryable(exc.value)


def test_missing_api_key_falls_back_to_mock() -> None:
    from app.agents.llm import build_llm_client

    assert isinstance(build_llm_client(Settings.from_env({}, env_file=None)), MockLLMClient)
