"""Deterministic negotiation. The engine decides; this module loads its state and records rounds.

v1 (`pricing.mode = 'price_list'`, §12, D7): the state is the latest ISSUED version's primary line and its
price-list snapshot (list price, minimum, steps, in the quote currency): a ladder of at most 3 equal steps
from the line's price to the minimum. Steps used and offers count per version; asks below the minimum
count per open negotiation (deals.low_ask_count, reset when a new version is issued). A line priced by
hand (no snapshot: custom/printed, D8) is never negotiated by the AI.

v2 (`'cost'`, kept): state from the MD-only cost snapshot; the per-piece cost is rebuilt UNROUNDED
(cost/1000 / 1000) so the ladder top equals the quoted price exactly; max concessions from the margin rule.

Both: concessions are traded for quantity, never for longer delivery (DB CHECK on
negotiation_rounds.traded_for). When a human must decide, the AI tells the buyer "within 60 minutes" and
is paused for the deal (NEGOTIATION_PAUSED).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.contracts import CounterVerdict, CounterVerdictKind, DealStage, RequirementPatch
from kx.core.errors import RuleViolation
from kx.core.event_payloads import NegotiationPaused
from kx.crm.deps import CrmDeps
from kx.crm.models import Deal, MarginRuleRow, NegotiationRound, QuoteLine, QuoteLineCost, QuoteVersion, Task
from kx.crm.pricing.engine import evaluate_counter as engine_counter
from kx.crm.pricing.engine import evaluate_ladder_counter, ladder_between, unit_cost_per_1000
from kx.crm.pricing.price_list import PriceTier, TierPrice, convert_tier
from kx.crm.pricing.types import CostInputs, LadderState, MarginBand, NegotiationState
from kx.crm.rules import PauseReason, round_verdict
from kx.crm.services import deals as deal_svc
from kx.crm.services import quotes, staff
from kx.crm.services.issuing import local_today
from kx.crm.services.journal import Actor, Emit, Timeline, record
from kx.crm.services.pricing import tiers_of_version
from kx.crm.services.quotes import ISSUED, costs_of, create_final_version, latest_version, tiers_of
from kx.crm.services.settings import BusinessSettings

THOUSAND = Decimal(1000)
EventReason = Literal["below_range", "concessions_exhausted", "exception", "manual"]


@dataclass(frozen=True, slots=True)
class LadderNegotiation:
    """v1 state for the deal's primary line; `state` None = a hand-priced line the AI never negotiates."""

    version: QuoteVersion
    line: QuoteLine
    state: LadderState | None
    tiers: tuple[PriceTier, ...]  # the line's price-list version, list prices in the quote currency


async def load_ladder(session: AsyncSession, deal: Deal) -> LadderNegotiation | None:
    """§12.1 ladder of the latest issued version (None before any quote is issued)."""
    version = await latest_version(session, deal.id, ISSUED)
    if version is None:
        return None
    line, snap = (await tiers_of(session, version.id))[0]
    if snap is None:
        return LadderNegotiation(version, line, None, ())
    # top = the line's own price, so a quote the salesperson edited inside the range never re-opens higher
    ladder = ladder_between(line.unit_price, min(snap.min_price, line.unit_price), snap.max_steps)
    rounds = list(
        (
            await session.execute(
                select(NegotiationRound)
                .where(NegotiationRound.deal_id == deal.id, NegotiationRound.quote_version_id == version.id)
                .order_by(NegotiationRound.round_no)
            )
        ).scalars()
    )
    offers = [r.offered_unit_price for r in rounds if r.offered_unit_price is not None]
    traded_qty = [
        r.buyer_ask_quantity
        for r in rounds
        if r.traded_for == "quantity_increase" and r.buyer_ask_quantity is not None
    ]
    fx = snap.fx_rate_to_aed
    tiers = tuple(
        PriceTier(
            t.tier_id,
            t.min_qty,
            t.max_qty,
            convert_tier(TierPrice(t.list_price, None, t.tier_id), fx).list_price,
        )
        for t in (await tiers_of_version(session, snap.price_tier_id) if snap.price_tier_id else ())
    )
    state = LadderState(
        ladder=ladder,
        steps_used=sum(1 for r in rounds if r.traded_for is not None),
        current_offer=offers[-1] if offers else line.unit_price,
        base_quantity=max([line.quantity, *traded_qty]),
        low_ask_count=deal.low_ask_count,
        tier_id=snap.price_tier_id,
        currency=version.currency,
    )
    return LadderNegotiation(version, line, state, tiers)


async def steps_left(session: AsyncSession, deal: Deal, s: BusinessSettings) -> int:
    """Concession steps still open on the deal (DealContext.concessions_left; a count, never the steps)."""
    if s.pricing_mode == "price_list":
        neg = await load_ladder(session, deal)
        if neg is None:
            return s.max_steps
        if neg.state is None:
            return 0
        return max(len(neg.state.ladder) - 1 - neg.state.steps_used, 0)
    cost = await load_state(session, deal, s)
    max_c = cost.state.band.max_concessions if cost else s.max_concessions_default
    return max(max_c - deal.concessions_used, 0)


async def agreed_terms(
    session: AsyncSession, deal: Deal, s: BusinessSettings
) -> tuple[QuoteVersion, Decimal, Decimal] | None:
    """The issued version, the current offer per piece and the quantity it was made for."""
    if s.pricing_mode == "price_list":
        neg = await load_ladder(session, deal)
        if neg is None:
            return None
        if neg.state is None:
            return neg.version, neg.line.unit_price, neg.line.quantity
        return neg.version, neg.state.current_offer, neg.state.base_quantity
    cost = await load_state(session, deal, s)
    if cost is None:
        return None
    return cost.version, cost.state.current_offer_unit_price, cost.state.base_quantity


@dataclass(frozen=True, slots=True)
class Negotiation:
    version: QuoteVersion
    line: QuoteLine
    state: NegotiationState


def unrounded_unit_cost(c: QuoteLineCost) -> Decimal:
    """Per-piece cost rebuilt from the snapshot: unit_cost_per_1000(...) / 1000 (exact, 7 dp)."""
    return (
        unit_cost_per_1000(
            CostInputs(
                material_rate_per_kg=c.material_rate,
                kg_per_1000=c.kg_per_1000,
                yield_pct=c.yield_pct,
                production_per_1000=c.production_per_1000,
                packaging_per_1000=c.packaging_per_1000,
                freight_per_1000=c.freight_per_1000,
            )
        )
        / THOUSAND
    )


async def load_state(session: AsyncSession, deal: Deal, s: BusinessSettings) -> Negotiation | None:
    """Engine state for the deal's primary line of the latest issued version (None before any quote)."""
    version = await latest_version(session, deal.id, ISSUED)
    if version is None:
        return None
    line, cost = (await costs_of(session, version.id))[0]
    max_c = (
        await session.scalar(
            select(MarginRuleRow.max_concessions).where(MarginRuleRow.id == cost.margin_rule_id)
        )
        if cost.margin_rule_id
        else None
    )
    band = MarginBand(
        markup_min_pct=cost.markup_min_pct,
        markup_max_pct=cost.markup_max_pct,
        max_concessions=s.max_concessions_default if max_c is None else max_c,
        rule_id=cost.margin_rule_id,
    )
    rounds = list(
        (
            await session.execute(
                select(NegotiationRound)
                .where(NegotiationRound.deal_id == deal.id, NegotiationRound.quote_version_id == version.id)
                .order_by(NegotiationRound.round_no)
            )
        ).scalars()
    )
    offers = [r.offered_unit_price for r in rounds if r.offered_unit_price is not None]
    traded_qty = [
        r.buyer_ask_quantity
        for r in rounds
        if r.traded_for == "quantity_increase" and r.buyer_ask_quantity is not None
    ]
    state = NegotiationState(
        unit_cost=unrounded_unit_cost(cost),
        band=band,
        concessions_used=deal.concessions_used,
        current_offer_unit_price=offers[-1] if offers else line.unit_price,
        base_quantity=max([line.quantity, *traded_qty]),
        currency=version.currency,
    )
    return Negotiation(version, line, state)


async def _next_round_no(session: AsyncSession, deal_id: uuid.UUID) -> int:
    return (
        int(
            await session.scalar(
                select(func.coalesce(func.max(NegotiationRound.round_no), 0)).where(
                    NegotiationRound.deal_id == deal_id
                )
            )
            or 0
        )
        + 1
    )


async def evaluate_counter(
    session: AsyncSession,
    deps: CrmDeps,
    deal_id: uuid.UUID,
    *,
    ask_unit_price: Decimal,
    ask_quantity: Decimal | None,
    channel: str,
    actor: Actor,
) -> CounterVerdict:
    """Answer a buyer's counter-offer; every answer is a recorded round (the deal row is locked)."""
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    s = await deps.settings.get(session)
    if not deal.ai_enabled:
        return CounterVerdict(
            kind=CounterVerdictKind.NEEDS_MANAGER,
            offer_unit_price=None,
            currency=deal.currency,
            concessions_left=0,
            reason="ai_paused",
        )
    if s.pricing_mode == "price_list":
        return await _evaluate_ladder(
            session,
            deps,
            deal,
            s,
            ask_unit_price=ask_unit_price,
            ask_quantity=ask_quantity,
            channel=channel,
            actor=actor,
        )
    neg = await load_state(session, deal, s)
    if neg is None:
        raise RuleViolation("no approved quote to negotiate on yet")
    now = deps.now()
    if neg.version.valid_until is not None and neg.version.valid_until < local_today(now, s):
        raise RuleViolation("the quote has expired; request a fresh quote")
    verdict = engine_counter(neg.state, ask_unit_price, ask_quantity)
    used = neg.state.concessions_used + (1 if verdict.traded_for is not None else 0)
    session.add(
        NegotiationRound(
            deal_id=deal.id,
            quote_version_id=neg.version.id,
            round_no=await _next_round_no(session, deal.id),
            channel=channel,
            buyer_ask_unit_price=ask_unit_price,
            buyer_ask_quantity=ask_quantity,
            verdict=round_verdict(verdict),
            offered_unit_price=verdict.offer_unit_price,
            concession_no=used if verdict.traded_for is not None else None,
            traded_for=verdict.traded_for,
            ai_reasoning=verdict.reason or None,
        )
    )
    deal.concessions_used = used
    if DealStage(deal.stage) is DealStage.QUOTATION:
        await deal_svc.move_stage(
            session, deal, DealStage.NEGOTIATION, actor, now=now, reason="buyer countered"
        )
    await record(
        session,
        actor,
        "negotiation.round",
        "deal",
        deal.id,
        after={
            "ask": ask_unit_price,
            "qty": ask_quantity,
            "verdict": verdict.kind.value,
            "offer": verdict.offer_unit_price,
        },
        timeline=Timeline(
            "negotiation",
            _round_summary(verdict, ask_unit_price),
            deal.id,
            deal.primary_contact_id,
            channel=channel,
        ),
    )
    if verdict.kind is CounterVerdictKind.NEEDS_MANAGER:
        below = verdict.reason == "below_range"
        await pause_ai(
            session,
            deps,
            deal,
            reason="below_range" if below else "exception",
            event_reason="below_range" if below else "concessions_exhausted",
            actor=actor,
            task_payload={
                "ask_unit_price": str(ask_unit_price),
                "ask_quantity": str(ask_quantity) if ask_quantity else None,
            },
        )
    await session.flush()
    return verdict


_K = CounterVerdictKind


async def _evaluate_ladder(
    session: AsyncSession,
    deps: CrmDeps,
    deal: Deal,
    s: BusinessSettings,
    *,
    ask_unit_price: Decimal,
    ask_quantity: Decimal | None,
    channel: str,
    actor: Actor,
) -> CounterVerdict:
    """v1: §12.2 rules 1-5 on the issued version's ladder; every answer is a recorded round."""
    neg = await load_ladder(session, deal)
    if neg is None:
        raise RuleViolation("no approved quote to negotiate on yet")
    now = deps.now()
    if neg.version.valid_until is not None and neg.version.valid_until < local_today(now, s):
        raise RuleViolation("the quote has expired; request a fresh quote")
    if neg.state is None:  # D8 / §23 scenario 5: custom items are never negotiated by the AI
        verdict = CounterVerdict(
            kind=_K.CHANGE_REQUEST,
            offer_unit_price=None,
            currency=neg.version.currency,
            concessions_left=0,
            reason="custom_item",
        )
    else:
        verdict = evaluate_ladder_counter(neg.state, ask_unit_price, ask_quantity, tiers=neg.tiers)
        if ask_unit_price < neg.state.ladder[-1] and verdict.kind in (_K.COUNTER, _K.NEEDS_MANAGER):
            deal.low_ask_count += 1  # §12.2 rule 4: the second one pauses the AI
    offered = verdict.offer_unit_price if verdict.kind in (_K.ACCEPT, _K.COUNTER) else None
    session.add(
        NegotiationRound(
            deal_id=deal.id,
            quote_version_id=neg.version.id,
            round_no=await _next_round_no(session, deal.id),
            channel=channel,
            buyer_ask_unit_price=ask_unit_price,
            buyer_ask_quantity=ask_quantity,
            verdict=round_verdict(verdict),
            offered_unit_price=offered,
            concession_no=(neg.state.steps_used + 1) if neg.state and verdict.traded_for else None,
            traded_for=verdict.traded_for,
            ai_reasoning=verdict.reason or None,
        )
    )
    if verdict.traded_for is not None:
        deal.concessions_used += 1
    if DealStage(deal.stage) is DealStage.QUOTATION:
        await deal_svc.move_stage(
            session, deal, DealStage.NEGOTIATION, actor, now=now, reason="buyer countered"
        )
    await record(
        session,
        actor,
        "negotiation.round",
        "deal",
        deal.id,
        after={"ask": ask_unit_price, "qty": ask_quantity, "verdict": verdict.kind.value, "offer": offered},
        timeline=Timeline(
            "negotiation",
            _round_summary(verdict, ask_unit_price),
            deal.id,
            deal.primary_contact_id,
            channel=channel,
        ),
    )
    await session.flush()
    match verdict.kind:
        case _K.NEEDS_MANAGER:
            reason: Literal["below_range", "concessions_exhausted"] = (
                "below_range" if verdict.reason == "below_range" else "concessions_exhausted"
            )
            await pause_ai(
                session,
                deps,
                deal,
                reason=reason,
                event_reason=reason,
                actor=actor,
                task_payload={
                    "ask_unit_price": str(ask_unit_price),
                    "ask_quantity": str(ask_quantity) if ask_quantity else None,
                },
            )
        case _K.REQUOTE if ask_quantity is not None:  # §12.2 rule 5: revised quote for the salesperson
            await deal_svc.patch_requirement(session, deal, RequirementPatch(quantity=ask_quantity), actor)
            await quotes.request_quote(session, deps, deal.id, actor)
        case _:
            pass
    await session.flush()
    return verdict


def _round_summary(v: CounterVerdict, ask: Decimal) -> str:
    offer = f"; offered {v.currency} {v.offer_unit_price}/pc" if v.offer_unit_price is not None else ""
    return f"Buyer asked {v.currency} {ask}/pc → {v.kind.value}{offer}"


async def pause_ai(
    session: AsyncSession,
    deps: CrmDeps,
    deal: Deal,
    *,
    reason: PauseReason,
    event_reason: EventReason,
    actor: Actor,
    task_payload: dict[str, str | None] | None = None,
) -> None:
    """Stop the AI on this deal and alert manager + MD with the 60-minute promise made to the buyer."""
    now = deps.now()
    s = await deps.settings.get(session)
    manager, md = await staff.manager_for(session, deal), await staff.md_user(session)
    respond_by = now + timedelta(minutes=s.below_range_reply_minutes)
    deal.ai_enabled, deal.ai_paused_reason, deal.ai_paused_at = False, reason, now
    session.add(
        Task(
            deal_id=deal.id,
            kind="below_range_review",
            assigned_user_id=manager.id,
            due_at=respond_by,
            payload={"reason": event_reason, **(task_payload or {})},
        )
    )
    alert = tuple(staff.staff_contact(u) for u in {manager.id: manager, md.id: md}.values())
    await session.flush()
    await record(
        session,
        actor,
        "deal.ai_paused",
        "deal",
        deal.id,
        after={"reason": reason},
        timeline=Timeline(
            "ai_paused",
            f"AI paused ({event_reason}); team to reply by {respond_by:%H:%M} UTC",
            deal.id,
            deal.primary_contact_id,
        ),
        emit=Emit(
            ev.NEGOTIATION_PAUSED,
            "deal",
            deal.id,
            NegotiationPaused(
                deal_ref=deal.id,
                deal_number=deal.number,
                reason=event_reason,
                alert=alert,
                respond_by=respond_by,
            ),
        ),
    )


async def resume_ai(session: AsyncSession, deal: Deal, actor: Actor) -> None:
    deal.ai_enabled, deal.ai_paused_reason, deal.ai_paused_at = True, None, None
    await session.flush()
    await record(
        session,
        actor,
        "deal.ai_resumed",
        "deal",
        deal.id,
        timeline=Timeline("ai_resumed", "AI handed back the conversation", deal.id, deal.primary_contact_id),
    )


async def manager_offer(
    session: AsyncSession,
    deps: CrmDeps,
    deal_id: uuid.UUID,
    unit_price: Decimal,
    *,
    actor: Actor,
    resume: bool = True,
) -> None:
    """T6: the manager sets an approved price (may be below range) and hands back to the AI.
    Recorded as an 'exception' round so the price becomes an allowed price the AI may say."""
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    s = await deps.settings.get(session)
    terms = await agreed_terms(session, deal, s)
    if terms is None:
        raise RuleViolation("no approved quote to negotiate on")
    issued = terms[0]
    session.add(
        NegotiationRound(
            deal_id=deal.id,
            quote_version_id=issued.id,
            round_no=await _next_round_no(session, deal.id),
            channel="crm",
            verdict="exception",
            offered_unit_price=unit_price,
            ai_reasoning=f"manager price by {actor.id}",
        )
    )
    await record(
        session,
        actor,
        "negotiation.manager_offer",
        "deal",
        deal.id,
        after={"unit_price": unit_price},
        timeline=Timeline(
            "negotiation",
            f"Manager approved {issued.currency} {unit_price}/pc",
            deal.id,
            deal.primary_contact_id,
        ),
    )
    if resume:
        await resume_ai(session, deal, actor)


async def mark_agreed(session: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID, actor: Actor) -> QuoteVersion:
    """§14: the buyer accepted the current offer: READY_TO_CLOSE and a 'final' version at the agreed price
    and quantity, which goes to Approval 2 before the final quote is sent (§12.2 rule 7: agreement in chat
    is not the final quote)."""
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    s = await deps.settings.get(session)
    terms = await agreed_terms(session, deal, s)
    if terms is None:
        raise RuleViolation("no approved quote to agree on")
    issued, price, qty = terms
    version = await create_final_version(
        session, deps, deal, issued, unit_price=price, quantity=qty, actor=actor
    )
    if DealStage(deal.stage) is not DealStage.READY_TO_CLOSE:
        await deal_svc.move_stage(
            session, deal, DealStage.READY_TO_CLOSE, actor, now=deps.now(), reason="buyer agreed"
        )
    return version
