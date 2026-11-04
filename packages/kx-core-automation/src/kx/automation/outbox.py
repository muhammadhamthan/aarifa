"""Emit catalogue events into the automation outbox inside the caller's transaction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from kx.automation.models import SCHEMA
from kx.core import events as ev
from kx.core.event_payloads import MessageReceived, MessageStatus, PriceProposed, make_event
from kx.core.events import Outbox

# The only events automation produces (everything else it consumes).
AutomationPayload = MessageReceived | MessageStatus | PriceProposed

_EVENT: dict[type[AutomationPayload], tuple[str, str]] = {
    MessageReceived: (ev.MESSAGE_RECEIVED, "inbound_message"),
    MessageStatus: (ev.MESSAGE_STATUS, "outbound_message"),
    PriceProposed: (ev.PRICE_PROPOSED, "price_observation"),
}


async def emit(session: AsyncSession, payload: AutomationPayload) -> None:
    event_type, aggregate_type = _EVENT[type(payload)]
    aggregate_id = payload.observation_ref if isinstance(payload, PriceProposed) else payload.message_ref
    await Outbox.add(session, SCHEMA, make_event(event_type, aggregate_type, aggregate_id, payload))
