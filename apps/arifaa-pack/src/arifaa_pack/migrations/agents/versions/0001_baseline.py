"""Baseline: apply db/ddl for the agents database.

Revision ID: agents_0001
Revises:
Create Date: 2026-11-04
"""

from __future__ import annotations

from arifaa_pack.migrations import apply_ddl_file
from kx.core.settings import LogicalDb

revision = "agents_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    apply_ddl_file(LogicalDb.AGENTS)


def downgrade() -> None:
    raise NotImplementedError("baseline cannot be downgraded; restore from snapshot")
