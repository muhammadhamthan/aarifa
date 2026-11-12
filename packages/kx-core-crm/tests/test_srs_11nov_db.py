"""11 Nov SRS over HTTP: §3 list visibility, §7A.1 reopen, §14 personal contact, §15 optional details."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import MGR_ID, SALES2_ID, SALES_ID
from sqlalchemy import select
from test_api_db import Auth
from test_api_db import api as api  # noqa: PLC0414 - re-export the `api` fixture
from test_approvals_db import sara_deal

from kx.core.auth import Role
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("price_list")]


async def test_a_salesperson_lists_only_their_own_deals(
    api: tuple[httpx.AsyncClient, Auth], databases: Any, deps: CrmDeps
) -> None:
    """§3: "the salesperson sees their own; the manager and MD see all" — on the board and the timeline."""
    client, auth = api
    deal_id = await sara_deal(databases, deps)
    auth.as_(Role.SALES, SALES2_ID)
    ids = {d["id"] for d in (await client.get("/v1/deals", params={"limit": 100})).json()["items"]}
    assert str(deal_id) not in ids
    assert (await client.get(f"/v1/deals/{deal_id}/timeline")).status_code == 403
    assert (await client.get(f"/v1/deals/{deal_id}/quotes")).status_code == 403
    auth.as_(Role.SALES, SALES_ID)
    mine = (await client.get("/v1/deals", params={"limit": 100})).json()["items"]
    assert str(deal_id) in {d["id"] for d in mine}
    assert (await client.get(f"/v1/deals/{deal_id}/timeline")).status_code == 200
    auth.as_(Role.MANAGER, MGR_ID)
    board = (await client.get("/v1/deals", params={"limit": 100, "country": "AE"})).json()["items"]
    assert str(deal_id) in {d["id"] for d in board}


async def test_reopen_needs_the_manager_with_mfa(
    api: tuple[httpx.AsyncClient, Auth], databases: Any, deps: CrmDeps
) -> None:
    client, auth = api
    deal_id = await sara_deal(databases, deps)
    auth.as_(Role.SALES, SALES_ID)
    version = (await client.get(f"/v1/deals/{deal_id}")).json()["version"]
    lost = await client.post(
        f"/v1/deals/{deal_id}/stage",
        json={"to": "LOST", "expected_version": version, "lost_reason": "opted_out"},
    )
    assert lost.status_code == 200 and lost.json()["lost_reason"] == "opted_out"
    assert (await client.post(f"/v1/deals/{deal_id}/reopen", json={"reason": "x"})).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID, mfa=False)
    assert (await client.post(f"/v1/deals/{deal_id}/reopen", json={"reason": "x"})).status_code == 403
    auth.as_(Role.MANAGER, MGR_ID)
    reopened = await client.post(f"/v1/deals/{deal_id}/reopen", json={"reason": "buyer came back"})
    assert reopened.status_code == 200
    assert reopened.json()["stage"] != "LOST" and reopened.json()["ai_enabled"] is False


async def test_personal_contact_shared_is_recorded(
    api: tuple[httpx.AsyncClient, Auth], databases: Any, deps: CrmDeps
) -> None:
    client, auth = api
    deal_id = await sara_deal(databases, deps)
    auth.as_(Role.SALES, SALES2_ID)
    assert (await client.post(f"/v1/deals/{deal_id}/personal-contact-shared")).status_code == 403
    auth.as_(Role.SALES, SALES_ID)
    r = await client.post(f"/v1/deals/{deal_id}/personal-contact-shared")
    assert r.status_code == 200 and r.json()["personal_contact_shared_at"] is not None
    kinds = [t["summary"] for t in (await client.get(f"/v1/deals/{deal_id}/timeline")).json()["items"]]
    assert any("personal contact" in k for k in kinds)


async def test_the_ai_stop_is_handed_to_automation_with_every_address(databases: Any, deps: CrmDeps) -> None:
    """§8.1 step 6: the AI read "please stop sending me these" -> recorded in the CRM, then automation gets
    the person and all their addresses (origin crm) to suppress, cancel and confirm."""
    from test_flow_db import outbox

    from kx.core import events as ev
    from kx.core.contracts import AiRole
    from kx.crm.gateway import CrmGatewayImpl

    deal_id = await sara_deal(databases, deps)
    gw = CrmGatewayImpl(databases, deps)
    await gw.record_opt_out(
        deal_id,
        scope="all",
        exact_words="please stop sending me these",
        reason=None,
        decided_by="ai:test:stop-v1",
        actor=AiRole.COMMUNICATION,
    )
    (stop,) = await outbox(databases, ev.CONSENT_OPTED_OUT, deal_id)
    assert stop["origin"] == "crm" and stop["source"] == "ai" and stop["address_hashes"]
    ctx = await gw.deal_context(deal_id)
    assert ctx.ai_enabled is False  # the AI stops at once


async def test_an_unclear_stop_pauses_and_asks_or_goes_to_the_salesperson(
    databases: Any, deps: CrmDeps
) -> None:
    from test_flow_db import outbox

    from kx.core import events as ev
    from kx.core.contracts import AiRole
    from kx.crm.gateway import CrmGatewayImpl

    deal_id = await sara_deal(databases, deps)
    gw = CrmGatewayImpl(databases, deps)
    await gw.flag_unclear_stop(deal_id, exact_words="ok stop it there", live=True, actor=AiRole.COMMUNICATION)
    await gw.flag_unclear_stop(deal_id, exact_words="stop it", live=False, actor=AiRole.COMMUNICATION)
    live, cold = await outbox(databases, ev.CONSENT_STOP_UNCLEAR, deal_id)
    assert live["live"] is True and live["answer_by"] is not None  # 24 h (optout.unclear_answer_hours)
    assert cold["live"] is False and cold["answer_by"] is None
    async with databases.transaction(LogicalDb.CRM) as s:
        kinds = (await s.execute(select(m.Task.kind).where(m.Task.deal_id == deal_id))).scalars().all()
    assert list(kinds).count("optout_review") == 1  # only the cold one goes to the salesperson
