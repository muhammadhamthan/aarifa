"""Shared fixtures.

DB tests (`@pytest.mark.db`) need a Postgres 16+ with pgvector, reachable at KX_TEST_PG_DSN
(admin DSN, e.g. postgresql://postgres@/postgres?host=/tmp&port=5433). Each session builds fresh
pack_crm / pack_automation / pack_agents databases from db/ddl/*.sql, so tests never share state
with a developer database. Without the env var, DB tests are skipped (unit + spec tests still run).
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncIterator, Iterator

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DDL = ROOT / "db" / "ddl"
ADMIN_DSN = os.environ.get("KX_TEST_PG_DSN")
DBS = {"crm": "01_crm.sql", "automation": "02_automation.sql", "agents": "03_agents.sql"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if ADMIN_DSN:
        return
    skip = pytest.mark.skip(reason="KX_TEST_PG_DSN not set")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


def _dsn(db: str, *, driver: str = "") -> str:
    assert ADMIN_DSN
    base, _, query = ADMIN_DSN.partition("?")
    head = base.rsplit("/", 1)[0]
    if driver:
        head = head.replace("postgresql://", f"postgresql+{driver}://", 1)
    return f"{head}/test_{db}" + (f"?{query}" if query else "")


@pytest.fixture(scope="session")
def pg_databases() -> Iterator[dict[str, str]]:
    """Create test_crm/test_automation/test_agents from the DDL; yield asyncpg DSNs per logical DB."""
    import psycopg

    assert ADMIN_DSN
    roles = (DDL / "00_roles.sql").read_text()
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(roles)
        for name in DBS:
            admin.execute(f'DROP DATABASE IF EXISTS "test_{name}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "test_{name}"')
    for name, file in DBS.items():
        with psycopg.connect(_dsn(name), autocommit=True) as conn:
            conn.execute((DDL / file).read_text())
    yield {name: _dsn(name, driver="asyncpg") for name in DBS}
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        for name in DBS:
            admin.execute(f'DROP DATABASE IF EXISTS "test_{name}" WITH (FORCE)')


@pytest.fixture
async def databases(pg_databases: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[object]:
    """kx.core.db.Databases wired to the test DBs (superuser DSN; role grants are tested in SQL)."""
    from kx.core.db import Databases
    from kx.core.settings import CoreSettings

    settings = CoreSettings(
        crm_dsn=pg_databases["crm"],
        automation_dsn=pg_databases["automation"],
        agents_dsn=pg_databases["agents"],
        company_code="pack",
    )
    dbs = Databases(settings)
    try:
        yield dbs
    finally:
        await dbs.dispose()
