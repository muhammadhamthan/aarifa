"""Human approval of every buyer-facing quote.

Approval 1 'initial' (commercial quote): a manager, or the MD when the total exceeds
approval.md_value_limit_aed. Approval 2 'final' (the agreed quote after negotiation): ALWAYS the MD
(T2; the DB CHECK enforces it too). Either approval releases that version to the buyer with its own
single-use send grant. Each step has a 2 h window measured only inside manager alert hours
(08:00-22:00 Dubai), so a quote submitted at 21:30 is due at 09:30 next day, not 23:30.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.auth import Principal, Role
from kx.core.errors import Conflict, Forbidden, NotFound, RuleViolation
from kx.core.event_payloads import ApprovalOverdue, QuoteCorrectionRequested, QuoteRejected, QuoteSubmitted
from kx.core.identity import new_id
from kx.core.money import display
from kx.crm.deps import CrmDeps
from kx.crm.models import Approval, ApprovalEvent, Deal, Quote, QuoteVersion
from kx.crm.services import staff
from kx.crm.services.issuing import issue_to_buyer
from kx.crm.services.journal import Actor, Emit, Timeline, record
from kx.crm.services.settings import BusinessSettings

ApprovalKind = Literal["initial", "final"]
Decision = Literal["approve", "reject", "correct"]
OPEN = ("pending", "overdue")
_PAYLOAD_KIND: dict[str, Literal["commercial", "final"]] = {"initial": "commercial", "final": "final"}


def initial_role(total_aed: object, s: BusinessSettings) -> Literal["manager", "md"]:
    limit = s.md_value_limit_aed
    return "md" if limit is not None and total_aed > limit else "manager"  # type: ignore[operator]


async def _context(session: AsyncSession, version_id: uuid.UUID) -> tuple[QuoteVersion, Quote, Deal]:
    row = (
        await session.execute(
            select(QuoteVersion, Quote, Deal)
            .join(Quote, Quote.id == QuoteVersion.quote_id)
            .join(Deal, Deal.id == Quote.deal_id)
            .where(QuoteVersion.id == version_id)
        )
    ).one()
    return row.tuple()


async def open_approval(
    session: AsyncSession,
    deps: CrmDeps,
    s: BusinessSettings,
    *,
    deal: Deal,
    quote: Quote,
    version: QuoteVersion,
    kind: ApprovalKind,
    role: Literal["manager", "md"],
    actor: Actor,
    now: datetime,
) -> Approval:
    """Open one approval step and emit QUOTE_SUBMITTED (automation alerts the approver; the MD is
    `escalate_to` on every quote so they are always informed)."""
    md = await staff.md_user(session)
    approver = md if role == "md" else await staff.manager_for(session, deal)
    due = s.alert_window().add_open_minutes(now, s.approval_window_minutes)
    approval = Approval(
        id=new_id(),
        quote_version_id=version.id,
        kind=kind,
        required_role=role,
        assigned_user_id=approver.id,
        window_minutes=s.approval_window_minutes,
        submitted_at=now,
        deadline_at=due,
    )
    session.add(approval)
    await session.flush()
    total_display = f"{version.currency} {display(version.total, version.currency):,}"
    await record(
        session,
        actor,
        f"approval.opened.{kind}",
        "approval",
        approval.id,
        after={"quote_version_id": version.id, "required_role": role, "deadline_at": due},
        timeline=Timeline(
            "approval",
            f"Quote {quote.number} v{version.version_no} awaits "
            f"{'MD' if role == 'md' else 'manager'} {kind} approval ({total_display})",
            deal.id,
            deal.primary_contact_id,
            ref_type="approval",
            ref_id=str(approval.id),
        ),
        emit=Emit(
            ev.QUOTE_SUBMITTED,
            "quote",
            quote.id,
            QuoteSubmitted(
                approval_ref=approval.id,
                quote_ref=quote.id,
                quote_version_ref=version.id,
                deal_ref=deal.id,
                kind=_PAYLOAD_KIND[kind],
                required_role=role,
                approver=staff.staff_contact(approver),
                escalate_to=None if approver.id == md.id else staff.staff_contact(md),
                due_at=due,
                quote_number=quote.number,
                total_display=total_display,
            ),
        ),
    )
    return approval


def _authorise(principal: Principal, approval: Approval) -> None:
    """Defence in depth behind require_human_mfa: people with MFA only, MD for MD steps."""
    if not principal.is_human or not principal.mfa:
        raise Forbidden("approvals need a person signed in with MFA")
    needed = Role.MD if approval.required_role == "md" else Role.MANAGER
    if not principal.has(needed):
        raise Forbidden(f"this approval requires the {needed.value}")


async def decide(
    session: AsyncSession,
    deps: CrmDeps,
    approval_id: uuid.UUID,
    principal: Principal,
    *,
    decision: Decision,
    note: str | None = None,
) -> Approval:
    approval = await session.get(Approval, approval_id, with_for_update=True)
    if approval is None:
        raise NotFound(f"approval {approval_id} not found")
    _authorise(principal, approval)
    if approval.status not in OPEN:
        raise Conflict(f"approval is already {approval.status}")
    if decision != "approve" and not (note and note.strip()):
        raise RuleViolation("a reject or correction needs a note for the team")
    actor, now = Actor.user(principal), deps.now()
    s = await deps.settings.get(session)
    version, quote, deal = await _context(session, approval.quote_version_id)
    status = {"approve": "approved", "reject": "rejected", "correct": "correction_requested"}[decision]
    approval.status, approval.decided_by, approval.decided_at, approval.decision_note = (
        status,
        actor.user_id,
        now,
        note,
    )
    session.add(
        ApprovalEvent(approval_id=approval.id, kind="decided", detail={"decision": decision, "note": note})
    )
    await session.flush()
    timeline = Timeline(
        "approval",
        f"{approval.kind.title()} approval: {status.replace('_', ' ')}" + (f" — {note}" if note else ""),
        deal.id,
        deal.primary_contact_id,
        ref_type="approval",
        ref_id=str(approval.id),
    )
    await record(
        session,
        actor,
        f"approval.{status}",
        "approval",
        approval.id,
        before={"status": "pending"},
        after={"status": status, "note": note},
        timeline=timeline,
    )
    match decision:
        case "approve":
            await issue_to_buyer(
                session,
                deps,
                s,
                deal=deal,
                quote=quote,
                version=version,
                approval=approval,
                kind=_PAYLOAD_KIND[approval.kind],
                actor=actor,
                now=now,
            )
        case "reject":
            version.status = "rejected"
            await record(
                session,
                actor,
                "quote.rejected",
                "quote_version",
                version.id,
                emit=Emit(
                    ev.QUOTE_REJECTED,
                    "quote",
                    quote.id,
                    QuoteRejected(
                        approval_ref=approval.id, quote_ref=quote.id, deal_ref=deal.id, reason=note or ""
                    ),
                ),
            )
        case _:
            version.status = "correction_requested"
            await record(
                session,
                actor,
                "quote.correction_requested",
                "quote_version",
                version.id,
                emit=Emit(
                    ev.QUOTE_CORRECTION_REQUESTED,
                    "quote",
                    quote.id,
                    QuoteCorrectionRequested(
                        approval_ref=approval.id, quote_ref=quote.id, deal_ref=deal.id, note=note or ""
                    ),
                ),
            )
    await session.flush()
    return approval


async def sweep_overdue(session: AsyncSession, deps: CrmDeps, now: datetime | None = None) -> int:
    """5-minute backstop: mark approvals past their window overdue and escalate to the MD.
    SKIP LOCKED lets two workers sweep at once without double alerts."""
    now = now or deps.now()
    stmt = (
        select(Approval)
        .where(Approval.status == "pending", Approval.deadline_at <= now)
        .order_by(Approval.deadline_at)
        .with_for_update(skip_locked=True)
        .limit(200)
    )
    approvals = list((await session.execute(stmt)).scalars())
    if not approvals:
        return 0
    md = staff.staff_contact(await staff.md_user(session))
    actor = Actor.system("approval_sweep")
    for a in approvals:
        version, _quote, deal = await _context(session, a.quote_version_id)
        a.status, a.escalated_to_md_at, a.escalation_step = "overdue", now, max(a.escalation_step, 4)
        if version.status == "pending_approval":
            version.status = "overdue"
        session.add(
            ApprovalEvent(approval_id=a.id, kind="overdue", detail={"deadline_at": a.deadline_at.isoformat()})
        )
        session.add(ApprovalEvent(approval_id=a.id, kind="escalated_to_md", detail={"by": "sweep"}))
        await record(
            session,
            actor,
            "approval.overdue",
            "approval",
            a.id,
            after={"deadline_at": a.deadline_at},
            timeline=Timeline(
                "approval",
                "Approval overdue — escalated to the MD",
                deal.id,
                deal.primary_contact_id,
                ref_type="approval",
                ref_id=str(a.id),
            ),
            emit=Emit(
                ev.APPROVAL_OVERDUE,
                "approval",
                a.id,
                ApprovalOverdue(
                    approval_ref=a.id, deal_ref=deal.id, overdue_since=a.deadline_at, escalate_to=md
                ),
            ),
        )
    await session.flush()
    return len(approvals)
