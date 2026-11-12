"""Safety rules of the 5 Nov flow, end to end (the SRS §3, §12.2 rules 6 and 8, §13, §18;
CONTRACTS.md §3)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal as D

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from kx.agents.graphs.deal import BuyerAsk
from kx.agents.orchestrator import PAYMENT_TOPIC, route
from kx.agents.testing import make_deal
from kx.core.auth import Principal, Role
from kx.core.contracts import AllowedPrice, DealStage
from kx.core.db import Databases
from kx.core.errors import Forbidden
from kx.core.identity import recipient_hash
from kx.core.price_guard import check_prices
from kx.crm import models as m
from kx.crm.gateway import GrantConsumerImpl
from kx.crm.services import access, approvals, assignment, quotes
from kx.crm.services.journal import Actor

from .world import CRM, World, crm_conftest, dubai

C = crm_conftest()
SARA = Principal(str(C.SALES_ID), C.SALES_ID, Role.SALES, mfa=True, is_human=True)
RAVI = Principal(str(C.SALES2_ID), C.SALES2_ID, Role.SALES, mfa=True, is_human=True)
MD = Principal(str(C.MD_ID), C.MD_ID, Role.MD, mfa=True, is_human=True)
SECRETS = ("0.23", "0.2300", "0.230000", "0.2433", "0.2367", "0.255", "0.29")  # minimums and un-offered steps


@pytest.fixture
async def world(databases: Databases) -> AsyncIterator[World]:
    await C.ensure_price_list(databases)
    yield World(databases, dubai(2026, 11, 10, 10, 0))


async def issued_deal(w: World, qty: int = 50_000) -> uuid.UUID:
    deal = await w.new_lead(qty=qty)
    await w.ai(
        deal,
        "Please send me the price.",
        ["Reply any time if you plan bigger volumes.", "Your quotation is being prepared."],
    )
    a = await w.open_approval(deal)
    async with w.dbs.transaction(CRM) as s:
        await approvals.decide(s, w.deps, a.id, SARA, decision="approve")
    await w.pump()
    return deal


# ---------------------------------------------------------------- the AI never sees or says the minimum
@pytest.mark.db
async def test_ai_never_sees_the_minimum_cost_or_steps(world: World) -> None:
    w = world
    deal = await issued_deal(w)
    for step in range(3):
        dumped = (await w.gw.deal_context(deal)).model_dump_json().lower()
        for word in ("min_price", "minimum", "floor", "ladder", "tier", "cost", "markup", "margin"):
            assert word not in dumped, (step, word)
        offered = {p.amount for p in (await w.gw.deal_context(deal)).allowed_prices}
        assert D("0.23") not in offered  # the floor is never an allowed price before it is offered
        await w.ai(
            deal,
            "Can you do 0.24?",
            [BuyerAsk(intent="counter", ask_unit_price=D("0.24")), "I can offer AED 0.2433 per piece."],
        )


@pytest.mark.db
async def test_a_reply_naming_the_minimum_is_blocked(world: World) -> None:
    w = world
    deal = await issued_deal(w)
    ctx = await w.gw.deal_context(deal)
    for leak in (
        "Our floor is AED 0.23 per piece.",
        "The minimum is 0.23 AED.",
        "Next step would be AED 0.2367.",
    ):
        assert not check_prices(leak, ctx.allowed_prices, default_currency="AED").ok, leak
    reply = await w.ai(
        deal,
        "What's the lowest you can go?",
        [
            BuyerAsk(intent="question"),
            "Our floor is AED 0.23 per piece.",
            "Still AED 0.2300.",
        ],
    )
    assert reply == "Our team will confirm pricing shortly."  # leaked twice -> the fixed fallback


@settings(max_examples=200, deadline=None)
@given(st.text(min_size=0, max_size=80))
def test_guard_never_lets_an_unlisted_amount_through(noise: str) -> None:
    allowed = (
        AllowedPrice(amount=D("0.25"), currency="AED"),
        AllowedPrice(amount=D("12500"), currency="AED"),
    )
    text = f"{noise} AED 0.23 {noise}"
    assert not check_prices(text, allowed, default_currency="AED").ok


# ---------------------------------------------------------------- payment always goes to a person
@pytest.mark.parametrize(
    "text",
    [
        "What are your payment terms?",
        "Can we do 30% advance and the rest on delivery?",
        "Send your bank details please",
        "Can you give us 60 days credit?",
        "We pay by letter of credit",
        "كيف يتم الدفع؟",
    ],
)
@pytest.mark.parametrize(
    "stage", [DealStage.NEW, DealStage.QUALIFIED, DealStage.QUOTATION, DealStage.NEGOTIATION]
)
def test_payment_topics_route_to_the_change_request_path_in_every_open_stage(
    text: str, stage: DealStage
) -> None:
    assert PAYMENT_TOPIC.search(text)
    rt = route(text, make_deal(stage, allowed=[("0.25", "unit")], unit_price="0.25"))
    assert (rt.graph.value if rt.graph else None, rt.reason) == ("deal", "payment_to_salesperson")


@pytest.mark.db
async def test_payment_question_becomes_a_payment_change_request_and_no_price(world: World) -> None:
    w = world
    deal = await issued_deal(w)
    reply = await w.ai(
        deal, "Can we pay 30% advance and the balance on delivery?", [BuyerAsk(intent="question")]
    )
    assert reply and "within 4 working hours" in reply
    assert await w.sql("SELECT type FROM crm.change_requests WHERE deal_id = :d", d=deal) == [("payment",)]


# ---------------------------------------------------------------- approvals and takeover
@pytest.mark.db
async def test_a_salesperson_cannot_approve_another_salespersons_deal(world: World) -> None:
    w = world
    deal = await w.new_lead(qty=50_000)
    await w.ai(deal, "Please send me the price.", ["Reply any time.", "Your quotation is being prepared."])
    a = await w.open_approval(deal)
    with pytest.raises(Forbidden):
        async with w.dbs.transaction(CRM) as s:
            await approvals.decide(s, w.deps, a.id, RAVI, decision="approve")
    async with w.dbs.transaction(CRM) as s:
        await approvals.decide(s, w.deps, a.id, SARA, decision="approve")


@pytest.mark.db
async def test_the_owner_is_read_only_during_an_md_takeover(world: World) -> None:
    w = world
    deal = await issued_deal(w)
    async with w.dbs.transaction(CRM) as s:
        await assignment.take_over(s, w.deps, deal, MD)
    async with w.dbs.transaction(CRM) as s:
        row = await quotes.deal_svc.get_deal(s, deal)
        with pytest.raises(Forbidden, match="taken over"):
            await access.check_edit(s, SARA, row)
        await access.check_view(s, SARA, row)  # still allowed to look
        await assignment.hand_back(s, w.deps, deal, MD)
    async with w.dbs.transaction(CRM) as s:
        await access.check_edit(s, SARA, await quotes.deal_svc.get_deal(s, deal))
        await quotes.revise_quote(
            s, w.deps, deal, quotes.QuoteEdits(delivery_place="Dubai"), Actor.user(SARA)
        )


# ---------------------------------------------------------------- grants
@pytest.mark.db
async def test_each_grant_is_single_use(world: World) -> None:
    w = world
    deal = await w.new_lead(qty=50_000)
    await w.ai(deal, "Please send me the price.", ["Reply any time.", "Your quotation is being prepared."])
    a = await w.open_approval(deal)
    async with w.dbs.transaction(CRM) as s:
        await approvals.decide(s, w.deps, a.id, SARA, decision="approve")
    async with w.dbs.transaction(CRM) as s:  # nothing relayed yet: use the grant ourselves first
        g = (
            await s.execute(
                select(m.QuoteSendGrant)
                .join(m.QuoteVersion, m.QuoteVersion.id == m.QuoteSendGrant.quote_version_id)
                .join(m.Quote, m.Quote.id == m.QuoteVersion.quote_id)
                .where(m.Quote.deal_id == deal)
            )
        ).scalar_one()
        contact = await quotes.deal_svc.contact_of(s, await quotes.deal_svc.get_deal(s, deal))
    consumer = GrantConsumerImpl(w.dbs, w.deps)
    rcpt = recipient_hash("whatsapp", contact.phone_e164 or "")
    first = uuid.uuid4()
    assert await consumer.consume_grant(g.id, g.content_sha256, first, rcpt)
    assert await consumer.consume_grant(g.id, g.content_sha256, first, rcpt)  # the same message may retry
    assert not await consumer.consume_grant(g.id, g.content_sha256, uuid.uuid4(), rcpt)  # never a second
    assert not await consumer.consume_grant(g.id, "0" * 64, uuid.uuid4(), rcpt)  # other content
    await w.pump()  # automation tries to send the approved quote: refused, the grant is used up
    assert w.sender.refused and not [
        r for r in w.sender.sent if r.kind.value == "quote" and r.deal_ref == deal
    ]
