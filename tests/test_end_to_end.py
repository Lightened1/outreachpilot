"""Full workflow through the API, with real browser automation against the demo portal and the
mock LLM: import -> validate -> research -> generate -> approve/reject -> execute -> results."""

from __future__ import annotations

import time

import pytest

from tests.conftest import needs_api_stack

pytestmark = [pytest.mark.api, pytest.mark.browser, needs_api_stack]
TERMINAL = {"succeeded", "failed", "cancelled"}


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as c:
        yield c


def wait_task(client, task_id: str, timeout: float = 45) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/tasks/{task_id}").json()
        if task["status"] in TERMINAL:
            return task
        time.sleep(0.1)
    raise AssertionError(f"task {task_id} did not finish: {task}")


def create(client, settings, person_id: int, name: str) -> int:
    r = client.post(
        "/prospects",
        json={"name": name, "company": "Demo Co", "source_url": f"{settings.demo_portal_url}/people/{person_id}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_full_workflow_with_rejection_and_approval(client, settings, demo) -> None:
    pid = create(client, settings, 1, "Priya Raman")

    task = client.post(f"/prospects/{pid}/research").json()
    assert task["status"] == "queued"
    assert client.get(f"/prospects/{pid}").json()["status"] in ("researching", "researched")
    assert wait_task(client, task["id"])["status"] == "succeeded"
    prospect = client.get(f"/prospects/{pid}").json()
    assert prospect["status"] == "researched"
    assert prospect["research_data"]["company"] == "Bluefin Logistics"

    task = client.post(f"/prospects/{pid}/generate-message", json={"tone": "friendly"}).json()
    assert wait_task(client, task["id"])["status"] == "succeeded"
    prospect = client.get(f"/prospects/{pid}").json()
    assert prospect["status"] == "pending_approval"
    first = prospect["latest_message"]
    assert first["status"] == "pending_approval" and first["tone"] == "friendly" and "Priya" in first["body"]

    # Nothing is sent while the message waits for a human.
    assert client.post(f"/prospects/{pid}/execute").status_code == 409
    assert demo.outbox() == []

    r = client.post(
        f"/prospects/{pid}/approve", json={"decision": "reject", "reviewer": "alex", "reason": "Too informal"}
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    task = client.post(f"/prospects/{pid}/generate-message", json={"tone": "concise"}).json()
    wait_task(client, task["id"])
    second = client.get(f"/prospects/{pid}").json()["latest_message"]
    assert second["version"] == 2 and second["tone"] == "concise"

    r = client.post(f"/prospects/{pid}/approve", json={"decision": "approve", "reviewer": "alex"})
    assert r.json()["status"] == "approved" and r.json()["latest_message"]["reviewer"] == "alex"

    task = client.post(f"/prospects/{pid}/execute").json()
    done = wait_task(client, task["id"])
    assert done["status"] == "succeeded" and done["result"]["confirmation_id"].startswith("MSG-")

    prospect = client.get(f"/prospects/{pid}").json()
    assert prospect["status"] == "completed"
    assert prospect["latest_message"]["status"] == "sent"
    outbox = demo.outbox()
    assert len(outbox) == 1 and outbox[0]["subject"] == second["subject"]
    assert outbox[0]["idempotency_key"] == f"outreachpilot-message-{second['id']}"

    events = [e["event_type"] for e in client.get(f"/prospects/{pid}/events").json()]
    for expected in (
        "prospect.imported",
        "prospect.validated",
        "research.requested",
        "research.completed",
        "message.generated",
        "message.rejected",
        "message.approved",
        "execute.requested",
        "message.sent",
    ):
        assert expected in events
    assert len(client.get(f"/prospects/{pid}/tasks").json()) == 4


def test_blocked_site_stops_and_is_reported(client, settings) -> None:
    pid = create(client, settings, 5, "Hana Sato")
    task = wait_task(client, client.post(f"/prospects/{pid}/research").json()["id"])
    assert task["status"] == "failed" and task["attempts"] == 1  # not retried
    assert task["error"]["code"] == "access_blocked"
    prospect = client.get(f"/prospects/{pid}").json()
    assert prospect["status"] == "blocked" and prospect["last_error"]["stage"] == "research"


def test_transient_errors_are_retried(client, settings) -> None:
    pid = create(client, settings, 6, "Daniel Kim")
    task = wait_task(client, client.post(f"/prospects/{pid}/research").json()["id"])
    assert task["status"] == "succeeded" and task["attempts"] == 3
    assert [e["status"] for e in task["history"]].count("retrying") == 2
    assert client.get(f"/prospects/{pid}").json()["status"] == "researched"


def test_cancel_running_task(client, settings) -> None:
    pid = create(client, settings, 4, "Tomás Ferreira")
    task = client.post(f"/prospects/{pid}/research").json()
    time.sleep(0.3)
    r = client.post(f"/tasks/{task['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    prospect = client.get(f"/prospects/{pid}").json()
    assert prospect["status"] == "failed" and prospect["last_error"]["code"] == "task_cancelled"
    # The user can retry explicitly afterwards.
    assert client.post(f"/prospects/{pid}/research").status_code == 202


def test_second_research_while_busy_is_refused(client, settings) -> None:
    pid = create(client, settings, 4, "Tomás Ferreira")
    first = client.post(f"/prospects/{pid}/research")
    assert first.status_code == 202
    assert client.post(f"/prospects/{pid}/research").status_code == 409
    client.post(f"/tasks/{first.json()['id']}/cancel")
