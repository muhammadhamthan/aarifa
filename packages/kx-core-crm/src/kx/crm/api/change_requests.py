"""Change-request inbox and card actions (§13.2 quick actions, §20 "My inbox"). Thin: services do the work."""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_role
from kx.core.errors import NotFound
from kx.crm.api.context import STAFF, ApiContext
from kx.crm.api.schemas import ChangeRequestOut, DeclineIn, QuoteEditsIn, QuoteVersionOut, ResolveIn
from kx.crm.models import ChangeRequest, Deal
from kx.crm.services import access, change_requests, quotes
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor

Staff = Annotated[Principal, Depends(require_role(*STAFF))]


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(prefix="/change-requests", tags=["change-requests"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    @r.get("", response_model=list[ChangeRequestOut])
    async def inbox(
        s: Session,
        p: Staff,
        status: Literal["open", "closed"] = "open",
        deal_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> list[ChangeRequestOut]:
        """Open cards with SLA timers, oldest due first. A salesperson sees the cards of their own deals."""
        open_ = change_requests.OPEN
        stmt = select(ChangeRequest).where(
            ChangeRequest.status.in_(open_) if status == "open" else ChangeRequest.status.not_in(open_)
        )
        if deal_id is not None:
            stmt = stmt.where(ChangeRequest.deal_id == deal_id)
        if p.role is Role.SALES:
            stmt = stmt.join(Deal, Deal.id == ChangeRequest.deal_id).where(Deal.owner_user_id == p.user_id)
        rows = (await s.execute(stmt.order_by(ChangeRequest.sla_due_at).limit(limit))).scalars()
        return [ChangeRequestOut.model_validate(c) for c in rows]

    @r.get("/{cr_id}", response_model=ChangeRequestOut)
    async def card(cr_id: uuid.UUID, s: Session, p: Staff) -> ChangeRequestOut:
        cr = await s.get(ChangeRequest, cr_id)
        if cr is None:
            raise NotFound(f"change request {cr_id} not found")
        access.ensure_can_view(p, await deal_svc.get_deal(s, cr.deal_id))
        return ChangeRequestOut.model_validate(cr)

    @r.post("/{cr_id}/start", response_model=ChangeRequestOut)
    async def start(cr_id: uuid.UUID, s: Session, p: Staff) -> ChangeRequestOut:
        return ChangeRequestOut.model_validate(await change_requests.start(s, deps, cr_id, p))

    @r.post("/{cr_id}/revise", response_model=QuoteVersionOut, status_code=201)
    async def revise(cr_id: uuid.UUID, body: QuoteEditsIn, s: Session, p: Staff) -> QuoteVersionOut:
        """ "Revise quote": a new version with the edits; approving it resolves the card (§13.3-4)."""
        cr = await s.get(ChangeRequest, cr_id)
        if cr is None:
            raise NotFound(f"change request {cr_id} not found")
        access.ensure_can_edit(p, await deal_svc.get_deal(s, cr.deal_id))
        edits = quotes.QuoteEdits(
            lines=tuple(quotes.LineEdit(e.line_no, e.unit_price, e.quantity) for e in body.lines),
            delivery_time_text=body.delivery_time_text,
            delivery_place=body.delivery_place,
            incoterm=body.incoterm,
            payment_terms_text=body.payment_terms_text,
            note=body.note or f"Change request ({cr.type})",
        )
        version = await quotes.revise_quote(s, deps, cr.deal_id, edits, Actor.user(p))
        await change_requests.link_revision(s, deps, cr_id, version.id, p)
        return QuoteVersionOut.model_validate(version)

    @r.post("/{cr_id}/resolve", response_model=ChangeRequestOut)
    async def resolve(cr_id: uuid.UUID, body: ResolveIn, s: Session, p: Staff) -> ChangeRequestOut:
        return ChangeRequestOut.model_validate(
            await change_requests.resolve(s, deps, cr_id, body.version_id, p)
        )

    @r.post("/{cr_id}/decline", response_model=ChangeRequestOut)
    async def decline(cr_id: uuid.UUID, body: DeclineIn, s: Session, p: Staff) -> ChangeRequestOut:
        return ChangeRequestOut.model_validate(await change_requests.decline(s, deps, cr_id, body.reason, p))

    return r
