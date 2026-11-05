"""Single-use send grants: the cross-database quote gate.

The CRM issues a grant whenever a human approval releases a buyer-facing quote: approval 1
(commercial quote) and approval 2 (MD, final agreed quote). It binds ONE content hash
(kx.core.identity.priced_content_sha256 over body + PDF hash) to ONE recipient (taken from the
contact record, never from the AI) on ONE channel, for `grant.ttl_hours`. Automation must consume it
(crm.consume_send_grant, atomic and idempotent per message id) immediately before sending; a False
answer means the message must not leave.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.errors import RuleViolation
from kx.core.identity import new_id, recipient_hash
from kx.crm.models import Contact, Deal, Quote, QuoteSendGrant, QuoteVersion
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor, Timeline, record
from kx.crm.services.settings import BusinessSettings

SendChannel = Literal["whatsapp", "email"]
_CONSUME = text("SELECT crm.consume_send_grant(:g, :s, :m, :r)")


def recipient(contact: Contact) -> tuple[SendChannel, str]:
    """Channel + address from the contact record: the buyer's preference, else WhatsApp, else email."""
    phone = contact.whatsapp_e164 or contact.phone_e164
    if contact.preferred_channel == "email" and contact.email:
        return "email", contact.email
    if phone:
        return "whatsapp", phone
    if contact.email:
        return "email", contact.email
    raise RuleViolation("contact has no WhatsApp number or email to send the quote to")


GrantPolicy = Literal["sales", "manager", "md", "within_range"]  # who released it (D1)


async def issue_grant(
    session: AsyncSession,
    *,
    version: QuoteVersion,
    approval_id: uuid.UUID | None,
    policy: GrantPolicy,
    contact: Contact,
    sha: str,
    s: BusinessSettings,
    now: datetime,
    actor: Actor,
) -> tuple[QuoteSendGrant, SendChannel, str]:
    channel, address = recipient(contact)
    grant = QuoteSendGrant(
        id=new_id(),
        quote_version_id=version.id,
        approval_id=approval_id,
        policy=policy,
        content_sha256=sha,
        channel=channel,
        recipient_hash=recipient_hash(channel, address),
        issued_at=now,
        expires_at=now + timedelta(hours=s.grant_ttl_hours),
    )
    session.add(grant)
    await session.flush()
    await record(
        session,
        actor,
        "grant.issued",
        "quote_send_grant",
        grant.id,
        after={
            "quote_version_id": version.id,
            "channel": channel,
            "policy": policy,
            "expires_at": grant.expires_at,
        },
    )
    return grant, channel, address


async def revoke_open(
    session: AsyncSession, version_ids: Iterable[uuid.UUID], reason: str, now: datetime
) -> None:
    """Superseded or cancelled versions must never be sendable again."""
    ids = list(version_ids)
    if ids:
        await session.execute(
            update(QuoteSendGrant)
            .where(
                QuoteSendGrant.quote_version_id.in_(ids),
                QuoteSendGrant.consumed_at.is_(None),
                QuoteSendGrant.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoked_reason=reason)
        )


async def consume_grant(
    session: AsyncSession,
    s: BusinessSettings,
    *,
    grant_id: uuid.UUID,
    sha: str,
    message_id: uuid.UUID,
    rcpt_hash: str,
    now: datetime,
) -> bool:
    """Atomic consume in SQL. The first successful consume marks the version 'sent' and schedules the
    day 2/5/10 follow-ups; a retry of the same message returns True without repeating side effects."""
    ok = bool(await session.scalar(_CONSUME, {"g": grant_id, "s": sha, "m": message_id, "r": rcpt_hash}))
    actor = Actor.system("grant_consumer")
    if not ok:
        await record(
            session, actor, "grant.refused", "quote_send_grant", grant_id, after={"message_id": message_id}
        )
        return False
    row = (
        await session.execute(
            select(QuoteVersion, Deal)
            .join(QuoteSendGrant, QuoteSendGrant.quote_version_id == QuoteVersion.id)
            .join(Quote, Quote.id == QuoteVersion.quote_id)
            .join(Deal, Deal.id == Quote.deal_id)
            .where(QuoteSendGrant.id == grant_id)
            .with_for_update(of=QuoteVersion)
        )
    ).one()
    version, deal = row.tuple()
    if version.status != "approved":
        return True  # idempotent retry: already sent
    version.status = "sent"
    await record(
        session,
        actor,
        "grant.consumed",
        "quote_send_grant",
        grant_id,
        after={"message_id": message_id, "quote_version_id": version.id},
        timeline=Timeline(
            "quote_sent",
            f"Quote version {version.version_no} sent",
            deal.id,
            deal.primary_contact_id,
            direction="out",
            ref_type="quote_version",
            ref_id=str(version.id),
        ),
    )
    await schedule_followups(session, s, deal, now, actor)
    return True


async def schedule_followups(
    session: AsyncSession, s: BusinessSettings, deal: Deal, sent_at: datetime, actor: Actor
) -> None:
    """Day 2/5/10 (settings) after the quote, moved into the buyer's contact hours. The deal's next
    action is the first one; automation gets all three and enforces the 2-per-week cap."""
    contact = await deal_svc.contact_of(session, deal)
    window = s.buyer_window(contact.timezone)
    for n, days in enumerate(s.followup_cadence_days):
        due = window.next_open(sent_at + timedelta(days=days))
        await deal_svc.set_next_action(session, deal, due, f"followup_d{days}", actor, primary=n == 0)
