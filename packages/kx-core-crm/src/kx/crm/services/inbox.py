"""§20.2 "My inbox": quotes to approve, change requests, below-minimum alerts, pricing-needed and
LOST-proposal tasks, buyer messages on handed-over deals, each with its SLA timer. A salesperson sees
what is assigned to them (and what they cover for an away colleague); the manager and MD see everything."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role
from kx.crm.models import Approval, ChangeRequest, Deal, Quote, QuoteVersion, Task, TimelineEvent, User
from kx.crm.services import approvals, change_requests

ItemKind = Literal[
    "approval",
    "special_price",
    "change_request",
    "below_minimum",
    "pricing_needed",
    "lost_proposal",
    "buyer_message",
]
_TASKS: Final[dict[str, ItemKind]] = {
    "below_range_review": "below_minimum",
    "pricing_needed": "pricing_needed",
    "lost_proposal": "lost_proposal",
}


@dataclass(frozen=True, slots=True)
class InboxItem:
    kind: ItemKind
    ref_id: str
    deal_id: uuid.UUID
    deal_number: str
    title: str
    due_at: datetime | None
    created_at: datetime
    overdue: bool


async def _people(session: AsyncSession, p: Principal) -> list[uuid.UUID] | None:
    """None = everyone (manager, MD); else the user and the away colleagues they back up (§9.3)."""
    if p.has(Role.MANAGER) or p.user_id is None:
        return None
    covered = (
        await session.execute(select(User.id).where(User.backup_user_id == p.user_id, User.is_away))
    ).scalars()
    return [p.user_id, *covered]


async def inbox_for(session: AsyncSession, p: Principal, now: datetime, limit: int = 200) -> list[InboxItem]:
    who = await _people(session, p)
    items: list[InboxItem] = []

    stmt = (
        select(Approval, Deal.id, Deal.number, Quote.number, QuoteVersion.version_no)
        .join(QuoteVersion, QuoteVersion.id == Approval.quote_version_id)
        .join(Quote, Quote.id == QuoteVersion.quote_id)
        .join(Deal, Deal.id == Quote.deal_id)
        .where(Approval.status.in_(approvals.OPEN))
    )
    if who is not None:
        stmt = stmt.where(Approval.assigned_user_id.in_(who))
    for a, deal_id, number, qnum, vno in (await session.execute(stmt.limit(limit))).tuples():
        kind: ItemKind = "special_price" if a.kind == "special_price" else "approval"
        items.append(
            InboxItem(
                kind,
                str(a.id),
                deal_id,
                number,
                f"Quote {qnum} v{vno} ({a.kind})",
                a.deadline_at,
                a.submitted_at,
                a.deadline_at <= now,
            )
        )

    cr_stmt = (
        select(ChangeRequest, Deal.number)
        .join(Deal, Deal.id == ChangeRequest.deal_id)
        .where(ChangeRequest.status.in_(change_requests.OPEN))
    )
    if who is not None:
        cr_stmt = cr_stmt.where(ChangeRequest.assigned_to.in_(who))
    for cr, number in (await session.execute(cr_stmt.limit(limit))).tuples():
        items.append(
            InboxItem(
                "change_request",
                str(cr.id),
                cr.deal_id,
                number,
                f"Change request ({cr.type}): {cr.buyer_text[:120]}",
                cr.sla_due_at,
                cr.created_at,
                cr.sla_due_at <= now,
            )
        )

    t_stmt = (
        select(Task, Deal.number)
        .join(Deal, Deal.id == Task.deal_id)
        .where(Task.status == "open", Task.kind.in_(tuple(_TASKS)))
    )
    if who is not None:
        t_stmt = t_stmt.where(Task.assigned_user_id.in_(who))
    for task, number in (await session.execute(t_stmt.limit(limit))).tuples():
        assert task.deal_id is not None
        items.append(
            InboxItem(
                _TASKS[task.kind],
                str(task.id),
                task.deal_id,
                number,
                task.kind.replace("_", " "),
                task.due_at,
                task.created_at,
                task.due_at is not None and task.due_at <= now,
            )
        )

    m_stmt = (
        select(TimelineEvent, Deal.number)
        .join(Deal, Deal.id == TimelineEvent.deal_id)
        .where(
            Deal.ai_paused_reason == "handover",
            TimelineEvent.kind == "message_in",
            or_(Deal.ai_paused_at.is_(None), TimelineEvent.at >= Deal.ai_paused_at),
        )
        .order_by(TimelineEvent.at.desc())
    )
    if who is not None:
        m_stmt = m_stmt.where(and_(Deal.owner_user_id.in_(who), Deal.taken_over_by.is_(None)))
    for ev, number in (await session.execute(m_stmt.limit(limit))).tuples():
        assert ev.deal_id is not None
        items.append(
            InboxItem("buyer_message", str(ev.id), ev.deal_id, number, ev.summary[:200], None, ev.at, False)
        )

    items.sort(key=lambda i: (i.due_at is None, i.due_at or i.created_at))
    return items[:limit]
