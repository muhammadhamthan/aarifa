"""WP12: the §17 notifications matrix, row by row (the SRS §17).

Each row: the event the CRM emits -> what automation does (who gets which template on which channel, or which
reminder ladder starts). CRM-only columns ("CRM", "CRM timeline", "Dashboard") are covered by the CRM tests
(timeline/inbox); rows with no automation action assert that nothing is sent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from test_auto_db import MANAGER, SALES, Clock, RecordingSender, build

from kx.automation.alerts import PauseAlerts, VoiceAlerts
from kx.automation.settings import AutomationSettings
from kx.core import events as ev
from kx.core.contracts import OutboundKind
from kx.core.event_payloads import (
    ChangeRequested,
    DealAssigned,
    DealWon,
    EventPayload,
    NegotiationPaused,
    OrderStatusChanged,
    QuoteApproved,
    QuoteSubmitted,
    StaffContact,
    make_event,
)

pytestmark = pytest.mark.db
MD = StaffContact(
    user_ref=uuid.uuid4(), name="The MD", phone="+971503333333", email=None, whatsapp="+971503333333"
)
BUYER = "+966501234567"
DEAL = uuid.uuid4()


@dataclass(frozen=True)
class Row:
    """One line of §17: event, payload, then the expected (template or kind, address) messages and ladder."""

    name: str
    event_type: str
    payload: EventPayload
    messages: tuple[tuple[str, str], ...] = ()
    ladder: str | None = None  # "approval" / "change_request": the §16 reminders start for that person
    pause_scheduled: bool = False


def _approved(kind: str, **kw: Any) -> QuoteApproved:
    return QuoteApproved(
        approval_ref=uuid.uuid4(),
        quote_ref=uuid.uuid4(),
        quote_version_ref=uuid.uuid4(),
        deal_ref=DEAL,
        contact_ref=uuid.uuid4(),
        kind=kind,
        grant_ref=uuid.uuid4(),
        channel="whatsapp",
        to_address=BUYER,  # type: ignore[arg-type]
        pdf_s3_key="q.pdf",
        body="Quote",
        subject="Quotation Q-2026-00012 from Arifa Pack",
        content_sha256="a" * 64,
        **kw,
    )


def rows(clock: Clock) -> list[Row]:
    now = clock.now
    return [
        Row(  # Salesperson WhatsApp + CRM; Manager -; MD dashboard; Buyer: Requirement Summary (not built, see report)
            "new qualified deal assigned",
            ev.DEAL_ASSIGNED,
            DealAssigned(deal_ref=DEAL, deal_number="D-2026-00042", assigned_to=SALES, reason="round_robin"),
            messages=(("staff_notice", SALES.whatsapp or ""),),
        ),
        Row(  # Salesperson + reminders; Manager at the 2 h escalation
            "quote waiting for approval",
            ev.QUOTE_SUBMITTED,
            QuoteSubmitted(
                approval_ref=uuid.uuid4(),
                quote_ref=uuid.uuid4(),
                quote_version_ref=uuid.uuid4(),
                deal_ref=DEAL,
                kind="commercial",
                required_role="sales",
                approver=SALES,
                escalate_to=MANAGER,
                due_at=now + timedelta(minutes=120),
                quote_number="Q-2026-00012",
                total_display="AED 13,125.00",
                remind_at=(now + timedelta(minutes=30), now + timedelta(minutes=60)),
            ),
            ladder="approval",
        ),
        Row(  # Salesperson CRM; Buyer quote PDF + message
            "quote sent", ev.QUOTE_APPROVED, _approved("commercial"), messages=(("quote", BUYER),)
        ),
        Row(  # Salesperson immediate; Manager notified; Buyer "within 60 minutes" (the AI said it)
            "buyer pushes below the minimum",
            ev.NEGOTIATION_PAUSED,
            NegotiationPaused(
                deal_ref=DEAL,
                reason="below_range",
                alert=(SALES,),
                notify=(MANAGER,),
                respond_by=now + timedelta(minutes=60),
            ),
            pause_scheduled=True,
        ),
        Row(  # Salesperson immediate + reminders; Manager at 2 h; Buyer "within 4 working hours" (the AI said it)
            "change request",
            ev.CHANGE_REQUESTED,
            ChangeRequested(
                deal_ref=DEAL,
                deal_number="D-2026-00042",
                change_request_ref=uuid.uuid4(),
                type="delivery",
                assigned_to=SALES,
                escalate_to=MANAGER,
                sla_due_at=now + timedelta(hours=4),
                remind_at=(now + timedelta(minutes=30), now + timedelta(minutes=60)),
                escalate_at=now + timedelta(minutes=120),
            ),
            ladder="change_request",
        ),
        Row(  # Salesperson alert; Manager notified; Buyer final quote
            "final quote approved and sent",
            ev.QUOTE_APPROVED,
            _approved("final", salesperson=SALES, manager=MANAGER),
            messages=(
                ("final_quote", BUYER),
                ("final_quote_alert", SALES.whatsapp or ""),
                ("staff_notice", MANAGER.whatsapp or ""),
            ),
        ),
        Row(  # Salesperson CRM; Manager notified; MD notified; Buyer thank-you
            "payment recorded / WON",
            ev.DEAL_WON,
            DealWon(
                deal_ref=DEAL,
                deal_number="D-2026-00042",
                contact_ref=uuid.uuid4(),
                notify=(MANAGER, MD),
                salesperson_name="Sara",
                buyer_channel="whatsapp",
                buyer_address=BUYER,
            ),
            messages=(
                ("staff_notice", MANAGER.whatsapp or ""),
                ("staff_notice", MD.whatsapp or ""),
                ("thank_you", BUYER),
            ),
        ),
        Row(  # Salesperson CRM; Buyer optional WhatsApp: off (client choice not made)
            "order status changed (buyer updates off)",
            ev.ORDER_STATUS_CHANGED,
            OrderStatusChanged(
                order_ref=uuid.uuid4(), deal_ref=DEAL, contact_ref=uuid.uuid4(), status="shipped"
            ),
        ),
        Row(  # ... and on
            "order status changed (buyer updates on)",
            ev.ORDER_STATUS_CHANGED,
            OrderStatusChanged(
                order_ref=uuid.uuid4(),
                deal_ref=DEAL,
                contact_ref=uuid.uuid4(),
                status="shipped",
                notify_buyer=True,
                buyer_address=BUYER,
                order_number="O-2026-00007",
            ),
            messages=(("order_update", BUYER),),
        ),
        Row(  # Salesperson yes; Manager yes; MD - (it took the deal itself)
            "MD takes over",
            ev.DEAL_ASSIGNED,
            DealAssigned(
                deal_ref=DEAL, assigned_to=MD, previous=SALES, notify=(SALES, MANAGER), reason="takeover"
            ),
            messages=(("staff_notice", SALES.whatsapp or ""), ("staff_notice", MANAGER.whatsapp or "")),
        ),
        Row(  # Salesperson yes (both); Manager yes
            "deal reassigned",
            ev.DEAL_ASSIGNED,
            DealAssigned(
                deal_ref=DEAL,
                assigned_to=MD.model_copy(update={"name": "Ravi"}),
                previous=SALES,
                notify=(SALES, MANAGER),
                reason="reassigned",
            ),
            messages=(
                ("staff_notice", MD.whatsapp or ""),
                ("staff_notice", SALES.whatsapp or ""),
                ("staff_notice", MANAGER.whatsapp or ""),
            ),
        ),
    ]


@pytest.mark.parametrize("index", range(11))
async def test_section_17_row(index: int, auto_db: Any, clock: Clock) -> None:
    row = rows(clock)[index]
    sender = RecordingSender()
    handlers, _, scheduler, _ = build(auto_db, clock, sender)
    assert await handlers.dispatch(make_event(row.event_type, "x", uuid.uuid4(), row.payload)), row.name
    got = [(r.template_name or r.kind.value, r.to_address) for r in sender.requests]
    assert got == list(row.messages), row.name
    ladder = scheduler.named("ladder")
    assert (ladder[0]["ticket"].kind if ladder else None) == row.ladder, row.name
    if ladder:
        assert ladder[0]["ticket"].approver == SALES and ladder[0]["ticket"].escalate_to == MANAGER
    assert bool(scheduler.named("pause")) is row.pause_scheduled, row.name


async def test_below_minimum_pause_alerts_the_salesperson_and_notifies_the_manager(
    auto_db: Any, clock: Clock
) -> None:
    """§17 row 5 when the scheduled job runs: WhatsApp + call to the salesperson, WhatsApp only to the manager."""
    sender = RecordingSender()
    calls: list[tuple[str, str]] = []

    class Caller:
        async def place_call(self, to_e164: str, twiml: str, status_callback_url: str) -> str:
            calls.append((to_e164, twiml))
            return "CA1"

    settings = AutomationSettings()
    alerts = PauseAlerts(settings, VoiceAlerts(auto_db, settings, Caller(), clock), sender)
    await alerts.alert(
        NegotiationPaused(
            deal_ref=DEAL,
            reason="below_range",
            alert=(SALES,),
            notify=(MANAGER,),
            respond_by=clock.now + timedelta(minutes=60),
        )
    )
    assert sorted(r.to_address for r in sender.requests) == sorted([SALES.whatsapp, MANAGER.whatsapp])
    assert [c[0] for c in calls] == [SALES.phone]


async def test_opt_out_sends_one_confirmation(auto_db: Any, clock: Clock) -> None:
    """§8.1 step 3 / §17 last row: one plain confirmation, in the buyer's language, on the thread they wrote
    on, inside the 24-hour window; follow-ups cancelled; a link click or a channel-only stop gets none."""
    from sqlalchemy import text

    from kx.core.event_payloads import OptedOut
    from kx.core.settings import LogicalDb

    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        account = (
            await s.execute(text("SELECT id FROM channel_accounts WHERE external_account_id = 'PNID1'"))
        ).scalar_one()
        thread = (
            await s.execute(
                text(
                    "INSERT INTO threads (channel_account_id, external_key, last_inbound_at) "
                    "VALUES (:a, :k, :t) RETURNING id"
                ),
                {"a": account, "k": BUYER, "t": clock.now},
            )
        ).scalar_one()
    sender = RecordingSender()
    handlers, _, scheduler, _ = build(auto_db, clock, sender)
    stop = OptedOut(
        contact_ref=None,
        deal_ref=DEAL,
        thread_ref=thread,
        address_hashes=("h" * 64,),
        scope="all",
        source="rule",
        channel="whatsapp",
        exact_words="إيقاف",
        language="ar",
        decided_by="word:إيقاف",
        at=clock.now,
        log_ref=None,
    )
    assert await handlers.dispatch(make_event(ev.CONSENT_OPTED_OUT, "x", uuid.uuid4(), stop))
    ((kind, to, body),) = [(r.kind, r.to_address, r.body) for r in sender.requests]
    assert (kind, to) == (OutboundKind.OPT_OUT_CONFIRMATION, BUYER) and "إلغاء" in (body or "")
    assert ("cancel_followups", {"deal_ref": DEAL}) in scheduler.calls
    link = stop.model_copy(update={"source": "unsubscribe_link"})
    assert await handlers.dispatch(make_event(ev.CONSENT_OPTED_OUT, "x", uuid.uuid4(), link))
    channel_only = stop.model_copy(update={"scope": "phone"})
    assert await handlers.dispatch(make_event(ev.CONSENT_OPTED_OUT, "x", uuid.uuid4(), channel_only))
    assert len(sender.requests) == 1  # nothing else leaves
