"""Background work for the CRM: periodic jobs (procrastinate Blueprint) and event handlers.

Wire in the worker:
    rt = CrmRuntime(databases, deps, publish=<publisher that hands crm outbox events to the bus>)
    app.add_tasks_from(build_blueprint(rt), namespace="crm")
    for name, handler in HANDLERS.items(): bus.subscribe(name, lambda e, h=handler: dispatch(rt, e, h))

Each job runs in one CRM transaction and is safe to run twice: the sweep and the relay use
SKIP LOCKED, handlers are de-duplicated with kx.core.events.inbox_once in the same transaction.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, Final

from procrastinate import Blueprint, RetryStrategy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.db import Databases
from kx.core.event_payloads import MessageReceived, PriceProposed, parse_payload
from kx.core.events import Event, Publisher, inbox_once, relay_once
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb
from kx.crm.deps import CrmDeps
from kx.crm.models import Deal
from kx.crm.models.base import SCHEMA
from kx.crm.rules import OPEN_STAGES
from kx.crm.services import approvals, change_requests, lost_proposals, price_proposals, quotes, retention
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor, Timeline, record

log = get_logger(__name__)
EventHandler = Callable[[AsyncSession, CrmDeps, Event], Awaitable[None]]
SUMMARY_CHARS: Final = 500


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
    if m.opted_out and deal.ai_enabled:  # §8: opt-out stops all messages; §17: the salesperson sees it
        deal.ai_enabled, deal.ai_paused_reason, deal.ai_paused_at = False, "opt_out", deps.now()
        await session.flush()
        await record(
            session,
            Actor.system("inbound"),
            "deal.opted_out",
            "deal",
            deal.id,
            after={"ai_paused_reason": "opt_out"},
            timeline=Timeline(
                "opt_out",
                "Buyer opted out: no more messages on any channel",
                deal.id,
                deal.primary_contact_id,
            ),
        )
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
}


async def dispatch(rt: CrmRuntime, event: Event, handler: EventHandler | None = None) -> bool:
    """Run the CRM handler for `event` in a CRM transaction. False when the CRM does not handle it."""
    fn = handler or HANDLERS.get(event.event_type)
    if fn is None:
        return False
    async with rt.databases.transaction(LogicalDb.CRM) as s:
        await fn(s, rt.deps, event)
    return True


# ---------------------------------------------------------------- procrastinate wiring
def build_blueprint(rt: CrmRuntime) -> Blueprint:
    """Tasks for `app.add_tasks_from(bp, namespace="crm")`. Times are UTC (Dubai = UTC+4)."""
    bp = Blueprint()
    retry = RetryStrategy(max_attempts=5, exponential_wait=5)

    @bp.periodic(cron="*/5 * * * *", periodic_id="approval_sweep")
    @bp.task(name="approval_sweep", queueing_lock="crm.approval_sweep", retry=retry)
    async def approval_sweep(timestamp: int) -> None:
        await run_approval_sweep(rt)

    @bp.periodic(cron="23 5 * * *", periodic_id="price_staleness")  # 09:23 Dubai
    @bp.task(name="price_staleness", queueing_lock="crm.price_staleness", retry=retry)
    async def price_staleness(timestamp: int) -> None:
        await run_price_staleness(rt)

    @bp.periodic(cron="7 4 * * *", periodic_id="lost_proposals")  # 08:07 Dubai, start of staff hours
    @bp.task(name="lost_proposals", queueing_lock="crm.lost_proposals", retry=retry)
    async def lost_proposals_job(timestamp: int) -> None:
        await run_lost_proposals(rt)

    @bp.periodic(cron="41 23 * * *", periodic_id="retention")  # 03:41 Dubai
    @bp.task(name="retention", queueing_lock="crm.retention", retry=retry)
    async def retention_job(timestamp: int) -> None:
        await run_retention(rt)

    @bp.periodic(cron="* * * * * */5", periodic_id="outbox_relay")  # every 5 s (croniter seconds field)
    @bp.task(name="outbox_relay", queueing_lock="crm.outbox_relay")
    async def outbox_relay(timestamp: int) -> None:
        while await run_outbox_relay(rt) == 100:  # drain bursts without waiting for the next tick
            pass

    @bp.task(name="handle_event", retry=retry)
    async def handle_event(event: dict[str, Any]) -> None:
        await dispatch(rt, Event.model_validate(event))

    return bp
