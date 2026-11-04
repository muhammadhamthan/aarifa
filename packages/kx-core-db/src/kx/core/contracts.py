"""Cross-package contracts. Packages never import each other: they depend on these types/protocols,
and the client app wires real implementations together (kx.crm implements CrmGateway & GrantConsumer,
kx.automation implements MessageSender).

Nothing here may expose cost, markup, margin range or floor: these DTOs reach the AI.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class DealStage(StrEnum):
    NEW = "NEW"
    QUALIFIED = "QUALIFIED"
    QUOTATION = "QUOTATION"
    NEGOTIATION = "NEGOTIATION"
    READY_TO_CLOSE = "READY_TO_CLOSE"
    PAYMENT = "PAYMENT"
    WON = "WON"
    LOST = "LOST"


class AiRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CONCIERGE = "factory_concierge"
    PRODUCT = "product_intelligence"
    MANUFACTURING = "manufacturing_intelligence"
    LEAD_QUALIFICATION = "lead_qualification"
    CONTACT_CAPTURE = "contact_capture"
    QUOTATION = "quotation"
    COMMERCIAL_APPROVAL = "commercial_approval"
    COMMUNICATION = "communication"
    FOLLOW_UP = "follow_up"
    DEAL_CLOSER = "deal_closer"
    REPEAT_SALES = "repeat_sales"


Channel = Literal["web_voice", "web_chat", "whatsapp", "email", "phone"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------- price guard types (implementation: kx.core.price_guard) -------------------------
class AllowedPrice(_Frozen):
    """An amount the AI may say for this deal: approved quote line, total, or an issued offer."""

    amount: Decimal
    currency: str
    label: str = ""


class PriceMention(_Frozen):
    amount: Decimal
    currency: str | None          # None when no currency could be determined
    start: int
    end: int
    raw: str
    is_percentage: bool = False


class GuardResult(_Frozen):
    ok: bool
    mentions: tuple[PriceMention, ...] = ()
    violations: tuple[PriceMention, ...] = ()


# ---------------- what the AI may see about a deal ------------------------------------------------
class ContactView(_Frozen):
    contact_id: uuid.UUID
    name: str | None
    company: str | None
    language: str
    timezone: str
    preferred_channel: str | None
    has_phone: bool
    has_email: bool
    phone_verified: bool


class RequirementView(_Frozen):
    requirement_id: uuid.UUID
    product_sku: str | None
    product_text: str | None
    spec: dict[str, str | int | float | bool | None]
    quantity: Decimal | None
    unit: str
    application: str | None
    delivery_country: str | None
    delivery_city: str | None
    needed_by: date | None


class QuoteLineView(_Frozen):
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal


class QuoteView(_Frozen):
    quote_number: str
    version_no: int
    status: str
    currency: str
    total: Decimal
    valid_until: date | None
    lines: tuple[QuoteLineView, ...]


class DealContext(_Frozen):
    deal_id: uuid.UUID
    number: str
    stage: DealStage
    ai_enabled: bool
    currency: str
    contact: ContactView
    requirements: tuple[RequirementView, ...]
    missing_fields: tuple[str, ...]
    latest_quote: QuoteView | None
    allowed_prices: tuple[AllowedPrice, ...]
    concessions_left: int
    estimated_delivery: date | None
    next_action_at: datetime | None


class RequirementPatch(BaseModel):
    product_sku: str | None = None
    product_text: str | None = None
    spec: dict[str, str | int | float | bool | None] | None = None
    quantity: Decimal | None = Field(default=None, gt=0)
    unit: str | None = None
    application: str | None = None
    delivery_country: str | None = Field(default=None, min_length=2, max_length=2)
    delivery_city: str | None = None
    needed_by: date | None = None


class ContactPatch(BaseModel):
    name: str | None = None
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    preferred_channel: Literal["whatsapp", "email", "phone"] | None = None
    language: str | None = None
    timezone: str | None = None


class QualifyResult(_Frozen):
    qualified: bool
    missing_fields: tuple[str, ...]


class CounterVerdictKind(StrEnum):
    ACCEPT = "accept"            # buyer's ask is at or above the current offer
    COUNTER = "counter"          # AI may offer `offer_unit_price`
    NEEDS_MANAGER = "needs_manager"  # below range, concessions exhausted, or exception


class CounterVerdict(_Frozen):
    kind: CounterVerdictKind
    offer_unit_price: Decimal | None
    currency: str
    concessions_left: int
    traded_for: Literal["quantity_increase", "goodwill"] | None = None
    reason: str = ""


class DeliveryEstimateView(_Frozen):
    estimated_ready: date
    estimated_delivery: date
    confirmed: bool


# ---------------- protocols implemented by packages, wired by the app -----------------------------
@runtime_checkable
class CrmGateway(Protocol):
    """What agents may ask of the CRM. Implemented by kx.crm."""

    async def resolve_deal(self, channel: Channel, address: str | None,
                           visitor_ref: uuid.UUID | None) -> uuid.UUID: ...
    async def deal_context(self, deal_id: uuid.UUID) -> DealContext: ...
    async def update_requirement(self, deal_id: uuid.UUID, patch: RequirementPatch,
                                 actor: AiRole) -> None: ...
    async def set_contact(self, deal_id: uuid.UUID, patch: ContactPatch, actor: AiRole) -> None: ...
    async def try_qualify(self, deal_id: uuid.UUID, actor: AiRole) -> QualifyResult: ...
    async def request_quote(self, deal_id: uuid.UUID, actor: AiRole) -> None: ...
    async def evaluate_counter(self, deal_id: uuid.UUID, ask_unit_price: Decimal,
                               ask_quantity: Decimal | None, channel: Channel) -> CounterVerdict: ...
    async def pause_ai(self, deal_id: uuid.UUID, reason: str, actor: AiRole) -> None: ...
    async def mark_agreed(self, deal_id: uuid.UUID, actor: AiRole) -> None: ...
    async def schedule_follow_up(self, deal_id: uuid.UUID, due_at: datetime, reason: str) -> None: ...
    async def delivery_estimate(self, deal_id: uuid.UUID) -> DeliveryEstimateView | None: ...
    async def add_timeline(self, deal_id: uuid.UUID, kind: str, summary: str, actor: AiRole) -> None: ...


@runtime_checkable
class GrantConsumer(Protocol):
    """Implemented by kx.crm; used by kx.automation before any priced message leaves."""

    async def consume_grant(self, grant_id: uuid.UUID, content_sha256: str, message_id: uuid.UUID,
                            recipient_hash: str) -> bool: ...


class OutboundKind(StrEnum):
    QUOTE = "quote"
    FINAL_QUOTE = "final_quote"
    COUNTER_OFFER = "counter_offer"
    REPLY = "reply"
    FOLLOWUP = "followup"
    SUMMARY_PDF = "summary_pdf"
    APPROVAL_ALERT = "approval_alert"
    MD_NOTICE = "md_notice"
    ORDER_UPDATE = "order_update"
    REORDER = "reorder"
    NOTIFICATION = "notification"


PRICED_KINDS = frozenset({OutboundKind.QUOTE, OutboundKind.FINAL_QUOTE, OutboundKind.COUNTER_OFFER})


class OutboundRequest(BaseModel):
    channel: Literal["whatsapp", "email"]
    to_address: str
    kind: OutboundKind
    body: str | None = None
    subject: str | None = None
    template_name: str | None = None
    template_params: tuple[str, ...] = ()
    media_s3_key: str | None = None
    content_sha256: str | None = None
    grant_id: uuid.UUID | None = None
    deal_ref: uuid.UUID | None = None
    contact_ref: uuid.UUID | None = None
    buyer_timezone: str | None = None
    requested_by: str
    idempotency_key: str


@runtime_checkable
class MessageSender(Protocol):
    """Implemented by kx.automation (OutboundService). The only way anything leaves the platform."""

    async def send(self, request: OutboundRequest) -> uuid.UUID: ...
