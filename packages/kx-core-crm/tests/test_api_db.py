"""HTTP smoke test of the staff API: routing, auth dependencies, RFC 9457 errors, keyset pages."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal as D
from typing import Any

import httpx
import pytest
from conftest import MD_ID, MGR_ID, SALES_ID, SKU, Seed, assign, new_phone
from fastapi import FastAPI

from kx.core.auth import Principal, Role, current_principal
from kx.core.contracts import AiRole, ContactPatch, RequirementPatch
from kx.core.errors import install_problem_handlers
from kx.crm.api import build_router
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("cost_mode")]


class Auth:
    def __init__(self) -> None:
        self.principal = Principal("md", MD_ID, Role.MD, mfa=True, is_human=True)

    def as_(self, role: Role, user_id: uuid.UUID, *, mfa: bool = True) -> None:
        self.principal = Principal(str(user_id), user_id, role, mfa=mfa, is_human=True)


@pytest.fixture
async def api(databases: Any, seed: Seed, deps: CrmDeps) -> AsyncIterator[tuple[httpx.AsyncClient, Auth]]:
    app = FastAPI()
    app.include_router(build_router(databases, deps), prefix="/v1")
    install_problem_handlers(app)
    auth = Auth()
    app.dependency_overrides[current_principal] = lambda: auth.principal
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        yield client, auth


async def test_staff_api_flow(api: tuple[httpx.AsyncClient, Auth], databases: Any, deps: CrmDeps) -> None:
    client, auth = api
    gw = CrmGatewayImpl(databases, deps)
    deal_ids = []
    for _ in range(3):
        deal_id = await gw.resolve_deal("whatsapp", new_phone(), None)
        await gw.update_requirement(
            deal_id,
            RequirementPatch(product_sku=SKU, quantity=D(50_000), delivery_country="AE"),
            AiRole.QUOTATION,
        )
        await gw.set_contact(deal_id, ContactPatch(name="Omar"), AiRole.CONTACT_CAPTURE)
        deal_ids.append(deal_id)

    # keyset pagination over the pipeline
    first = (await client.get("/v1/deals", params={"limit": 2})).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = (await client.get("/v1/deals", params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert {d["id"] for d in first["items"]}.isdisjoint({d["id"] for d in second["items"]})
    bad = await client.get("/v1/deals", params={"cursor": "%%%"})
    assert bad.status_code == 422 and bad.headers["content-type"] == "application/problem+json"

    deal = (await client.get(f"/v1/deals/{deal_ids[0]}")).json()
    assert deal["stage"] == "NEW" and deal["missing_fields"] == [] and deal["contact"]["full_name"] == "Omar"

    # stage machine via HTTP: LOST without reason is a problem+json 422; stale version is 409
    r = await client.post(
        f"/v1/deals/{deal_ids[2]}/stage", json={"to": "LOST", "expected_version": deal["version"]}
    )
    assert r.status_code == 422
    r = await client.post(
        f"/v1/deals/{deal_ids[2]}/stage", json={"to": "QUALIFIED", "expected_version": 999, "reason": "x"}
    )
    assert r.status_code == 409 and r.json()["type"].endswith("/conflict")
    r = await client.post(f"/v1/deals/{deal_ids[0]}/stage", json={"to": "QUALIFIED", "expected_version": 1})
    assert r.status_code == 200 and r.json()["stage"] == "QUALIFIED"

    # quote by the deal's salesperson, approvals list, decision needs MFA; costs are MD-only
    auth.as_(Role.SALES, MGR_ID)  # a salesperson who does not own the deal (§3)
    assert (await client.post(f"/v1/deals/{deal_ids[0]}/quotes")).status_code == 403
    await assign(databases, deal_ids[0], SALES_ID)
    auth.as_(Role.SALES, SALES_ID)
    version = (await client.post(f"/v1/deals/{deal_ids[0]}/quotes")).json()
    assert version["status"] == "pending_approval" and "unit_cost" not in str(version)
    assert (await client.get("/v1/approvals")).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID)
    open_ = (await client.get("/v1/approvals", params={"limit": 100})).json()["items"]
    approval = next(a for a in open_ if a["quote_version_id"] == version["id"])
    assert (await client.get(f"/v1/quote-versions/{version['id']}/costs")).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID, mfa=False)
    r = await client.post(f"/v1/approvals/{approval['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 403
    auth.as_(Role.MANAGER, MGR_ID)
    r = await client.post(f"/v1/approvals/{approval['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    got = (await client.get(f"/v1/quote-versions/{version['id']}")).json()
    assert got["status"] == "approved" and got["lines"][0]["unit_price"] == "0.227804"

    auth.as_(Role.MD, MD_ID)
    costs = (await client.get(f"/v1/quote-versions/{version['id']}/costs")).json()
    assert costs[0]["markup_pct"] == "50.00" and costs[0]["markup_min_pct"] == "40.00"
    timeline = (await client.get(f"/v1/deals/{deal_ids[0]}/timeline", params={"limit": 3})).json()
    assert len(timeline["items"]) == 3 and timeline["next_cursor"]
    summary = (await client.get("/v1/dashboard/summary")).json()
    assert summary["deals_by_stage"]["QUOTATION"] >= 1 and any(
        m["level"] == "ok" for m in summary["materials"]
    )
    settings = (await client.get("/v1/settings")).json()
    assert any(s["key"] == "grant.ttl_hours" for s in settings)
    r = await client.put("/v1/settings/grant.ttl_hours", json={"value": 0})
    assert r.status_code == 422
    r = await client.put("/v1/settings/grant.ttl_hours", json={"value": 48})
    assert r.status_code == 200 and r.json()["value"] == 48

    # takeover: manager pauses the AI and hands back
    auth.as_(Role.MANAGER, MGR_ID)
    r = await client.post(f"/v1/deals/{deal_ids[0]}/ai", json={"enabled": False})
    assert r.json()["ai_enabled"] is False and r.json()["ai_paused_reason"] == "human_takeover"
    r = await client.post(f"/v1/deals/{deal_ids[0]}/ai", json={"enabled": True})
    assert r.json()["ai_enabled"] is True
