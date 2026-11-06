"""Staff alerts: the §16 reminder ladder (approvals and change requests) and the below-minimum alert.

Everything runs in alert hours 08:00-22:00 Asia/Dubai only; outside them a step waits for 08:00.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kx.automation.channels import ChannelError
from kx.automation.channels.voice_ivr import (
    CALL_STATUS,
    PROVIDER,
    TERMINAL_STATUSES,
    AlertScript,
    Digit,
    alert_twiml,
    approval_alert_script,
    change_request_script,
    escalation_script,
    negotiation_paused_script,
)
from kx.automation.models import CallAttempt, CallPurpose
from kx.automation.ports import ApprovalStatusReader, Clock, TaskScheduler, utc_now
from kx.automation.repo import advisory_lock
from kx.automation.settings import AutomationSettings
from kx.core.contracts import MessageSender, OutboundKind, OutboundRequest
from kx.core.db import Databases
from kx.core.errors import RuleViolation
from kx.core.event_payloads import ChangeRequested, NegotiationPaused, QuoteSubmitted, StaffContact
from kx.core.identity import normalise_phone
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)
_REQUESTED_BY: Final = "system:alerts"


class Caller(Protocol):
    async def place_call(self, to_e164: str, twiml: str, status_callback_url: str) -> str: ...


class VoiceAlerts:
    """Places scripted alert calls and keeps `call_attempts` current from Twilio callbacks."""

    def __init__(
        self, databases: Databases, settings: AutomationSettings, caller: Caller, clock: Clock = utc_now
    ) -> None:
        self._db = databases
        self._settings = settings
        self._caller = caller
        self._now = clock

    async def call(
        self,
        *,
        purpose: CallPurpose,
        to: str,
        script: AlertScript,
        attempt_no: int,
        approval_ref: uuid.UUID | None = None,
        deal_ref: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Place one call. Idempotent per (approval, attempt_no): a retried job does not ring twice."""
        to_e164 = normalise_phone(to)
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            if approval_ref is not None and await self._already_called(s, approval_ref, attempt_no, purpose):
                return None
            attempt = CallAttempt(
                purpose=purpose,
                approval_ref=approval_ref,
                deal_ref=deal_ref,
                to_e164=to_e164,
                provider=PROVIDER,
                attempt_no=attempt_no,
            )
            s.add(attempt)
            await s.flush()
            attempt_id = attempt.id
        base = self._settings.twilio.status_callback_base_url.rstrip("/")
        twiml = alert_twiml(script, f"{base}/webhooks/twilio/voice/{attempt_id}", self._settings.twilio)
        try:
            sid = await self._caller.place_call(
                to_e164, twiml, f"{base}/webhooks/twilio/status?attempt_id={attempt_id}"
            )
        except ChannelError as exc:
            log.warning("ivr.call_failed", attempt_id=str(attempt_id), error=exc.detail)
            await self._update(attempt_id, status="failed", ended_at=self._now())
            return attempt_id
        await self._update(attempt_id, provider_call_id=sid)
        return attempt_id

    async def _already_called(
        self, s: AsyncSession, approval_ref: uuid.UUID, attempt_no: int, purpose: CallPurpose
    ) -> bool:
        await advisory_lock(s, f"call:{approval_ref}:{attempt_no}")
        return (
            await s.execute(
                select(CallAttempt.id).where(
                    CallAttempt.approval_ref == approval_ref,
                    CallAttempt.attempt_no == attempt_no,
                    CallAttempt.purpose == purpose,
                )
            )
        ).first() is not None

    async def _update(self, attempt_id: uuid.UUID, **values: object) -> None:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            await s.execute(update(CallAttempt).where(CallAttempt.id == attempt_id).values(**values))

    async def record_digits(self, s: AsyncSession, attempt_id: uuid.UUID, digits: str) -> uuid.UUID | None:
        """Store the key pressed; returns the approval to escalate when the approver pressed 2."""
        approval_ref = (
            await s.execute(
                update(CallAttempt)
                .where(CallAttempt.id == attempt_id)
                .values(digits_pressed=digits, status="answered")
                .returning(CallAttempt.approval_ref)
            )
        ).scalar_one_or_none()
        return approval_ref if digits == Digit.ESCALATE.value else None

    async def record_status(
        self, s: AsyncSession, attempt_id: uuid.UUID, call_sid: str, twilio_status: str
    ) -> None:
        status = CALL_STATUS.get(twilio_status)
        if status is None:
            return
        now = self._now()
        values: dict[str, object] = {"status": status, "provider_call_id": call_sid}
        if status == "answered":
            values["started_at"] = now
        if status in TERMINAL_STATUSES:
            values["ended_at"] = now
        # A terminal or answered row never moves back to ringing/queued (callbacks can arrive out of order).
        settled = (
            ("answered", *TERMINAL_STATUSES) if status in {"queued", "ringing"} else tuple(TERMINAL_STATUSES)
        )
        await s.execute(
            update(CallAttempt)
            .where(CallAttempt.id == attempt_id, CallAttempt.status.not_in(settled))
            .values(**values)
        )


class LadderTicket(BaseModel):
    """One thing waiting for a person (§16 "Staff reminders"): a quote approval or a change request.

    Built from QuoteSubmitted / ChangeRequested; the CRM computed every time in alert hours from
    `approval.reminder_minutes`, so automation only follows the schedule."""

    model_config = ConfigDict(frozen=True)

    ref: uuid.UUID  # approval_ref or change_request_ref (the CRM status reader knows both)
    kind: Literal["approval", "change_request"]
    deal_ref: uuid.UUID
    approver: StaffContact
    escalate_to: StaffContact | None
    remind_at: tuple[datetime, ...]  # [WhatsApp reminder (30 min), alert call (60 min)]
    escalate_at: datetime  # 120 min: the manager (WhatsApp + call)
    title: str  # what the message and the call say, e.g. "Quote Q-2026-00012 for AED 13,125.00"

    @classmethod
    def for_approval(cls, p: QuoteSubmitted) -> LadderTicket:
        what = "A special price on quote" if p.kind == "special_price" else "Quote"
        return cls(
            ref=p.approval_ref,
            kind="approval",
            deal_ref=p.deal_ref,
            approver=p.approver,
            escalate_to=p.escalate_to,
            remind_at=p.remind_at,
            escalate_at=p.due_at,
            title=f"{what} {p.quote_number} for {p.total_display}",
        )

    @classmethod
    def for_change(cls, p: ChangeRequested) -> LadderTicket:
        return cls(
            ref=p.alert_ref or p.change_request_ref,
            kind="change_request",
            deal_ref=p.deal_ref,
            approver=p.assigned_to,
            escalate_to=p.escalate_to,
            remind_at=p.remind_at,
            escalate_at=p.escalate_at or p.sla_due_at,
            title=f"A buyer {p.type} change request on deal {p.deal_number}".replace("  ", " "),
        )

    @property
    def purpose(self) -> CallPurpose:
        return "approval_alert" if self.kind == "approval" else "change_request_alert"


# §16 ladder steps: 1 = at once, 2 = WhatsApp reminder, 3 = alert call, 4 = escalation to the manager
ALERT, REMIND, CALL, ESCALATE = 1, 2, 3, 4


class ApprovalLadder:
    """§16 staff reminders for approvals and change requests (§13.5), in alert hours only:

    0 min CRM notification + WhatsApp to the approver, 30 min WhatsApp reminder, 60 min alert call
    (scripted, not AI), 120 min escalation to the manager (WhatsApp + call). Every step first asks the CRM
    whether the item still waits; a decided one never rings anyone. Pressing 1 on the call stops the
    calls to the approver; pressing 2 escalates now. The escalation still happens if nothing was decided.
    Calls never approve anything: approval needs MFA in the CRM."""

    def __init__(
        self,
        *,
        databases: Databases,
        settings: AutomationSettings,
        approvals: ApprovalStatusReader,
        voice: VoiceAlerts,
        sender: MessageSender,
        scheduler: TaskScheduler,
        clock: Clock = utc_now,
    ) -> None:
        self._db = databases
        self._settings = settings
        self._approvals = approvals
        self._voice = voice
        self._sender = sender
        self._scheduler = scheduler
        self._now = clock

    async def start(self, ticket: LadderTicket) -> None:
        await self._scheduler.schedule_ladder_step(
            ticket, ALERT, self._settings.alert_window.next_open(self._now())
        )

    def _when(self, ticket: LadderTicket, step: int) -> datetime | None:
        if step in (REMIND, CALL):
            idx = step - REMIND
            return ticket.remind_at[idx] if idx < len(ticket.remind_at) else None
        return ticket.escalate_at if step == ESCALATE else None

    async def _next(self, ticket: LadderTicket, step: int) -> None:
        for nxt in range(step + 1, ESCALATE + 1):
            if (at := self._when(ticket, nxt)) is not None:
                await self._scheduler.schedule_ladder_step(ticket, nxt, max(at, self._now()))
                return

    async def step(self, ticket: LadderTicket, step: int) -> None:
        if not await self._approvals.is_pending(ticket.ref):
            return
        window, now = self._settings.alert_window, self._now()
        if not window.contains(now):
            await self._scheduler.schedule_ladder_step(ticket, step, window.next_open(now))
            return
        digits = await self._digits(ticket.ref)
        if Digit.ESCALATE.value in digits or step >= ESCALATE:
            await self._escalate(ticket)
            return
        acknowledged = Digit.ACKNOWLEDGE.value in digits
        a = self._settings.alerts
        match step:
            case 1:
                template = (
                    a.approval_request_template if ticket.kind == "approval" else a.change_request_template
                )
                await self._message(ticket, ticket.approver, template, (ticket.title,), "alert")
            case 2 if not acknowledged:
                await self._message(
                    ticket, ticket.approver, a.approval_reminder_template, (ticket.title,), "remind"
                )
            case 3 if not acknowledged and ticket.approver.phone:
                script = (
                    approval_alert_script(ticket.title)
                    if ticket.kind == "approval"
                    else change_request_script(ticket.title)
                )
                await self._voice.call(
                    purpose=ticket.purpose,
                    to=ticket.approver.phone,
                    attempt_no=CALL,
                    script=script,
                    approval_ref=ticket.ref,
                    deal_ref=ticket.deal_ref,
                )
            case _:
                pass
        await self._next(ticket, step)

    async def _message(
        self, ticket: LadderTicket, to: StaffContact, template: str, params: tuple[str, ...], tag: str
    ) -> None:
        await _notify(
            self._sender,
            to,
            template,
            params,
            key=f"ladder:{ticket.ref}:{tag}:{to.user_ref}",
            deal_ref=ticket.deal_ref,
        )

    async def _digits(self, ref: uuid.UUID) -> set[str]:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            rows = await s.execute(
                select(CallAttempt.digits_pressed).where(
                    CallAttempt.approval_ref == ref, CallAttempt.digits_pressed.is_not(None)
                )
            )
            return {d for d in rows.scalars() if d}

    async def _escalate(self, ticket: LadderTicket) -> None:
        manager = ticket.escalate_to
        if manager is None:  # the approver already is the top of the chain
            log.info("ladder.no_escalation_target", ref=str(ticket.ref))
            return
        params = (ticket.title, ticket.approver.name)
        await self._message(ticket, manager, self._settings.alerts.escalation_template, params, "escalate")
        if manager.phone:
            await self._voice.call(
                purpose=ticket.purpose,
                to=manager.phone,
                attempt_no=ESCALATE,
                script=escalation_script(*params),
                approval_ref=ticket.ref,
                deal_ref=ticket.deal_ref,
            )


class PauseAlerts:
    """Below-range pause (T6): the buyer was promised an answer within 60 minutes, so manager and MD
    are told at once (WhatsApp + call), inside alert hours."""

    def __init__(self, settings: AutomationSettings, voice: VoiceAlerts, sender: MessageSender) -> None:
        self._settings = settings
        self._voice = voice
        self._sender = sender

    async def alert(self, paused: NegotiationPaused) -> None:
        """§17 "Buyer pushes below the minimum": the salesperson is alerted at once (WhatsApp + call), the
        manager is notified (WhatsApp)."""
        respond_by = paused.respond_by.astimezone(ZoneInfo(self._settings.alerts.timezone)).strftime("%H:%M")
        await notify_staff(
            self._sender,
            paused.notify,
            self._settings.alerts.negotiation_paused_template,
            (respond_by,),
            key=f"paused:{paused.deal_ref}:{_stamp(paused.respond_by)}:notify",
            deal_ref=paused.deal_ref,
        )
        for contact in paused.alert:
            await _notify(
                self._sender,
                contact,
                self._settings.alerts.negotiation_paused_template,
                (respond_by,),
                key=f"paused:{paused.deal_ref}:{_stamp(paused.respond_by)}:{contact.user_ref}",
                deal_ref=paused.deal_ref,
            )
            if contact.phone:
                await self._voice.call(
                    purpose="below_range_alert",
                    to=contact.phone,
                    attempt_no=1,
                    script=negotiation_paused_script(respond_by),
                    deal_ref=paused.deal_ref,
                )


async def notify_staff(
    sender: MessageSender,
    contacts: tuple[StaffContact, ...],
    template: str,
    params: tuple[str, ...],
    *,
    key: str,
    deal_ref: uuid.UUID,
    not_before: datetime | None = None,
) -> None:
    """One WhatsApp notice to each staff contact (§17 "notified"), inside alert hours via `not_before`."""
    for contact in contacts:
        await _notify(
            sender,
            contact,
            template,
            params,
            key=f"{key}:{contact.user_ref}",
            deal_ref=deal_ref,
            not_before=not_before,
        )


async def _notify(
    sender: MessageSender,
    contact: StaffContact,
    template: str,
    params: tuple[str, ...],
    *,
    key: str,
    deal_ref: uuid.UUID,
    not_before: datetime | None = None,
) -> None:
    """WhatsApp to a staff member who chose WhatsApp (D4: `StaffContact.whatsapp` is set only then)."""
    to = contact.whatsapp
    if to is None:
        return
    try:
        await sender.send(
            OutboundRequest(
                channel="whatsapp",
                to_address=to,
                kind=OutboundKind.MD_NOTICE,
                template_name=template,
                template_params=params,
                deal_ref=deal_ref,
                not_before=not_before,
                requested_by=_REQUESTED_BY,
                idempotency_key=key,
            )
        )
    except RuleViolation as exc:  # the call still goes out; a refused WhatsApp must not stop it
        log.warning("alerts.whatsapp_refused", reason=exc.extra.get("reason"), user_ref=str(contact.user_ref))


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M")
