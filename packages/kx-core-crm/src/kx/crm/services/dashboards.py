"""§21 dashboard figures shared by the dashboards API and the scheduled reports (one definition each;
the formulas the SRS leaves open are OPEN_QUESTIONS #63, documented in kx/crm/api/reports.py)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Select, distinct, func, literal_column, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from kx.crm.models import (
    Approval,
    AuditEvent,
    ChangeRequest,
    Consent,
    Deal,
    Quote,
    QuoteVersion,
    Task,
    User,
)
from kx.crm.rules import OPEN_STAGES

OPEN = [st.value for st in OPEN_STAGES]
PRICE_LIST_ACTIONS = ("price_tier.created", "price_tier.updated", "price_tier.deleted", "price_list.imported")


class SalespersonBoard(BaseModel):
    user_id: uuid.UUID
    name: str
    open_deals_by_stage: dict[str, int]
    waiting_for_me: int  # open approvals + open tasks + open change requests assigned to me
    avg_response_minutes: float | None  # approvals submitted -> decided
    quotes_sent: int
    deals_won: int


class ManagerBoard(BaseModel):
    period_from: date
    period_to: date
    salespeople: list[SalespersonBoard]
    below_minimum_requests: int
    change_requests_by_type: dict[str, int]
    sla_breaches: int  # approvals escalated + change requests closed after (or still open past) their SLA
    price_list_changes: int
    opted_out: int
    opt_out_reasons: dict[str, int]


async def counts(s: AsyncSession, stmt: Select[*tuple[Any, ...]]) -> dict[str, int]:
    return {str(k): int(v) for k, v in (await s.execute(stmt)).tuples().all() if k is not None}


async def salesperson_board(
    s: AsyncSession, user: User, since: datetime, until: datetime
) -> SalespersonBoard:
    mine = Deal.owner_user_id == user.id
    by_stage = await counts(
        s, select(Deal.stage, func.count()).where(mine, Deal.stage.in_(OPEN)).group_by(Deal.stage)
    )
    waiting = sum(
        [
            await s.scalar(
                select(func.count()).where(
                    Approval.approver_user_id == user.id, Approval.status.in_(("pending", "overdue"))
                )
            )
            or 0,
            await s.scalar(
                select(func.count()).where(Task.assigned_user_id == user.id, Task.status == "open")
            )
            or 0,
            await s.scalar(
                select(func.count()).where(
                    ChangeRequest.assigned_to == user.id, ChangeRequest.status.in_(("open", "in_progress"))
                )
            )
            or 0,
        ]
    )
    response = await s.scalar(
        select(func.avg(func.extract("epoch", Approval.decided_at - Approval.submitted_at) / 60)).where(
            Approval.decided_by == user.id, Approval.decided_at >= since, Approval.decided_at < until
        )
    )
    sent = await s.scalar(
        select(func.count())
        .select_from(QuoteVersion)
        .join(Quote, Quote.id == QuoteVersion.quote_id)
        .join(Deal, Deal.id == Quote.deal_id)
        .where(
            mine,
            QuoteVersion.status == "sent",
            QuoteVersion.created_at >= since,
            QuoteVersion.created_at < until,
        )
    )
    won = await s.scalar(select(func.count()).where(mine, Deal.won_at >= since, Deal.won_at < until))
    return SalespersonBoard(
        user_id=user.id,
        name=user.full_name,
        open_deals_by_stage=by_stage,
        waiting_for_me=waiting,
        avg_response_minutes=float(response) if response is not None else None,
        quotes_sent=sent or 0,
        deals_won=won or 0,
    )


async def opt_outs(s: AsyncSession, since: datetime, until: datetime) -> tuple[int, dict[str, int]]:
    count = await s.scalar(select(func.count()).where(Deal.opted_out_at >= since, Deal.opted_out_at < until))
    reason = Consent.evidence["reason"].astext
    reasons = await counts(
        s,
        # one stop writes a consent row per channel in one transaction (same `at`): count each stop once
        select(
            func.coalesce(reason, "(none given)"),
            func.count(distinct(tuple_(Consent.contact_id, Consent.at))),
        )
        .where(Consent.status == "revoked", Consent.at >= since, Consent.at < until)
        .group_by(literal_column("1")),
    )
    return count or 0, reasons


async def manager_board(
    s: AsyncSession, start: date, end: date, since: datetime, until: datetime
) -> ManagerBoard:
    """§21 manager: everyone's board, below-minimum requests, change requests by type, SLA breaches,
    price-list changes, plus opt-outs and the buyers' own reasons (§8.1). `since`/`until` are UTC bounds."""
    sales = (
        await s.execute(select(User).where(User.role == "sales", User.is_active).order_by(User.full_name))
    ).scalars()
    boards = [await salesperson_board(s, u, since, until) for u in sales]
    in_period = (ChangeRequest.created_at >= since, ChangeRequest.created_at < until)
    late_cr = await s.scalar(
        select(func.count()).where(
            *in_period,
            func.coalesce(ChangeRequest.closed_at, func.now()) > ChangeRequest.sla_due_at,
        )
    )
    escalated = await s.scalar(
        select(func.count()).where(Approval.escalated_at >= since, Approval.escalated_at < until)
    )
    opted, reasons = await opt_outs(s, since, until)
    return ManagerBoard(
        period_from=start,
        period_to=end,
        salespeople=boards,
        below_minimum_requests=await s.scalar(
            select(func.count()).where(
                Task.kind == "below_range_review", Task.created_at >= since, Task.created_at < until
            )
        )
        or 0,
        change_requests_by_type=await counts(
            s, select(ChangeRequest.type, func.count()).where(*in_period).group_by(ChangeRequest.type)
        ),
        sla_breaches=(late_cr or 0) + (escalated or 0),
        price_list_changes=await s.scalar(
            select(func.count()).where(
                AuditEvent.action.in_(
                    (
                        "price_tier.created",
                        "price_tier.updated",
                        "price_tier.deleted",
                        "price_list.imported",
                    )
                ),
                AuditEvent.at >= since,
                AuditEvent.at < until,
            )
        )
        or 0,
        opted_out=opted,
        opt_out_reasons=reasons,
    )
