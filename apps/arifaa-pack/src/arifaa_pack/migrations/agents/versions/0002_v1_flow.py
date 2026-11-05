"""v1 flow (5 Nov): link turns to agent runs; remember how far the rolling summary goes.

Revision ID: agents_0002
Revises: agents_0001
Create Date: 2026-11-05

Source: docs/V1_CHANGE_PLAN.md WP1 (agents). Idempotent by construction; forward-only like the baseline.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "agents_0002"
down_revision = "agents_0001"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = agents, public;
-- the run that produced an AI turn (plain ref: a buyer turn is stored before its run exists)
ALTER TABLE turns ADD COLUMN IF NOT EXISTS run_id uuid;
CREATE INDEX IF NOT EXISTS turns_run_idx ON turns (run_id) WHERE run_id IS NOT NULL;
-- conversations.summary covers turns 1..summary_upto_turn
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS summary_upto_turn integer;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
