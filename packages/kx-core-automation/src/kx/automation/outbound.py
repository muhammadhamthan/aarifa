"""OutboundService: the single choke point every message leaves through (`kx.core.contracts.MessageSender`).

`send()` decides *whether and when* a message may go and records it; `deliver()` (run by the
`deliver_outbound` job) talks to the provider. Order of checks in `send()`:

1. idempotency: a replayed `idempotency_key` returns the original message id, nothing else happens;
2. kill switch and per-recipient rate limit: transient refusals, nothing recorded, caller may retry;
3. suppression, WhatsApp 24 h window, send grant: permanent refusals, recorded as a `blocked` row
   (when the DDL allows) plus a MESSAGE_STATUS event the CRM writes to its audit log;
4. contact hours: alerts to staff wait for 08:00-22:00 Dubai, follow-ups for 09:00-18:00 buyer time
   and the weekly cap;
5. insert `queued` row, commit, enqueue delivery (ordered per thread via the procrastinate `lock`).

Priced content hash (must match what the CRM froze when it issued the grant):
    no PDF:   sha256_hex(text.encode())
    with PDF: sha256_hex(text.encode() + b"\\n" + sha256_hex(pdf_bytes).encode())
where `text` is the body, or for a template send the template body rendered with its parameters,
so the hash covers exactly the words the buyer receives.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kx.automation.channels import ChannelError
from kx.automation.channels.email import Attachment
from kx.automation.channels.whatsapp import DocumentRef
from kx.automation.contact_hours import WEEK, next_followup_slot
from kx.automation.models import ChannelAccount, MessageTemplate, OutboundMessage, OutboundStatus, Thread
from kx.automation.outbox import emit
from kx.automation.ports import Clock, MediaStore, TaskScheduler, utc_now
from kx.automation.ratelimit import Bucket, Decision
from kx.automation.repo import ThreadRef, advisory_lock, channel_account, is_suppressed, upsert_thread
from kx.automation.settings import AutomationSettings
from kx.core.contracts import PRICED_KINDS, GrantConsumer, OutboundKind, OutboundRequest
from kx.core.db import Databases
from kx.core.errors import RuleViolation
from kx.core.event_payloads import MessageStatus
from kx.core.identity import new_id, normalise_address, priced_content_sha256, recipient_hash, sha256_hex
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)

STAFF_KINDS: Final = frozenset({OutboundKind.APPROVAL_ALERT, OutboundKind.MD_NOTICE})
# Proactive outreach the buyer did not just ask for: buyer contact hours + weekly cap apply.
PROACTIVE_KINDS: Final = frozenset({OutboundKind.FOLLOWUP, OutboundKind.REORDER})
_INACTIVE: Final = ("blocked", "cancelled", "failed")
# Refusals that clear by themselves (switch turned back on, bucket refilled): callers may retry.
TRANSIENT_REASONS: Final = frozenset({"kill_switch", "rate_limited"})
_PLACEHOLDER: Final = re.compile(r"\{\{\s*(\w+)\s*\}\}")


# ---------------------------------------------------------------- ports used only here
class KillSwitchReader(Protocol):
    async def engaged(self, channel: str) -> bool: ...


class RateLimiter(Protocol):
    async def take(self, scope: str, key: str, bucket: Bucket, cost: int = 1) -> Decision: ...


class WhatsAppTransport(Protocol):
    async def send_text(self, to_e164: str, body: str, *, preview_url: bool = False) -> str: ...
    async def send_template(
        self, to_e164: str, name: str, language: str, params: tuple[str, ...] = ()
    ) -> str: ...
    async def send_document(self, to_e164: str, document: DocumentRef, caption: str | None = None) -> str: ...
    async def upload_media(self, data: bytes, mime_type: str, filename: str) -> str: ...


class EmailTransport(Protocol):
    async def send(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        attachment: Attachment | None = None,
        in_reply_to: str | None = None,
    ) -> str: ...


class TransientDeliveryError(Exception):
    """Raised by `deliver()` so the job queue retries later (same job id keeps per-thread order)."""


# ---------------------------------------------------------------- pure helpers
def content_sha256(text: str, pdf: bytes | None) -> str:
    """Hash of what is about to leave, in the one canonical form shared with the CRM."""
    return priced_content_sha256(text, None if pdf is None else sha256_hex(pdf))


def render_template(body: str, variables: list[str], params: tuple[str, ...]) -> str:
    """Fill `{{1}}`-style (Meta) or `{{name}}`-style placeholders; a missing value is a refusal."""

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        index = int(name) - 1 if name.isdigit() else (variables.index(name) if name in variables else -1)
        if not 0 <= index < len(params):
            raise RuleViolation(f"template parameter {name!r} missing", reason="template_params")
        return params[index]

    return _PLACEHOLDER.sub(value, body)


@dataclass(frozen=True, slots=True)
class _Prepared:
    """Everything `send()` needs that comes from outside the database transaction."""

    address: str
    rhash: str
    text: str | None
    subject: str | None
    pdf: bytes | None


# ---------------------------------------------------------------- service
class OutboundService:
    def __init__(
        self,
        *,
        databases: Databases,
        settings: AutomationSettings,
        grants: GrantConsumer,
        kill_switch: KillSwitchReader,
        limiter: RateLimiter,
        scheduler: TaskScheduler,
        media: MediaStore,
        whatsapp: WhatsAppTransport,
        email: EmailTransport,
        clock: Clock = utc_now,
    ) -> None:
        self._db = databases
        self._settings = settings
        self._grants = grants
        self._kill = kill_switch
        self._limiter = limiter
        self._scheduler = scheduler
        self._media = media
        self._whatsapp = whatsapp
        self._email = email
        self._now = clock
        rl = settings.rate_limit
        self._recipient_bucket = Bucket(rl.recipient_capacity, rl.recipient_refill_per_minute)
        self._channel_bucket = Bucket(rl.channel_capacity, rl.channel_refill_per_minute)

    # ============================================================ send
    async def send(self, request: OutboundRequest) -> uuid.UUID:
        if (existing := await self._existing(request.idempotency_key)) is not None:
            return existing
        address, rhash = _recipient(request)
        await self._transient_checks(request.channel, rhash)

        message_id = new_id()
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            account = await channel_account(
                s, request.channel, "internal_alerts" if request.kind in STAFF_KINDS else "customer"
            )
            thread = await upsert_thread(
                s, account, address, contact_ref=request.contact_ref, deal_ref=request.deal_ref
            )
            template = await self._template(s, request)
            prepared = await self._prepare(request, address, rhash, template)
            reason = await self._block_reason(
                s, request=request, p=prepared, thread=thread, template=template, message_id=message_id
            )
            not_before = None if reason else await self._not_before(s, request, prepared)
            stored = await self._insert(
                s,
                message_id=message_id,
                request=request,
                p=prepared,
                account=account,
                thread=thread,
                template=template,
                reason=reason,
                not_before=not_before,
            )
            if stored is not None and stored != message_id:
                return stored  # lost an idempotency race: the winner's id
            if reason:
                await emit(
                    s,
                    MessageStatus(
                        message_ref=message_id,
                        status="failed",
                        provider_message_id=None,
                        error=f"blocked:{reason}",
                        at=self._now(),
                    ),
                )
        if reason:
            log.warning(
                "outbound.blocked", message_id=str(message_id), reason=reason, kind=request.kind.value
            )
            raise RuleViolation(f"message refused: {reason}", reason=reason, message_id=str(message_id))
        in_future = not_before is not None and not_before > self._now()
        await self._scheduler.enqueue_delivery(
            message_id,
            lock=None if in_future else f"thread:{thread.id}",
            at=not_before,
            template_params=request.template_params,
        )
        return message_id

    async def _existing(self, idempotency_key: str) -> uuid.UUID | None:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            return (
                await s.execute(
                    select(OutboundMessage.id).where(OutboundMessage.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()

    async def _transient_checks(self, channel: str, rhash: str) -> None:
        if await self._kill.engaged(channel):
            raise RuleViolation(f"outbound {channel} is switched off", reason="kill_switch")
        decision = await self._limiter.take("recipient", rhash, self._recipient_bucket)
        if not decision.allowed:
            raise RuleViolation(
                "too many messages to this recipient",
                reason="rate_limited",
                retry_after_seconds=decision.retry_after_seconds,
            )

    async def _template(self, s: AsyncSession, request: OutboundRequest) -> MessageTemplate | None:
        if request.template_name is None:
            return None
        template = (
            await s.execute(
                select(MessageTemplate).where(
                    MessageTemplate.channel == request.channel,
                    MessageTemplate.name == request.template_name,
                    MessageTemplate.language == self._settings.whatsapp.template_language,
                    MessageTemplate.provider_status == "approved",
                )
            )
        ).scalar_one_or_none()
        if template is None:
            raise RuleViolation(f"template {request.template_name!r} is not approved", reason="template")
        return template

    async def _prepare(
        self, request: OutboundRequest, address: str, rhash: str, template: MessageTemplate | None
    ) -> _Prepared:
        text = (
            render_template(template.body, template.variables, request.template_params)
            if template is not None
            else request.body
        )
        subject = request.subject or (template.subject if template is not None else None)
        needs_pdf = request.kind in PRICED_KINDS and request.media_s3_key is not None
        pdf = await self._media.get(request.media_s3_key) if needs_pdf and request.media_s3_key else None
        return _Prepared(address, rhash, text, subject, pdf)

    async def _block_reason(
        self,
        s: AsyncSession,
        *,
        request: OutboundRequest,
        p: _Prepared,
        thread: ThreadRef,
        template: MessageTemplate | None,
        message_id: uuid.UUID,
    ) -> str | None:
        if p.text is None and request.media_s3_key is None:
            raise RuleViolation("message has no content", reason="empty")
        if await is_suppressed(s, request.channel, p.rhash):
            return "opt_out"
        if request.channel == "whatsapp" and template is None and not self._session_open(thread):
            return "window_closed"
        if request.kind in PRICED_KINDS:
            return await self._grant_refusal(request, p, message_id)
        return None

    def _session_open(self, thread: ThreadRef) -> bool:
        """Meta allows free-form messages only within 24 h of the buyer's last message."""
        hours = timedelta(hours=self._settings.whatsapp.session_hours)
        return thread.last_inbound_at is not None and self._now() - thread.last_inbound_at < hours

    async def _grant_refusal(
        self, request: OutboundRequest, p: _Prepared, message_id: uuid.UUID
    ) -> str | None:
        """Priced content leaves only with a CRM grant for exactly these bytes and this recipient."""
        if request.grant_id is None or request.content_sha256 is None:
            raise RuleViolation("priced message without a send grant", reason="no_grant")
        if content_sha256(p.text or "", p.pdf) != request.content_sha256:
            return "content_mismatch"
        consumed = await self._grants.consume_grant(
            request.grant_id, request.content_sha256, message_id, p.rhash
        )
        return None if consumed else "grant_refused"

    async def _not_before(self, s: AsyncSession, request: OutboundRequest, p: _Prepared) -> datetime | None:
        now = self._now()
        if request.kind in STAFF_KINDS:
            opens = self._settings.alert_window.next_open(now)
            return opens if opens > now else None
        if request.kind not in PROACTIVE_KINDS:
            return None
        await advisory_lock(s, f"followup:{p.rhash}")  # two schedulers must not both take the last slot
        slot = next_followup_slot(
            now,
            await self._proactive_times(s, p.address, now),
            self._settings.followup_window(request.buyer_timezone),
            self._settings.followup.max_per_week,
        )
        return slot if slot > now else None

    async def _proactive_times(self, s: AsyncSession, address: str, now: datetime) -> list[datetime]:
        at = func.coalesce(OutboundMessage.not_before, OutboundMessage.created_at)
        rows = await s.execute(
            select(at).where(
                OutboundMessage.to_address == address,
                OutboundMessage.kind.in_([k.value for k in PROACTIVE_KINDS]),
                OutboundMessage.status.not_in(_INACTIVE),
                at > now - WEEK,
            )
        )
        return list(rows.scalars())

    async def _insert(
        self,
        s: AsyncSession,
        *,
        message_id: uuid.UUID,
        request: OutboundRequest,
        p: _Prepared,
        account: uuid.UUID,
        thread: ThreadRef,
        template: MessageTemplate | None,
        reason: str | None,
        not_before: datetime | None,
    ) -> uuid.UUID | None:
        """Insert the row; on an idempotency race return the winner's id. A refused priced message
        that reuses an already-consumed grant cannot be stored (grant_ref is UNIQUE): None."""
        status: OutboundStatus = "blocked" if reason else "queued"
        stmt = (
            pg_insert(OutboundMessage)
            .values(
                id=message_id,
                thread_id=thread.id,
                channel_account_id=account,
                to_address=p.address,
                kind=request.kind.value,
                template_id=template.id if template else None,
                subject=p.subject,
                body=p.text,
                media_s3_key=request.media_s3_key,
                content_sha256=request.content_sha256,
                grant_ref=request.grant_id,
                deal_ref=request.deal_ref,
                requested_by=request.requested_by,
                idempotency_key=request.idempotency_key,
                status=status,
                block_reason=reason,
                not_before=not_before,
            )
            .on_conflict_do_nothing()
            .returning(OutboundMessage.id)
        )
        if (inserted := (await s.execute(stmt)).scalar_one_or_none()) is not None:
            return inserted
        return (
            await s.execute(
                select(OutboundMessage.id).where(OutboundMessage.idempotency_key == request.idempotency_key)
            )
        ).scalar_one_or_none()

    async def requeue(self, message_id: uuid.UUID, lock: str | None) -> None:
        """Re-enqueue a queued row whose job was lost (see jobs.requeue_lost); de-duplicated by the queue."""
        await self._scheduler.enqueue_delivery(message_id, lock=lock, at=None)

    # ============================================================ deliver
    async def deliver(
        self, message_id: uuid.UUID, template_params: tuple[str, ...] = (), *, final_attempt: bool = False
    ) -> None:
        """Hand one queued message to its provider. Safe to run twice: only a `queued` row is sent."""
        claimed = await self._claim(message_id)
        if claimed is None:
            return
        row, channel, template = claimed
        try:
            provider_id = await self._transmit(row, channel, template, template_params)
        except ChannelError as exc:
            if exc.retryable and not final_attempt:
                await self._set_status(message_id, "queued", error=exc.detail)
                raise TransientDeliveryError(exc.detail) from exc
            await self._finish(message_id, "failed", None, exc.detail)
            return
        except Exception as exc:
            await self._finish(message_id, "failed", None, type(exc).__name__)
            raise
        await self._finish(message_id, "sent", provider_id, None)

    async def _claim(
        self, message_id: uuid.UUID
    ) -> tuple[OutboundMessage, str, MessageTemplate | None] | None:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            found = (
                await s.execute(
                    select(OutboundMessage, ChannelAccount.channel)
                    .join(ChannelAccount, ChannelAccount.id == OutboundMessage.channel_account_id)
                    .where(OutboundMessage.id == message_id)
                    .with_for_update(of=OutboundMessage)
                )
            ).one_or_none()
            if found is None or found[0].status != "queued":
                return None
            row, channel = found
            if await self._kill.engaged(channel):  # the switch also stops what was already queued
                await self._mark(s, row, "cancelled", block_reason="kill_switch")
                return None
            if not (await self._limiter.take("channel", channel, self._channel_bucket)).allowed:
                raise TransientDeliveryError("channel rate limit")
            template = await s.get(MessageTemplate, row.template_id) if row.template_id else None
            if channel == "whatsapp" and template is None and not await self._thread_open(s, row):
                await self._mark(s, row, "blocked", block_reason="window_closed")
                return None
            row.status = "sending"
            return row, channel, template

    async def _thread_open(self, s: AsyncSession, row: OutboundMessage) -> bool:
        """Scheduled messages can outlive the 24 h window they were accepted in: check again."""
        thread = await s.get(Thread, row.thread_id) if row.thread_id else None
        return thread is not None and self._session_open(
            ThreadRef(thread.id, thread.contact_ref, thread.deal_ref, thread.last_inbound_at)
        )

    async def _transmit(
        self, row: OutboundMessage, channel: str, template: MessageTemplate | None, params: tuple[str, ...]
    ) -> str:
        pdf = await self._media.get(row.media_s3_key) if row.media_s3_key else None
        filename = (row.media_s3_key or "document.pdf").rsplit("/", 1)[-1]
        if channel == "email":
            attachment = Attachment(filename, "application/pdf", pdf) if pdf is not None else None
            return await self._email.send(
                to_address=row.to_address,
                subject=row.subject or "Arifaa Pack",
                body=row.body or "",
                attachment=attachment,
            )
        if template is not None:
            return await self._whatsapp.send_template(
                row.to_address, template.name, template.language, params
            )
        if pdf is not None:
            media_id = await self._whatsapp.upload_media(pdf, "application/pdf", filename)
            return await self._whatsapp.send_document(
                row.to_address, DocumentRef(filename, media_id=media_id), caption=row.body
            )
        return await self._whatsapp.send_text(row.to_address, row.body or "")

    async def _set_status(self, message_id: uuid.UUID, status: OutboundStatus, *, error: str | None) -> None:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            await s.execute(
                update(OutboundMessage)
                .where(OutboundMessage.id == message_id)
                .values(status=status, error=error)
            )

    async def _finish(
        self, message_id: uuid.UUID, status: OutboundStatus, provider_id: str | None, error: str | None
    ) -> None:
        now = self._now()
        values: dict[str, Any] = {"status": status, "provider_message_id": provider_id, "error": error}
        if status == "sent":
            values["sent_at"] = now
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            await s.execute(update(OutboundMessage).where(OutboundMessage.id == message_id).values(**values))
            await emit(
                s,
                MessageStatus(
                    message_ref=message_id,
                    status="sent" if status == "sent" else "failed",
                    provider_message_id=provider_id,
                    error=error,
                    at=now,
                ),
            )

    async def _mark(
        self, s: AsyncSession, row: OutboundMessage, status: OutboundStatus, *, block_reason: str
    ) -> None:
        row.status, row.block_reason = status, block_reason
        await emit(
            s,
            MessageStatus(
                message_ref=row.id,
                status="failed",
                provider_message_id=None,
                error=f"{status}:{block_reason}",
                at=self._now(),
            ),
        )


def _recipient(request: OutboundRequest) -> tuple[str, str]:
    try:
        return normalise_address(request.channel, request.to_address), recipient_hash(
            request.channel, request.to_address
        )
    except ValueError as exc:
        raise RuleViolation("invalid recipient address", reason="invalid_recipient") from exc
