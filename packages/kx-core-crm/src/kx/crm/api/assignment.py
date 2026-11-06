"""Reassign, MD takeover and hand-back (§9.4, §3, D5). Thin: kx.crm.services.assignment does the work."""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_role
from kx.crm.api.context import MANAGERS, ApiContext
from kx.crm.api.deals import deal_detail
from kx.crm.api.schemas import DealDetail, ReassignIn
from kx.crm.services import assignment

Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]
MdOnly = Annotated[Principal, Depends(require_role(Role.MD))]


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
        return await deal_detail(s, await assignment.hand_back(s, deps, deal_id, p))

    return r
