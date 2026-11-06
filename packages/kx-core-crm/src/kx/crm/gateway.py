"""kx.core.contracts implementations: what the AI may ask of the CRM, the send-grant check, and the
approval-status reader automation's alert ladder needs.

Every method runs in its own CRM transaction. DealContext is assembled field by field from
buyer-safe data: no cost, markup, margin range or floor is ever read into it, and `allowed_prices` is
the closed list of amounts the AI may say (issued quote lines + totals + issued counter offers).
"""

from __future__ import annotations

import uuid
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.contracts import (
    AiRole,
    AllowedPrice,
    ChangeField,
    ChangeType,
    Channel,
    ContactPatch,
    ContactView,
    CounterVerdict,
    CounterVerdictKind,
    DealContext,
    DealStage,
    DeliveryEstimateView,
    QualifyResult,
    QuoteLineView,
    QuoteView,
    RequirementPatch,
    RequirementView,
)
from kx.core.db import Databases
from kx.core.money import MONEY_PLACES, q
from kx.core.settings import LogicalDb
from kx.crm.deps import CrmDeps
from kx.crm.models import (
    Approval,
    ChangeRequest,
    Company,
    Contact,
    DealRequirement,
    DeliveryEstimateRow,
    NegotiationRound,
    Product,
    Quote,
    QuoteLine,
    QuoteVersion,
)
from kx.crm.rules import pause_reason
from kx.crm.services import approvals, assignment, change_requests, grants, negotiation, quotes, staff
from kx.crm.services import deals as deal_svc
from kx.crm.services.issuing import lines_of
from kx.crm.services.journal import Actor
from kx.crm.services.pricing import best_lane, build_delivery_estimate, current_costing


# ---------------------------------------------------------------- buyer-safe views (pure)
def contact_view(c: Contact, company: Company | None) -> ContactView:
    return ContactView(
        contact_id=c.id,
        name=c.full_name,
        company=company.name if company else None,
        language=c.language,
        timezone=c.timezone,
        preferred_channel=c.preferred_channel,
        has_phone=bool(c.phone_e164 or c.whatsapp_e164),
        has_email=bool(c.email),
        phone_verified=c.phone_verified_at is not None,
    )


def requirement_view(r: DealRequirement, sku: str | None) -> RequirementView:
    return RequirementView(
        requirement_id=r.id,
        product_sku=sku,
        product_text=r.product_text,
        spec=r.spec,
        quantity=r.quantity,
        unit=r.unit,
        application=r.application,
        delivery_country=r.delivery_country,
        delivery_city=r.delivery_city,
        needed_by=r.needed_by,
    )


def quote_view(number: str, v: QuoteVersion, lines: list[QuoteLine]) -> QuoteView:
    return QuoteView(
        quote_number=number,
        version_no=v.version_no,
        status=v.status,
        currency=v.currency,
        total=v.total,
        valid_until=v.valid_until,
        lines=tuple(
            QuoteLineView(
                description=ln.description,
                quantity=ln.quantity,
                unit=ln.unit,
                unit_price=ln.unit_price,
                line_total=ln.line_total,
            )
            for ln in lines
        ),
    )


def allowed_prices(
    v: QuoteVersion, lines: list[QuoteLine], offers: list[NegotiationRound]
) -> tuple[AllowedPrice, ...]:
    """Closed list of amounts the AI may state, all in the quote currency."""
    cur = v.currency
    out = [
        AllowedPrice(amount=v.subtotal, currency=cur, label="subtotal"),
        AllowedPrice(amount=v.total, currency=cur, label="total"),
    ]
    if v.vat_amount:
        out.append(AllowedPrice(amount=v.vat_amount, currency=cur, label="vat"))
    for ln in lines:
        out += [
            AllowedPrice(amount=ln.unit_price, currency=cur, label=f"line {ln.line_no} unit price"),
            AllowedPrice(amount=ln.line_total, currency=cur, label=f"line {ln.line_no} total"),
        ]
    qty = lines[0].quantity if lines else None
    for r in offers:
        if r.offered_unit_price is None:
            continue
        out.append(AllowedPrice(amount=r.offered_unit_price, currency=cur, label=f"offer round {r.round_no}"))
        if (n := r.buyer_ask_quantity or qty) is not None:
            out.append(
                AllowedPrice(
                    amount=q(r.offered_unit_price * n, MONEY_PLACES),
                    currency=cur,
                    label=f"offer round {r.round_no} total",
                )
            )
    unique: dict[tuple[Decimal, str], AllowedPrice] = {}
    for p in out:
        unique.setdefault((p.amount.normalize(), p.currency), p)
    return tuple(unique.values())


class CrmGatewayImpl:
    """kx.core.contracts.CrmGateway for agents. Construct once with the process's Databases + CrmDeps."""

    def __init__(self, databases: Databases, deps: CrmDeps) -> None:
        self._dbs = databases
        self._deps = deps

    def _tx(self) -> AbstractAsyncContextManager[AsyncSession]:
        return self._dbs.transaction(LogicalDb.CRM)

    async def resolve_deal(
        self, channel: Channel, address: str | None, visitor_ref: uuid.UUID | None
    ) -> uuid.UUID:
        async with self._tx() as s:
            deal = await deal_svc.resolve_deal(
                s, channel, address, visitor_ref, Actor.system(f"inbound:{channel}")
            )
            return deal.id

    async def deal_context(self, deal_id: uuid.UUID) -> DealContext:
        async with self._tx() as s:
            return await build_context(s, self._deps, deal_id)

    async def update_requirement(self, deal_id: uuid.UUID, patch: RequirementPatch, actor: AiRole) -> None:
        async with self._tx() as s:
            await deal_svc.patch_requirement(
                s, await deal_svc.get_deal(s, deal_id, lock=True), patch, Actor.ai(actor)
            )

    async def set_contact(self, deal_id: uuid.UUID, patch: ContactPatch, actor: AiRole) -> None:
        async with self._tx() as s:
            await deal_svc.patch_contact(
                s, await deal_svc.get_deal(s, deal_id, lock=True), patch, Actor.ai(actor)
            )

    async def try_qualify(self, deal_id: uuid.UUID, actor: AiRole) -> QualifyResult:
        async with self._tx() as s:
            deal = await deal_svc.get_deal(s, deal_id, lock=True)
            result = await deal_svc.try_qualify(s, deal, Actor.ai(actor), self._deps.now())
            if result.qualified and deal.owner_user_id is None:  # §5 step 4: assign and alert
                await assignment.assign(s, self._deps, deal, Actor.system("assignment"))
            return result

    async def request_quote(self, deal_id: uuid.UUID, actor: AiRole) -> None:
        async with self._tx() as s:
            await quotes.request_quote(s, self._deps, deal_id, Actor.ai(actor))

    async def evaluate_counter(
        self, deal_id: uuid.UUID, ask_unit_price: Decimal, ask_quantity: Decimal | None, channel: Channel
    ) -> CounterVerdict:
        async with self._tx() as s:
            verdict = await negotiation.evaluate_counter(
                s,
                self._deps,
                deal_id,
                ask_unit_price=ask_unit_price,
                ask_quantity=ask_quantity,
                channel=channel,
                actor=Actor.ai(AiRole.DEAL_CLOSER),
            )
        if verdict.kind is CounterVerdictKind.REQUOTE:
            # §12.2 rule 5: the new tier price is not approved yet, so the AI must not state it
            verdict = verdict.model_copy(update={"offer_unit_price": None})
        return verdict

    async def pause_ai(self, deal_id: uuid.UUID, reason: str, actor: AiRole) -> None:
        async with self._tx() as s:
            deal = await deal_svc.get_deal(s, deal_id, lock=True)
            if deal.ai_enabled:
                db_reason = pause_reason(reason)
                await negotiation.pause_ai(
                    s,
                    self._deps,
                    deal,
                    reason=db_reason,
                    event_reason="below_range"
                    if db_reason == "below_range"
                    else "exception"
                    if db_reason == "exception"
                    else "manual",
                    actor=Actor.ai(actor),
                )

    async def mark_agreed(self, deal_id: uuid.UUID, actor: AiRole) -> None:
        async with self._tx() as s:
            await negotiation.mark_agreed(s, self._deps, deal_id, Actor.ai(actor))

    async def schedule_follow_up(self, deal_id: uuid.UUID, due_at: datetime, reason: str) -> None:
        async with self._tx() as s:
            deal = await deal_svc.get_deal(s, deal_id, lock=True)
            await deal_svc.set_next_action(s, deal, due_at, reason, Actor.ai(AiRole.FOLLOW_UP))

    async def delivery_estimate(self, deal_id: uuid.UUID) -> DeliveryEstimateView | None:
        async with self._tx() as s:
            return await delivery_view(s, self._deps, deal_id)

    async def add_timeline(self, deal_id: uuid.UUID, kind: str, summary: str, actor: AiRole) -> None:
        async with self._tx() as s:
            await deal_svc.add_note(s, await deal_svc.get_deal(s, deal_id), kind, summary, Actor.ai(actor))

    async def raise_change_request(
        self,
        deal_id: uuid.UUID,
        type: ChangeType,
        buyer_text: str,
        fields: tuple[ChangeField, ...],
        actor: AiRole,
    ) -> uuid.UUID:
        """§13 / D12: the salesperson gets a card; the AI only acknowledges ("within 4 working hours")."""
        async with self._tx() as s:
            cr = await change_requests.raise_change_request(
                s, self._deps, deal_id, type=type, buyer_text=buyer_text, fields=fields, actor=Actor.ai(actor)
            )
            return cr.id

    async def set_cover_note(self, deal_id: uuid.UUID, note: str, actor: AiRole) -> None:
        """D9: the AI's "open to discuss" line for the message of the quote waiting for approval."""
        async with self._tx() as s:
            await quotes.set_cover_note(s, deal_id, note, Actor.ai(actor))


async def build_context(s: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID) -> DealContext:
    deal = await deal_svc.get_deal(s, deal_id)
    settings = await deps.settings.get(s)
    contact = await deal_svc.contact_of(s, deal)
    company = await s.get(Company, contact.company_id) if contact.company_id else None
    req_rows = (
        (
            await s.execute(
                select(DealRequirement, Product.sku)
                .outerjoin(Product, Product.id == DealRequirement.product_id)
                .where(DealRequirement.deal_id == deal.id)
                .order_by(DealRequirement.created_at)
            )
        )
        .tuples()
        .all()
    )
    issued = await quotes.latest_version(s, deal.id, quotes.ISSUED)
    latest_quote: QuoteView | None = None
    prices: tuple[AllowedPrice, ...] = ()
    if issued is not None:
        number = await s.scalar(select(Quote.number).where(Quote.id == issued.quote_id))
        lines = await lines_of(s, issued.id)
        offers = list(
            (
                await s.execute(
                    select(NegotiationRound)
                    .where(NegotiationRound.quote_version_id == issued.id)
                    .order_by(NegotiationRound.round_no)
                )
            ).scalars()
        )
        latest_quote, prices = quote_view(number or "", issued, lines), allowed_prices(issued, lines, offers)
    left = await negotiation.steps_left(s, deal, settings)
    estimate = await _latest_estimate(s, deal.id)
    return DealContext(
        deal_id=deal.id,
        number=deal.number,
        stage=DealStage(deal.stage),
        ai_enabled=deal.ai_enabled and settings.flag("ai_enabled", True),
        currency=deal.currency,
        contact=contact_view(contact, company),
        requirements=tuple(requirement_view(r, sku) for r, sku in req_rows),
        missing_fields=await deal_svc.missing_for(s, deal),
        latest_quote=latest_quote,
        allowed_prices=prices,
        concessions_left=left,
        estimated_delivery=(estimate.confirmed_date or estimate.estimated_delivery) if estimate else None,
        next_action_at=deal.next_action_at,
        salesperson_name=owner.full_name if (owner := await staff.owner_of(s, deal)) else None,
        change_request_hours=settings.change_request_sla_working_hours,
    )


async def _latest_estimate(s: AsyncSession, deal_id: uuid.UUID) -> DeliveryEstimateRow | None:
    stmt = (
        select(DeliveryEstimateRow)
        .where(DeliveryEstimateRow.deal_id == deal_id)
        .order_by(DeliveryEstimateRow.created_at.desc(), DeliveryEstimateRow.id.desc())
        .limit(1)
    )
    return (await s.execute(stmt)).scalar_one_or_none()


async def delivery_view(s: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID) -> DeliveryEstimateView | None:
    """Latest estimate, or a fresh one (persisted as T14 training data) once product + quantity are known."""
    est = await _latest_estimate(s, deal_id)
    if est is None:
        deal = await deal_svc.get_deal(s, deal_id)
        reqs = await deal_svc.requirements_of(s, deal_id)
        if not reqs or reqs[0].product_id is None or reqs[0].quantity is None:
            return None
        now, req = deps.now(), reqs[0]
        costing = await current_costing(s, req.product_id, now)
        est = await build_delivery_estimate(
            s,
            deal,
            req,
            costing=costing,
            lane=await best_lane(s, req.delivery_country, req.delivery_port),
            now=now,
        )
    return DeliveryEstimateView(
        estimated_ready=est.estimated_ready,
        estimated_delivery=est.confirmed_date or est.estimated_delivery,
        confirmed=est.confirmed_date is not None,
    )


class GrantConsumerImpl:
    """kx.core.contracts.GrantConsumer: automation calls this right before a priced message leaves."""

    def __init__(self, databases: Databases, deps: CrmDeps) -> None:
        self._dbs = databases
        self._deps = deps

    async def consume_grant(
        self, grant_id: uuid.UUID, content_sha256: str, message_id: uuid.UUID, recipient_hash: str
    ) -> bool:
        async with self._dbs.transaction(LogicalDb.CRM) as s:
            settings = await self._deps.settings.get(s)
            return await grants.consume_grant(
                s,
                settings,
                grant_id=grant_id,
                sha=content_sha256,
                message_id=message_id,
                rcpt_hash=recipient_hash,
                now=self._deps.now(),
            )


class ApprovalStatusReaderImpl:
    """Satisfies kx.automation's ApprovalStatusReader (structural Protocol, not imported): the reminder
    ladder asks before every step whether an approval or a change request still waits for a human, so a
    decided one never rings anyone's phone (§16)."""

    def __init__(self, databases: Databases) -> None:
        self._dbs = databases

    async def is_pending(self, approval_ref: uuid.UUID) -> bool:
        async with self._dbs.transaction(LogicalDb.CRM) as s:
            status = await s.scalar(select(Approval.status).where(Approval.id == approval_ref))
            if status is not None:
                return status in approvals.OPEN
            card = await s.scalar(select(ChangeRequest.status).where(ChangeRequest.alert_ref == approval_ref))
        return card in change_requests.OPEN
