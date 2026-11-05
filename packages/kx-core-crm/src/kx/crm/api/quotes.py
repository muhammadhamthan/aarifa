"""Quote versions (buyer-facing lines for staff) and MD-only cost snapshots; approval decisions."""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.core.errors import NotFound
from kx.crm.api.context import MANAGERS, STAFF, ApiContext
from kx.crm.api.pagination import Page, keyset, page_of
from kx.crm.api.schemas import ApprovalOut, DecisionIn, LineCostOut, QuoteLineOut, QuoteVersionOut
from kx.crm.models import Approval, QuoteVersion
from kx.crm.services import approvals, quotes
from kx.crm.services.issuing import lines_of

Staff = Annotated[Principal, Depends(require_role(*STAFF))]
Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]
MdOnly = Annotated[Principal, Depends(require_role(Role.MD))]
# D1: the salesperson, the manager or the MD on takeover; the service checks which deal (access.py)
Approver = Annotated[Principal, Depends(require_human_mfa(Role.SALES, Role.MANAGER, Role.MD))]


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(tags=["quotes"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806

    async def version_or_404(s: AsyncSession, version_id: uuid.UUID) -> QuoteVersion:
        if (v := await s.get(QuoteVersion, version_id)) is None:
            raise NotFound(f"quote version {version_id} not found")
        return v

    @r.get("/quote-versions/{version_id}", response_model=QuoteVersionOut)
    async def get_version(version_id: uuid.UUID, s: Session, _: Staff) -> QuoteVersionOut:
        v = await version_or_404(s, version_id)
        lines = [QuoteLineOut.model_validate(ln) for ln in await lines_of(s, v.id)]
        return QuoteVersionOut.model_validate(v).model_copy(update={"lines": lines})

    @r.get("/quote-versions/{version_id}/costs", response_model=list[LineCostOut])
    async def get_costs(version_id: uuid.UUID, s: Session, _: MdOnly) -> list[LineCostOut]:
        """Cost build-up of each line: MD only (T3)."""
        await version_or_404(s, version_id)
        return [
            LineCostOut.model_validate(
                {
                    "line_no": line.line_no,
                    **{f: getattr(cost, f) for f in LineCostOut.model_fields if f != "line_no"},
                }
            )
            for line, cost in await quotes.costs_of(s, version_id)
        ]

    @r.get("/approvals", response_model=Page[ApprovalOut])
    async def list_approvals(
        s: Session,
        _: Manager,
        status: Literal["open", "decided"] = "open",
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Page[ApprovalOut]:
        cond = (
            Approval.status.in_(approvals.OPEN)
            if status == "open"
            else Approval.status.not_in(approvals.OPEN)
        )
        stmt = keyset(
            select(Approval).where(cond),
            cursor,
            limit,
            Approval.submitted_at,
            Approval.id,
            parse=(datetime.fromisoformat, uuid.UUID),
        )
        rows, nxt = page_of(list((await s.execute(stmt)).scalars()), limit, lambda a: (a.submitted_at, a.id))
        return Page(items=[ApprovalOut.model_validate(a) for a in rows], next_cursor=nxt)

    @r.post("/approvals/{approval_id}/decision", response_model=ApprovalOut)
    async def decide(approval_id: uuid.UUID, body: DecisionIn, s: Session, p: Approver) -> ApprovalOut:
        """Human + MFA only. Whose deal it is, takeover and special price are checked in the service."""
        return ApprovalOut.model_validate(
            await approvals.decide(s, ctx.deps, approval_id, p, decision=body.decision, note=body.note)
        )

    return r
