"""12 Nov audit fixes over HTTP and the job runner: §8.1 step 4 salesperson stop, §20.7 change history, §7A.3
team-board filters, §21 scheduled reports, §3 visibility (quote versions, exports) and §14 who contacts buyers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import httpx
import pytest
from conftest import MGR_ID, PRODUCT_ID, SALES2_ID, SALES_ID, SKU, Seed, assign, new_phone
from sqlalchemy import select
from test_api_db import Auth
from test_api_db import api as api  # noqa: PLC0414 - re-export the `api` fixture

from kx.core import events as ev
from kx.core.auth import Role
from kx.core.contracts import AiRole, ContactPatch, RequirementPatch
from kx.core.settings import LogicalDb
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl
from kx.crm.models import AuditEvent, OutboxEvent
from kx.crm.services import scheduled_reports

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("cost_mode")]
Api = tuple[httpx.AsyncClient, Auth]


async def _deal(
    databases: Any, deps: Any, owner: uuid.UUID | None = SALES_ID, name: str = "Omar"
) -> uuid.UUID:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await gw.resolve_deal("whatsapp", new_phone(), None)
    await gw.set_contact(deal_id, ContactPatch(name=name), AiRole.CONTACT_CAPTURE)
    if owner is not None:
        await assign(databases, deal_id, owner)
    return deal_id


async def test_salesperson_records_a_stop_add_only(api: Api, databases: Any, deps: Any, seed: Seed) -> None:
    client, auth = api
    deal_id = await _deal(databases, deps)
    body = {"scope": "all", "words": "please stop messaging me", "reason": "found cheaper"}
    auth.as_(Role.SALES, SALES2_ID)  # not their deal (§3)
    assert (await client.post(f"/v1/deals/{deal_id}/opt-out", json=body)).status_code in (403, 404)
    auth.as_(Role.SALES, SALES_ID)
    r = await client.post(f"/v1/deals/{deal_id}/opt-out", json=body)
    assert r.status_code == 200 and r.json()["opted_out_at"] is not None
    assert r.json()["ai_enabled"] is False
    async with databases.transaction(LogicalDb.CRM) as s:
        events = (
            await s.execute(
                select(OutboxEvent.payload).where(
                    OutboxEvent.event_type == ev.CONSENT_OPTED_OUT, OutboxEvent.aggregate_id == str(deal_id)
                )
            )
        ).scalars()
        payload = next(iter(events))
    assert payload["source"] == "salesperson" and payload["reason"] == "found cheaper"
    assert payload["decided_by"] == f"salesperson:{SALES_ID}"


async def test_product_and_price_list_change_history(api: Api, seed: Seed, price_list: str) -> None:
    client, auth = api
    auth.as_(Role.MANAGER, MGR_ID)
    assert (await client.patch(f"/v1/products/{PRODUCT_ID}", json={"moq": 12_345})).status_code == 200
    tier = await client.post(
        f"/v1/products/{PRODUCT_ID}/price-tiers",
        json={"min_qty": "999000", "list_price": "0.5", "min_price": "0.4"},
    )
    assert tier.status_code == 201
    auth.as_(Role.SALES, SALES_ID)  # §20.7: everyone who sees the price list sees its history
    history = (await client.get(f"/v1/products/{PRODUCT_ID}/history")).json()
    actions = [h["action"] for h in history]
    assert "product.updated" in actions and "price_tier.created" in actions
    assert history == sorted(history, key=lambda h: h["at"], reverse=True)
    whole = (await client.get("/v1/price-list/history", params={"limit": 500})).json()
    assert any(h["entity_id"] == tier.json()["id"] for h in whole)
    assert all(h["action"].startswith(("price_tier.", "price_list.")) for h in whole)


async def test_team_board_unassigned_and_escalated(api: Api, databases: Any, deps: Any, seed: Seed) -> None:
    client, auth = api
    nobody = await _deal(databases, deps, owner=None)
    owned = await _deal(databases, deps)
    auth.as_(Role.MANAGER, MGR_ID)
    unassigned = (await client.get("/v1/deals", params={"unassigned": "true", "limit": 100})).json()["items"]
    ids = {d["id"] for d in unassigned}
    assert str(nobody) in ids and str(owned) not in ids
    escalated = (await client.get("/v1/deals", params={"escalated": "true", "limit": 100})).json()["items"]
    assert str(nobody) not in {d["id"] for d in escalated}  # no approval or change request yet


async def test_scheduled_reports_go_once_to_manager_and_md(databases: Any, seed: Seed, deps: CrmDeps) -> None:
    monday_9am_dubai = datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
    timed = CrmDeps(
        uploader=deps.uploader,
        settings=deps.settings,
        letterhead=deps.letterhead,
        clock=lambda: monday_9am_dubai,
    )
    async with databases.transaction(LogicalDb.CRM) as s:
        assert await scheduled_reports.send_due(s, timed) == 2  # daily summary + weekly report
    async with databases.transaction(LogicalDb.CRM) as s:
        assert await scheduled_reports.send_due(s, timed) == 0  # a redelivered job sends nothing twice
        keys = set(
            (
                await s.execute(select(AuditEvent.entity_id).where(AuditEvent.action == "report.sent"))
            ).scalars()
        )
        notices = list(
            (
                await s.execute(
                    select(OutboxEvent.payload).where(
                        OutboxEvent.event_type == ev.STAFF_NOTICE, OutboxEvent.aggregate_type == "report"
                    )
                )
            ).scalars()
        )
    assert {"daily:2026-11-15", "weekly:2026-11-15"} <= keys
    daily = next(n for n in notices if n["key"] == "report:daily:2026-11-15")
    assert daily["text"].startswith("Daily summary 15 Nov 2026:") and "\n" not in daily["text"]
    assert {c["name"] for c in daily["notify"]} == {"The Manager", "The MD"}  # reports.recipients default


async def test_scheduled_reports_wait_for_their_time(databases: Any, seed: Seed, deps: CrmDeps) -> None:
    early = datetime(2026, 11, 18, 3, 0, tzinfo=UTC)  # 07:00 Dubai, before the 08:00 default
    timed = CrmDeps(
        uploader=deps.uploader, settings=deps.settings, letterhead=deps.letterhead, clock=lambda: early
    )
    async with databases.transaction(LogicalDb.CRM) as s:
        assert await scheduled_reports.send_due(s, timed) == 0


async def test_sales_export_holds_only_visible_contacts(
    api: Api, databases: Any, deps: Any, seed: Seed
) -> None:
    client, auth = api
    await _deal(databases, deps, owner=SALES_ID, name="Export Visible Zaid")
    auth.as_(Role.SALES, SALES2_ID)
    assert "Export Visible Zaid" not in (await client.get("/v1/exports/contacts")).text
    auth.as_(Role.SALES, SALES_ID)
    assert "Export Visible Zaid" in (await client.get("/v1/exports/contacts")).text


async def test_quote_version_needs_deal_access_and_admin_never_contacts_buyers(
    api: Api, databases: Any, deps: Any, seed: Seed
) -> None:
    client, auth = api
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await _deal(databases, deps)
    await gw.update_requirement(
        deal_id,
        RequirementPatch(product_sku=SKU, quantity=D(50_000), delivery_country="AE"),
        AiRole.QUOTATION,
    )
    auth.as_(Role.MANAGER, MGR_ID)
    r = await client.post(f"/v1/deals/{deal_id}/stage", json={"to": "QUALIFIED", "expected_version": 1})
    assert r.status_code == 200
    auth.as_(Role.SALES, SALES_ID)
    version = (await client.post(f"/v1/deals/{deal_id}/quotes")).json()
    assert (await client.get(f"/v1/quote-versions/{version['id']}")).status_code == 200
    auth.as_(Role.SALES, SALES2_ID)  # §3: another salesperson's deal
    assert (await client.get(f"/v1/quote-versions/{version['id']}")).status_code in (403, 404)
    auth.as_(Role.ADMIN, uuid.uuid4())
    msg = {"text": "hello", "client_key": "admin-key-0001"}
    assert (await client.post(f"/v1/deals/{deal_id}/messages", json=msg)).status_code == 403
    assert (
        await client.post(f"/v1/deals/{deal_id}/call", json={"client_key": "admin-call-01"})
    ).status_code == 403
