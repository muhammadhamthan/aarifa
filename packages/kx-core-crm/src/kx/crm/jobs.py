"""Background work for the CRM: periodic job bodies and event handlers (no job-queue import: the app runs
them on Celery, D25).

Wire in the app:
    rt = CrmRuntime(databases, deps, publish=<publisher that hands crm outbox events to the bus>)
    periodic_jobs(rt)          # name -> coroutine for Beat (schedules in arifaa_pack.schedule)
    HANDLERS / dispatch(rt, e) # events the CRM consumes, run as `crm.handle_event` jobs

Each job runs in one CRM transaction and is safe to run twice: the sweep and the relay use
SKIP LOCKED, handlers are de-duplicated with kx.core.events.inbox_once in the same transaction.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.db import Databases
from kx.core.event_payloads import (
    ConversationSummaryUpdated,
    DealQualified,
    MessageReceived,
    MessageStatus,
    OptedOut,
    PriceProposed,
    ProductPublished,
    QuoteNotDelivered,
    parse_payload,
)
from kx.core.events import Event, Publisher, inbox_once, relay_once
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb
from kx.crm.deps import CrmDeps
from kx.crm.models import Contact, Deal
from kx.crm.models.base import SCHEMA
from kx.crm.rules import OPEN_STAGES
from kx.crm.services import (
    approvals,
    catalogue,
    change_requests,
    consent,
    issuing,
    lost_proposals,
    phone,
    price_proposals,
    quotes,
    retention,
    scheduled_reports,
    summary,
)
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor, Emit, Timeline, record

log = get_logger(__name__)
_STAFF_KINDS: Final = frozenset({"approval_alert", "md_notice"})
_SIGNAL: Final[dict[str, phone.Signal]] = {"delivered": "delivered", "read": "read", "failed": "failed"}
EventHandler = Callable[[AsyncSession, CrmDeps, Event], Awaitable[None]]
SUMMARY_CHARS: Final = 500
AI_SUMMARY_CHARS: Final = 1200


@dataclass(frozen=True, slots=True)
class CrmRuntime:
    databases: Databases
    deps: CrmDeps
    publish: Publisher


# ---------------------------------------------------------------- job bodies (plain async, testable)
async def run_approval_sweep(rt: CrmRuntime) -> int:
    async with rt.databases.transaction(LogicalDb.CRM) as s:
        return await approvals.sweep_overdue(s, rt.deps) + await change_requests.sweep_escalations(s, rt.deps)


async def run_lost_proposals(rt: CrmRuntime) -> int:
    async with rt.databases.transaction(LogicalDb.CRM) as s:
        return await lost_proposals.propose_lost(s, rt.deps)


async def run_price_staleness(rt: CrmRuntime) -> int:
    """Daily: expire quotes past their validity (§24); PRICE_STALE at warn/block level in cost mode (v2)."""
    async with rt.databases.transaction(LogicalDb.CRM) as s:
        settings = await rt.deps.settings.get(s)
        stale = 0
        if settings.pricing_mode == "cost":  # D14: no staleness warning or block in price-list mode (v1)
            stale = await price_proposals.emit_stale(
                s, settings, rt.deps.now(), Actor.system("price_staleness")
            )
        expired = await quotes.expire_quotes(s, rt.deps)
    log.info("crm.price_staleness", stale=stale, expired_quotes=expired)
    return stale


async def run_retention(rt: CrmRuntime) -> dict[str, int]:
    async with rt.databases.transaction(LogicalDb.CRM) as s:
        counts = await retention.run_retention(s, rt.deps)
    log.info("crm.retention", **counts)
    return counts


async def run_scheduled_reports(rt: CrmRuntime) -> int:
    """§21: the daily summary and the weekly report, once each when their time (a setting) has passed."""
    async with rt.databases.transaction(LogicalDb.CRM) as s:
        sent = await scheduled_reports.send_due(s, rt.deps)
    if sent:
        log.info("crm.scheduled_reports", sent=sent)
    return sent


async def run_outbox_relay(rt: CrmRuntime, limit: int = 100) -> int:
    async with rt.databases.transaction(LogicalDb.CRM) as s:
        return await relay_once(s, SCHEMA, rt.publish, limit)


# ---------------------------------------------------------------- event handlers
def once(name: str) -> Callable[[EventHandler], EventHandler]:
    """Run the handler at most once per event (inbox row written in the handler's own transaction)."""

    def wrap(fn: EventHandler) -> EventHandler:
        @wraps(fn)
        async def handler(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
            if await inbox_once(session, SCHEMA, event.event_id, name):
                await fn(session, deps, event)

        return handler

    return wrap


@once("crm.price_proposed")
async def on_price_proposed(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
    """A scraper observation becomes a *proposed* material rate awaiting manager approval."""
    p = parse_payload(event, PriceProposed)
    if await price_proposals.propose_rate(session, p, Actor.system("price_scraper")) is None:
        log.warning("crm.price_proposed.unknown_material", material=p.material_code)


@once("crm.message_received")
async def on_message_received(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
    """Inbound buyer message -> deal timeline (text truncated; full chat stays in automation/agents)."""
    m = parse_payload(event, MessageReceived)
    deal = await _deal_for_message(session, m)
    if deal is None:
        log.info("crm.message_received.unmatched", message_ref=str(m.message_ref))
        return
    # §8.1: a stop arrives as its own event (consent.opted_out.v1 -> on_opted_out); here only the timeline
    if m.channel == "whatsapp":  # §8.2: the buyer replied from that number -> Verified (+ profile name)
        contact = await session.get(Contact, deal.primary_contact_id)
        if contact is not None:
            await phone.apply_signal(session, contact, "reply", m.received_at, profile_name=m.profile_name)
    await record(
        session,
        Actor.system("inbound"),
        "message.received",
        "deal",
        deal.id,
        timeline=Timeline(
            "message_in",
            m.text[:SUMMARY_CHARS] or "(attachment)",
            deal.id,
            deal.primary_contact_id,
            channel=m.channel,
            direction="in",
            ref_type="inbound_message",
            ref_id=str(m.message_ref),
            actor_type="buyer",
        ),
    )


@once("crm.opted_out")
async def on_opted_out(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
    """§8.1 step 8: a stop automation applied (a rule, the unsubscribe link, the 24-hour timer). A stop the
    CRM decided itself (origin crm) is already applied."""
    p = parse_payload(event, OptedOut)
    if p.origin == "crm":
        return
    contact = await session.get(Contact, p.contact_ref) if p.contact_ref else None
    if contact is None and p.address_hashes:
        contact = await deal_svc.contact_by_recipient_hash(session, p.address_hashes[0])
    if contact is None:
        log.info("crm.opted_out.unknown_contact", log_ref=str(p.log_ref))
        return
    deal = await session.get(Deal, p.deal_ref) if p.deal_ref else None
    deal = deal or await _latest_deal(session, contact.id)
    await consent.apply_opt_out(
        session,
        deps,
        deal=deal,
        contact=contact,
        scope=p.scope,
        source=p.source,
        words=p.exact_words,
        reason=p.reason,
        decided_by=p.decided_by,
        channel=p.channel,
        actor=Actor.system(f"opt_out:{p.source}"),
    )
    missing = set(contact.recipient_hashes or ()) - set(p.address_hashes)
    if p.scope == "all" and (missing or p.contact_ref is None):
        # step 2 "stop at once, everywhere": automation knew one address (e.g. the email link); hand it the
        # person and every address the CRM holds
        await record(
            session,
            Actor.system(f"opt_out:{p.source}"),
            "consent.opt_out_extended",
            "contact",
            contact.id,
            emit=Emit(
                ev.CONSENT_OPTED_OUT,
                "contact",
                contact.id,
                p.model_copy(
                    update={
                        "contact_ref": contact.id,
                        "deal_ref": deal.id if deal else p.deal_ref,
                        "address_hashes": tuple(sorted(missing)),
                        "origin": "crm",
                        "log_ref": None,
                    }
                ),
            ),
        )


@once("crm.phone_status")
async def on_message_status(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
    """§8.2 step 3: read WhatsApp's signals silently (Delivered, Read, failed) for messages to the buyer. A
    message our own gate refused (blocked, cancelled) says nothing about the number."""
    p = parse_payload(event, MessageStatus)
    if p.channel != "whatsapp" or p.contact_ref is None or p.kind in _STAFF_KINDS:
        return
    if p.error and p.error.startswith(("blocked:", "cancelled:")):
        return
    signal = _SIGNAL.get(p.status)
    contact = await session.get(Contact, p.contact_ref)
    if signal is None or contact is None:
        return
    await phone.apply_signal(session, contact, signal, p.at)
    if signal == "failed" and p.kind == "summary_pdf" and p.deal_ref is not None:
        deal = await session.get(Deal, p.deal_ref)
        if deal is not None:
            await summary.summary_not_delivered(session, deps, deal)


@once("crm.requirement_summary")
async def on_deal_qualified(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
    """§5 step 3 / §6.5 / §17 row 1: the qualified deal's Requirement Summary goes to the buyer."""
    p = parse_payload(event, DealQualified)
    deal = await session.get(Deal, p.deal_ref)
    if deal is not None:
        await summary.send_requirement_summary(session, deps, deal)


@once("crm.cms_product")
async def on_product_published(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
    """§7 / D22: a CMS publish or unpublish mirrors the descriptive product record by product code."""
    await catalogue.mirror_product(session, parse_payload(event, ProductPublished), deps.now())


@once("crm.quote_not_delivered")
async def on_quote_not_delivered(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
    """D32: the same approved quote by email (its own grant), else the salesperson is told to call."""
    await issuing.resend_by_email(session, deps, parse_payload(event, QuoteNotDelivered))


@once("crm.ai_summary")
async def on_summary_updated(session: AsyncSession, deps: CrmDeps, event: Event) -> None:
    """§7A.2 (D23): the short AI summary at the top of the deal page. A display field, not a business state:
    written without bumping the deal version (staff edits are not refused because the AI re-summarised),
    audited but kept off the timeline (it changes every few turns). An older summary never replaces a newer
    one (events can arrive out of order)."""
    p = parse_payload(event, ConversationSummaryUpdated)
    result = await session.execute(
        update(Deal)
        .where(
            Deal.id == p.deal_ref,
            (Deal.ai_summary_at.is_(None)) | (Deal.ai_summary_at < p.at),
        )
        .values(ai_summary=p.summary[:AI_SUMMARY_CHARS], ai_summary_at=p.at)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0):
        await record(session, Actor.system("ai_summary"), "deal.ai_summary_updated", "deal", p.deal_ref)


async def _latest_deal(session: AsyncSession, contact_id: uuid.UUID) -> Deal | None:
    stmt = (
        select(Deal)
        .where(Deal.primary_contact_id == contact_id)
        .order_by(Deal.stage.in_([s.value for s in OPEN_STAGES]).desc(), Deal.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _deal_for_message(session: AsyncSession, m: MessageReceived) -> Deal | None:
    if m.deal_ref is not None and (deal := await session.get(Deal, m.deal_ref)) is not None:
        return deal
    contact_id = m.contact_ref
    if contact_id is None and (c := await deal_svc.contact_by_recipient_hash(session, m.from_address_hash)):
        contact_id = c.id
    if contact_id is None:
        return None
    stmt = (
        select(Deal)
        .where(Deal.primary_contact_id == contact_id)
        .order_by(Deal.stage.in_([s.value for s in OPEN_STAGES]).desc(), Deal.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


HANDLERS: Final[dict[str, EventHandler]] = {
    ev.PRICE_PROPOSED: on_price_proposed,
    ev.MESSAGE_RECEIVED: on_message_received,
    ev.CONSENT_OPTED_OUT: on_opted_out,
    ev.MESSAGE_STATUS: on_message_status,
    ev.DEAL_QUALIFIED: on_deal_qualified,
    ev.CONVERSATION_SUMMARY_UPDATED: on_summary_updated,
    ev.CATALOGUE_PRODUCT_PUBLISHED: on_product_published,
    ev.QUOTE_NOT_DELIVERED: on_quote_not_delivered,
}


async def dispatch(rt: CrmRuntime, event: Event, handler: EventHandler | None = None) -> bool:
    """Run the CRM handler for `event` in a CRM transaction. False when the CRM does not handle it."""
    fn = handler or HANDLERS.get(event.event_type)
    if fn is None:
        return False
    async with rt.databases.transaction(LogicalDb.CRM) as s:
        await fn(s, rt.deps, event)
    return True


# ---------------------------------------------------------------- periodic jobs (Beat, by name)
Periodic = Callable[[datetime], Awaitable[Any]]


def periodic_jobs(rt: CrmRuntime) -> dict[str, Periodic]:
    """CRM work Beat runs on a timer. Times are UTC in `arifaa_pack.schedule` (Dubai = UTC+4)."""

    async def relay(_: datetime) -> int:
        total = 0
        while (n := await run_outbox_relay(rt)) == 100:  # drain bursts without waiting for the next tick
            total += n
        return total + n

    async def sweep(_: datetime) -> int:
        return await run_approval_sweep(rt)

    async def staleness(_: datetime) -> int:
        return await run_price_staleness(rt)

    async def lost(_: datetime) -> int:
        return await run_lost_proposals(rt)

    async def retention_(_: datetime) -> dict[str, int]:
        return await run_retention(rt)

    async def reports(_: datetime) -> int:
        return await run_scheduled_reports(rt)

    return {
        "relay.crm": relay,
        "crm.approval_sweep": sweep,
        "crm.price_staleness": staleness,
        "crm.lost_proposals": lost,
        "crm.retention": retention_,
        "crm.scheduled_reports": reports,
    }
