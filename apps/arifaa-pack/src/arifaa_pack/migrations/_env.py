"""Shared Alembic env: every script directory's env.py calls `run()`. Online mode only."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


def run() -> None:
    cfg = context.config
    engine = create_engine(cfg.attributes["dsn"], poolclass=NullPool)
    with engine.connect() as connection:
        # version table in public: the app schema does not exist before revision 0001 creates it
        context.configure(
            connection=connection,
            target_metadata=None,
            transactional_ddl=True,
            version_table="alembic_version",
            version_table_schema="public",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
