"""Versioned domain events, the transactional outbox, inbox de-duplication and an in-memory bus.

Write path:   service changes rows  +  Outbox.add(session, event)   -> one commit
Relay:        relay_once() claims unpublished rows (SKIP LOCKED) and hands them to a publisher
Consume:      handlers wrap work in `inbox_once()` so a redelivered event is a no-op
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

import orjson
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.identity import new_id

# ---- event catalogue (name.vN). Add new versions; never change an existing payload. ----------
DEAL_QUALIFIED: Final = "deal.qualified.v1"
DEAL_STAGE_CHANGED: Final = "deal.stage_changed.v1"
DEAL_NEXT_ACTION_SET: Final = "deal.next_action_set.v1"
NEGOTIATION_PAUSED: Final = "negotiation.paused.v1"
QUOTE_SUBMITTED: Final = "quote.submitted.v1"
QUOTE_APPROVED: Final = "quote.approved.v1"
QUOTE_REJECTED: Final = "quote.rejected.v1"
QUOTE_CORRECTION_REQUESTED: Final = "quote.correction_requested.v1"
APPROVAL_OVERDUE: Final = "approval.overdue.v1"
MESSAGE_RECEIVED: Final = "message.received.v1"
MESSAGE_STATUS: Final = "message.status.v1"
PRICE_PROPOSED: Final = "price.proposed.v1"
PRICE_STALE: Final = "price.stale.v1"
ORDER_STATUS_CHANGED: Final = "order.status_changed.v1"
CONTACT_ANONYMISED: Final = "contact.anonymised.v1"
SETTINGS_CHANGED: Final = "settings.changed.v1"
LANE_RISK_ASSESSED: Final = "lane.risk_assessed.v1"
KB_CONTENT_PUBLISHED: Final = "kb.content_published.v1"
AGENT_RUN_COMPLETED: Final = "agent.run_completed.v1"
CHANGE_REQUESTED: Final = "change_request.raised.v1"  # §13 / D12: a buyer change for the salesperson
CHANGE_REQUEST_CLOSED: Final = "change_request.closed.v1"  # resolved (new version) or declined
DEAL_ASSIGNED: Final = "deal.assigned.v1"  # §9 assignment, reassignment, MD takeover and hand-back
DEAL_WON: Final = "deal.won.v1"  # §17 "Payment recorded / WON": manager + MD notified, buyer thanked
# ---- 11 Nov SRS (docs/V1_CHANGE_PLAN.md WP14-WP18)
CONSENT_OPTED_OUT: Final = "consent.opted_out.v1"  # §8.1: a stop (all channels or one), from any source
CONSENT_STOP_UNCLEAR: Final = "consent.stop_unclear.v1"  # §8.1 step 6: follow-ups pause; ask or review
STAFF_NOTICE: Final = "staff.notice_requested.v1"  # a one-line notice to named staff (alert hours)
DEAL_REQUIREMENT_SUMMARY: Final = (
    "deal.requirement_summary.v1"  # §6.5 / §8.2: the PDF that confirms the number
)
# D32: a WhatsApp quote not delivered in 10 minutes goes by email (its own grant)
QUOTE_NOT_DELIVERED: Final = "quote.not_delivered.v1"
QUOTE_RESEND_REQUESTED: Final = "quote.resend_requested.v1"
CATALOGUE_PRODUCT_PUBLISHED: Final = "catalogue.product_published.v1"  # §7: CMS product -> CRM by code
CONVERSATION_SUMMARY_UPDATED: Final = "conversation.summary_updated.v1"  # §7A.2: AI summary on the deal


class Event(BaseModel):
    """Envelope shared by every event."""

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID = Field(default_factory=new_id)
    event_type: str
    event_version: int = 1
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: uuid.UUID | None = None
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Outbox:
    """Writes events in the caller's transaction (so state and event commit together)."""

    @staticmethod
    async def add(session: AsyncSession, schema: str, event: Event) -> None:
        await session.execute(
            text(
                f"INSERT INTO {_ident(schema)}.outbox_events "  # noqa: S608 - schema is validated
                "(id, event_type, event_version, correlation_id, aggregate_type, aggregate_id, payload) "
                "VALUES (:id, :t, :v, :c, :at, :aid, CAST(:p AS jsonb))"
            ),
            {
                "id": event.event_id,
                "t": event.event_type,
                "v": event.event_version,
                "c": event.correlation_id,
                "at": event.aggregate_type,
                "aid": event.aggregate_id,
                "p": orjson.dumps(event.payload, default=str).decode(),
            },
        )


Publisher = Callable[[Event], Awaitable[None]]


async def relay_once(session: AsyncSession, schema: str, publish: Publisher, limit: int = 100) -> int:
    """Claim up to `limit` unpublished events in order, publish them, mark published. Returns count."""
    rows = (
        (
            await session.execute(
                text(
                    f"SELECT id, event_type, event_version, correlation_id, aggregate_type, aggregate_id, "  # noqa: S608
                    f"payload, created_at FROM {_ident(schema)}.outbox_events "
                    "WHERE published_at IS NULL ORDER BY sequence LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"n": limit},
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        await publish(
            Event(
                event_id=row["id"],
                event_type=row["event_type"],
                event_version=row["event_version"],
                correlation_id=row["correlation_id"],
                aggregate_type=row["aggregate_type"],
                aggregate_id=row["aggregate_id"],
                payload=row["payload"],
                occurred_at=row["created_at"],
            )
        )
    if rows:
        await session.execute(
            text(
                f"UPDATE {_ident(schema)}.outbox_events SET published_at = now(), "  # noqa: S608
                "attempts = attempts + 1 WHERE id = ANY(:ids)"
            ),
            {"ids": [r["id"] for r in rows]},
        )
    return len(rows)


async def inbox_once(session: AsyncSession, schema: str, event_id: uuid.UUID, handler: str) -> bool:
    """True the first time (handler should run); False if this handler already processed the event."""
    result = await session.execute(
        text(
            f"INSERT INTO {_ident(schema)}.inbox_events (event_id, handler) VALUES (:e, :h) "  # noqa: S608
            "ON CONFLICT DO NOTHING RETURNING 1"
        ),
        {"e": event_id, "h": handler},
    )
    return result.scalar_one_or_none() is not None


Handler = Callable[[Event], Awaitable[None]]


class InMemoryBus:
    """Synchronous fan-out bus for tests and single-process local runs."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self.published: list[Event] = []

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subs[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        for handler in self._subs.get(event.event_type, ()):
            await handler(event)

    @property
    def subscriptions(self) -> Mapping[str, list[Handler]]:
        return self._subs


_SCHEMAS: Final = frozenset({"crm", "automation", "agents"})


def _ident(schema: str) -> str:
    if schema not in _SCHEMAS:
        raise ValueError(f"unknown schema {schema!r}")
    return schema
