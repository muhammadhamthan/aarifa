"""Deal (negotiation) graph: extract the buyer's ask -> CRM verdict -> reply by verdict -> guard.

The LLM never decides a price. It only (a) reads what the buyer asked for, which we cross-check
against the amounts actually present in the buyer's text, and (b) phrases a reply around the ONE price
the CRM's deterministic negotiation policy returned. Below range the reply is the fixed 60-minute
sentence (T6) and the CRM has already paused the AI and alerted manager + MD.
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
from kx.agents.registry import Phrase
from kx.core.contracts import AllowedPrice, CounterVerdict, CounterVerdictKind
from kx.core.price_guard import extract_prices

_RULES = (
    "Never offer a longer delivery time as a concession. Advance payment is always required. "
    "Do not mention any other price."
)


class BuyerAsk(BaseModel):
    intent: Literal["counter", "accept", "question", "reject"]
    ask_unit_price: Decimal | None = Field(default=None, description="Per-piece price the buyer proposed")
    ask_quantity: Decimal | None = Field(default=None, description="Quantity in pieces, if stated")
    currency: str | None = None


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
                    f"wrote one. Deal currency: {ctx.deal.currency}."
                ),
                HumanMessage(state["text"]),
            ]
        )
        ask = _grounded(raw, state["text"], ctx.deal.currency)
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
        if state.get("verdict") is None:
            ask = BuyerAsk.model_validate(state["ask"])
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
        if v.kind is CounterVerdictKind.NEEDS_MANAGER or v.offer_unit_price is None:
            ctx.log.handoff = True  # CRM paused the AI and alerted manager + MD when it returned this verdict
            return {"reply": ctx.prompts.phrase(Phrase.MANAGER_60, ctx.language), "status": "handoff"}
        offer = AllowedPrice(amount=v.offer_unit_price, currency=v.currency, label="offer per piece")
        if v.kind is CounterVerdictKind.ACCEPT:
            await ctx.gateway.mark_agreed(ctx.deal_id, ctx.spec.role)
            how = (
                f"Confirm the agreed unit price {v.currency} {v.offer_unit_price}. Say the final quotation "
                "will be confirmed by our management and sent shortly."
            )
            allowed: tuple[AllowedPrice, ...] = (offer, *ctx.deal.allowed_prices)
        else:
            trade = " for the larger quantity" if v.traded_for == "quantity_increase" else ""
            how = f"Offer exactly {v.currency} {v.offer_unit_price} per piece{trade}. Be warm and brief."
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
