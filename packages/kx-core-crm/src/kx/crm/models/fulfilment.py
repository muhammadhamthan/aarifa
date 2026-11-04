"""After the deal: payments (advance always required), orders, stock, factory visits, reorders."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import CHAR, BigInteger, Boolean, Date, Integer, Numeric, SmallInteger, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from kx.crm.models.base import DB_DEFAULT, TOUCHED, TZ, Base, Money, created_at, ref, updated_at, uuid_pk

Qty3 = Numeric(16, 3, asdecimal=True)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(ref("deals.id"))
    confirmed_by: Mapped[uuid.UUID] = mapped_column(ref("users.id"))
    confirmed_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    method: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    paid_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(Text)


class Order(Base):
    __tablename__ = "orders"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[str] = mapped_column(Text, unique=True, server_default=DB_DEFAULT)
    deal_id: Mapped[uuid.UUID] = mapped_column(ref("deals.id"), unique=True)
    final_quote_version_id: Mapped[uuid.UUID] = mapped_column(ref("quote_versions.id"))
    delivery_estimate_id: Mapped[uuid.UUID | None] = mapped_column(ref("delivery_estimates.id"))
    status: Mapped[str] = mapped_column(Text, default="in_production")
    production_started_on: Mapped[date | None] = mapped_column(Date)
    ready_on: Mapped[date | None] = mapped_column(Date)
    shipped_on: Mapped[date | None] = mapped_column(Date)
    delivered_on: Mapped[date | None] = mapped_column(Date)
    tracking_ref: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="manual")
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ref("orders.id"))
    status: Mapped[str] = mapped_column(Text)
    on_date: Mapped[date] = mapped_column(Date)
    by_user: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    source: Mapped[str] = mapped_column(Text, default="manual")
    at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class StockItem(Base):
    __tablename__ = "stock_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    item_type: Mapped[str] = mapped_column(Text)  # raw | finished
    material_code: Mapped[str | None] = mapped_column(ref("materials.code"))
    product_id: Mapped[uuid.UUID | None] = mapped_column(ref("products.id"))
    quantity: Mapped[Decimal] = mapped_column(Qty3, default=Decimal(0))
    reserved_quantity: Mapped[Decimal] = mapped_column(Qty3, default=Decimal(0))
    unit: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="manual")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stock_item_id: Mapped[uuid.UUID] = mapped_column(ref("stock_items.id"))
    delta: Mapped[Decimal] = mapped_column(Qty3)
    kind: Mapped[str] = mapped_column(Text)  # adjust | reserve | release | consume | receive
    order_id: Mapped[uuid.UUID | None] = mapped_column(ref("orders.id"))
    by_user: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class VisitSlot(Base):
    __tablename__ = "visit_slots"

    id: Mapped[uuid.UUID] = uuid_pk()
    weekday: Mapped[int] = mapped_column(SmallInteger)
    starts_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)
    capacity: Mapped[int] = mapped_column(SmallInteger, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class FactoryVisit(Base):
    __tablename__ = "factory_visits"

    id: Mapped[uuid.UUID] = uuid_pk()
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ref("deals.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(ref("contacts.id"))
    slot_id: Mapped[uuid.UUID | None] = mapped_column(ref("visit_slots.id"))
    visit_at: Mapped[datetime] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(Text, default="requested")
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    created_at: Mapped[datetime] = created_at()


class ReorderOpportunity(Base):
    __tablename__ = "reorder_opportunities"

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ref("companies.id"))
    product_id: Mapped[uuid.UUID] = mapped_column(ref("products.id"))
    last_order_on: Mapped[date] = mapped_column(Date)
    avg_interval_days: Mapped[int] = mapped_column(Integer)
    due_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, default="open")
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ref("deals.id"))
    created_at: Mapped[datetime] = created_at()
