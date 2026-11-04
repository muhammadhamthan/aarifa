"""Deals, captured requirements, stage history, the per-deal timeline and staff tasks."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CHAR, BigInteger, Boolean, Date, Integer, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kx.crm.models.base import DB_DEFAULT, TOUCHED, TZ, Base, created_at, ref, updated_at, uuid_pk


class Deal(Base):
    """One sales opportunity. `version` is the optimistic lock: every UPDATE bumps it and a stale
    writer gets StaleDataError (mapped to 409 Conflict by kx.crm.services.deals)."""

    __tablename__ = "deals"

    id: Mapped[uuid.UUID] = uuid_pk()
    number: Mapped[str] = mapped_column(Text, unique=True, server_default=DB_DEFAULT)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ref("companies.id"))
    primary_contact_id: Mapped[uuid.UUID] = mapped_column(ref("contacts.id"))
    stage: Mapped[str] = mapped_column(Text, default="NEW")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_paused_reason: Mapped[str | None] = mapped_column(Text)
    ai_paused_at: Mapped[datetime | None] = mapped_column(TZ)
    source: Mapped[str] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(CHAR(3), default="AED")
    concessions_used: Mapped[int] = mapped_column(SmallInteger, default=0)
    lost_reason: Mapped[str | None] = mapped_column(Text)
    lost_note: Mapped[str | None] = mapped_column(Text)
    qualified_at: Mapped[datetime | None] = mapped_column(TZ)
    won_at: Mapped[datetime | None] = mapped_column(TZ)
    lost_at: Mapped[datetime | None] = mapped_column(TZ)
    next_action_at: Mapped[datetime | None] = mapped_column(TZ)
    next_action_kind: Mapped[str | None] = mapped_column(Text)
    next_action_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __mapper_args__ = {**TOUCHED, "version_id_col": version}  # noqa: RUF012 - SQLAlchemy mapper config


class DealRequirement(Base):
    __tablename__ = "deal_requirements"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    deal_id: Mapped[uuid.UUID] = mapped_column(ref("deals.id", ondelete="CASCADE"))
    product_id: Mapped[uuid.UUID | None] = mapped_column(ref("products.id"))
    product_text: Mapped[str | None] = mapped_column(Text)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    unit: Mapped[str] = mapped_column(Text, default="pcs")
    application: Mapped[str | None] = mapped_column(Text)
    delivery_country: Mapped[str | None] = mapped_column(CHAR(2))
    delivery_city: Mapped[str | None] = mapped_column(Text)
    delivery_port: Mapped[str | None] = mapped_column(Text)
    needed_by: Mapped[date | None] = mapped_column(Date)
    incoterm: Mapped[str | None] = mapped_column(Text)
    captured_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class StageHistory(Base):
    __tablename__ = "stage_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[uuid.UUID] = mapped_column(ref("deals.id", ondelete="CASCADE"))
    from_stage: Mapped[str | None] = mapped_column(Text)
    to_stage: Mapped[str] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ref("deals.id", ondelete="CASCADE"))
    contact_id: Mapped[uuid.UUID | None] = mapped_column(ref("contacts.id"))
    kind: Mapped[str] = mapped_column(Text)
    channel: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    ref_type: Mapped[str | None] = mapped_column(Text)
    ref_id: Mapped[str | None] = mapped_column(Text)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = uuid_pk()
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ref("deals.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    due_at: Mapped[datetime | None] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(Text, default="open")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = created_at()
    done_at: Mapped[datetime | None] = mapped_column(TZ)
    done_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
