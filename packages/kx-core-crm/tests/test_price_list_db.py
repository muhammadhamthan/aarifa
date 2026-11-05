"""WP2 on real Postgres: v1 price-list quotes and negotiation (docs/BUSINESS_FLOW_V1.md §10-12, §23).

Done when (V1_CHANGE_PLAN WP2): quotes are built from tiers with FX and VAT, the staleness block is skipped,
and the negotiation state is no longer built from cost in price-list mode.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from conftest import LIST_PRODUCT_ID, LIST_SKU, MGR_ID, PRICE_LIST_FROM, new_phone, set_setting
from sqlalchemy import func, select, update
from test_flow_db import MGR, approve_and_send, open_approval, outbox

from kx.core import events as ev
from kx.core.contracts import AiRole, ContactPatch, CounterVerdictKind, DealStage, RequirementPatch
from kx.core.db import Databases
from kx.core.money import display
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl
from kx.crm.services import deals, quotes
from kx.crm.services.pricing import PricingNeeded, QuantityOutsideTiers

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("price_list")]
CRM = LogicalDb.CRM
AI = AiRole.QUOTATION
K = CounterVerdictKind


async def list_deal(
    gw: CrmGatewayImpl, *, country: str = "AE", qty: int = 50_000, spec: dict[str, object] | None = None
) -> uuid.UUID:
    deal_id = await gw.resolve_deal("whatsapp", new_phone(), None)
    await gw.update_requirement(
        deal_id,
        RequirementPatch(
            product_sku=LIST_SKU, quantity=D(qty), delivery_country=country, spec=spec or {"lid": "yes"}
        ),
        AI,
    )
    await gw.set_contact(deal_id, ContactPatch(name="Ahmed", company="Gulf Catering"), AiRole.CONTACT_CAPTURE)
    assert (await gw.try_qualify(deal_id, AiRole.LEAD_QUALIFICATION)).qualified
    return deal_id


async def _version(dbs: Databases, deal_id: uuid.UUID) -> m.QuoteVersion:
    async with dbs.transaction(CRM) as s:
        v = await quotes.latest_version(s, deal_id)
        assert v is not None
        return v


# ---------------------------------------------------------------- §11.1 the quote
async def test_dubai_quote_from_the_price_list(databases: Databases, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw)
    await gw.request_quote(deal_id, AI)
    async with databases.transaction(CRM) as s:
        v = await quotes.latest_version(s, deal_id)
        assert v is not None and v.status == "pending_approval" and v.kind == "initial"
        ((line, snap),) = await quotes.tiers_of(s, v.id)
        assert (line.unit_price, line.line_total) == (D("0.25"), D("12500.0000"))
        assert (display(v.subtotal, "AED"), display(v.vat_amount, "AED"), display(v.total, "AED")) == (
            D("12500.00"),
            D("625.00"),
            D("13125.00"),
        )
        assert (v.incoterm, v.delivery_time_text, v.fx_rate, v.delivery_estimate_id) == (
            "EXW",
            "10–14 working days after advance payment",
            None,
            None,
        )
        assert v.payment_terms_text == "Advance payment is required before production starts."
        assert snap is not None and (snap.list_price, snap.min_price, snap.max_steps) == (
            D("0.25"),
            D("0.23"),
            3,
        )
        assert await quotes.costs_of(s, v.id) == []  # no cost snapshot in price-list mode
        assert v.approver_summary and "minimum AED 0.23" in v.approver_summary
    submitted = (await outbox(databases, ev.QUOTE_SUBMITTED, deal_id))[-1]
    assert submitted["total_display"] == "AED 13,125.00"


async def test_riyadh_export_in_usd_at_the_printed_rate(databases: Databases, deps: CrmDeps) -> None:
    await set_setting(databases, "quote.currency_by_country", {"SA": "USD"})
    try:
        gw = CrmGatewayImpl(databases, deps)
        deal_id = await list_deal(gw, country="SA")
        await gw.request_quote(deal_id, AI)
        v = await _version(databases, deal_id)
        assert (v.currency, v.vat_rate, display(v.total, "USD")) == ("USD", D(0), D("3403.68"))
        assert (v.fx_rate, v.fx_rate_to_aed, v.fx_rate_date) == (
            D("3.6725"),
            D("3.6725"),
            datetime(2026, 1, 1).date(),
        )
        async with databases.transaction(CRM) as s:
            ((line, snap),) = await quotes.tiers_of(s, v.id)
            assert line.unit_price == D("0.068074") and snap is not None and snap.min_price == D("0.062628")
            assert (await deals.get_deal(s, deal_id)).currency == "USD"
    finally:
        await set_setting(databases, "quote.currency_by_country", {})


async def test_no_staleness_block_in_price_list_mode(databases: Databases, deps: CrmDeps) -> None:
    """D14: the product has no cost recipe and no material rate at all; v1 still quotes it."""
    async with databases.transaction(CRM) as s:
        assert await s.scalar(select(func.count()).where(m.ProductCosting.product_id == LIST_PRODUCT_ID)) == 0
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw)
    await gw.request_quote(deal_id, AI)
    assert (await _version(databases, deal_id)).status == "pending_approval"


@pytest.mark.parametrize(
    ("qty", "spec", "error"),
    [
        (3_000, None, QuantityOutsideTiers),  # below the MOQ / lowest tier (D12)
        (50_000, {"printing": "2-colour logo on the lid"}, PricingNeeded),  # §23 scenario 5 (D8)
    ],
)
async def test_quotes_the_ai_may_not_price(
    databases: Databases, deps: CrmDeps, qty: int, spec: dict[str, object] | None, error: type[Exception]
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw, qty=qty, spec=spec)
    with pytest.raises(error):
        await gw.request_quote(deal_id, AI)


# ---------------------------------------------------------------- §12 negotiation on the ladder
async def test_scenario_2_negotiation_with_a_quantity_increase(databases: Databases, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw)
    await gw.request_quote(deal_id, AI)
    await approve_and_send(databases, deps, deal_id, MGR)
    ctx = await gw.deal_context(deal_id)
    assert ctx.concessions_left == 3
    assert {p.amount for p in ctx.allowed_prices} >= {D("0.25"), D("12500.0000")}
    for secret in (D("0.23"), D("0.2433"), D("0.2367")):  # minimum and steps: not before they are offered
        assert secret not in {p.amount for p in ctx.allowed_prices}

    v1 = await gw.evaluate_counter(deal_id, D("0.21"), None, "whatsapp")  # below the minimum, 1st time
    assert (v1.kind, v1.offer_unit_price, v1.reason) == (K.COUNTER, D("0.2433"), "ask_for_quantity")
    assert (await gw.deal_context(deal_id)).stage is DealStage.NEGOTIATION
    v2 = await gw.evaluate_counter(deal_id, D("0.23"), D(70_000), "whatsapp")  # 70,000 at 0.23
    assert (v2.kind, v2.offer_unit_price, v2.traded_for) == (K.COUNTER, D("0.2367"), "quantity_increase")
    v3 = await gw.evaluate_counter(deal_id, D("0.2367"), D(70_000), "whatsapp")  # deal at 0.2367
    assert (v3.kind, v3.offer_unit_price) == (K.ACCEPT, D("0.2367"))
    ctx = await gw.deal_context(deal_id)
    assert ctx.ai_enabled and ctx.concessions_left == 1
    assert D("0.23") not in {p.amount for p in ctx.allowed_prices}  # the floor was never offered

    await gw.mark_agreed(deal_id, AiRole.DEAL_CLOSER)
    async with databases.transaction(CRM) as s:
        v = await quotes.latest_version(s, deal_id)
        assert v is not None and v.kind == "final"
        ((line, snap),) = await quotes.tiers_of(s, v.id)
        assert (line.quantity, line.unit_price, display(line.line_total, "AED")) == (
            D(70_000),
            D("0.2367"),
            D("16569.00"),
        )
        assert snap is not None and snap.min_price == D("0.23")
        assert v.delivery_time_text == "10–14 working days after advance payment"
        deal = await deals.get_deal(s, deal_id)
        assert (deal.stage, deal.concessions_used, deal.low_ask_count) == ("READY_TO_CLOSE", 2, 1)
    assert (await open_approval(databases, deal_id)).kind == "final"


async def test_scenario_3_insisting_below_the_minimum_pauses_the_ai(
    databases: Databases, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw)
    await gw.request_quote(deal_id, AI)
    await approve_and_send(databases, deps, deal_id, MGR)
    assert (await gw.evaluate_counter(deal_id, D("0.21"), None, "whatsapp")).kind is K.COUNTER
    v = await gw.evaluate_counter(deal_id, D("0.20"), None, "whatsapp")  # insists below the minimum
    assert (v.kind, v.offer_unit_price, v.reason) == (K.NEEDS_MANAGER, None, "below_range")
    paused = (await outbox(databases, ev.NEGOTIATION_PAUSED, deal_id))[-1]
    assert paused["reason"] == "below_range"
    async with databases.transaction(CRM) as s:
        deal = await deals.get_deal(s, deal_id)
        assert (deal.ai_enabled, deal.ai_paused_reason, deal.low_ask_count) == (False, "below_range", 2)


async def test_all_steps_used_then_lower_pauses_with_concessions_exhausted(
    databases: Databases, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw, qty=50_000)
    await gw.request_quote(deal_id, AI)
    await approve_and_send(databases, deps, deal_id, MGR)
    for ask, qty, offer in (
        (D("0.24"), 50_000, "0.2433"),
        (D("0.235"), 60_000, "0.2367"),
        (D("0.23"), 70_000, "0.23"),
    ):
        v = await gw.evaluate_counter(deal_id, ask, D(qty), "whatsapp")
        assert v.offer_unit_price == D(offer)
    v = await gw.evaluate_counter(deal_id, D("0.20"), D(70_000), "whatsapp")  # §23 scenario 3, step 1
    assert (v.kind, v.reason, v.concessions_left) == (K.NEEDS_MANAGER, "concessions_exhausted", 0)
    async with databases.transaction(CRM) as s:
        deal = await deals.get_deal(s, deal_id)
        assert (deal.ai_paused_reason, deal.concessions_used) == ("concessions_exhausted", 3)


async def test_quantity_into_a_higher_tier_drafts_a_revised_quote_for_approval(
    databases: Databases, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw, qty=20_000)  # 10,000-49,999 tier: AED 0.28
    await gw.request_quote(deal_id, AI)
    await approve_and_send(databases, deps, deal_id, MGR)
    v = await gw.evaluate_counter(deal_id, D("0.24"), D(50_000), "whatsapp")
    # the AI gets no price: the 0.25 revision waits for the salesperson (§12.2 rule 5)
    assert (v.kind, v.offer_unit_price, v.requires_approval) == (K.REQUOTE, None, True)
    async with databases.transaction(CRM) as s:
        rev = await quotes.latest_version(s, deal_id)
        assert rev is not None and (rev.kind, rev.status, rev.version_no) == (
            "revision",
            "pending_approval",
            2,
        )
        ((line, _),) = await quotes.tiers_of(s, rev.id)
        assert (line.quantity, line.unit_price) == (D(50_000), D("0.25"))
        assert (await deals.get_deal(s, deal_id)).stage == "QUOTATION"
    ctx = await gw.deal_context(deal_id)
    assert ctx.latest_quote is None and ctx.allowed_prices == ()  # nothing issued until approved


async def test_open_quote_keeps_its_prices_when_the_price_list_changes(
    databases: Databases, deps: CrmDeps
) -> None:
    """§24: a new price-list version (newer valid_from) affects new quotes only."""
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw)
    await gw.request_quote(deal_id, AI)
    await approve_and_send(databases, deps, deal_id, MGR)
    async with databases.transaction(CRM) as s:
        s.add(
            m.ProductPriceTier(
                product_id=LIST_PRODUCT_ID,
                min_qty=D(50_000),
                list_price=D("0.26"),
                min_price=D("0.20"),
                valid_from=datetime.now(UTC),
                created_by=MGR_ID,
            )
        )
    try:
        v = await gw.evaluate_counter(deal_id, D("0.24"), None, "whatsapp")
        assert v.offer_unit_price == D("0.2433")  # still the 0.25 / 0.23 ladder of the open quote
        new_deal = await list_deal(gw)
        await gw.request_quote(new_deal, AI)
        async with databases.transaction(CRM) as s:
            nv = await quotes.latest_version(s, new_deal)
            assert nv is not None
            ((line, _),) = await quotes.tiers_of(s, nv.id)
            assert line.unit_price == D("0.26")
    finally:  # quotes reference the tier now, so park that price-list version in the far future
        async with databases.transaction(CRM) as s:
            await s.execute(
                update(m.ProductPriceTier)
                .where(
                    m.ProductPriceTier.product_id == LIST_PRODUCT_ID,
                    m.ProductPriceTier.valid_from > PRICE_LIST_FROM,
                )
                .values(valid_from=datetime(2999, 1, 1, tzinfo=UTC))
            )


async def test_deal_context_never_carries_the_minimum(databases: Databases, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await list_deal(gw)
    await gw.request_quote(deal_id, AI)
    await approve_and_send(databases, deps, deal_id, MGR)
    await gw.evaluate_counter(deal_id, D("0.24"), None, "whatsapp")
    dumped = (await gw.deal_context(deal_id)).model_dump_json().lower()
    for word in ("minimum", "min_price", "floor", "ladder", "tier", "cost", "markup"):
        assert word not in dumped, word
    for secret in ('"0.23"', '"0.2300"', '"0.2367"', '"0.230000"'):
        assert secret not in dumped, secret
