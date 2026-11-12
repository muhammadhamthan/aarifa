"""WP14 (§8.1, D18) on Postgres: person-level suppression, the 24-hour no-answer timer, the unsubscribe link."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from automation_fakes import Clock
from fastapi import FastAPI
from sqlalchemy import text
from test_auto_outbound_db import BUYER, Env, open_session, req
from test_auto_outbound_db import env as env  # noqa: PLC0414 - re-export the fixture

from kx.automation import optout
from kx.automation.channels.email import build_mime
from kx.automation.handlers import stop_after_no_answer
from kx.automation.settings import AutomationSettings
from kx.automation.unsubscribe import PATH, UnsubscribeDeps, UnsubscribeLinks, build_router, read_token
from kx.core.contracts import OutboundKind
from kx.core.errors import RuleViolation
from kx.core.event_payloads import StopUnclear
from kx.core.identity import recipient_hash
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db
SECRET = "test-service-secret"


async def test_a_stop_follows_the_person_to_another_address(env: Env) -> None:
    """Step 2: one suppression list; a stop on the person blocks whatever address they are reached on."""
    await open_session(env)
    contact = uuid.uuid4()
    async with env.db.transaction(LogicalDb.AUTOMATION) as s:
        await optout.apply_stop(
            s,
            optout.StopRecord(
                scope="all",
                source="ai",
                channel="crm",
                address_hashes=(),
                contact_ref=contact,
                deal_ref=None,
                thread_ref=None,
                words="please stop sending me these",
                reason=None,
                decided_by="ai:test",
                language="en",
                at=env.clock.now,
            ),
            announce=False,
        )
    with pytest.raises(RuleViolation) as info:
        await env.service.send(req(contact_ref=contact))
    assert info.value.extra["reason"] == "opt_out"
    assert await env.service.send(req(key="other-person"))  # someone else is not affected


async def test_queued_messages_are_cancelled_but_not_the_confirmation(env: Env) -> None:
    """Step 2: "All queued follow-ups, reminders and templates are cancelled"."""
    await open_session(env)
    queued = await env.service.send(req(key="queued-1"))
    async with env.db.transaction(LogicalDb.AUTOMATION) as s:
        await optout.apply_stop(
            s,
            optout.StopRecord(
                scope="all",
                source="rule",
                channel="whatsapp",
                address_hashes=(recipient_hash("whatsapp", BUYER),),
                contact_ref=None,
                deal_ref=None,
                thread_ref=None,
                words="STOP",
                reason=None,
                decided_by="word:stop",
                language="en",
                at=env.clock.now,
            ),
        )
        status = (
            await s.execute(
                text("SELECT status, block_reason FROM outbound_messages WHERE id = :i"), {"i": queued}
            )
        ).one()
        stopped = (await s.execute(text("SELECT stopped FROM opt_out_log"))).scalar_one()
    assert tuple(status) == ("cancelled", "opt_out") and stopped["queued_messages_cancelled"] == 1
    confirm = req(OutboundKind.OPT_OUT_CONFIRMATION, key=f"opt-out:{recipient_hash('whatsapp', BUYER)}")
    assert await env.service.send(confirm)


async def test_no_answer_within_24_hours_is_an_opt_out_and_any_reply_answers(
    auto_db: Any, clock: Clock
) -> None:
    """Step 6: the AI asked "should we stop messaging you?"; no answer by `answer_by` = opt-out."""
    deal, contact = uuid.uuid4(), uuid.uuid4()
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        account = (
            await s.execute(text("SELECT id FROM channel_accounts WHERE external_account_id = 'PNID1'"))
        ).scalar_one()
        thread = (
            await s.execute(
                text(
                    "INSERT INTO threads (channel_account_id, external_key, contact_ref, deal_ref, last_inbound_at) "
                    "VALUES (:a, :k, :c, :d, :t) RETURNING id"
                ),
                {"a": account, "k": BUYER, "c": contact, "d": deal, "t": clock.now},
            )
        ).scalar_one()
    unclear = StopUnclear(
        contact_ref=contact,
        deal_ref=deal,
        live=True,
        exact_words="hmm stop it there",
        asked_at=clock.now,
        answer_by=clock.now + timedelta(hours=24),
    )
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:  # the buyer answered: nothing stops
        await s.execute(
            text(
                "INSERT INTO inbound_messages (thread_id, provider, provider_message_id, kind, text, raw, received_at) "
                "VALUES (:t, 'meta_cloud', 'wamid.ANS', 'text', 'no, carry on', '{}', :at)"
            ),
            {"t": thread, "at": clock.now + timedelta(minutes=3)},
        )
        assert await stop_after_no_answer(s, unclear, clock.now + timedelta(hours=24)) is None
    silent = unclear.model_copy(update={"asked_at": clock.now + timedelta(hours=1)})
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:  # no message after this question
        log_ref = await stop_after_no_answer(s, silent, clock.now + timedelta(hours=25))
        row = (
            await s.execute(
                text("SELECT source, scope, decided_by FROM opt_out_log WHERE id = :i"), {"i": log_ref}
            )
        ).one()
    assert tuple(row) == ("no_answer", "all", "rule:no_answer_24h")


def test_buyer_emails_carry_one_click_unsubscribe_headers() -> None:
    """Step 10 / RFC 8058: outside the body, so a priced email's grant hash is untouched."""
    link = UnsubscribeLinks(SECRET, "https://api.example.ae")("Buyer@Example.com")
    msg = build_mime(
        from_address="sales@example.ae",
        to_address="buyer@example.com",
        subject="Q",
        body="b",
        list_unsubscribe=link,
    )
    assert msg["List-Unsubscribe"] == f"<{link}>"
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    token = link.split("t=", 1)[1]
    assert read_token(SECRET, token) == recipient_hash("email", "buyer@example.com")
    assert read_token("another-secret", token) is None


async def test_unsubscribe_link_get_changes_nothing_post_stops_everywhere(auto_db: Any, clock: Clock) -> None:
    app = FastAPI()
    app.include_router(
        build_router(UnsubscribeDeps(auto_db, AutomationSettings(company_name="Arifaa Pack"), SECRET, clock))
    )
    link = UnsubscribeLinks(SECRET, "http://t")("buyer@example.com")
    path = link.removeprefix("http://t")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        page = await client.get(path)  # a mail scanner fetching the link must not opt anyone out
        assert page.status_code == 200 and "Reason (optional)" in page.text
        async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
            assert (await s.execute(text("SELECT count(*) FROM suppressions"))).scalar_one() == 0
        done = await client.post(
            path,
            content=b"reason=too+many+emails",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert done.status_code == 200 and "unsubscribed" in done.text
        assert (await client.post(f"{PATH}?t=forged.abc")).status_code == 400
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        rows = (await s.execute(text("SELECT channel FROM suppressions"))).all()
        log = (await s.execute(text("SELECT source, volunteered_reason FROM opt_out_log"))).one()
        events = (await s.execute(text("SELECT event_type FROM outbox_events"))).all()
    assert rows == [("all",)] and tuple(log) == ("unsubscribe_link", "too many emails")
    assert [e[0] for e in events] == ["consent.opted_out.v1"]  # the CRM extends it to the person


async def test_requirement_summary_goes_as_a_whatsapp_template_with_the_pdf(
    auto_db: Any, clock: Clock
) -> None:
    """§8.2 step 2: the summary is the useful thing that proves the number (never a code); if WhatsApp refuses,
    a failed status hands the decision back to the CRM (email, else the salesperson)."""
    from test_auto_db import RecordingSender, build

    from kx.core import events as ev
    from kx.core.event_payloads import RequirementSummary, make_event

    summary = RequirementSummary(
        deal_ref=uuid.uuid4(),
        contact_ref=uuid.uuid4(),
        deal_number="D-2026-00001",
        pdf_s3_key="pack/summaries/D-1.pdf",
        caption="Hi Ahmed, here's the summary of what we discussed — 50,000 containers. Our team is preparing your quotation.",
        summary_text="50,000 containers",
        buyer_name="Ahmed",
        whatsapp=BUYER,
        language="en",
    )
    sender = RecordingSender()
    handlers, *_ = build(auto_db, clock, sender)
    assert await handlers.dispatch(make_event(ev.DEAL_REQUIREMENT_SUMMARY, "deal", summary.deal_ref, summary))
    (sent,) = sender.requests
    assert (sent.kind, sent.template_name, sent.template_params, sent.media_s3_key) == (
        OutboundKind.SUMMARY_PDF,
        "requirement_summary",
        ("Ahmed", "50,000 containers"),
        "pack/summaries/D-1.pdf",
    )
    refused = RecordingSender(
        RuleViolation("template 'requirement_summary' is not approved", reason="template")
    )
    handlers, *_ = build(auto_db, clock, refused)
    again = summary.model_copy(update={"deal_ref": uuid.uuid4()})
    assert await handlers.dispatch(make_event(ev.DEAL_REQUIREMENT_SUMMARY, "deal", again.deal_ref, again))
    async with auto_db.transaction(LogicalDb.AUTOMATION) as s:
        row = (
            await s.execute(
                text(
                    "SELECT payload->>'status', payload->>'kind', payload->>'error' FROM outbox_events "
                    "WHERE event_type = 'message.status.v1' AND payload->>'deal_ref' = :d"
                ),
                {"d": str(again.deal_ref)},
            )
        ).one()
    assert tuple(row) == ("failed", "summary_pdf", "undeliverable:template")
