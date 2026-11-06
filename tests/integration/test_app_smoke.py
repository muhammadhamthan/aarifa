"""Whole-app smoke test on real Postgres: migrate fresh DBs, boot the API with every package wired,
check health + auth, and prove an event fans out to durable jobs for each subscribed package."""

from __future__ import annotations

import uuid

import psycopg
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from kx.core import events as ev
from kx.core.contracts import AiRole
from kx.core.event_payloads import PriceProposed, make_event

from ..conftest import ADMIN_DSN

pytestmark = pytest.mark.db
NAMES = {"crm": "smoke_crm", "automation": "smoke_automation", "agents": "smoke_agents"}


def _dsn(db: str, driver: str = "") -> str:
    assert ADMIN_DSN
    base, _, query = ADMIN_DSN.partition("?")
    head = base.rsplit("/", 1)[0].replace("postgresql://", f"postgresql{driver}://", 1)
    return f"{head}/{db}" + (f"?{query}" if query else "")


@pytest.fixture(scope="module")
def migrated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    from arifaa_pack.cli import main

    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        for name in NAMES.values():
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{name}"')
    mp = pytest.MonkeyPatch()
    for key, name in NAMES.items():
        mp.setenv(f"KX_{key.upper()}_DSN", _dsn(name, "+asyncpg"))
    mp.setenv("KX_AWS_REGION", "me-central-1")
    mp.setenv("AWS_ACCESS_KEY_ID", "test")
    mp.setenv("AWS_SECRET_ACCESS_KEY", "test")
    _clear_caches()
    main(["migrate"])
    main(["migrate"])  # idempotent
    yield {k: _dsn(v) for k, v in NAMES.items()}
    mp.undo()
    _clear_caches()


def _clear_caches() -> None:
    from arifaa_pack.config import app_settings
    from kx.core.settings import get_settings

    get_settings.cache_clear()
    app_settings.cache_clear()


def test_migrations_create_all_schemas(migrated: dict[str, str]) -> None:
    expect = {"crm": 45, "automation": 12, "agents": 14}
    for key, dsn in migrated.items():
        with psycopg.connect(dsn) as c:
            n = c.execute(
                "select count(*) from information_schema.tables where table_schema=%s", (key,)
            ).fetchone()
            assert n and n[0] >= expect[key]
    with psycopg.connect(migrated["automation"]) as c:
        assert c.execute("select to_regclass('public.procrastinate_jobs')").fetchone() != (None,)


async def test_api_boots_and_routes_events(migrated: dict[str, str]) -> None:
    from arifaa_pack.api import create_app

    app = create_app()
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http,
    ):
        assert (await http.get("/healthz")).json() == {"status": "ok"}
        r = await http.get("/v1/deals")
        assert r.status_code == 401 and r.headers["content-type"].startswith("application/problem+json")
        assert (await http.get("/webhooks/whatsapp", params={"hub.mode": "subscribe"})).status_code in {
            401,
            403,
        }

        container = app.state.container
        # WP9 / D14: v2 features are off by the crm.settings seed; the scraper task exists, unscheduled
        periodic = {p.task.name for p in container.proc.periodic_registry.periodic_tasks.values()}
        assert "automation:run_price_scrapes" in container.proc.tasks
        assert "automation:run_price_scrapes" not in periodic and "automation:relay_outbox" in periodic
        assert not container.agent_service.registry.get(AiRole.REPEAT_SALES).enabled
        event = make_event(
            ev.PRICE_PROPOSED,
            "price_observation",
            uuid.uuid4(),
            PriceProposed(
                observation_ref=uuid.uuid4(),
                price_source_ref=uuid.uuid4(),
                material_code="AL8011",
                price="12.40",
                currency="AED",
                unit="kg",
                observed_at="2026-11-04T06:00:00Z",
                source_url="https://example.org/lme",
            ),
        )
        await container.bus.publish(event)
        with psycopg.connect(migrated["automation"]) as c:
            rows = c.execute(
                "select task_name from procrastinate_jobs where args->'event'->>'event_id' = %s",
                (str(event.event_id),),
            ).fetchall()
        assert {r[0] for r in rows} == {"app:crm_event"}  # CRM consumes price proposals; nobody else
