"""Propose LOST after silence (the SRS §5 step 15, §16, §23 scenario 9; D15).

"No reply 14 days after day 10: the system proposes LOST (no response) and the salesperson confirms."
The proposal is a `lost_proposal` task for the deal's salesperson; the deal never moves by itself.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.crm.deps import CrmDeps
from kx.crm.models import Deal, Quote, QuoteSendGrant, QuoteVersion, Task, TimelineEvent
from kx.crm.rules import lost_proposal_due
from kx.crm.services import staff
from kx.crm.services.journal import Actor, Timeline, record

AWAITING = ("QUOTATION", "NEGOTIATION")


async def propose_lost(session: AsyncSession, deps: CrmDeps, now: datetime | None = None) -> int:
    """Daily: one open `lost_proposal` task per silent deal (idempotent)."""
    now = now or deps.now()
    s = await deps.settings.get(session)
    last_sent = (
        select(Quote.deal_id, func.max(QuoteSendGrant.consumed_at).label("sent_at"))
        .join(QuoteVersion, QuoteVersion.quote_id == Quote.id)
        .join(QuoteSendGrant, QuoteSendGrant.quote_version_id == QuoteVersion.id)
        .where(QuoteSendGrant.consumed_at.is_not(None))
        .group_by(Quote.deal_id)
        .subquery()
    )
    replied = exists().where(
        TimelineEvent.deal_id == Deal.id,
        TimelineEvent.kind == "message_in",
        TimelineEvent.at > last_sent.c.sent_at,
    )
    proposed = exists().where(
        and_(Task.deal_id == Deal.id, Task.kind == "lost_proposal", Task.status == "open")
    )
    rows = (
        await session.execute(
            select(Deal, last_sent.c.sent_at)
            .join(last_sent, last_sent.c.deal_id == Deal.id)
            .where(Deal.stage.in_(AWAITING), ~replied, ~proposed)
            .with_for_update(of=Deal, skip_locked=True)
        )
    ).tuples()
    made = 0
    for deal, sent_at in list(rows):
        due = lost_proposal_due(sent_at, s.followup_cadence_days, s.followup_lost_after_days)
        if due > now:
            continue
        owner = await staff.approver_for(session, deal)
        session.add(
            Task(
                deal_id=deal.id,
                kind="lost_proposal",
                assigned_user_id=owner.id,
                due_at=now,
                payload={"lost_reason": "no_response_postponed", "quote_sent_at": sent_at.isoformat()},
            )
        )
        await session.flush()
        await record(
            session,
            Actor.system("lost_proposal"),
            "deal.lost_proposed",
            "deal",
            deal.id,
            timeline=Timeline(
                "lost_proposal",
                f"No reply after the last follow-up: LOST (no response) proposed to {owner.full_name}",
                deal.id,
                deal.primary_contact_id,
            ),
        )
        made += 1
    return made
