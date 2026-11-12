"""§7 / D22 CMS publish webhook (11 Nov plan WP18): signed, replay-safe, re-indexes the AI's knowledge per
locale and mirrors products to the CRM without any price."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import orjson
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from kx.automation.cms import build_router, signature
from kx.core import events as ev
from kx.core.errors import install_problem_handlers
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db
SECRET = "cms-test-secret"


@pytest.fixture
async def client(auto_db: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(build_router(auto_db, SECRET))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


def product_event(**kw: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "operation": "publish",
        "collection": "products",
        "doc_id": "cms-42",
        "locales": ["en", "ar"],
        "url": "/products/alu-750",
        "product": {
            "code": "alu-750",
            "name": "Aluminium container 750 ml",
            "category_code": "ALU_CONTAINER",
            "spec": {"capacity_ml": 750, "material": "8011", "price_aed": 0.42, "Unit Cost": 1},
            "custom_printing": True,
        },
    } | kw


async def post(
    client: httpx.AsyncClient, body: dict[str, Any], *, secret: str = SECRET, ts: int | None = None
) -> httpx.Response:
    raw = orjson.dumps(body)
    stamp = str(ts if ts is not None else int(time.time()))
    headers = {"x-kx-timestamp": stamp, "x-kx-signature": signature(secret, stamp, raw)}
    return await client.post("/webhooks/cms", content=raw, headers=headers)


async def outbox(auto_db: Any) -> list[tuple[str, dict[str, Any]]]:
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        rows = await s.execute(text("SELECT event_type, payload FROM outbox_events ORDER BY sequence"))
        return [(r[0], r[1]) for r in rows]


async def test_publish_reindexes_each_locale_and_mirrors_the_product_without_prices(
    client: httpx.AsyncClient, auto_db: Any
) -> None:
    body = product_event()
    assert (await post(client, body)).status_code == 200
    assert (await post(client, body)).status_code == 200  # the CMS retried: handled once
    events = await outbox(auto_db)
    kb = [p for t, p in events if t == ev.KB_CONTENT_PUBLISHED]
    assert sorted(p["locale"] for p in kb) == ["ar", "en"] and kb[0]["doc_id"] == "cms-42"
    (product,) = [p for t, p in events if t == ev.CATALOGUE_PRODUCT_PUBLISHED]
    assert product["code"] == "alu-750" and product["published"] is True
    assert product["spec"] == {
        "capacity_ml": 750,
        "material": "8011",
    }  # D22: no price ever comes from the CMS


async def test_unpublish_reaches_the_crm_and_the_knowledge_base(
    client: httpx.AsyncClient, auto_db: Any
) -> None:
    assert (await post(client, product_event(operation="unpublish"))).status_code == 200
    events = await outbox(auto_db)
    assert any(t == ev.KB_CONTENT_PUBLISHED for t, _ in events)  # the indexer drops what is not published
    (product,) = [p for t, p in events if t == ev.CATALOGUE_PRODUCT_PUBLISHED]
    assert product["published"] is False


async def test_media_and_settings_are_not_ai_knowledge(client: httpx.AsyncClient, auto_db: Any) -> None:
    body = product_event(collection="media", product=None)
    assert (await post(client, body)).status_code == 200
    assert await outbox(auto_db) == []


@pytest.mark.parametrize("case", ["bad_secret", "too_old", "unsigned"])
async def test_unsigned_or_stale_requests_write_nothing(
    client: httpx.AsyncClient, auto_db: Any, case: str
) -> None:
    body = product_event()
    if case == "bad_secret":
        r = await post(client, body, secret="wrong")
    elif case == "too_old":
        r = await post(client, body, ts=int(time.time()) - 3600)
    else:
        r = await client.post("/webhooks/cms", content=orjson.dumps(body))
    assert r.status_code == 401 and await outbox(auto_db) == []
