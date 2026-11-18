"""Research -> generate -> human approval -> execute, in one terminal session (no API or database).

Useful for a quick look at the agent and the approval gate. The full workflow with persistence,
statuses, audit events and background tasks runs through the FastAPI service (see README).

    python -m demo.pipeline_demo                 # asks you to approve the draft
    python -m demo.pipeline_demo --tone friendly
    python -m demo.pipeline_demo --show-hallucination-guard
"""

from __future__ import annotations

import argparse
import asyncio
import json
import textwrap

from app.agents.llm import build_llm_client
from app.agents.message_agent import MessageAgent, build_facts
from app.agents.mock_llm import MockLLMClient
from app.automation.browser import BrowserEngine
from app.automation.portal import DemoPortalClient
from app.config import Settings
from app.core.enums import Tone
from app.core.errors import AppError
from app.core.logging import configure_logging
from demo.browser_demo import start_portal


def box(title: str, text: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 60 - len(title)))
    print(text)


async def show_guard(settings: Settings) -> None:
    """Script the mock model to invent a statistic first, then answer correctly on the retry."""
    invented = json.dumps(
        {
            "subject": "Quick idea for Bluefin",
            "body": "Hi Priya, I saw Bluefin Logistics cut reporting time by 73% last quarter after your Gartner "
            "award. Worth a chat?\n\nAlex Morgan\nSolutions Engineer, Northwind Analytics",
            "facts_used": ["company"],
        }
    )
    llm = MockLLMClient(script=[invented])  # first call scripted, later calls use the template
    agent = MessageAgent(llm, settings)
    facts = {
        "name": "Priya Raman",
        "job_title": "Head of Operations",
        "company": "Bluefin Logistics",
        "recent_activity": ["Wrote about moving weekly warehouse reports from spreadsheets to dashboards"],
    }
    message = await agent.generate(facts, tone=Tone.CONCISE)
    box("First draft (rejected by the validator)", textwrap.fill(json.loads(invented)["body"], 90))
    box(f"Accepted draft after {message.attempts} attempts", message.body)


async def run(settings: Settings, tone: Tone, auto: str | None) -> None:
    base = settings.demo_portal_url
    llm = build_llm_client(settings)
    agent = MessageAgent(llm, settings)
    async with BrowserEngine(settings) as engine:
        portal = DemoPortalClient(engine, settings)
        prospect = {
            "name": "Priya Raman",
            "job_title": "Head of Operations",
            "company": "Bluefin Logistics",
            "source_url": f"{base}/people/1",
        }

        print("[1/4] Researching the prospect in the browser ...")
        research = await portal.research(prospect["source_url"], expected_name=prospect["name"])
        facts = build_facts(prospect, research.facts())
        box("Facts the model is allowed to use", json.dumps(facts, indent=2))

        print(f"\n[2/4] Generating a {tone.value} message with {type(llm).__name__} ...")
        message = await agent.generate(facts, tone=tone)
        box(f"Draft (subject: {message.subject})", message.body)
        print(f"\nfacts_used={message.facts_used} attempts={message.attempts} model={message.model}")

        print("\n[3/4] Human approval: nothing is sent without it.")
        decision = auto or (await asyncio.to_thread(input, "Approve this message? [y/N] ")).strip().lower()
        if decision not in ("y", "yes", "approve"):
            print("Rejected. Nothing was sent.")
            await llm.aclose()
            return

        print("\n[4/4] Sending through the portal's compose form ...")
        result = await portal.send_message(
            prospect["source_url"],
            subject=message.subject,
            body=message.body,
            idempotency_key="pipeline-demo-priya",
            expected_name=prospect["name"],
        )
        box("Execution result", json.dumps(result.model_dump(), indent=2))
    await llm.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tone", choices=[t.value for t in Tone], default=Tone.PROFESSIONAL.value)
    parser.add_argument("--yes", action="store_const", const="y", dest="auto", help="approve without prompting")
    parser.add_argument("--no", action="store_const", const="n", dest="auto", help="reject without prompting")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--show-hallucination-guard", action="store_true", help="only show the validator rejecting an invented claim"
    )
    args = parser.parse_args()

    configure_logging("WARNING")
    url, server = start_portal()
    settings = Settings.from_env(demo_portal_url=url, browser_headless=not args.headed)
    try:
        if args.show_hallucination_guard:
            asyncio.run(show_guard(settings))
        else:
            asyncio.run(run(settings, Tone(args.tone), args.auto))
    except AppError as exc:
        print(f"\nStopped: {exc.code}: {exc.message}")
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
