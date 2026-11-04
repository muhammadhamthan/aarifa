"""Deterministic negotiation (T5/T6). The engine decides; this module loads its state and records rounds.

* State comes from the latest ISSUED version (approved/sent) and its MD-only cost snapshot; the per-piece
  cost is rebuilt UNROUNDED (cost/1000 / 1000) so the ladder top equals the quoted price exactly.
* Max concessions per deal come from the margin rule; concessions are traded for quantity, never for
  longer delivery (DB CHECK on negotiation_rounds.traded_for).
* Below range / exhausted: the AI tells the buyer "within 60 minutes", AI is paused for the deal, and
  the manager + MD are alerted immediately (NEGOTIATION_PAUSED).
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
from kx.core.contracts import CounterVerdict, CounterVerdictKind, DealStage
from kx.core.errors import RuleViolation
from kx.core.event_payloads import NegotiationPaused
from kx.crm.deps import CrmDeps
from kx.crm.models import Deal, MarginRuleRow, NegotiationRound, QuoteLine, QuoteLineCost, QuoteVersion, Task
from kx.crm.pricing.engine import evaluate_counter as engine_counter
from kx.crm.pricing.engine import unit_cost_per_1000
from kx.crm.pricing.types import CostInputs, MarginBand, NegotiationState
from kx.crm.rules import PauseReason, round_verdict
from kx.crm.services import deals as deal_svc
from kx.crm.services import staff
from kx.crm.services.issuing import local_today
from kx.crm.services.journal import Actor, Emit, Timeline, record
from kx.crm.services.quotes import ISSUED, costs_of, create_final_version, latest_version
from kx.crm.services.settings import BusinessSettings

THOUSAND = Decimal(1000)
EventReason = Literal["below_range", "concessions_exhausted", "exception", "manual"]


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
    neg = await load_state(session, deal, s)
    if neg is None:
        raise RuleViolation("no approved quote to negotiate on")
    session.add(
        NegotiationRound(
            deal_id=deal.id,
            quote_version_id=neg.version.id,
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
            f"Manager approved {neg.version.currency} {unit_price}/pc",
            deal.id,
            deal.primary_contact_id,
        ),
    )
    if resume:
        await resume_ai(session, deal, actor)


async def mark_agreed(session: AsyncSession, deps: CrmDeps, deal_id: uuid.UUID, actor: Actor) -> QuoteVersion:
    """Buyer accepted the current offer: READY_TO_CLOSE and a 'final' version at the agreed price and
    quantity, which goes to the MD (approval 2) before the final quote is sent."""
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    s = await deps.settings.get(session)
    neg = await load_state(session, deal, s)
    if neg is None:
        raise RuleViolation("no approved quote to agree on")
    st = neg.state
    version = await create_final_version(
        session,
        deps,
        deal,
        neg.version,
        unit_price=st.current_offer_unit_price,
        quantity=st.base_quantity,
        actor=actor,
    )
    if DealStage(deal.stage) is not DealStage.READY_TO_CLOSE:
        await deal_svc.move_stage(
            session, deal, DealStage.READY_TO_CLOSE, actor, now=deps.now(), reason="buyer agreed"
        )
    return version
