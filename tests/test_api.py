"""REST API: status codes, validation errors, duplicate handling and workflow guards."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from tests.conftest import needs_api_stack

pytestmark = [pytest.mark.api, needs_api_stack]


@pytest.fixture
def client(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as c:
        yield c


def new_prospect(client, **kw):
    body = {
        "name": "Priya Raman",
        "job_title": "Head of Operations",
        "company": "Bluefin Logistics",
        "source_url": f"{client.app.state.container.settings.demo_portal_url}/people/1",
    }
    body.update(kw)
    return client.post("/prospects", json=body)


def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["config"]["llm_mode"] == "mock"
    assert "openai_api_key" not in r.text and "password" not in r.text


def test_openapi_docs_are_served(client) -> None:
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/health",
        "/prospects",
        "/prospects/{prospect_id}",
        "/prospects/{prospect_id}/research",
        "/prospects/{prospect_id}/generate-message",
        "/prospects/{prospect_id}/approve",
        "/prospects/{prospect_id}/execute",
        "/tasks/{task_id}",
    ):
        assert path in paths


def test_create_get_list_and_patch(client) -> None:
    r = new_prospect(client)
    assert r.status_code == 201
    created = r.json()
    assert created["status"] == "validated" and created["allowed_next_statuses"] == ["researching"]
    pid = created["id"]
    assert client.get(f"/prospects/{pid}").json()["name"] == "Priya Raman"
    listing = client.get("/prospects", params={"q": "bluefin"}).json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == pid
    r = client.patch(f"/prospects/{pid}", json={"notes": "Prefers e-mail"})
    assert r.status_code == 200 and r.json()["notes"] == "Prefers e-mail"
    events = client.get(f"/prospects/{pid}/events").json()
    assert [e["event_type"] for e in events] == ["prospect.imported", "prospect.validated", "prospect.updated"]


def test_duplicate_returns_409_with_existing_id(client) -> None:
    first = new_prospect(client).json()
    r = new_prospect(client, source_url=first["source_url"] + "/", name="Priya R. Raman")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_prospect"
    assert r.json()["error"]["details"]["existing_id"] == first["id"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "X", "company": "Y"},
        {"name": "Ana Lima"},
        {"name": "Ana Lima", "source_url": "javascript:alert(1)"},
        {"company": "Only Company"},
    ],
)
def test_invalid_input_returns_422(client, body) -> None:
    r = client.post("/prospects", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "request_validation_error"


def test_status_cannot_be_patched(client) -> None:
    pid = new_prospect(client).json()["id"]
    r = client.patch(f"/prospects/{pid}", json={"status": "completed"})
    assert r.status_code == 422


def test_not_found(client) -> None:
    assert client.get("/prospects/999").status_code == 404
    assert client.get("/tasks/doesnotexist").json()["error"]["code"] == "not_found"


def test_workflow_guards(client) -> None:
    pid = new_prospect(client).json()["id"]
    r = client.post(f"/prospects/{pid}/generate-message", json={"tone": "friendly"})
    assert r.status_code == 409  # validated -> generating is not allowed: research first
    r = client.post(f"/prospects/{pid}/execute")
    assert r.status_code == 409 and "approved" in r.json()["error"]["message"]
    r = client.post(f"/prospects/{pid}/approve", json={"decision": "approve", "reviewer": "alex"})
    assert r.status_code == 409
    r = client.post(f"/prospects/{pid}/approve", json={"decision": "reject", "reviewer": "alex"})
    assert r.status_code == 422  # a rejection needs a reason
    r = client.post(f"/prospects/{pid}/generate-message", json={"tone": "sarcastic"})
    assert r.status_code == 422


def test_research_needs_an_allowed_url(client) -> None:
    no_url = client.post("/prospects", json={"name": "Ana Lima", "company": "Kestrel"}).json()
    r = client.post(f"/prospects/{no_url['id']}/research")
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed"
    external = new_prospect(client, name="Some One", source_url="https://www.linkedin.com/in/someone").json()
    r = client.post(f"/prospects/{external['id']}/research")
    assert r.status_code == 403 and r.json()["error"]["code"] == "host_not_allowed"
    assert client.get(f"/prospects/{external['id']}").json()["status"] == "validated"  # nothing started


def test_delete_removes_the_prospect_and_its_history(client) -> None:
    pid = new_prospect(client).json()["id"]
    assert client.delete(f"/prospects/{pid}").status_code == 204
    assert client.get(f"/prospects/{pid}").status_code == 404
    assert client.get(f"/prospects/{pid}/events").status_code == 404
    assert client.delete(f"/prospects/{pid}").status_code == 404


def test_messages_endpoint_lists_versions(client) -> None:
    pid = new_prospect(client).json()["id"]
    assert client.get(f"/prospects/{pid}/messages").json() == []


def test_csv_import_reports_each_row(client) -> None:
    portal = client.app.state.container.settings.demo_portal_url
    csv_text = (
        "Name,Title,Company,Profile URL,Ignored\n"
        f"Priya Raman,Head of Operations,Bluefin Logistics,{portal}/people/1,x\n"
        f"Priya Raman,Head of Operations,Bluefin Logistics,{portal}/people/1/,x\n"
        "R2-D2,Droid,Rebellion,,x\n"
        "\n"
        "Marcus Oyelaran,CTO,Tidewater Health,,\n"
    )
    r = client.post("/prospects/import", json={"csv": csv_text})
    assert r.status_code == 200, r.text
    report = r.json()
    assert (report["created"], report["duplicates"], report["invalid"]) == (2, 1, 1)
    assert [row["status"] for row in report["rows"]] == ["created", "duplicate", "invalid", "created"]
    assert report["rows"][1]["prospect_id"] == report["rows"][0]["prospect_id"]
    assert client.get("/prospects").json()["total"] == 2


def test_csv_without_name_column_is_rejected(client) -> None:
    r = client.post("/prospects/import", json={"csv": "company,url\nAcme,http://x\n"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_failed"


def test_stats_and_multi_status_filter(client) -> None:
    new_prospect(client)
    new_prospect(client, name="Marcus Oyelaran", source_url=None, company="Tidewater Health")
    stats = client.get("/stats").json()
    assert stats["total"] == 2 and stats["by_status"]["validated"] == 2 and stats["by_status"]["failed"] == 0
    r = client.get("/prospects", params=[("status", "validated"), ("status", "failed"), ("sort", "-id")])
    assert [p["name"] for p in r.json()["items"]] == ["Marcus Oyelaran", "Priya Raman"]


def test_console_and_security_headers(client) -> None:
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200 and "OutreachPilot" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/ui/app.js").status_code == 200
    api = client.get("/health", headers={"X-Request-ID": "req-123"})
    assert api.headers["x-request-id"] == "req-123"
    assert api.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in api.headers  # keeps Swagger UI working at /docs


def test_api_key_is_enforced_when_configured(settings) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    key = "k" * 32
    with TestClient(create_app(settings.model_copy(update={"api_key": SecretStr(key)}))) as c:
        assert c.get("/health").status_code == 200  # health stays public for probes
        r = c.get("/prospects")
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
        assert c.get("/prospects", headers={"X-API-Key": "wrong" * 8}).status_code == 401
        assert c.get("/prospects", headers={"X-API-Key": key}).status_code == 200
        assert c.get("/stats", headers={"X-API-Key": key}).status_code == 200
        assert c.get("/ui/").status_code == 200  # the console itself loads and then asks for the key
