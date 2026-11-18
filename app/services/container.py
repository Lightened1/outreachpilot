"""Composition root: builds and owns every long-lived service for one app instance."""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.llm import LLMClient, build_llm_client
from app.agents.message_agent import MessageAgent
from app.automation.browser import BrowserEngine
from app.automation.portal import DemoPortalClient
from app.config import Settings
from app.core.logging import get_logger
from app.core.retry import RetryPolicy
from app.core.tasks import TaskManager
from app.database import Database
from app.services.task_store import SqlTaskStore
from app.services.workflow import WorkflowService

log = get_logger(__name__)


@dataclass
class Container:
    settings: Settings
    db: Database
    tasks: TaskManager
    browser: BrowserEngine
    portal: DemoPortalClient
    llm: LLMClient
    agent: MessageAgent
    workflow: WorkflowService

    @classmethod
    async def create(cls, settings: Settings, *, llm: LLMClient | None = None) -> Container:
        db = Database(settings.database_url)
        await db.create_all()
        settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        store = SqlTaskStore(db.session_factory)
        recovered = await store.recover_interrupted()
        if recovered:
            log.warning("tasks.recovered", extra={"count": recovered})
        tasks = TaskManager(
            store,
            max_concurrency=settings.task_max_concurrency,
            default_policy=RetryPolicy(
                max_attempts=settings.task_max_attempts,
                base_delay=settings.retry_base_delay_s,
                max_delay=settings.retry_max_delay_s,
            ),
            default_timeout_s=settings.task_timeout_s,
        )
        browser = BrowserEngine(settings)  # started lazily on the first automation job
        portal = DemoPortalClient(browser, settings)
        llm = llm or build_llm_client(settings)
        agent = MessageAgent(llm, settings)
        workflow = WorkflowService(
            settings=settings, sessions=db.session_factory, tasks=tasks, portal=portal, agent=agent
        )
        log.info("app.ready", extra=settings.public_summary())
        return cls(settings, db, tasks, browser, portal, llm, agent, workflow)

    async def shutdown(self) -> None:
        await self.tasks.shutdown()
        await self.browser.stop()
        await self.llm.aclose()
        await self.db.dispose()
        log.info("app.stopped")
