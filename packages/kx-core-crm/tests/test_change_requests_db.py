"""WP4 (CRM) on real Postgres: the change-request lifecycle (docs/BUSINESS_FLOW_V1.md §13, §16, §23 scenario 4).

Done when: a CRM DB test covers the full lifecycle (raise -> start -> revise -> approve = resolved; decline).
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import SALES2_ID, SALES_ID
from test_api_db import Auth
from test_api_db import api as api  # noqa: PLC0414 - re-export the `api` fixture
from test_approvals_db import sara_deal
from test_flow_db import MGR, SARA, approve_and_send, decide, outbox, principal

from kx.core import events as ev
from kx.core.auth import Role
from kx.core.contracts import AiRole, ChangeField
from kx.core.db import Databases
from kx.core.errors import Conflict, Forbidden, RuleViolation
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import ApprovalStatusReaderImpl, CrmGatewayImpl
from kx.crm.services import change_requests, quotes
from kx.crm.services.journal import Actor

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("price_list")]
CRM = LogicalDb.CRM
RAVI = principal(Role.SALES, SALES2_ID)
# Monday 9 Nov 2026, 10:00 Dubai
MONDAY_10 = datetime(2026, 11, 9, 6, 0, tzinfo=UTC)
WORDS = "Can you deliver in 5 days?"


async def issued_deal(dbs: Databases, deps: CrmDeps) -> uuid.UUID:
    deal_id = await sara_deal(dbs, deps)
    await approve_and_send(dbs, deps, deal_id, SARA)
    return deal_id


async def raise_delivery(dbs: Databases, deps: CrmDeps, deal_id: uuid.UUID) -> m.ChangeRequest:
    gw = CrmGatewayImpl(dbs, deps)
    ref = await gw.raise_change_request(
        deal_id,
        "delivery",
        WORDS,
        (ChangeField(field="delivery_time", requested="5 days"),),
        AiRole.DEAL_CLOSER,
    )
    async with dbs.transaction(CRM) as s:
        cr = await s.get(m.ChangeRequest, ref)
        assert cr is not None
        return cr


async def test_raise_builds_the_card_and_alerts_the_salesperson(databases: Databases, deps: CrmDeps) -> None:
    at_10 = replace(deps, clock=lambda: MONDAY_10)
    deal_id = await issued_deal(databases, at_10)
    cr = await raise_delivery(databases, at_10, deal_id)
    assert (cr.type, cr.status, cr.buyer_text, cr.assigned_to) == ("delivery", "open", WORDS, SALES_ID)
    # §23 scenario 4: current "10-14 working days", requested "5 days", with the buyer's words
    assert cr.fields == [
        {
            "field": "delivery_time",
            "current": "10–14 working days after advance payment",
            "requested": "5 days",
        }
    ]
    assert cr.sla_due_at == MONDAY_10 + timedelta(hours=4)  # "within 4 working hours"
    raised = (await outbox(databases, ev.CHANGE_REQUESTED, deal_id))[-1]
    assert (raised["assigned_to"]["name"], raised["escalate_to"]["name"], raised["type"]) == (
        "Sara",
        "The Manager",
        "delivery",
    )
    assert [datetime.fromisoformat(t) for t in raised["remind_at"]] == [
        MONDAY_10 + timedelta(minutes=30),
        MONDAY_10 + timedelta(minutes=60),
    ]
    assert datetime.fromisoformat(raised["escalate_at"]) == MONDAY_10 + timedelta(minutes=120)
    assert await ApprovalStatusReaderImpl(databases).is_pending(cr.id)  # the ladder keeps reminding


async def test_sla_counts_alert_hours_only(databases: Databases, deps: CrmDeps) -> None:
    at_2030 = replace(deps, clock=lambda: datetime(2026, 11, 9, 16, 30, tzinfo=UTC))  # 20:30 Dubai
    deal_id = await issued_deal(databases, at_2030)
    cr = await raise_delivery(databases, at_2030, deal_id)
    # 1.5 h before 22:00, the other 2.5 h from 08:00 next day -> 10:30 Dubai
    assert cr.sla_due_at == datetime(2026, 11, 10, 6, 30, tzinfo=UTC)


async def test_scenario_4_full_lifecycle_through_the_api(
    api: tuple[httpx.AsyncClient, Auth], databases: Databases, deps: CrmDeps
) -> None:
    client, auth = api
    deal_id = await issued_deal(databases, deps)
    cr = await raise_delivery(databases, deps, deal_id)
    auth.as_(Role.SALES, SALES2_ID)  # Ravi: not his deal
    assert (await client.get("/v1/change-requests")).json() == []
    assert (await client.post(f"/v1/change-requests/{cr.id}/start")).status_code == 403
    auth.as_(Role.SALES, SALES_ID)
    inbox = (await client.get("/v1/change-requests")).json()
    assert [c["id"] for c in inbox if c["deal_id"] == str(deal_id)] == [str(cr.id)]
    assert (await client.post(f"/v1/change-requests/{cr.id}/start")).json()["status"] == "in_progress"
    # "Revise quote": Sara sets "5 working days from stock" (she checked the stock table)
    r = await client.post(
        f"/v1/change-requests/{cr.id}/revise",
        json={
            "delivery_time_text": "5 working days from stock",
            "note": "Updated: delivery in 5 working days",
        },
    )
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert (v2["kind"], v2["delivery_time_text"], v2["status"]) == (
        "revision",
        "5 working days from stock",
        "pending_approval",
    )
    card = (await client.get(f"/v1/change-requests/{cr.id}")).json()
    assert (card["status"], card["resolved_version_id"]) == ("in_progress", v2["id"])
    await decide(databases, deps, deal_id, SARA)  # approved and sent -> resolved
    card = (await client.get(f"/v1/change-requests/{cr.id}")).json()
    assert card["status"] == "resolved"
    closed = (await outbox(databases, ev.CHANGE_REQUEST_CLOSED, deal_id))[-1]
    assert (closed["status"], closed["resolved_version_ref"]) == ("resolved", v2["id"])
    sent = (await outbox(databases, ev.QUOTE_APPROVED, deal_id))[-1]
    assert sent["quote_version_ref"] == v2["id"]
    assert not await ApprovalStatusReaderImpl(databases).is_pending(cr.id)  # the ladder stops
    assert (
        await client.get("/v1/change-requests", params={"status": "closed", "deal_id": str(deal_id)})
    ).json()


async def test_decline_needs_a_reason_and_closes_the_card(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await issued_deal(databases, deps)
    cr = await raise_delivery(databases, deps, deal_id)
    async with databases.transaction(CRM) as s:
        with pytest.raises(RuleViolation):
            await change_requests.decline(s, deps, cr.id, "  ", SARA)
    with pytest.raises(Forbidden):
        async with databases.transaction(CRM) as s:
            await change_requests.decline(s, deps, cr.id, "no", RAVI)
    async with databases.transaction(CRM) as s:
        done = await change_requests.decline(s, deps, cr.id, "Our earliest is 10 working days.", SARA)
        assert (done.status, done.closed_at is not None) == ("declined", True)
    closed = (await outbox(databases, ev.CHANGE_REQUEST_CLOSED, deal_id))[-1]
    assert (closed["status"], closed["decline_reason"]) == ("declined", "Our earliest is 10 working days.")
    with pytest.raises(Conflict):
        async with databases.transaction(CRM) as s:
            await change_requests.start(s, deps, cr.id, SARA)


async def test_resolve_with_an_issued_version_only(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await issued_deal(databases, deps)
    cr = await raise_delivery(databases, deps, deal_id)
    async with databases.transaction(CRM) as s:
        draft = await quotes.revise_quote(
            s, deps, deal_id, quotes.QuoteEdits(delivery_place="Dubai"), Actor.user(SARA)
        )
    with pytest.raises(RuleViolation):
        async with databases.transaction(CRM) as s:
            await change_requests.resolve(s, deps, cr.id, draft.id, SARA)
    await decide(databases, deps, deal_id, MGR)
    async with databases.transaction(CRM) as s:
        done = await change_requests.resolve(s, deps, cr.id, draft.id, SARA)
        assert done.status == "resolved"


async def test_untouched_card_is_escalated_after_120_alert_hour_minutes(
    databases: Databases, deps: CrmDeps
) -> None:
    at_10 = replace(deps, clock=lambda: MONDAY_10)
    deal_id = await issued_deal(databases, at_10)
    cr = await raise_delivery(databases, at_10, deal_id)
    async with databases.transaction(CRM) as s:
        await s.execute(
            m.ChangeRequest.__table__.update().where(m.ChangeRequest.id == cr.id).values(created_at=MONDAY_10)
        )
    for minutes, escalated in ((119, False), (121, True)):
        async with databases.transaction(CRM) as s:
            await change_requests.sweep_escalations(s, deps, MONDAY_10 + timedelta(minutes=minutes))
            row = await s.get(m.ChangeRequest, cr.id, populate_existing=True)
            assert row is not None and (row.escalated_at is not None) is escalated, minutes
