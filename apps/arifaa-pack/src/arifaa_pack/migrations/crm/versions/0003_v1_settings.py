"""v1 flow: settings found while building WP2-WP12 that the plan's WP1 seed did not list.

Revision ID: crm_0003
Revises: crm_0002
Create Date: 2026-11-05

Each key is a rule of docs/BUSINESS_FLOW_V1.md that needs a manager-editable value; where the documents
give no value, the default is the safe one and the gap is logged in docs/OPEN_QUESTIONS.md.
Idempotent (ON CONFLICT DO NOTHING keeps edited values); forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0003"
down_revision = "crm_0002"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;
INSERT INTO settings (key, value, description) VALUES
 ('quote.payment_terms_text', '"Advance payment is required before production starts."', 'Default payment terms printed on quotes; the salesperson can edit them (§11.2)'),
 ('quote.currency_by_country', '{}', 'Quote currency per delivery country, e.g. {"SA":"SAR"}; unlisted countries quote in AED (§8, §10)'),
 ('negotiation.free_samples_allowed', 'false', 'May the AI promise free samples or freebies (§12.2 rule 6)')
ON CONFLICT (key) DO NOTHING;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
