"""One async engine per logical database; sessions with explicit transactions.

Each package uses ONLY its own database:  kx.crm -> CRM, kx.automation -> AUTOMATION, kx.agents -> AGENTS.
Cross-database work goes through service calls or events (see kx.core.events), never SQL joins.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import orjson
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from kx.core.settings import CoreSettings, LogicalDb

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def make_base(schema: str) -> type[DeclarativeBase]:
    """Declarative base bound to one Postgres schema (crm / automation / agents)."""

    class Base(DeclarativeBase):
        metadata = MetaData(schema=schema, naming_convention=NAMING)

    return Base


def _json_dumps(value: object) -> str:
    return orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS).decode()


class Databases:
    """Owns the engines. Create once per process (FastAPI lifespan / worker startup)."""

    def __init__(self, settings: CoreSettings, only: set[LogicalDb] | None = None) -> None:
        self._engines: dict[LogicalDb, AsyncEngine] = {}
        self._makers: dict[LogicalDb, async_sessionmaker[AsyncSession]] = {}
        for db in only or set(LogicalDb):
            engine = create_async_engine(
                settings.dsn_for(db),
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_pre_ping=True,
                json_serializer=_json_dumps,
                json_deserializer=orjson.loads,
                connect_args={"server_settings": {
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "application_name": f"kx-{settings.company_code}-{db.value}",
                    "search_path": f"{db.value},public",
                }},
            )
            self._engines[db] = engine
            self._makers[db] = async_sessionmaker(engine, expire_on_commit=False)

    def engine(self, db: LogicalDb) -> AsyncEngine:
        return self._engines[db]

    @asynccontextmanager
    async def transaction(self, db: LogicalDb) -> AsyncIterator[AsyncSession]:
        """Session inside one transaction: commit on success, rollback on error."""
        async with self._makers[db]() as session, session.begin():
            yield session

    def dependency(self, db: LogicalDb) -> Callable[[], AsyncIterator[AsyncSession]]:
        """FastAPI dependency yielding a transactional session for `db`."""

        async def _dep() -> AsyncIterator[AsyncSession]:
            async with self.transaction(db) as session:
                yield session

        return _dep

    async def dispose(self) -> None:
        for engine in self._engines.values():
            await engine.dispose()
