"""WP8 on real Postgres: payment, WON, LOST (docs/BUSINESS_FLOW_V1.md §5, §15, §16, §23 scenarios 1 and 9; D15)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from sqlalchemy import select
from test_approvals_db import sara_deal
from test_flow_db import SARA, approve_and_send, outbox

from kx.core import events as ev
from kx.core.contracts import AiRole, DealStage
from kx.core.db import Databases
from kx.core.errors import RuleViolation
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl
from kx.crm.rules import LOST_REASONS, lost_proposal_due
from kx.crm.services import deals, lost_proposals, orders
from kx.crm.services.journal import Actor

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("price_list")]
CRM = LogicalDb.CRM


async def agreed_deal(dbs: Databases, deps: CrmDeps) -> uuid.UUID:
    gw = CrmGatewayImpl(dbs, deps)
    deal_id = await sara_deal(dbs, deps)
    await approve_and_send(dbs, deps, deal_id, SARA)
    await gw.mark_agreed(deal_id, AiRole.DEAL_CLOSER)
    await approve_and_send(dbs, deps, deal_id, SARA)
    return deal_id


async def test_scenario_1_advance_then_won_with_the_reorder_date(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await agreed_deal(databases, deps)
    async with databases.transaction(CRM) as s:
        with pytest.raises(RuleViolation, match="amount, currency and method"):
            await orders.record_payment(s, deps, deal_id, Actor.user(SARA), amount=D(6562), currency="AED")
    async with databases.transaction(CRM) as s:
        pay = await orders.record_payment(
            s,
            deps,
            deal_id,
            Actor.user(SARA),
            amount=D("6562.50"),
            currency="aed",
            method="bank transfer",
            reference="TT-1001",
            proof_s3_key="pack/payments/tt-1001.pdf",
        )
        assert (pay.currency, pay.proof_s3_key) == ("AED", "pack/payments/tt-1001.pdf")
        assert (await deals.get_deal(s, deal_id)).stage == "PAYMENT"  # the advance moves it to PAYMENT
    async with databases.transaction(CRM) as s:
        with pytest.raises(RuleViolation, match="re-order date"):
            await orders.mark_won(s, deps, deal_id, Actor.user(SARA), expected_reorder_date=None)
    reorder = deps.now().date() + timedelta(days=30)  # "about a month later"
    async with databases.transaction(CRM) as s:
        order = await orders.mark_won(s, deps, deal_id, Actor.user(SARA), expected_reorder_date=reorder)
        assert (order.status, order.expected_reorder_date) == ("in_production", reorder)
        assert (await deals.get_deal(s, deal_id)).stage == "WON"
    for status in ("ready", "shipped", "delivered"):
        async with databases.transaction(CRM) as s:
            await orders.update_order_status(s, deps, order.id, status, Actor.user(SARA), tracking_ref="BL-1")
    assert [e["status"] for e in await outbox(databases, ev.ORDER_STATUS_CHANGED, deal_id)] == [
        "in_production",
        "ready",
        "shipped",
        "delivered",
    ]


async def test_won_needs_the_advance(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await agreed_deal(databases, deps)
    async with databases.transaction(CRM) as s:
        with pytest.raises(RuleViolation):
            await orders.mark_won(s, deps, deal_id, Actor.user(SARA), expected_reorder_date=date(2027, 1, 1))


def test_lost_reasons_are_section_15() -> None:
    assert {"budget", "duplicate", "other", "price_too_high", "no_response_postponed"} <= LOST_REASONS


async def test_lost_with_a_new_reason_and_a_note(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await sara_deal(databases, deps)
    async with databases.transaction(CRM) as s:
        deal = await deals.get_deal(s, deal_id, lock=True)
        await deals.move_stage(
            s,
            deal,
            DealStage.LOST,
            Actor.user(SARA),
            now=deps.now(),
            lost_reason="budget",
            lost_note="next year",
        )
        assert (deal.lost_reason, deal.lost_note) == ("budget", "next year")


def test_scenario_9_lost_is_proposed_14_days_after_day_10() -> None:
    monday = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
    assert lost_proposal_due(monday, (2, 5, 10), 14) == monday + timedelta(days=24)


async def test_silent_buyer_gets_a_lost_proposal_task_never_an_automatic_lost(
    databases: Databases, deps: CrmDeps
) -> None:
    deal_id = await sara_deal(databases, deps)
    await approve_and_send(databases, deps, deal_id, SARA)
    later = replace(deps, clock=lambda: datetime.now(UTC) + timedelta(days=25))
    async with databases.transaction(CRM) as s:
        await lost_proposals.propose_lost(s, deps)  # not due yet for this deal
        assert not await _proposals(s, deal_id)
        await lost_proposals.propose_lost(s, later)
        (task,) = await _proposals(s, deal_id)
        assert (task.assigned_user_id, task.payload["lost_reason"]) == (SARA.user_id, "no_response_postponed")
        assert (await deals.get_deal(s, deal_id)).stage == "QUOTATION"  # the salesperson confirms (§15)
        await lost_proposals.propose_lost(s, later)
        assert len(await _proposals(s, deal_id)) == 1  # idempotent


async def test_a_reply_stops_the_lost_proposal(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await sara_deal(databases, deps)
    await approve_and_send(databases, deps, deal_id, SARA)
    async with databases.transaction(CRM) as s:
        s.add(
            m.TimelineEvent(
                deal_id=deal_id,
                kind="message_in",
                summary="thanks, reviewing",
                actor_type="buyer",
                at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
    later = replace(deps, clock=lambda: datetime.now(UTC) + timedelta(days=30))
    async with databases.transaction(CRM) as s:
        await lost_proposals.propose_lost(s, later)
        assert not await _proposals(s, deal_id)


async def _proposals(s: Any, deal_id: uuid.UUID) -> list[m.Task]:
    rows = await s.execute(select(m.Task).where(m.Task.deal_id == deal_id, m.Task.kind == "lost_proposal"))
    return list(rows.scalars())


# ---------------------------------------------------------------- WP12: §17 rows emitted by the CRM
async def test_won_notifies_manager_and_md_and_thanks_the_buyer(databases: Databases, deps: CrmDeps) -> None:
    from conftest import set_setting

    deal_id = await agreed_deal(databases, deps)
    await set_setting(databases, "notifications.order_status_to_buyer", True)
    try:
        async with databases.transaction(CRM) as s:
            await orders.record_payment(
                s, deps, deal_id, Actor.user(SARA), amount=D(100), currency="AED", method="cash"
            )
            await orders.mark_won(
                s,
                deps,
                deal_id,
                Actor.user(SARA),
                expected_reorder_date=deps.now().date() + timedelta(days=30),
            )
    finally:
        await set_setting(databases, "notifications.order_status_to_buyer", False)
    (won,) = await outbox(databases, ev.DEAL_WON, deal_id)
    assert {n["name"] for n in won["notify"]} == {"The Manager", "The MD"}
    assert (won["salesperson_name"], won["buyer_channel"]) == ("Sara", "whatsapp") and won["buyer_address"]
    (created,) = await outbox(databases, ev.ORDER_STATUS_CHANGED, deal_id)
    assert created["notify_buyer"] is True and created["order_number"].startswith("O-")


async def test_opt_out_pauses_the_deal_for_the_salesperson(databases: Databases, deps: CrmDeps) -> None:
    from kx.core.event_payloads import MessageReceived, make_event
    from kx.crm.jobs import HANDLERS, CrmRuntime, dispatch

    deal_id = await sara_deal(databases, deps)
    msg = MessageReceived(
        message_ref=uuid.uuid4(),
        thread_ref=uuid.uuid4(),
        channel="whatsapp",
        from_address_hash="h" * 64,
        contact_ref=None,
        deal_ref=deal_id,
        text="STOP",
        received_at=datetime.now(UTC),
        provider_message_id="wamid.x",
        opted_out=True,
    )

    async def _noop(_: object) -> None:
        return None

    event = make_event(ev.MESSAGE_RECEIVED, "x", uuid.uuid4(), msg)
    await dispatch(CrmRuntime(databases, deps, publish=_noop), event, HANDLERS[ev.MESSAGE_RECEIVED])
    async with databases.transaction(CRM) as s:
        deal = await deals.get_deal(s, deal_id)
        assert (deal.ai_enabled, deal.ai_paused_reason) == (False, "opt_out")
