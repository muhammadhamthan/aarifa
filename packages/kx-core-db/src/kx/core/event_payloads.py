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


class EventPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class StaffContact(EventPayload):
    """A CRM user to alert (manager / MD). Staff numbers, never buyer data."""

    user_ref: uuid.UUID
    name: str
    phone: str | None
    email: str | None
    whatsapp: str | None = None


class DealQualified(EventPayload):
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID


class DealStageChanged(EventPayload):
    deal_ref: uuid.UUID
    from_stage: DealStage | None
    to_stage: DealStage
    reason: str = ""


class DealNextActionSet(EventPayload):
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID
    due_at: datetime
    reason: Literal["followup_d2", "followup_d5", "followup_d10", "reorder", "custom"]
    buyer_timezone: str
    preferred_channel: Literal["whatsapp", "email"] = "whatsapp"


class NegotiationPaused(EventPayload):
    """AI told the buyer "within 60 minutes" and stopped; manager + MD must act."""

    deal_ref: uuid.UUID
    deal_number: str = ""  # spoken in the alert call ("deal D-2026-0042")
    reason: Literal["below_range", "concessions_exhausted", "exception", "manual"]
    alert: tuple[StaffContact, ...]  # §17: the salesperson, alerted at once (WhatsApp + call)
    respond_by: datetime
    notify: tuple[StaffContact, ...] = ()  # §17: the manager, notified (WhatsApp)


class QuoteSubmitted(EventPayload):
    """An approval is open (§11.3, §16). `approver` is the deal's salesperson (D1; the MD on a takeover,
    the manager for a special price); automation alerts them and runs the reminder ladder, escalating to
    `escalate_to` (the manager) at `due_at`."""

    approval_ref: uuid.UUID
    quote_ref: uuid.UUID
    quote_version_ref: uuid.UUID
    deal_ref: uuid.UUID
    kind: Literal["commercial", "final", "special_price"]
    required_role: Literal["sales", "manager", "md"]
    approver: StaffContact
    escalate_to: StaffContact | None
    due_at: datetime  # escalation time, counted in 08:00-22:00 alert hours (the timer pauses outside)
    quote_number: str
    total_display: str  # e.g. "AED 11,390.20" for the alert text
    # §16 reminders before the escalation (WhatsApp at 30 min, alert call at 60 min), in alert hours
    remind_at: tuple[datetime, ...] = ()


class QuoteApproved(EventPayload):
    """An approval step finished. When `grant_ref` is set a buyer-facing quote is ready and automation
    sends it: after approval 1 (manager, commercial quote) and after approval 2 (MD, final quote).
    No grant means an internal step only (nothing goes to the buyer)."""

    approval_ref: uuid.UUID
    quote_ref: uuid.UUID
    quote_version_ref: uuid.UUID
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID
    kind: Literal["commercial", "final"]
    grant_ref: uuid.UUID | None  # set whenever the quote goes to the buyer
    channel: Literal["whatsapp", "email"] | None
    to_address: str | None  # from the contact record, never from the AI
    pdf_s3_key: str | None
    body: str | None
    subject: str | None
    content_sha256: str | None  # kx.core.identity.priced_content_sha256(body, pdf_sha)
    buyer_timezone: str | None = None
    buyer_country: str | None = None  # ISO-2: the regional WhatsApp number (§11.4)
    language: str | None = None
    # final quote only (§14.5, §17): the salesperson is alerted and the manager notified
    salesperson: StaffContact | None = None
    manager: StaffContact | None = None


class QuoteRejected(EventPayload):
    approval_ref: uuid.UUID
    quote_ref: uuid.UUID
    deal_ref: uuid.UUID
    reason: str


class QuoteCorrectionRequested(EventPayload):
    approval_ref: uuid.UUID
    quote_ref: uuid.UUID
    deal_ref: uuid.UUID
    note: str


class ApprovalOverdue(EventPayload):
    approval_ref: uuid.UUID
    deal_ref: uuid.UUID
    overdue_since: datetime
    escalate_to: StaffContact


class ChangeRequested(EventPayload):
    """§13 / D12: the buyer asked for something the AI may not decide. The salesperson is alerted at once
    and reminded like an approval (§13.5, §16): `remind_at`, then escalation to `escalate_to` at
    `escalate_at`. `sla_due_at` is the promise made to the buyer ("within 4 working hours")."""

    deal_ref: uuid.UUID
    deal_number: str = ""
    change_request_ref: uuid.UUID
    type: Literal["product", "spec", "printing", "quantity", "delivery", "samples", "payment", "other"]
    assigned_to: StaffContact
    escalate_to: StaffContact | None = None
    sla_due_at: datetime
    remind_at: tuple[datetime, ...] = ()
    escalate_at: datetime | None = None
    alert_ref: uuid.UUID | None = None  # the reminder ladder's ref; a new one after a reassignment


class DealAssigned(EventPayload):
    """§9 / §17: a deal got (or changed) its salesperson, or the MD took it over / handed it back. Automation
    alerts `assigned_to` (WhatsApp + CRM) and notifies everyone in `notify`."""

    deal_ref: uuid.UUID
    deal_number: str = ""
    assigned_to: StaffContact
    previous: StaffContact | None = None
    notify: tuple[StaffContact, ...] = ()
    reason: Literal[
        "round_robin",
        "rule",
        "returning_customer",
        "backup",
        "manager",
        "reassigned",
        "takeover",
        "hand_back",
    ]


class ChangeRequestClosed(EventPayload):
    """The salesperson resolved the card (a new version was sent) or declined it with a reason the AI
    passes on politely (§13.4)."""

    deal_ref: uuid.UUID
    change_request_ref: uuid.UUID
    status: Literal["resolved", "declined"]
    decline_reason: str | None = None
    resolved_version_ref: uuid.UUID | None = None


class MessageReceived(EventPayload):
    message_ref: uuid.UUID  # automation.inbound_messages.id
    thread_ref: uuid.UUID
    channel: Literal["whatsapp", "email", "phone"]
    from_address_hash: str  # recipient_hash; raw address stays in automation
    contact_ref: uuid.UUID | None
    deal_ref: uuid.UUID | None
    text: str
    media_s3_keys: tuple[str, ...] = ()
    received_at: datetime
    provider_message_id: str
    opted_out: bool = False  # §8 / §17: STOP, unsubscribe, إيقاف ... (the address is already suppressed)
    # §8.1 step 1 (rules first): a stop word inside a sentence -> the AI reads the meaning (step 6)
    stop_in_sentence: bool = False
    stop_language: str | None = None  # language of the matched stop word (confirmation language)
    profile_name: str | None = None  # §8.2: the WhatsApp profile name shown when the buyer replies


ConsentScope = Literal["all", "whatsapp", "email", "phone"]
StopSource = Literal["rule", "ai", "unsubscribe_link", "salesperson", "no_answer"]


class OptedOut(EventPayload):
    """§8.1: a stop. `scope="all"` is the full opt-out (badge, LOST proposed); a channel scope stops one
    channel only ("use email instead" -> whatsapp, "don't call me" -> phone). Raised by automation (rules,
    the unsubscribe link, the 24-hour no-answer timer) or by the CRM (the AI's tool, a salesperson)."""

    contact_ref: uuid.UUID | None
    deal_ref: uuid.UUID | None
    thread_ref: uuid.UUID | None = None
    address_hashes: tuple[str, ...] = ()  # every address to suppress (the CRM adds the contact's others)
    scope: ConsentScope
    source: StopSource
    channel: str  # where the buyer said it (whatsapp, email, web_chat, web_voice, crm)
    exact_words: str | None = None  # permanent log (§8.1 step 9)
    reason: str | None = None  # only a reason the buyer volunteered (step 4)
    language: str | None = None
    decided_by: str  # the rule id, or the AI model + prompt version, or the user
    at: datetime
    log_ref: uuid.UUID | None = None  # automation.opt_out_log id when automation already applied it
    origin: Literal["automation", "crm"] = "automation"  # the producer already applied its own side


class StopUnclear(EventPayload):
    """§8.1 step 6: an unclear stop. Follow-ups pause at once; `live` = the AI asked "should we stop
    messaging you?" (no answer by `answer_by` = opt-out); otherwise the salesperson reviews."""

    contact_ref: uuid.UUID | None
    deal_ref: uuid.UUID
    thread_ref: uuid.UUID | None = None
    live: bool
    exact_words: str
    asked_at: datetime
    answer_by: datetime | None = None


class StaffNotice(EventPayload):
    """A one-line notice to named staff (WhatsApp in alert hours, per their alert channels; CRM always)."""

    deal_ref: uuid.UUID | None
    notify: tuple[StaffContact, ...]
    text: str
    key: str  # idempotency: one notice per key


class RequirementSummary(EventPayload):
    """§6.5 / §8.2: the one-page Requirement Summary (no prices) to send to the buyer's WhatsApp (an
    approved template with a document header), else email. Sending it is what confirms the number."""

    deal_ref: uuid.UUID
    contact_ref: uuid.UUID
    deal_number: str
    pdf_s3_key: str
    caption: str  # "Hi Ahmed, here's the summary of what we discussed ..." (§8.2 step 2)
    summary_text: str = ""  # the middle of the caption: the WhatsApp template's second parameter
    buyer_name: str | None = None
    whatsapp: str | None = None
    email: str | None = None
    language: str = "en"
    buyer_country: str | None = None


class ProductPublished(EventPayload):
    """§7 / D22: a CMS product was published (or unpublished). The CRM upserts it by product code; prices,
    tiers, MOQ for quoting and the default delivery time are never taken from the CMS."""

    code: str
    cms_id: str
    name: str
    category_code: str | None = None
    category_name: str | None = None
    spec: dict[str, object] = {}
    published: bool = True
    custom_printing: bool | None = None


class QuoteNotDelivered(EventPayload):
    """D32: a priced WhatsApp message was not delivered within the wait. Automation reports the exact content
    it sent (body, subject, PDF key, content hash) so the CRM can grant the same bytes on email."""

    message_ref: uuid.UUID
    grant_ref: uuid.UUID
    deal_ref: uuid.UUID | None
    contact_ref: uuid.UUID | None
    kind: Literal["quote", "final_quote", "counter_offer"]
    status: str
    body: str | None
    subject: str | None
    pdf_s3_key: str | None
    content_sha256: str


class QuoteResendRequested(EventPayload):
    """D32: the CRM granted the same approved quote on email; automation sends it (nothing else reacts)."""

    grant_ref: uuid.UUID
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID
    kind: Literal["quote", "final_quote", "counter_offer"]
    channel: Literal["email"]
    to_address: str
    body: str | None
    subject: str | None
    pdf_s3_key: str | None
    content_sha256: str
    buyer_timezone: str | None = None
    language: str | None = None


class ConversationSummaryUpdated(EventPayload):
    """§7A.2: the short AI summary shown at the top of the deal page."""

    deal_ref: uuid.UUID
    summary: str
    at: datetime


class MessageStatus(EventPayload):
    message_ref: uuid.UUID  # automation.outbound_messages.id
    status: Literal["queued", "sent", "delivered", "read", "failed", "bounced", "complained"]
    provider_message_id: str | None
    error: str | None = None
    at: datetime
    # §8.2: which buyer and channel the status is about (phone status is read from WhatsApp signals)
    contact_ref: uuid.UUID | None = None
    deal_ref: uuid.UUID | None = None
    channel: str | None = None
    kind: str | None = None


class PriceProposed(EventPayload):
    """Scraper found a new raw-material price; CRM stores it as a *proposed* material_rate."""

    observation_ref: uuid.UUID
    price_source_ref: uuid.UUID
    material_code: str
    price: Decimal
    currency: str
    unit: str
    observed_at: datetime
    source_url: str


class PriceStale(EventPayload):
    material_code: str
    days_since_approval: int
    level: Literal["warn", "block"]


class OrderStatusChanged(EventPayload):
    order_ref: uuid.UUID
    deal_ref: uuid.UUID
    contact_ref: uuid.UUID
    status: str
    note: str = ""
    # §15.3 / §17: the optional buyer WhatsApp update (client choice: notifications.order_status_to_buyer)
    notify_buyer: bool = False
    buyer_address: str | None = None
    order_number: str = ""
    language: str | None = None
    buyer_timezone: str | None = None


class DealWon(EventPayload):
    """§17 "Payment recorded / WON": manager and MD notified; the buyer gets a thank-you message."""

    deal_ref: uuid.UUID
    deal_number: str
    contact_ref: uuid.UUID
    notify: tuple[StaffContact, ...]
    salesperson_name: str | None = None
    buyer_channel: Literal["whatsapp", "email"] | None = None
    buyer_address: str | None = None
    language: str | None = None
    buyer_timezone: str | None = None


class ContactAnonymised(EventPayload):
    contact_ref: uuid.UUID
    recipient_hashes: tuple[str, ...]  # automation/agents delete or blank their copies


class SettingsChanged(EventPayload):
    key: str
    changed_by: uuid.UUID | None


class LaneRiskAssessed(EventPayload):
    lane_ref: uuid.UUID
    risk_score: int
    buffer_pct: Decimal
    buffer_days: int
    summary: str


class KbContentPublished(EventPayload):
    collection: str
    doc_id: str
    locale: str
    url: str | None = None


class AgentRunCompleted(EventPayload):
    run_ref: uuid.UUID
    graph: str
    deal_ref: uuid.UUID | None
    status: Literal["ok", "guard_blocked", "error", "handoff"]


PAYLOADS: dict[str, type[EventPayload]] = {
    ev.DEAL_QUALIFIED: DealQualified,
    ev.DEAL_STAGE_CHANGED: DealStageChanged,
    ev.DEAL_NEXT_ACTION_SET: DealNextActionSet,
    ev.NEGOTIATION_PAUSED: NegotiationPaused,
    ev.QUOTE_SUBMITTED: QuoteSubmitted,
    ev.QUOTE_APPROVED: QuoteApproved,
    ev.QUOTE_REJECTED: QuoteRejected,
    ev.QUOTE_CORRECTION_REQUESTED: QuoteCorrectionRequested,
    ev.APPROVAL_OVERDUE: ApprovalOverdue,
    ev.MESSAGE_RECEIVED: MessageReceived,
    ev.MESSAGE_STATUS: MessageStatus,
    ev.PRICE_PROPOSED: PriceProposed,
    ev.PRICE_STALE: PriceStale,
    ev.ORDER_STATUS_CHANGED: OrderStatusChanged,
    ev.CONTACT_ANONYMISED: ContactAnonymised,
    ev.SETTINGS_CHANGED: SettingsChanged,
    ev.LANE_RISK_ASSESSED: LaneRiskAssessed,
    ev.KB_CONTENT_PUBLISHED: KbContentPublished,
    ev.AGENT_RUN_COMPLETED: AgentRunCompleted,
    ev.CHANGE_REQUESTED: ChangeRequested,
    ev.CHANGE_REQUEST_CLOSED: ChangeRequestClosed,
    ev.DEAL_ASSIGNED: DealAssigned,
    ev.DEAL_WON: DealWon,
    ev.CONSENT_OPTED_OUT: OptedOut,
    ev.CONSENT_STOP_UNCLEAR: StopUnclear,
    ev.STAFF_NOTICE: StaffNotice,
    ev.DEAL_REQUIREMENT_SUMMARY: RequirementSummary,
    ev.CATALOGUE_PRODUCT_PUBLISHED: ProductPublished,
    ev.CONVERSATION_SUMMARY_UPDATED: ConversationSummaryUpdated,
    ev.QUOTE_NOT_DELIVERED: QuoteNotDelivered,
    ev.QUOTE_RESEND_REQUESTED: QuoteResendRequested,
}


def make_event(
    event_type: str,
    aggregate_type: str,
    aggregate_id: object,
    payload: EventPayload,
    correlation_id: uuid.UUID | None = None,
) -> ev.Event:
    """Build an Event, checking the payload model matches the event name."""
    expected = PAYLOADS[event_type]
    if not isinstance(payload, expected):
        raise TypeError(f"{event_type} needs {expected.__name__}, got {type(payload).__name__}")
    return ev.Event(
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        correlation_id=correlation_id,
        payload=payload.model_dump(mode="json"),
    )


def parse_payload[T: EventPayload](event: ev.Event, model: type[T]) -> T:
    return model.model_validate(event.payload)
