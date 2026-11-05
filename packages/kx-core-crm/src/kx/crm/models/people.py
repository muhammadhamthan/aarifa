"""Staff users, MD-editable business settings and feature flags."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, BigInteger, Boolean, Text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kx.crm.models.base import DB_DEFAULT, TOUCHED, TZ, Base, created_at, ref, updated_at, uuid_pk


class User(Base):
    __tablename__ = "users"
    __mapper_args__ = TOUCHED

    id: Mapped[uuid.UUID] = uuid_pk()
    cognito_sub: Mapped[str | None] = mapped_column(Text, unique=True)
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    full_name: Mapped[str] = mapped_column(Text)
    phone_e164: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)  # md | manager | admin | sales
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # D4: preferred alert channels per user (CRM in-app notifications are always on, §16)
    alert_channels: Mapped[list[str]] = mapped_column(ARRAY(Text), default=lambda: ["whatsapp"])
    # D5 / §9 optional assignment rules: what this salesperson covers (empty = no rule match)
    languages: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    countries: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    category_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    is_away: Mapped[bool] = mapped_column(Boolean, default=False)
    backup_user_id: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    round_robin_seq: Mapped[int | None] = mapped_column(BigInteger)  # last assignment, crm.assignment_seq
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    description: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class SettingHistory(Base):
    __tablename__ = "settings_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(Text)
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any] = mapped_column(JSONB)
    changed_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    changed_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ref("users.id"))
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
