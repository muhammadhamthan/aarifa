"""WP5 on real Postgres: assignment, away/backup, reassign, MD takeover and hand-back (§9, §3, §23 sc. 6/8; D5).

Done when: DB tests cover concurrent assignment with no duplicates.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from decimal import Decimal as D

import httpx
import pytest
from conftest import LIST_SKU, MD_ID, MGR_ID, SALES2_ID, SALES_ID, assign, new_phone
from sqlalchemy import select, update
from test_api_db import Auth
from test_api_db import api as api  # noqa: PLC0414 - re-export the `api` fixture
from test_flow_db import MD, MGR, SARA, decide, open_approval, outbox, principal

from kx.core import events as ev
from kx.core.auth import Role
from kx.core.contracts import AiRole, ContactPatch, RequirementPatch
from kx.core.db import Databases
from kx.core.errors import Forbidden
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl
from kx.crm.services import assignment, deals
from kx.crm.services.journal import Actor

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("price_list")]
CRM = LogicalDb.CRM
AI = AiRole.QUOTATION
RAVI = principal(Role.SALES, SALES2_ID)


async def new_deal(
    gw: CrmGatewayImpl, *, phone: str | None = None, language: str = "en", country: str = "AE"
) -> uuid.UUID:
    deal_id = await gw.resolve_deal("whatsapp", phone or new_phone(), None)
    await gw.update_requirement(
        deal_id, RequirementPatch(product_sku=LIST_SKU, quantity=D(50_000), delivery_country=country), AI
    )
    await gw.set_contact(deal_id, ContactPatch(name="Ahmed", language=language), AiRole.CONTACT_CAPTURE)
    return deal_id


async def owner(dbs: Databases, deal_id: uuid.UUID) -> uuid.UUID | None:
    async with dbs.transaction(CRM) as s:
        return (await deals.get_deal(s, deal_id)).owner_user_id


async def set_user(dbs: Databases, user_id: uuid.UUID, **values: object) -> None:
    async with dbs.transaction(CRM) as s:
        await s.execute(update(m.User).where(m.User.id == user_id).values(**values))


# ---------------------------------------------------------------- §5 step 4 / §9.1 round-robin
async def test_qualifying_assigns_a_salesperson_and_alerts_them(databases: Databases, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await new_deal(gw)
    assert await owner(databases, deal_id) is None
    assert (await gw.try_qualify(deal_id, AiRole.LEAD_QUALIFICATION)).qualified
    who = await owner(databases, deal_id)
    assert who in (SALES_ID, SALES2_ID)
    assigned = (await outbox(databases, ev.DEAL_ASSIGNED, deal_id))[-1]
    assert assigned["assigned_to"]["user_ref"] == str(who) and assigned["reason"] in (
        "round_robin",
        "returning_customer",
    )
    assert (await gw.deal_context(deal_id)).salesperson_name in ("Sara", "Ravi")


async def test_concurrent_assignments_take_turns_without_duplicates(
    databases: Databases, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    ids = [await new_deal(gw) for _ in range(10)]

    async def one(deal_id: uuid.UUID) -> uuid.UUID:
        async with databases.transaction(CRM) as s:
            deal = await deals.get_deal(s, deal_id, lock=True)
            return (await assignment.assign(s, deps, deal, Actor.system("test"))).id

    picked = await asyncio.gather(*(one(d) for d in ids))
    assert Counter(picked) == {SALES_ID: 5, SALES2_ID: 5}  # strict alternation between Sara and Ravi
    async with databases.transaction(CRM) as s:
        seqs = (await s.execute(select(m.User.round_robin_seq).where(m.User.role == "sales"))).scalars().all()
    assert len(set(seqs)) == len(seqs)


async def test_returning_customer_goes_to_their_last_salesperson(databases: Databases, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    phone = new_phone()
    first = await new_deal(gw, phone=phone)
    await gw.try_qualify(first, AiRole.LEAD_QUALIFICATION)
    await assign(databases, first, SALES2_ID)
    async with databases.transaction(CRM) as s:  # the first deal ends; the buyer comes back later
        deal = await deals.get_deal(s, first, lock=True)
        await deals.move_stage(
            s, deal, deals.DealStage.LOST, Actor.user(MGR), now=deps.now(), lost_reason="price_too_high"
        )
    second = await new_deal(gw, phone=phone)
    assert second != first
    await gw.try_qualify(second, AiRole.LEAD_QUALIFICATION)
    assert await owner(databases, second) == SALES2_ID
    assert (await outbox(databases, ev.DEAL_ASSIGNED, second))[-1]["reason"] == "returning_customer"


async def test_language_rule_and_away_backup(databases: Databases, deps: CrmDeps) -> None:
    from conftest import set_setting

    gw = CrmGatewayImpl(databases, deps)
    await set_setting(databases, "assignment.rules", ["language"])
    try:
        arabic = await new_deal(gw, language="ar", country="SA")
        await gw.try_qualify(arabic, AiRole.LEAD_QUALIFICATION)
        assert await owner(databases, arabic) == SALES_ID  # §23 scenario 7: Sara speaks Arabic
        await set_user(databases, SALES_ID, is_away=True, backup_user_id=SALES2_ID)
        try:
            covered = await new_deal(gw, language="ar", country="SA")
            await gw.try_qualify(covered, AiRole.LEAD_QUALIFICATION)
            assert await owner(databases, covered) == SALES2_ID  # Sara away: her backup Ravi
        finally:
            await set_user(databases, SALES_ID, is_away=False, backup_user_id=None)
    finally:
        await set_setting(databases, "assignment.rules", [])


async def test_away_salesperson_open_approvals_go_to_the_backup(databases: Databases, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await new_deal(gw)
    await gw.try_qualify(deal_id, AiRole.LEAD_QUALIFICATION)
    await assign(databases, deal_id, SALES_ID)
    await gw.request_quote(deal_id, AI)
    async with databases.transaction(CRM) as s:
        await assignment.set_availability(s, deps, SALES_ID, is_away=True, backup_user_id=SALES2_ID, p=SARA)
    try:
        a = await open_approval(databases, deal_id)
        assert a.assigned_user_id == SALES2_ID
        await decide(databases, deps, deal_id, RAVI)  # the backup covers (§9.3)
    finally:
        async with databases.transaction(CRM) as s:
            await assignment.set_availability(s, deps, SALES_ID, is_away=False, backup_user_id=None, p=MGR)


# ---------------------------------------------------------------- §9.4 reassign, §3 takeover (HTTP)
async def test_scenario_6_manager_reassigns_and_the_pending_approval_follows(
    api: tuple[httpx.AsyncClient, Auth], databases: Databases, deps: CrmDeps
) -> None:
    client, auth = api
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await new_deal(gw)
    await gw.try_qualify(deal_id, AiRole.LEAD_QUALIFICATION)
    await assign(databases, deal_id, SALES_ID)
    await gw.request_quote(deal_id, AI)
    before = await open_approval(databases, deal_id)
    auth.as_(Role.SALES, SALES_ID)
    assert (
        await client.post(f"/v1/deals/{deal_id}/reassign", json={"user_id": str(SALES2_ID)})
    ).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID)
    r = await client.post(f"/v1/deals/{deal_id}/reassign", json={"user_id": str(SALES2_ID)})
    assert r.status_code == 200 and r.json()["owner_user_id"] == str(SALES2_ID)
    moved = await open_approval(databases, deal_id)
    assert moved.id != before.id and moved.assigned_user_id == SALES2_ID
    async with databases.transaction(CRM) as s:
        assert (await s.get(m.Approval, before.id, populate_existing=True)).status == "cancelled"  # type: ignore[union-attr]
    event = (await outbox(databases, ev.DEAL_ASSIGNED, deal_id))[-1]
    assert (event["reason"], event["assigned_to"]["name"], event["previous"]["name"]) == (
        "reassigned",
        "Ravi",
        "Sara",
    )
    assert {c["name"] for c in event["notify"]} == {"Sara", "The Manager"}  # both salespersons + the manager
    with pytest.raises(Forbidden):
        await decide(databases, deps, deal_id, SARA)
    await decide(databases, deps, deal_id, RAVI)


async def test_scenario_8_md_takes_over_and_hands_back(
    api: tuple[httpx.AsyncClient, Auth], databases: Databases, deps: CrmDeps
) -> None:
    client, auth = api
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await new_deal(gw)
    await gw.try_qualify(deal_id, AiRole.LEAD_QUALIFICATION)
    await assign(databases, deal_id, SALES_ID)
    await gw.request_quote(deal_id, AI)
    auth.as_(Role.MANAGER, MGR_ID)
    assert (await client.post(f"/v1/deals/{deal_id}/takeover")).status_code == 403  # MD only
    auth.as_(Role.MD, MD_ID)
    r = (await client.post(f"/v1/deals/{deal_id}/takeover")).json()
    assert (r["taken_over_by"], r["ai_enabled"], r["ai_paused_reason"]) == (str(MD_ID), False, "md_takeover")
    assert (await open_approval(databases, deal_id)).assigned_user_id == MD_ID  # approvals come to the MD
    taken = (await outbox(databases, ev.DEAL_ASSIGNED, deal_id))[-1]
    assert taken["reason"] == "takeover" and {c["name"] for c in taken["notify"]} == {"Sara", "The Manager"}
    auth.as_(Role.SALES, SALES_ID)
    assert (await client.get(f"/v1/deals/{deal_id}")).status_code == 200  # Sara keeps view access
    assert (await client.post(f"/v1/deals/{deal_id}/quotes")).status_code == 403
    await decide(databases, deps, deal_id, MD)  # the MD approves while it holds the deal
    auth.as_(Role.MD, MD_ID)
    r = (await client.post(f"/v1/deals/{deal_id}/handback")).json()
    assert r["taken_over_by"] is None and r["owner_user_id"] == str(SALES_ID)
    assert (await outbox(databases, ev.DEAL_ASSIGNED, deal_id))[-1]["reason"] == "hand_back"
    auth.as_(Role.SALES, SALES_ID)
    assert (
        await client.post(f"/v1/deals/{deal_id}/quotes/revise", json={"delivery_place": "Dubai"})
    ).status_code == 201


async def test_availability_route(api: tuple[httpx.AsyncClient, Auth], databases: Databases) -> None:
    client, auth = api
    auth.as_(Role.SALES, SALES2_ID)
    assert (await client.put(f"/v1/users/{SALES_ID}/availability", json={"is_away": True})).status_code == 403
    auth.as_(Role.SALES, SALES_ID)
    r = await client.put(
        f"/v1/users/{SALES_ID}/availability", json={"is_away": True, "backup_user_id": str(SALES2_ID)}
    )
    assert r.status_code == 200 and r.json()["is_away"] is True
    auth.as_(Role.MANAGER, MGR_ID)
    assert (await client.put(f"/v1/users/{SALES_ID}/availability", json={"is_away": False})).json()[
        "is_away"
    ] is False
