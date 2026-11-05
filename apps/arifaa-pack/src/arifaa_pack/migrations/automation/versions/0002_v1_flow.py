"""v1 flow (5 Nov): WhatsApp number per region, template params, bounce/complaint statuses, call ladder.

Revision ID: automation_0002
Revises: automation_0001
Create Date: 2026-11-05

Source: docs/V1_CHANGE_PLAN.md WP1 (automation) and docs/BUSINESS_FLOW_V1.md §11.4, §16, §19.
Idempotent by construction; forward-only like the baseline.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "automation_0002"
down_revision = "automation_0001"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = automation, public;

-- D11 / §11.4 / §19: one WhatsApp number per region, picked by the buyer's country
ALTER TABLE channel_accounts ADD COLUMN IF NOT EXISTS region text;
ALTER TABLE channel_accounts ADD COLUMN IF NOT EXISTS countries text[] NOT NULL DEFAULT ARRAY[]::text[];
ALTER TABLE channel_accounts ADD COLUMN IF NOT EXISTS is_default boolean NOT NULL DEFAULT false;
-- at most one default (fallback) account per channel and purpose
CREATE UNIQUE INDEX IF NOT EXISTS channel_accounts_default_uq ON channel_accounts (channel, purpose)
    WHERE is_default;

-- DDL v1.1: template parameters, contact and recipient hash on every message;
-- §19 email bounces and complaints stop email to that address
ALTER TABLE outbound_messages ADD COLUMN IF NOT EXISTS template_params jsonb;
ALTER TABLE outbound_messages ADD COLUMN IF NOT EXISTS contact_ref uuid;
ALTER TABLE outbound_messages ADD COLUMN IF NOT EXISTS recipient_hash char(64);
ALTER TABLE outbound_messages DROP CONSTRAINT IF EXISTS outbound_messages_status_check;
ALTER TABLE outbound_messages ADD CONSTRAINT outbound_messages_status_check CHECK (status IN
    ('queued','blocked','sending','sent','delivered','read','failed','cancelled','bounced','complained'));
CREATE INDEX IF NOT EXISTS outbound_contact_idx ON outbound_messages (contact_ref, created_at DESC)
    WHERE contact_ref IS NOT NULL;

-- §16 staff reminder ladder: one row per (approval or change request, attempt); change requests and
-- below-minimum alerts ring staff with their own scripts (§19: staff only, never buyers)
ALTER TABLE call_attempts DROP CONSTRAINT IF EXISTS call_attempts_purpose_check;
ALTER TABLE call_attempts ADD CONSTRAINT call_attempts_purpose_check CHECK (purpose IN
    ('approval_alert','below_range_alert','customer_callback','inbound','change_request_alert'));
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'call_attempts_approval_attempt_uq') THEN
    ALTER TABLE call_attempts ADD CONSTRAINT call_attempts_approval_attempt_uq UNIQUE (approval_ref, attempt_no);
  END IF;
END $$;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
