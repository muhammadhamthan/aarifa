"""Deal (negotiation) graph: extract the buyer's ask -> CRM verdict -> reply by verdict -> guard.

The LLM never decides a price. It only (a) reads what the buyer asked for, which we cross-check
against the amounts actually present in the buyer's text, and (b) phrases a reply around the ONE price
the CRM's deterministic negotiation policy returned (docs/BUSINESS_FLOW_V1.md §12.2).

The AI negotiates price only (§12, D12). Anything else the buyer asks for (product, specification,
printing, a quantity outside the tiers, delivery, samples, payment, other) becomes a change request for
the salesperson, and the reply is the fixed §13.1 sentence ("updated quotation within 4 working hours"),
never a promise. Payment is never discussed by the AI (§13 "Payment", D13).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from kx.agents.context import AgentContext
from kx.agents.graphs.compose import compose
from kx.agents.models import ModelSet
from kx.agents.orchestrator import PAYMENT_TOPIC
from kx.agents.registry import Phrase, team_member
from kx.core.contracts import AllowedPrice, ChangeField, ChangeType, CounterVerdict, CounterVerdictKind
from kx.core.money import MONEY_PLACES, display, q
from kx.core.price_guard import extract_prices

_RULES = (
    "Never offer a longer delivery time as a concession. Advance payment is always required. "
    "Never discuss payment terms. Do not mention any other price."
)
# §13 "Payment: any payment topic ... always go to the salesperson". Deterministic, before the LLM.


class BuyerAsk(BaseModel):
    intent: Literal["counter", "accept", "question", "reject", "change"]
    ask_unit_price: Decimal | None = Field(default=None, description="Per-piece price the buyer proposed")
    ask_quantity: Decimal | None = Field(default=None, description="Quantity in pieces, if stated")
    currency: str | None = None
    change_type: ChangeType | None = Field(
        default=None,
        description="intent=change only: product/spec/printing/quantity/delivery/samples/payment/other",
    )
    change_field: str | None = Field(
        default=None, description="intent=change only: e.g. delivery_time, product, printing, incoterm"
    )
    requested: str | None = Field(default=None, description="intent=change only: what the buyer asked for")


class DealState(TypedDict, total=False):
    text: str
    ask: dict[str, Any]
    verdict: dict[str, Any] | None
    reply: str
    status: Literal["ok", "handoff"]


def _grounded(ask: BuyerAsk, text: str, currency: str) -> BuyerAsk:
    """Drop an extracted price the buyer never wrote (LLM hallucination would otherwise reach the CRM)."""
    if ask.ask_unit_price is None:
        return ask
    written = {m.amount for m in extract_prices(text, default_currency=currency) if not m.is_percentage}
    if ask.ask_unit_price in written:
        return ask
    return ask.model_copy(
        update={"ask_unit_price": None, "intent": "question" if ask.intent == "counter" else ask.intent}
    )


def _payment(ask: BuyerAsk, text: str) -> BuyerAsk:
    if PAYMENT_TOPIC.search(text) is None:
        return ask
    return ask.model_copy(
        update={
            "intent": "change",
            "change_type": "payment",
            "change_field": "payment_terms",
            "requested": text,
        }
    )


async def _change_request(
    ctx: AgentContext, text: str, type: ChangeType, field: str, requested: str
) -> DealState:
    """§13.1: raise the card, then the fixed acknowledgement. No price, no promise."""
    await ctx.gateway.raise_change_request(
        ctx.deal_id, type, text, (ChangeField(field=field, requested=requested[:500]),), ctx.spec.role
    )
    reply = ctx.prompts.phrase(Phrase.CHANGE_REQUEST, ctx.language).format(
        name=team_member(ctx.deal.salesperson_name, ctx.language), hours=ctx.deal.change_request_hours
    )
    return {"reply": reply, "status": "ok"}


def _confirm_terms(
    ctx: AgentContext, v: CounterVerdict, ask: BuyerAsk
) -> tuple[str, tuple[AllowedPrice, ...]]:
    """§14.2: confirm back product, quantity, unit price, total, delivery time and Incoterm. The agreed total
    is the CRM's price x the agreed quantity (exact Decimal), the only new amount allowed."""
    assert v.offer_unit_price is not None
    quote = ctx.deal.latest_quote
    line = quote.lines[0] if quote and quote.lines else None
    qty = ask.ask_quantity or (line.quantity if line else None)
    parts = [f"unit price {v.currency} {v.offer_unit_price}"]
    agreed: tuple[AllowedPrice, ...] = ()
    if qty is not None:
        total = q(v.offer_unit_price * qty, MONEY_PLACES)
        agreed = (AllowedPrice(amount=total, currency=v.currency, label="agreed total"),)
        parts.insert(
            0, f"{qty:,.0f} {line.unit if line else 'pcs'}" + (f" of {line.description}" if line else "")
        )
        parts.append(f"total {v.currency} {display(total, v.currency):,}")
    if quote and quote.delivery_time_text:
        parts.append(f"delivery {quote.delivery_time_text}")
    if quote and quote.incoterm:
        parts.append(f"Incoterm {quote.incoterm}")
    how = (
        "Confirm back exactly these agreed terms: " + "; ".join(parts) + ". Say the final quotation will be "
        "confirmed by our sales team and sent shortly. Do not mention payment."
    )
    return how, agreed


def build_deal_graph(
    models: ModelSet, extractor: ModelSet | None = None
) -> CompiledStateGraph[DealState, AgentContext, DealState, DealState]:
    extract_model = (extractor or models).structured(BuyerAsk)

    async def extract(state: DealState, runtime: Runtime[AgentContext]) -> DealState:
        ctx = runtime.context
        raw = await extract_model.ainvoke(
            [
                SystemMessage(
                    "Classify the buyer's negotiation message. Extract the unit price ONLY if the buyer "
                    f"wrote one. Deal currency: {ctx.deal.currency}. Use intent=change when the buyer asks "
                    "for anything other than a price: another product or size, a specification, printing, "
                    "a delivery time, date, place or Incoterm, samples, payment, documents or a complaint."
                ),
                HumanMessage(state["text"]),
            ]
        )
        ask = _payment(_grounded(raw, state["text"], ctx.deal.currency), state["text"])
        if (
            ask.intent == "accept"
            and ask.ask_unit_price is None
            and ctx.deal.latest_quote
            and len(ctx.deal.latest_quote.lines) == 1
        ):
            ask = ask.model_copy(update={"ask_unit_price": ctx.deal.latest_quote.lines[0].unit_price})
        return {"ask": ask.model_dump(mode="json")}

    async def evaluate(state: DealState, runtime: Runtime[AgentContext]) -> DealState:
        ctx = runtime.context
        ask = BuyerAsk.model_validate(state["ask"])
        if ask.intent not in ("counter", "accept") or ask.ask_unit_price is None:
            return {"verdict": None}
        verdict = await ctx.gateway.evaluate_counter(
            ctx.deal_id, ask.ask_unit_price, ask.ask_quantity, ctx.channel
        )
        return {"verdict": verdict.model_dump(mode="json")}

    async def respond(state: DealState, runtime: Runtime[AgentContext]) -> DealState:
        ctx = runtime.context
        text = state["text"]
        ask = BuyerAsk.model_validate(state["ask"])
        if state.get("verdict") is None:
            if ask.intent == "change":
                return await _change_request(
                    ctx, text, ask.change_type or "other", ask.change_field or "other", ask.requested or text
                )
            how = (
                "The buyer declined; thank them, ask what would work for them."
                if ask.intent == "reject"
                else "Answer the buyer's question about the quotation."
            )
            return {
                "reply": await compose(models, ctx, text, f"{how} {_RULES}", ctx.deal.allowed_prices),
                "status": "ok",
            }
        v = CounterVerdict.model_validate(state["verdict"])
        if v.kind is CounterVerdictKind.CHANGE_REQUEST:  # quantity outside the tiers, or a custom item (D8)
            qty = f"{ask.ask_quantity:,.0f} pcs" if ask.ask_quantity is not None else text
            kind: ChangeType = "quantity" if v.reason == "quantity_outside_tiers" else "other"
            return await _change_request(ctx, text, kind, "quantity" if kind == "quantity" else "price", qty)
        if v.kind is CounterVerdictKind.REQUOTE:  # §12.2 rule 5: no new price until the salesperson approves
            name = team_member(ctx.deal.salesperson_name, ctx.language)
            return {
                "reply": ctx.prompts.phrase(Phrase.REVISED_QUOTE, ctx.language).format(name=name),
                "status": "ok",
            }
        if v.kind is CounterVerdictKind.NEEDS_MANAGER or v.offer_unit_price is None:
            ctx.log.handoff = True  # the CRM paused the AI and alerted the salesperson when it returned this
            return {"reply": ctx.prompts.phrase(Phrase.MANAGER_60, ctx.language), "status": "handoff"}
        offer = AllowedPrice(amount=v.offer_unit_price, currency=v.currency, label="offer per piece")
        if v.kind is CounterVerdictKind.ACCEPT:
            await ctx.gateway.mark_agreed(ctx.deal_id, ctx.spec.role)
            how, agreed = _confirm_terms(ctx, v, ask)
            allowed: tuple[AllowedPrice, ...] = (offer, *agreed, *ctx.deal.allowed_prices)
        else:
            trade = " for the larger quantity" if v.traded_for == "quantity_increase" else ""
            ask_more = (
                " Ask whether they can increase the quantity for a better price."
                if v.reason == "ask_for_quantity"
                else ""
            )
            offer_text = f"Offer exactly {v.currency} {v.offer_unit_price} per piece{trade}."
            how = f"{offer_text} Be warm and brief.{ask_more}"
            allowed = (offer,)
        return {"reply": await compose(models, ctx, text, f"{how} {_RULES}", allowed), "status": "ok"}

    g = StateGraph(DealState, context_schema=AgentContext)
    g.add_node("extract", extract)
    g.add_node("evaluate", evaluate)
    g.add_node("respond", respond)
    g.add_edge(START, "extract")
    g.add_edge("extract", "evaluate")
    g.add_edge("evaluate", "respond")
    g.add_edge("respond", END)
    return g.compile(name="deal")
