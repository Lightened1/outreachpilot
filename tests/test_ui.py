"""Operator console tests: the real static UI in Chromium, served with the production CSP.

API calls are answered by an in-memory fake (Playwright request interception) that follows the same
JSON contract and the real workflow transition table, so the UI is tested without a database.
Set UI_SCREENSHOT_DIR to save screenshots of each state.
"""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, Route, expect, sync_playwright
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.core.enums import ProspectStatus
from app.core.workflow import allowed_next

pytestmark = pytest.mark.browser

UI_DIR = Path(__file__).resolve().parents[1] / "app" / "ui"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
NOW = "2026-11-18T10:00:00+00:00"

PRIYA_FACTS = {
    "name": "Priya Raman",
    "headline": "Head of Operations",
    "company": "Bluefin Logistics",
    "location": "Rotterdam, Netherlands",
    "about": "Leads a 40-person operations team running cross-dock warehouses.",
    "recent_activity": ["Wrote about moving weekly warehouse reports from spreadsheets to dashboards"],
    "skills": ["Operations management", "Lean"],
    "profile_url": "http://127.0.0.1:8010/people/1",
}


def _prospect(pid: int, name: str, status: str, **extra: Any) -> dict[str, Any]:
    base = {
        "id": pid,
        "name": name,
        "job_title": extra.pop("job_title", None),
        "company": extra.pop("company", None),
        "source_url": f"http://127.0.0.1:8010/people/{pid}",
        "notes": None,
        "status": status,
        "research_data": None,
        "last_error": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(extra)
    return base


def _message(mid: int, pid: int, version: int, status: str, body: str, **extra: Any) -> dict[str, Any]:
    msg = {
        "id": mid,
        "version": version,
        "tone": "professional",
        "subject": "Question about Bluefin Logistics",
        "body": body,
        "facts_used": ["name", "recent_activity", "company"],
        "model": "mock-llm",
        "generation_attempts": 1,
        "status": status,
        "reviewer": None,
        "review_reason": None,
        "reviewed_at": None,
        "sent_at": None,
        "execution_result": None,
        "created_at": NOW,
    }
    msg.update(extra)
    msg["_prospect"] = pid
    return msg


class FakeApi:
    def __init__(self, require_key: str | None = None) -> None:
        self.require_key = require_key
        self.requests: list[tuple[str, str, dict[str, Any] | None, dict[str, str]]] = []
        self.prospects = {
            1: _prospect(
                1,
                "Priya Raman",
                "pending_approval",
                job_title="Head of Operations",
                company="Bluefin Logistics",
                research_data=copy.deepcopy(PRIYA_FACTS),
            ),
            2: _prospect(2, "Marcus Oyelaran", "completed", job_title="CTO", company="Tidewater Health"),
            5: _prospect(
                5,
                "Hana Sato",
                "blocked",
                company="Kestrel Energy",
                last_error={
                    "stage": "research",
                    "code": "access_blocked",
                    "task_id": "t5",
                    "message": "The site is restricting access (rate limit or verification challenge). The automation "
                    "stopped and did not try to bypass it.",
                    "details": {"status": 429},
                },
            ),
            4: _prospect(4, "Tomás Ferreira", "validated", company="Ferreira Freight"),
        }
        body1 = (
            "Hi Priya,\n\nI noticed your team at Bluefin Logistics. Would you be open to a call?\n\n"
            "Best regards,\nAlex Morgan"
        )
        body2 = (
            "Hi Priya,\n\nI read that you wrote about moving weekly warehouse reports from spreadsheets to "
            "dashboards. It sounds relevant to the work your team does at Bluefin Logistics.\n\nWould you be "
            "open to a short call to compare notes?\n\nBest regards,\nAlex Morgan"
        )
        self.messages = [
            _message(12, 1, 2, "pending_approval", body2),
            _message(11, 1, 1, "rejected", body1, reviewer="ami", review_reason="Too generic"),
            _message(
                21,
                2,
                1,
                "sent",
                body2.replace("Priya", "Marcus"),
                sent_at=NOW,
                reviewer="ami",
                execution_result={
                    "confirmation_id": "MSG-1A2B3C4D",
                    "recipient": "Marcus Oyelaran",
                    "sent_subject": "Hello",
                    "page_url": "http://127.0.0.1:8010/sent/1",
                    "screenshot": None,
                    "duplicate": False,
                },
            ),
        ]
        self.events = {
            1: [
                {
                    "id": 1,
                    "task_id": None,
                    "event_type": "prospect.imported",
                    "actor": "api",
                    "detail": {},
                    "created_at": NOW,
                },
                {
                    "id": 2,
                    "task_id": None,
                    "event_type": "message.rejected",
                    "actor": "ami",
                    "detail": {"version": 1, "reason": "Too generic"},
                    "created_at": NOW,
                },
            ]
        }
        self.tasks: dict[str, dict[str, Any]] = {}

    # -- helpers --------------------------------------------------------------------------------
    def detail(self, pid: int) -> dict[str, Any]:
        p = self.prospects[pid]
        msgs = self.msgs(pid)
        return {
            **p,
            "latest_message": msgs[0] if msgs else None,
            "allowed_next_statuses": allowed_next(ProspectStatus(p["status"])),
        }

    def msgs(self, pid: int) -> list[dict[str, Any]]:
        found = [m for m in self.messages if m["_prospect"] == pid]
        return [{k: v for k, v in m.items() if k != "_prospect"} for m in sorted(found, key=lambda m: -m["version"])]

    def task(self, pid: int, kind: str) -> dict[str, Any]:
        tid = f"task{len(self.tasks) + 1}"
        self.tasks[tid] = {
            "id": tid,
            "type": kind,
            "prospect_id": pid,
            "status": "queued",
            "attempts": 0,
            "max_attempts": 3,
            "created_at": NOW,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "history": [],
            "_polls": 0,
        }
        return self.public(self.tasks[tid])

    @staticmethod
    def public(task: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in task.items() if not k.startswith("_")}

    # -- request handling -------------------------------------------------------------------------
    def handle(self, route: Route) -> None:
        req = route.request
        url = req.url.split("://", 1)[1].split("/", 1)[1]
        path, _, query = ("/" + url).partition("?")
        body = json.loads(req.post_data) if req.post_data else None
        self.requests.append((req.method, path, body, req.headers))
        if self.require_key and path != "/health" and req.headers.get("x-api-key") != self.require_key:
            return self.reply(
                route,
                401,
                {"error": {"code": "unauthorized", "message": "A valid X-API-Key header is required.", "details": {}}},
            )
        status, payload = self.dispatch(req.method, path, query, body)
        return self.reply(route, status, payload)

    @staticmethod
    def reply(route: Route, status: int, payload: Any) -> None:
        route.fulfill(
            status=status, content_type="application/json", body="" if payload is None else json.dumps(payload)
        )

    def dispatch(self, method: str, path: str, query: str, body: Any) -> tuple[int, Any]:
        params: dict[str, list[str]] = {}
        for pair in filter(None, query.split("&")):
            k, _, v = pair.partition("=")
            params.setdefault(k, []).append(v)
        if path == "/health":
            return 200, {
                "status": "ok",
                "app": "OutreachPilot",
                "version": "1.0.0",
                "database": "ok",
                "config": {"llm_mode": "mock", "llm_model": "mock", "demo_portal_url": "http://127.0.0.1:8010"},
                "tasks": {},
                "time": NOW,
            }
        if path == "/stats":
            counts = {s.value: 0 for s in ProspectStatus}
            for p in self.prospects.values():
                counts[p["status"]] += 1
            return 200, {"total": len(self.prospects), "by_status": counts, "tasks": {}}
        if path == "/prospects" and method == "GET":
            items = [p for p in self.prospects.values() if not params.get("status") or p["status"] in params["status"]]
            return 200, {"items": items, "total": len(items), "limit": 100, "offset": 0}
        if path == "/prospects/import" and method == "POST":
            lines = [ln for ln in body["csv"].splitlines() if ln.strip()][1:]
            rows = [
                {
                    "row": i + 2,
                    "status": "created" if "," in ln else "invalid",
                    "prospect_id": None,
                    "name": ln.split(",")[0],
                    "errors": [] if "," in ln else ["source_url: required"],
                }
                for i, ln in enumerate(lines)
            ]
            created = sum(r["status"] == "created" for r in rows)
            return 200, {"created": created, "duplicates": 0, "invalid": len(rows) - created, "rows": rows}
        m = re.fullmatch(r"/prospects/(\d+)(/[\w-]+)?", path)
        if m:
            pid, sub = int(m.group(1)), m.group(2) or ""
            if pid not in self.prospects:
                return 404, {"error": {"code": "not_found", "message": "Not found", "details": {}}}
            p = self.prospects[pid]
            if method == "GET" and not sub:
                return 200, self.detail(pid)
            if method == "DELETE" and not sub:
                del self.prospects[pid]
                return 204, None
            if sub == "/messages":
                return 200, self.msgs(pid)
            if sub == "/events":
                return 200, self.events.get(pid, [])
            if sub == "/tasks":
                return 200, [self.public(t) for t in self.tasks.values() if t["prospect_id"] == pid]
            if sub == "/approve":
                latest = next(m for m in self.messages if m["_prospect"] == pid and m["status"] == "pending_approval")
                latest["status"] = "approved" if body["decision"] == "approve" else "rejected"
                latest["reviewer"] = body["reviewer"]
                latest["review_reason"] = body.get("reason")
                p["status"] = latest["status"]
                return 200, self.detail(pid)
            if sub == "/execute":
                p["status"] = "executing"
                return 202, self.task(pid, "execute")
        m = re.fullmatch(r"/tasks/(\w+)", path)
        if m:
            t = self.tasks[m.group(1)]
            t["_polls"] += 1
            if t["_polls"] >= 2:
                t.update(status="succeeded", attempts=1, finished_at=NOW)
                p = self.prospects[t["prospect_id"]]
                p["status"] = "completed"
                for msg in self.messages:
                    if msg["_prospect"] == p["id"] and msg["status"] == "approved":
                        msg.update(
                            status="sent",
                            sent_at=NOW,
                            execution_result={
                                "confirmation_id": "MSG-9F8E7D6C",
                                "recipient": p["name"],
                                "sent_subject": msg["subject"],
                                "page_url": "http://127.0.0.1:8010/sent/MSG-9F8E7D6C",
                                "screenshot": None,
                                "duplicate": False,
                            },
                        )
            else:
                t.update(status="running", attempts=1)
            return 200, self.public(t)
        return 404, {"error": {"code": "not_found", "message": f"No fake route for {method} {path}", "details": {}}}


class _CSP(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        return response


@pytest.fixture(scope="module")
def ui_url() -> Iterator[str]:
    app = Starlette(routes=[Mount("/ui", StaticFiles(directory=UI_DIR, html=True))], middleware=[Middleware(_CSP)])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def browser_page() -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        yield page
        browser.close()


def open_console(page: Page, ui_url: str, fake: FakeApi, path: str = "/ui/") -> list[str]:
    problems: list[str] = []
    page.on("console", lambda msg: problems.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: problems.append(str(exc)))
    page.route(re.compile(r".*/(health|stats|prospects|tasks)(/.*)?(\?.*)?$"), fake.handle)
    page.goto(ui_url + path)
    return problems


def shot(page: Page, name: str) -> None:
    folder = os.getenv("UI_SCREENSHOT_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        # the detail pane scrolls on its own; grow the viewport so the whole page is captured
        height = page.evaluate("Math.max(document.querySelector('#detail').scrollHeight + 56, innerHeight)")
        page.set_viewport_size({"width": page.viewport_size["width"], "height": int(height)})
        page.screenshot(path=str(Path(folder) / f"{name}.png"))


def test_console_opens_on_the_review_queue(ui_url: str, browser_page: Page) -> None:
    fake = FakeApi()
    problems = open_console(browser_page, ui_url, fake)
    page = browser_page
    expect(page.get_by_role("tab", name=re.compile("Needs review"))).to_have_attribute("aria-selected", "true")
    expect(page.locator(".pitem")).to_have_count(1)
    expect(page.locator("#env")).to_contain_text("offline mock model")
    expect(page.get_by_role("link", name="Practice website")).to_have_attribute("href", "http://127.0.0.1:8010")
    page.get_by_role("tab", name=re.compile("^All")).click()
    expect(page.locator(".pitem")).to_have_count(4)
    shot(page, "01-list")
    assert problems == []  # no CSP violations or script errors


def test_review_highlights_evidence_and_records_the_decision(ui_url: str, browser_page: Page) -> None:
    fake = FakeApi()
    problems = open_console(browser_page, ui_url, fake, "/ui/#/review/1")
    page = browser_page
    expect(page.locator("#p-name")).to_have_text("Priya Raman")
    marks = page.locator("mark.ev[data-fact]")
    expect(marks.first).to_be_visible()
    assert "recent_activity" in [marks.nth(i).get_attribute("data-fact") for i in range(marks.count())]
    marks.filter(has_text="Bluefin Logistics").first.hover()
    expect(page.locator('.fact[data-key="company"]')).to_have_class(re.compile("hot"))
    shot(page, "02-review")

    # the earlier version can be compared word by word
    page.get_by_label(re.compile("Show changes from version 1")).check()
    expect(page.locator(".body.diff ins").first).to_be_visible()
    page.get_by_label(re.compile("Show changes from version 1")).uncheck()

    # rejecting needs a reason; approving needs a reviewer name
    page.get_by_role("button", name="Reject", exact=True).click()
    page.get_by_role("button", name="Reject draft").click()
    expect(page.locator(".toast.bad")).to_contain_text("reason")
    page.get_by_role("button", name="Keep reviewing").click()
    page.get_by_role("button", name="Approve").click()
    expect(page.locator(".toast.bad").last).to_contain_text("your name")
    page.get_by_label("Your name").fill("ami")
    page.get_by_role("button", name="Approve").click()
    expect(page.locator(".p-head .pill")).to_have_text("Approved")
    approve = [r for r in fake.requests if r[1] == "/prospects/1/approve"][-1]
    assert approve[2] == {"decision": "approve", "reviewer": "ami"}

    # sending asks for confirmation, then follows the task to completion
    page.get_by_role("button", name="Send message").click()
    page.locator("#confirm-ok").click()
    expect(page.locator(".p-head .pill")).to_have_text("Sent", timeout=10_000)
    expect(page.get_by_text("Confirmation MSG-9F8E7D6C")).to_be_visible()
    shot(page, "03-sent")
    assert problems == []


def test_blocked_prospect_explains_that_automation_stopped(ui_url: str, browser_page: Page) -> None:
    fake = FakeApi()
    open_console(browser_page, ui_url, fake, "/ui/#/problems/5")
    page = browser_page
    expect(page.locator(".notice.warn h3")).to_have_text("The site restricted access")
    expect(page.locator(".notice.warn")).to_contain_text("did not try to bypass it")
    expect(page.locator(".step.warn")).to_have_text("Researched")
    expect(page.get_by_role("button", name="Try research again")).to_be_visible()
    shot(page, "04-blocked")
    page.evaluate("location.hash = '#/all/4'")  # links and back/forward change only the hash
    expect(page.locator("#p-name")).to_have_text("Tomás Ferreira")
    expect(page.get_by_role("button", name="Research profile")).to_be_visible()


def test_api_key_is_requested_and_then_sent(ui_url: str, browser_page: Page) -> None:
    fake = FakeApi(require_key="k" * 24)
    open_console(browser_page, ui_url, fake)
    page = browser_page
    expect(page.locator("#key-dialog")).to_be_visible()
    page.get_by_role("textbox", name="API key").fill("k" * 24)
    page.get_by_role("button", name="Save key").click()
    expect(page.locator(".pitem").first).to_be_visible()
    assert fake.requests[-1][3].get("x-api-key") == "k" * 24


def test_csv_import_shows_a_per_row_report(ui_url: str, browser_page: Page) -> None:
    fake = FakeApi()
    open_console(browser_page, ui_url, fake)
    page = browser_page
    page.get_by_role("button", name="Import CSV").click()
    page.get_by_label("Or paste CSV").fill("name,company\nElena Varga,Kestrel Energy\nBroken row\n")
    page.get_by_role("button", name="Import", exact=True).click()
    expect(page.locator(".import-result .summary")).to_have_text("1 added, 0 already existed, 1 not valid.")
    expect(page.locator(".import-result tbody tr")).to_have_count(2)
    shot(page, "05-import")


def test_layout_works_on_a_phone(ui_url: str, browser_page: Page) -> None:
    fake = FakeApi()
    page = browser_page
    page.set_viewport_size({"width": 390, "height": 844})
    open_console(page, ui_url, fake)
    page.locator(".pitem").first.click()
    expect(page.locator("#p-name")).to_be_visible()
    expect(page.locator(".rail")).to_be_hidden()
    shot(page, "06-phone")
    page.get_by_role("button", name="Back to list").click()
    expect(page.locator(".rail")).to_be_visible()
