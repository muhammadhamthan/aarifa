"""Event handlers for the app's relay. Each runs at most once per event (`inbox_once` in the agents DB).

The inbox row is written AFTER the handler succeeds instead of in a transaction held open across
LLM calls (seconds, idle-in-transaction). That is safe because every handler is idempotent on its own:
replies via the agent_runs claim + the sender's idempotency key, resumes via the run claim, deletes
and re-indexing by nature.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, runtime_checkable

from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy import text

from kx.agents.knowledge import KnowledgeIndexer
from kx.agents.memory import TurnStore
from kx.agents.orm import SCHEMA
from kx.agents.service import AgentService, Deliver, InboundTurn
from kx.core import events as ev
from kx.core.contracts import MessageSender, OutboundKind, OutboundRequest
from kx.core.db import Databases
from kx.core.event_payloads import (
    ContactAnonymised,
    KbContentPublished,
    MessageReceived,
    QuoteApproved,
    QuoteCorrectionRequested,
    QuoteRejected,
    parse_payload,
)
from kx.core.events import Event, inbox_once
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)
EventHandler = Callable[[Event], Awaitable[None]]

HANDLERS: Final[dict[str, str]] = {  # event type -> stable inbox handler name
    ev.MESSAGE_RECEIVED: "agents.reply",
    ev.QUOTE_APPROVED: "agents.quotation_resume",
    ev.QUOTE_REJECTED: "agents.quotation_resume",
    ev.QUOTE_CORRECTION_REQUESTED: "agents.quotation_resume",
    ev.CONTACT_ANONYMISED: "agents.anonymise",
    ev.KB_CONTENT_PUBLISHED: "agents.kb_reindex",
}


@dataclass(frozen=True, slots=True)
class ReplyAddress:
    channel: Literal["whatsapp", "email"]
    address: str
    contact_ref: uuid.UUID | None = None
    timezone: str | None = None


@runtime_checkable
class ReplyAddressBook(Protocol):
    """App-wired from automation's threads: the recipient never comes from the model."""

    async def for_thread(self, thread_ref: uuid.UUID) -> ReplyAddress | None: ...
    async def for_deal(self, deal_ref: uuid.UUID) -> ReplyAddress | None: ...


@dataclass(frozen=True, slots=True)
class AgentHandlers:
    databases: Databases
    service: AgentService
    sender: MessageSender
    addresses: ReplyAddressBook
    indexer: KnowledgeIndexer | None = None
    checkpointer: BaseCheckpointSaver[Any] | None = None

    def handlers(self) -> dict[str, EventHandler]:
        impl: dict[str, EventHandler] = {
            ev.MESSAGE_RECEIVED: self._message,
            ev.QUOTE_APPROVED: self._quote,
            ev.QUOTE_REJECTED: self._quote,
            ev.QUOTE_CORRECTION_REQUESTED: self._quote,
            ev.CONTACT_ANONYMISED: self._anonymise,
            ev.KB_CONTENT_PUBLISHED: self._kb,
        }
        return {t: self._once(HANDLERS[t], h) for t, h in impl.items()}

    async def dispatch(self, event: Event) -> bool:
        handler = self.handlers().get(event.event_type)
        if handler is None:
            return False
        await handler(event)
        return True

    def _once(self, name: str, handler: EventHandler) -> EventHandler:
        async def run(event: Event) -> None:
            async with self.databases.transaction(LogicalDb.AGENTS) as s:
                seen = (
                    await s.execute(
                        text("SELECT 1 FROM inbox_events WHERE event_id = :e AND handler = :h"),
                        {"e": event.event_id, "h": name},
                    )
                ).first()
            if seen is not None:
                return
            await handler(event)
            async with self.databases.transaction(LogicalDb.AGENTS) as s:
                await inbox_once(s, SCHEMA, event.event_id, name)

        return run

    def _deliver(self, addr: ReplyAddress, deal_ref: uuid.UUID, key: str) -> Deliver:
        async def send(body: str, language: str) -> None:
            await self.sender.send(
                OutboundRequest(
                    channel=addr.channel,
                    to_address=addr.address,
                    kind=OutboundKind.REPLY,
                    body=body,
                    deal_ref=deal_ref,
                    contact_ref=addr.contact_ref,
                    buyer_timezone=addr.timezone,
                    language=language,
                    requested_by="agent:reply",
                    idempotency_key=key,
                )
            )

        return send

    async def _message(self, event: Event) -> None:
        m = parse_payload(event, MessageReceived)
        addr = await self.addresses.for_thread(m.thread_ref)
        deal_ref = m.deal_ref
        if deal_ref is None and addr is not None:
            deal_ref = await self.service.gateway.resolve_deal(addr.channel, addr.address, None)
        if deal_ref is None or not m.text.strip():
            log.info("agents.message_skipped", message_ref=str(m.message_ref))
            return
        if m.opted_out:  # §8.1 steps 2-3: the stop already happened; the one confirmation is all that leaves
            log.info("agents.opted_out_no_reply", message_ref=str(m.message_ref))
            return
        deliver = self._deliver(addr, deal_ref, f"agent-reply:{event.event_id}") if addr else None
        await self.service.handle_turn(
            InboundTurn(
                channel=m.channel,
                text=m.text,
                deal_id=deal_ref,
                thread_key=f"{m.channel}:{m.thread_ref}",
                trigger_event=event.event_type,
                trigger_event_id=event.event_id,
                contact_ref=m.contact_ref,
                stop_word=m.stop_in_sentence,
            ),
            deliver,
        )

    async def _quote(self, event: Event) -> None:
        if event.event_type == ev.QUOTE_APPROVED:
            p = parse_payload(event, QuoteApproved)
            deal_ref, outcome = p.deal_ref, {"kind": "approved", "approval_kind": p.kind}
        elif event.event_type == ev.QUOTE_REJECTED:
            r = parse_payload(event, QuoteRejected)
            deal_ref, outcome = r.deal_ref, {"kind": "rejected", "note": r.reason}
        else:
            c = parse_payload(event, QuoteCorrectionRequested)
            deal_ref, outcome = c.deal_ref, {"kind": "correction", "note": c.note}
        addr = await self.addresses.for_deal(deal_ref)
        deliver = self._deliver(addr, deal_ref, f"agent-quote:{event.event_id}") if addr else None
        await self.service.resume_quotation(
            deal_ref,
            outcome,
            trigger_event=event.event_type,
            trigger_event_id=event.event_id,
            deliver=deliver,
        )

    async def _anonymise(self, event: Event) -> None:
        p = parse_payload(event, ContactAnonymised)
        async with self.databases.transaction(LogicalDb.AGENTS) as s:
            deals = await TurnStore.anonymise(s, p.contact_ref)
        if self.checkpointer is not None:
            for deal_ref in deals:
                await self.checkpointer.adelete_thread(str(deal_ref))

    async def _kb(self, event: Event) -> None:
        if self.indexer is not None:
            p = parse_payload(event, KbContentPublished)
            await self.indexer.reindex(p.collection, p.doc_id, p.locale)
