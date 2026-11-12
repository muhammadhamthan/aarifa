"""§14 "same channel, same number" (D21): after hand-over the salesperson writes and calls from the CRM.

* **Reply:** a text to the buyer on the deal's channel and company number, signed "— Sara, Arifa Pack"
  (WhatsApp) or with the salesperson's name as the sender of a company-address email. It goes out through
  `OutboundService` like every other message (§4), so consent, opt-out and WhatsApp's 24-hour rule apply.
  After 24 h without a buyer message only the approved re-open template may restart the chat (§14 practical
  notes); the refusal tells the screen to offer it.
* **No price outside an approved quote:** the price guard (`kx.core.price_guard`) runs on staff text too.
  Any amount that is not in the latest issued quote or an issued offer is refused before anything leaves.
  Percentages are not checked here: advance and balance terms are discussed by people (§14 step 7, §15).
* **Click-to-call:** rings the salesperson's phone, then bridges to the buyer from the company number; the
  call is logged on the deal (no personal phones).
* Writing to the buyer is a human takeover: the AI stops replying on the deal (§12 "take over").
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal
from kx.core.contracts import OutboundKind, OutboundRequest
from kx.core.errors import RuleViolation, Unavailable
from kx.core.price_guard import check_prices
from kx.crm.deps import CrmDeps
from kx.crm.gateway import build_context
from kx.crm.models import Deal, User
from kx.crm.services import access, issuing
from kx.crm.services import deals as deal_svc
from kx.crm.services.grants import SendChannel, recipient, recipient_on
from kx.crm.services.journal import Actor, Timeline, record

SIGNATURE: Final = "— {name}, {company}"
TIMELINE_CHARS: Final = 500


async def _author(session: AsyncSession, p: Principal) -> User:
    user = await session.get(User, p.user_id) if p.user_id else None
    if user is None or not user.is_active:
        raise RuleViolation("this staff account cannot write to buyers", reason="no_user")
    return user


def signed(text: str, name: str, company: str) -> str:
    """§14: "messages are signed with the salesperson's name ('— Sara, Arifa Pack')"."""
    return f"{text.rstrip()}\n\n{SIGNATURE.format(name=name, company=company)}"


async def _take_over(session: AsyncSession, deps: CrmDeps, deal: Deal, actor: Actor) -> None:
    """A person writing to the buyer means the AI must not answer on this deal any more."""
    if not deal.ai_enabled:
        return
    deal.ai_enabled, deal.ai_paused_reason, deal.ai_paused_at = False, "human_takeover", deps.now()
    await session.flush()
    await record(
        session,
        actor,
        "deal.ai_paused",
        "deal",
        deal.id,
        after={"reason": "human_takeover"},
        timeline=Timeline(
            "ai_paused", "AI paused: a salesperson is replying", deal.id, deal.primary_contact_id
        ),
    )


async def _check_amounts(session: AsyncSession, deps: CrmDeps, deal: Deal, text: str) -> None:
    allowed = (await build_context(session, deps, deal.id)).allowed_prices
    result = check_prices(text, allowed, default_currency=deal.currency)
    amounts = [m.raw for m in result.violations if not m.is_percentage]
    if amounts:
        raise RuleViolation(
            "a price that is not in an approved quote cannot be sent; revise the quote instead",
            reason="unapproved_price",
            amounts=amounts,
        )


async def reply(
    session: AsyncSession,
    deps: CrmDeps,
    deal: Deal,
    p: Principal,
    *,
    text: str,
    client_key: str,
    channel: SendChannel | None = None,
    reopen_template: bool = False,
) -> uuid.UUID:
    """Send one staff message to the buyer; returns the outbound message id (idempotent per
    `client_key`, so a double-tapped Send goes once). `reopen_template` sends the approved §14 template
    instead of `text`, for a WhatsApp chat whose 24-hour window has closed."""
    await access.check_contact_buyer(session, p, deal)  # who may contact the buyer, before any infra check
    if deps.sender is None:
        raise Unavailable("sending is not configured")
    if deal.opted_out_at is not None:
        raise RuleViolation("the buyer opted out; nothing can be sent (§8.1)", reason="opt_out")
    author = await _author(session, p)
    contact = await deal_svc.contact_of(session, deal)
    chosen, address = recipient_on(contact, channel) if channel else recipient(contact)
    company = deps.letterhead.name
    settings = await deps.settings.get(session)
    request = OutboundRequest(
        channel=chosen,
        to_address=address,
        kind=OutboundKind.STAFF_REPLY,
        deal_ref=deal.id,
        contact_ref=contact.id,
        buyer_timezone=contact.timezone,
        buyer_country=await issuing.buyer_country(session, deal),
        language=contact.language,
        requested_by=f"user:{author.id}",
        idempotency_key=f"staff-reply:{deal.id}:{client_key}",
        sender_name=f"{author.full_name}, {company}" if chosen == "email" else None,
    )
    if reopen_template:
        if chosen != "whatsapp":
            raise RuleViolation("the re-open template is for WhatsApp only", reason="template")
        request = request.model_copy(
            update={
                "template_name": settings.handover_reopen_template,
                "template_params": (author.full_name, company),
            }
        )
        shown = f"[re-open template] {author.full_name}, {company}"
    else:
        if not text.strip():
            raise RuleViolation("the message is empty", reason="empty")
        await _check_amounts(session, deps, deal, text)
        body = signed(text, author.full_name, company) if chosen == "whatsapp" else text
        request = request.model_copy(
            update={"body": body, "subject": f"{company} — {deal.number}" if chosen == "email" else None}
        )
        shown = text
    message_id = await deps.sender.send(request)
    actor = Actor("user", str(author.id), author.id)
    await _take_over(session, deps, deal, actor)
    await record(
        session,
        actor,
        "deal.staff_replied",
        "deal",
        deal.id,
        after={"channel": chosen, "message_ref": str(message_id), "template": reopen_template},
        timeline=Timeline(
            "message_out",
            f"{author.full_name}: {shown}"[:TIMELINE_CHARS],
            deal.id,
            deal.primary_contact_id,
            channel=chosen,
            direction="out",
            ref_type="outbound_message",
            ref_id=str(message_id),
        ),
    )
    return message_id


async def call_buyer(
    session: AsyncSession, deps: CrmDeps, deal: Deal, p: Principal, *, client_key: str
) -> uuid.UUID:
    """§14 "click-to-call from the CRM through the company number. The call is logged on the deal"."""
    await access.check_contact_buyer(session, p, deal)  # who may contact the buyer, before any infra check
    if deps.caller is None:
        raise Unavailable("calling is not configured")
    author = await _author(session, p)
    if not author.phone_e164:
        raise RuleViolation("add your phone number to your profile to place calls", reason="no_staff_phone")
    contact = await deal_svc.contact_of(session, deal)
    buyer = contact.phone_e164 or contact.whatsapp_e164
    if not buyer:
        raise RuleViolation("the buyer has no phone number", reason="no_address")
    attempt = await deps.caller.bridge(
        deal_ref=deal.id,
        contact_ref=contact.id,
        staff_e164=author.phone_e164,
        buyer_e164=buyer,
        idempotency_key=f"click-to-call:{deal.id}:{client_key}",
    )
    await record(
        session,
        Actor("user", str(author.id), author.id),
        "deal.call_started",
        "deal",
        deal.id,
        after={"call_ref": str(attempt)},
        timeline=Timeline(
            "call",
            f"{author.full_name} called the buyer from the company number",
            deal.id,
            deal.primary_contact_id,
            channel="phone",
            direction="out",
            ref_type="call_attempt",
            ref_id=str(attempt),
        ),
    )
    return attempt
