"""§21 scheduled reports: "a daily summary and a weekly report to the manager and MD (recipients and times
are settings)".

Revision ID: crm_0011
Revises: crm_0010
Create Date: 2026-11-12

Times are company time (the `approval.alert_hours` zone). 08:00 (the start of alert hours) and Monday are
proposals until the client confirms them (docs/OPEN_QUESTIONS.md #67). Idempotent.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0011"
down_revision = "crm_0010"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;
INSERT INTO settings (key, value, description) VALUES
 ('reports.daily_time',  '"08:00"',             'Daily summary to the recipients, company time (§21)'),
 ('reports.weekly_day',  '1',                   'Weekly report weekday, ISO (1 = Monday) (§21)'),
 ('reports.weekly_time', '"08:00"',             'Weekly report time, company time (§21)'),
 ('reports.recipients',  '["manager", "md"]',   'CRM roles that receive the scheduled reports (§21)')
ON CONFLICT (key) DO NOTHING;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
