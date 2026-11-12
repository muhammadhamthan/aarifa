"""Deal routes for the team: pipeline list, detail, timeline, stage moves, AI takeover, payment, orders."""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, exists, or_, select
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
    QuoteEditsIn,
    QuoteVersionOut,
    RequirementOut,
    StageMoveIn,
    TimelineOut,
)
from kx.crm.models import (
    Approval,
    ChangeRequest,
    Deal,
    DealRequirement,
    Product,
    Quote,
    QuoteVersion,
    TimelineEvent,
    User,
)
from kx.crm.services import access, approvals, assignment, change_requests, negotiation, quotes
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor

Staff = Annotated[Principal, Depends(require_role(*STAFF))]
Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]
HumanManager = Annotated[Principal, Depends(require_human_mfa(Role.MANAGER))]
HumanStaff = Annotated[Principal, Depends(require_human_mfa(Role.SALES, Role.MANAGER))]


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
            "ai_summary": deal.ai_summary,
            "ai_summary_at": deal.ai_summary_at,
            "personal_contact_shared_at": deal.personal_contact_shared_at,
        }
    )


def visible_to[S: Select[Any]](stmt: S, p: Principal) -> S:
    """§3 "the salesperson sees their own; the manager and MD see all": a salesperson's lists show their
    own deals plus those of an away colleague they cover as backup (§9.3)."""
    if p.role is not Role.SALES:
        return stmt
    covered = select(User.id).where(User.is_away.is_(True), User.backup_user_id == p.user_id)
    return stmt.where(or_(Deal.owner_user_id == p.user_id, Deal.owner_user_id.in_(covered)))


class DealFilters(BaseModel):
    """Query parameters of the §20.3 deals board."""

    stage: DealStage | None = None
    owner_user_id: uuid.UUID | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    product_id: uuid.UUID | None = None
    sku: str | None = None
    min_value_aed: Decimal | None = None
    max_value_aed: Decimal | None = None
    opted_out: bool | None = None
    # §7A.3 step 2 team board: deals nobody owns yet, and deals with an approval or change request past its
    # SLA and escalated to the manager (§16) that is still open
    unassigned: bool | None = None
    escalated: bool | None = None
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


def _escalated() -> Any:
    """An open approval escalated to the manager at 120 minutes, or an open change request past its SLA and
    escalated (§16)."""
    approval = (
        select(Approval.id)
        .join(QuoteVersion, QuoteVersion.id == Approval.quote_version_id)
        .join(Quote, Quote.id == QuoteVersion.quote_id)
        .where(
            Quote.deal_id == Deal.id,
            Approval.status.in_(approvals.OPEN),
            Approval.escalated_at.is_not(None),
        )
    )
    change = select(ChangeRequest.id).where(
        ChangeRequest.deal_id == Deal.id,
        ChangeRequest.status.in_(change_requests.OPEN),
        ChangeRequest.escalated_at.is_not(None),
    )
    return or_(exists(approval), exists(change))


def filtered(stmt: Select[tuple[Deal]], f: DealFilters) -> Select[tuple[Deal]]:
    if f.stage is not None:
        stmt = stmt.where(Deal.stage == f.stage.value)
    if f.owner_user_id is not None:
        stmt = stmt.where(Deal.owner_user_id == f.owner_user_id)
    if f.opted_out is not None:
        stmt = stmt.where(Deal.opted_out_at.is_not(None) if f.opted_out else Deal.opted_out_at.is_(None))
    if f.unassigned is not None:
        stmt = stmt.where(Deal.owner_user_id.is_(None) if f.unassigned else Deal.owner_user_id.is_not(None))
    if f.escalated is not None:
        stmt = stmt.where(_escalated() if f.escalated else ~_escalated())
    req = select(DealRequirement.id).where(DealRequirement.deal_id == Deal.id)
    if f.country:
        stmt = stmt.where(exists(req.where(DealRequirement.delivery_country == f.country.upper())))
    if f.product_id is not None:
        stmt = stmt.where(exists(req.where(DealRequirement.product_id == f.product_id)))
    if f.sku:
        by_sku = req.join(Product, Product.id == DealRequirement.product_id).where(Product.sku == f.sku)
        stmt = stmt.where(exists(by_sku))
    total = (
        select(QuoteVersion.total_aed)
        .join(Quote, Quote.current_version_id == QuoteVersion.id)
        .where(Quote.deal_id == Deal.id)
        .scalar_subquery()
    )
    if f.min_value_aed is not None:
        stmt = stmt.where(total >= f.min_value_aed)
    if f.max_value_aed is not None:
        stmt = stmt.where(total <= f.max_value_aed)
    return stmt


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(prefix="/deals", tags=["deals"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    detail = deal_detail

    @r.get("", response_model=Page[DealSummary])
    async def list_deals(s: Session, p: Staff, f: Annotated[DealFilters, Query()]) -> Page[DealSummary]:
        """§20.3 deals board: by stage, with filters for salesperson, country, product and value (the
        current quote's total in AED). §7A.3 team board: `unassigned=true`, `escalated=true`. A salesperson
        sees only their own (and covered) deals (§3)."""
        stmt = keyset(
            filtered(visible_to(select(Deal), p), f),
            f.cursor,
            f.limit,
            Deal.updated_at,
            Deal.id,
            parse=(datetime.fromisoformat, uuid.UUID),
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), f.limit, lambda d: (d.updated_at, d.id))
        return Page(items=[DealSummary.model_validate(d) for d in rows], next_cursor=nxt)

    @r.get("/{deal_id}", response_model=DealDetail)
    async def get_deal(deal_id: uuid.UUID, s: Session, p: Staff) -> DealDetail:
        """§20.4 deal page: buyer, company, requirement, stage, AI state, AI summary (§7A.2), opt-out."""
        deal = await deal_svc.get_deal(s, deal_id)
        await access.check_view(s, p, deal)
        return await detail(s, deal)

    @r.get("/{deal_id}/timeline", response_model=Page[TimelineOut])
    async def timeline(
        deal_id: uuid.UUID,
        s: Session,
        p: Staff,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Page[TimelineOut]:
        """§20.4 full timeline: every message, call transcript, stage change and AI action (newest first)."""
        await access.check_view(s, p, await deal_svc.get_deal(s, deal_id))
        stmt = keyset(
            select(TimelineEvent).where(TimelineEvent.deal_id == deal_id), cursor, limit, TimelineEvent.id
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), limit, lambda t: (t.id,))
        return Page(items=[TimelineOut.model_validate(t) for t in rows], next_cursor=nxt)

    @r.get("/{deal_id}/quotes", response_model=list[QuoteVersionOut])
    async def deal_quotes(deal_id: uuid.UUID, s: Session, p: Staff) -> list[QuoteVersionOut]:
        """§20.4 quote versions, newest first (the screen compares two side by side)."""
        await access.check_view(s, p, await deal_svc.get_deal(s, deal_id))
        stmt = (
            select(QuoteVersion)
            .join(Quote, Quote.id == QuoteVersion.quote_id)
            .where(Quote.deal_id == deal_id)
            .order_by(QuoteVersion.version_no.desc())
        )
        return [QuoteVersionOut.model_validate(v) for v in (await s.execute(stmt)).scalars()]

    @r.post("/{deal_id}/stage", response_model=DealDetail)
    async def move(deal_id: uuid.UUID, body: StageMoveIn, s: Session, p: Staff) -> DealDetail:
        """Move the deal to another stage (rules in §5; LOST needs a reason). `expected_version` guards
        against overwriting a newer change (409)."""
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
        """Ask the backend to build the quote from the price list (§11.1). The AI never prices; staff approve
        before anything is sent."""
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
            buyer_note=body.buyer_note,
            cover_note=body.cover_note,
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
        """§12 below-minimum alert, manager (MFA): set an approved special price (may be below the range, D3);
        it becomes a price the AI may state, and `resume_ai` hands the chat back to the AI."""
        await negotiation.manager_offer(
            s, deps, deal_id, body.unit_price, actor=Actor.user(p), resume=body.resume_ai
        )

    return r
