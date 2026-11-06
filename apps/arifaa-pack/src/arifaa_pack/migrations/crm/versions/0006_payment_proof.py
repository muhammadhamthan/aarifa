"""v1 flow WP8: the advance payment record carries its proof file (§15.1).

Revision ID: crm_0006
Revises: crm_0005
Create Date: 2026-11-06

"The salesperson records the advance: amount, currency, method, reference, date, and a proof file."
Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0006"
down_revision = "crm_0005"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS proof_s3_key text;
CREATE INDEX IF NOT EXISTS tasks_lost_proposal_idx ON tasks (deal_id) WHERE kind = 'lost_proposal' AND status = 'open';
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
