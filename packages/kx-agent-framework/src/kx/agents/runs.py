"""agent_runs bookkeeping: an idempotent claim per trigger event, then one write of the outcome
(run row, tool calls, guard events, AGENT_RUN_COMPLETED in the agents outbox) in one transaction."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from kx.agents.context import RunLog
from kx.agents.orm import SCHEMA, AgentRun, GuardEvent, ToolCall
from kx.core import events as ev
from kx.core.event_payloads import AgentRunCompleted, make_event
from kx.core.events import Outbox
from kx.core.identity import new_id

RunStatus = Literal["ok", "failed", "blocked", "handoff"]
_EVENT_STATUS: Final[dict[str, Literal["ok", "guard_blocked", "error", "handoff"]]] = {
    "ok": "ok",
    "blocked": "guard_blocked",
    "failed": "error",
    "handoff": "handoff",
}

# One run per (event, graph). A failed run may be re-claimed (the event is redelivered); a live or
# finished run under ANY graph blocks a second one, even if the deal stage changed in between.
_CLAIM: Final = text("""
INSERT INTO agent_runs (id, conversation_id, deal_ref, graph, role, trigger_event, trigger_event_id,
                        model, prompt_version, status)
SELECT :id, :conv, :deal, :graph, :role, :trig, :eid, :model, :pv, 'running'
WHERE NOT EXISTS (SELECT 1 FROM agent_runs WHERE trigger_event_id = :eid AND graph <> :graph
                  AND status <> 'failed')
ON CONFLICT (trigger_event_id, graph) DO UPDATE
   SET status = 'running', error = NULL, started_at = now(), finished_at = NULL
 WHERE agent_runs.status = 'failed'
RETURNING id, (xmax = 0) AS fresh
""")


@dataclass(frozen=True, slots=True)
class Claim:
    run_id: uuid.UUID
    fresh: bool  # False when re-claiming a failed run (the buyer turn is already stored)


class UsageMeter(AsyncCallbackHandler):
    """Sums token usage of every model call in a run (incl. fallbacks and structured calls)."""

    def __init__(self, log: RunLog) -> None:
        self._log = log

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for gens in response.generations:
            for gen in gens:
                msg = gen.message if isinstance(gen, ChatGeneration) else None
                usage = msg.usage_metadata if isinstance(msg, AIMessage) else None
                if usage:
                    self._log.input_tokens += usage.get("input_tokens", 0)
                    self._log.output_tokens += usage.get("output_tokens", 0)
                    self._log.cached_tokens += (usage.get("input_token_details") or {}).get("cache_read", 0)


class RunStore:
    @staticmethod
    async def claim(
        s: AsyncSession,
        *,
        trigger_event: str,
        trigger_event_id: uuid.UUID,
        graph: str,
        role: str,
        deal_ref: uuid.UUID | None,
        conversation_id: uuid.UUID | None,
        model: str,
        prompt_version: str,
    ) -> Claim | None:
        row = (
            await s.execute(
                _CLAIM,
                {
                    "id": new_id(),
                    "conv": conversation_id,
                    "deal": deal_ref,
                    "graph": graph,
                    "role": role,
                    "trig": trigger_event,
                    "eid": trigger_event_id,
                    "model": model,
                    "pv": prompt_version,
                },
            )
        ).first()
        return None if row is None else Claim(row.id, bool(row.fresh))

    @staticmethod
    async def finish(
        s: AsyncSession,
        run_id: uuid.UUID,
        *,
        status: RunStatus,
        log: RunLog,
        latency_ms: int,
        graph: str,
        deal_ref: uuid.UUID | None,
        cost_usd: Decimal = Decimal(0),
        error: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(
                status=status,
                input_tokens=log.input_tokens,
                output_tokens=log.output_tokens,
                cached_tokens=log.cached_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                error=error,
                langfuse_trace_id=trace_id,
                finished_at=datetime.now(UTC),
            )
        )
        s.add_all(
            ToolCall(
                run_id=run_id,
                tool=t.tool,
                args=t.args,
                result_summary=t.result_summary,
                status=t.status,
                duration_ms=t.duration_ms,
            )
            for t in log.tool_calls
        )
        s.add_all(
            GuardEvent(run_id=run_id, guard=g.guard, action=g.action, detail=g.detail)
            for g in log.guard_events
        )
        await Outbox.add(
            s,
            SCHEMA,
            make_event(
                ev.AGENT_RUN_COMPLETED,
                "agent_run",
                run_id,
                AgentRunCompleted(
                    run_ref=run_id, graph=graph, deal_ref=deal_ref, status=_EVENT_STATUS[status]
                ),
            ),
        )

    @staticmethod
    async def fail(s: AsyncSession, run_id: uuid.UUID, error: str) -> None:
        await s.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id)
            .values(status="failed", error=error[:2000], finished_at=datetime.now(UTC))
        )
