"""Change requests (docs/BUSINESS_FLOW_V1.md §13, D12): anything the buyer asks that the AI may not decide.

Types: product/variant, specification, printing/branding, quantity (below the MOQ or outside the tiers),
delivery, samples, payment (always a person, never the AI) and other.

1. The AI acknowledges without promising: "updated quotation within 4 working hours" (setting).
2. The card is created on the deal with the buyer's exact words, the table of what changes (field, value
   on the quote, value asked for) and the AI's notes; the salesperson is alerted at once (CHANGE_REQUESTED).
3. The salesperson revises the quote (a new version pre-filled from the card) and approves it.
4. Status: open -> in_progress -> resolved (the new version was approved and sent) or declined (the AI
   passes the reason on politely).
5. No action: the approval reminder ladder (§16), then escalation to the manager.

Timers count in alert hours 08:00-22:00 Asia/Dubai, the only staff hours the business flow defines.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.auth import Principal
from kx.core.contracts import ChangeField, ChangeType
from kx.core.errors import Conflict, NotFound, RuleViolation
from kx.core.event_payloads import ChangeRequestClosed, ChangeRequested
from kx.core.identity import new_id
from kx.crm.deps import CrmDeps
from kx.crm.models import ChangeRequest, Deal, Quote, QuoteLine, QuoteVersion
from kx.crm.services import access, staff
from kx.crm.services import deals as deal_svc
from kx.crm.services.issuing import lines_of
from kx.crm.services.journal import Actor, Emit, Timeline, record

OPEN: Final = ("open", "in_progress")
_ISSUED: Final = ("approved", "sent")
# card fields the CRM can fill from the quote ("current value on the quote", §13.2)
_ALIASES: Final = {"delivery": "delivery_time", "delivery_date": "delivery_time", "qty": "quantity"}


def _current_values(version: QuoteVersion | None, line: QuoteLine | None) -> dict[str, str | None]:
    if version is None:
        return {}
    return {
        "delivery_time": version.delivery_time_text,
        "incoterm": version.incoterm,
        "delivery_place": version.delivery_place,
        "payment_terms": version.payment_terms_text,
        "quantity": None if line is None else f"{line.quantity:,.0f} {line.unit}",
        "product": None if line is None else line.description,
        "unit_price": None if line is None else f"{version.currency} {line.unit_price}",
    }


def fill_current(
    fields: Sequence[ChangeField], version: QuoteVersion | None, line: QuoteLine | None
) -> list[dict[str, str | None]]:
    """The AI gives field + requested value; the CRM, which knows the quote, fills the current value."""
    known = _current_values(version, line)
    out = []
    for f in fields:
        key = _ALIASES.get(f.field.lower(), f.field.lower())
        out.append({"field": key, "current": f.current or known.get(key), "requested": f.requested})
    return out


async def _quote_version(session: AsyncSession, deal_id: uuid.UUID) -> QuoteVersion | None:
    """The version the buyer is looking at: the latest issued one, else the latest draft."""
    for statuses in (_ISSUED, None):
        stmt = (
            select(QuoteVersion)
            .join(Quote, Quote.id == QuoteVersion.quote_id)
            .where(Quote.deal_id == deal_id)
            .order_by(QuoteVersion.version_no.desc())
            .limit(1)
        )
        if statuses:
            stmt = stmt.where(QuoteVersion.status.in_(statuses))
        if (v := (await session.execute(stmt)).scalar_one_or_none()) is not None:
            return v
    return None


async def raise_change_request(
    session: AsyncSession,
    deps: CrmDeps,
    deal_id: uuid.UUID,
    *,
    type: ChangeType,
    buyer_text: str,
    fields: Sequence[ChangeField] = (),
    buyer_text_en: str | None = None,
    ai_notes: str | None = None,
    actor: Actor,
) -> ChangeRequest:
    """§13.2: create the card, alert the salesperson, start the §16 ladder (via CHANGE_REQUESTED)."""
    if not buyer_text.strip():
        raise RuleViolation("a change request needs the buyer's words")
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    s = await deps.settings.get(session)
    now = deps.now()
    version = await _quote_version(session, deal.id)
    line = (await lines_of(session, version.id))[0] if version is not None else None
    assignee = await staff.approver_for(session, deal)
    manager = await staff.manager_user(session)
    window = s.alert_window()
    cr_id = new_id()
    cr = ChangeRequest(
        id=cr_id,
        alert_ref=cr_id,
        deal_id=deal.id,
        quote_version_id=version.id if version else None,
        type=type,
        buyer_text=buyer_text[:4000],
        buyer_text_en=buyer_text_en,
        fields=fill_current(fields, version, line),
        ai_notes=ai_notes,
        status="open",
        assigned_to=assignee.id,
        sla_due_at=window.add_open_minutes(now, s.change_request_sla_working_hours * 60),
    )
    session.add(cr)
    await session.flush()
    await record(
        session,
        actor,
        "change_request.raised",
        "change_request",
        cr.id,
        after={"type": type, "assigned_to": assignee.id, "sla_due_at": cr.sla_due_at, "fields": cr.fields},
        timeline=Timeline(
            "change_request",
            f"Change request ({type}) for {assignee.full_name}: {buyer_text[:300]}",
            deal.id,
            deal.primary_contact_id,
            ref_type="change_request",
            ref_id=str(cr.id),
        ),
        emit=Emit(
            ev.CHANGE_REQUESTED,
            "change_request",
            cr.id,
            ChangeRequested(
                deal_ref=deal.id,
                deal_number=deal.number,
                change_request_ref=cr.id,
                type=type,
                assigned_to=staff.staff_contact(assignee),
                escalate_to=None if manager.id == assignee.id else staff.staff_contact(manager),
                sla_due_at=cr.sla_due_at,
                remind_at=tuple(window.add_open_minutes(now, m) for m in s.reminder_minutes[:-1]),
                escalate_at=window.add_open_minutes(now, s.escalate_after_minutes()),
                alert_ref=cr.alert_ref,
            ),
        ),
    )
    return cr


async def reroute(session: AsyncSession, deps: CrmDeps, deal: Deal, actor: Actor) -> int:
    """§9.4 reassignment / §3 takeover: open cards go to the person now responsible, with a fresh ladder
    (the previous person's reminders stop because their alert_ref is no longer current)."""
    s = await deps.settings.get(session)
    now = deps.now()
    window = s.alert_window()
    assignee = await staff.approver_for(session, deal)
    manager = await staff.manager_user(session)
    cards = await open_for_deal(session, deal.id)
    for cr in cards:
        if cr.assigned_to == assignee.id:
            continue
        cr.assigned_to, cr.alert_ref = assignee.id, new_id()
        await session.flush()
        await record(
            session,
            actor,
            "change_request.rerouted",
            "change_request",
            cr.id,
            after={"assigned_to": assignee.id},
            emit=Emit(
                ev.CHANGE_REQUESTED,
                "change_request",
                cr.id,
                ChangeRequested(
                    deal_ref=deal.id,
                    deal_number=deal.number,
                    change_request_ref=cr.id,
                    type=cr.type,
                    assigned_to=staff.staff_contact(assignee),
                    escalate_to=None if manager.id == assignee.id else staff.staff_contact(manager),
                    sla_due_at=cr.sla_due_at,
                    remind_at=tuple(window.add_open_minutes(now, m) for m in s.reminder_minutes[:-1]),
                    escalate_at=window.add_open_minutes(now, s.escalate_after_minutes()),
                    alert_ref=cr.alert_ref,
                ),
            ),
        )
    return len(cards)


async def _card(session: AsyncSession, cr_id: uuid.UUID, *, lock: bool = True) -> tuple[ChangeRequest, Deal]:
    cr = await session.get(ChangeRequest, cr_id, with_for_update=lock)
    if cr is None:
        raise NotFound(f"change request {cr_id} not found")
    return cr, await deal_svc.get_deal(session, cr.deal_id)


def _ensure_open(cr: ChangeRequest) -> None:
    if cr.status not in OPEN:
        raise Conflict(f"change request is already {cr.status}")


async def start(session: AsyncSession, deps: CrmDeps, cr_id: uuid.UUID, p: Principal) -> ChangeRequest:
    """Open -> In progress (the salesperson picked it up)."""
    cr, deal = await _card(session, cr_id)
    await access.check_edit(session, p, deal)
    _ensure_open(cr)
    if cr.status == "open":
        cr.status, cr.started_at = "in_progress", deps.now()
        await session.flush()
        await record(
            session,
            Actor.user(p),
            "change_request.started",
            "change_request",
            cr.id,
            before={"status": "open"},
            after={"status": "in_progress"},
        )
    return cr


async def link_revision(
    session: AsyncSession, deps: CrmDeps, cr_id: uuid.UUID, version_id: uuid.UUID, p: Principal
) -> ChangeRequest:
    """ "Revise quote" from the card: the new version answers this request once it is approved and sent."""
    cr, deal = await _card(session, cr_id)
    await access.check_edit(session, p, deal)
    _ensure_open(cr)
    version = await session.get(QuoteVersion, version_id)
    quote = await session.get(Quote, version.quote_id) if version else None
    if version is None or quote is None or quote.deal_id != deal.id:
        raise RuleViolation("the revised version must belong to the same deal")
    cr.resolved_version_id = version.id
    if cr.status == "open":
        cr.status, cr.started_at = "in_progress", deps.now()
    await session.flush()
    await record(
        session,
        Actor.user(p),
        "change_request.revision_drafted",
        "change_request",
        cr.id,
        after={"version_id": version.id},
        timeline=Timeline(
            "change_request",
            f"Revised quote v{version.version_no} drafted for the change request ({cr.type})",
            deal.id,
            deal.primary_contact_id,
            ref_type="change_request",
            ref_id=str(cr.id),
        ),
    )
    return cr


async def _close(
    session: AsyncSession,
    cr: ChangeRequest,
    deal: Deal,
    *,
    status: Literal["resolved", "declined"],
    actor: Actor,
    now: datetime,
) -> None:
    cr.status, cr.closed_at = status, now
    await session.flush()
    what = f"declined: {cr.decline_reason}" if status == "declined" else "resolved (new version sent)"
    await record(
        session,
        actor,
        f"change_request.{status}",
        "change_request",
        cr.id,
        after={"status": status, "decline_reason": cr.decline_reason, "version_id": cr.resolved_version_id},
        timeline=Timeline(
            "change_request",
            f"Change request ({cr.type}) {what}",
            deal.id,
            deal.primary_contact_id,
            ref_type="change_request",
            ref_id=str(cr.id),
        ),
        emit=Emit(
            ev.CHANGE_REQUEST_CLOSED,
            "change_request",
            cr.id,
            ChangeRequestClosed(
                deal_ref=deal.id,
                change_request_ref=cr.id,
                status=status,
                decline_reason=cr.decline_reason,
                resolved_version_ref=cr.resolved_version_id,
            ),
        ),
    )


async def resolve(
    session: AsyncSession, deps: CrmDeps, cr_id: uuid.UUID, version_id: uuid.UUID, p: Principal
) -> ChangeRequest:
    """Mark resolved with the version that answered it (that version must already be issued)."""
    cr, deal = await _card(session, cr_id)
    await access.check_edit(session, p, deal)
    _ensure_open(cr)
    version = await session.get(QuoteVersion, version_id)
    quote = await session.get(Quote, version.quote_id) if version else None
    if version is None or quote is None or quote.deal_id != deal.id or version.status not in _ISSUED:
        raise RuleViolation("resolve with an approved and sent version of this deal")
    cr.resolved_version_id = version.id
    await _close(session, cr, deal, status="resolved", actor=Actor.user(p), now=deps.now())
    return cr


async def resolve_for_version(
    session: AsyncSession, version: QuoteVersion, actor: Actor, now: datetime
) -> int:
    """Called when a version is approved: the cards it answers are resolved (§13.4 "new version sent")."""
    cards = list(
        (
            await session.execute(
                select(ChangeRequest)
                .where(ChangeRequest.resolved_version_id == version.id, ChangeRequest.status.in_(OPEN))
                .with_for_update()
            )
        ).scalars()
    )
    for cr in cards:
        await _close(
            session, cr, await deal_svc.get_deal(session, cr.deal_id), status="resolved", actor=actor, now=now
        )
    return len(cards)


async def decline(
    session: AsyncSession, deps: CrmDeps, cr_id: uuid.UUID, reason: str, p: Principal
) -> ChangeRequest:
    """Declined with the salesperson's reason; the AI tells the buyer politely (§13.4)."""
    if not reason.strip():
        raise RuleViolation("declining needs a reason the AI can pass on to the buyer")
    cr, deal = await _card(session, cr_id)
    await access.check_edit(session, p, deal)
    _ensure_open(cr)
    cr.decline_reason = reason.strip()[:1000]
    await _close(session, cr, deal, status="declined", actor=Actor.user(p), now=deps.now())
    return cr


async def sweep_escalations(session: AsyncSession, deps: CrmDeps, now: datetime | None = None) -> int:
    """§13.5 / §16: an open card nobody acted on for 120 alert-hour minutes is escalated to the manager
    (marked on the card and the timeline; automation's ladder already alerts the manager)."""
    now = now or deps.now()
    s = await deps.settings.get(session)
    window = s.alert_window()
    cards = list(
        (
            await session.execute(
                select(ChangeRequest)
                .where(ChangeRequest.status == "open", ChangeRequest.escalated_at.is_(None))
                .order_by(ChangeRequest.created_at)
                .with_for_update(skip_locked=True)
                .limit(200)
            )
        ).scalars()
    )
    done = 0
    for cr in cards:
        if window.add_open_minutes(cr.created_at, s.escalate_after_minutes()) > now:
            continue
        cr.escalated_at = now
        deal = await deal_svc.get_deal(session, cr.deal_id)
        await record(
            session,
            Actor.system("change_request_sweep"),
            "change_request.escalated",
            "change_request",
            cr.id,
            timeline=Timeline(
                "change_request",
                f"Change request ({cr.type}) not picked up — escalated to the manager",
                deal.id,
                deal.primary_contact_id,
                ref_type="change_request",
                ref_id=str(cr.id),
            ),
        )
        done += 1
    await session.flush()
    return done


async def open_for_deal(session: AsyncSession, deal_id: uuid.UUID) -> list[ChangeRequest]:
    stmt = (
        select(ChangeRequest)
        .where(ChangeRequest.deal_id == deal_id, ChangeRequest.status.in_(OPEN))
        .order_by(ChangeRequest.created_at)
    )
    return list((await session.execute(stmt)).scalars())
