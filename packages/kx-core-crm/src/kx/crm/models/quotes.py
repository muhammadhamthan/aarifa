"""Quotes, frozen versions and lines, MD-only cost snapshots, approvals, send grants, negotiation.

DB triggers freeze a version's prices and lines once it leaves 'draft'; services therefore insert
lines while the version is draft and only then move it to 'pending_approval'.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ARRAY, CHAR, BigInteger, Date, Integer, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kx.crm.models.base import DB_DEFAULT, TZ, Base, Money, Pct, UnitPrice, created_at, ref, uuid_pk


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[uuid.UUID] = uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(ref("deals.id"))
    number: Mapped[str] = mapped_column(Text, unique=True, server_default=DB_DEFAULT)
    # deferred FK in the DDL (quote <-> current version cycle)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ref("quote_versions.id", use_alter=True, name="quotes_current_version_fk")
    )
    created_at: Mapped[datetime] = created_at()


class DeliveryEstimateRow(Base):
    """Rule-based delivery estimate; `inputs` is the full feature snapshot kept as T14 training data."""

    __tablename__ = "delivery_estimates"

    id: Mapped[uuid.UUID] = uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(ref("deals.id"))
    lane_id: Mapped[uuid.UUID | None] = mapped_column(ref("freight_lanes.id"))
    backlog_days: Mapped[int] = mapped_column(SmallInteger)
    production_days: Mapped[int] = mapped_column(SmallInteger)
    material_wait_days: Mapped[int] = mapped_column(SmallInteger)
    packing_days: Mapped[int] = mapped_column(SmallInteger)
    transit_days: Mapped[int] = mapped_column(SmallInteger)
    buffer_days: Mapped[int] = mapped_column(SmallInteger)
    estimated_ready: Mapped[date] = mapped_column(Date)
    estimated_delivery: Mapped[date] = mapped_column(Date)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    confirmed_date: Mapped[date | None] = mapped_column(Date)
    confirmed_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = created_at()


class QuoteVersion(Base):
    __tablename__ = "quote_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    quote_id: Mapped[uuid.UUID] = mapped_column(ref("quotes.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)  # initial | revision | counter | final
    status: Mapped[str] = mapped_column(Text, default="draft")
    currency: Mapped[str] = mapped_column(CHAR(3))
    fx_rate_to_aed: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(1))
    subtotal: Mapped[Decimal] = mapped_column(Money)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(0))
    vat_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    total: Mapped[Decimal] = mapped_column(Money)
    total_aed: Mapped[Decimal] = mapped_column(Money)
    incoterm: Mapped[str] = mapped_column(Text, default="EXW")
    freight_lane_id: Mapped[uuid.UUID | None] = mapped_column(ref("freight_lanes.id"))
    validity_days: Mapped[int] = mapped_column(SmallInteger)
    valid_until: Mapped[date | None] = mapped_column(Date)
    clauses: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    assumptions: Mapped[str | None] = mapped_column(Text)
    approver_summary: Mapped[str | None] = mapped_column(Text)
    delivery_estimate_id: Mapped[uuid.UUID | None] = mapped_column(ref("delivery_estimates.id"))
    created_by_type: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    correction_note: Mapped[str | None] = mapped_column(Text)
    pdf_s3_key: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    created_at: Mapped[datetime] = created_at()


class QuoteLine(Base):
    __tablename__ = "quote_lines"

    id: Mapped[uuid.UUID] = uuid_pk()
    quote_version_id: Mapped[uuid.UUID] = mapped_column(ref("quote_versions.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(SmallInteger)
    product_id: Mapped[uuid.UUID | None] = mapped_column(ref("products.id"))
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    unit: Mapped[str] = mapped_column(Text, default="pcs")
    unit_price: Mapped[Decimal] = mapped_column(UnitPrice)
    line_total: Mapped[Decimal] = mapped_column(Money)


class QuoteLineCost(Base):
    """MD-only cost snapshot per line: the exact inputs that priced it (never in buyer/AI payloads)."""

    __tablename__ = "quote_line_costs"

    quote_line_id: Mapped[uuid.UUID] = mapped_column(
        ref("quote_lines.id", ondelete="CASCADE"), primary_key=True
    )
    material_rate_id: Mapped[uuid.UUID | None] = mapped_column(ref("material_rates.id"))
    material_rate: Mapped[Decimal] = mapped_column(Money)
    kg_per_1000: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    yield_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    production_per_1000: Mapped[Decimal] = mapped_column(Money)
    packaging_per_1000: Mapped[Decimal] = mapped_column(Money)
    freight_per_1000: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    unit_cost: Mapped[Decimal] = mapped_column(UnitPrice)
    markup_pct: Mapped[Decimal] = mapped_column(Pct)
    margin_rule_id: Mapped[uuid.UUID | None] = mapped_column(ref("margin_rules.id"))
    markup_min_pct: Mapped[Decimal] = mapped_column(Pct)
    markup_max_pct: Mapped[Decimal] = mapped_column(Pct)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = uuid_pk()
    quote_version_id: Mapped[uuid.UUID] = mapped_column(ref("quote_versions.id"))
    kind: Mapped[str] = mapped_column(Text)  # initial | final | below_range
    required_role: Mapped[str] = mapped_column(Text)  # manager | md
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    status: Mapped[str] = mapped_column(Text, default="pending")
    window_minutes: Mapped[int] = mapped_column(Integer)
    submitted_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    deadline_at: Mapped[datetime] = mapped_column(TZ)
    escalation_step: Mapped[int] = mapped_column(SmallInteger, default=0)
    escalated_to_md_at: Mapped[datetime | None] = mapped_column(TZ)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    decision_note: Mapped[str | None] = mapped_column(Text)


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(ref("approvals.id"))
    kind: Mapped[str] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class QuoteSendGrant(Base):
    """Single-use permission to send one approved content (sha) to one recipient on one channel."""

    __tablename__ = "quote_send_grants"

    id: Mapped[uuid.UUID] = uuid_pk()
    quote_version_id: Mapped[uuid.UUID] = mapped_column(ref("quote_versions.id"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(ref("approvals.id"))
    policy: Mapped[str] = mapped_column(Text)  # manager | md | within_range
    content_sha256: Mapped[str] = mapped_column(CHAR(64))
    channel: Mapped[str] = mapped_column(Text)
    recipient_hash: Mapped[str] = mapped_column(CHAR(64))
    issued_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    consumed_at: Mapped[datetime | None] = mapped_column(TZ)
    consumed_by_message_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_reason: Mapped[str | None] = mapped_column(Text)


class NegotiationRound(Base):
    __tablename__ = "negotiation_rounds"

    id: Mapped[uuid.UUID] = uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(ref("deals.id"))
    quote_version_id: Mapped[uuid.UUID] = mapped_column(ref("quote_versions.id"))
    round_no: Mapped[int] = mapped_column(SmallInteger)
    channel: Mapped[str] = mapped_column(Text)
    buyer_ask_unit_price: Mapped[Decimal | None] = mapped_column(UnitPrice)
    buyer_ask_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    verdict: Mapped[str] = mapped_column(Text)
    offered_unit_price: Mapped[Decimal | None] = mapped_column(UnitPrice)
    concession_no: Mapped[int | None] = mapped_column(SmallInteger)
    traded_for: Mapped[str | None] = mapped_column(Text)
    ai_reasoning: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
