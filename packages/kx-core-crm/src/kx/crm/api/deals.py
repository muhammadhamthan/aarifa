"""Deal routes for the team: pipeline list, detail, timeline, stage moves, AI takeover, payment, orders."""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.core.contracts import DealStage
from kx.crm.api.context import MANAGERS, STAFF, ApiContext
from kx.crm.api.pagination import Page, keyset, page_of
from kx.crm.api.schemas import (
    AiToggleIn,
    ContactOut,
    DealDetail,
    DealSummary,
    ManagerOfferIn,
    OrderOut,
    OrderStatusIn,
    PaymentIn,
    QuoteEditsIn,
    QuoteVersionOut,
    RequirementOut,
    StageMoveIn,
    TimelineOut,
)
from kx.crm.models import Deal, Quote, QuoteVersion, TimelineEvent
from kx.crm.services import access, assignment, negotiation, orders, quotes
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor

Staff = Annotated[Principal, Depends(require_role(*STAFF))]
Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]
HumanManager = Annotated[Principal, Depends(require_human_mfa(Role.MANAGER))]


async def deal_detail(s: AsyncSession, deal: Deal) -> DealDetail:
    contact = await deal_svc.contact_of(s, deal)
    reqs = await deal_svc.requirements_of(s, deal.id)
    return DealDetail.model_validate(
        {
            **DealSummary.model_validate(deal).model_dump(),
            "contact": ContactOut.model_validate(contact),
            "requirements": [RequirementOut.model_validate(x) for x in reqs],
            "missing_fields": list(await deal_svc.missing_for(s, deal)),
            "lost_reason": deal.lost_reason,
            "lost_note": deal.lost_note,
        }
    )


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(prefix="/deals", tags=["deals"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    detail = deal_detail

    @r.get("", response_model=Page[DealSummary])
    async def list_deals(
        s: Session,
        _: Staff,
        stage: DealStage | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Page[DealSummary]:
        stmt = select(Deal)
        if stage is not None:
            stmt = stmt.where(Deal.stage == stage.value)
        stmt = keyset(
            stmt, cursor, limit, Deal.updated_at, Deal.id, parse=(datetime.fromisoformat, uuid.UUID)
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), limit, lambda d: (d.updated_at, d.id))
        return Page(items=[DealSummary.model_validate(d) for d in rows], next_cursor=nxt)

    @r.get("/{deal_id}", response_model=DealDetail)
    async def get_deal(deal_id: uuid.UUID, s: Session, p: Staff) -> DealDetail:
        deal = await deal_svc.get_deal(s, deal_id)
        await access.check_view(s, p, deal)
        return await detail(s, deal)

    @r.get("/{deal_id}/timeline", response_model=Page[TimelineOut])
    async def timeline(
        deal_id: uuid.UUID,
        s: Session,
        _: Staff,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Page[TimelineOut]:
        stmt = keyset(
            select(TimelineEvent).where(TimelineEvent.deal_id == deal_id), cursor, limit, TimelineEvent.id
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), limit, lambda t: (t.id,))
        return Page(items=[TimelineOut.model_validate(t) for t in rows], next_cursor=nxt)

    @r.get("/{deal_id}/quotes", response_model=list[QuoteVersionOut])
    async def deal_quotes(deal_id: uuid.UUID, s: Session, _: Staff) -> list[QuoteVersionOut]:
        stmt = (
            select(QuoteVersion)
            .join(Quote, Quote.id == QuoteVersion.quote_id)
            .where(Quote.deal_id == deal_id)
            .order_by(QuoteVersion.version_no.desc())
        )
        return [QuoteVersionOut.model_validate(v) for v in (await s.execute(stmt)).scalars()]

    @r.post("/{deal_id}/stage", response_model=DealDetail)
    async def move(deal_id: uuid.UUID, body: StageMoveIn, s: Session, p: Staff) -> DealDetail:
        deal = await deal_svc.get_deal(s, deal_id, lock=True)
        await access.check_edit(s, p, deal)
        await deal_svc.move_stage(
            s,
            deal,
            body.to,
            Actor.user(p),
            now=deps.now(),
            reason=body.reason,
            lost_reason=body.lost_reason,
            lost_note=body.lost_note,
            expected_version=body.expected_version,
        )
        if body.to is DealStage.QUALIFIED and deal.owner_user_id is None:  # §5 step 4
            await assignment.assign(s, deps, deal, Actor.user(p))
        return await detail(s, deal)

    @r.post("/{deal_id}/quotes", response_model=QuoteVersionOut, status_code=201)
    async def request_quote(deal_id: uuid.UUID, s: Session, p: Staff) -> QuoteVersionOut:
        await access.check_edit(s, p, await deal_svc.get_deal(s, deal_id))
        return QuoteVersionOut.model_validate(await quotes.request_quote(s, deps, deal_id, Actor.user(p)))

    @r.post("/{deal_id}/quotes/revise", response_model=QuoteVersionOut, status_code=201)
    async def revise_quote(deal_id: uuid.UUID, body: QuoteEditsIn, s: Session, p: Staff) -> QuoteVersionOut:
        """§11.3 Edit then approve: a new version; below the minimum goes to the manager first (D3)."""
        await access.check_edit(s, p, await deal_svc.get_deal(s, deal_id))
        edits = quotes.QuoteEdits(
            lines=tuple(quotes.LineEdit(e.line_no, e.unit_price, e.quantity) for e in body.lines),
            delivery_time_text=body.delivery_time_text,
            delivery_place=body.delivery_place,
            incoterm=body.incoterm,
            payment_terms_text=body.payment_terms_text,
            note=body.note,
        )
        return QuoteVersionOut.model_validate(
            await quotes.revise_quote(s, deps, deal_id, edits, Actor.user(p))
        )

    @r.post("/{deal_id}/ai", response_model=DealDetail)
    async def toggle_ai(deal_id: uuid.UUID, body: AiToggleIn, s: Session, p: Manager) -> DealDetail:
        """Manager/MD takes over (AI off) or hands the conversation back (AI on)."""
        deal = await deal_svc.get_deal(s, deal_id, lock=True)
        if body.enabled and not deal.ai_enabled:
            await negotiation.resume_ai(s, deal, Actor.user(p))
        elif not body.enabled and deal.ai_enabled:
            reason = "md_takeover" if p.role is Role.MD and body.reason == "human_takeover" else body.reason
            await negotiation.pause_ai(
                s, deps, deal, reason=reason, event_reason="manual", actor=Actor.user(p)
            )
        return await detail(s, deal)

    @r.post("/{deal_id}/offer", status_code=204)
    async def manager_offer(deal_id: uuid.UUID, body: ManagerOfferIn, s: Session, p: HumanManager) -> None:
        await negotiation.manager_offer(
            s, deps, deal_id, body.unit_price, actor=Actor.user(p), resume=body.resume_ai
        )

    @r.post("/{deal_id}/payments", response_model=OrderOut, status_code=201)
    async def payment(deal_id: uuid.UUID, body: PaymentIn, s: Session, p: HumanManager) -> OrderOut:
        order = await orders.record_payment(s, deps, deal_id, Actor.user(p), **body.model_dump())
        return OrderOut.model_validate(order)

    @r.patch("/orders/{order_id}", response_model=OrderOut)
    async def order_status(order_id: uuid.UUID, body: OrderStatusIn, s: Session, p: Manager) -> OrderOut:
        order = await orders.update_order_status(
            s,
            deps,
            order_id,
            body.status,
            Actor.user(p),
            on_date=body.on_date,
            tracking_ref=body.tracking_ref,
        )
        return OrderOut.model_validate(order)

    return r
