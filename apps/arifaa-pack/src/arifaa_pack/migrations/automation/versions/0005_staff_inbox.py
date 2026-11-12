"""§14 same-number handover (11 Nov plan WP17, D21): the salesperson's name on emails, click-to-call.

Revision ID: automation_0005
Revises: automation_0004
Create Date: 2026-11-12

Sources: the SRS §14 "Email: from the same company address, with the salesperson's name as sender" (the
From display name is stored per message so a delayed delivery still carries it) and "Calls: click-to-call
from the CRM through the company number. The call is logged on the deal; no personal phones" (a new
`click_to_call` call purpose). Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "automation_0005"
down_revision = "automation_0004"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = automation, public;

ALTER TABLE outbound_messages ADD COLUMN IF NOT EXISTS sender_name text;

ALTER TABLE call_attempts DROP CONSTRAINT IF EXISTS call_attempts_purpose_check;
ALTER TABLE call_attempts ADD CONSTRAINT call_attempts_purpose_check CHECK (purpose IN
    ('approval_alert','below_range_alert','customer_callback','inbound','change_request_alert',
     'click_to_call'));
-- a double-clicked "Call" button rings the salesperson once
ALTER TABLE call_attempts ADD COLUMN IF NOT EXISTS idempotency_key text;
CREATE UNIQUE INDEX IF NOT EXISTS call_attempts_idempotency_uq ON call_attempts (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
