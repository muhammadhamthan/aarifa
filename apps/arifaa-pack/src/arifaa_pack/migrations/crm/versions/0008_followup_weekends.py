"""v1 flow: follow-ups on the buyer country's working days (§16, §23 scenario 9, §24 "Holiday or weekend").

Revision ID: crm_0008
Revises: crm_0007
Create Date: 2026-11-06

ISO weekdays off per buyer country (1 = Monday ... 7 = Sunday). The business flow evidences the Saudi weekend
only ("day 5 is a Saturday, a Saudi weekend day"); other countries are added by the manager. Idempotent.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0008"
down_revision = "crm_0007"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;
INSERT INTO settings (key, value, description) VALUES
 ('followup.weekend_days_by_country', '{"SA": [5, 6]}', 'ISO weekdays off per buyer country; follow-ups move to the next working day (§16)')
ON CONFLICT (key) DO NOTHING;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
