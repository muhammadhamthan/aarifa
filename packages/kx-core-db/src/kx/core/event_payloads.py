"""Typed payloads for the event catalogue. Producers build these and pass `.model_dump(mode="json")`
as `Event.payload`; consumers parse with `Model.model_validate(event.payload)`.

One model per event name. Add fields only with defaults; breaking changes get a new `.vN` event.
Payloads never carry cost, markup or floor (consumers include the AI).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from kx.core import events as ev
from kx.core.contracts import DealStage


class _P(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class StaffContact(_P):
    """A CRM user to alert (manager / MD). Staff numbers, never buyer data."""

    user_ref: uuid.UUID
    name: str
    phone: str | None
    email: str | None
    whatsapp: str | None = None


class DealQualified(_P):
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID


class DealStageChanged(_P):
    deal_ref: uuid.UUID
    from_stage: DealStage | None
    to_stage: DealStage
    reason: str = ""


class DealNextActionSet(_P):
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID
    due_at: datetime
    reason: Literal["followup_d2", "followup_d5", "followup_d10", "reorder", "custom"]
    buyer_timezone: str
    preferred_channel: Literal["whatsapp", "email"] = "whatsapp"


class NegotiationPaused(_P):
    """AI told the buyer "within 60 minutes" and stopped; manager + MD must act."""

    deal_ref: uuid.UUID
    reason: Literal["below_range", "concessions_exhausted", "exception", "manual"]
    alert: tuple[StaffContact, ...]
    respond_by: datetime


class QuoteSubmitted(_P):
    """An approval is open. Automation runs the alert ladder (3 IVR calls / 20 min, then MD)."""

    approval_ref: uuid.UUID
    quote_ref: uuid.UUID
    quote_version_ref: uuid.UUID
    deal_ref: uuid.UUID
    kind: Literal["commercial", "final"]
    required_role: Literal["manager", "md"]
    approver: StaffContact
    escalate_to: StaffContact | None
    due_at: datetime                        # computed with the 08:00-22:00 window (timer pauses)
    quote_number: str
    total_display: str                      # e.g. "AED 11,390.20" for the alert text


class QuoteApproved(_P):
    """Final approval done and a single-use send grant issued; automation sends the quote."""

    approval_ref: uuid.UUID
    quote_ref: uuid.UUID
    quote_version_ref: uuid.UUID
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID
    kind: Literal["commercial", "final"]
    grant_ref: uuid.UUID | None             # set only when kind == "final"
    channel: Literal["whatsapp", "email"] | None
    to_address: str | None                  # from the contact record, never from the AI
    pdf_s3_key: str | None
    body: str | None
    subject: str | None
    content_sha256: str | None              # sha256 over body + "\n" + pdf sha256 (see kx.crm.grants)
    buyer_timezone: str | None = None


class QuoteRejected(_P):
    approval_ref: uuid.UUID
    quote_ref: uuid.UUID
    deal_ref: uuid.UUID
    reason: str


class QuoteCorrectionRequested(_P):
    approval_ref: uuid.UUID
    quote_ref: uuid.UUID
    deal_ref: uuid.UUID
    note: str


class ApprovalOverdue(_P):
    approval_ref: uuid.UUID
    deal_ref: uuid.UUID
    overdue_since: datetime
    escalate_to: StaffContact


class MessageReceived(_P):
    message_ref: uuid.UUID                  # automation.inbound_messages.id
    thread_ref: uuid.UUID
    channel: Literal["whatsapp", "email", "phone"]
    from_address_hash: str                  # recipient_hash; raw address stays in automation
    contact_ref: uuid.UUID | None
    deal_ref: uuid.UUID | None
    text: str
    media_s3_keys: tuple[str, ...] = ()
    received_at: datetime
    provider_message_id: str


class MessageStatus(_P):
    message_ref: uuid.UUID                  # automation.outbound_messages.id
    status: Literal["queued", "sent", "delivered", "read", "failed", "bounced", "complained"]
    provider_message_id: str | None
    error: str | None = None
    at: datetime


class PriceProposed(_P):
    """Scraper found a new raw-material price; CRM stores it as a *proposed* material_rate."""

    observation_ref: uuid.UUID
    price_source_ref: uuid.UUID
    material_code: str
    price: Decimal
    currency: str
    unit: str
    observed_at: datetime
    source_url: str


class PriceStale(_P):
    material_code: str
    days_since_approval: int
    level: Literal["warn", "block"]


class OrderStatusChanged(_P):
    order_ref: uuid.UUID
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID
    status: str
    note: str = ""


class ContactAnonymised(_P):
    contact_ref: uuid.UUID
    recipient_hashes: tuple[str, ...]       # automation/agents delete or blank their copies


class SettingsChanged(_P):
    key: str
    changed_by: uuid.UUID | None


class LaneRiskAssessed(_P):
    lane_ref: uuid.UUID
    risk_score: int
    buffer_pct: Decimal
    buffer_days: int
    summary: str


class KbContentPublished(_P):
    collection: str
    doc_id: str
    locale: str
    url: str | None = None


class AgentRunCompleted(_P):
    run_ref: uuid.UUID
    graph: str
    deal_ref: uuid.UUID | None
    status: Literal["ok", "guard_blocked", "error", "handoff"]


PAYLOADS: dict[str, type[_P]] = {
    ev.DEAL_QUALIFIED: DealQualified, ev.DEAL_STAGE_CHANGED: DealStageChanged,
    ev.DEAL_NEXT_ACTION_SET: DealNextActionSet, ev.NEGOTIATION_PAUSED: NegotiationPaused,
    ev.QUOTE_SUBMITTED: QuoteSubmitted, ev.QUOTE_APPROVED: QuoteApproved, ev.QUOTE_REJECTED: QuoteRejected,
    ev.QUOTE_CORRECTION_REQUESTED: QuoteCorrectionRequested, ev.APPROVAL_OVERDUE: ApprovalOverdue,
    ev.MESSAGE_RECEIVED: MessageReceived, ev.MESSAGE_STATUS: MessageStatus, ev.PRICE_PROPOSED: PriceProposed,
    ev.PRICE_STALE: PriceStale, ev.ORDER_STATUS_CHANGED: OrderStatusChanged,
    ev.CONTACT_ANONYMISED: ContactAnonymised, ev.SETTINGS_CHANGED: SettingsChanged,
    ev.LANE_RISK_ASSESSED: LaneRiskAssessed, ev.KB_CONTENT_PUBLISHED: KbContentPublished,
    ev.AGENT_RUN_COMPLETED: AgentRunCompleted,
}


def make_event(event_type: str, aggregate_type: str, aggregate_id: object, payload: _P,
               correlation_id: uuid.UUID | None = None) -> ev.Event:
    """Build an Event, checking the payload model matches the event name."""
    expected = PAYLOADS[event_type]
    if not isinstance(payload, expected):
        raise TypeError(f"{event_type} needs {expected.__name__}, got {type(payload).__name__}")
    return ev.Event(event_type=event_type, aggregate_type=aggregate_type, aggregate_id=str(aggregate_id),
                    correlation_id=correlation_id, payload=payload.model_dump(mode="json"))


def parse_payload[T: _P](event: ev.Event, model: type[T]) -> T:
    return model.model_validate(event.payload)
