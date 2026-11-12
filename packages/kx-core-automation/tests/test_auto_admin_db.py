"""§20.11 admin settings in automation (11 Nov plan WP28): regional WhatsApp numbers and message templates."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from kx.automation.admin_api import build_router
from kx.core.auth import Principal, Role, current_principal
from kx.core.errors import install_problem_handlers

pytestmark = pytest.mark.db
ADMIN = Principal("admin", uuid.uuid4(), Role.ADMIN, mfa=True, is_human=True)


@pytest.fixture
async def client(auto_db: Any) -> AsyncIterator[tuple[httpx.AsyncClient, list[Principal]]]:
    who = [ADMIN]
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(build_router(auto_db))
    app.dependency_overrides[current_principal] = lambda: who[0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, who


async def test_whatsapp_template_waits_for_meta_then_is_usable(
    client: tuple[httpx.AsyncClient, list[Principal]],
) -> None:
    c, _ = client
    body = {
        "channel": "whatsapp",
        "name": "staff_reopen",
        "language": "en",
        "body": "{{1}} from {{2}} here about your order - reply to continue.",
    }
    r = await c.put("/v1/admin/message-templates", json=body)
    assert r.status_code == 200 and r.json()["provider_status"] == "submitted"
    r = await c.post(
        f"/v1/admin/message-templates/{r.json()['id']}/status", json={"provider_status": "approved"}
    )
    assert r.json()["provider_status"] == "approved"
    r = await c.put("/v1/admin/message-templates", json=body | {"body": "changed text {{1}} {{2}}"})
    assert r.json()["provider_status"] == "submitted"  # an edit needs Meta's approval again


async def test_regional_numbers_and_admin_only(client: tuple[httpx.AsyncClient, list[Principal]]) -> None:
    c, who = client
    accounts = (await c.get("/v1/admin/channel-accounts")).json()
    assert accounts and "secret_ref" not in accounts[0]
    r = await c.patch(f"/v1/admin/channel-accounts/{accounts[0]['id']}", json={"countries": ["SA", "AE"]})
    assert r.status_code == 200 and r.json()["countries"] == ["SA", "AE"]
    who[0] = Principal("sales", uuid.uuid4(), Role.SALES, mfa=True, is_human=True)
    assert (
        await c.patch(f"/v1/admin/channel-accounts/{accounts[0]['id']}", json={"is_default": True})
    ).status_code == 403
