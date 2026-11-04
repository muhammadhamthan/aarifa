"""Staff alerts: the approval call ladder and below-range negotiation alerts.

Ladder (CONTRACTS §3, brief T1): when a quote is submitted the approver is called up to N times
(default 3) `gap` minutes apart, only between 08:00 and 22:00 Dubai (outside that the next call
waits for 08:00). It stops as soon as the approval is decided in the CRM or the approver presses 1.
After N calls without an acknowledgement, or at once if the approver presses 2, the MD gets a
WhatsApp template and a call. Calls never approve anything: approval needs MFA in the CRM.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from zoneinfo import ZoneInfo

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
from kx.core.event_payloads import NegotiationPaused, QuoteSubmitted, StaffContact
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
        return approval_ref if digits == Digit.SEND_TO_MD.value else None

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


class ApprovalLadder:
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

    async def start(self, submitted: QuoteSubmitted) -> None:
        await self._scheduler.schedule_ladder_step(
            submitted, 1, self._settings.alert_window.next_open(self._now())
        )

    async def step(self, submitted: QuoteSubmitted, attempt_no: int) -> None:
        ref = submitted.approval_ref
        if not await self._approvals.is_pending(ref):
            return
        digits = await self._digits(ref)
        if Digit.ACKNOWLEDGE.value in digits:
            return
        window, now = self._settings.alert_window, self._now()
        if not window.contains(now):
            await self._scheduler.schedule_ladder_step(submitted, attempt_no, window.next_open(now))
            return
        calls = self._settings.alerts.approval_calls
        if Digit.SEND_TO_MD.value in digits or attempt_no > calls:
            await self._escalate(submitted, calls + 1)
            return
        if submitted.approver.phone:
            await self._voice.call(
                purpose="approval_alert",
                to=submitted.approver.phone,
                attempt_no=attempt_no,
                script=approval_alert_script(submitted.quote_number, submitted.total_display),
                approval_ref=ref,
                deal_ref=submitted.deal_ref,
            )
        gap = timedelta(minutes=self._settings.alerts.gap_minutes)
        await self._scheduler.schedule_ladder_step(submitted, attempt_no + 1, window.next_open(now + gap))

    async def _digits(self, approval_ref: uuid.UUID) -> set[str]:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            rows = await s.execute(
                select(CallAttempt.digits_pressed).where(
                    CallAttempt.approval_ref == approval_ref, CallAttempt.digits_pressed.is_not(None)
                )
            )
            return {d for d in rows.scalars() if d}

    async def _escalate(self, submitted: QuoteSubmitted, attempt_no: int) -> None:
        md = submitted.escalate_to
        if md is None:  # the approver already is the MD
            log.info("ladder.no_escalation_target", approval_ref=str(submitted.approval_ref))
            return
        params = (submitted.quote_number, submitted.total_display, submitted.approver.name)
        await _notify(
            self._sender,
            md,
            self._settings.alerts.escalation_template,
            params,
            key=f"ladder:{submitted.approval_ref}:md",
            deal_ref=submitted.deal_ref,
        )
        if md.phone:
            await self._voice.call(
                purpose="approval_alert",
                to=md.phone,
                attempt_no=attempt_no,
                script=escalation_script(*params),
                approval_ref=submitted.approval_ref,
                deal_ref=submitted.deal_ref,
            )


class PauseAlerts:
    """Below-range pause (T6): the buyer was promised an answer within 60 minutes, so manager and MD
    are told at once (WhatsApp + call), inside alert hours."""

    def __init__(self, settings: AutomationSettings, voice: VoiceAlerts, sender: MessageSender) -> None:
        self._settings = settings
        self._voice = voice
        self._sender = sender

    async def alert(self, paused: NegotiationPaused) -> None:
        respond_by = paused.respond_by.astimezone(ZoneInfo(self._settings.alerts.timezone)).strftime("%H:%M")
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


async def _notify(
    sender: MessageSender,
    contact: StaffContact,
    template: str,
    params: tuple[str, ...],
    *,
    key: str,
    deal_ref: uuid.UUID,
) -> None:
    to = contact.whatsapp or contact.phone
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
                requested_by=_REQUESTED_BY,
                idempotency_key=key,
            )
        )
    except RuleViolation as exc:  # the call still goes out; a refused WhatsApp must not stop it
        log.warning("alerts.whatsapp_refused", reason=exc.extra.get("reason"), user_ref=str(contact.user_ref))


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y%m%dT%H%M")
