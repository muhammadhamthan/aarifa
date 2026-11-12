"""Catalogue and cost inputs: products, cost recipes, material rates, FX, freight lanes, margin rules.

Everything here is MD/manager data. None of it may reach the AI (see kx.crm.gateway.DealContext).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CHAR, Boolean, Date, Integer, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kx.crm.models.base import (
    DB_DEFAULT,
    TOUCHED,
    TZ,
    Base,
    Money,
    Pct,
    UnitPrice,
    created_at,
    ref,
    updated_at,
    uuid_pk,
)


class ProductCategory(Base):
    __tablename__ = "product_categories"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    # D6 shortcut: minimum = list price less this %, unless the tier sets its own minimum
    min_discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))


class Material(Base):
    __tablename__ = "materials"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text, default="kg")
    currency: Mapped[str] = mapped_column(CHAR(3), default="AED")


class Product(Base):
    __tablename__ = "products"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    sku: Mapped[str] = mapped_column(Text, unique=True)
    cms_product_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    category_code: Mapped[str] = mapped_column(ref("product_categories.code"))
    unit: Mapped[str] = mapped_column(Text, default="pcs")
    moq: Mapped[int | None] = mapped_column(Integer)
    carton_qty: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # D10: default delivery time "X-Y working days after advance payment", editable on the quote
    default_lead_days_min: Mapped[int | None] = mapped_column(SmallInteger)
    default_lead_days_max: Mapped[int | None] = mapped_column(SmallInteger)
    # D8: printed variants are priced by the salesperson; not quotable = no list price, never AI-priced
    custom_printing: Mapped[bool] = mapped_column(Boolean, default=False)
    is_quotable: Mapped[bool] = mapped_column(Boolean, default=True)
    # §7 (D22): descriptive content mirrored from the CMS by product code; the CMS is its master
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    cms_published: Mapped[bool] = mapped_column(Boolean, default=False)
    cms_synced_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ProductPriceTier(Base):
    """D6 / §10: one row per product per quantity tier: the list price (the AI quotes it) and the
    minimum (the AI never goes below it). Rows sharing a `valid_from` form one price-list version and never
    overlap (DB exclusion constraint). Manager/admin data: never in AI or buyer payloads."""

    __tablename__ = "product_price_tiers"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(ref("products.id"))
    min_qty: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    max_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    list_price: Mapped[Decimal] = mapped_column(UnitPrice)
    min_price: Mapped[Decimal | None] = mapped_column(UnitPrice)
    currency: Mapped[str] = mapped_column(CHAR(3), default="AED")
    valid_from: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class ProductCosting(Base):
    __tablename__ = "product_costings"

    id: Mapped[uuid.UUID] = uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(ref("products.id"))
    material_code: Mapped[str] = mapped_column(ref("materials.code"))
    kg_per_1000: Mapped[Decimal] = mapped_column(Numeric(12, 4))
    yield_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    production_cost_per_1000: Mapped[Decimal] = mapped_column(Money)
    packaging_cost_per_1000: Mapped[Decimal] = mapped_column(Money)
    pcs_per_40hc: Mapped[int | None] = mapped_column(Integer)
    pieces_per_day: Mapped[int] = mapped_column(Integer)
    packing_days: Mapped[int] = mapped_column(SmallInteger, default=1)
    currency: Mapped[str] = mapped_column(CHAR(3), default="AED")
    effective_from: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    created_at: Mapped[datetime] = created_at()


class MaterialRate(Base):
    """A raw-material price. Only `approved` rows are used for quotes; one approved row per material."""

    __tablename__ = "material_rates"

    id: Mapped[uuid.UUID] = uuid_pk()
    material_code: Mapped[str] = mapped_column(ref("materials.code"))
    rate_per_unit: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(CHAR(3), default="AED")
    source: Mapped[str] = mapped_column(Text)  # manual | scrape | supplier_quote | erp
    source_ref: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    observation_ref: Mapped[uuid.UUID | None] = mapped_column()
    change_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 3))
    status: Mapped[str] = mapped_column(Text)  # proposed | approved | rejected | superseded
    proposed_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(TZ)
    effective_at: Mapped[datetime | None] = mapped_column(TZ)
    note: Mapped[str | None] = mapped_column(Text)


class PriceSource(Base):
    __tablename__ = "price_sources"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    material_code: Mapped[str | None] = mapped_column(ref("materials.code"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    terms_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(TZ)
    last_status: Mapped[str | None] = mapped_column(Text)


class SupplierLeadTime(Base):
    __tablename__ = "supplier_lead_times"

    id: Mapped[uuid.UUID] = uuid_pk()
    material_code: Mapped[str] = mapped_column(ref("materials.code"))
    supplier_name: Mapped[str] = mapped_column(Text)
    lead_time_days: Mapped[int] = mapped_column(SmallInteger)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class FxRate(Base):
    __tablename__ = "fx_rates"

    currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    rate_to_aed: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    as_of: Mapped[date] = mapped_column(Date, primary_key=True)
    source: Mapped[str] = mapped_column(Text)


class FreightLane(Base):
    __tablename__ = "freight_lanes"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    code: Mapped[str] = mapped_column(Text, unique=True)
    origin_port: Mapped[str] = mapped_column(Text)
    dest_country: Mapped[str] = mapped_column(CHAR(2))
    dest_port: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text)
    container_cost: Mapped[Decimal | None] = mapped_column(Money)
    surcharge: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    currency: Mapped[str] = mapped_column(CHAR(3), default="USD")
    transit_days: Mapped[int] = mapped_column(SmallInteger)
    risk_score: Mapped[int] = mapped_column(SmallInteger, default=0)
    buffer_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal(0))
    buffer_days: Mapped[int] = mapped_column(SmallInteger, default=0)
    rate_source: Mapped[str | None] = mapped_column(Text)
    rate_updated_at: Mapped[datetime | None] = mapped_column(TZ)
    risk_updated_at: Mapped[datetime | None] = mapped_column(TZ)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class LaneRiskHistory(Base):
    __tablename__ = "lane_risk_history"

    id: Mapped[uuid.UUID] = uuid_pk()
    lane_id: Mapped[uuid.UUID] = mapped_column(ref("freight_lanes.id"))
    score: Mapped[int] = mapped_column(SmallInteger)
    summary: Mapped[str] = mapped_column(Text)
    sources: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    assessment_ref: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    created_at: Mapped[datetime] = created_at()


class MarginRuleRow(Base):
    """Markup range on cost (T3) per category with optional customer-type / quantity overrides (T4)."""

    __tablename__ = "margin_rules"

    id: Mapped[uuid.UUID] = uuid_pk()
    category_code: Mapped[str] = mapped_column(ref("product_categories.code"))
    customer_type: Mapped[str | None] = mapped_column(Text)
    min_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    max_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    markup_min_pct: Mapped[Decimal] = mapped_column(Pct)
    markup_max_pct: Mapped[Decimal] = mapped_column(Pct)
    max_concessions: Mapped[int] = mapped_column(SmallInteger, default=3)
    priority: Mapped[int] = mapped_column(SmallInteger, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
