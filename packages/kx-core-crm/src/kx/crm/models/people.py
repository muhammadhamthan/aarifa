"""Staff users, MD-editable business settings and feature flags."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, CHAR, BigInteger, Boolean, Integer, Text
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


class StaffCredential(Base):
    """In-house login (D26): Argon2id password, encrypted TOTP seed, lock-out. One row per staff user."""

    __tablename__ = "staff_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(ref("users.id", ondelete="CASCADE"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text)
    password_changed_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(TZ)
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TZ)
    last_login_at: Mapped[datetime | None] = mapped_column(TZ)
    is_break_glass: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)


class AuthSession(Base):
    """A refresh-token session (rotated on every use; only its hash is stored)."""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ref("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    amr: Mapped[list[str]] = mapped_column(ARRAY(Text))
    mfa: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    last_used_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column()
    user_agent: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)


class AuthChallenge(Base):
    """A login step in progress (password ok, second step pending) or a password recovery."""

    __tablename__ = "auth_challenges"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ref("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True)
    code_hash: Mapped[str | None] = mapped_column(CHAR(64))
    tries: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=DB_DEFAULT)
    expires_at: Mapped[datetime] = mapped_column(TZ)
    consumed_at: Mapped[datetime | None] = mapped_column(TZ)
