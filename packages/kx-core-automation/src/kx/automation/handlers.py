"""Reactions to events from other packages (CRM). Each handler runs at most once per event
(`inbox_once` in the automation DB, same transaction); if it raises, the inbox row rolls back and
the event is retried, so every side effect below is idempotent (idempotency keys / queueing locks).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from kx.automation.alerts import ApprovalLadder, LadderTicket, notify_staff
from kx.automation.models import SCHEMA, ChannelAccount, Thread
from kx.automation.outbound import TRANSIENT_REASONS
from kx.automation.ports import Clock, TaskScheduler, utc_now
from kx.automation.repo import ALL_CHANNELS
from kx.automation.settings import AutomationSettings
from kx.core import events as ev
from kx.core.contracts import MessageSender, OutboundKind, OutboundRequest
from kx.core.db import Databases
from kx.core.errors import RuleViolation
from kx.core.event_payloads import (
    ChangeRequested,
    ContactAnonymised,
    DealAssigned,
    DealNextActionSet,
    DealWon,
    MessageReceived,
    NegotiationPaused,
    OrderStatusChanged,
    QuoteApproved,
    QuoteSubmitted,
    parse_payload,
)
from kx.core.events import Event, inbox_once
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)

Handler = Callable[[AsyncSession, Event], Awaitable[None]]

# SQL twin of kx.core.identity.recipient_hash for addresses we store normalised (E.164 / lower-case
# email). Kept next to its only use; tests/test_auto_db.py checks both agree.
_SQL_RECIPIENT_HASH: Final = (
    "encode(sha256(convert_to(CASE WHEN position('@' in {col}) > 0 THEN 'email:' "
    "ELSE 'phone:' END || {col}, 'UTF8')), 'hex')"
)


async def _send_or_skip(sender: MessageSender, request: OutboundRequest) -> None:
    """Permanent refusals are final (logged, CRM gets MESSAGE_STATUS); transient ones retry the event."""
    try:
        await sender.send(request)
    except RuleViolation as exc:
        if exc.extra.get("reason") in TRANSIENT_REASONS:
            raise
        log.warning("handler.send_refused", reason=exc.extra.get("reason"), key=request.idempotency_key)


class FollowUps:
    """Sends the day 2/5/10 (or reorder) reminder when its job fires. The address comes from the
    buyer's own thread (the contact record's channel), never from an agent."""

    def __init__(self, databases: Databases, settings: AutomationSettings, sender: MessageSender) -> None:
        self._db = databases
        self._settings = settings
        self._sender = sender

    async def send(self, action: DealNextActionSet) -> None:
        address = await self._address(action)
        if address is None:
            log.info("followup.no_thread", deal_ref=str(action.deal_ref), channel=action.preferred_channel)
            return
        kind = OutboundKind.REORDER if action.reason == "reorder" else OutboundKind.FOLLOWUP
        await _send_or_skip(
            self._sender,
            OutboundRequest(
                channel=action.preferred_channel,
                to_address=address,
                kind=kind,
                template_name=self._settings.followup.templates[action.reason],
                deal_ref=action.deal_ref,
                contact_ref=action.contact_ref,
                buyer_timezone=action.buyer_timezone,
                requested_by="system:followup",
                idempotency_key=f"followup:{action.deal_ref}:{action.reason}:{action.due_at:%Y%m%d}",
            ),
        )

    async def _address(self, action: DealNextActionSet) -> str | None:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            return (
                await s.execute(
                    select(Thread.external_key)
                    .join(ChannelAccount, ChannelAccount.id == Thread.channel_account_id)
                    .where(
                        Thread.contact_ref == action.contact_ref,
                        ChannelAccount.channel == action.preferred_channel,
                        ChannelAccount.purpose == "customer",
                        Thread.external_key.not_like("anon:%"),
                    )
                    .order_by(Thread.last_inbound_at.desc().nulls_last())
                    .limit(1)
                )
            ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class EventHandlers:
    databases: Databases
    settings: AutomationSettings
    sender: MessageSender
    ladder: ApprovalLadder
    scheduler: TaskScheduler
    clock: Clock = utc_now

    def registry(self) -> dict[str, tuple[str, Handler]]:
        """event type -> (stable handler name for the inbox, handler)."""
        return {
            ev.QUOTE_SUBMITTED: ("automation.approval_ladder", self._quote_submitted),
            ev.CHANGE_REQUESTED: ("automation.change_request_ladder", self._change_requested),
            ev.QUOTE_APPROVED: ("automation.send_approved_quote", self._quote_approved),
            ev.DEAL_NEXT_ACTION_SET: ("automation.schedule_followup", self._next_action_set),
            ev.NEGOTIATION_PAUSED: ("automation.negotiation_paused", self._negotiation_paused),
            ev.CONTACT_ANONYMISED: ("automation.anonymise_contact", self._contact_anonymised),
            ev.DEAL_ASSIGNED: ("automation.deal_assigned", self._deal_assigned),
            ev.DEAL_WON: ("automation.deal_won", self._deal_won),
            ev.ORDER_STATUS_CHANGED: ("automation.order_update", self._order_status_changed),
            ev.MESSAGE_RECEIVED: ("automation.opt_out_confirmation", self._message_received),
        }

    async def dispatch(self, event: Event) -> bool:
        """Run the handler once; False when there is none or it already ran for this event."""
        entry = self.registry().get(event.event_type)
        if entry is None:
            return False
        name, handler = entry
        async with self.databases.transaction(LogicalDb.AUTOMATION) as s:
            if not await inbox_once(s, SCHEMA, event.event_id, name):
                return False
            await handler(s, event)
        return True

    async def _quote_submitted(self, _: AsyncSession, event: Event) -> None:
        await self.ladder.start(LadderTicket.for_approval(parse_payload(event, QuoteSubmitted)))

    async def _change_requested(self, _: AsyncSession, event: Event) -> None:
        """§13.2 immediate alert to the salesperson, then the §16 ladder with the change-request script."""
        await self.ladder.start(LadderTicket.for_change(parse_payload(event, ChangeRequested)))

    async def _quote_approved(self, _: AsyncSession, event: Event) -> None:
        """Send the approved quote with its single-use grant. The CRM put the recipient, exact body
        and content hash in the event; automation adds nothing to the priced content."""
        p = parse_payload(event, QuoteApproved)
        if p.grant_ref is None or p.channel is None or p.to_address is None or p.content_sha256 is None:
            log.info("quote_approved.nothing_to_send", approval_ref=str(p.approval_ref), kind=p.kind)
            return
        await _send_or_skip(
            self.sender,
            OutboundRequest(
                channel=p.channel,
                to_address=p.to_address,
                kind=OutboundKind.FINAL_QUOTE if p.kind == "final" else OutboundKind.QUOTE,
                body=p.body,
                subject=p.subject,
                media_s3_key=p.pdf_s3_key,
                content_sha256=p.content_sha256,
                grant_id=p.grant_ref,
                deal_ref=p.deal_ref,
                contact_ref=p.contact_ref,
                buyer_timezone=p.buyer_timezone,
                buyer_country=p.buyer_country,
                language=p.language,
                requested_by="system:quote_approved",
                idempotency_key=f"quote-approved:{p.approval_ref}",
            ),
        )
        if p.kind == "final":  # §14.5 / §17: salesperson alerted, manager notified
            await self._final_quote_notices(event, p)

    async def _final_quote_notices(self, event: Event, p: QuoteApproved) -> None:
        at = self.settings.alert_window.next_open(self.clock())
        a = self.settings.alerts
        what = f"Final quote ({p.subject})" if p.subject else "The final quote"
        if p.salesperson is not None:
            await notify_staff(
                self.sender,
                (p.salesperson,),
                a.final_quote_template,
                (f"{what} approved and sent. The buyer is yours for payment and the order.",),
                key=f"final:{event.event_id}:alert",
                deal_ref=p.deal_ref,
                not_before=at,
            )
        if p.manager is not None and (p.salesperson is None or p.manager.user_ref != p.salesperson.user_ref):
            await notify_staff(
                self.sender,
                (p.manager,),
                a.notice_template,
                (f"{what} approved and sent.",),
                key=f"final:{event.event_id}:notice",
                deal_ref=p.deal_ref,
                not_before=at,
            )

    async def _next_action_set(self, _: AsyncSession, event: Event) -> None:
        action = parse_payload(event, DealNextActionSet)
        await self.scheduler.schedule_followup(action, max(action.due_at, self.clock()))

    async def _negotiation_paused(self, _: AsyncSession, event: Event) -> None:
        paused = parse_payload(event, NegotiationPaused)
        await self.scheduler.schedule_pause_alert(paused, self.settings.alert_window.next_open(self.clock()))

    async def _deal_assigned(self, _: AsyncSession, event: Event) -> None:
        """§17 "New qualified deal assigned" (the salesperson: WhatsApp + CRM) and "MD takes over or deal
        reassigned" (salesperson and manager), in alert hours."""
        p = parse_payload(event, DealAssigned)
        text = {
            "takeover": f"The MD took over deal {p.deal_number}",
            "hand_back": f"Deal {p.deal_number} was handed back to {p.assigned_to.name}",
            "reassigned": f"Deal {p.deal_number} was reassigned to {p.assigned_to.name}",
        }.get(p.reason, f"New deal {p.deal_number} assigned to you")
        # §17 "MD takes over": the salesperson and the manager are told; the MD did it, so not the MD
        to = p.notify if p.reason == "takeover" else (p.assigned_to, *p.notify)
        await notify_staff(
            self.sender,
            to,
            self.settings.alerts.notice_template,
            (text,),
            key=f"assigned:{event.event_id}",
            deal_ref=p.deal_ref,
            not_before=self.settings.alert_window.next_open(self.clock()),
        )

    async def _deal_won(self, _: AsyncSession, event: Event) -> None:
        """§17 "Payment recorded / WON": manager + MD notified, the buyer gets the thank-you message."""
        p = parse_payload(event, DealWon)
        await notify_staff(
            self.sender,
            p.notify,
            self.settings.alerts.notice_template,
            (
                f"Deal {p.deal_number} is WON"
                + (f" by {p.salesperson_name}" if p.salesperson_name else "")
                + ".",
            ),
            key=f"won:{event.event_id}",
            deal_ref=p.deal_ref,
            not_before=self.settings.alert_window.next_open(self.clock()),
        )
        if p.buyer_channel is not None and p.buyer_address is not None:
            await _send_or_skip(
                self.sender,
                OutboundRequest(
                    channel=p.buyer_channel,
                    to_address=p.buyer_address,
                    kind=OutboundKind.NOTIFICATION,
                    template_name=self.settings.alerts.thank_you_template,
                    template_params=(p.salesperson_name or "",),
                    deal_ref=p.deal_ref,
                    contact_ref=p.contact_ref,
                    buyer_timezone=p.buyer_timezone,
                    language=p.language,
                    requested_by="system:deal_won",
                    idempotency_key=f"won-thanks:{p.deal_ref}",
                ),
            )

    async def _order_status_changed(self, _: AsyncSession, event: Event) -> None:
        """§17 "Order status changed -> Buyer: optional WhatsApp" (the CRM sets `notify_buyer`)."""
        p = parse_payload(event, OrderStatusChanged)
        if not p.notify_buyer or p.buyer_address is None:
            return
        await _send_or_skip(
            self.sender,
            OutboundRequest(
                channel="whatsapp",
                to_address=p.buyer_address,
                kind=OutboundKind.ORDER_UPDATE,
                template_name=self.settings.alerts.order_update_template,
                template_params=(p.order_number, p.status.replace("_", " ")),
                deal_ref=p.deal_ref,
                contact_ref=p.contact_ref,
                buyer_timezone=p.buyer_timezone,
                language=p.language,
                requested_by="system:order_update",
                idempotency_key=f"order:{p.order_ref}:{p.status}",
            ),
        )

    async def _message_received(self, s: AsyncSession, event: Event) -> None:
        """§17 "Buyer opts out -> Confirmation": one confirmation on the thread the buyer wrote on."""
        p = parse_payload(event, MessageReceived)
        if not p.opted_out:
            return
        thread = await s.get(Thread, p.thread_ref)
        if thread is None:
            return
        await _send_or_skip(
            self.sender,
            OutboundRequest(
                channel="whatsapp",
                to_address=thread.external_key,
                kind=OutboundKind.NOTIFICATION,
                template_name=self.settings.alerts.opt_out_confirmation_template,
                deal_ref=p.deal_ref,
                contact_ref=p.contact_ref,
                requested_by="system:opt_out",
                idempotency_key=f"opt-out:{p.from_address_hash}",
            ),
        )

    async def _contact_anonymised(self, s: AsyncSession, event: Event) -> None:
        await anonymise(s, parse_payload(event, ContactAnonymised), self.clock())


async def anonymise(s: AsyncSession, p: ContactAnonymised, now: datetime) -> None:
    """Retention (T20): blank our copies of the contact's addresses and message content, and keep a
    suppression so nothing is sent to that address again. S3 objects are removed by lifecycle rules."""
    hashes = list(p.recipient_hashes)
    threads = list(
        (
            await s.execute(
                text(
                    "UPDATE threads SET external_key = 'anon:' || id::text "  # noqa: S608 - constant SQL fragment
                    f"WHERE contact_ref = :c OR {_SQL_RECIPIENT_HASH.format(col='external_key')} = ANY(:h) "
                    "RETURNING id"
                ),
                {"c": p.contact_ref, "h": hashes},
            )
        ).scalars()
    )
    await s.execute(
        text(
            "UPDATE inbound_messages SET text = NULL, media_s3_key = NULL, raw = '{}'::jsonb, "
            "processed_at = COALESCE(processed_at, :now) WHERE thread_id = ANY(:t)"
        ),
        {"t": threads, "now": now},
    )
    await s.execute(
        text(
            "UPDATE outbound_messages SET to_address = 'anonymised', body = NULL, subject = NULL "  # noqa: S608
            f"WHERE thread_id = ANY(:t) OR {_SQL_RECIPIENT_HASH.format(col='to_address')} = ANY(:h)"
        ),
        {"t": threads, "h": hashes},
    )
    await s.execute(
        text(
            "UPDATE call_attempts SET to_e164 = 'anonymised' "  # noqa: S608
            f"WHERE {_SQL_RECIPIENT_HASH.format(col='to_e164')} = ANY(:h)"
        ),
        {"h": hashes},
    )
    if hashes:
        await s.execute(
            text(
                "INSERT INTO suppressions (channel, address_hash, reason) "
                "SELECT :ch, h, 'anonymised' FROM unnest(CAST(:h AS text[])) AS h ON CONFLICT DO NOTHING"
            ),
            {"ch": ALL_CHANNELS, "h": hashes},
        )
