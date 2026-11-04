"""Conversation graph: one `create_agent` serving the pre-quote and service roles.

Middleware order matters (first = outermost around the model call):
RoleMiddleware (prompt + tools) -> PriceGuardMiddleware (check, regenerate once with a note appended to
the role prompt, fallback) -> provider fallback -> model. Call limits stop tool loops.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph.state import CompiledStateGraph

from kx.agents.context import AgentContext
from kx.agents.guard import PriceGuardMiddleware
from kx.agents.middleware import RoleMiddleware, ToolAuditMiddleware
from kx.agents.models import ModelSet
from kx.agents.tools import ALL_TOOLS


def build_conversation_agent(
    models: ModelSet, *, model_calls: int = 6, tool_calls: int = 8
) -> CompiledStateGraph[Any, AgentContext, Any, Any]:
    return create_agent(
        models.primary,
        tools=list(ALL_TOOLS),
        middleware=[
            RoleMiddleware(),
            PriceGuardMiddleware(),
            *models.middleware(),
            ToolAuditMiddleware(),
            ModelCallLimitMiddleware(run_limit=model_calls, exit_behavior="end"),
            ToolCallLimitMiddleware(run_limit=tool_calls),
        ],
        context_schema=AgentContext,
        name="conversation",
    )


def final_reply(messages: Sequence[BaseMessage]) -> str | None:
    """Last AI message without pending tool calls (what the buyer sees)."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg.text if not msg.tool_calls and msg.text.strip() else None
    return None
