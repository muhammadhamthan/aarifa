"""Reassign, MD takeover and hand-back (§9.4, §3, D5). Thin: kx.crm.services.assignment does the work."""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_role
from kx.crm.api.context import MANAGERS, STAFF, ApiContext
from kx.crm.api.deals import deal_detail
from kx.crm.api.schemas import DealDetail, ManualQuoteIn, QuoteVersionOut, ReassignIn
from kx.crm.services import access, assignment, quotes
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor

Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]
MdOnly = Annotated[Principal, Depends(require_role(Role.MD))]
Staff = Annotated[Principal, Depends(require_role(*STAFF))]


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(prefix="/deals", tags=["deals"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    @r.post("/{deal_id}/reassign", response_model=DealDetail)
    async def reassign(deal_id: uuid.UUID, body: ReassignIn, s: Session, p: Manager) -> DealDetail:
        """§9.4: the manager or MD reassigns; both salespersons are notified, pending items follow."""
        return await deal_detail(s, await assignment.reassign(s, deps, deal_id, body.user_id, p))

    @r.post("/{deal_id}/takeover", response_model=DealDetail)
    async def take_over(deal_id: uuid.UUID, s: Session, p: MdOnly) -> DealDetail:
        """§3: the MD takes the deal over; the salesperson is read-only until hand-back."""
        return await deal_detail(s, await assignment.take_over(s, deps, deal_id, p))

    @r.post("/{deal_id}/handback", response_model=DealDetail)
    async def hand_back(deal_id: uuid.UUID, s: Session, p: MdOnly) -> DealDetail:
        """End an MD or manager takeover: the salesperson can edit and approve again (§3)."""
        return await deal_detail(s, await assignment.hand_back(s, deps, deal_id, p))

    @r.post("/{deal_id}/quotes/manual", response_model=QuoteVersionOut, status_code=201)
    async def manual_quote(deal_id: uuid.UUID, body: ManualQuoteIn, s: Session, p: Staff) -> QuoteVersionOut:
        """D8 / §23 scenario 5: a quote priced by hand by the salesperson, then approved as usual."""
        await access.check_edit(s, p, await deal_svc.get_deal(s, deal_id))
        version = await quotes.manual_quote(
            s,
            deps,
            deal_id,
            [
                quotes.ManualLine(x.description, x.quantity, x.unit_price, x.unit, x.product_id)
                for x in body.lines
            ],
            delivery_time_text=body.delivery_time_text,
            buyer_note=body.buyer_note,
            actor=Actor.user(p),
        )
        return QuoteVersionOut.model_validate(version)

    return r
