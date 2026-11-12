"""In-house staff login + MFA (11 Nov plan WP21, D26): credentials, sessions, one-time challenges.

Revision ID: crm_0010
Revises: crm_0009
Create Date: 2026-11-12

Source: platform docs (Recommendation "Staff login + MFA", Fallback plan "Staff login", Platform contest round 4:
"Argon2 hashing, rate limits, lockout, audit log, RS256 tokens"; TOTP first, email code only as fallback and
recovery; a break-glass admin account with no MFA bypass, every use alerted). Replaces Amazon Cognito
(`users.cognito_sub` is kept, unused, for history). Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0010"
down_revision = "crm_0009"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;

CREATE TABLE IF NOT EXISTS staff_credentials (
    user_id              uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    password_hash        text NOT NULL,                  -- Argon2id, PHC string
    password_changed_at  timestamptz NOT NULL DEFAULT now(),
    must_change_password boolean NOT NULL DEFAULT true,  -- a temporary password from an admin
    totp_secret_enc      text,                           -- Fernet-encrypted base32 seed
    totp_enabled_at      timestamptz,
    totp_last_step       bigint,                         -- replay protection
    failed_attempts      integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until         timestamptz,
    last_login_at        timestamptz,
    is_break_glass       boolean NOT NULL DEFAULT false, -- every use alerts the MD and the manager
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    id            uuid PRIMARY KEY,
    user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash    char(64) NOT NULL UNIQUE,              -- sha256 of the refresh token (never stored raw)
    amr           text[] NOT NULL,                       -- e.g. {pwd,otp} or {pwd,email}
    mfa           boolean NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_used_at  timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz,
    replaced_by   uuid,
    user_agent    text,
    ip            text
);
CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions (user_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS auth_challenges (
    id           uuid PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose      text NOT NULL CHECK (purpose IN ('login','reset')),
    token_hash   char(64) NOT NULL UNIQUE,               -- sha256 of the challenge token
    code_hash    char(64),                               -- sha256 of the email code, once sent
    tries        integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    consumed_at  timestamptz
);
CREATE INDEX IF NOT EXISTS auth_challenges_user_idx ON auth_challenges (user_id, created_at DESC);
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
