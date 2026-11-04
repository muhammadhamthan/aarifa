"""Small SQL building blocks shared by outbound, inbound and jobs (all run inside a caller's session)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kx.automation.models import ChannelAccount, Suppression, Thread, WebhookReceipt
from kx.core.errors import RuleViolation

ALL_CHANNELS: Final = "all"
Purpose = Literal["customer", "internal_alerts"]


@dataclass(frozen=True, slots=True)
class ThreadRef:
    id: uuid.UUID
    contact_ref: uuid.UUID | None
    deal_ref: uuid.UUID | None
    last_inbound_at: datetime | None


async def channel_account(session: AsyncSession, channel: str, prefer: Purpose) -> uuid.UUID:
    """Active sending identity for `channel`; staff alerts prefer the internal-alerts number if one exists."""
    row = (
        await session.execute(
            select(ChannelAccount.id)
            .where(ChannelAccount.channel == channel, ChannelAccount.status == "active")
            .order_by((ChannelAccount.purpose == prefer).desc(), ChannelAccount.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise RuleViolation(f"no active {channel} channel account", reason="no_channel_account")
    return row


async def account_by_external_id(session: AsyncSession, provider: str, external_id: str) -> uuid.UUID | None:
    return (
        await session.execute(
            select(ChannelAccount.id).where(
                ChannelAccount.provider == provider, ChannelAccount.external_account_id == external_id
            )
        )
    ).scalar_one_or_none()


async def account_by_address(
    session: AsyncSession, channel: str, addresses: tuple[str, ...]
) -> uuid.UUID | None:
    return (
        await session.execute(
            select(ChannelAccount.id)
            .where(ChannelAccount.channel == channel, func.lower(ChannelAccount.address).in_(addresses))
            .limit(1)
        )
    ).scalar_one_or_none()


async def upsert_thread(
    session: AsyncSession,
    account_id: uuid.UUID,
    external_key: str,
    *,
    contact_ref: uuid.UUID | None = None,
    deal_ref: uuid.UUID | None = None,
    inbound_at: datetime | None = None,
) -> ThreadRef:
    """One thread per (sending identity, normalised address). Known refs are never overwritten by NULL."""
    ins = pg_insert(Thread).values(
        channel_account_id=account_id,
        external_key=external_key,
        contact_ref=contact_ref,
        deal_ref=deal_ref,
        last_inbound_at=inbound_at,
    )
    stmt = ins.on_conflict_do_update(
        index_elements=[Thread.channel_account_id, Thread.external_key],
        set_={
            "contact_ref": func.coalesce(ins.excluded.contact_ref, Thread.contact_ref),
            "deal_ref": func.coalesce(ins.excluded.deal_ref, Thread.deal_ref),
            "last_inbound_at": func.greatest(ins.excluded.last_inbound_at, Thread.last_inbound_at),
        },
    ).returning(Thread.id, Thread.contact_ref, Thread.deal_ref, Thread.last_inbound_at)
    row = (await session.execute(stmt)).one()
    return ThreadRef(row.id, row.contact_ref, row.deal_ref, row.last_inbound_at)


async def is_suppressed(session: AsyncSession, channel: str, address_hash: str) -> bool:
    return (
        await session.execute(
            select(Suppression.reason)
            .where(Suppression.address_hash == address_hash, Suppression.channel.in_((channel, ALL_CHANNELS)))
            .limit(1)
        )
    ).scalar_one_or_none() is not None


async def suppress(session: AsyncSession, channel: str, address_hash: str, reason: str) -> None:
    await session.execute(
        pg_insert(Suppression)
        .values(channel=channel, address_hash=address_hash, reason=reason)
        .on_conflict_do_nothing()
    )


async def first_receipt(session: AsyncSession, provider: str, external_id: str) -> bool:
    """True the first time a provider event id is seen. Only called after the signature verified,
    so an attacker cannot pre-register ids and make us drop the real deliveries later."""
    result = await session.execute(
        pg_insert(WebhookReceipt)
        .values(provider=provider, external_id=external_id, signature_ok=True)
        .on_conflict_do_nothing()
        .returning(WebhookReceipt.provider)
    )
    return result.scalar_one_or_none() is not None


async def advisory_lock(session: AsyncSession, key: str) -> None:
    """Serialise concurrent work on one key until the transaction ends (e.g. a buyer's weekly cap)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})
