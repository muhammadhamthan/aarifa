"""Assemble pricing-engine inputs from CRM data: approved material rates only, current cost recipe,
freight lane with its risk buffer, FX, and the MD's margin band. The maths itself is
kx.crm.pricing.engine (pure); this module is the I/O that feeds it.

Staleness is enforced HERE, at quote time: a rate not reviewed for `pricing.stale_block_days` raises
RuleViolation, so quotes stay blocked even if the daily staleness job never runs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.errors import RuleViolation
from kx.core.money import MONEY_PLACES, q
from kx.crm.models import (
    Company,
    Deal,
    DealRequirement,
    DeliveryEstimateRow,
    FreightLane,
    FxRate,
    MarginRuleRow,
    MaterialRate,
    Order,
    Product,
    ProductCosting,
    QuoteLine,
    StockItem,
    SupplierLeadTime,
)
from kx.crm.pricing.engine import estimate_delivery, freight_per_1000, resolve_band
from kx.crm.pricing.types import (
    CostInputs,
    DeliveryInputs,
    FreightInputs,
    MarginBand,
    MarginRule,
    QuoteLineInput,
)
from kx.crm.rules import Staleness, days_since, staleness
from kx.crm.services.settings import BusinessSettings

BASE = "AED"


@dataclass(frozen=True, slots=True)
class PricingInputs:
    """Everything needed to price one requirement and to snapshot its cost (MD-only)."""

    requirement: DealRequirement
    product: Product
    costing: ProductCosting
    rate: MaterialRate
    line: QuoteLineInput  # cost in the quote currency
    band: MarginBand
    lane: FreightLane | None
    staleness: Staleness
    rate_age_days: int

    @property
    def incoterm(self) -> str:
        # T13: unknown port / no lane -> EXW, freight quoted separately later
        return "CFR" if self.lane is not None else "EXW"


# ---------------------------------------------------------------- lookups
async def fx_to_aed(session: AsyncSession, currency: str, on: date) -> Decimal:
    """AED per 1 unit of `currency`, latest published on or before `on`."""
    if currency.upper() == BASE:
        return Decimal(1)
    stmt = (
        select(FxRate.rate_to_aed)
        .where(FxRate.currency == currency.upper(), FxRate.as_of <= on)
        .order_by(FxRate.as_of.desc())
        .limit(1)
    )
    if (rate := await session.scalar(stmt)) is None:
        raise RuleViolation(f"no FX rate for {currency} on or before {on}")
    return rate


async def current_rate(session: AsyncSession, material_code: str) -> MaterialRate | None:
    stmt = (
        select(MaterialRate)
        .where(MaterialRate.material_code == material_code, MaterialRate.status == "approved")
        .order_by(MaterialRate.effective_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def current_costing(session: AsyncSession, product_id: Any, now: datetime) -> ProductCosting:
    stmt = (
        select(ProductCosting)
        .where(ProductCosting.product_id == product_id, ProductCosting.effective_from <= now)
        .order_by(ProductCosting.effective_from.desc())
        .limit(1)
    )
    if (costing := (await session.execute(stmt)).scalar_one_or_none()) is None:
        raise RuleViolation("product has no cost recipe; the MD must enter one before quoting")
    return costing


async def best_lane(session: AsyncSession, country: str | None, port: str | None) -> FreightLane | None:
    """Prefer the buyer's port, then sea, then the lowest war-risk score, then the cheapest container."""
    if country is None:
        return None
    order: list[Any] = [(FreightLane.dest_port == port).desc().nulls_last()] if port else []
    order += [(FreightLane.mode == "sea").desc(), FreightLane.risk_score, FreightLane.container_cost]
    stmt = (
        select(FreightLane)
        .where(
            FreightLane.dest_country == country,
            FreightLane.is_active,
            FreightLane.container_cost.is_not(None),
        )
        .order_by(*order)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def margin_band(
    session: AsyncSession, category: str, customer_type: str | None, quantity: Decimal
) -> MarginBand:
    rows = (
        await session.execute(
            select(MarginRuleRow).where(MarginRuleRow.category_code == category, MarginRuleRow.is_active)
        )
    ).scalars()
    rules = [
        MarginRule(
            rule_id=r.id,
            category_code=r.category_code,
            customer_type=r.customer_type,
            min_qty=r.min_qty,
            max_qty=r.max_qty,
            markup_min_pct=r.markup_min_pct,
            markup_max_pct=r.markup_max_pct,
            max_concessions=r.max_concessions,
            priority=r.priority,
        )
        for r in rows
    ]
    try:
        return resolve_band(rules, category, customer_type, quantity)
    except LookupError as exc:
        raise RuleViolation(f"no margin range set for category {category}; the MD must set one") from exc


# ---------------------------------------------------------------- staleness
def assert_quotable(
    rate: MaterialRate | None, material_code: str, s: BusinessSettings, now: datetime
) -> tuple[Staleness, int]:
    """Block quoting on missing or stale approved rates (warn level is returned for the approver)."""
    if rate is None or rate.decided_at is None:
        raise RuleViolation(
            f"no approved price for material {material_code}; new quotes are blocked",
            material_code=material_code,
        )
    level = staleness(rate.decided_at, now, s.stale_warn_days, s.stale_block_days)
    age = days_since(rate.decided_at, now)
    if level == "block":
        raise RuleViolation(
            f"material price {material_code} not reviewed for {age} days; new quotes are blocked "
            f"until a manager approves a current price",
            material_code=material_code,
            days=age,
        )
    return level, age


# ---------------------------------------------------------------- assembly
def to_currency_inputs(cost: CostInputs, fx: Decimal) -> CostInputs:
    """Convert AED cost components to the quote currency (4 dp, the precision stored in quote_line_costs,
    so negotiation can rebuild the exact cost from the snapshot)."""
    if fx == 1:
        return cost

    def conv(v: Decimal) -> Decimal:
        return q(v / fx, MONEY_PLACES)

    return replace(
        cost,
        material_rate_per_kg=conv(cost.material_rate_per_kg),
        production_per_1000=conv(cost.production_per_1000),
        packaging_per_1000=conv(cost.packaging_per_1000),
        freight_per_1000=conv(cost.freight_per_1000),
    )


async def pricing_inputs(
    session: AsyncSession,
    deal: Deal,
    req: DealRequirement,
    *,
    s: BusinessSettings,
    quote_fx: Decimal,
    now: datetime,
) -> PricingInputs:
    if req.product_id is None or req.quantity is None:
        raise RuleViolation("requirement needs a matched product and a quantity before quoting")
    product = await session.get(Product, req.product_id)
    assert product is not None
    costing = await current_costing(session, product.id, now)
    rate = await current_rate(session, costing.material_code)
    level, age = assert_quotable(rate, costing.material_code, s, now)
    assert rate is not None
    today = now.date()
    rate_aed = rate.rate_per_unit * await fx_to_aed(session, rate.currency, today)
    lane = await best_lane(session, req.delivery_country, req.delivery_port)
    freight = Decimal(0)
    if lane is not None and lane.container_cost is not None and costing.pcs_per_40hc:
        lane_fx = await fx_to_aed(session, lane.currency, today)
        freight = freight_per_1000(
            FreightInputs(
                container_cost=lane.container_cost * lane_fx,
                surcharge=lane.surcharge * lane_fx,
                buffer_pct=lane.buffer_pct,
                pcs_per_40hc=costing.pcs_per_40hc,
            )
        )
    else:
        lane = None
    cost_aed = CostInputs(
        material_rate_per_kg=q(rate_aed, MONEY_PLACES),
        kg_per_1000=costing.kg_per_1000,
        yield_pct=costing.yield_pct,
        production_per_1000=costing.production_cost_per_1000,
        packaging_per_1000=costing.packaging_cost_per_1000,
        freight_per_1000=freight,
    )
    customer_type = None
    if deal.company_id is not None and (company := await session.get(Company, deal.company_id)) is not None:
        customer_type = company.customer_type
    band = await margin_band(session, product.category_code, customer_type, req.quantity)
    line = QuoteLineInput(
        description=_describe(product, req),
        quantity=req.quantity,
        cost=to_currency_inputs(cost_aed, quote_fx),
        unit=product.unit,
        product_id=product.id,
    )
    return PricingInputs(
        requirement=req,
        product=product,
        costing=costing,
        rate=rate,
        line=line,
        band=band,
        lane=lane,
        staleness=level,
        rate_age_days=age,
    )


def _describe(product: Product, req: DealRequirement) -> str:
    spec = ", ".join(f"{k}: {v}" for k, v in sorted(req.spec.items()) if v not in (None, ""))
    return f"{product.name} ({product.sku})" + (f" — {spec}" if spec else "")


def vat_pct(req: DealRequirement, s: BusinessSettings) -> Decimal:
    """UAE VAT on domestic supplies; exports are zero-rated."""
    return s.vat_pct_domestic if (req.delivery_country or "AE") == "AE" else Decimal(0)


# ---------------------------------------------------------------- delivery estimate (T14)
async def build_delivery_estimate(
    session: AsyncSession,
    deal: Deal,
    req: DealRequirement,
    *,
    costing: ProductCosting,
    lane: FreightLane | None,
    now: datetime,
) -> DeliveryEstimateRow:
    """Rule-based estimate (stock, material wait, backlog, production, packing, transit + lane buffer),
    stored with its full input snapshot as training data for the later ML model. Independent of prices,
    so a delivery question is answered even while stale prices block quoting."""
    assert req.quantity is not None
    finished = await _available(session, StockItem.product_id == costing.product_id)
    raw = await _available(session, StockItem.material_code == costing.material_code, raw=True)
    need_kg = req.quantity / 1000 * costing.kg_per_1000 * 100 / costing.yield_pct
    shortfall = raw is None or raw < need_kg  # unknown stock: promise the safer (later) date
    lead = (
        await session.scalar(
            select(func.min(SupplierLeadTime.lead_time_days)).where(
                SupplierLeadTime.material_code == costing.material_code
            )
        )
        or 0
    )
    backlog_qty = await session.scalar(
        select(func.coalesce(func.sum(QuoteLine.quantity), 0))
        .join(Order, Order.final_quote_version_id == QuoteLine.quote_version_id)
        .where(Order.status == "in_production", QuoteLine.product_id == costing.product_id)
    ) or Decimal(0)
    backlog_days = math.ceil(backlog_qty / costing.pieces_per_day)
    di = DeliveryInputs(
        today=now.date(),
        quantity=req.quantity,
        pieces_per_day=costing.pieces_per_day,
        finished_stock=finished or Decimal(0),
        backlog_days=backlog_days,
        material_shortfall=shortfall,
        supplier_lead_days=int(lead),
        packing_days=costing.packing_days,
        transit_days=lane.transit_days if lane else 0,
        buffer_days=lane.buffer_days if lane else 0,
    )
    est = estimate_delivery(di)
    row = DeliveryEstimateRow(
        deal_id=deal.id,
        lane_id=lane.id if lane else None,
        backlog_days=di.backlog_days,
        production_days=est.production_days,
        material_wait_days=est.material_wait_days,
        packing_days=di.packing_days,
        transit_days=di.transit_days,
        buffer_days=di.buffer_days,
        estimated_ready=est.ready_date,
        estimated_delivery=est.delivery_date,
        inputs={
            "today": di.today.isoformat(),
            "quantity": str(di.quantity),
            "pieces_per_day": di.pieces_per_day,
            "finished_stock": str(di.finished_stock),
            "backlog_days": di.backlog_days,
            "material_shortfall": di.material_shortfall,
            "supplier_lead_days": di.supplier_lead_days,
            "packing_days": di.packing_days,
            "transit_days": di.transit_days,
            "buffer_days": di.buffer_days,
            "product_id": str(costing.product_id),
            "material_code": costing.material_code,
            "lane_code": lane.code if lane else None,
            "lane_risk_score": lane.risk_score if lane else None,
        },
    )
    session.add(row)
    await session.flush()
    return row


async def _available(session: AsyncSession, cond: Any, *, raw: bool = False) -> Decimal | None:
    item_type = "raw" if raw else "finished"
    row = (
        await session.execute(
            select(StockItem.quantity, StockItem.reserved_quantity).where(
                StockItem.item_type == item_type, cond
            )
        )
    ).first()
    return None if row is None else max(row[0] - row[1], Decimal(0))
