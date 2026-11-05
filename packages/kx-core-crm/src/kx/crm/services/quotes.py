"""Quotes: build a priced version from the deal's requirements, freeze it, and open approval 1.

A deal has one quote (one number, Q-YYYY-NNNNN) with numbered versions. Each new version supersedes
the open ones (their approvals are cancelled and their grants revoked) so only one version can ever be
approved and sent. Lines are inserted while the version is 'draft'; the DB freezes them the moment
the version moves to 'pending_approval'.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.contracts import DealStage
from kx.core.errors import RuleViolation
from kx.core.identity import new_id
from kx.core.money import MONEY_PLACES, display, q
from kx.crm.deps import CrmDeps
from kx.crm.models import (
    Approval,
    Deal,
    DealRequirement,
    Product,
    Quote,
    QuoteLine,
    QuoteLineCost,
    QuoteLineTier,
    QuoteVersion,
)
from kx.crm.pricing.engine import price_line, quote_totals
from kx.crm.pricing.price_list import TierPrice, convert_tier, min_price_for, resolve_tier
from kx.crm.pricing.types import PricedLine
from kx.crm.rules import QUOTABLE_STAGES
from kx.crm.services import approvals, grants
from kx.crm.services import deals as deal_svc
from kx.crm.services.issuing import lines_of, local_today
from kx.crm.services.journal import Actor, Timeline, record
from kx.crm.services.pricing import (
    ListPricingInputs,
    PricingInputs,
    build_delivery_estimate,
    category_min_pct,
    current_tiers,
    fx_to_aed,
    fx_with_date,
    lead_time_text,
    list_pricing_inputs,
    pricing_inputs,
    vat_pct,
)
from kx.crm.services.settings import BusinessSettings

VersionKind = Literal["initial", "revision", "counter", "final"]
ISSUED: Final = ("approved", "sent")
LIVE: Final = ("draft", "pending_approval", "overdue", "approved", "sent", "correction_requested")

CLAUSES: Final[dict[str, tuple[str, ...]]] = {
    "en": (
        "Advance payment is required before production starts.",
        "Prices are valid for {days} days from the quotation date.",
        "Delivery dates are estimates and are confirmed by our team when the order is placed.",
        "Ex works (EXW) factory; freight can be quoted separately.",
    ),
    "ar": (
        "يلزم دفع مقدم قبل بدء الإنتاج.",
        "الأسعار صالحة لمدة {days} أيام من تاريخ عرض السعر.",
        "مواعيد التسليم تقديرية ويؤكدها فريقنا عند تأكيد الطلب.",
        "تسليم من المصنع (EXW)؛ يمكن تسعير الشحن بشكل منفصل.",
    ),
}


@dataclass(frozen=True, slots=True)
class LineValues:
    """The buyer-visible values of one quote line (price-list mode has no cost or markup)."""

    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal
    product_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DraftLine:
    """A priced line plus its staff-only snapshot, ready to insert: the cost snapshot (v2 cost mode) or
    the price-list snapshot (v1: list price, minimum, steps)."""

    priced: PricedLine | LineValues
    cost: QuoteLineCost | None = None
    tier: QuoteLineTier | None = None


@dataclass(frozen=True, slots=True)
class VersionTerms:
    """§11.2 delivery and terms, D10 default delivery time, D11 signature, §10 printed FX line."""

    delivery_time_text: str | None = None
    delivery_place: str | None = None
    payment_terms_text: str | None = None
    fx_rate: Decimal | None = None
    fx_rate_date: date | None = None
    signed_by_user_id: uuid.UUID | None = None


def clauses_for(language: str, validity_days: int, exw: bool) -> list[str]:
    texts = CLAUSES.get(language, CLAUSES["en"])
    chosen = texts[:3] + (texts[3:] if exw else ())
    return [t.format(days=validity_days) for t in chosen]


def _cost_snapshot(inp: PricingInputs, pl: PricedLine) -> QuoteLineCost:
    c = inp.line.cost
    return QuoteLineCost(
        material_rate_id=inp.rate.id,
        material_rate=c.material_rate_per_kg,
        kg_per_1000=c.kg_per_1000,
        yield_pct=c.yield_pct,
        production_per_1000=c.production_per_1000,
        packaging_per_1000=c.packaging_per_1000,
        freight_per_1000=c.freight_per_1000,
        unit_cost=pl.unit_cost,
        markup_pct=pl.markup_pct,
        margin_rule_id=inp.band.rule_id,
        markup_min_pct=inp.band.markup_min_pct,
        markup_max_pct=inp.band.markup_max_pct,
    )


def _approver_summary(
    inputs: Sequence[PricingInputs],
    drafts: Sequence[DraftLine],
    currency: str,
    total: Decimal,
    estimate: object,
) -> str:
    """Internal summary for the approver (manager/MD may see markup; the buyer and AI never do)."""
    parts = []
    for inp, d in zip(inputs, drafts, strict=True):
        p = d.priced
        assert isinstance(p, PricedLine)  # cost mode only
        warn = " — PRICE REVIEW DUE" if inp.staleness == "warn" else ""
        parts.append(
            f"{p.quantity:,.0f} x {inp.product.sku} @ {currency} {p.unit_price} (markup {p.markup_pct}% "
            f"of {inp.band.markup_min_pct}-{inp.band.markup_max_pct}%); {inp.rate.material_code} rate "
            f"approved {inp.rate_age_days} d ago{warn}; {'lane ' + inp.lane.code if inp.lane else 'EXW'}"
        )
    parts.append(f"Total {currency} {display(total, currency):,}; delivery est. {estimate}")
    return "\n".join(parts)


async def quote_for(session: AsyncSession, deal: Deal) -> Quote:
    """The deal's quote, created on first use."""
    quote = (
        await session.execute(select(Quote).where(Quote.deal_id == deal.id).limit(1))
    ).scalar_one_or_none()
    if quote is None:
        quote = Quote(id=new_id(), deal_id=deal.id)
        session.add(quote)
        await session.flush()
    return quote


async def latest_version(
    session: AsyncSession, deal_id: uuid.UUID, statuses: Sequence[str] | None = None
) -> QuoteVersion | None:
    stmt = (
        select(QuoteVersion)
        .join(Quote, Quote.id == QuoteVersion.quote_id)
        .where(Quote.deal_id == deal_id)
        .order_by(QuoteVersion.version_no.desc())
        .limit(1)
    )
    if statuses:
        stmt = stmt.where(QuoteVersion.status.in_(statuses))
    return (await session.execute(stmt)).scalar_one_or_none()


async def costs_of(session: AsyncSession, version_id: uuid.UUID) -> list[tuple[QuoteLine, QuoteLineCost]]:
    """MD-only: lines with their cost snapshot."""
    stmt = (
        select(QuoteLine, QuoteLineCost)
        .join(QuoteLineCost, QuoteLineCost.quote_line_id == QuoteLine.id)
        .where(QuoteLine.quote_version_id == version_id)
        .order_by(QuoteLine.line_no)
    )
    return [r.tuple() for r in (await session.execute(stmt)).all()]


async def tiers_of(
    session: AsyncSession, version_id: uuid.UUID
) -> list[tuple[QuoteLine, QuoteLineTier | None]]:
    """Staff-only: lines with their price-list snapshot (None for a line the salesperson priced by hand)."""
    stmt = (
        select(QuoteLine, QuoteLineTier)
        .outerjoin(QuoteLineTier, QuoteLineTier.quote_line_id == QuoteLine.id)
        .where(QuoteLine.quote_version_id == version_id)
        .order_by(QuoteLine.line_no)
    )
    return [(r[0], r[1]) for r in (await session.execute(stmt)).all()]


async def _supersede_live(session: AsyncSession, quote: Quote, now: datetime) -> str | None:
    """Retire every live version of the quote; returns the correction note of the latest, if any."""
    live = list(
        (
            await session.execute(
                select(QuoteVersion)
                .where(QuoteVersion.quote_id == quote.id, QuoteVersion.status.in_(LIVE))
                .order_by(QuoteVersion.version_no.desc())
            )
        ).scalars()
    )
    if not live:
        return None
    ids = [v.id for v in live]
    note = (
        (
            await session.scalar(
                select(Approval.decision_note)
                .where(Approval.quote_version_id == ids[0], Approval.status == "correction_requested")
                .order_by(Approval.decided_at.desc())
                .limit(1)
            )
        )
        if live[0].status == "correction_requested"
        else None
    )
    await session.execute(
        update(Approval)
        .where(Approval.quote_version_id.in_(ids), Approval.status.in_(approvals.OPEN))
        .values(status="cancelled")
    )
    await grants.revoke_open(session, ids, "superseded", now)
    for v in live:
        v.status = "superseded"
    await session.flush()
    return note


async def _insert_version(
    session: AsyncSession,
    *,
    quote: Quote,
    deal: Deal,
    kind: VersionKind,
    currency: str,
    fx: Decimal,
    drafts: Sequence[DraftLine],
    vat: Decimal,
    incoterm: str,
    lane_id: uuid.UUID | None,
    s: BusinessSettings,
    language: str,
    summary: str,
    estimate_id: uuid.UUID | None,
    actor: Actor,
    correction_note: str | None,
    terms: VersionTerms | None = None,
) -> QuoteVersion:
    terms = terms or VersionTerms()
    totals = quote_totals((d.priced.line_total for d in drafts), vat)
    next_no = (
        await session.scalar(
            select(func.coalesce(func.max(QuoteVersion.version_no), 0)).where(
                QuoteVersion.quote_id == quote.id
            )
        )
        or 0
    ) + 1
    version = QuoteVersion(
        id=new_id(),
        quote_id=quote.id,
        version_no=next_no,
        kind=kind,
        status="draft",
        currency=currency,
        fx_rate_to_aed=fx,
        subtotal=totals.subtotal,
        vat_rate=vat,
        vat_amount=totals.vat_amount,
        total=totals.total,
        total_aed=q(totals.total * fx, MONEY_PLACES),
        incoterm=incoterm,
        freight_lane_id=lane_id,
        validity_days=s.quote_validity_days,
        clauses=clauses_for(language, s.quote_validity_days, incoterm == "EXW"),
        approver_summary=summary,
        delivery_estimate_id=estimate_id,
        created_by_type="ai" if actor.type == "ai" else "user",
        created_by=actor.id,
        correction_note=correction_note,
        delivery_time_text=terms.delivery_time_text,
        delivery_place=terms.delivery_place,
        payment_terms_text=terms.payment_terms_text,
        fx_rate=terms.fx_rate,
        fx_rate_date=terms.fx_rate_date,
        signed_by_user_id=terms.signed_by_user_id,
    )
    session.add(version)
    await session.flush()
    for no, d in enumerate(drafts, start=1):
        p = d.priced
        line = QuoteLine(
            id=new_id(),
            quote_version_id=version.id,
            line_no=no,
            product_id=p.product_id,
            description=p.description,
            quantity=p.quantity,
            unit=p.unit,
            unit_price=p.unit_price,
            line_total=p.line_total,
        )
        session.add(line)
        await session.flush()
        for snap in (d.cost, d.tier):
            if snap is not None:
                snap.quote_line_id = line.id
                session.add(snap)
    await session.flush()
    version.status = "pending_approval"  # from here the DB freezes prices and lines
    quote.current_version_id = version.id
    await session.flush()
    await record(
        session,
        actor,
        "quote.version_created",
        "quote_version",
        version.id,
        after={"kind": kind, "version_no": next_no, "total": version.total, "currency": currency},
        timeline=Timeline(
            "quote",
            f"Quote {quote.number} v{next_no} ({kind}) drafted: {currency} "
            f"{display(version.total, currency):,}",
            deal.id,
            deal.primary_contact_id,
            ref_type="quote_version",
            ref_id=str(version.id),
        ),
    )
    return version


def _list_summary(inputs: Sequence[ListPricingInputs], currency: str, total: Decimal) -> str:
    """Approval-card summary for the salesperson (staff-only: may name the minimum, §11.3)."""
    parts = [
        f"{i.line.quantity:,.0f} x {i.product.sku} @ {currency} {i.line.unit_price} (list price; "
        f"minimum {currency} {i.snapshot_min})"
        for i in inputs
    ]
    parts.append(f"Total {currency} {display(total, currency):,}")
    return "\n".join(parts)


async def _request_list_quote(
    session: AsyncSession,
    deps: CrmDeps,
    deal: Deal,
    *,
    reqs: Sequence[DealRequirement],
    s: BusinessSettings,
    actor: Actor,
    now: datetime,
) -> QuoteVersion:
    """v1 (D6, §11.1): every line at its tier's list price, converted at the day's FX, VAT by country."""
    currency = s.quote_currency(reqs[0].delivery_country, deal.currency)
    deal.currency = currency
    fx, fx_date = await fx_with_date(session, currency, now.date())
    inputs = [await list_pricing_inputs(session, r, quote_fx=fx, now=now) for r in reqs]
    drafts = [
        DraftLine(
            LineValues(
                i.description,
                i.line.quantity,
                i.product.unit,
                i.line.unit_price,
                i.line.line_total,
                i.product.id,
            ),
            tier=QuoteLineTier(
                price_tier_id=i.tier.tier_id,
                list_price=i.snapshot_list,
                min_price=i.snapshot_min,
                max_steps=s.max_steps,
                fx_rate_to_aed=fx,
            ),
        )
        for i in inputs
    ]
    contact = await deal_svc.contact_of(session, deal)
    quote = await quote_for(session, deal)
    note = await _supersede_live(session, quote, now)
    has_versions = bool(await session.scalar(select(func.count()).where(QuoteVersion.quote_id == quote.id)))
    vat = vat_pct(reqs[0], s)
    total = quote_totals((d.priced.line_total for d in drafts), vat).total
    return await _insert_version(
        session,
        quote=quote,
        deal=deal,
        kind="revision" if has_versions else "initial",
        currency=currency,
        fx=fx,
        drafts=drafts,
        vat=vat,
        incoterm=s.default_incoterm,
        lane_id=None,
        s=s,
        language=contact.language,
        summary=_list_summary(inputs, currency, total),
        estimate_id=None,
        actor=actor,
        correction_note=note,
        terms=VersionTerms(
            delivery_time_text=lead_time_text(inputs[0].product),
            delivery_place=None,
            payment_terms_text=s.payment_terms_text,
            fx_rate=None if fx == 1 else fx,
            fx_rate_date=fx_date,
            signed_by_user_id=deal.owner_user_id,
        ),
    )


async def request_quote(
    session: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID, actor: Actor
) -> QuoteVersion:
    """Price every requirement, freeze a new version, open approval 1.

    v1 (`pricing.mode = 'price_list'`): the tier's list price (§11.1). v2 (`'cost'`): the top of the MD's
    markup band on cost, with the delivery estimate."""
    now = deps.now()
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    stage = DealStage(deal.stage)
    if stage not in QUOTABLE_STAGES:
        raise RuleViolation(f"a quote can be requested only when the deal is qualified (stage is {stage})")
    s = await deps.settings.get(session)
    reqs = await deal_svc.requirements_of(session, deal.id)
    if not reqs:
        raise RuleViolation("deal has no requirements to quote")
    if s.pricing_mode == "price_list":
        version = await _request_list_quote(session, deps, deal, reqs=reqs, s=s, actor=actor, now=now)
        return await _after_draft(
            session, deps, s, deal=deal, version=version, stage=stage, actor=actor, now=now
        )
    fx = await fx_to_aed(session, deal.currency, now.date())
    inputs = [await pricing_inputs(session, deal, r, s=s, quote_fx=fx, now=now) for r in reqs]
    drafts = [
        DraftLine(pl, _cost_snapshot(inp, pl)) for inp in inputs for pl in (price_line(inp.line, inp.band),)
    ]
    first = inputs[0]
    estimate = await build_delivery_estimate(
        session, deal, first.requirement, costing=first.costing, lane=first.lane, now=now
    )
    contact = await deal_svc.contact_of(session, deal)
    quote = await quote_for(session, deal)
    note = await _supersede_live(session, quote, now)
    has_versions = bool(await session.scalar(select(func.count()).where(QuoteVersion.quote_id == quote.id)))
    vat = vat_pct(reqs[0], s)
    total = quote_totals((d.priced.line_total for d in drafts), vat).total
    version = await _insert_version(
        session,
        quote=quote,
        deal=deal,
        kind="revision" if has_versions else "initial",
        currency=deal.currency,
        fx=fx,
        drafts=drafts,
        vat=vat,
        incoterm=inputs[0].incoterm,
        lane_id=inputs[0].lane.id if inputs[0].lane else None,
        s=s,
        language=contact.language,
        summary=_approver_summary(inputs, drafts, deal.currency, total, estimate.estimated_delivery),
        estimate_id=estimate.id,
        actor=actor,
        correction_note=note,
    )
    return await _after_draft(session, deps, s, deal=deal, version=version, stage=stage, actor=actor, now=now)


async def _after_draft(
    session: AsyncSession,
    deps: CrmDeps,
    s: BusinessSettings,
    *,
    deal: Deal,
    version: QuoteVersion,
    stage: DealStage,
    actor: Actor,
    now: datetime,
) -> QuoteVersion:
    """Stage to QUOTATION (§5: NEGOTIATION -> QUOTATION when a revised quote is drafted), open approval 1."""
    if stage is not DealStage.QUOTATION:
        await deal_svc.move_stage(
            session, deal, DealStage.QUOTATION, actor, now=now, reason="quote requested"
        )
    quote = await session.get(Quote, version.quote_id)
    assert quote is not None
    await approvals.open_approval(
        session,
        deps,
        s,
        deal=deal,
        quote=quote,
        version=version,
        kind="revision" if version.kind == "revision" else "initial",
        role=approvals.initial_role(version.total_aed, s),
        actor=actor,
        now=now,
    )
    return version


async def create_final_version(
    session: AsyncSession,
    deps: CrmDeps,
    deal: Deal,
    issued: QuoteVersion,
    *,
    unit_price: Decimal,
    quantity: Decimal,
    actor: Actor,
) -> QuoteVersion:
    """§14.3: the buyer agreed: a 'final' version at the agreed price/quantity for the primary line (other
    lines unchanged), sent to Approval 2 by the deal's salesperson (§14.4, D1)."""
    now = deps.now()
    s = await deps.settings.get(session)
    quote = await session.get(Quote, issued.quote_id)
    assert quote is not None
    costs = {line.id: cost for line, cost in await costs_of(session, issued.id)}
    drafts: list[DraftLine] = []
    for idx, (line, tier) in enumerate(await tiers_of(session, issued.id)):
        price, qty = (unit_price, quantity) if idx == 0 else (line.unit_price, line.quantity)
        total = q(price * qty, MONEY_PLACES)
        if (cost := costs.get(line.id)) is not None:  # v2 cost mode: MD cost snapshot at the agreed markup
            markup = (
                q((price / cost.unit_cost - 1) * 100, Decimal("0.01")) if cost.unit_cost else cost.markup_pct
            )
            pl = PricedLine(
                line.description, qty, line.unit, cost.unit_cost, markup, price, total, line.product_id
            )
            snap = QuoteLineCost(**{c: getattr(cost, c) for c in _COST_COLS}, markup_pct=markup)
            drafts.append(DraftLine(pl, cost=snap))
            continue
        tier_snap = None if tier is None else QuoteLineTier(**{c: getattr(tier, c) for c in _TIER_COLS})
        drafts.append(
            DraftLine(
                LineValues(line.description, qty, line.unit, price, total, line.product_id), tier=tier_snap
            )
        )
    contact = await deal_svc.contact_of(session, deal)
    await _supersede_live(session, quote, now)
    version = await _insert_version(
        session,
        quote=quote,
        deal=deal,
        kind="final",
        currency=issued.currency,
        fx=issued.fx_rate_to_aed,
        drafts=drafts,
        vat=issued.vat_rate,
        incoterm=issued.incoterm,
        lane_id=issued.freight_lane_id,
        s=s,
        language=contact.language,
        summary=f"Agreed with buyer: {quantity:,.0f} @ {issued.currency} {unit_price}",
        estimate_id=issued.delivery_estimate_id,
        actor=actor,
        correction_note=None,
        terms=VersionTerms(  # §14.3: the agreed price and the terms of the issued quote
            delivery_time_text=issued.delivery_time_text,
            delivery_place=issued.delivery_place,
            payment_terms_text=issued.payment_terms_text,
            fx_rate=issued.fx_rate,
            fx_rate_date=issued.fx_rate_date,
            signed_by_user_id=deal.owner_user_id or issued.signed_by_user_id,
        ),
    )
    await approvals.open_approval(
        session,
        deps,
        s,
        deal=deal,
        quote=quote,
        version=version,
        kind="final",
        role=approvals.initial_role(version.total_aed, s),
        actor=actor,
        now=now,
    )
    return version


# ---------------------------------------------------------------- §11.3 "Edit then approve"
@dataclass(frozen=True, slots=True)
class LineEdit:
    line_no: int
    unit_price: Decimal | None = None
    quantity: Decimal | None = None


@dataclass(frozen=True, slots=True)
class QuoteEdits:
    """What the salesperson changed on the approval screen or the change-request card (§11.3, §13.3)."""

    lines: tuple[LineEdit, ...] = ()
    delivery_time_text: str | None = None
    delivery_place: str | None = None
    incoterm: str | None = None
    payment_terms_text: str | None = None
    note: str | None = None


async def _tier_snapshot(
    session: AsyncSession,
    product_id: uuid.UUID,
    qty: Decimal,
    *,
    fx: Decimal,
    s: BusinessSettings,
    now: datetime,
) -> QuoteLineTier | None:
    """Price-list snapshot for `qty` in the quote currency, or None when no tier contains it."""
    product = await session.get(Product, product_id)
    try:
        tier = resolve_tier(await current_tiers(session, product_id, now), qty)
    except LookupError:
        return None
    assert product is not None
    floor = min_price_for(tier, await category_min_pct(session, product.category_code))
    conv = convert_tier(TierPrice(tier.list_price, floor, tier.tier_id), fx)
    assert conv.min_price is not None
    return QuoteLineTier(
        price_tier_id=tier.tier_id,
        list_price=conv.list_price,
        min_price=min(conv.min_price, conv.list_price),
        max_steps=s.max_steps,
        fx_rate_to_aed=fx,
    )


async def revise_quote(
    session: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID, edits: QuoteEdits, actor: Actor
) -> QuoteVersion:
    """A new version from the latest one with the salesperson's edits (§11.3.3: approval locks a version,
    any change creates a new one). A unit price must stay between the tier minimum and the list price; a
    price below the minimum first goes to the manager as a special price (§11.3, §12.2 rule 4, D3)."""
    now = deps.now()
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    s = await deps.settings.get(session)
    base = await latest_version(session, deal.id, (*LIVE, "expired"))
    if base is None:
        raise RuleViolation("there is no quote to revise yet")
    quote = await session.get(Quote, base.quote_id)
    assert quote is not None
    by_no = {e.line_no: e for e in edits.lines}
    drafts: list[DraftLine] = []
    special = False
    for line, old_snap in await tiers_of(session, base.id):
        snap = old_snap
        e = by_no.get(line.line_no, LineEdit(line.line_no))
        price = line.unit_price if e.unit_price is None else e.unit_price
        qty = line.quantity if e.quantity is None else e.quantity
        if price <= 0 or qty <= 0:
            raise RuleViolation(f"line {line.line_no}: price and quantity must be positive")
        total = (
            line.line_total
            if (price, qty) == (line.unit_price, line.quantity)
            else q(price * qty, MONEY_PLACES)
        )
        if snap is not None and qty != line.quantity and line.product_id is not None:
            # a new quantity may sit in another tier (or outside them: then it is priced by hand, D12)
            snap = await _tier_snapshot(session, line.product_id, qty, fx=base.fx_rate_to_aed, s=s, now=now)
        new_snap: QuoteLineTier | None = None
        if snap is not None:
            if price > max(snap.list_price, line.unit_price):
                raise RuleViolation(f"line {line.line_no}: the unit price may not exceed the list price")
            special = special or price < snap.min_price
            new_snap = QuoteLineTier(**{c: getattr(snap, c) for c in _TIER_COLS})
        drafts.append(
            DraftLine(
                LineValues(line.description, qty, line.unit, price, total, line.product_id), tier=new_snap
            )
        )
    incoterm = edits.incoterm or base.incoterm
    contact = await deal_svc.contact_of(session, deal)
    await _supersede_live(session, quote, now)
    version = await _insert_version(
        session,
        quote=quote,
        deal=deal,
        kind="final" if base.kind == "final" else "revision",
        currency=base.currency,
        fx=base.fx_rate_to_aed,
        drafts=drafts,
        vat=base.vat_rate,
        incoterm=incoterm,
        lane_id=base.freight_lane_id,
        s=s,
        language=contact.language,
        summary=(edits.note or "Revised by the salesperson")
        + (" — special price below the minimum" if special else ""),
        estimate_id=base.delivery_estimate_id,
        actor=actor,
        correction_note=edits.note,
        terms=VersionTerms(
            delivery_time_text=edits.delivery_time_text or base.delivery_time_text,
            delivery_place=edits.delivery_place or base.delivery_place,
            payment_terms_text=edits.payment_terms_text or base.payment_terms_text,
            fx_rate=base.fx_rate,
            fx_rate_date=base.fx_rate_date,
            signed_by_user_id=deal.owner_user_id or base.signed_by_user_id,
        ),
    )
    if DealStage(deal.stage) is DealStage.NEGOTIATION and version.kind == "revision":
        await deal_svc.move_stage(session, deal, DealStage.QUOTATION, actor, now=now, reason="quote revised")
    await approvals.open_approval(
        session,
        deps,
        s,
        deal=deal,
        quote=quote,
        version=version,
        kind="special_price" if special else ("final" if version.kind == "final" else "revision"),
        role=approvals.initial_role(version.total_aed, s),
        actor=actor,
        now=now,
    )
    return version


_COST_COLS: Final = (
    "material_rate_id",
    "material_rate",
    "kg_per_1000",
    "yield_pct",
    "production_per_1000",
    "packaging_per_1000",
    "freight_per_1000",
    "unit_cost",
    "margin_rule_id",
    "markup_min_pct",
    "markup_max_pct",
)
_TIER_COLS: Final = ("price_tier_id", "list_price", "min_price", "max_steps", "fx_rate_to_aed")


async def expire_quotes(session: AsyncSession, deps: CrmDeps) -> int:
    """Daily: issued versions past valid_until (7 days in the shipping crisis) expire; grants are revoked."""
    now = deps.now()
    s = await deps.settings.get(session)
    today = local_today(now, s)
    ids = list(
        (
            await session.execute(
                update(QuoteVersion)
                .where(QuoteVersion.status.in_(ISSUED), QuoteVersion.valid_until < today)
                .values(status="expired")
                .returning(QuoteVersion.id)
            )
        ).scalars()
    )
    await grants.revoke_open(session, ids, "expired", now)
    for vid in ids:
        await record(session, Actor.system("quote_expiry"), "quote.expired", "quote_version", vid)
    return len(ids)


__all__ = [
    "CLAUSES",
    "ISSUED",
    "DraftLine",
    "costs_of",
    "create_final_version",
    "expire_quotes",
    "latest_version",
    "lines_of",
    "quote_for",
    "request_quote",
]
