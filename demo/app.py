"""Local demo portal: a small prospect-management web UI used as the automation target.

It is deliberately a separate, tiny Starlette app (Starlette is the framework FastAPI is built on),
so the browser automation drives a real web server over HTTP exactly as it would a third-party
site, without needing any external account.

Features: login with a session cookie, a searchable people list, profile pages, a message
compose form with idempotency keys, a robots.txt, and pages that reproduce slow responses,
transient 503s and an access-blocked challenge.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from demo.data import PEOPLE, PEOPLE_BY_ID, Person

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SESSION_COOKIE = "demo_session"
ROBOTS_TXT = "User-agent: *\nDisallow: /private/\nDisallow: /admin/\n"


@dataclass
class DemoConfig:
    username: str = field(default_factory=lambda: os.getenv("DEMO_USERNAME", "demo"))
    password: str = field(default_factory=lambda: os.getenv("DEMO_PASSWORD", "demo-password"))
    slow_delay_s: float = float(os.getenv("DEMO_SLOW_DELAY_S", "20"))
    flaky_failures: int = 2


@dataclass
class SentMessage:
    id: str
    person_id: int
    recipient: str
    subject: str
    body: str
    idempotency_key: str
    sent_at: str


@dataclass
class DemoState:
    sessions: set[str] = field(default_factory=set)
    outbox: list[SentMessage] = field(default_factory=list)
    flaky_hits: dict[int, int] = field(default_factory=dict)
    logins: int = 0

    def by_key(self, key: str) -> SentMessage | None:
        return next((m for m in self.outbox if m.idempotency_key == key), None)


def _state(request: Request) -> DemoState:
    return request.app.state.demo_state


def _config(request: Request) -> DemoConfig:
    return request.app.state.demo_config


def _logged_in(request: Request) -> bool:
    return request.cookies.get(SESSION_COOKIE, "") in _state(request).sessions


def _login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


async def _form(request: Request) -> dict[str, str]:
    """Parse an urlencoded form body (no python-multipart dependency needed)."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _person_or_404(request: Request, *, private: bool = False) -> Person | Response:
    try:
        person = PEOPLE_BY_ID[int(request.path_params["person_id"])]
    except (KeyError, ValueError):
        return TEMPLATES.TemplateResponse(request, "not_found.html", status_code=404)
    if person.private != private:
        return TEMPLATES.TemplateResponse(request, "not_found.html", status_code=404)
    return person


async def _apply_behavior(request: Request, person: Person) -> Response | None:
    """Reproduce slow, flaky and blocked behaviour for the demo profiles that have it."""
    cfg, state = _config(request), _state(request)
    if person.behavior == "slow":
        await asyncio.sleep(cfg.slow_delay_s)
    elif person.behavior == "flaky":
        hits = state.flaky_hits.get(person.id, 0)
        state.flaky_hits[person.id] = hits + 1
        if hits < cfg.flaky_failures:
            return PlainTextResponse("503 Service Temporarily Unavailable", status_code=503)
    elif person.behavior == "blocked":
        return TEMPLATES.TemplateResponse(request, "blocked.html", status_code=429)
    return None


# ---- pages ------------------------------------------------------------------------------------
async def home(request: Request) -> Response:
    return RedirectResponse("/people", status_code=303)


async def robots(request: Request) -> Response:
    return PlainTextResponse(ROBOTS_TXT)


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def _login_context(request: Request, nxt: str, error: str | None) -> dict[str, object]:
    cfg = _config(request)
    # Only reveal the stock demo login; a custom DEMO_PASSWORD is never shown.
    shown = cfg.username if (cfg.username, cfg.password) == ("demo", "demo-password") else None
    return {"next": nxt, "error": error, "demo_username": shown, "demo_password": "demo-password" if shown else None}


async def login_page(request: Request) -> Response:
    nxt = request.query_params.get("next", "/people")
    return TEMPLATES.TemplateResponse(request, "login.html", _login_context(request, nxt, None))


async def login_submit(request: Request) -> Response:
    form, cfg = await _form(request), _config(request)
    nxt = form.get("next") or "/people"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/people"
    if form.get("username") == cfg.username and secrets.compare_digest(form.get("password", ""), cfg.password):
        token = secrets.token_urlsafe(24)
        state = _state(request)
        state.sessions.add(token)
        state.logins += 1
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
        return resp
    return TEMPLATES.TemplateResponse(
        request, "login.html", _login_context(request, nxt, "Invalid username or password."), status_code=401
    )


async def logout(request: Request) -> Response:
    _state(request).sessions.discard(request.cookies.get(SESSION_COOKIE, ""))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


async def people_list(request: Request) -> Response:
    if not _logged_in(request):
        return _login_redirect(request)
    q = request.query_params.get("q", "").strip().lower()
    people = [
        p
        for p in PEOPLE
        if not p.private and (not q or q in p.name.lower() or q in p.company.lower() or q in p.headline.lower())
    ]
    return TEMPLATES.TemplateResponse(request, "people.html", {"people": people, "q": q})


async def person_page(request: Request, *, private: bool = False) -> Response:
    if not _logged_in(request):
        return _login_redirect(request)
    person = _person_or_404(request, private=private)
    if not isinstance(person, Person):
        return person
    blocked = await _apply_behavior(request, person)
    if blocked is not None:
        return blocked
    return TEMPLATES.TemplateResponse(request, "person.html", {"p": person})


async def private_person_page(request: Request) -> Response:
    return await person_page(request, private=True)


async def compose_page(request: Request) -> Response:
    if not _logged_in(request):
        return _login_redirect(request)
    person = _person_or_404(request)
    if not isinstance(person, Person):
        return person
    blocked = await _apply_behavior(request, person)
    if blocked is not None:
        return blocked
    return TEMPLATES.TemplateResponse(
        request, "compose.html", {"p": person, "error": None, "key": secrets.token_hex(8)}
    )


async def compose_submit(request: Request) -> Response:
    if not _logged_in(request):
        return _login_redirect(request)
    person = _person_or_404(request)
    if not isinstance(person, Person):
        return person
    form, state = await _form(request), _state(request)
    subject, body = form.get("subject", "").strip(), form.get("body", "").strip()
    key = form.get("idempotency_key", "").strip() or secrets.token_hex(8)
    if not subject or not body or len(subject) > 200 or len(body) > 2000:
        return TEMPLATES.TemplateResponse(
            request,
            "compose.html",
            {"p": person, "error": "Subject and message are required (max 200/2000 characters).", "key": key},
            status_code=422,
        )
    existing = state.by_key(key)
    if existing is not None:  # idempotent: re-submitting the same key does not send twice
        return RedirectResponse(f"/sent/{existing.id}?duplicate=1", status_code=303)
    msg = SentMessage(
        id="MSG-" + secrets.token_hex(4).upper(),
        person_id=person.id,
        recipient=person.name,
        subject=subject,
        body=body,
        idempotency_key=key,
        sent_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    state.outbox.append(msg)
    return RedirectResponse(f"/sent/{msg.id}", status_code=303)


async def sent_page(request: Request) -> Response:
    if not _logged_in(request):
        return _login_redirect(request)
    msg = next((m for m in _state(request).outbox if m.id == request.path_params["msg_id"]), None)
    if msg is None:
        return TEMPLATES.TemplateResponse(request, "not_found.html", status_code=404)
    return TEMPLATES.TemplateResponse(
        request, "sent.html", {"m": msg, "duplicate": request.query_params.get("duplicate") == "1"}
    )


# ---- JSON helpers for tests and demos (not used by the automation) ------------------------------
async def api_outbox(request: Request) -> Response:
    return JSONResponse([m.__dict__ for m in _state(request).outbox])


async def api_reset(request: Request) -> Response:
    request.app.state.demo_state = DemoState()
    return JSONResponse({"status": "reset"})


def create_demo_app(config: DemoConfig | None = None) -> Starlette:
    routes = [
        Route("/", home),
        Route("/robots.txt", robots),
        Route("/health", health),
        Route("/login", login_page, methods=["GET"]),
        Route("/login", login_submit, methods=["POST"]),
        Route("/logout", logout),
        Route("/people", people_list),
        Route("/people/{person_id}", person_page),
        Route("/people/{person_id}/message", compose_page, methods=["GET"]),
        Route("/people/{person_id}/message", compose_submit, methods=["POST"]),
        Route("/private/people/{person_id}", private_person_page),
        Route("/sent/{msg_id}", sent_page),
        Route("/api/outbox", api_outbox),
        Route("/api/reset", api_reset, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    app.state.demo_config = config or DemoConfig()
    app.state.demo_state = DemoState()
    return app


app = create_demo_app()
