"""Agent middleware for the role-switching conversation agent.

RoleMiddleware: one compiled agent serves every conversation role; per model call it installs the
role's system prompt (static part cache-tagged for Anthropic prompt caching) plus the live deal facts,
and narrows the tool list to the role's subset.
ToolAuditMiddleware: denies tools outside the role (defence against hallucinated calls), turns CRM
rule refusals into tool errors the model can explain, and records every call (PII-masked).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from kx.agents.context import AgentContext, ToolCallRecord
from kx.agents.language import language_name
from kx.core.contracts import AllowedPrice
from kx.core.errors import DomainError
from kx.core.logging import mask_pii

_CHANNEL_STYLE = {
    "web_voice": "You are speaking aloud: two or three short sentences, no lists, no markdown.",
    "phone": "You are speaking aloud: two or three short sentences, no lists, no markdown.",
    "whatsapp": "WhatsApp: short paragraphs, no markdown tables.",
    "email": "Email: complete, polite, structured; no subject line.",
    "web_chat": "Website chat: concise and friendly.",
}


def deal_facts(ctx: AgentContext, allowed: Sequence[AllowedPrice] | None = None) -> str:
    """The per-turn, uncached part of the system prompt. `allowed` narrows the prices (deal graph)."""
    d = ctx.deal
    prices = (d.allowed_prices if ctx.spec.may_quote_prices else ()) if allowed is None else tuple(allowed)
    lines = [
        f"Deal {d.number}, stage {d.stage.value}. Reply ONLY in {language_name(ctx.language)}.",
        _CHANNEL_STYLE.get(ctx.channel, ""),
    ]
    if d.missing_fields:
        lines.append("Still missing before qualification: " + ", ".join(d.missing_fields) + ".")
    lines.extend(
        f"Requirement: {r.product_text or r.product_sku or '?'}, qty {r.quantity or '?'} {r.unit}, "
        f"to {r.delivery_country or '?'}."
        for r in d.requirements
    )
    if prices:
        lines.append(
            "ALLOWED PRICES (state no other amount): "
            + "; ".join(f"{p.currency} {p.amount} ({p.label})" for p in prices)
        )
    else:
        lines.append("ALLOWED PRICES: none. Do not state any price or amount.")
    if d.estimated_delivery:
        lines.append(
            f"Estimated delivery (always say 'estimated, confirmed by our team'): {d.estimated_delivery}."
        )
    if ctx.summary:
        lines.append(f"Earlier in this conversation: {ctx.summary}")
    return "\n".join(x for x in lines if x)


class RoleMiddleware(AgentMiddleware[AgentState[Any], AgentContext]):
    async def awrap_model_call(
        self,
        request: ModelRequest[AgentContext],
        handler: Callable[[ModelRequest[AgentContext]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        ctx = request.runtime.context
        static: dict[str, Any] = {
            "type": "text",
            "text": ctx.prompts.system_prompt(ctx.spec, ctx.language).text,
        }
        if isinstance(request.model, ChatAnthropic):
            static["cache_control"] = {"type": "ephemeral"}  # caches tools + role prompt across turns
        system = SystemMessage(content=[static, {"type": "text", "text": deal_facts(ctx)}])
        allowed = set(ctx.spec.tools)
        tools = [t for t in request.tools if isinstance(t, dict) or t.name in allowed]
        return await handler(request.override(system_message=system, tools=tools))


class ToolAuditMiddleware(AgentMiddleware[AgentState[Any], AgentContext]):
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        ctx = cast("AgentContext", request.runtime.context)
        call = request.tool_call
        name, call_id = call["name"], call["id"] or ""
        args = {k: mask_pii(v) if isinstance(v, str) else v for k, v in call["args"].items()}
        started = time.perf_counter()

        def record(status: Any, summary: str) -> None:
            ctx.log.tool_calls.append(
                ToolCallRecord(
                    name, args, status, mask_pii(summary)[:500], int((time.perf_counter() - started) * 1000)
                )
            )

        if name not in ctx.spec.tools:
            record("denied", "tool not allowed for role")
            return ToolMessage(content=f"Tool {name} is not available.", tool_call_id=call_id, status="error")
        try:
            result = await handler(request)
        except DomainError as exc:  # CRM refused (rule, conflict, not found): let the model explain
            record("error", exc.detail)
            return ToolMessage(content=f"Not possible: {exc.detail}", tool_call_id=call_id, status="error")
        if isinstance(result, ToolMessage):
            record("error" if result.status == "error" else "ok", result.text)
        else:
            record("ok", "command")
        return result
