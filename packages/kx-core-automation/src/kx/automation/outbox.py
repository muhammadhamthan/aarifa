"""Emit catalogue events into the automation outbox inside the caller's transaction."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from kx.automation.models import SCHEMA
from kx.core import events as ev
from kx.core.event_payloads import (
    KbContentPublished,
    MessageReceived,
    MessageStatus,
    OptedOut,
    PriceProposed,
    ProductPublished,
    QuoteNotDelivered,
    make_event,
)
from kx.core.events import Outbox

# The only events automation produces (everything else it consumes).
AutomationPayload = (
    MessageReceived
    | MessageStatus
    | PriceProposed
    | OptedOut
    | ProductPublished
    | KbContentPublished
    | QuoteNotDelivered
)

_EVENT: dict[type[AutomationPayload], tuple[str, str]] = {
    MessageReceived: (ev.MESSAGE_RECEIVED, "inbound_message"),
    MessageStatus: (ev.MESSAGE_STATUS, "outbound_message"),
    PriceProposed: (ev.PRICE_PROPOSED, "price_observation"),
    OptedOut: (ev.CONSENT_OPTED_OUT, "opt_out"),  # §8.1: a rule, the unsubscribe link or the 24 h timer
    ProductPublished: (ev.CATALOGUE_PRODUCT_PUBLISHED, "cms_product"),  # §7: the CMS publish webhook
    KbContentPublished: (ev.KB_CONTENT_PUBLISHED, "cms_document"),  # §7: re-index the AI knowledge
    QuoteNotDelivered: (ev.QUOTE_NOT_DELIVERED, "outbound_message"),  # D32
}


def _aggregate(payload: AutomationPayload) -> object:
    match payload:
        case PriceProposed():
            return payload.observation_ref
        case OptedOut():
            return payload.log_ref or payload.contact_ref or payload.deal_ref
        case ProductPublished():
            return payload.code
        case KbContentPublished():
            return f"{payload.collection}:{payload.doc_id}:{payload.locale}"
        case _:
            return payload.message_ref


async def emit(session: AsyncSession, payload: AutomationPayload) -> None:
    event_type, aggregate_type = _EVENT[type(payload)]
    await Outbox.add(session, SCHEMA, make_event(event_type, aggregate_type, _aggregate(payload), payload))
