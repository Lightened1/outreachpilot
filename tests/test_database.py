"""Database operations: prospect persistence, duplicate detection, task store, restart recovery."""

from __future__ import annotations

import pytest

from tests.conftest import needs_api_stack

pytestmark = [pytest.mark.anyio, pytest.mark.api, needs_api_stack]


@pytest.fixture
async def db(settings):
    from app.database import Database

    database = Database(settings.database_url)
    await database.create_all()
    yield database
    await database.dispose()


def payload(**kw):
    from app.schemas import ProspectCreate

    data = {
        "name": "Priya Raman",
        "job_title": "Head of Operations",
        "company": "Bluefin Logistics",
        "source_url": "http://127.0.0.1:8010/people/1",
    }
    data.update(kw)
    return ProspectCreate(**data)


async def test_create_persists_and_validates(db) -> None:
    from app.core.enums import ProspectStatus
    from app.services.prospects import ProspectService

    async with db.session_factory() as s:
        p = await ProspectService(s).create(payload())
        assert p.id and p.status is ProspectStatus.VALIDATED
        events = await ProspectService(s).events(p.id)
        assert [e.event_type for e in events] == ["prospect.imported", "prospect.validated"]
    async with db.session_factory() as s:  # new session: really stored
        stored = await ProspectService(s).get(p.id)
        assert stored.name == "Priya Raman" and stored.dedupe_key == "url:http://127.0.0.1:8010/people/1"


@pytest.mark.parametrize(
    "dupe",
    [
        {"source_url": "HTTP://127.0.0.1:8010/people/1/"},
        {"source_url": "http://127.0.0.1:8010/people/1?ref=list", "name": "Priya R. Raman"},
    ],
)
async def test_duplicate_urls_are_rejected(db, dupe) -> None:
    from app.core.errors import DuplicateProspectError
    from app.services.prospects import ProspectService

    async with db.session_factory() as s:
        first = await ProspectService(s).create(payload())
        with pytest.raises(DuplicateProspectError) as exc:
            await ProspectService(s).create(payload(**dupe))
        assert exc.value.details["existing_id"] == first.id


async def test_duplicate_name_and_company_without_url(db) -> None:
    from app.core.errors import DuplicateProspectError
    from app.services.prospects import ProspectService

    async with db.session_factory() as s:
        await ProspectService(s).create(payload(source_url=None, name="José Núñez", company="ACME"))
        with pytest.raises(DuplicateProspectError):
            await ProspectService(s).create(payload(source_url=None, name="jose nunez", company="acme"))
        other = await ProspectService(s).create(payload(source_url=None, name="José Núñez", company="Globex"))
        assert other.id


async def test_unique_constraint_backs_up_the_check(db) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.models import Prospect

    async with db.session_factory() as s:
        s.add(Prospect(name="A B", company="C", dedupe_key="k"))
        await s.commit()
        s.add(Prospect(name="A B", company="C", dedupe_key="k"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_list_filters_search_and_paginates(db) -> None:
    from app.core.enums import ProspectStatus
    from app.services.prospects import ProspectService

    async with db.session_factory() as s:
        svc = ProspectService(s)
        for i, (name, company) in enumerate([("Ana Lima", "Kestrel"), ("Ben Ode", "Atlas"), ("Cara Diaz", "Kestrel")]):
            await svc.create(payload(name=name, company=company, source_url=f"http://127.0.0.1:8010/people/{i + 10}"))
        items, total = await svc.list(q="kestrel")
        assert total == 2 and [p.name for p in items] == ["Ana Lima", "Cara Diaz"]
        items, total = await svc.list(limit=1, offset=1)
        assert total == 3 and len(items) == 1 and items[0].name == "Ben Ode"
        assert (await svc.list(status=ProspectStatus.COMPLETED))[1] == 0


async def test_update_rechecks_identity(db) -> None:
    from app.core.enums import ProspectStatus
    from app.core.errors import DuplicateProspectError, InvalidTransitionError
    from app.schemas import ProspectUpdate
    from app.services.prospects import ProspectService

    async with db.session_factory() as s:
        svc = ProspectService(s)
        a = await svc.create(payload())
        b = await svc.create(payload(name="Elena Varga", source_url="http://127.0.0.1:8010/people/3"))
        updated = await svc.update(b.id, ProspectUpdate(job_title="Data Platform Lead", notes="met online"))
        assert updated.job_title == "Data Platform Lead"
        with pytest.raises(DuplicateProspectError):
            await svc.update(b.id, ProspectUpdate(source_url=a.source_url))
        b.status = ProspectStatus.RESEARCHING
        await s.commit()
        with pytest.raises(InvalidTransitionError):
            await svc.update(b.id, ProspectUpdate(notes="busy"))


async def test_task_store_roundtrip_and_recovery(db) -> None:
    from app.core.enums import ProspectStatus, TaskStatus
    from app.core.tasks import TaskRecord
    from app.services.prospects import ProspectService
    from app.services.task_store import SqlTaskStore

    store = SqlTaskStore(db.session_factory)
    async with db.session_factory() as s:
        p = await ProspectService(s).create(payload())
        p.status = ProspectStatus.RESEARCHING
        await s.commit()
    rec = TaskRecord(id="t" * 32, type="research", prospect_id=p.id, max_attempts=3)
    rec.add_event(TaskStatus.QUEUED, "queued")
    rec.attempts = 1
    rec.add_event(TaskStatus.RUNNING, "attempt 1 started")
    await store.save(rec)
    loaded = await store.get(rec.id)
    assert loaded is not None and loaded.status is TaskStatus.RUNNING and len(loaded.history) == 2
    assert [r.id for r in await store.list(prospect_id=p.id)] == [rec.id]

    assert await store.recover_interrupted() == 1  # simulated restart
    recovered = await store.get(rec.id)
    assert recovered.status is TaskStatus.FAILED and recovered.error["code"] == "interrupted"
    async with db.session_factory() as s:
        prospect = await ProspectService(s).get(p.id)
        assert prospect.status is ProspectStatus.FAILED and prospect.last_error["code"] == "interrupted"
