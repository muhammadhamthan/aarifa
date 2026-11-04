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


class DealDetail(DealSummary):
    contact: ContactOut
    requirements: list[RequirementOut]
    missing_fields: list[str]
    lost_reason: str | None
    lost_note: str | None


class StageMoveIn(BaseModel):
    to: DealStage
    expected_version: int
    reason: str | None = Field(default=None, max_length=500)
    lost_reason: LostReason | None = None
    lost_note: str | None = Field(default=None, max_length=2000)


class AiToggleIn(BaseModel):
    enabled: bool
    reason: Literal["human_takeover", "md_takeover", "opt_out", "exception"] = "human_takeover"


class ManagerOfferIn(BaseModel):
    unit_price: Decimal = Field(gt=0, max_digits=14, decimal_places=6)
    resume_ai: bool = True


class PaymentIn(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    method: str | None = None
    reference: str | None = None
    paid_on: date | None = None
    note: str | None = None


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
    status: str
    submitted_at: datetime
    deadline_at: datetime
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None


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
