"""§14 same-number hand-over (11 Nov plan WP17, D21, D23): the salesperson replies from the CRM inbox,
calls through the company number, and the deal page shows the AI summary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import SALES2_ID, SALES_ID, Seed, assign, new_phone

from kx.core import events as ev
from kx.core.auth import Principal, Role
from kx.core.contracts import AiRole, ContactPatch, OutboundKind, OutboundRequest
from kx.core.errors import Forbidden, RuleViolation
from kx.core.event_payloads import ConversationSummaryUpdated, make_event
from kx.core.settings import LogicalDb
from kx.crm import jobs as crm_jobs
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl
from kx.crm.models import Deal, TimelineEvent
from kx.crm.services import deals as deal_svc
from kx.crm.services import handover

pytestmark = pytest.mark.db
SARA = Principal(str(SALES_ID), SALES_ID, Role.SALES, mfa=True, is_human=True)
RAVI = Principal(str(SALES2_ID), SALES2_ID, Role.SALES, mfa=True, is_human=True)


@dataclass
class Sender:
    refuse: str | None = None
    sent: list[OutboundRequest] = field(default_factory=list)

    async def send(self, request: OutboundRequest) -> uuid.UUID:
        if self.refuse:
            raise RuleViolation(f"message refused: {self.refuse}", reason=self.refuse)
        self.sent.append(request)
        return uuid.uuid5(uuid.NAMESPACE_URL, request.idempotency_key)


@dataclass
class Caller:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def bridge(self, **kw: Any) -> uuid.UUID:
        self.calls.append(kw)
        return uuid.uuid4()


@dataclass
class Env:
    databases: Any
    deps: CrmDeps
    sender: Sender
    caller: Caller
    deal_id: uuid.UUID


@pytest.fixture
async def env(databases: Any, seed: Seed, deps: CrmDeps) -> Env:
    sender, caller = Sender(), Caller()
    wired = replace(deps, sender=sender, caller=caller)
    gw = CrmGatewayImpl(databases, wired)
    deal_id = await gw.resolve_deal("whatsapp", new_phone(), None)
    await gw.set_contact(
        deal_id, ContactPatch(name="Omar", email="omar@buyer.example"), AiRole.CONTACT_CAPTURE
    )
    await assign(databases, deal_id, SALES_ID)
    return Env(databases, wired, sender, caller, deal_id)


async def reply(env: Env, p: Principal = SARA, **kw: Any) -> uuid.UUID:
    kw.setdefault("client_key", uuid.uuid4().hex)
    kw.setdefault("text", "Thanks Omar, I will send the PO details shortly.")
    async with env.databases.transaction(LogicalDb.CRM) as s:
        deal = await deal_svc.get_deal(s, env.deal_id, lock=True)
        return await handover.reply(s, env.deps, deal, p, **kw)


async def deal_row(env: Env) -> Deal:
    async with env.databases.transaction(LogicalDb.CRM) as s:
        return await deal_svc.get_deal(s, env.deal_id)


async def timeline(env: Env, kind: str) -> list[TimelineEvent]:
    from sqlalchemy import select

    async with env.databases.transaction(LogicalDb.CRM) as s:
        stmt = select(TimelineEvent).where(TimelineEvent.deal_id == env.deal_id, TimelineEvent.kind == kind)
        return list((await s.execute(stmt)).scalars())


async def test_whatsapp_reply_is_signed_goes_through_the_gate_and_stops_the_ai(env: Env) -> None:
    message_id = await reply(env)
    (sent,) = env.sender.sent
    assert sent.channel == "whatsapp" and sent.kind is OutboundKind.STAFF_REPLY
    assert sent.body is not None and sent.body.endswith("— Sara, Arifaa Pack (test)")
    assert sent.sender_name is None and sent.deal_ref == env.deal_id
    deal = await deal_row(env)
    assert not deal.ai_enabled and deal.ai_paused_reason == "human_takeover"
    (line,) = await timeline(env, "message_out")
    assert line.ref_id == str(message_id) and line.direction == "out" and line.summary.startswith("Sara:")


async def test_a_double_tap_sends_once(env: Env) -> None:
    key = uuid.uuid4().hex
    first, second = await reply(env, client_key=key), await reply(env, client_key=key)
    assert first == second
    assert {r.idempotency_key for r in env.sender.sent} == {f"staff-reply:{env.deal_id}:{key}"}


async def test_a_price_outside_an_approved_quote_is_refused_before_anything_leaves(env: Env) -> None:
    with pytest.raises(RuleViolation) as err:
        await reply(env, text="Special for you: AED 9.50 per piece")
    assert err.value.extra["reason"] == "unapproved_price" and env.sender.sent == []
    assert (await deal_row(env)).ai_enabled  # nothing happened


async def test_payment_terms_in_percent_are_for_people_to_discuss(env: Env) -> None:
    """§14 step 7 / §15: advance and balance terms are discussed by humans; only amounts are guarded."""
    await reply(env, text="We need a 30% advance before production starts.")
    assert len(env.sender.sent) == 1


async def test_only_the_deals_salesperson_or_a_manager_may_write(env: Env) -> None:
    with pytest.raises(Forbidden):
        await reply(env, RAVI)
    assert env.sender.sent == []


async def test_after_24_hours_the_reopen_template_restarts_the_chat(env: Env) -> None:
    env.sender.refuse = "window_closed"
    with pytest.raises(RuleViolation) as err:
        await reply(env)
    assert err.value.extra["reason"] == "window_closed"
    assert (await deal_row(env)).ai_enabled and await timeline(env, "message_out") == []
    env.sender.refuse = None
    await reply(env, text="", reopen_template=True)
    (sent,) = env.sender.sent
    assert sent.template_name == "staff_reopen" and sent.body is None
    assert sent.template_params == ("Sara", "Arifaa Pack (test)")


async def test_email_reply_uses_the_company_address_with_the_salespersons_name(env: Env) -> None:
    await reply(env, channel="email", text="Please find the PO details below.")
    (sent,) = env.sender.sent
    assert sent.channel == "email" and sent.to_address == "omar@buyer.example"
    assert sent.sender_name == "Sara, Arifaa Pack (test)" and sent.body == "Please find the PO details below."
    assert sent.subject is not None and (await deal_row(env)).number in sent.subject


async def test_an_opted_out_buyer_gets_nothing(env: Env) -> None:
    async with env.databases.transaction(LogicalDb.CRM) as s:
        deal = await deal_svc.get_deal(s, env.deal_id, lock=True)
        deal.opted_out_at = datetime.now(UTC)
    with pytest.raises(RuleViolation):
        await reply(env)
    assert env.sender.sent == []


async def test_click_to_call_rings_through_the_company_number_and_is_logged(env: Env) -> None:
    async with env.databases.transaction(LogicalDb.CRM) as s:
        deal = await deal_svc.get_deal(s, env.deal_id)
        call_id = await handover.call_buyer(s, env.deps, deal, SARA, client_key="tap-1")
    (call,) = env.caller.calls
    assert call["staff_e164"] == "+971501110003" and call["deal_ref"] == env.deal_id
    assert call["idempotency_key"] == f"click-to-call:{env.deal_id}:tap-1"
    (line,) = await timeline(env, "call")
    assert line.ref_id == str(call_id) and line.channel == "phone"


async def test_ai_summary_lands_on_the_deal_without_bumping_its_version(env: Env) -> None:
    before = await deal_row(env)
    now = datetime.now(UTC)

    async def publish(text: str, at: datetime) -> None:
        payload = ConversationSummaryUpdated(deal_ref=env.deal_id, summary=text, at=at)
        event = make_event(ev.CONVERSATION_SUMMARY_UPDATED, "deal", env.deal_id, payload)
        async with env.databases.transaction(LogicalDb.CRM) as s:
            await crm_jobs.on_summary_updated(s, env.deps, event)

    await publish("Wants 50,000 containers to Colombo; waiting for the quote.", now)
    await publish("An older summary that arrived late.", now - timedelta(minutes=5))
    after = await deal_row(env)
    assert after.ai_summary == "Wants 50,000 containers to Colombo; waiting for the quote."
    assert after.version == before.version  # staff edits are not refused because the AI re-summarised
