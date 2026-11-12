"""Who may act on a deal (the SRS §3, D1, D2, D5). Pure checks on loaded rows.

* Approve and send quotes: the deal's salesperson, the manager (any deal), and the MD only on a deal it
  has taken over (D1). A price below the tier minimum needs a manager-approved `special_price` (D3).
* MD takeover: the salesperson keeps view access but cannot edit or approve until hand-back (§3).
* A salesperson works on their own deals only (§3 "the salesperson sees their own").
* Every approval needs a person signed in with MFA (`require_human_mfa` at the route, again here).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role
from kx.core.errors import Forbidden
from kx.crm.models import Approval, Deal
from kx.crm.services import staff

TAKEN_OVER = "the MD has taken over this deal: you can view it until it is handed back"


def _mine(p: Principal, deal: Deal, covering: uuid.UUID | None) -> bool:
    """The salesperson's own deal, or one they cover for an away colleague (§9.3)."""
    return p.user_id is not None and p.user_id in (deal.owner_user_id, covering)


def ensure_can_edit(p: Principal, deal: Deal, *, covering: uuid.UUID | None = None) -> None:
    """Edits by staff (quotes, requirement, stage, change requests). 403 for the owner during a takeover."""
    if p.role is Role.SALES:
        if deal.taken_over_by is not None:
            raise Forbidden(TAKEN_OVER)
        if not _mine(p, deal, covering):
            raise Forbidden("this deal is assigned to another salesperson")


def ensure_can_view(p: Principal, deal: Deal, *, covering: uuid.UUID | None = None) -> None:
    """§3: the salesperson sees their own deals (also while the MD has taken one over); others see all."""
    if p.role is Role.SALES and not _mine(p, deal, covering):
        raise Forbidden("this deal is assigned to another salesperson")


async def check_edit(session: AsyncSession, p: Principal, deal: Deal) -> None:
    covering = await staff.covering_user(session, deal) if p.role is Role.SALES else None
    ensure_can_edit(p, deal, covering=covering)


async def check_contact_buyer(session: AsyncSession, p: Principal, deal: Deal) -> None:
    """§14 / WP17: who may write to or call the buyer from the CRM: the deal's salesperson (or the colleague
    covering them), the manager, and the MD on a deal it has taken over (the §3 "approve and send" rule).
    Admin / IT never talks to buyers."""
    if p.role is Role.ADMIN:
        raise Forbidden("admin accounts do not contact buyers")
    if p.role is Role.MD and not _md_on_takeover(p, deal):
        raise Forbidden("take the deal over to contact the buyer")
    await check_edit(session, p, deal)


async def check_view(session: AsyncSession, p: Principal, deal: Deal) -> None:
    covering = await staff.covering_user(session, deal) if p.role is Role.SALES else None
    ensure_can_view(p, deal, covering=covering)


def _md_on_takeover(p: Principal, deal: Deal) -> bool:
    return p.role is Role.MD and deal.taken_over_by is not None and deal.taken_over_by == p.user_id


def ensure_can_approve(
    p: Principal, deal: Deal, approval: Approval, *, covering: uuid.UUID | None = None
) -> None:
    """D1 / §3 "Approve and send quotes", D3 special price, plus the optional MD value limit."""
    if not p.is_human or not p.mfa:
        raise Forbidden("approvals need a person signed in with MFA")
    if approval.kind == "special_price":  # D3: below the minimum -> the manager
        if p.role is Role.MANAGER or _md_on_takeover(p, deal):
            return
        raise Forbidden("a price below the minimum needs the manager's approval")
    if approval.required_role == "md" and deal.taken_over_by is None:
        # approval.md_value_limit_aed is set and the quote is above it (off in v1 by default)
        if p.role is Role.MD:
            return
        raise Forbidden("this quote is above the MD value limit: the MD approves it")
    match p.role:
        case Role.MANAGER:
            return  # D1: the manager may approve any deal
        case Role.MD if _md_on_takeover(p, deal):
            return
        case Role.MD:
            raise Forbidden("the MD approves only deals it has taken over")
        case Role.SALES:
            ensure_can_edit(p, deal, covering=covering)
            return
        case _:
            raise Forbidden("approvals are for the deal's salesperson, the manager or the MD on takeover")
