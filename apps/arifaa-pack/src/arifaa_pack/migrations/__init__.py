"""Schema migrations: one Alembic script directory per logical database (crm, automation, agents).

Revision 0001 of each applies the reviewed DDL in db/ddl (the source of truth for v1); later revisions are
ordinary Alembic ops. Migrations run as the database's owning app role over psycopg (sync driver: it runs
multi-statement DDL with $$ bodies without prepared statements). Extensions are pre-created by the infra
bootstrap as the RDS master user (CREATE EXTENSION IF NOT EXISTS is then a no-op for the app role).
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterable
from typing import Any, cast

import psycopg
from alembic import command, op
from alembic.config import Config

from kx.core.settings import CoreSettings, LogicalDb, get_settings

HERE = pathlib.Path(__file__).resolve().parent
DDL_FILES = {LogicalDb.CRM: "01_crm.sql", LogicalDb.AUTOMATION: "02_automation.sql",
             LogicalDb.AGENTS: "03_agents.sql"}


def ddl_dir() -> pathlib.Path:
    """KX_DDL_DIR (container: /app/db/ddl) or the repo's db/ddl when running from a checkout."""
    if env := os.environ.get("KX_DDL_DIR"):
        return pathlib.Path(env)
    for parent in HERE.parents:
        candidate = parent / "db" / "ddl"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("db/ddl not found; set KX_DDL_DIR")


def migration_dsn(db: LogicalDb, settings: CoreSettings | None = None) -> str:
    """KX_MIGRATE_<DB>_DSN override, else the app DSN switched to the psycopg driver."""
    if env := os.environ.get(f"KX_MIGRATE_{db.name}_DSN"):
        return env
    return (settings or get_settings()).dsn_for(db).replace("+asyncpg", "+psycopg", 1)


def config(db: LogicalDb, dsn: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE / db.value))
    cfg.attributes["dsn"] = dsn or migration_dsn(db)
    cfg.attributes["logical_db"] = db
    return cfg


def upgrade(dbs: Iterable[LogicalDb] = tuple(LogicalDb), revision: str = "head",
            dsns: dict[LogicalDb, str] | None = None) -> None:
    for db in dbs:
        command.upgrade(config(db, (dsns or {}).get(db)), revision)


def current(db: LogicalDb, dsn: str | None = None) -> None:
    command.current(config(db, dsn), verbose=True)


def apply_ddl_file(db: LogicalDb) -> None:
    """Used by each baseline revision: execute the whole reviewed DDL file in the migration transaction."""
    sql = (ddl_dir() / DDL_FILES[db]).read_text(encoding="utf-8")
    # Raw psycopg cursor with params=None: no %-placeholder parsing (the DDL contains regexes and LIKE '%').
    dbapi = cast("psycopg.Connection[Any]", op.get_bind().connection.dbapi_connection)
    with dbapi.cursor() as cur:
        cur.execute(sql)
