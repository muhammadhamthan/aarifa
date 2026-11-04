"""kx.core against real Postgres: outbox ordering, relay, inbox de-dupe, single-use send grants."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from kx.core.events import Event, Outbox, inbox_once, relay_once
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db


async def test_outbox_relay_in_order_once(databases) -> None:  # type: ignore[no-untyped-def]
    async with databases.transaction(LogicalDb.CRM) as s:
        for i in range(3):
            await Outbox.add(s, "crm", Event(event_type="t.v1", aggregate_type="deal", aggregate_id=f"d{i}",
                                             payload={"n": i}))
    got: list[str] = []

    async def publish(e: Event) -> None:
        got.append(e.aggregate_id)

    async with databases.transaction(LogicalDb.CRM) as s:
        assert await relay_once(s, "crm", publish) == 3
    async with databases.transaction(LogicalDb.CRM) as s:
        assert await relay_once(s, "crm", publish) == 0
    assert got == ["d0", "d1", "d2"]


async def test_inbox_runs_handler_once(databases) -> None:  # type: ignore[no-untyped-def]
    eid = uuid.uuid4()
    async with databases.transaction(LogicalDb.AUTOMATION) as s:
        assert await inbox_once(s, "automation", eid, "send_quote")
        assert not await inbox_once(s, "automation", eid, "send_quote")
        assert await inbox_once(s, "automation", eid, "other_handler")


async def test_crm_tables_exist(databases) -> None:  # type: ignore[no-untyped-def]
    async with databases.transaction(LogicalDb.CRM) as s:
        n = (await s.execute(text(
            "select count(*) from information_schema.tables where table_schema='crm'"))).scalar_one()
    assert n >= 45
