"""WP15 (§8.2, §6.5, §17 row 1; D19): silent phone confirmation and the Requirement Summary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from typing import Any

import pytest
from conftest import SKU, new_phone
from sqlalchemy import select
from test_flow_db import outbox

from kx.core import events as ev
from kx.core.contracts import AiRole, ContactPatch, RequirementPatch
from kx.core.event_payloads import DealQualified, MessageStatus, make_event
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl
from kx.crm.jobs import HANDLERS, CrmRuntime, dispatch
from kx.crm.pdf import SummaryDocument, SummaryLine, summary_caption
from kx.crm.services.phone import next_status

CRM = LogicalDb.CRM


@pytest.mark.parametrize(
    ("current", "signal", "expected"),
    [
        ("unknown", "delivered", "reachable"),
        ("reachable", "read", "likely"),
        ("likely", "reply", "verified"),
        ("unknown", "failed", "not_reached"),
        ("not_reached", "delivered", "reachable"),  # a later delivery proves it after all
        ("verified", "delivered", None),  # forward only
        ("likely", "failed", None),  # a failure never downgrades a number that proved itself
        ("reachable", "read", "likely"),
    ],
)
def test_phone_status_moves_forward_only(current: str, signal: Any, expected: str | None) -> None:
    """§8.2 step 3 table; "no Read tick is not a failure"."""
    assert next_status(current, signal) == expected


def test_the_summary_caption_is_service_wording_without_prices() -> None:
    from kx.crm.pdf import Letterhead

    doc = SummaryDocument(
        reference="D-1",
        issued_on=datetime(2026, 11, 9, tzinfo=UTC).date(),
        language="en",
        buyer_name="Ahmed",
        buyer_company=None,
        lines=(SummaryLine("aluminium containers, 750 ml with lid", "ALC-750-L", D(50_000), "pcs"),),
        delivery_country="SA",
        delivery_city="Riyadh",
        needed_by=None,
        letterhead=Letterhead(),
    )
    caption = summary_caption(doc)
    assert caption == (
        "Hi Ahmed, here's the summary of what we discussed — 50,000 aluminium containers, 750 ml with lid, "
        "delivery to Riyadh. Our team is preparing your quotation."
    )
    assert "AED" not in caption and "code" not in caption.casefold() and "verify" not in caption.casefold()


async def _web_lead(databases: Any, deps: CrmDeps, *, email: str | None) -> uuid.UUID:
    """A website buyer: the number was typed, not proven (phone status unknown)."""
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await gw.resolve_deal("web_chat", None, uuid.uuid4())
    await gw.update_requirement(
        deal_id,
        RequirementPatch(product_sku=SKU, quantity=D(50_000), delivery_country="AE"),
        AiRole.LEAD_QUALIFICATION,
    )
    await gw.set_contact(
        deal_id, ContactPatch(name="Ahmed", phone=new_phone(), email=email), AiRole.CONTACT_CAPTURE
    )
    return deal_id


async def _contact(databases: Any, deal_id: uuid.UUID) -> m.Contact:
    async with databases.transaction(CRM) as s:
        deal = await s.get(m.Deal, deal_id)
        assert deal is not None
        contact = await s.get(m.Contact, deal.primary_contact_id)
        assert contact is not None
        return contact


async def _noop(_: object) -> None:
    return None


@pytest.mark.db
@pytest.mark.usefixtures("price_list")
async def test_qualified_deal_gets_a_summary_and_whatsapp_signals_confirm_the_number(
    databases: Any, deps: CrmDeps
) -> None:
    deal_id = await _web_lead(databases, deps, email=None)
    contact = await _contact(databases, deal_id)
    assert contact.phone_status == "unknown"
    rt = CrmRuntime(databases, deps, publish=_noop)
    await dispatch(
        rt,
        make_event(
            ev.DEAL_QUALIFIED, "deal", deal_id, DealQualified(deal_ref=deal_id, contact_ref=contact.id)
        ),
    )
    (summary,) = await outbox(databases, ev.DEAL_REQUIREMENT_SUMMARY, deal_id)
    assert summary["whatsapp"] and summary["pdf_s3_key"].endswith(".pdf") and "AED" not in summary["caption"]
    for status in ("delivered", "read"):
        await dispatch(
            rt,
            make_event(
                ev.MESSAGE_STATUS,
                "outbound_message",
                uuid.uuid4(),
                MessageStatus(
                    message_ref=uuid.uuid4(),
                    status=status,  # type: ignore[arg-type]
                    provider_message_id="wamid.S",
                    at=datetime.now(UTC),
                    contact_ref=contact.id,
                    deal_ref=deal_id,
                    channel="whatsapp",
                    kind="summary_pdf",
                ),
            ),
        )
    assert (await _contact(databases, deal_id)).phone_status == "likely"


@pytest.mark.db
@pytest.mark.usefixtures("price_list")
async def test_a_failed_summary_goes_by_email_else_the_salesperson_is_told(
    databases: Any, deps: CrmDeps
) -> None:
    rt = CrmRuntime(databases, deps, publish=_noop)
    for email in ("ahmed@example.com", None):
        deal_id = await _web_lead(databases, deps, email=email)
        contact = await _contact(databases, deal_id)
        failed = MessageStatus(
            message_ref=uuid.uuid4(),
            status="failed",
            provider_message_id=None,
            error="undeliverable:template",
            at=datetime.now(UTC),
            contact_ref=contact.id,
            deal_ref=deal_id,
            channel="whatsapp",
            kind="summary_pdf",
        )
        await dispatch(
            rt, make_event(ev.MESSAGE_STATUS, "x", uuid.uuid4(), failed), HANDLERS[ev.MESSAGE_STATUS]
        )
        assert (await _contact(databases, deal_id)).phone_status == "not_reached"
        summaries = await outbox(databases, ev.DEAL_REQUIREMENT_SUMMARY, deal_id)
        async with databases.transaction(CRM) as s:
            tasks = (await s.execute(select(m.Task.kind).where(m.Task.deal_id == deal_id))).scalars().all()
        if email:
            assert [x["email"] for x in summaries] == [email] and summaries[0]["whatsapp"] is None
        else:
            assert summaries == [] and "phone_unreached" in tasks


@pytest.mark.db
@pytest.mark.usefixtures("price_list")
async def test_a_blocked_send_says_nothing_about_the_number(databases: Any, deps: CrmDeps) -> None:
    deal_id = await _web_lead(databases, deps, email=None)
    contact = await _contact(databases, deal_id)
    blocked = MessageStatus(
        message_ref=uuid.uuid4(),
        status="failed",
        provider_message_id=None,
        error="blocked:window_closed",
        at=datetime.now(UTC),
        contact_ref=contact.id,
        deal_ref=deal_id,
        channel="whatsapp",
        kind="reply",
    )
    await dispatch(
        CrmRuntime(databases, deps, publish=_noop), make_event(ev.MESSAGE_STATUS, "x", uuid.uuid4(), blocked)
    )
    assert (await _contact(databases, deal_id)).phone_status == "unknown"
