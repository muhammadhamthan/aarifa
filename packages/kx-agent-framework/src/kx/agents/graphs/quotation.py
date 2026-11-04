"""Quotation graph: qualify -> request quote -> wait (durable interrupt) for the human approval -> reply.

The wait can last hours (2 h approval window, paused overnight), so the graph is checkpointed
(AsyncPostgresSaver, thread_id = deal ref) and resumed by the QUOTE_APPROVED / QUOTE_REJECTED /
QUOTE_CORRECTION_REQUESTED handlers with `Command(resume=...)`. The approved quote itself is sent by
automation with the CRM's single-use send grant; this graph never sends priced content.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Checkpointer, interrupt

from kx.agents.context import AgentContext
from kx.agents.graphs.compose import compose
from kx.agents.models import ModelSet
from kx.core.errors import Conflict

Outcome = Literal["approved", "rejected", "correction"]


class QuotationState(TypedDict, total=False):
    text: str
    reply: str | None
    status: Literal["not_qualified", "waiting", "ok", "handoff"]
    outcome: dict[str, Any] | None


def build_quotation_graph(
    models: ModelSet, checkpointer: Checkpointer = None
) -> CompiledStateGraph[QuotationState, AgentContext, QuotationState, QuotationState]:
    async def qualify(state: QuotationState, runtime: Runtime[AgentContext]) -> QuotationState:
        ctx = runtime.context
        result = await ctx.gateway.try_qualify(ctx.deal_id, ctx.spec.role)
        if result.qualified:
            return {"status": "waiting"}
        how = "Before we can quote, politely ask for: " + ", ".join(result.missing_fields) + "."
        return {
            "status": "not_qualified",
            "reply": await compose(models, ctx, state.get("text", ""), how, ()),
        }

    async def request(state: QuotationState, runtime: Runtime[AgentContext]) -> QuotationState:
        ctx = runtime.context
        with suppress(Conflict):  # already requested (redelivered message): wait for the same approval
            await ctx.gateway.request_quote(ctx.deal_id, ctx.spec.role)
        how = (
            "Tell the buyer in one or two sentences the quotation is being prepared and reviewed by our team."
        )
        return {"reply": await compose(models, ctx, state.get("text", ""), how, ())}

    async def wait(state: QuotationState) -> QuotationState:
        outcome = interrupt({"waiting_for": "quote_approval"})  # resumed by the QUOTE_* handlers
        return {"outcome": dict(outcome), "reply": None}

    async def respond(state: QuotationState, runtime: Runtime[AgentContext]) -> QuotationState:
        ctx = runtime.context
        outcome = state.get("outcome") or {}
        kind = outcome.get("kind")
        await ctx.gateway.add_timeline(
            ctx.deal_id, f"quote_{kind}", str(outcome.get("note", ""))[:500], ctx.spec.role
        )
        if kind == "rejected":
            how = "Tell the buyer our team is reviewing the request and will contact them shortly. No prices."
            return {"status": "handoff", "reply": await compose(models, ctx, "", how, ())}
        return {"status": "ok", "reply": None}  # approved: automation sends the quote body itself

    def after_qualify(state: QuotationState) -> str:
        return END if state.get("status") == "not_qualified" else "request"

    def after_wait(state: QuotationState) -> str:
        return "wait" if (state.get("outcome") or {}).get("kind") == "correction" else "respond"

    g = StateGraph(QuotationState, context_schema=AgentContext)
    g.add_node("qualify", qualify)
    g.add_node("request", request)
    g.add_node("wait", wait)
    g.add_node("respond", respond)
    g.add_edge(START, "qualify")
    g.add_conditional_edges("qualify", after_qualify, ["request", END])
    g.add_edge("request", "wait")
    g.add_conditional_edges("wait", after_wait, ["wait", "respond"])
    g.add_edge("respond", END)
    return g.compile(checkpointer=checkpointer, name="quotation")
