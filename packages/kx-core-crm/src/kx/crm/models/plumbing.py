"""Reporting projection, append-only audit log and messaging plumbing (outbox / inbox / idempotency).

The outbox and inbox are written by kx.core.events with plain SQL; they are mapped here so the
metadata covers all 45 tables and dashboards can read them with typed queries.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Date, Identity, Numeric, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kx.crm.models.base import DB_DEFAULT, TZ, Base


class RptDailyMetric(Base):
    __tablename__ = "rpt_daily_metrics"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    metric: Mapped[str] = mapped_column(Text, primary_key=True)
    dimension: Mapped[str] = mapped_column(Text, primary_key=True, default="")
    value: Mapped[Decimal] = mapped_column(Numeric(18, 4))


class AuditEvent(Base):
    """Append-only (a trigger refuses UPDATE/DELETE). Written through kx.crm.services.journal only."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    actor_type: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(Text)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    event_version: Mapped[int] = mapped_column(SmallInteger, default=1)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column()
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    aggregate_type: Mapped[str] = mapped_column(Text)
    aggregate_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    published_at: Mapped[datetime | None] = mapped_column(TZ)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)


class InboxEvent(Base):
    __tablename__ = "inbox_events"

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    handler: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str] = mapped_column(Text)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
