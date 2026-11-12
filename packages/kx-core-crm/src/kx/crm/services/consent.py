"""§8.1 STOP in the CRM (D18): consent rows, the Opted out badge, notices and the LOST proposal.

Two ways in, one rule set:
* automation matched a rule (or the unsubscribe link, or the 24-hour timer) and already suppressed the
  addresses -> `consent.opted_out.v1` (origin automation) -> `apply_opt_out` here;
* the AI read the meaning of a sentence (step 6) or a salesperson recorded it -> `record_opt_out` applies
  it here AND announces `consent.opted_out.v1` (origin crm, with every address of the person) so automation
  suppresses, cancels the queue and sends the one confirmation.

The AI may add a stop, never remove one: nothing in this module lifts a stop. Step 7 ("the buyer writes again
later") restarts a normal conversation; follow-ups resume only after a fresh, logged consent (a person).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.contracts import DealStage
from kx.core.event_payloads import ConsentScope, OptedOut, StaffNotice, StopSource, StopUnclear
from kx.crm.deps import CrmDeps
from kx.crm.models import Consent, Contact, Deal, Task
from kx.crm.rules import OPEN_STAGES
from kx.crm.services import staff
from kx.crm.services.journal import Actor, Emit, Timeline, record

# CRM consent channels per stop scope (crm.consents CHECK: whatsapp, email, call, marketing ...)
_CONSENT_CHANNELS: Final[dict[str, tuple[str, ...]]] = {
    "all": ("whatsapp", "email", "call", "marketing"),
    "whatsapp": ("whatsapp",),
    "email": ("email",),
    "phone": ("call",),
}
_SCOPE_TEXT: Final = {
    "all": "Buyer opted out: no more messages or calls on any channel",
    "whatsapp": "Buyer asked to stop WhatsApp (email only from now on)",
    "email": "Buyer asked to stop email",
    "phone": "Buyer asked not to be called",
}


async def apply_opt_out(
    session: AsyncSession,
    deps: CrmDeps,
    *,
    deal: Deal | None,
    contact: Contact,
    scope: ConsentScope,
    source: StopSource,
    words: str | None,
    reason: str | None,
    decided_by: str,
    channel: str,
    actor: Actor,
) -> None:
    now = deps.now()
    for consent_channel in _CONSENT_CHANNELS[scope]:
        session.add(
            Consent(
                contact_id=contact.id,
                channel=consent_channel,
                status="revoked",
                wording=(words or "(unsubscribe link)")[:2000],
                evidence={"source": source, "decided_by": decided_by, "scope": scope, "reason": reason},
                captured_via=channel,
            )
        )
    if scope == "whatsapp" and contact.email:  # "use email instead": switch the channel (step 6)
        contact.preferred_channel = "email"
    elif scope == "email" and (contact.whatsapp_e164 or contact.phone_e164):
        contact.preferred_channel = "whatsapp"
    await session.flush()
    if deal is None:
        return
    if scope != "all":
        await record(
            session,
            actor,
            "consent.channel_stopped",
            "deal",
            deal.id,
            after={"scope": scope, "source": source},
            timeline=Timeline("opt_out", _SCOPE_TEXT[scope], deal.id, contact.id, channel=channel),
        )
        return
    await _opted_out_badge(session, deps, deal, contact, source=source, reason=reason, actor=actor, now=now)


async def _opted_out_badge(
    session: AsyncSession,
    deps: CrmDeps,
    deal: Deal,
    contact: Contact,
    *,
    source: StopSource,
    reason: str | None,
    actor: Actor,
    now: datetime,
) -> None:
    """Step 8: Opted out badge; salesperson and manager notified; LOST proposed (reason Opted out, or the
    buyer's own) for the salesperson to confirm. Step 2: the AI stops."""
    first = deal.opted_out_at is None
    deal.opted_out_at = deal.opted_out_at or now
    if deal.ai_enabled or deal.ai_paused_reason != "opt_out":
        deal.ai_enabled, deal.ai_paused_reason, deal.ai_paused_at = False, "opt_out", now
    await session.flush()
    if not first:
        return
    owner = await staff.approver_for(session, deal)
    manager = await staff.manager_user(session)
    notify = tuple({u.id: staff.staff_contact(u) for u in (owner, manager)}.values())
    why = f" ({reason})" if reason else ""
    await record(
        session,
        actor,
        "deal.opted_out",
        "deal",
        deal.id,
        after={"opted_out_at": now, "source": source, "reason": reason},
        timeline=Timeline("opt_out", _SCOPE_TEXT["all"] + why, deal.id, contact.id),
        emit=Emit(
            ev.STAFF_NOTICE,
            "deal",
            deal.id,
            StaffNotice(
                deal_ref=deal.id,
                notify=notify,
                text=f"Deal {deal.number}: the buyer opted out{why}. LOST is proposed for you to confirm.",
                key=f"opted-out:{deal.id}",
            ),
        ),
    )
    if DealStage(deal.stage) in OPEN_STAGES and not await _open_lost_proposal(session, deal.id):
        session.add(
            Task(
                deal_id=deal.id,
                kind="lost_proposal",
                assigned_user_id=owner.id,
                due_at=now,
                payload={"lost_reason": "opted_out", "buyer_reason": reason, "source": source},
            )
        )
        await session.flush()


async def _open_lost_proposal(session: AsyncSession, deal_id: uuid.UUID) -> bool:
    found = await session.scalar(
        select(Task.id).where(Task.deal_id == deal_id, Task.kind == "lost_proposal", Task.status == "open")
    )
    return found is not None


async def record_opt_out(
    session: AsyncSession,
    deps: CrmDeps,
    deal: Deal,
    *,
    scope: ConsentScope,
    words: str,
    reason: str | None,
    decided_by: str,
    source: StopSource,
    actor: Actor,
) -> None:
    """A stop decided in the CRM (the AI read the meaning, step 6; or a salesperson after a conversation the
    buyer started, step 4): apply it here and hand it to automation with every address of the person."""
    contact = await session.get(Contact, deal.primary_contact_id)
    assert contact is not None  # FK
    await apply_opt_out(
        session,
        deps,
        deal=deal,
        contact=contact,
        scope=scope,
        source=source,
        words=words,
        reason=reason,
        decided_by=decided_by,
        channel="crm",
        actor=actor,
    )
    await record(
        session,
        actor,
        "consent.opted_out",
        "deal",
        deal.id,
        after={"scope": scope, "source": source, "decided_by": decided_by},
        emit=Emit(
            ev.CONSENT_OPTED_OUT,
            "deal",
            deal.id,
            OptedOut(
                contact_ref=contact.id,
                deal_ref=deal.id,
                address_hashes=tuple(contact.recipient_hashes or ()),
                scope=scope,
                source=source,
                channel="crm",
                exact_words=words,
                reason=reason,
                language=contact.language,
                decided_by=decided_by,
                at=deps.now(),
                origin="crm",
            ),
        ),
    )


async def flag_unclear_stop(
    session: AsyncSession, deps: CrmDeps, deal: Deal, *, words: str, live: bool, actor: Actor
) -> None:
    """Step 6, unclear: follow-ups pause at once. Live -> the AI asks one question and no answer within
    `optout.unclear_answer_hours` is an opt-out. Not live -> the salesperson reviews it (a task)."""
    settings = await deps.settings.get(session)
    now = deps.now()
    owner = await staff.approver_for(session, deal)
    if not live:
        session.add(
            Task(
                deal_id=deal.id,
                kind="optout_review",
                assigned_user_id=owner.id,
                due_at=now,
                payload={"buyer_text": words[:2000]},
            )
        )
        await session.flush()
    answer_by = now + settings.optout_unclear_answer if live else None
    await record(
        session,
        actor,
        "consent.stop_unclear",
        "deal",
        deal.id,
        after={"live": live},
        timeline=Timeline(
            "opt_out",
            (
                "Unclear stop: the AI asked whether to stop messaging; follow-ups paused"
                if live
                else f"Unclear stop for {owner.full_name} to review; follow-ups paused"
            )
            + f": “{words[:300]}”",
            deal.id,
            deal.primary_contact_id,
        ),
        emit=Emit(
            ev.CONSENT_STOP_UNCLEAR,
            "deal",
            deal.id,
            StopUnclear(
                contact_ref=deal.primary_contact_id,
                deal_ref=deal.id,
                live=live,
                exact_words=words,
                asked_at=now,
                answer_by=answer_by,
            ),
        ),
    )
