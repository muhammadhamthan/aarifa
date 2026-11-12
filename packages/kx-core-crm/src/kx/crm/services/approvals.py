"""Human approval of every buyer-facing quote (the SRS §3, §11.3, §14.4, §16; D1, D3).

* Approval 1 (first quote and every revised quote) and Approval 2 (the final quote) are both approved by
  the deal's **salesperson**; the **manager** may approve any deal; the **MD** only a deal it has taken
  over (D1). There is no second check. Who may decide is `kx.crm.services.access.ensure_can_approve`.
* A unit price below the tier minimum first needs a **special_price** approval by the manager (D3). Once
  approved it unlocks that price for that quote version only, and the salesperson's approval follows.
* No action: reminders at 30 and 60 minutes, escalation to the manager at 120 minutes, all counted in
  alert hours 08:00-22:00 Asia/Dubai (the timer pauses outside), so a quote submitted at 21:30 escalates
  at 09:30 next day (§16, §23 scenario 6).
* The 4 Nov MD value limit is off in v1; when `approval.md_value_limit_aed` is set, quotes above it go to
  the MD instead.
* Approve releases the version to the buyer with its own single-use send grant.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.auth import Principal, Role
from kx.core.errors import Conflict, NotFound, RuleViolation
from kx.core.event_payloads import ApprovalOverdue, QuoteCorrectionRequested, QuoteRejected, QuoteSubmitted
from kx.core.identity import new_id
from kx.core.money import display
from kx.crm.deps import CrmDeps
from kx.crm.models import Approval, ApprovalEvent, Deal, Quote, QuoteVersion
from kx.crm.services import access, change_requests, staff
from kx.crm.services import deals as deal_svc
from kx.crm.services.grants import GrantPolicy
from kx.crm.services.issuing import issue_to_buyer
from kx.crm.services.journal import Actor, Emit, Timeline, record
from kx.crm.services.settings import BusinessSettings

ApprovalKind = Literal["initial", "revision", "counter", "final", "special_price"]
RequiredRole = Literal["sales", "manager", "md"]
Decision = Literal["approve", "reject", "correct"]
OPEN = ("pending", "overdue")
_PAYLOAD_KIND: dict[str, Literal["commercial", "final", "special_price"]] = {
    "initial": "commercial",
    "revision": "commercial",
    "counter": "commercial",
    "final": "final",
    "special_price": "special_price",
}
_POLICY: dict[Role, GrantPolicy] = {Role.SALES: "sales", Role.MANAGER: "manager", Role.MD: "md"}


def initial_role(total_aed: object, s: BusinessSettings) -> RequiredRole:
    """D1: the salesperson. Only with `approval.md_value_limit_aed` set do larger quotes go to the MD."""
    limit = s.md_value_limit_aed
    return "md" if limit is not None and total_aed > limit else "sales"  # type: ignore[operator]


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
    role: RequiredRole,
    actor: Actor,
    now: datetime,
) -> Approval:
    """Open one approval step and emit QUOTE_SUBMITTED: automation alerts the approver now and runs the
    reminder ladder; `escalate_to` (the manager) is called in at the 120-minute mark (§16)."""
    manager = await staff.manager_user(session)
    if kind == "special_price":
        approver, escalate = manager, None  # the manager is the top of this step (D3)
    elif role == "md":
        approver, escalate = await staff.md_user(session), None
    else:
        approver, escalate = await staff.approver_for(session, deal), manager
    if escalate is not None and escalate.id == approver.id:
        escalate = None
    minutes = s.escalate_after_minutes()
    due = s.alert_window().add_open_minutes(now, minutes)
    approval = Approval(
        id=new_id(),
        quote_version_id=version.id,
        kind=kind,
        required_role="manager" if kind == "special_price" else role,
        assigned_user_id=approver.id,
        approver_user_id=deal.owner_user_id,
        window_minutes=minutes,
        submitted_at=now,
        deadline_at=due,
    )
    session.add(approval)
    await session.flush()
    total_display = f"{version.currency} {display(version.total, version.currency):,}"
    what = "special price" if kind == "special_price" else f"{kind} quote"
    await record(
        session,
        actor,
        f"approval.opened.{kind}",
        "approval",
        approval.id,
        after={"quote_version_id": version.id, "approver": approver.id, "deadline_at": due},
        timeline=Timeline(
            "approval",
            f"Quote {quote.number} v{version.version_no} awaits {approver.full_name}'s {what} approval "
            f"({total_display})",
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
                required_role=approval.required_role,
                approver=staff.staff_contact(approver),
                escalate_to=None if escalate is None else staff.staff_contact(escalate),
                due_at=due,
                quote_number=quote.number,
                total_display=total_display,
                # §16: WhatsApp reminder at 30, alert call at 60 alert-hour minutes; escalation at `due_at`
                remind_at=tuple(s.alert_window().add_open_minutes(now, m) for m in s.reminder_minutes[:-1]),
            ),
        ),
    )
    return approval


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
    version, quote, deal = await _context(session, approval.quote_version_id)
    access.ensure_can_approve(principal, deal, approval, covering=await staff.covering_user(session, deal))
    if approval.status not in OPEN:
        raise Conflict(f"approval is already {approval.status}")
    if decision != "approve" and not (note and note.strip()):
        raise RuleViolation("a reject or correction needs a note for the team")
    actor, now = Actor.user(principal), deps.now()
    s = await deps.settings.get(session)
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
    label = "Special price" if approval.kind == "special_price" else f"{approval.kind.title()} approval"
    timeline = Timeline(
        "approval",
        f"{label}: {status.replace('_', ' ')}" + (f" — {note}" if note else ""),
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
        case "approve" if approval.kind == "special_price":
            # D3: the price is unlocked for this version only; now the salesperson approves the quote
            await open_approval(
                session,
                deps,
                s,
                deal=deal,
                quote=quote,
                version=version,
                kind=version.kind,  # type: ignore[arg-type]
                role="sales",
                actor=actor,
                now=now,
            )
        case "approve":
            await issue_to_buyer(
                session,
                deps,
                s,
                deal=deal,
                quote=quote,
                version=version,
                approval=approval,
                kind="final" if approval.kind == "final" else "commercial",
                actor=actor,
                now=now,
                policy=_POLICY.get(principal.role, "sales"),
            )
            await change_requests.resolve_for_version(session, version, actor, now)  # §13.4
            if approval.kind == "final":  # D13: the AI steps back once the final quote is approved
                await deal_svc.hand_over(session, deal, actor, now)
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


async def reroute_open(session: AsyncSession, deps: CrmDeps, deal: Deal, actor: Actor) -> int:
    """§9.4 / §23 scenario 6 ("the manager ... reassigns the deal to Ravi"): after a reassignment, takeover
    or hand-back, each pending approval is cancelled and re-opened for the person now responsible, so the
    previous person's reminders stop and the new one is alerted. The manager's special-price step stays."""
    s = await deps.settings.get(session)
    now = deps.now()
    rows = (
        await session.execute(
            select(Approval, QuoteVersion, Quote)
            .join(QuoteVersion, QuoteVersion.id == Approval.quote_version_id)
            .join(Quote, Quote.id == QuoteVersion.quote_id)
            .where(Quote.deal_id == deal.id, Approval.status.in_(OPEN), Approval.kind != "special_price")
        )
    ).tuples()
    target = await staff.approver_for(session, deal)
    moved = 0
    for a, version, quote in list(rows):
        if a.assigned_user_id == target.id:
            continue
        a.status = "cancelled"
        await session.flush()
        await open_approval(
            session,
            deps,
            s,
            deal=deal,
            quote=quote,
            version=version,
            kind=a.kind,  # type: ignore[arg-type]
            role=a.required_role,  # type: ignore[arg-type]
            actor=actor,
            now=now,
        )
        moved += 1
    return moved


async def sweep_overdue(session: AsyncSession, deps: CrmDeps, now: datetime | None = None) -> int:
    """5-minute backstop for §16: approvals past their 120-minute (alert-hour) window are marked overdue
    and escalated to the manager, who may approve or reassign (§23 scenario 6). SKIP LOCKED lets two
    workers sweep at once without double alerts."""
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
    manager = staff.staff_contact(await staff.manager_user(session))
    actor = Actor.system("approval_sweep")
    for a in approvals:
        version, _quote, deal = await _context(session, a.quote_version_id)
        a.status, a.escalated_at, a.escalation_step = "overdue", now, max(a.escalation_step, 3)
        if version.status == "pending_approval":
            version.status = "overdue"
        session.add(
            ApprovalEvent(approval_id=a.id, kind="overdue", detail={"deadline_at": a.deadline_at.isoformat()})
        )
        session.add(ApprovalEvent(approval_id=a.id, kind="escalated_to_manager", detail={"by": "sweep"}))
        await record(
            session,
            actor,
            "approval.overdue",
            "approval",
            a.id,
            after={"deadline_at": a.deadline_at},
            timeline=Timeline(
                "approval",
                "Approval overdue — escalated to the manager",
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
                    approval_ref=a.id, deal_ref=deal.id, overdue_since=a.deadline_at, escalate_to=manager
                ),
            ),
        )
    await session.flush()
    return len(approvals)
