"""v1 flow WP12: the client's choice in the §17 notifications matrix.

Revision ID: crm_0007
Revises: crm_0006
Create Date: 2026-11-06

§15.3 / §17 "Order status changed ... Buyer: optional WhatsApp": whether the buyer gets a WhatsApp update for
each order status is a client choice, off until the client says yes. Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0007"
down_revision = "crm_0006"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;
INSERT INTO settings (key, value, description) VALUES
 ('notifications.order_status_to_buyer', 'false', 'Send the buyer a WhatsApp update for each order status (§15.3, client choice)')
ON CONFLICT (key) DO NOTHING;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
