"""Provider webhooks. Thin: verify the signature on the raw request, record in one transaction,
enqueue slow work (media download, inbound mail parsing) after commit, answer 200 fast.

Mount with `app.include_router(build_router(...))` -> /webhooks/whatsapp, /webhooks/twilio/...,
/webhooks/ses. Unsigned or badly signed requests get 401 (via kx.core problem handlers, which the
app installs) and write nothing.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl

import orjson
from fastapi import APIRouter, Request, Response
from pydantic import ValidationError

from kx.automation import inbound
from kx.automation.alerts import VoiceAlerts
from kx.automation.channels import voice_ivr, whatsapp
from kx.automation.channels.email import parse_ses_event, s3_objects
from kx.automation.channels.postmark import parse_inbound as parse_postmark
from kx.automation.channels.sns import SnsEnvelope, SnsVerifier
from kx.automation.ports import MediaStore, TaskScheduler
from kx.automation.repo import first_receipt
from kx.automation.settings import AutomationSettings
from kx.core.db import Databases
from kx.core.errors import Unauthorized
from kx.core.identity import constant_time_equals, sha256_hex
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)
_XML = "application/xml"


@dataclass(frozen=True, slots=True)
class WebhookDeps:
    databases: Databases
    settings: AutomationSettings
    scheduler: TaskScheduler
    voice: VoiceAlerts
    sns: SnsVerifier
    media: MediaStore | None = None  # Postmark inbound attachments arrive inline (D33)


def build_router(deps: WebhookDeps) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)
    _whatsapp_routes(router, deps)
    _twilio_routes(router, deps)
    _ses_routes(router, deps)
    _postmark_routes(router, deps)
    return router


def _whatsapp_routes(router: APIRouter, deps: WebhookDeps) -> None:
    wa = deps.settings.whatsapp

    @router.get("/whatsapp")
    async def whatsapp_verify(request: Request) -> Response:
        q = request.query_params
        challenge = whatsapp.verify_challenge(
            wa.verify_token.get_secret_value(),
            q.get("hub.mode"),
            q.get("hub.verify_token"),
            q.get("hub.challenge"),
        )
        if challenge is None:
            raise Unauthorized("verify token mismatch")
        return Response(challenge, media_type="text/plain")

    @router.post("/whatsapp")
    async def whatsapp_events(request: Request) -> Response:
        raw = await request.body()
        signature = request.headers.get(whatsapp.SIGNATURE_HEADER)
        if not whatsapp.verify_signature(wa.app_secret.get_secret_value(), raw, signature):
            raise Unauthorized("bad signature")
        hook = whatsapp.parse_webhook(orjson.loads(raw))
        async with deps.databases.transaction(LogicalDb.AUTOMATION) as s:
            media_pending = await inbound.record_whatsapp(s, hook)
        for inbound_id in media_pending:
            await deps.scheduler.enqueue_media_ingest(inbound_id)
        return Response(status_code=200)


def _twilio_routes(router: APIRouter, deps: WebhookDeps) -> None:
    tw = deps.settings.twilio

    async def signed_form(request: Request) -> dict[str, str]:
        # Twilio posts application/x-www-form-urlencoded; stdlib parsing avoids a multipart dependency.
        form = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
        # Twilio signs the public URL it called, not the address the load balancer forwards to.
        public = f"{tw.status_callback_base_url.rstrip('/')}{request.url.path}"
        if request.url.query:
            public = f"{public}?{request.url.query}"
        signature = request.headers.get(voice_ivr.SIGNATURE_HEADER)
        if not voice_ivr.validate_signature(tw.auth_token.get_secret_value(), public, form, signature):
            raise Unauthorized("bad signature")
        return form

    @router.post("/twilio/voice/{attempt_id}")
    async def twilio_gather(attempt_id: uuid.UUID, request: Request) -> Response:
        digits = (await signed_form(request)).get("Digits", "")[:1]
        async with deps.databases.transaction(LogicalDb.AUTOMATION) as s:
            escalate = await deps.voice.record_digits(s, attempt_id, digits) if digits else None
        if escalate is not None:
            await deps.scheduler.pull_ladder_forward(escalate)
        return Response(voice_ivr.gather_reply_twiml(digits or None, tw), media_type=_XML)

    @router.post("/twilio/status")
    async def twilio_status(request: Request) -> Response:
        form = await signed_form(request)
        try:
            attempt_id = uuid.UUID(request.query_params.get("attempt_id", ""))
        except ValueError:
            return Response(status_code=204)
        async with deps.databases.transaction(LogicalDb.AUTOMATION) as s:
            await deps.voice.record_status(s, attempt_id, form.get("CallSid", ""), form.get("CallStatus", ""))
        return Response(status_code=204)


def _ses_routes(router: APIRouter, deps: WebhookDeps) -> None:
    @router.post("/ses")
    async def ses_events(request: Request) -> Response:
        try:
            env = SnsEnvelope.model_validate_json(await request.body())
        except ValidationError as exc:
            raise Unauthorized("not an SNS message") from exc
        if not await deps.sns.verify(env):
            raise Unauthorized("bad SNS signature")
        if env.Type == "SubscriptionConfirmation":
            await deps.sns.confirm_subscription(env)
            log.info("sns.subscribed", topic=env.TopicArn)
        elif env.Type == "Notification":
            await _ses_notification(deps, env)
        return Response(status_code=200)


async def _ses_notification(deps: WebhookDeps, env: SnsEnvelope) -> None:
    message = orjson.loads(env.Message)
    if objects := s3_objects(message):  # inbound mail stored by Mail Manager: parse it in a job
        for bucket, key in objects:
            if bucket == deps.settings.ses.inbound_bucket:
                await deps.scheduler.enqueue_email_ingest(key)
        return
    if (event := parse_ses_event(message)) is not None:
        async with deps.databases.transaction(LogicalDb.AUTOMATION) as s:
            if await first_receipt(s, "ses", env.MessageId):
                await inbound.record_ses_event(s, event)


def _basic_ok(header: str | None, user: str, password: str) -> bool:
    if not user or not password or not header or not header.startswith("Basic "):
        return False
    try:
        given = base64.b64decode(header[6:], validate=True).decode()
    except ValueError:
        return False
    return constant_time_equals(given, f"{user}:{password}")


def _postmark_routes(router: APIRouter, deps: WebhookDeps) -> None:
    @router.post("/postmark/inbound")
    async def postmark_inbound(request: Request) -> Response:
        """D33 buyer replies through Postmark. Postmark does not sign webhooks; the URL carries Basic auth
        (https://user:password@api.<domain>/webhooks/postmark/inbound) and the WAF allows Postmark's IPs."""
        pm = deps.settings.postmark
        if not _basic_ok(
            request.headers.get("authorization"), pm.inbound_user, pm.inbound_password.get_secret_value()
        ):
            return Response(status_code=403)  # 403 stops Postmark's retries for a misconfigured hook
        mail = parse_postmark(orjson.loads(await request.body()))
        if not mail.message_id or not mail.from_address:
            return Response(status_code=200)  # nothing we can thread; do not make Postmark retry
        folder = f"email/{sha256_hex(mail.message_id.encode())[:24]}"
        keys: list[str] = []
        for i, attachment in enumerate(mail.attachments if deps.media is not None else ()):
            name = "".join(c if c.isalnum() or c in "._-" else "_" for c in attachment.filename)[:80]
            keys.append(f"{folder}/{i}-{name}")
            assert deps.media is not None
            await deps.media.put(keys[-1], attachment.data, attachment.content_type)
        async with deps.databases.transaction(LogicalDb.AUTOMATION) as s:
            await inbound.record_email(s, mail, s3_key=None, attachment_keys=tuple(keys), provider="postmark")
        return Response(status_code=200)
