"""Per-run context injected into graphs (`context=`), tools (`ToolRuntime.context`) and middleware.

The deal id lives here, never in tool arguments, so the model cannot act on another buyer's deal.
`RunLog` collects tool calls, guard events and token usage in memory; `runs.RunStore` writes them
in one transaction when the run ends (no DB round-trips inside the agent loop).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from kx.agents.knowledge import KnowledgeSearch
from kx.agents.registry import AgentSpec, PromptStore
from kx.core.contracts import Channel, CrmGateway, DealContext

GuardKind = Literal["price_number", "injection", "disclosure", "cost_leak", "language"]
GuardAction = Literal["blocked", "rewritten", "flagged"]


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    tool: str
    args: dict[str, Any]  # PII-masked
    status: Literal["ok", "error", "denied"]
    result_summary: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class GuardEventRecord:
    guard: GuardKind
    action: GuardAction
    detail: dict[str, Any]


@dataclass(slots=True)
class RunLog:
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    guard_events: list[GuardEventRecord] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    regenerations: int = 0  # the price guard regenerates at most once per run
    handoff: bool = False

    @property
    def blocked(self) -> bool:
        return any(g.action == "blocked" for g in self.guard_events)


@dataclass(frozen=True, slots=True)
class AgentContext:
    deal: DealContext
    spec: AgentSpec
    channel: Channel
    language: str
    gateway: CrmGateway
    prompts: PromptStore
    log: RunLog = field(default_factory=RunLog)
    knowledge: KnowledgeSearch | None = None
    conversation_id: uuid.UUID | None = None
    summary: str | None = None  # rolling summary of turns older than the window

    @property
    def deal_id(self) -> uuid.UUID:
        return self.deal.deal_id
