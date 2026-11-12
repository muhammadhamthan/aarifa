"""§6 website conversations (11 Nov plan WP19): when the visitor consented to voice.

Revision ID: agents_0003
Revises: agents_0002
Create Date: 2026-11-12

Source: the SRS §6 step 1, "Pressing [Enter the factory] is the visitor's consent to start voice"
(OPEN_QUESTIONS #57: the consent is stored with its time; voice tokens are refused without it).
Idempotent; forward-only.
"""

from __future__ import annotations

from arifaa_pack.migrations import execute_sql

revision = "agents_0003"
down_revision = "agents_0002"
branch_labels = None
depends_on = None

SQL = r"""
SET search_path = agents, public;

ALTER TABLE visitors ADD COLUMN IF NOT EXISTS voice_consent_at timestamptz;
"""


def upgrade() -> None:
    execute_sql(SQL)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from snapshot")
