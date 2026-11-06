"""After the deal (docs/BUSINESS_FLOW_V1.md §15; D13, D15): advance -> PAYMENT, WON + order, order status.

1. Payment (human only): the salesperson agrees the method with the buyer and records the advance (amount,
   currency, method, reference, date, proof file). The advance is always required. The deal moves to PAYMENT.
2. WON: once the advance is confirmed the salesperson marks the deal WON and enters the expected re-order
   date (stored now for the v2 re-order follow-ups). An order record is created.
3. Order statuses: in production -> ready -> shipped -> delivered, updated by hand.
The stage machine refuses WON without a payment row. The `source` columns ('manual' | 'erp') are the seam for
the v2 ERP plugin.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from kx.core import events as ev
from kx.core.contracts import DealStage
from kx.core.errors import Conflict, NotFound, RuleViolation
from kx.core.event_payloads import OrderStatusChanged
from kx.core.identity import new_id
from kx.crm.deps import CrmDeps
from kx.crm.models import Order, OrderStatusHistory, Payment, StockItem, StockMovement
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor, Emit, Timeline, record
from kx.crm.services.quotes import ISSUED, latest_version

ORDER_FLOW: Final[dict[str, frozenset[str]]] = {
    "in_production": frozenset({"ready", "cancelled"}),
    "ready": frozenset({"shipped", "cancelled"}),
    "shipped": frozenset({"delivered"}),
    "delivered": frozenset(),
    "cancelled": frozenset(),
}
_DATE_FIELD: Final = {
    "in_production": "production_started_on",
    "ready": "ready_on",
    "shipped": "shipped_on",
    "delivered": "delivered_on",
}


async def record_payment(
    session: AsyncSession,
    deps: CrmDeps,
    deal_id: uuid.UUID,
    actor: Actor,
    *,
    amount: Decimal | None = None,
    currency: str | None = None,
    method: str | None = None,
    reference: str | None = None,
    paid_on: date | None = None,
    note: str | None = None,
    proof_s3_key: str | None = None,
) -> Payment:
    """§15.1: the salesperson records the advance received outside the system: READY_TO_CLOSE -> PAYMENT."""
    if actor.user_id is None:
        raise RuleViolation("a payment is confirmed by a CRM user")
    if amount is None or currency is None or not method:
        raise RuleViolation("record the advance with its amount, currency and method (§15.1)")
    now = deps.now()
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    final = await latest_version(session, deal.id, ISSUED)
    if final is None:
        raise RuleViolation("no approved quote for this deal")
    if DealStage(deal.stage) is DealStage.READY_TO_CLOSE:
        await deal_svc.move_stage(session, deal, DealStage.PAYMENT, actor, now=now, reason="advance invoiced")
    payment = Payment(
        deal_id=deal.id,
        confirmed_by=actor.user_id,
        amount=amount,
        currency=currency.upper(),
        method=method,
        reference=reference,
        paid_on=paid_on or now.date(),
        note=note,
        proof_s3_key=proof_s3_key,
    )
    session.add(payment)
    await session.flush()
    await record(
        session,
        actor,
        "payment.recorded",
        "deal",
        deal.id,
        after={"amount": amount, "currency": currency, "method": method, "reference": reference},
        timeline=Timeline(
            "payment",
            f"Advance recorded: {currency.upper()} {amount} by {method}",
            deal.id,
            deal.primary_contact_id,
        ),
    )
    return payment


async def mark_won(
    session: AsyncSession,
    deps: CrmDeps,
    deal_id: uuid.UUID,
    actor: Actor,
    *,
    expected_reorder_date: date | None,
) -> Order:
    """§15.2: WON needs the recorded advance and the expected re-order date (D15); the order is created."""
    now = deps.now()
    deal = await deal_svc.get_deal(session, deal_id, lock=True)
    if expected_reorder_date is None:
        raise RuleViolation("enter the expected re-order date to mark the deal WON (§15.2)")
    if expected_reorder_date < now.date():
        raise RuleViolation("the expected re-order date is in the past")
    final = await latest_version(session, deal.id, ISSUED)
    if final is None:
        raise RuleViolation("no approved quote for this deal")
    await deal_svc.move_stage(
        session, deal, DealStage.WON, actor, now=now, reason="advance payment confirmed"
    )
    order = Order(
        id=new_id(),
        deal_id=deal.id,
        final_quote_version_id=final.id,
        delivery_estimate_id=final.delivery_estimate_id,
        production_started_on=now.date(),
        expected_reorder_date=expected_reorder_date,
    )
    session.add(order)
    await session.flush()
    await _order_changed(
        session,
        order,
        contact_id=deal.primary_contact_id,
        status="in_production",
        day=now.date(),
        actor=actor,
        note="order created",
    )
    return order


async def update_order_status(
    session: AsyncSession,
    deps: CrmDeps,
    order_id: uuid.UUID,
    status: str,
    actor: Actor,
    *,
    on_date: date | None = None,
    tracking_ref: str | None = None,
) -> Order:
    order = await session.get(Order, order_id, with_for_update=True)
    if order is None:
        raise NotFound(f"order {order_id} not found")
    if status not in ORDER_FLOW.get(order.status, frozenset()):
        raise RuleViolation(f"order cannot move from {order.status} to {status}")
    day = on_date or deps.now().date()
    order.status = status
    if field := _DATE_FIELD.get(status):
        setattr(order, field, day)
    if tracking_ref:
        order.tracking_ref = tracking_ref
    deal = await deal_svc.get_deal(session, order.deal_id)
    await session.flush()
    await _order_changed(
        session,
        order,
        contact_id=deal.primary_contact_id,
        status=status,
        day=day,
        actor=actor,
        note=tracking_ref or "",
    )
    return order


async def _order_changed(
    session: AsyncSession,
    order: Order,
    *,
    contact_id: uuid.UUID,
    status: str,
    day: date,
    actor: Actor,
    note: str,
) -> None:
    session.add(
        OrderStatusHistory(
            order_id=order.id, status=status, on_date=day, by_user=actor.user_id, source=order.source
        )
    )
    await record(
        session,
        actor,
        "order.status_changed",
        "order",
        order.id,
        after={"status": status},
        timeline=Timeline(
            "order",
            f"Order {order.number}: {status.replace('_', ' ')}",
            order.deal_id,
            contact_id,
            ref_type="order",
            ref_id=str(order.id),
        ),
        emit=Emit(
            ev.ORDER_STATUS_CHANGED,
            "order",
            order.id,
            OrderStatusChanged(
                order_ref=order.id, deal_ref=order.deal_id, contact_ref=contact_id, status=status, note=note
            ),
        ),
    )


async def move_stock(
    session: AsyncSession,
    stock_item_id: uuid.UUID,
    delta: Decimal,
    kind: str,
    actor: Actor,
    *,
    order_id: uuid.UUID | None = None,
    note: str | None = None,
) -> StockItem:
    """One stock movement. reserve/release move reserved_quantity; the rest move on-hand quantity."""
    item = await session.get(StockItem, stock_item_id, with_for_update=True)
    if item is None:
        raise NotFound(f"stock item {stock_item_id} not found")
    match kind:
        case "reserve" | "release":
            change = delta if kind == "reserve" else -delta
            if item.reserved_quantity + change < 0 or item.reserved_quantity + change > item.quantity:
                raise Conflict("reservation exceeds stock on hand")
            item.reserved_quantity += change
        case "adjust" | "receive" | "consume":
            item.quantity += -abs(delta) if kind == "consume" else delta
        case _:
            raise RuleViolation(f"unknown stock movement {kind!r}")
    item.updated_by = actor.user_id
    session.add(
        StockMovement(
            stock_item_id=item.id, delta=delta, kind=kind, order_id=order_id, by_user=actor.user_id, note=note
        )
    )
    await session.flush()
    await record(
        session,
        actor,
        f"stock.{kind}",
        "stock_item",
        item.id,
        after={"delta": delta, "quantity": item.quantity, "reserved": item.reserved_quantity},
    )
    return item
