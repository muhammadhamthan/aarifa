"""Typed ORM for the `automation` database. Mirrors db/ddl/02_automation.sql column for column.

The DDL is the source of truth (CHECK constraints live there); these classes give typed access.
Server defaults are declared so ORM inserts and raw SQL behave the same way.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import (
    ARRAY,
    CHAR,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, MappedColumn, mapped_column

from kx.core.db import make_base
from kx.core.identity import new_id

SCHEMA: Final = "automation"

if TYPE_CHECKING:  # make_base returns a runtime class; give mypy a static one with the same shape

    class Base(DeclarativeBase): ...
else:
    Base = make_base(SCHEMA)

_NOW = text("now()")
_EMPTY_JSON = text("'{}'::jsonb")

OutboundStatus = Literal["queued", "blocked", "sending", "sent", "delivered", "read", "failed", "cancelled"]
CallStatus = Literal["queued", "ringing", "answered", "no_answer", "busy", "failed", "completed"]
CallPurpose = Literal["approval_alert", "below_range_alert", "customer_callback", "inbound"]
InboundKind = Literal[
    "text", "audio", "image", "document", "button", "interactive", "email", "call", "reaction", "other"
]
ScrapeStatus = Literal["running", "ok", "failed", "blocked"]


def _uuid_pk() -> MappedColumn[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, default=new_id, server_default=text("gen_random_uuid()")
    )


def _ts(*, nullable: bool = True, now: bool = False) -> MappedColumn[Any]:
    return mapped_column(DateTime(timezone=True), nullable=nullable, server_default=_NOW if now else None)


class ChannelAccount(Base):
    __tablename__ = "channel_accounts"
    __table_args__ = (UniqueConstraint("provider", "external_account_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    channel: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    external_account_id: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    secret_ref: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    created_at: Mapped[datetime] = _ts(nullable=False, now=True)


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = (UniqueConstraint("channel", "name", "language"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    channel: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, server_default=text("'en'"))
    category: Mapped[str] = mapped_column(Text)
    provider_status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    variables: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("ARRAY[]::text[]"))
    updated_at: Mapped[datetime] = _ts(nullable=False, now=True)


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (
        UniqueConstraint("channel_account_id", "external_key"),
        Index("threads_contact_idx", "contact_ref"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    channel_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channel_accounts.id"))
    external_key: Mapped[str] = mapped_column(Text)
    contact_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deal_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_inbound_at: Mapped[datetime | None] = _ts()
    human_owner_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _ts(nullable=False, now=True)


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (
        UniqueConstraint("provider", "provider_message_id"),
        Index("inbound_unprocessed_idx", "received_at", postgresql_where=text("processed_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("threads.id"))
    provider: Mapped[str] = mapped_column(Text)
    provider_message_id: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    media_s3_key: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = _ts(nullable=False, now=True)
    processed_at: Mapped[datetime | None] = _ts()


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        Index("outbound_status_idx", "status", "not_before"),
        Index("outbound_deal_idx", "deal_ref", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("threads.id"))
    channel_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channel_accounts.id"))
    to_address: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message_templates.id"))
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    media_s3_key: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    grant_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), unique=True)
    deal_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    requested_by: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    block_reason: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    not_before: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(nullable=False, now=True)
    sent_at: Mapped[datetime | None] = _ts()
    delivered_at: Mapped[datetime | None] = _ts()
    read_at: Mapped[datetime | None] = _ts()


class WebhookReceipt(Base):
    __tablename__ = "webhook_receipts"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    signature_ok: Mapped[bool] = mapped_column(Boolean)
    received_at: Mapped[datetime] = _ts(nullable=False, now=True)


class Suppression(Base):
    __tablename__ = "suppressions"

    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    address_hash: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = _ts(nullable=False, now=True)


class CallAttempt(Base):
    __tablename__ = "call_attempts"
    __table_args__ = (Index("call_attempts_approval_idx", "approval_ref", "attempt_no"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    purpose: Mapped[str] = mapped_column(Text)
    approval_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deal_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    to_e164: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    provider_call_id: Mapped[str | None] = mapped_column(Text, unique=True)
    attempt_no: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    digits_pressed: Mapped[str | None] = mapped_column(Text)
    recording_notice_played: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    recording_s3_key: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = _ts()
    ended_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(nullable=False, now=True)


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_code: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = _ts(nullable=False, now=True)
    finished_at: Mapped[datetime | None] = _ts()
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    items: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)


class PriceObservation(Base):
    __tablename__ = "price_observations"
    __table_args__ = (UniqueConstraint("source_code", "material_code", "observed_for"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scrape_runs.id"))
    source_code: Mapped[str] = mapped_column(Text)
    material_code: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    currency: Mapped[str] = mapped_column(CHAR(3))
    unit: Mapped[str] = mapped_column(Text)
    observed_for: Mapped[date] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=_EMPTY_JSON)
    created_at: Mapped[datetime] = _ts(nullable=False, now=True)


class OutboxEvent(Base):
    """Written through kx.core.events.Outbox; mapped here so the schema is complete and typed."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("outbox_unpublished_idx", "created_at", postgresql_where=text("published_at IS NULL")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_type: Mapped[str] = mapped_column(Text)
    event_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    aggregate_type: Mapped[str] = mapped_column(Text)
    aggregate_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts(nullable=False, now=True)
    published_at: Mapped[datetime | None] = _ts()
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))


class InboxEvent(Base):
    __tablename__ = "inbox_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    handler: Mapped[str] = mapped_column(Text, primary_key=True)
    processed_at: Mapped[datetime] = _ts(nullable=False, now=True)
