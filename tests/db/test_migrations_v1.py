"""WP1: revision 0002 (5 Nov flow) on real Postgres.

Done when (docs/V1_CHANGE_PLAN.md WP1): `kx migrate` runs twice on fresh DBs and on the 0001 state. The
ORM-vs-information_schema checks live in each package (test_orm_matches_ddl); this file proves the upgrade
path from a 0001 database that already holds data, idempotency, and the new constraints.
"""

from __future__ import annotations

import importlib.util
import pathlib
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from kx.core.identity import recipient_hash

from ..conftest import ADMIN_DSN, migrate

pytestmark = pytest.mark.db
NAMES = {"crm": "mig_crm", "automation": "mig_automation", "agents": "mig_agents"}


def _dsn(db: str) -> str:
    assert ADMIN_DSN
    base, _, query = ADMIN_DSN.partition("?")
    return f"{base.rsplit('/', 1)[0]}/{db}" + (f"?{query}" if query else "")


@pytest.fixture(scope="module")
def at_0001() -> Iterator[dict[str, str]]:
    """Fresh DBs migrated to the 0001 baseline only, with 4 Nov data in them."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        for name in NAMES.values():
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{name}"')
    dsns = {k: _dsn(v) for k, v in NAMES.items()}
    for key, dsn in dsns.items():
        migrate({key: dsn}, f"{key}_0001")
    with psycopg.connect(dsns["crm"]) as c:
        c.execute("SET search_path = crm")
        c.execute(
            "INSERT INTO users (id, email, full_name, role) VALUES "
            "('00000000-0000-7000-8000-000000000001', 'md@x.ae', 'MD', 'md')"
        )
        c.execute(
            "INSERT INTO contacts (id, full_name, phone_e164, email, source) VALUES "
            "('00000000-0000-7000-8000-000000000002', 'Ahmed', '+971501234567', 'ahmed@buyer.ae', 'whatsapp')"
        )
        # a customised 4 Nov setting must survive the seed
        c.execute("UPDATE settings SET value = '10' WHERE key = 'quote.validity_days'")
    yield dsns
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        for name in NAMES.values():
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def upgraded(at_0001: dict[str, str]) -> dict[str, str]:
    migrate(at_0001)
    migrate(at_0001)  # second run is a no-op
    return at_0001


def _revisions(db: str) -> list[Any]:
    """The 5 Nov revisions (0002 and later) of one database, oldest first."""
    import arifaa_pack.migrations as mig

    out = []
    for path in sorted((pathlib.Path(mig.__file__).parent / db / "versions").glob("000[2-9]_*.py")):
        spec = importlib.util.spec_from_file_location(f"_mig_{db}_{path.stem}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        out.append(module)
    return out


def _one(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(dsn) as c:
        row = c.execute(sql, params).fetchone()
    assert row is not None
    return row[0]


def _fails(dsn: str, sql: str) -> bool:
    """True when the statement violates a constraint; a statement that succeeds is committed."""
    with psycopg.connect(dsn) as c:
        try:
            c.execute(sql)
        except psycopg.errors.IntegrityError:
            c.rollback()
            return True
        c.commit()
    return False


def test_versions_are_head(upgraded: dict[str, str]) -> None:
    for key, dsn in upgraded.items():
        assert _one(dsn, "select version_num from public.alembic_version") == _revisions(key)[-1].revision


def test_sql_is_idempotent_when_rerun(upgraded: dict[str, str]) -> None:
    """A half-applied revision can be re-run: every statement is IF NOT EXISTS / ON CONFLICT."""
    for key, dsn in upgraded.items():
        for module in _revisions(key):
            with psycopg.connect(dsn) as c:
                c.execute(str(module.SQL).encode())


def test_settings_seeded_and_existing_values_kept(upgraded: dict[str, str]) -> None:
    crm = upgraded["crm"]
    expect = {
        "pricing.mode": "price_list",
        "quote.vat_pct_domestic": 5,
        "quote.default_incoterm": "EXW",
        "negotiation.max_steps": 3,
        "change_request.sla_working_hours": 4,
        "approval.reminder_minutes": [30, 60, 120],
        "approval.md_value_limit_aed": None,
        "followup.lost_after_days": 14,
        "assignment.rules": [],
        "features.scraping": False,
        "features.delivery_calc": False,
        "features.news_digest": False,
    }
    for key, value in expect.items():
        assert _one(crm, "select value from crm.settings where key = %s", (key,)) == value, key
    assert _one(crm, "select value from crm.settings where key = 'quote.validity_days'") == 10
    assert not _one(
        crm, "select bool_or(enabled) from crm.feature_flags where key in ('news_digest','price_scraping')"
    )


def test_contact_recipient_hashes_match_python(upgraded: dict[str, str]) -> None:
    hashes = _one(upgraded["crm"], "select recipient_hashes from crm.contacts where full_name = 'Ahmed'")
    assert set(hashes) == {
        recipient_hash("whatsapp", "+971501234567"),
        recipient_hash("email", "ahmed@buyer.ae"),
    }
    assert (
        _one(
            upgraded["crm"],
            "select count(*) from crm.contacts where recipient_hashes @> ARRAY[%s]",
            (recipient_hash("email", "ahmed@buyer.ae"),),
        )
        == 1
    )


def _approval_sql(kind: str, role: str) -> str:
    return (
        "WITH d AS (INSERT INTO crm.deals (primary_contact_id, source) VALUES "
        "('00000000-0000-7000-8000-000000000002','whatsapp') RETURNING id), "
        "q AS (INSERT INTO crm.quotes (deal_id) SELECT id FROM d RETURNING id), "
        "v AS (INSERT INTO crm.quote_versions (quote_id, version_no, kind, currency, subtotal, total, total_aed, "
        "validity_days, created_by_type, created_by) SELECT id, 1, 'final', 'AED', 1, 1, 1, 7, 'ai', 'x' FROM q "
        "RETURNING id) "
        "INSERT INTO crm.approvals (quote_version_id, kind, required_role, window_minutes, deadline_at) "
        f"SELECT id, '{kind}', '{role}', 120, now() FROM v"
    )


def test_salesperson_may_approve_final_and_special_price_is_manager_only(upgraded: dict[str, str]) -> None:
    crm = upgraded["crm"]
    assert not _fails(crm, _approval_sql("final", "sales"))  # D1: the 4 Nov "final = MD" CHECK is gone
    assert not _fails(crm, _approval_sql("special_price", "manager"))
    assert _fails(crm, _approval_sql("special_price", "sales"))  # D3
    assert _fails(crm, _approval_sql("final", "admin"))


def test_price_tiers_never_overlap_and_min_le_list(upgraded: dict[str, str]) -> None:
    crm = upgraded["crm"]
    with psycopg.connect(crm) as c:
        c.execute("INSERT INTO crm.product_categories (code, name) VALUES ('ALU', 'Aluminium')")
        pid = uuid.uuid4()
        c.execute(
            "INSERT INTO crm.products (id, sku, name, category_code) VALUES (%s, 'ALC-750-L', 'x', 'ALU')",
            (pid,),
        )
        c.execute(
            "INSERT INTO crm.product_price_tiers (product_id, min_qty, max_qty, list_price, min_price, valid_from) "
            "VALUES (%s, 5000, 9999, 0.32, 0.29, '2026-11-05'), (%s, 10000, 49999, 0.28, 0.255, '2026-11-05'), "
            "(%s, 50000, NULL, 0.25, 0.23, '2026-11-05')",
            (pid, pid, pid),
        )
    tier = "INSERT INTO crm.product_price_tiers (product_id, min_qty, max_qty, list_price, min_price, valid_from) "
    assert _fails(crm, tier + f"VALUES ('{pid}', 9000, 12000, 0.30, 0.27, '2026-11-05')")  # overlaps 2 tiers
    assert _fails(crm, tier + f"VALUES ('{pid}', 60000, NULL, 0.24, 0.22, '2026-11-05')")  # inside 50,000+
    assert _fails(crm, tier + f"VALUES ('{pid}', 1000, 4999, 0.30, 0.31, '2026-11-05')")  # min > list
    # the next price-list version (newer valid_from) may reuse the same ranges
    assert not _fails(crm, tier + f"VALUES ('{pid}', 50000, NULL, 0.26, 0.24, '2026-12-01')")


def test_new_quote_fields_freeze_after_draft(upgraded: dict[str, str]) -> None:
    crm = upgraded["crm"]
    with psycopg.connect(crm) as c:
        c.execute(_approval_sql("final", "sales"))
        vid = c.execute("select id from crm.quote_versions order by created_at desc limit 1").fetchone()
        assert vid
        c.execute(
            "UPDATE crm.quote_versions SET delivery_time_text = '10-14 working days' WHERE id = %s", vid
        )
        c.execute("UPDATE crm.quote_versions SET status = 'pending_approval' WHERE id = %s", vid)
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE crm.quote_versions SET delivery_time_text = '5 days' WHERE id = %s", vid)


def test_automation_constraints(upgraded: dict[str, str]) -> None:
    auto = upgraded["automation"]
    ref = uuid.uuid4()
    call = (
        "INSERT INTO automation.call_attempts (purpose, approval_ref, to_e164, provider, attempt_no) "
        f"VALUES ('change_request_alert', '{ref}', '+971500000000', 'twilio', 1)"
    )
    assert not _fails(auto, call)
    assert _fails(auto, call)  # UNIQUE (approval_ref, attempt_no)
    account = (
        "INSERT INTO automation.channel_accounts (channel, provider, external_account_id, display_name, address, "
        "purpose, secret_ref, region, countries, is_default) VALUES "
        "('whatsapp', 'meta_cloud', '{id}', 'Arifa Pack', '+97150', 'customer', 'arn', 'GCC', '{{AE,SA}}', true)"
    )
    assert not _fails(auto, account.format(id="P1"))
    assert _fails(auto, account.format(id="P2"))  # one default per channel and purpose


def test_agents_columns(upgraded: dict[str, str]) -> None:
    cols = _one(
        upgraded["agents"],
        "select array_agg(table_name || '.' || column_name) from information_schema.columns "
        "where table_schema = 'agents' and column_name in ('run_id', 'summary_upto_turn')",
    )
    assert {"turns.run_id", "conversations.summary_upto_turn", "tool_calls.run_id"} <= set(cols)
