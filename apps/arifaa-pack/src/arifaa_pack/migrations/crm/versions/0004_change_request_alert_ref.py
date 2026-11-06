"""v1 flow WP5: change-request reminders follow the person now responsible.

Revision ID: crm_0004
Revises: crm_0003
Create Date: 2026-11-06

When a deal is reassigned or taken over (§9.4, §3, D5), the open change-request cards get a new
`alert_ref`: the reminder ladder of the previous person stops (its ref is no longer current) and a new
one starts for the new person (§13.5, §16). Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0004"
down_revision = "crm_0003"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;
ALTER TABLE change_requests ADD COLUMN IF NOT EXISTS alert_ref uuid;
UPDATE change_requests SET alert_ref = id WHERE alert_ref IS NULL;
ALTER TABLE change_requests ALTER COLUMN alert_ref SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS change_requests_alert_ref_uq ON change_requests (alert_ref);
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
