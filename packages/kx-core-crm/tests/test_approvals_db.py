"""WP3 on real Postgres: the salesperson approves (docs/BUSINESS_FLOW_V1.md §3, §11.3, §14.4, §16; D1, D3).

Done when (V1_CHANGE_PLAN WP3): the salesperson can approve their own deal but not another salesperson's;
the manager can approve any deal; the MD only after a takeover; the owner can't edit during a takeover;
a final quote approved by the salesperson issues a grant.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import httpx
import pytest
from conftest import MD_ID, MGR_ID, SALES2_ID, SALES_ID, assign, set_setting
from sqlalchemy import update
from test_api_db import Auth
from test_api_db import api as api  # noqa: PLC0414 - re-export the `api` fixture
from test_flow_db import MD, MGR, SARA, approve_and_send, decide, open_approval, outbox, principal
from test_price_list_db import list_deal

from kx.core import events as ev
from kx.core.auth import Role
from kx.core.contracts import AiRole
from kx.core.db import Databases
from kx.core.errors import Forbidden, RuleViolation
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl, GrantConsumerImpl
from kx.crm.services import approvals, quotes
from kx.crm.services.journal import Actor

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("price_list")]
CRM = LogicalDb.CRM
AI = AiRole.QUOTATION
RAVI = principal(Role.SALES, SALES2_ID)
DUBAI = 4  # UTC+4


async def sara_deal(dbs: Databases, deps: CrmDeps, **kw: Any) -> uuid.UUID:
    gw = CrmGatewayImpl(dbs, deps)
    deal_id = await list_deal(gw, **kw)
    await assign(dbs, deal_id, SALES_ID)
    await gw.request_quote(deal_id, AI)
    return deal_id


async def take_over(dbs: Databases, deal_id: uuid.UUID, by: uuid.UUID | None) -> None:
    async with dbs.transaction(CRM) as s:
        await s.execute(
            update(m.Deal)
            .where(m.Deal.id == deal_id)
            .values(taken_over_by=by, taken_over_at=None if by is None else datetime.now(UTC))
        )


async def _decide(dbs: Databases, deps: CrmDeps, approval_id: uuid.UUID, who: Any) -> m.Approval:
    async with dbs.transaction(CRM) as s:
        return await approvals.decide(s, deps, approval_id, who, decision="approve")


# ---------------------------------------------------------------- D1: who approves
async def test_salesperson_approves_own_deal_not_another_salespersons(
    databases: Databases, deps: CrmDeps
) -> None:
    deal_id = await sara_deal(databases, deps)
    a = await open_approval(databases, deal_id)
    assert (a.kind, a.required_role, a.assigned_user_id, a.approver_user_id) == (
        "initial",
        "sales",
        SALES_ID,
        SALES_ID,
    )
    submitted = (await outbox(databases, ev.QUOTE_SUBMITTED, deal_id))[-1]
    assert (submitted["approver"]["name"], submitted["escalate_to"]["name"]) == ("Sara", "The Manager")
    for who in (RAVI, principal(Role.SALES, SALES_ID, mfa=False), principal(Role.ADMIN, MGR_ID)):
        with pytest.raises(Forbidden):
            await _decide(databases, deps, a.id, who)
    approved = await approve_and_send(databases, deps, deal_id, SARA)
    async with databases.transaction(CRM) as s:
        g = await s.get(m.QuoteSendGrant, uuid.UUID(approved["grant_ref"]))
        assert g is not None and g.policy == "sales"


async def test_manager_approves_any_deal(databases: Databases, deps: CrmDeps) -> None:
    for owner in (SALES_ID, SALES2_ID):
        gw = CrmGatewayImpl(databases, deps)
        deal_id = await list_deal(gw)
        await assign(databases, deal_id, owner)
        await gw.request_quote(deal_id, AI)
        approved = await approve_and_send(databases, deps, deal_id, MGR)
        assert approved["kind"] == "commercial"


async def test_md_approves_only_after_a_takeover(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await sara_deal(databases, deps)
    a = await open_approval(databases, deal_id)
    with pytest.raises(Forbidden, match="taken over"):
        await _decide(databases, deps, a.id, MD)
    await take_over(databases, deal_id, MD_ID)
    try:
        with pytest.raises(Forbidden, match="taken over"):  # the owner is read-only meanwhile
            await _decide(databases, deps, a.id, SARA)
        await _decide(databases, deps, a.id, MD)
        # §23 scenario 8: while taken over, new approvals come to the MD
        async with databases.transaction(CRM) as s:
            await quotes.revise_quote(
                s, deps, deal_id, quotes.QuoteEdits(delivery_place="Riyadh warehouse"), Actor.user(MD)
            )
        assert (await outbox(databases, ev.QUOTE_SUBMITTED, deal_id))[-1]["approver"]["name"] == "The MD"
    finally:
        await take_over(databases, deal_id, None)


async def test_owner_cannot_edit_during_a_takeover(
    api: tuple[httpx.AsyncClient, Auth], databases: Databases, deps: CrmDeps
) -> None:
    client, auth = api
    deal_id = await sara_deal(databases, deps)
    auth.as_(Role.SALES, SALES_ID)
    assert (await client.get(f"/v1/deals/{deal_id}")).status_code == 200
    await take_over(databases, deal_id, MD_ID)
    try:
        assert (await client.get(f"/v1/deals/{deal_id}")).status_code == 200  # keeps view access (§3)
        for url, body in (
            (f"/v1/deals/{deal_id}/quotes", None),
            (f"/v1/deals/{deal_id}/quotes/revise", {"delivery_place": "Dubai"}),
            (
                f"/v1/deals/{deal_id}/stage",
                {"to": "LOST", "expected_version": 1, "lost_reason": "price_too_high"},
            ),
        ):
            r = await client.post(url, json=body)
            assert r.status_code == 403 and "taken over" in r.json()["detail"], url
        a = await open_approval(databases, deal_id)
        r = await client.post(f"/v1/approvals/{a.id}/decision", json={"decision": "approve"})
        assert r.status_code == 403
        auth.as_(Role.SALES, SALES2_ID)  # another salesperson cannot even view it
        assert (await client.get(f"/v1/deals/{deal_id}")).status_code == 403
    finally:
        await take_over(databases, deal_id, None)


async def test_final_quote_approved_by_the_salesperson_issues_a_single_use_grant(
    databases: Databases, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await sara_deal(databases, deps)
    first = await approve_and_send(databases, deps, deal_id, SARA)
    await gw.mark_agreed(deal_id, AiRole.DEAL_CLOSER)
    a = await open_approval(databases, deal_id)
    assert (a.kind, a.assigned_user_id) == ("final", SALES_ID)
    await decide(databases, deps, deal_id, SARA)
    final = (await outbox(databases, ev.QUOTE_APPROVED, deal_id))[-1]
    assert final["kind"] == "final" and final["grant_ref"] != first["grant_ref"]
    consumer = GrantConsumerImpl(databases, deps)
    from kx.core.identity import recipient_hash

    rcpt = recipient_hash(final["channel"], final["to_address"])
    grant, sha = uuid.UUID(final["grant_ref"]), final["content_sha256"]
    assert await consumer.consume_grant(grant, sha, msg := uuid.uuid4(), rcpt)
    assert await consumer.consume_grant(grant, sha, msg, rcpt)  # the same message may retry
    assert not await consumer.consume_grant(grant, sha, uuid.uuid4(), rcpt)  # never a second message


# ---------------------------------------------------------------- §11.3 edit then approve, D3 special price
async def test_edit_then_approve_stays_between_minimum_and_list(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await sara_deal(databases, deps)
    edits = quotes.QuoteEdits(
        lines=(quotes.LineEdit(1, unit_price=D("0.24")),), incoterm="DAP", delivery_place="Dubai"
    )
    async with databases.transaction(CRM) as s:
        v2 = await quotes.revise_quote(s, deps, deal_id, edits, Actor.user(SARA))
        assert (v2.kind, v2.version_no, v2.incoterm, v2.delivery_place) == ("revision", 2, "DAP", "Dubai")
    a = await open_approval(databases, deal_id)
    assert (a.kind, a.assigned_user_id) == ("revision", SALES_ID)
    with pytest.raises(RuleViolation, match="list price"):
        async with databases.transaction(CRM) as s:
            await quotes.revise_quote(
                s,
                deps,
                deal_id,
                quotes.QuoteEdits(lines=(quotes.LineEdit(1, unit_price=D("0.26")),)),
                Actor.user(SARA),
            )


async def test_scenario_3_special_price_needs_the_manager_then_the_salesperson(
    databases: Databases, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await sara_deal(databases, deps, qty=100_000)
    await approve_and_send(databases, deps, deal_id, SARA)
    async with databases.transaction(CRM) as s:  # Sara proposes 0.215 (minimum 0.23)
        v2 = await quotes.revise_quote(
            s,
            deps,
            deal_id,
            quotes.QuoteEdits(lines=(quotes.LineEdit(1, unit_price=D("0.215")),)),
            Actor.user(SARA),
        )
    special = await open_approval(databases, deal_id)
    assert (special.kind, special.required_role, special.assigned_user_id) == (
        "special_price",
        "manager",
        MGR_ID,
    )
    submitted = (await outbox(databases, ev.QUOTE_SUBMITTED, deal_id))[-1]
    assert (submitted["kind"], submitted["approver"]["name"]) == ("special_price", "The Manager")
    for who in (SARA, MD):
        with pytest.raises(Forbidden):
            await _decide(databases, deps, special.id, who)
    await _decide(databases, deps, special.id, MGR)
    assert not await outbox(databases, ev.QUOTE_APPROVED, deal_id) or all(
        e["quote_version_ref"] != str(v2.id) for e in await outbox(databases, ev.QUOTE_APPROVED, deal_id)
    )  # nothing goes to the buyer on the special-price step
    sales = await open_approval(databases, deal_id)
    assert (sales.kind, sales.quote_version_id, sales.assigned_user_id) == ("revision", v2.id, SALES_ID)
    sent = await approve_and_send(databases, deps, deal_id, SARA)
    assert sent["quote_version_ref"] == str(v2.id)
    ctx = await gw.deal_context(deal_id)
    assert D("0.215") in {p.amount for p in ctx.allowed_prices}
    # the special price unlocked v2 only: nothing lower is ever offered on it
    v = await gw.evaluate_counter(deal_id, D("0.21"), None, "whatsapp")
    assert v.offer_unit_price in (None, D("0.215"))


async def test_special_price_declined_keeps_the_quote_unsent(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await sara_deal(databases, deps)
    async with databases.transaction(CRM) as s:
        await quotes.revise_quote(
            s,
            deps,
            deal_id,
            quotes.QuoteEdits(lines=(quotes.LineEdit(1, unit_price=D("0.20")),)),
            Actor.user(SARA),
        )
    special = await open_approval(databases, deal_id)
    async with databases.transaction(CRM) as s:
        await approvals.decide(s, deps, special.id, MGR, decision="reject", note="best price stays AED 0.23")
        v = await quotes.latest_version(s, deal_id)
        assert v is not None and v.status == "rejected"


# ---------------------------------------------------------------- §16 timers in alert hours, MD value limit
@pytest.mark.parametrize(
    ("submitted_dubai", "due_dubai"),
    [
        (datetime(2026, 11, 9, 14, 0), datetime(2026, 11, 9, 16, 0)),  # scenario 6: 14:00 -> 16:00
        (datetime(2026, 11, 9, 21, 30), datetime(2026, 11, 10, 9, 30)),  # pauses 22:00, resumes 08:00
    ],
)
async def test_escalation_counts_alert_hours_only(
    databases: Databases, deps: CrmDeps, submitted_dubai: datetime, due_dubai: datetime
) -> None:
    at = (submitted_dubai - timedelta(hours=DUBAI)).replace(tzinfo=UTC)
    deal_id = await sara_deal(databases, replace(deps, clock=lambda: at))
    a = await open_approval(databases, deal_id)
    assert a.deadline_at == (due_dubai - timedelta(hours=DUBAI)).replace(tzinfo=UTC)
    assert a.window_minutes == 120


async def test_md_value_limit_routes_to_the_md_only_when_set(databases: Databases, deps: CrmDeps) -> None:
    await set_setting(databases, "approval.md_value_limit_aed", 10_000)
    try:
        deal_id = await sara_deal(databases, deps)  # AED 13,125.00 > 10,000
        a = await open_approval(databases, deal_id)
        assert (a.required_role, a.assigned_user_id) == ("md", MD_ID)
        with pytest.raises(Forbidden, match="MD value limit"):
            await _decide(databases, deps, a.id, SARA)
        await _decide(databases, deps, a.id, MD)
    finally:
        await set_setting(databases, "approval.md_value_limit_aed", None)


# ---------------------------------------------------------------- WP6: the message that carries the quote
async def test_sent_quote_message_carries_the_ai_cover_line_signature_and_region(
    databases: Databases, deps: CrmDeps
) -> None:
    from kx.core.price_guard import check_prices

    gw = CrmGatewayImpl(databases, deps)
    deal_id = await sara_deal(databases, deps)
    cover = "If 50,000 grows into a bigger or repeat order, just reply and we can look at it together."
    await gw.set_cover_note(deal_id, "  " + cover + "  ", AiRole.QUOTATION)
    sent = await approve_and_send(databases, deps, deal_id, SARA)
    assert cover in sent["body"] and sent["body"].rstrip().endswith("— Sara, Arifaa Pack (test)")
    assert (sent["buyer_country"], sent["language"]) == ("AE", "en")
    ctx = await gw.deal_context(deal_id)
    assert check_prices(sent["body"], ctx.allowed_prices, default_currency="AED").ok


# ---------------------------------------------------------------- WP7: final approval hands over (D13)
async def test_final_approval_hands_the_deal_to_the_salesperson(databases: Databases, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await sara_deal(databases, deps)
    await approve_and_send(databases, deps, deal_id, SARA)
    assert (await gw.deal_context(deal_id)).ai_enabled
    await gw.mark_agreed(deal_id, AiRole.DEAL_CLOSER)
    final = await approve_and_send(databases, deps, deal_id, SARA)
    assert (final["salesperson"]["name"], final["manager"]["name"]) == ("Sara", "The Manager")
    ctx = await gw.deal_context(deal_id)
    assert (ctx.ai_enabled, ctx.ai_paused_reason, ctx.stage.value) == (False, "handover", "READY_TO_CLOSE")
    first = (await outbox(databases, ev.QUOTE_APPROVED, deal_id))[0]
    assert first["salesperson"] is None  # only the final quote alerts and notifies (§17)
