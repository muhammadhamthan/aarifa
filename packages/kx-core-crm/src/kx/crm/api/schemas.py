"""Request/response models for the staff CRM API. Cost data has its own MD-only model (LineCostOut)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kx.core.contracts import DealStage
from kx.crm.rules import LostReason


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DealSummary(_Out):
    id: uuid.UUID
    number: str
    stage: DealStage
    ai_enabled: bool
    ai_paused_reason: str | None
    currency: str
    concessions_used: int
    next_action_at: datetime | None
    owner_user_id: uuid.UUID | None
    taken_over_by: uuid.UUID | None = None
    opted_out_at: datetime | None = None  # §8.1 step 8: the Opted out badge
    version: int
    updated_at: datetime


class RequirementOut(_Out):
    id: uuid.UUID
    product_id: uuid.UUID | None
    product_text: str | None
    spec: dict[str, Any]
    quantity: Decimal | None
    unit: str
    delivery_country: str | None
    delivery_city: str | None
    needed_by: date | None


class ContactOut(_Out):
    id: uuid.UUID
    full_name: str | None
    phone_e164: str | None
    whatsapp_e164: str | None
    email: str | None
    preferred_channel: str | None
    language: str
    timezone: str
    # §8.2: Verified | Likely | Reachable | Not reached (+ unknown before any signal), with its time
    phone_status: str = "unknown"
    phone_status_at: datetime | None = None
    whatsapp_profile_name: str | None = None
    company_id: uuid.UUID | None = None


class DealDetail(DealSummary):
    contact: ContactOut
    requirements: list[RequirementOut]
    missing_fields: list[str]
    lost_reason: str | None
    lost_note: str | None
    ai_summary: str | None = None  # §7A.2: shown at the top of the deal page
    ai_summary_at: datetime | None = None
    personal_contact_shared_at: datetime | None = None  # §14 practical notes


class StaffMessageIn(BaseModel):
    """§14: the salesperson writes to the buyer from the CRM inbox (same channel, same company number)."""

    text: str = Field(default="", max_length=4000)
    client_key: str = Field(min_length=8, max_length=80)  # the screen's id for this message: one send per tap
    channel: Literal["whatsapp", "email"] | None = None  # default: the buyer's channel on the contact record
    reopen_template: bool = False  # WhatsApp after 24 h of silence: the approved re-open template instead


class StaffMessageOut(BaseModel):
    message_id: uuid.UUID  # automation outbound message; its delivery status arrives on the timeline


class CallIn(BaseModel):
    client_key: str = Field(min_length=8, max_length=80)


class CallOut(BaseModel):
    call_id: uuid.UUID  # the call log row; the salesperson's phone rings first, then the buyer


class OptOutIn(BaseModel):
    """§8.1 step 4: a salesperson records a stop the buyer asked for in a conversation the buyer started
    (e.g. on a call). Add only: nobody removes a stop here (step 6)."""

    scope: Literal["all", "whatsapp", "email", "phone"] = "all"
    words: str = Field(min_length=1, max_length=2000, description="what the buyer said, as close as possible")
    reason: str | None = Field(default=None, max_length=2000, description="only a reason the buyer gave")


class ReopenIn(BaseModel):
    """§7A.1 / D24: the manager reopens a LOST deal (back to the stage it was lost from)."""

    reason: str = Field(min_length=1, max_length=500)


class StageMoveIn(BaseModel):
    to: DealStage
    expected_version: int
    reason: str | None = Field(default=None, max_length=500)
    lost_reason: LostReason | None = None
    lost_note: str | None = Field(default=None, max_length=2000)


class AiToggleIn(BaseModel):
    """Pause the AI (any reason) or resume it. A deal paused for `opt_out` cannot be resumed here: the AI
    may never remove a stop (§8.1 step 6); only a fresh, logged consent from the buyer lifts it."""

    enabled: bool
    reason: Literal["human_takeover", "md_takeover", "opt_out", "exception"] = "human_takeover"


class ManagerOfferIn(BaseModel):
    unit_price: Decimal = Field(gt=0, max_digits=14, decimal_places=6)
    resume_ai: bool = True


class PaymentIn(BaseModel):
    """§15.1 / D20: the salesperson confirms the advance was received. Every detail is optional; an amount
    needs its currency."""

    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    method: str | None = Field(default=None, min_length=1, max_length=100)
    reference: str | None = Field(default=None, max_length=200)
    paid_on: date | None = None
    note: str | None = Field(default=None, max_length=2000)
    proof_s3_key: str | None = Field(default=None, max_length=500)


class PaymentOut(_Out):
    id: uuid.UUID
    deal_id: uuid.UUID
    amount: Decimal | None
    currency: str | None
    method: str | None
    reference: str | None
    paid_on: date | None


class WonIn(BaseModel):
    expected_reorder_date: date | None = None  # D20: optional; when filled, v2 re-order follow-ups use it


class OrderStatusIn(BaseModel):
    status: Literal["ready", "shipped", "delivered", "cancelled"]
    on_date: date | None = None
    tracking_ref: str | None = None


class OrderOut(_Out):
    id: uuid.UUID
    number: str
    deal_id: uuid.UUID
    status: str
    final_quote_version_id: uuid.UUID
    expected_reorder_date: date | None = None


class TimelineOut(_Out):
    id: int
    kind: str
    channel: str | None
    direction: str | None
    summary: str
    actor_type: str
    actor_id: str | None
    at: datetime


class QuoteLineOut(_Out):
    line_no: int
    description: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal


class QuoteVersionOut(_Out):
    id: uuid.UUID
    quote_id: uuid.UUID
    version_no: int
    kind: str
    status: str
    currency: str
    subtotal: Decimal
    vat_rate: Decimal
    vat_amount: Decimal
    total: Decimal
    total_aed: Decimal
    incoterm: str
    validity_days: int
    valid_until: date | None
    clauses: list[str]
    approver_summary: str | None
    delivery_time_text: str | None = None
    delivery_place: str | None = None
    payment_terms_text: str | None = None
    fx_rate: Decimal | None = None
    fx_rate_date: date | None = None
    cover_note: str | None = None
    change_summary: str | None = None
    pdf_s3_key: str | None
    created_at: datetime
    lines: list[QuoteLineOut] = []


class LineCostOut(_Out):
    """MD only."""

    line_no: int
    material_rate: Decimal
    kg_per_1000: Decimal
    yield_pct: Decimal
    production_per_1000: Decimal
    packaging_per_1000: Decimal
    freight_per_1000: Decimal
    unit_cost: Decimal
    markup_pct: Decimal
    markup_min_pct: Decimal
    markup_max_pct: Decimal


class ApprovalOut(_Out):
    id: uuid.UUID
    quote_version_id: uuid.UUID
    kind: str
    required_role: str
    assigned_user_id: uuid.UUID | None
    approver_user_id: uuid.UUID | None = None
    status: str
    submitted_at: datetime
    deadline_at: datetime
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None


class LineEditIn(BaseModel):
    line_no: int = Field(ge=1)
    unit_price: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=6)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)


class QuoteEditsIn(BaseModel):
    """§11.3 "Edit then approve" / §13.3 "Revise quote": only the fields that change."""

    lines: list[LineEditIn] = []
    delivery_time_text: str | None = Field(default=None, max_length=300)
    delivery_place: str | None = Field(default=None, max_length=300)
    incoterm: Literal["EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DDP"] | None = None
    payment_terms_text: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=2000)
    buyer_note: str | None = Field(default=None, max_length=500)  # e.g. "Updated: delivery in 5 working days"
    cover_note: str | None = Field(default=None, max_length=500)


class ChangeRequestOut(_Out):
    """The §13.2 card: the buyer's words, what changes, the SLA timer."""

    id: uuid.UUID
    deal_id: uuid.UUID
    quote_version_id: uuid.UUID | None
    type: str
    buyer_text: str
    buyer_text_en: str | None
    fields: list[dict[str, Any]]
    ai_notes: str | None
    status: str
    assigned_to: uuid.UUID | None
    sla_due_at: datetime
    escalated_at: datetime | None
    resolved_version_id: uuid.UUID | None
    decline_reason: str | None
    created_at: datetime


class ManualLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    unit_price: Decimal = Field(gt=0, max_digits=14, decimal_places=6)
    unit: str = "pcs"
    product_id: uuid.UUID | None = None


class ManualQuoteIn(BaseModel):
    """D8: the salesperson prices a custom, printed or unpriced item by hand."""

    lines: list[ManualLineIn] = Field(min_length=1)
    delivery_time_text: str | None = Field(default=None, max_length=300)
    buyer_note: str | None = Field(default=None, max_length=500)


class ReassignIn(BaseModel):
    user_id: uuid.UUID


class AvailabilityIn(BaseModel):
    """§9.3 away / holiday and the backup who covers."""

    is_away: bool
    backup_user_id: uuid.UUID | None = None


class UserOut(_Out):
    id: uuid.UUID
    full_name: str
    role: str
    is_active: bool
    is_away: bool
    backup_user_id: uuid.UUID | None
    languages: list[str]
    countries: list[str]
    category_codes: list[str]
    alert_channels: list[str]


class ResolveIn(BaseModel):
    version_id: uuid.UUID


class DeclineIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject", "correct"]
    note: str | None = Field(default=None, max_length=2000)


class RateOut(_Out):
    id: uuid.UUID
    material_code: str
    rate_per_unit: Decimal
    currency: str
    source: str
    source_url: str | None
    change_pct: Decimal | None
    status: str
    proposed_at: datetime
    decided_at: datetime | None
    note: str | None


class RateDecisionIn(BaseModel):
    rate_per_unit: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    note: str | None = Field(default=None, max_length=1000)


class FreshnessOut(BaseModel):
    material_code: str
    days_since_approval: int | None
    level: Literal["ok", "warn", "block"]


class SettingOut(_Out):
    key: str
    value: Any
    description: str
    updated_at: datetime


class SettingIn(BaseModel):
    value: Any


class FlagIn(BaseModel):
    enabled: bool


class DashboardSummary(BaseModel):
    deals_by_stage: dict[str, int]
    open_pipeline_aed: Decimal
    approvals_pending: int
    approvals_overdue: int
    open_tasks: int
    ai_paused_deals: int
    materials: list[FreshnessOut]
