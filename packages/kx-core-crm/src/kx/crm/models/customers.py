"""Buyers: companies, contacts (one person = one phone/email) and their consent log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, Text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kx.crm.models.base import DB_DEFAULT, TOUCHED, TZ, Base, created_at, ref, updated_at, uuid_pk


class Company(Base):
    __tablename__ = "companies"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    country_code: Mapped[str | None] = mapped_column(CHAR(2))
    city: Mapped[str | None] = mapped_column(Text)
    customer_type: Mapped[str | None] = mapped_column(Text)
    vat_trn: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    anonymised_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Contact(Base):
    __tablename__ = "contacts"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    company_id: Mapped[uuid.UUID | None] = mapped_column(ref("companies.id"))
    full_name: Mapped[str | None] = mapped_column(Text)
    designation: Mapped[str | None] = mapped_column(Text)
    phone_e164: Mapped[str | None] = mapped_column(Text)
    whatsapp_e164: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(CITEXT)
    preferred_channel: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, default="en")
    timezone: Mapped[str] = mapped_column(Text, default="Asia/Dubai")
    phone_verified_at: Mapped[datetime | None] = mapped_column(TZ)
    source: Mapped[str] = mapped_column(Text)
    visitor_ref: Mapped[uuid.UUID | None] = mapped_column()
    anonymised_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = uuid_pk()
    contact_id: Mapped[uuid.UUID] = mapped_column(ref("contacts.id"))
    channel: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    wording: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    captured_via: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
