"""Whole-app smoke test on real Postgres: migrate fresh DBs, boot the API with every package wired,
check health + auth, and prove an event fans out to durable jobs (automation.scheduled_jobs, D25) for each
subscribed package."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

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
    with psycopg.connect(migrated["automation"]) as c:  # the job queue is a table now (no procrastinate)
        assert c.execute("select to_regclass('automation.scheduled_jobs')").fetchone() != (None,)


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
        # Beat's timetable names every periodic job the worker knows (and nothing it does not)
        from arifaa_pack.schedule import beat_schedule

        assert set(beat_schedule()) == set(container.periodic)
        # WP9 / D14: v2 features are off by the crm.settings seed: the daily scraper returns at once
        assert await container.periodic["automation.price_scrapes"](datetime.now(UTC)) is None
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
        await container.bus.publish(event)  # re-published by a relay retry: still one job per package
        with psycopg.connect(migrated["automation"]) as c:
            rows = c.execute(
                "select id, task, status from automation.scheduled_jobs where args->'event'->>'event_id' = %s",
                (str(event.event_id),),
            ).fetchall()
        assert [(r[1], r[2]) for r in rows] == [("crm.handle_event", "todo")]  # only the CRM consumes it
        assert await container.runner.run(rows[0][0]) == "done"  # a worker runs it once ...
        assert await container.runner.run(rows[0][0]) == "skipped"  # ... and a redelivery is a no-op
