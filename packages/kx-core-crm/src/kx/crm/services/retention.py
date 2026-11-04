"""Retention (T20): lost leads anonymised after 12 months, open leads idle 24 months closed and
anonymised, won customers kept 5 years after their last win.

Anonymising blanks the person's identifiers in the CRM and emits CONTACT_ANONYMISED with the recipient
hashes computed BEFORE blanking, so automation and agents can purge their own copies (chats, audio,
transcripts) without the CRM ever sending them the raw address. Deal and order rows stay for reporting.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import ColumnElement, Exists, Select, and_, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.contracts import DealStage
from kx.core.event_payloads import ContactAnonymised
from kx.core.identity import recipient_hash
from kx.crm.deps import CrmDeps
from kx.crm.models import Company, Contact, Deal, TimelineEvent
from kx.crm.rules import OPEN_STAGES
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor, Emit, record

MESSAGE_KINDS: Final = ("message_in", "message_out", "call")
BATCH: Final = 200


def months_ago(now: datetime, months: int) -> datetime:
    return now - timedelta(days=round(months * 30.4375))


def contact_hashes(c: Contact) -> tuple[str, ...]:
    pairs = (("phone", c.phone_e164), ("whatsapp", c.whatsapp_e164), ("email", c.email))
    return tuple(sorted({recipient_hash(ch, addr) for ch, addr in pairs if addr}))  # type: ignore[arg-type]


async def anonymise_contact(session: AsyncSession, contact: Contact, reason: str, now: datetime) -> None:
    hashes = contact_hashes(contact)
    for attr in ("full_name", "designation", "phone_e164", "whatsapp_e164", "email", "visitor_ref"):
        setattr(contact, attr, None)
    contact.anonymised_at = now
    await session.execute(
        update(TimelineEvent)
        .where(TimelineEvent.contact_id == contact.id, TimelineEvent.kind.in_(MESSAGE_KINDS))
        .values(summary="[removed under retention policy]")
    )
    if contact.company_id is not None:
        others = await session.scalar(
            select(func.count()).where(
                Contact.company_id == contact.company_id,
                Contact.id != contact.id,
                Contact.anonymised_at.is_(None),
            )
        )
        if not others:
            await session.execute(
                update(Company)
                .where(Company.id == contact.company_id)
                .values(name="[anonymised]", vat_trn=None, website=None, notes=None, anonymised_at=now)
            )
    await session.flush()
    await record(
        session,
        Actor.system("retention"),
        "contact.anonymised",
        "contact",
        contact.id,
        after={"reason": reason},
        emit=Emit(
            ev.CONTACT_ANONYMISED,
            "contact",
            contact.id,
            ContactAnonymised(contact_ref=contact.id, recipient_hashes=hashes),
        ),
    )


def _live_contacts() -> Select[Contact]:
    return select(Contact).where(Contact.anonymised_at.is_(None))


def _has_deal(*conds: ColumnElement[bool]) -> Exists:
    return exists().where(Deal.primary_contact_id == Contact.id, *conds)


async def run_retention(session: AsyncSession, deps: CrmDeps) -> dict[str, int]:
    """One pass of all three policies (daily job). Returns counts per policy."""
    now = deps.now()
    s = await deps.settings.get(session)
    open_stages = [st.value for st in OPEN_STAGES]
    lost_cut = months_ago(now, s.retention_lost_lead_months)
    idle_cut = months_ago(now, s.retention_open_lead_chat_months)
    won_cut = now - timedelta(days=round(s.retention_won_customer_years * 365.25))
    counts = {"lost": 0, "idle": 0, "won": 0}

    # 1. lost leads: every deal lost > 12 months ago, nothing open or won since
    lost = (
        _live_contacts()
        .where(
            _has_deal(Deal.stage == DealStage.LOST.value, Deal.lost_at < lost_cut),
            ~_has_deal(Deal.stage.in_([*open_stages, DealStage.WON.value])),
            ~_has_deal(Deal.lost_at >= lost_cut),
        )
        .limit(BATCH)
    )
    for c in (await session.execute(lost)).scalars():
        await anonymise_contact(session, c, "lost_lead", now)
        counts["lost"] += 1

    # 2. open leads with no activity for 24 months: close as LOST (no response) and anonymise
    idle = (
        select(Deal)
        .join(Contact, Contact.id == Deal.primary_contact_id)
        .where(
            Contact.anonymised_at.is_(None),
            Deal.stage.in_(open_stages),
            Deal.updated_at < idle_cut,
            ~exists().where(TimelineEvent.deal_id == Deal.id, TimelineEvent.at >= idle_cut),
        )
        .limit(BATCH)
        .with_for_update(of=Deal, skip_locked=True)
    )
    actor = Actor.system("retention")
    for deal in list((await session.execute(idle)).scalars()):
        await deal_svc.move_stage(
            session,
            deal,
            DealStage.LOST,
            actor,
            now=now,
            lost_reason="no_response_postponed",
            reason="retention: idle 24 months",
        )
        await anonymise_contact(session, await deal_svc.contact_of(session, deal), "idle_open_lead", now)
        counts["idle"] += 1

    # 3. won customers: last win older than 5 years and nothing open
    last_win = (
        select(func.max(Deal.won_at))
        .where(Deal.primary_contact_id == Contact.id)
        .correlate(Contact)
        .scalar_subquery()
    )
    won = (
        _live_contacts()
        .where(and_(last_win.is_not(None), last_win < won_cut), ~_has_deal(Deal.stage.in_(open_stages)))
        .limit(BATCH)
    )
    for c in (await session.execute(won)).scalars():
        await anonymise_contact(session, c, "won_customer_expired", now)
        counts["won"] += 1
    return counts


__all__ = ["anonymise_contact", "contact_hashes", "months_ago", "run_retention"]
