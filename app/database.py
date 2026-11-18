"""Async SQLAlchemy engine, session factory and schema creation (SQLite via aiosqlite)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Database:
    """Owns the engine and the session factory for one application instance."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self._ensure_sqlite_dir(url)
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, connect_args={"timeout": 15})
        event.listen(self.engine.sync_engine, "connect", self._sqlite_pragmas)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @staticmethod
    def _ensure_sqlite_dir(url: str) -> None:
        database = make_url(url).database
        if database and database != ":memory:":
            Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    async def create_all(self) -> None:
        from app import models  # noqa: F401 - registers the tables on Base.metadata

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - reported as unhealthy
            return False

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
