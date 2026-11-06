"""AgentService: one inbound buyer turn -> route -> one idempotent run -> guarded reply.

Used by the MESSAGE_RECEIVED handler (WhatsApp/email), the web-chat API and the voice worker alike.
`GraphRunner` is the DB-free core (also used by the offline evals); `AgentService` adds transcripts,
run bookkeeping and delivery. The reply is delivered BEFORE the run is marked ok: if delivery fails
the run is marked failed and the redelivered event re-runs it (the sender's idempotency key keeps
the buyer from getting two messages).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Checkpointer, Command

from kx.agents.context import AgentContext, GuardEventRecord, RunLog
from kx.agents.graphs.conversation import build_conversation_agent, final_reply
from kx.agents.graphs.deal import build_deal_graph
from kx.agents.graphs.quotation import build_quotation_graph
from kx.agents.guard import clip_for_channel, looks_like_injection
from kx.agents.knowledge import KnowledgeSearch
from kx.agents.language import reply_language
from kx.agents.memory import TurnStore
from kx.agents.models import ModelRouter
from kx.agents.orchestrator import Route, route
from kx.agents.registry import (
    AgentRegistry,
    DefaultPromptStore,
    GraphName,
    ModelTier,
    Phrase,
    PromptStore,
    team_member,
)
from kx.agents.runs import RunStatus, RunStore, UsageMeter
from kx.agents.tracing import Tracer
from kx.core.contracts import AiRole, Channel, CrmGateway
from kx.core.db import Databases
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)
Deliver = Callable[[str, str], Awaitable[None]]  # (reply text, language)


@dataclass(frozen=True, slots=True)
class InboundTurn:
    channel: Channel
    text: str
    deal_id: uuid.UUID
    thread_key: str  # stable per buyer thread, e.g. "whatsapp:<thread_ref>"
    trigger_event: str
    trigger_event_id: uuid.UUID
    contact_ref: uuid.UUID | None = None
    language: str | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    reply: str | None
    route: Route
    status: RunStatus | Literal["silent", "duplicate", "acknowledged"]
    language: str
    run_id: uuid.UUID | None = None
    log: RunLog = field(default_factory=RunLog)


class GraphRunner:
    """Compiled graphs per model tier, executed for a route."""

    def __init__(
        self, models: ModelRouter, registry: AgentRegistry, checkpointer: Checkpointer = None
    ) -> None:
        self.models = models
        self.registry = registry
        self._agents = {
            t: build_conversation_agent(models.for_tier(t)) for t in (ModelTier.FAST, ModelTier.SMART)
        }
        self._deal = build_deal_graph(models.for_tier(ModelTier.SMART), models.for_tier(ModelTier.FAST))
        self.quotation = build_quotation_graph(models.for_tier(ModelTier.FAST), checkpointer)
        self.has_checkpointer = checkpointer is not None

    async def run(
        self, rt: Route, ctx: AgentContext, text: str, history: Sequence[BaseMessage], config: RunnableConfig
    ) -> tuple[str | None, RunStatus]:
        if rt.graph is GraphName.DEAL:
            out = await self._deal.ainvoke({"text": text}, config, context=ctx)
            return out.get("reply"), out.get("status", "ok")
        if rt.graph is GraphName.QUOTATION and self.has_checkpointer:
            cfg: RunnableConfig = {**config, "configurable": {"thread_id": str(ctx.deal_id)}}
            if not (await self.quotation.aget_state(cfg)).interrupts:  # never restart a pending approval
                res = await self.quotation.ainvoke(
                    {"text": text, "reply": None, "outcome": None}, cfg, context=ctx, version="v2"
                )
                return res.value.get("reply"), "ok"
        agent = self._agents[ModelTier.FAST if ctx.spec.tier is ModelTier.FAST else ModelTier.SMART]
        out = await agent.ainvoke({"messages": [*history, HumanMessage(text)]}, config, context=ctx)
        reply = final_reply(out["messages"])
        if reply is None:  # call limit hit or empty answer: hand over rather than go quiet
            ctx.log.handoff = True
            reply = ctx.prompts.phrase(Phrase.HANDOFF, ctx.language)
        return reply, "ok"

    async def resume(
        self, ctx: AgentContext, outcome: dict[str, Any], config: RunnableConfig
    ) -> tuple[str | None, RunStatus]:
        """Resume the pending approval wait; (None, "ok") when nothing is pending for the deal."""
        cfg: RunnableConfig = {**config, "configurable": {"thread_id": str(ctx.deal_id)}}
        if not (await self.quotation.aget_state(cfg)).interrupts:
            return None, "ok"
        res = await self.quotation.ainvoke(Command(resume=outcome), cfg, context=ctx, version="v2")
        status: RunStatus = "handoff" if res.value.get("status") == "handoff" else "ok"
        return res.value.get("reply"), status


def _status(log_: RunLog, graph_status: RunStatus) -> RunStatus:
    if log_.blocked:
        return "blocked"
    return "handoff" if log_.handoff or graph_status == "handoff" else "ok"


class AgentService:
    def __init__(
        self,
        *,
        databases: Databases,
        gateway: CrmGateway,
        models: ModelRouter,
        registry: AgentRegistry | None = None,
        prompts: PromptStore | None = None,
        knowledge: KnowledgeSearch | None = None,
        checkpointer: Checkpointer = None,
        tracer: Tracer | None = None,
        turns: TurnStore | None = None,
    ) -> None:
        self.db = databases
        self.gateway = gateway
        self.registry = registry or AgentRegistry()
        self.prompts = prompts or DefaultPromptStore()
        self.knowledge = knowledge
        self.tracer = tracer or Tracer()
        self.turns = turns or TurnStore()
        self.runner = GraphRunner(models, self.registry, checkpointer)

    def _config(self, ctx: AgentContext, graph: str, run_id: uuid.UUID) -> RunnableConfig:
        return {
            "callbacks": [UsageMeter(ctx.log), *self.tracer.callbacks()],
            "run_name": graph,
            "metadata": Tracer.metadata(
                deal_ref=ctx.deal_id, role=ctx.spec.role.value, graph=graph, run_id=run_id
            ),
            "recursion_limit": 30,
        }

    async def handle_turn(self, turn: InboundTurn, deliver: Deliver | None = None) -> TurnResult:
        deal = await self.gateway.deal_context(turn.deal_id)
        language = reply_language(turn.text, turn.language or deal.contact.language)
        async with self.db.transaction(LogicalDb.AGENTS) as s:
            conv_id = await TurnStore.open_conversation(
                s,
                thread_key=turn.thread_key,
                channel=turn.channel,
                deal_ref=deal.deal_id,
                contact_ref=turn.contact_ref,
                language=language,
            )
            history = await self.turns.history(s, conv_id)
            rt = route(turn.text, deal, first_turn=history.turn_count == 0, disabled=self.registry.disabled())
            if rt.action in ("silent", "acknowledge"):
                await TurnStore.append(s, conv_id, "buyer", turn.text, language=language)
            else:
                assert rt.graph is not None
                spec = self.registry.get(rt.role)
                claim = await RunStore.claim(
                    s,
                    trigger_event=turn.trigger_event,
                    trigger_event_id=turn.trigger_event_id,
                    graph=rt.graph.value,
                    role=rt.role.value,
                    deal_ref=deal.deal_id,
                    conversation_id=conv_id,
                    model=self.runner.models.for_tier(spec.tier).name,
                    prompt_version=self.prompts.system_prompt(spec, language).version,
                )
                if claim is None:
                    return TurnResult(None, rt, "duplicate", language)
                if claim.fresh:
                    await TurnStore.append(s, conv_id, "buyer", turn.text, language=language)
        if rt.action in ("silent", "acknowledge"):  # AI paused / handed over: the salesperson's inbox
            await self.gateway.add_timeline(
                deal.deal_id, "inbound_message", turn.text[:500], AiRole.ORCHESTRATOR
            )
            if rt.action == "silent":
                return TurnResult(None, rt, "silent", language)
            ack = self.prompts.phrase(Phrase.HANDOVER_ACK, language).format(
                name=deal.salesperson_name or team_member(None, language)
            )
            if deliver is not None:
                await deliver(ack, language)
            return TurnResult(ack, rt, "acknowledged", language)

        ctx = AgentContext(
            deal=deal,
            spec=spec,
            channel=turn.channel,
            language=language,
            gateway=self.gateway,
            prompts=self.prompts,
            knowledge=self.knowledge,
            conversation_id=conv_id,
            summary=history.summary,
        )
        if looks_like_injection(turn.text):
            ctx.log.guard_events.append(GuardEventRecord("injection", "flagged", {"text": turn.text[:200]}))
        assert claim is not None
        return await self._execute(ctx, rt, claim.run_id, turn.text, history.messages, deliver=deliver)

    async def resume_quotation(
        self,
        deal_id: uuid.UUID,
        outcome: dict[str, Any],
        *,
        trigger_event: str,
        trigger_event_id: uuid.UUID,
        deliver: Deliver | None = None,
    ) -> TurnResult | None:
        deal = await self.gateway.deal_context(deal_id)
        spec = self.registry.get(AiRole.QUOTATION)
        rt = Route("reply", GraphName.QUOTATION, AiRole.QUOTATION, f"quote_{outcome.get('kind')}")
        language = deal.contact.language
        async with self.db.transaction(LogicalDb.AGENTS) as s:
            claim = await RunStore.claim(
                s,
                trigger_event=trigger_event,
                trigger_event_id=trigger_event_id,
                graph=GraphName.QUOTATION.value,
                role=spec.role.value,
                deal_ref=deal_id,
                conversation_id=None,
                model=self.runner.models.for_tier(spec.tier).name,
                prompt_version=self.prompts.system_prompt(spec, language).version,
            )
        if claim is None:
            return None
        ctx = AgentContext(
            deal=deal,
            spec=spec,
            channel="whatsapp",
            language=language,
            gateway=self.gateway,
            prompts=self.prompts,
        )
        return await self._execute(ctx, rt, claim.run_id, None, (), deliver=deliver, outcome=outcome)

    async def _execute(
        self,
        ctx: AgentContext,
        rt: Route,
        run_id: uuid.UUID,
        text: str | None,
        history: Sequence[BaseMessage],
        *,
        deliver: Deliver | None,
        outcome: dict[str, Any] | None = None,
    ) -> TurnResult:
        assert rt.graph is not None
        started = time.perf_counter()
        config = self._config(ctx, rt.graph.value, run_id)
        try:
            if outcome is not None:
                reply, graph_status = await self.runner.resume(ctx, outcome, config)
            else:
                assert text is not None
                reply, graph_status = await self.runner.run(rt, ctx, text, history, config)
            if reply:
                reply = clip_for_channel(reply, ctx.channel)
                if deliver is not None:
                    await deliver(reply, ctx.language)
        except Exception as exc:
            async with self.db.transaction(LogicalDb.AGENTS) as s:
                await RunStore.fail(s, run_id, f"{type(exc).__name__}: {exc}")
            raise
        status = _status(ctx.log, graph_status)
        async with self.db.transaction(LogicalDb.AGENTS) as s:
            if reply and ctx.conversation_id is not None:
                await TurnStore.append(
                    s, ctx.conversation_id, "ai", reply, role=ctx.spec.role.value, language=ctx.language
                )
            await RunStore.finish(
                s,
                run_id,
                status=status,
                log=ctx.log,
                graph=rt.graph.value,
                deal_ref=ctx.deal_id,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        if ctx.conversation_id is not None:
            try:
                await self.turns.summarise(
                    self.db, self.runner.models.for_tier(ModelTier.FAST), ctx.conversation_id
                )
            except Exception:  # summary is an optimisation; never fail the turn for it
                log.warning("agents.summary_failed", conversation_id=str(ctx.conversation_id), exc_info=True)
        return TurnResult(reply, rt, status, ctx.language, run_id, ctx.log)
