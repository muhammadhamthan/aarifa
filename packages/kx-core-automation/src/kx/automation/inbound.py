"""Recording what providers tell us: inbound messages, delivery statuses, bounces and complaints.

Every function runs inside the caller's transaction so the receipt, the rows and the outbox event
commit together (a crash can never leave a stored message without its MESSAGE_RECEIVED event).
Raw addresses stay in this database; events carry only `recipient_hash`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kx.automation.channels import whatsapp as wa
from kx.automation.channels.email import InboundEmail, SesEvent
from kx.automation.models import InboundMessage, OutboundMessage, OutboundStatus, Thread
from kx.automation.outbox import emit
from kx.automation.repo import (
    ThreadRef,
    account_by_address,
    account_by_external_id,
    channel_account,
    first_receipt,
    suppress,
    upsert_thread,
)
from kx.core.event_payloads import MessageReceived, MessageStatus
from kx.core.identity import recipient_hash
from kx.core.logging import get_logger

log = get_logger(__name__)

# A status may only move forward; e.g. a late "delivered" must not overwrite "read".
_FORWARD: Final[dict[OutboundStatus, tuple[OutboundStatus, ...]]] = {
    "sent": ("sending",),
    "delivered": ("sending", "sent"),
    "read": ("sending", "sent", "delivered"),
    "failed": ("sending", "sent", "delivered"),
}
_STAMP: Final[dict[str, str]] = {"sent": "sent_at", "delivered": "delivered_at", "read": "read_at"}
EventStatus = Literal["sent", "delivered", "read", "failed", "bounced", "complained"]
_ROW_STATUS: Final[dict[EventStatus, OutboundStatus | None]] = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "bounced": "failed",
    "complained": None,
}


@dataclass(frozen=True, slots=True)
class _Stored:
    id: uuid.UUID
    thread: ThreadRef


async def _store(
    s: AsyncSession,
    *,
    thread: ThreadRef,
    provider: str,
    provider_message_id: str,
    kind: str,
    text: str | None,
    media_s3_key: str | None,
    raw: dict[str, Any],
    received_at: datetime,
) -> _Stored | None:
    """Insert once per (provider, provider_message_id); None when it was already stored."""
    stmt = (
        pg_insert(InboundMessage)
        .values(
            thread_id=thread.id,
            provider=provider,
            provider_message_id=provider_message_id,
            kind=kind,
            text=text,
            media_s3_key=media_s3_key,
            raw=raw,
            received_at=received_at,
        )
        .on_conflict_do_nothing(index_elements=["provider", "provider_message_id"])
        .returning(InboundMessage.id)
    )
    inserted = (await s.execute(stmt)).scalar_one_or_none()
    return _Stored(inserted, thread) if inserted is not None else None


async def _received(
    s: AsyncSession,
    stored: _Stored,
    *,
    channel: Literal["whatsapp", "email"],
    address: str,
    text: str,
    media: tuple[str, ...],
    at: datetime,
    provider_message_id: str,
) -> None:
    await emit(
        s,
        MessageReceived(
            message_ref=stored.id,
            thread_ref=stored.thread.id,
            channel=channel,
            from_address_hash=recipient_hash(channel, address),
            contact_ref=stored.thread.contact_ref,
            deal_ref=stored.thread.deal_ref,
            text=text,
            media_s3_keys=media,
            received_at=at,
            provider_message_id=provider_message_id,
        ),
    )


# ---------------------------------------------------------------- WhatsApp
async def record_whatsapp(
    s: AsyncSession, hook: wa.WaWebhook, opt_out_keywords: frozenset[str]
) -> list[uuid.UUID]:
    """Store messages + statuses. Returns inbound ids whose media must be fetched before they are
    announced (the AI needs the voice note / image, not just "a file arrived")."""
    media_pending: list[uuid.UUID] = []
    for m in hook.messages:
        if not await first_receipt(s, wa.PROVIDER, m.message_id):
            continue
        account = await account_by_external_id(s, wa.PROVIDER, m.phone_number_id)
        if account is None:
            log.warning("whatsapp.unknown_phone_number_id", phone_number_id=m.phone_number_id)
            continue
        thread = await upsert_thread(s, account, m.from_e164, inbound_at=m.received_at)
        stored = await _store(
            s,
            thread=thread,
            provider=wa.PROVIDER,
            provider_message_id=m.message_id,
            kind=m.kind,
            text=m.text or None,
            media_s3_key=None,
            raw=m.raw,
            received_at=m.received_at,
        )
        if stored is None:
            continue
        if m.kind == "text" and m.text.strip().casefold() in opt_out_keywords:
            await suppress(s, "whatsapp", recipient_hash("whatsapp", m.from_e164), "stop_keyword")
        if m.media is not None:
            media_pending.append(stored.id)
        elif m.kind in {"text", "button", "interactive"}:
            await _received(
                s,
                stored,
                channel="whatsapp",
                address=m.from_e164,
                text=m.text,
                media=(),
                at=m.received_at,
                provider_message_id=m.message_id,
            )
    for st in hook.statuses:
        if st.status != "deleted" and await first_receipt(s, wa.PROVIDER, f"{st.message_id}:{st.status}"):
            await advance_status(s, st.message_id, st.status, at=st.at, error=st.error)
    return media_pending


async def attach_media(s: AsyncSession, inbound_id: uuid.UUID, key: str) -> None:
    """Media stored in S3: link it and announce the message (once: only while media_s3_key is NULL)."""
    row = (
        await s.execute(
            update(InboundMessage)
            .where(InboundMessage.id == inbound_id, InboundMessage.media_s3_key.is_(None))
            .values(media_s3_key=key)
            .returning(InboundMessage)
        )
    ).scalar_one_or_none()
    if row is None:
        return
    thread = await s.get(Thread, row.thread_id)
    assert thread is not None  # FK
    ref = ThreadRef(thread.id, thread.contact_ref, thread.deal_ref, thread.last_inbound_at)
    await _received(
        s,
        _Stored(row.id, ref),
        channel="whatsapp",
        address=thread.external_key,
        text=row.text or "",
        media=(key,),
        at=row.received_at,
        provider_message_id=row.provider_message_id,
    )


async def media_id_of(s: AsyncSession, inbound_id: uuid.UUID) -> tuple[str, str | None] | None:
    raw = (
        await s.execute(select(InboundMessage.raw).where(InboundMessage.id == inbound_id))
    ).scalar_one_or_none()
    if raw is None:
        return None
    media = next((raw[k] for k in ("image", "audio", "document", "video", "sticker") if k in raw), None)
    return (str(media["id"]), media.get("mime_type")) if isinstance(media, dict) else None


# ---------------------------------------------------------------- statuses (WhatsApp + SES)
async def advance_status(
    s: AsyncSession, provider_message_id: str, status: EventStatus, *, at: datetime, error: str | None = None
) -> None:
    """Move an outbound row forward and announce it. Bounce/complaint are announced as such even
    though the row only records failed / unchanged (the DDL has no such statuses)."""
    row_status = _ROW_STATUS[status]
    if row_status is None:
        message_id = (
            await s.execute(
                select(OutboundMessage.id).where(OutboundMessage.provider_message_id == provider_message_id)
            )
        ).scalar_one_or_none()
    else:
        values: dict[str, Any] = {"status": row_status, "error": error}
        if (stamp := _STAMP.get(row_status)) is not None:
            values[stamp] = at
        message_id = (
            await s.execute(
                update(OutboundMessage)
                .where(
                    OutboundMessage.provider_message_id == provider_message_id,
                    OutboundMessage.status.in_(_FORWARD[row_status]),
                )
                .values(**values)
                .returning(OutboundMessage.id)
            )
        ).scalar_one_or_none()
    if message_id is not None:
        await emit(
            s,
            MessageStatus(
                message_ref=message_id,
                status=status,
                provider_message_id=provider_message_id,
                error=error,
                at=at,
            ),
        )


async def record_ses_event(s: AsyncSession, event: SesEvent) -> None:
    match event.kind:
        case "delivery":
            await advance_status(s, event.provider_message_id, "delivered", at=event.at)
        case "bounce" if event.permanent:
            await _suppress_emails(s, event.recipients, "bounce")
            await advance_status(s, event.provider_message_id, "bounced", at=event.at, error=event.detail)
        case "complaint":
            await _suppress_emails(s, event.recipients, "complaint")
            await advance_status(s, event.provider_message_id, "complained", at=event.at, error=event.detail)
        case "reject":
            await advance_status(s, event.provider_message_id, "failed", at=event.at, error="ses_reject")
        case _:
            return


async def _suppress_emails(s: AsyncSession, addresses: Iterable[str], reason: str) -> None:
    for address in addresses:
        await suppress(s, "email", recipient_hash("email", address), reason)


# ---------------------------------------------------------------- email
async def record_email(
    s: AsyncSession, mail: InboundEmail, *, s3_key: str, attachment_keys: tuple[str, ...]
) -> None:
    if not await first_receipt(s, "ses", mail.message_id):
        return
    account = await account_by_address(s, "email", mail.to_addresses) or await channel_account(
        s, "email", "customer"
    )
    thread = await upsert_thread(s, account, mail.from_address, inbound_at=mail.received_at)
    raw = {
        "s3_key": s3_key,
        "subject": mail.subject,
        "in_reply_to": mail.in_reply_to,
        "attachments": list(attachment_keys),
    }
    stored = await _store(
        s,
        thread=thread,
        provider="ses",
        provider_message_id=mail.message_id,
        kind="email",
        text=mail.text,
        media_s3_key=attachment_keys[0] if attachment_keys else None,
        raw=raw,
        received_at=mail.received_at,
    )
    if stored is not None:
        text = f"{mail.subject}\n\n{mail.text}".strip() if mail.subject else mail.text
        await _received(
            s,
            stored,
            channel="email",
            address=mail.from_address,
            text=text,
            media=attachment_keys,
            at=mail.received_at,
            provider_message_id=mail.message_id,
        )
