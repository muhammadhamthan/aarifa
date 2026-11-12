"""§20 screens 6/7/9/11/12 and §21 dashboards (11 Nov plan WP28) over HTTP."""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from conftest import MD_ID, MGR_ID, PRODUCT_ID, SALES2_ID, SALES_ID, Seed, assign, new_phone
from openpyxl import load_workbook  # type: ignore[import-untyped]
from test_api_db import Auth
from test_api_db import api as api  # noqa: PLC0414 - re-export the `api` fixture

from kx.core.auth import Role
from kx.core.contracts import AiRole, ContactPatch
from kx.crm.gateway import CrmGatewayImpl

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("cost_mode")]
Api = tuple[httpx.AsyncClient, Auth]


async def test_contacts_follow_deal_visibility(api: Api, databases: Any, deps: Any, seed: Seed) -> None:
    client, auth = api
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await gw.resolve_deal("whatsapp", new_phone(), None)
    await gw.set_contact(deal_id, ContactPatch(name="Visible Omar"), AiRole.CONTACT_CAPTURE)
    await assign(databases, deal_id, SALES_ID)
    auth.as_(Role.SALES, SALES_ID)
    mine = (await client.get("/v1/contacts", params={"q": "Visible Omar"})).json()["items"]
    assert [c["full_name"] for c in mine] == ["Visible Omar"]
    auth.as_(Role.SALES, SALES2_ID)
    assert (await client.get("/v1/contacts", params={"q": "Visible Omar"})).json()["items"] == []
    r = await client.get(f"/v1/contacts/{mine[0]['id']}")
    assert r.status_code == 404
    auth.as_(Role.SALES, SALES_ID)
    r = await client.patch(f"/v1/contacts/{mine[0]['id']}", json={"designation": "Buyer"})
    assert r.status_code == 200 and r.json()["designation"] == "Buyer"


async def test_product_edits_touch_only_crm_owned_fields(api: Api, seed: Seed) -> None:
    client, auth = api
    auth.as_(Role.MANAGER, MGR_ID)
    name = (await client.get(f"/v1/products/{PRODUCT_ID}")).json()["name"]
    r = await client.patch(f"/v1/products/{PRODUCT_ID}", json={"moq": 20_000, "name": "ignored"})
    assert r.status_code == 200 and r.json()["moq"] == 20_000
    assert r.json()["name"] == name  # D22: the CMS owns the description
    auth.as_(Role.MANAGER, MGR_ID, mfa=False)
    assert (await client.patch(f"/v1/products/{PRODUCT_ID}", json={"moq": 1})).status_code == 403


async def test_fx_override_wins(api: Api, seed: Seed) -> None:
    client, auth = api
    auth.as_(Role.MANAGER, MGR_ID)
    r = await client.post(
        "/v1/fx-rates", json={"currency": "SAR", "rate_to_aed": "0.98", "as_of": "2026-11-12"}
    )
    assert r.status_code == 201 and r.json()["source"] == "manual"
    rates = {x["currency"]: x for x in (await client.get("/v1/fx-rates")).json()}
    assert rates["SAR"]["rate_to_aed"] == "0.98000000"


async def test_dashboards_per_role(api: Api, seed: Seed) -> None:
    client, auth = api
    auth.as_(Role.SALES, SALES_ID)
    mine = await client.get("/v1/dashboards/salesperson")
    assert mine.status_code == 200 and mine.json()["name"] == "Sara"
    assert (
        await client.get("/v1/dashboards/salesperson", params={"user_id": str(SALES2_ID)})
    ).status_code == 403
    assert (await client.get("/v1/dashboards/manager")).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID)
    board = (await client.get("/v1/dashboards/manager")).json()
    assert {"salespeople", "opted_out", "opt_out_reasons", "sla_breaches"} <= board.keys()
    assert (await client.get("/v1/dashboards/md")).status_code == 403
    auth.as_(Role.MD, MD_ID)
    md = (await client.get("/v1/dashboards/md")).json()
    assert {"pipeline_aed_by_stage", "win_rate", "takeover_queue", "top_buyers"} <= md.keys()


async def test_exports_csv_and_xlsx(api: Api, seed: Seed) -> None:
    client, auth = api
    auth.as_(Role.MANAGER, MGR_ID)
    csv = await client.get("/v1/exports/products", params={"format": "csv"})
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("sku,name,category_code")
    xlsx = await client.get("/v1/exports/products", params={"format": "xlsx"})
    sheet = load_workbook(io.BytesIO(xlsx.content)).active
    assert sheet is not None and sheet.cell(1, 1).value == "sku"
