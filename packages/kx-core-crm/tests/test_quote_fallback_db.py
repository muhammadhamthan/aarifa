"""D32 (11 Nov plan WP25): a WhatsApp quote not delivered in 10 minutes goes by email with the same approved
content and its own email grant; without an email the salesperson is told to call."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from test_approvals_db import sara_deal
from test_flow_db import SARA, approve_and_send, outbox

from kx.core import events as ev
from kx.core.contracts import AiRole, ContactPatch
from kx.core.db import Databases
from kx.core.event_payloads import QuoteNotDelivered
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import CrmGatewayImpl
from kx.crm.services import issuing

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("price_list")]


def not_delivered(approved: dict[str, Any]) -> QuoteNotDelivered:
    return QuoteNotDelivered(
        message_ref=uuid.uuid4(),
        grant_ref=uuid.UUID(approved["grant_ref"]),
        deal_ref=uuid.UUID(approved["deal_ref"]),
        contact_ref=uuid.UUID(approved["contact_ref"]),
        kind="quote",
        status="sent",
        body=approved["body"],
        subject=approved["subject"],
        pdf_s3_key=approved["pdf_s3_key"],
        content_sha256=approved["content_sha256"],
    )


async def resend(dbs: Databases, deps: CrmDeps, p: QuoteNotDelivered) -> bool:
    async with dbs.transaction(LogicalDb.CRM) as s:
        return await issuing.resend_by_email(s, deps, p)


async def test_same_quote_goes_by_email_with_its_own_grant(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await sara_deal(databases, deps)
    approved = await approve_and_send(databases, deps, deal_id, SARA)
    assert approved["channel"] == "whatsapp"
    await CrmGatewayImpl(databases, deps).set_contact(
        deal_id, ContactPatch(email="buyer@example.com"), AiRole.CONTACT_CAPTURE
    )
    assert await resend(databases, deps, not_delivered(approved))
    (req,) = await outbox(databases, ev.QUOTE_RESEND_REQUESTED, deal_id)
    assert (req["channel"], req["to_address"]) == ("email", "buyer@example.com")
    assert req["content_sha256"] == approved["content_sha256"] and req["body"] == approved["body"]
    assert req["grant_ref"] != approved["grant_ref"]
    async with databases.transaction(LogicalDb.CRM) as s:
        grant = await s.get(m.QuoteSendGrant, uuid.UUID(req["grant_ref"]))
    assert (
        grant is not None and grant.channel == "email" and grant.content_sha256 == approved["content_sha256"]
    )


async def test_without_an_email_the_salesperson_is_told_to_call(databases: Databases, deps: CrmDeps) -> None:
    deal_id = await sara_deal(databases, deps)
    approved = await approve_and_send(databases, deps, deal_id, SARA)
    assert not await resend(databases, deps, not_delivered(approved))
    assert await outbox(databases, ev.QUOTE_RESEND_REQUESTED, deal_id) == []
    async with databases.transaction(LogicalDb.CRM) as s:
        kinds = (await s.execute(select(m.Task.kind).where(m.Task.deal_id == deal_id))).scalars().all()
    assert "phone_unreached" in kinds
