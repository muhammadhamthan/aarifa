"""Webhook routes end to end (ASGI) against Postgres: signatures, dedupe, outbox events, statuses."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from automation_fakes import FakeScheduler
from fastapi import FastAPI
from sqlalchemy import text
from test_auto_whatsapp import meta_payload, text_message
from twilio.request_validator import RequestValidator

from kx.automation.alerts import VoiceAlerts
from kx.automation.channels.sns import SnsEnvelope, SnsVerifier
from kx.automation.settings import AutomationSettings
from kx.automation.webhooks import WebhookDeps, build_router
from kx.core.errors import install_problem_handlers
from kx.core.identity import hmac_sha256_hex, recipient_hash
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db
SECRET = "app-secret"
BASE = "https://api.arifapack.com"
SETTINGS = AutomationSettings.model_validate(
    {
        "whatsapp": {"app_secret": SECRET, "verify_token": "vt"},
        "twilio": {"auth_token": "tw-token", "status_callback_base_url": BASE},
        "ses": {"sns_topic_arns": ["arn:topic"], "inbound_bucket": "kx-mail"},
    }
)


class FakeCaller:
    async def place_call(self, to_e164: str, twiml: str, status_callback_url: str) -> str:
        return "CA1"


class TrustingSns(SnsVerifier):
    """Signature maths is covered in test_auto_sns; here only routing matters."""

    async def verify(self, env: SnsEnvelope) -> bool:
        return env.TopicArn == "arn:topic"


@dataclass
class Hook:
    http: httpx.AsyncClient
    db: Any
    scheduler: FakeScheduler

    async def sql(self, statement: str, **params: Any) -> list[Any]:
        async with self.db.transaction(LogicalDb.AUTOMATION) as s:
            return list((await s.execute(text(statement), params)).all())

    async def whatsapp(self, payload: dict[str, Any], *, secret: str = SECRET) -> httpx.Response:
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac_sha256_hex(secret.encode(), body)
        return await self.http.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": sig})


@pytest.fixture
async def hook(auto_db: Any) -> AsyncIterator[Hook]:
    scheduler = FakeScheduler()
    app = FastAPI()
    install_problem_handlers(app)
    voice = VoiceAlerts(auto_db, SETTINGS, FakeCaller())
    app.include_router(
        build_router(
            WebhookDeps(
                auto_db,
                SETTINGS,
                scheduler,
                voice,
                TrustingSns(httpx.AsyncClient(), frozenset({"arn:topic"})),
            )
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http:
        yield Hook(http, auto_db, scheduler)


async def test_verify_challenge(hook: Hook) -> None:
    ok = await hook.http.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"},
    )
    assert (ok.status_code, ok.text) == (200, "42")
    bad = await hook.http.get(
        "/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "42"}
    )
    assert bad.status_code == 401


async def test_bad_signature_writes_nothing(hook: Hook) -> None:
    response = await hook.whatsapp(meta_payload([text_message()]), secret="wrong")
    assert response.status_code == 401
    assert await hook.sql("SELECT count(*) FROM webhook_receipts") == [(0,)]


async def test_inbound_text_stored_once_and_announced_with_hash_only(hook: Hook) -> None:
    payload = meta_payload(
        [text_message()], contacts=[{"profile": {"name": "Ahmed"}, "wa_id": "971501234567"}]
    )
    assert (await hook.whatsapp(payload)).status_code == 200
    assert (await hook.whatsapp(payload)).status_code == 200  # Meta redelivers
    rows = await hook.sql("SELECT kind, text FROM inbound_messages")
    assert rows == [("text", "Need 50,000 pouches")]
    (event,) = await hook.sql("SELECT event_type, payload FROM outbox_events")
    assert event[0] == "message.received.v1"
    assert event[1]["from_address_hash"] == recipient_hash("whatsapp", "+971501234567")
    assert "971501234567" not in json.dumps(event[1])
    (thread,) = await hook.sql("SELECT external_key, last_inbound_at IS NOT NULL FROM threads")
    assert thread == ("+971501234567", True)


async def test_media_message_waits_for_fetch(hook: Hook) -> None:
    msg = {
        "from": "971501234567",
        "id": "wamid.AUD",
        "timestamp": "1793600000",
        "type": "audio",
        "audio": {"id": "M3", "mime_type": "audio/ogg"},
    }
    await hook.whatsapp(meta_payload([msg]))
    assert len(hook.scheduler.named("media")) == 1
    assert await hook.sql("SELECT count(*) FROM outbox_events") == [(0,)]


async def test_stop_keyword_suppresses(hook: Hook) -> None:
    """§8.1 steps 1-2 and 9: the whole-message word stops every channel at once and is logged for good."""
    await hook.whatsapp(meta_payload([text_message(body=" STOP ")]))
    assert await hook.sql("SELECT channel, reason FROM suppressions") == [("all", "rule:all")]
    assert await hook.sql("SELECT scope, source, decided_by, exact_words FROM opt_out_log") == [
        ("all", "rule", "word:stop", " STOP ")
    ]
    events = await hook.sql("SELECT event_type, payload->>'opted_out' FROM outbox_events ORDER BY sequence")
    assert events == [("consent.opted_out.v1", None), ("message.received.v1", "true")]


async def test_channel_phrase_stops_one_channel_and_a_sentence_goes_to_the_ai(hook: Hook) -> None:
    """§8.1 step 6: "use email instead" stops WhatsApp only; "stop, let me check" is read by the AI."""
    await hook.whatsapp(meta_payload([text_message(body="Please use email instead")]))
    assert await hook.sql("SELECT channel FROM suppressions") == [("whatsapp",)]
    await hook.whatsapp(meta_payload([text_message(wamid="wamid.IN2", body="stop, let me check")]))
    rows = await hook.sql(
        "SELECT payload->>'stop_in_sentence' FROM outbox_events WHERE event_type = 'message.received.v1' "
        "ORDER BY sequence"
    )
    assert rows == [("false",), ("true",)]
    logged = await hook.sql(
        "SELECT scope, decided_by FROM opt_out_log WHERE exact_words IN ('Please use email instead', "
        "'stop, let me check')"
    )
    assert logged == [("whatsapp", "channel:use email instead")]  # the sentence stopped nothing by itself


async def test_statuses_move_forward_only(hook: Hook) -> None:
    await hook.sql(
        "INSERT INTO outbound_messages (channel_account_id, to_address, kind, requested_by, idempotency_key, status, "
        "provider_message_id) SELECT id, '+971501234567', 'reply', 'ai', 'k1', 'sent', 'wamid.OUT' "
        "FROM channel_accounts WHERE external_account_id = 'PNID1' RETURNING id"
    )
    status = {"id": "wamid.OUT", "timestamp": "1793600100", "recipient_id": "971501234567"}
    await hook.whatsapp(meta_payload(statuses=[status | {"status": "read"}]))
    await hook.whatsapp(meta_payload(statuses=[status | {"status": "delivered"}]))  # late, out of order
    assert await hook.sql("SELECT status, read_at IS NOT NULL FROM outbound_messages") == [("read", True)]
    events = await hook.sql("SELECT payload->>'status' FROM outbox_events ORDER BY sequence")
    assert events == [("read",)]


def twilio_headers(path: str, form: dict[str, str]) -> dict[str, str]:
    return {"X-Twilio-Signature": RequestValidator("tw-token").compute_signature(f"{BASE}{path}", form)}


async def test_twilio_gather_press_2_pulls_ladder_forward(hook: Hook) -> None:
    approval = uuid.uuid4()
    (row,) = await hook.sql(
        "INSERT INTO call_attempts (purpose, approval_ref, to_e164, provider) "
        "VALUES ('approval_alert', :a, '+971501111111', 'twilio') RETURNING id",
        a=approval,
    )
    path = f"/webhooks/twilio/voice/{row[0]}"
    form = {"CallSid": "CA1", "Digits": "2"}
    unsigned = await hook.http.post(path, data=form)
    assert unsigned.status_code == 401
    response = await hook.http.post(path, data=form, headers=twilio_headers(path, form))
    assert response.status_code == 200 and "manager is being alerted" in response.text
    assert hook.scheduler.named("pull") == [{"approval_ref": approval}]
    assert await hook.sql("SELECT digits_pressed, status FROM call_attempts") == [("2", "answered")]


async def test_twilio_status_never_regresses(hook: Hook) -> None:
    (row,) = await hook.sql(
        "INSERT INTO call_attempts (purpose, to_e164, provider) "
        "VALUES ('below_range_alert', '+971501111111', 'twilio') RETURNING id"
    )
    path = f"/webhooks/twilio/status?attempt_id={row[0]}"
    for status in ("completed", "ringing"):
        form = {"CallSid": "CA7", "CallStatus": status}
        assert (await hook.http.post(path, data=form, headers=twilio_headers(path, form))).status_code == 204
    assert await hook.sql("SELECT status, provider_call_id, ended_at IS NOT NULL FROM call_attempts") == [
        ("completed", "CA7", True)
    ]


def sns(message: dict[str, Any], topic: str = "arn:topic", message_id: str = "sns-1") -> dict[str, Any]:
    return {
        "Type": "Notification",
        "MessageId": message_id,
        "TopicArn": topic,
        "Message": json.dumps(message),
        "Timestamp": "2026-11-03T06:00:00Z",
        "SignatureVersion": "2",
        "Signature": "x",
        "SigningCertURL": "https://sns.me-central-1.amazonaws.com/c.pem",
    }


async def test_ses_bounce_suppresses_and_fails(hook: Hook) -> None:
    await hook.sql(
        "INSERT INTO outbound_messages (channel_account_id, to_address, kind, requested_by, idempotency_key, status, "
        "provider_message_id) SELECT id, 'a@b.com', 'reply', 'ai', 'k1', 'sent', '0102-x' "
        "FROM channel_accounts WHERE channel = 'email' RETURNING id"
    )
    bounce = {
        "eventType": "Bounce",
        "mail": {"messageId": "0102-x", "timestamp": "2026-11-03T06:00:00Z"},
        "bounce": {"bounceType": "Permanent", "bouncedRecipients": [{"emailAddress": "a@b.com"}]},
    }
    for _ in range(2):
        assert (await hook.http.post("/webhooks/ses", json=sns(bounce))).status_code == 200
    assert await hook.sql("SELECT status FROM outbound_messages") == [("failed",)]
    assert await hook.sql("SELECT channel, reason FROM suppressions") == [("email", "bounce")]
    assert await hook.sql("SELECT payload->>'status' FROM outbox_events") == [("bounced",)]
    assert (await hook.http.post("/webhooks/ses", json=sns(bounce, topic="arn:evil"))).status_code == 401


async def test_ses_inbound_mail_enqueues_only_our_bucket(hook: Hook) -> None:
    records = {
        "Records": [
            {
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": "kx-mail"}, "object": {"key": "in/1"}},
            },
            {
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": "other"}, "object": {"key": "in/2"}},
            },
        ]
    }
    await hook.http.post("/webhooks/ses", json=sns(records))
    assert hook.scheduler.named("email") == [{"key": "in/1"}]
