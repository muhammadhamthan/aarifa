"""v1 flow WP6: the buyer-facing lines of the quote message (not the PDF).

Revision ID: crm_0005
Revises: crm_0004
Create Date: 2026-11-06

* `cover_note`: the natural "open to discuss" line written by the AI communication agent for the message
  only (§11.4, D9); the salesperson sees it on the approval screen.
* `change_summary`: what changed in a revised quote, written by the salesperson for the buyer (§13.3
  "a short message that explains what changed"); kept apart from internal correction notes.

Both are part of the message body, which the send grant hashes at approval. Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "crm_0005"
down_revision = "crm_0004"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = crm, public;
ALTER TABLE quote_versions ADD COLUMN IF NOT EXISTS cover_note text;
ALTER TABLE quote_versions ADD COLUMN IF NOT EXISTS change_summary text;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
