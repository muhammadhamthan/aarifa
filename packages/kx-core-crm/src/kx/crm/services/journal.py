"""The one place a state change is recorded: audit row + optional timeline row + optional outbox event,
all in the caller's transaction, so a change and its trail commit (or roll back) together.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal
from kx.core.contracts import AiRole
from kx.core.errors import Forbidden
from kx.core.event_payloads import EventPayload, make_event
from kx.core.events import Outbox
from kx.core.logging import trace_id_var
from kx.crm.models import AuditEvent, TimelineEvent
from kx.crm.models.base import SCHEMA

ActorType = Literal["user", "ai", "system"]


@dataclass(frozen=True, slots=True)
class Actor:
    """Who caused a change. `id` is a user uuid, an AI role name or a system job name."""

    type: ActorType
    id: str
    user_id: uuid.UUID | None = None

    @classmethod
    def ai(cls, role: AiRole) -> Actor:
        return cls("ai", role.value)

    @classmethod
    def system(cls, name: str) -> Actor:
        return cls("system", name)

    @classmethod
    def user(cls, principal: Principal) -> Actor:
        if principal.user_id is None:
            raise Forbidden("token has no CRM user id")
        return cls("user", str(principal.user_id), principal.user_id)


@dataclass(frozen=True, slots=True)
class Timeline:
    """A line on the deal timeline the team reads. `actor_type='buyer'` for inbound messages."""

    kind: str
    summary: str
    deal_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    channel: str | None = None
    direction: Literal["in", "out", "internal"] | None = "internal"
    ref_type: str | None = None
    ref_id: str | None = None
    actor_type: Literal["user", "ai", "system", "buyer"] | None = None


@dataclass(frozen=True, slots=True)
class Emit:
    """An outbox event to write with the change."""

    event_type: str
    aggregate_type: str
    aggregate_id: object
    payload: EventPayload
    correlation_id: uuid.UUID | None = None


async def record(
    session: AsyncSession,
    actor: Actor,
    action: str,
    entity_type: str,
    entity_id: object,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    timeline: Timeline | None = None,
    emit: Emit | tuple[Emit, ...] = (),
) -> None:
    """Audit + timeline + events for one state change (same transaction as the change itself)."""
    session.add(
        AuditEvent(
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            before=_jsonable(before),
            after=_jsonable(after),
            trace_id=trace_id_var.get(),
        )
    )
    if timeline is not None:
        session.add(
            TimelineEvent(
                deal_id=timeline.deal_id,
                contact_id=timeline.contact_id,
                kind=timeline.kind,
                channel=timeline.channel,
                direction=timeline.direction,
                summary=timeline.summary,
                ref_type=timeline.ref_type,
                ref_id=timeline.ref_id,
                actor_type=timeline.actor_type or actor.type,
                actor_id=actor.id,
            )
        )
    for e in (emit,) if isinstance(emit, Emit) else emit:
        await Outbox.add(
            session,
            SCHEMA,
            make_event(
                e.event_type, e.aggregate_type, e.aggregate_id, e.payload, correlation_id=e.correlation_id
            ),
        )


def _jsonable(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Audit snapshots go to jsonb: stringify Decimal / UUID / datetime so nothing is lost to float."""
    if value is None:
        return None
    return {
        k: v if v is None or isinstance(v, (str, int, bool, list, dict)) else str(v) for k, v in value.items()
    }
