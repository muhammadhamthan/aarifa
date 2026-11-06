"""WP11: the CRM API behind the §20 screens (inbox, price list + Excel, stock, settings for the new keys)."""

from __future__ import annotations

import io
from decimal import Decimal as D

import httpx
import pytest
from conftest import LIST_PRODUCT_ID, MGR_ID, SALES2_ID, SALES_ID, set_setting
from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]
from sqlalchemy import select
from test_api_db import Auth
from test_api_db import api as api  # noqa: PLC0414 - re-export the `api` fixture
from test_approvals_db import sara_deal
from test_change_requests_db import raise_delivery
from test_flow_db import SARA, approve_and_send

from kx.core.auth import Role
from kx.core.contracts import AiRole
from kx.core.db import Databases
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl
from kx.crm.services.price_lists import parse_rows

pytestmark = pytest.mark.usefixtures("price_list")
CRM = LogicalDb.CRM
SKUS = {"ALC-750-L", "ALU-750"}


# ---------------------------------------------------------------- Excel row validation (pure)
def test_import_rows_are_validated_before_anything_is_saved() -> None:
    rows = [
        ("ALC-750-L", 5000, 9999, "0.32", "0.29", "AED"),
        ("ALC-750-L", 9000, 49999, "0.28", "0.255", "AED"),  # overlaps row 2
        ("ALC-750-L", 50000, None, 0.25, 0.30, None),  # minimum above list (floats from Excel)
        ("NOPE-1", 1000, None, "0.5", None, "AED"),  # unknown code
        ("ALU-750", "x", None, "0.5", None, "USD"),  # not a number, not AED
        (None, None, None, None, None, None),  # blank row: skipped
    ]
    parsed, errors = parse_rows(rows, SKUS)
    got = {(e.row, e.field) for e in errors}
    assert {(3, "min_qty"), (4, "min_price"), (5, "sku"), (6, "min_qty"), (6, "currency")} <= got
    assert parsed[2].list_price == D("0.25") and parsed[2].min_price == D("0.3")  # exact, not 0.25000000001


def test_valid_rows_have_no_errors() -> None:
    rows = [
        ("ALC-750-L", 5000, 9999, "0.32", "0.29", "AED"),
        ("ALC-750-L", 10000, 49999, "0.28", "0.255", "AED"),
        ("ALC-750-L", 50000, None, "0.25", "0.23", "AED"),
    ]
    parsed, errors = parse_rows(rows, SKUS)
    assert errors == [] and len(parsed) == 3


# ---------------------------------------------------------------- inbox (§20.2)
@pytest.mark.db
async def test_inbox_shows_my_approvals_and_change_requests_with_timers(
    api: tuple[httpx.AsyncClient, Auth], databases: Databases, deps: CrmDeps
) -> None:
    client, auth = api
    deal_id = await sara_deal(databases, deps)  # quote waiting for Sara
    await approve_and_send(databases, deps, deal_id, SARA)
    cr = await raise_delivery(databases, deps, deal_id)
    auth.as_(Role.SALES, SALES_ID)
    mine = [i for i in (await client.get("/v1/inbox")).json() if i["deal_id"] == str(deal_id)]
    assert {i["kind"] for i in mine} == {"change_request"} and mine[0]["ref_id"] == str(cr.id)
    assert mine[0]["due_at"] is not None and mine[0]["overdue"] is False
    other = await sara_deal(databases, deps)  # a second deal waiting for Sara's approval
    kinds = {i["kind"] for i in (await client.get("/v1/inbox")).json() if i["deal_id"] == str(other)}
    assert kinds == {"approval"}
    auth.as_(Role.SALES, SALES2_ID)  # Ravi does not see Sara's work
    assert all(i["deal_id"] not in (str(deal_id), str(other)) for i in (await client.get("/v1/inbox")).json())
    auth.as_(Role.MANAGER, MGR_ID)  # the manager sees everything
    assert {str(deal_id), str(other)} <= {i["deal_id"] for i in (await client.get("/v1/inbox")).json()}


@pytest.mark.db
async def test_inbox_shows_buyer_messages_on_handed_over_deals(
    api: tuple[httpx.AsyncClient, Auth], databases: Databases, deps: CrmDeps
) -> None:
    client, auth = api
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await sara_deal(databases, deps)
    await approve_and_send(databases, deps, deal_id, SARA)
    await gw.mark_agreed(deal_id, AiRole.DEAL_CLOSER)
    await approve_and_send(databases, deps, deal_id, SARA)  # final -> handover
    async with databases.transaction(CRM) as s:
        s.add(
            m.TimelineEvent(deal_id=deal_id, kind="message_in", summary="How do I pay?", actor_type="buyer")
        )
    auth.as_(Role.SALES, SALES_ID)
    items = [i for i in (await client.get("/v1/inbox")).json() if i["deal_id"] == str(deal_id)]
    assert [(i["kind"], i["title"]) for i in items] == [("buyer_message", "How do I pay?")]


# ---------------------------------------------------------------- price list (§10, §20.7)
@pytest.mark.db
async def test_price_tier_crud_is_manager_only_and_audited(
    api: tuple[httpx.AsyncClient, Auth], databases: Databases
) -> None:
    client, auth = api
    url = f"/v1/products/{LIST_PRODUCT_ID}/price-tiers"
    auth.as_(Role.SALES, SALES_ID)
    assert len((await client.get(url)).json()) >= 3  # salespersons can read the price list
    body = {
        "min_qty": "1000",
        "max_qty": "4999",
        "list_price": "0.40",
        "min_price": "0.36",
        "valid_from": "2026-01-01T00:00:00Z",
    }
    assert (await client.post(url, json=body)).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID, mfa=False)
    assert (await client.post(url, json=body)).status_code == 403  # prices need MFA
    auth.as_(Role.MANAGER, MGR_ID)
    created = (await client.post(url, json=body)).json()
    overlap = {**body, "min_qty": "4000", "max_qty": "6000"}
    assert (await client.post(url, json=overlap)).status_code == 422
    r = await client.patch(f"/v1/price-tiers/{created['id']}", json={"min_price": "0.37"})
    assert r.status_code == 200 and D(r.json()["min_price"]) == D("0.37")
    assert (
        await client.patch(f"/v1/price-tiers/{created['id']}", json={"min_price": "0.50"})
    ).status_code == 422
    async with databases.transaction(CRM) as s:
        audit = (
            await s.execute(
                select(m.AuditEvent).where(
                    m.AuditEvent.entity_id == created["id"], m.AuditEvent.action == "price_tier.updated"
                )
            )
        ).scalar_one()
        assert (D(audit.before["min_price"]), D(audit.after["min_price"]), audit.actor_id) == (
            D("0.36"),
            D("0.37"),
            str(MGR_ID),
        )
    assert (await client.delete(f"/v1/price-tiers/{created['id']}")).status_code == 204


@pytest.mark.db
async def test_excel_export_and_import(api: tuple[httpx.AsyncClient, Auth], databases: Databases) -> None:
    client, auth = api
    auth.as_(Role.MANAGER, MGR_ID)
    exported = await client.get("/v1/price-list/export")
    sheet = load_workbook(io.BytesIO(exported.content)).active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0] == ("sku", "min_qty", "max_qty", "list_price", "min_price", "currency")
    assert ("ALC-750-L", 50000, None, "0.250000", "0.230000", "AED") in rows

    def xlsx(data: list[tuple[object, ...]]) -> bytes:
        wb = Workbook()
        ws = wb.active
        ws.append(list(rows[0]))
        for r in data:
            ws.append(list(r))
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    bad = await client.post(
        "/v1/price-list/import",
        content=xlsx(
            [("ALC-750-L", 5000, None, "0.30", "0.35", "AED"), ("UNKNOWN", 1, None, "1", None, "AED")]
        ),
    )
    assert bad.status_code == 200 and bad.json()["saved"] == 0
    assert {(e["row"], e["field"]) for e in bad.json()["errors"]} == {(2, "min_price"), (3, "sku")}
    async with databases.transaction(CRM) as s:
        before = len(list((await s.execute(select(m.ProductPriceTier.id))).scalars()))
    good = await client.post(
        "/v1/price-list/import",
        content=xlsx(
            [("ALU-750", 1000, 9999, "0.40", "0.36", "AED"), ("ALU-750", 10000, None, "0.35", None, "AED")]
        ),
    )
    assert good.json() == {"saved": 2, "errors": []}
    async with databases.transaction(CRM) as s:
        assert len(list((await s.execute(select(m.ProductPriceTier.id))).scalars())) == before + 2
    auth.as_(Role.SALES, SALES_ID)
    assert (await client.post("/v1/price-list/import", content=b"x")).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID)
    assert (await client.post("/v1/price-list/import", content=b"not a workbook")).status_code == 422


# ---------------------------------------------------------------- stock (§15.5) and settings (§20.11)
@pytest.mark.db
async def test_stock_table_is_set_by_the_manager_and_read_by_sales(
    api: tuple[httpx.AsyncClient, Auth],
) -> None:
    client, auth = api
    body = {"product_id": str(LIST_PRODUCT_ID), "quantity": "60000", "unit": "pcs"}  # §23 scenario 4
    auth.as_(Role.SALES, SALES_ID)
    assert (await client.put("/v1/stock", json=body)).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID)
    assert (await client.put("/v1/stock", json=body)).json()["quantity"] == "60000.000"
    auth.as_(Role.SALES, SALES_ID)
    row = next(x for x in (await client.get("/v1/stock")).json() if x["product_id"] == str(LIST_PRODUCT_ID))
    assert (row["item_type"], row["quantity"]) == ("finished", "60000.000")


@pytest.mark.db
async def test_new_settings_are_editable_by_the_manager_and_validated(
    api: tuple[httpx.AsyncClient, Auth], databases: Databases
) -> None:
    client, auth = api
    auth.as_(Role.MANAGER, MGR_ID)
    keys = {x["key"] for x in (await client.get("/v1/settings")).json()}
    assert {
        "pricing.mode",
        "negotiation.max_steps",
        "change_request.sla_working_hours",
        "assignment.rules",
        "approval.reminder_minutes",
        "quote.currency_by_country",
        "features.scraping",
    } <= keys
    assert (await client.put("/v1/settings/change_request.sla_working_hours", json={"value": 3})).json()[
        "value"
    ] == 3
    assert (
        await client.put("/v1/settings/approval.reminder_minutes", json={"value": [60, 30, 120]})
    ).status_code == 422
    assert (
        await client.put("/v1/settings/assignment.rules", json={"value": ["language", "zodiac"]})
    ).status_code == 422
    assert (await client.put("/v1/settings/pricing.mode", json={"value": "guess"})).status_code == 422
    await set_setting(databases, "change_request.sla_working_hours", 4)
    auth.as_(Role.SALES, SALES_ID)
    assert (await client.get("/v1/settings")).status_code == 403
