"""One guarded model call used by the StateGraph nodes (deal and quotation graphs)."""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from kx.agents.context import AgentContext
from kx.agents.guard import PriceGuard, with_note
from kx.agents.middleware import deal_facts
from kx.agents.models import ModelSet
from kx.core.contracts import AllowedPrice


async def compose(  # noqa: PLR0917 - mirrors the prompt parts
    models: ModelSet,
    ctx: AgentContext,
    buyer_text: str,
    instruction: str,
    allowed: Sequence[AllowedPrice],
    config: RunnableConfig | None = None,
) -> str:
    """Write a reply following `instruction`; the guard allows exactly `allowed` and nothing else."""
    base = SystemMessage(
        content=[
            {"type": "text", "text": ctx.prompts.system_prompt(ctx.spec, ctx.language).text},
            {"type": "text", "text": f"{deal_facts(ctx, allowed)}\nINSTRUCTION: {instruction}"},
        ]
    )
    runnable = models.runnable()

    async def generate(system: SystemMessage) -> str:
        messages: list[BaseMessage] = [system, HumanMessage(buyer_text)]
        return (await runnable.ainvoke(messages, config)).text

    async def regenerate(note: str) -> str | None:
        return await generate(with_note(base, note))

    guard = PriceGuard.for_context(ctx, allowed)
    return await guard.enforce(await generate(base), regenerate, ctx.log)
