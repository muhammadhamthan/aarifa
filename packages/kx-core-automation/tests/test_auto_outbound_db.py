"""OutboundService against Postgres: every refusal path, idempotency, scheduling, delivery."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from automation_fakes import (
    Clock,
    FakeEmail,
    FakeGrants,
    FakeKill,
    FakeLimiter,
    FakeMedia,
    FakeScheduler,
    FakeWhatsApp,
)
from sqlalchemy import select, text

from kx.automation.channels import ChannelError
from kx.automation.models import OutboundMessage
from kx.automation.outbound import OutboundService, TransientDeliveryError, content_sha256
from kx.automation.settings import AutomationSettings
from kx.core.contracts import OutboundKind, OutboundRequest
from kx.core.errors import RuleViolation
from kx.core.identity import recipient_hash
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db
BUYER = "+971501234567"


@dataclass
class Env:
    service: OutboundService
    db: Any
    grants: FakeGrants
    kill: FakeKill
    limiter: FakeLimiter
    scheduler: FakeScheduler
    media: FakeMedia
    whatsapp: FakeWhatsApp
    email: FakeEmail
    clock: Clock

    async def rows(self) -> list[OutboundMessage]:
        async with self.db.transaction(LogicalDb.AUTOMATION) as s:
            return list(
                (await s.execute(select(OutboundMessage).order_by(OutboundMessage.created_at))).scalars()
            )

    async def sql(self, statement: str, **params: Any) -> list[Any]:
        async with self.db.transaction(LogicalDb.AUTOMATION) as s:
            result = await s.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []


@pytest.fixture
async def env(auto_db: Any, clock: Clock) -> Env:
    parts = dict(
        grants=FakeGrants(),
        kill=FakeKill(),
        limiter=FakeLimiter(),
        scheduler=FakeScheduler(),
        media=FakeMedia(),
        whatsapp=FakeWhatsApp(),
        email=FakeEmail(),
    )
    service = OutboundService(
        databases=auto_db,
        settings=AutomationSettings(),
        grants=parts["grants"],
        kill_switch=parts["kill"],
        limiter=parts["limiter"],
        scheduler=parts["scheduler"],
        media=parts["media"],
        whatsapp=parts["whatsapp"],
        email=parts["email"],
        clock=clock,
    )
    return Env(service=service, db=auto_db, clock=clock, **parts)  # type: ignore[arg-type]


async def open_session(env: Env, address: str = BUYER) -> None:
    """The buyer messaged us 1 h ago: the WhatsApp 24 h window is open."""
    await env.sql(
        "INSERT INTO threads (channel_account_id, external_key, last_inbound_at) "
        "SELECT id, :a, :t FROM channel_accounts WHERE external_account_id = 'PNID1'",
        a=address,
        t=env.clock.now - timedelta(hours=1),
    )


def req(kind: OutboundKind = OutboundKind.REPLY, key: str | None = None, **kw: Any) -> OutboundRequest:
    base: dict[str, Any] = {
        "channel": "whatsapp",
        "to_address": "050 123 4567",
        "kind": kind,
        "body": "Hello",
        "requested_by": "ai:communication",
        "idempotency_key": key or str(uuid.uuid4()),
    }
    return OutboundRequest(**(base | kw))


def priced(
    env: Env, body: str = "Quote Q-1045 total AED 11,390.20", pdf: bytes | None = None, **kw: Any
) -> OutboundRequest:
    if pdf is not None:
        env.media.objects["quotes/Q-1045.pdf"] = pdf
        kw["media_s3_key"] = "quotes/Q-1045.pdf"
    return req(
        OutboundKind.FINAL_QUOTE,
        body=body,
        grant_id=uuid.uuid4(),
        content_sha256=content_sha256(body, pdf),
        **kw,
    )


# ---------------------------------------------------------------- refusals
async def test_priced_without_grant_is_refused_and_audited(env: Env) -> None:
    await open_session(env)
    with pytest.raises(RuleViolation) as info:
        await env.service.send(req(OutboundKind.QUOTE, body="AED 100"))
    assert info.value.extra["reason"] == "no_grant"
    assert await env.rows() == [] and env.scheduler.calls == [] and env.grants.calls == []


async def test_priced_sha_mismatch_refused_without_consuming(env: Env) -> None:
    await open_session(env)
    request = priced(env).model_copy(update={"body": "Quote Q-1045 total AED 9,000.00"})
    with pytest.raises(RuleViolation) as info:
        await env.service.send(request)
    assert info.value.extra["reason"] == "content_mismatch" and env.grants.calls == []
    (row,) = await env.rows()
    assert (row.status, row.block_reason) == ("blocked", "content_mismatch")
    events = await env.sql("SELECT payload->>'status', payload->>'error' FROM outbox_events")
    assert events == [("failed", "blocked:content_mismatch")]


async def test_grant_consumer_false_refuses(env: Env) -> None:
    await open_session(env)
    env.grants.result = False
    with pytest.raises(RuleViolation) as info:
        await env.service.send(priced(env))
    assert info.value.extra["reason"] == "grant_refused"
    (row,) = await env.rows()
    assert row.status == "blocked" and env.scheduler.calls == []


async def test_grant_consumed_with_hash_message_id_and_recipient(env: Env) -> None:
    await open_session(env)
    pdf = b"%PDF-1.7 final quote"
    request = priced(env, pdf=pdf)
    message_id = await env.service.send(request)
    ((grant, sha, mid, rhash),) = env.grants.calls
    assert (grant, sha, mid) == (request.grant_id, content_sha256(request.body or "", pdf), message_id)
    assert rhash == recipient_hash("whatsapp", BUYER)
    (row,) = await env.rows()
    assert (row.status, row.grant_ref, row.to_address) == ("queued", request.grant_id, BUYER)


async def test_reused_grant_refused_even_when_row_cannot_be_stored(env: Env) -> None:
    await open_session(env)
    first = priced(env)
    await env.service.send(first)
    env.grants.result = False  # the CRM says: already consumed by the first message
    with pytest.raises(RuleViolation):
        await env.service.send(first.model_copy(update={"idempotency_key": "another"}))
    assert len(await env.rows()) == 1
    assert (
        len(await env.sql("SELECT 1 FROM outbox_events WHERE payload->>'error' = 'blocked:grant_refused'"))
        == 1
    )


async def test_idempotent_replay_returns_same_id(env: Env) -> None:
    await open_session(env)
    first = await env.service.send(req(key="reply:abc"))
    again = await env.service.send(req(key="reply:abc", body="different"))
    assert first == again and len(await env.rows()) == 1 and len(env.scheduler.named("delivery")) == 1


async def test_kill_switch_refuses_without_recording(env: Env) -> None:
    env.kill.off.add("all")
    with pytest.raises(RuleViolation) as info:
        await env.service.send(req())
    assert info.value.extra["reason"] == "kill_switch" and await env.rows() == []


async def test_rate_limited_refusal_is_transient(env: Env) -> None:
    env.limiter.allow = False
    with pytest.raises(RuleViolation) as info:
        await env.service.send(req())
    assert info.value.extra["reason"] == "rate_limited" and info.value.extra["retry_after_seconds"] == 30.0
    assert env.limiter.taken == [("recipient", recipient_hash("whatsapp", BUYER))]


async def test_suppressed_recipient_blocked(env: Env) -> None:
    await open_session(env)
    await env.sql(
        "INSERT INTO suppressions VALUES ('whatsapp', :h, 'stop_keyword', now())",
        h=recipient_hash("whatsapp", BUYER),
    )
    with pytest.raises(RuleViolation) as info:
        await env.service.send(req())
    assert info.value.extra["reason"] == "opt_out"


async def test_whatsapp_free_text_needs_open_window_but_template_does_not(env: Env) -> None:
    with pytest.raises(RuleViolation) as info:
        await env.service.send(req())
    assert info.value.extra["reason"] == "window_closed"
    await env.service.send(req(OutboundKind.NOTIFICATION, body=None, template_name="followup_1"))
    with pytest.raises(RuleViolation):
        await env.service.send(req(OutboundKind.NOTIFICATION, body=None, template_name="draft_one"))


async def test_priced_template_hash_covers_rendered_params(env: Env) -> None:
    rendered = "Your quote Q-1045 total AED 11,390.20"
    request = req(
        OutboundKind.QUOTE,
        body=None,
        template_name="quote_ready",
        template_params=("Q-1045", "AED 11,390.20"),
        grant_id=uuid.uuid4(),
        content_sha256=content_sha256(rendered, None),
    )
    await env.service.send(request)
    (row,) = await env.rows()
    assert row.body == rendered
    assert env.scheduler.named("delivery")[0]["template_params"] == ("Q-1045", "AED 11,390.20")
    with pytest.raises(RuleViolation):  # same hash, different amount in the params
        await env.service.send(
            request.model_copy(
                update={
                    "template_params": ("Q-1045", "AED 9,000.00"),
                    "idempotency_key": "k2",
                    "grant_id": uuid.uuid4(),
                }
            )
        )


# ---------------------------------------------------------------- scheduling
async def test_followup_weekly_cap_and_window(env: Env) -> None:
    env.clock.now = env.clock.now.replace(hour=16)  # 20:00 Dubai: outside 09-18
    followup = {"body": None, "template_name": "followup_1", "buyer_timezone": "Asia/Dubai"}
    ids = [await env.service.send(req(OutboundKind.FOLLOWUP, **followup)) for _ in range(3)]
    rows = {r.id: r for r in await env.rows()}
    slots = [rows[i].not_before for i in ids]
    assert all(s is not None for s in slots)
    local = [s.astimezone(env.clock.now.tzinfo).isoformat() for s in slots if s]
    assert local[0] == local[1] == "2026-11-04T05:00:00+00:00"  # next 09:00 Dubai
    assert local[2] == "2026-11-11T05:00:00+00:00"  # third waits a week
    assert all(
        c["lock"] is None for c in env.scheduler.named("delivery")
    )  # future jobs never hold the thread


async def test_staff_alert_waits_for_alert_window(env: Env) -> None:
    env.clock.now = env.clock.now.replace(hour=19)  # 23:00 Dubai
    await env.service.send(
        req(
            OutboundKind.APPROVAL_ALERT,
            body=None,
            template_name="approval_escalation",
            template_params=("Q-1", "AED 1", "Sara"),
        )
    )
    (row,) = await env.rows()
    assert row.not_before is not None and row.not_before.isoformat() == "2026-11-04T04:00:00+00:00"
    account = await env.sql("SELECT purpose FROM channel_accounts WHERE id = :i", i=row.channel_account_id)
    assert account == [("internal_alerts",)]


async def test_immediate_reply_locks_thread(env: Env) -> None:
    await open_session(env)
    await env.service.send(req())
    (call,) = env.scheduler.named("delivery")
    assert call["lock"].startswith("thread:") and call["at"] is None


# ---------------------------------------------------------------- delivery
async def test_deliver_sends_once_and_emits_status(env: Env) -> None:
    await open_session(env)
    message_id = await env.service.send(req(body="Your summary"))
    await env.service.deliver(message_id)
    await env.service.deliver(message_id)  # duplicate job: no second send
    assert env.whatsapp.sent == [("text", (BUYER, "Your summary"))]
    (row,) = await env.rows()
    assert (row.status, row.provider_message_id) == ("sent", "wamid.1") and row.sent_at is not None
    statuses = await env.sql(
        "SELECT payload->>'status' FROM outbox_events WHERE event_type = 'message.status.v1'"
    )
    assert statuses == [("sent",)]


async def test_deliver_pdf_quote_by_whatsapp_document(env: Env) -> None:
    await open_session(env)
    message_id = await env.service.send(priced(env, pdf=b"%PDF"))
    await env.service.deliver(message_id)
    kind, (to, doc, caption) = env.whatsapp.sent[0]
    assert (kind, to, doc.media_id, doc.filename) == ("document", BUYER, "media-1", "Q-1045.pdf")
    assert caption == "Quote Q-1045 total AED 11,390.20"


async def test_deliver_email_with_attachment(env: Env) -> None:
    body = "Please find your quote."
    env.media.objects["q.pdf"] = b"%PDF"
    message_id = await env.service.send(
        req(
            OutboundKind.FINAL_QUOTE,
            channel="email",
            to_address="Buyer@Example.com",
            body=body,
            subject="Quote Q-1",
            media_s3_key="q.pdf",
            grant_id=uuid.uuid4(),
            content_sha256=content_sha256(body, b"%PDF"),
        )
    )
    await env.service.deliver(message_id)
    (mail,) = env.email.sent
    assert mail["to"] == "buyer@example.com" and mail["attachment"].data == b"%PDF"


async def test_transient_failure_requeues_then_final_attempt_fails(env: Env) -> None:
    await open_session(env)
    message_id = await env.service.send(req())
    env.whatsapp.error = ChannelError("meta_cloud", "HTTP 503", retryable=True, status=503)
    with pytest.raises(TransientDeliveryError):
        await env.service.deliver(message_id)
    assert (await env.rows())[0].status == "queued"
    await env.service.deliver(message_id, final_attempt=True)
    assert (await env.rows())[0].status == "failed"


async def test_kill_switch_at_delivery_cancels(env: Env) -> None:
    await open_session(env)
    message_id = await env.service.send(req())
    env.kill.off.add("whatsapp")
    await env.service.deliver(message_id)
    (row,) = await env.rows()
    assert (row.status, row.block_reason) == ("cancelled", "kill_switch") and env.whatsapp.sent == []


async def test_window_rechecked_at_delivery(env: Env) -> None:
    await open_session(env)
    message_id = await env.service.send(req())
    env.clock.now += timedelta(hours=30)
    await env.service.deliver(message_id)
    assert (await env.rows())[0].block_reason == "window_closed"


async def test_opt_out_confirmation_is_the_one_message_after_stop(env: Env) -> None:
    """§17 "Buyer opts out -> Confirmation": everything else stays blocked by the suppression."""
    await open_session(env)
    await env.sql(
        "INSERT INTO message_templates (channel, name, language, category, provider_status, body) "
        "VALUES ('whatsapp', 'opt_out_confirmation', 'en', 'utility', 'approved', 'You will not receive more messages.')"
    )
    await env.sql(
        "INSERT INTO suppressions VALUES ('whatsapp', :h, 'stop_keyword', now())",
        h=recipient_hash("whatsapp", BUYER),
    )
    with pytest.raises(RuleViolation) as info:
        await env.service.send(req())
    assert info.value.extra["reason"] == "opt_out"
    confirm = req(
        OutboundKind.NOTIFICATION, key="opt-out:abc", body=None, template_name="opt_out_confirmation"
    )
    assert await env.service.send(confirm)
    with pytest.raises(RuleViolation):  # the template alone does not open the door
        await env.service.send(
            req(OutboundKind.NOTIFICATION, body=None, template_name="opt_out_confirmation")
        )
