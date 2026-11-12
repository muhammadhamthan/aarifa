"""Per-deal actions: after the deal (§15 payment, WON, order status), §7A.1 reopen, §14 hand-over (reply
from the CRM inbox, click-to-call, personal contact).

Thin: kx.crm.services.deals does the work.
"""

# No `from __future__ import annotations` here: FastAPI must evaluate the closure-local `Session`
# alias; PEP 649 (Python 3.14) lazy annotations resolve it, string annotations would not.

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from kx.core.auth import Principal, Role, require_human_mfa, require_role
from kx.crm.api.context import MANAGERS, STAFF, ApiContext
from kx.crm.api.deals import deal_detail
from kx.crm.api.schemas import (
    CallIn,
    CallOut,
    DealDetail,
    OptOutIn,
    OrderOut,
    OrderStatusIn,
    PaymentIn,
    PaymentOut,
    ReopenIn,
    StaffMessageIn,
    StaffMessageOut,
    WonIn,
)
from kx.crm.services import access, consent, handover, orders
from kx.crm.services import deals as deal_svc
from kx.crm.services.journal import Actor

Staff = Annotated[Principal, Depends(require_role(*STAFF))]
HumanManager = Annotated[Principal, Depends(require_human_mfa(Role.MANAGER))]
HumanStaff = Annotated[Principal, Depends(require_human_mfa(Role.SALES, Role.MANAGER))]
Manager = Annotated[Principal, Depends(require_role(*MANAGERS))]


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter(prefix="/deals", tags=["deals"])
    Session = Annotated[AsyncSession, Depends(ctx.session)]  # noqa: N806
    deps = ctx.deps

    @r.post("/{deal_id}/reopen", response_model=DealDetail)
    async def reopen(deal_id: uuid.UUID, body: ReopenIn, s: Session, p: HumanManager) -> DealDetail:
        """§7A.1 / D24: the manager (or MD) reopens a LOST deal to the stage it was lost from; the AI stays
        paused until a person resumes it."""
        deal = await deal_svc.get_deal(s, deal_id, lock=True)
        reopened = await deal_svc.reopen(s, deal, Actor.user(p), now=deps.now(), reason=body.reason)
        return await deal_detail(s, reopened)

    @r.post("/{deal_id}/personal-contact-shared", response_model=DealDetail)
    async def personal_contact_shared(deal_id: uuid.UUID, s: Session, p: Staff) -> DealDetail:
        """§14 practical notes: record that the salesperson shared a personal contact with the buyer."""
        deal = await deal_svc.get_deal(s, deal_id, lock=True)
        await access.check_edit(s, p, deal)
        return await deal_detail(
            s, await deal_svc.personal_contact_shared(s, deal, Actor.user(p), deps.now())
        )

    @r.post("/{deal_id}/messages", response_model=StaffMessageOut, status_code=202)
    async def staff_message(
        deal_id: uuid.UUID, body: StaffMessageIn, s: Session, p: Staff
    ) -> StaffMessageOut:
        """§14 / D21: reply to the buyer on the same channel and company number, signed with the
        salesperson's name. Goes through the outbound gate (consent, opt-out, 24-hour rule); a price that is
        not in an approved quote is refused (422 `unapproved_price`). WhatsApp after 24 h of buyer silence
        answers 422 `window_closed`: send again with `reopen_template=true`. The AI stops replying."""
        deal = await deal_svc.get_deal(s, deal_id, lock=True)
        message_id = await handover.reply(
            s,
            deps,
            deal,
            p,
            text=body.text,
            client_key=body.client_key,
            channel=body.channel,
            reopen_template=body.reopen_template,
        )
        return StaffMessageOut(message_id=message_id)

    @r.post("/{deal_id}/call", response_model=CallOut, status_code=202)
    async def call(deal_id: uuid.UUID, body: CallIn, s: Session, p: Staff) -> CallOut:
        """§14: click-to-call through the company number. The salesperson's phone rings first, then the
        buyer; logged on the deal. Refused (422 `opt_out`) when the buyer asked not to be called."""
        deal = await deal_svc.get_deal(s, deal_id)
        return CallOut(call_id=await handover.call_buyer(s, deps, deal, p, client_key=body.client_key))

    @r.post("/{deal_id}/opt-out", response_model=DealDetail)
    async def opt_out(deal_id: uuid.UUID, body: OptOutIn, s: Session, p: Staff) -> DealDetail:
        """§8.1 step 4: the salesperson records a stop (and the buyer's own reason, if given) after a
        conversation the buyer started. Same effect as a STOP: suppression list, queued follow-ups cancelled,
        Opted out badge, LOST proposed. Add only; a stop is never removed here."""
        deal = await deal_svc.get_deal(s, deal_id, lock=True)
        await access.check_edit(s, p, deal)
        await consent.record_opt_out(
            s,
            deps,
            deal,
            scope=body.scope,
            words=body.words,
            reason=body.reason,
            decided_by=f"salesperson:{p.user_id}",
            source="salesperson",
            actor=Actor.user(p),
        )
        return await deal_detail(s, deal)

    @r.post("/{deal_id}/payments", response_model=PaymentOut, status_code=201)
    async def payment(deal_id: uuid.UUID, body: PaymentIn, s: Session, p: HumanStaff) -> PaymentOut:
        """§15.1 / D20: the salesperson (human, MFA) confirms the advance; every detail is optional. The deal
        moves to PAYMENT."""
        await access.check_edit(s, p, await deal_svc.get_deal(s, deal_id))
        payment = await orders.record_payment(s, deps, deal_id, Actor.user(p), **body.model_dump())
        return PaymentOut.model_validate(payment)

    @r.post("/{deal_id}/won", response_model=OrderOut, status_code=201)
    async def won(deal_id: uuid.UUID, body: WonIn, s: Session, p: HumanStaff) -> OrderOut:
        """§15.2: WON (mandatory) with an optional expected re-order date; the order record is created."""
        await access.check_edit(s, p, await deal_svc.get_deal(s, deal_id))
        order = await orders.mark_won(
            s, deps, deal_id, Actor.user(p), expected_reorder_date=body.expected_reorder_date
        )
        return OrderOut.model_validate(order)

    @r.patch("/orders/{order_id}", response_model=OrderOut)
    async def order_status(order_id: uuid.UUID, body: OrderStatusIn, s: Session, p: Manager) -> OrderOut:
        """§15.3 order tracking: in production -> ready -> shipped -> delivered (the buyer is told if
        configured)."""
        order = await orders.update_order_status(
            s,
            deps,
            order_id,
            body.status,
            Actor.user(p),
            on_date=body.on_date,
            tracking_ref=body.tracking_ref,
        )
        return OrderOut.model_validate(order)

    return r
