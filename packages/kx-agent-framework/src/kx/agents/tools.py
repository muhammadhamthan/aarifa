"""Agent tools: thin async wrappers over the injected `CrmGateway` and the KB.

Every tool takes the deal from `ToolRuntime.context` (never from model arguments) and returns only
`kx.core.contracts` DTOs, which by construction carry no cost, markup, margin range or floor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from langchain.tools import ToolRuntime, tool
from langchain_core.tools import BaseTool

from kx.agents.context import AgentContext
from kx.core.contracts import ContactPatch, DealStage, RequirementPatch

Runtime = ToolRuntime[AgentContext]
_MAX_FOLLOW_UP: Final = timedelta(days=30)


@tool
async def get_deal_context(runtime: Runtime) -> str:
    """Current deal: stage, requirements, missing fields, latest quote and the prices you may state."""
    ctx = runtime.context
    fresh = await ctx.gateway.deal_context(ctx.deal_id)
    return fresh.model_dump_json(exclude={"deal_id": True, "contact": {"contact_id"}})


@tool
async def update_requirement(requirement: RequirementPatch, runtime: Runtime) -> str:
    """Save what the buyer said about product, quantity, unit, application or delivery country/city."""
    ctx = runtime.context
    await ctx.gateway.update_requirement(ctx.deal_id, requirement, ctx.spec.role)
    return "saved"


@tool
async def set_contact(contact: ContactPatch, runtime: Runtime) -> str:
    """Save the buyer's name, company, phone, email, preferred channel or language."""
    ctx = runtime.context
    await ctx.gateway.set_contact(ctx.deal_id, contact, ctx.spec.role)
    return "saved"


@tool
async def try_qualify(runtime: Runtime) -> str:
    """Check whether the deal has everything needed for a quotation; lists what is still missing."""
    ctx = runtime.context
    return (await ctx.gateway.try_qualify(ctx.deal_id, ctx.spec.role)).model_dump_json()


@tool
async def search_knowledge(query: str, runtime: Runtime) -> str:
    """Search approved product, factory and FAQ content. Cite the title of what you use."""
    ctx = runtime.context
    if ctx.knowledge is None:
        return "No approved content available; say the team will confirm."
    result = await ctx.knowledge.search(query, ctx.language)
    if result.weak:
        await ctx.knowledge.record_gap(query, ctx.language, ctx.conversation_id)
        return "No approved answer found. Say our team will confirm this detail."
    return "\n\n".join(f"[{h.title}] {h.text}" for h in result.hits)


@tool
async def delivery_estimate(runtime: Runtime) -> str:
    """Estimated ready and delivery dates (always say: estimated, confirmed by our team)."""
    ctx = runtime.context
    est = await ctx.gateway.delivery_estimate(ctx.deal_id)
    return "No estimate yet; the team will confirm." if est is None else est.model_dump_json()


@tool
async def request_quote(runtime: Runtime) -> str:
    """Ask the team to prepare the quotation (only once the deal is qualified)."""
    ctx = runtime.context
    if ctx.deal.stage is not DealStage.QUALIFIED:
        return f"Not possible at stage {ctx.deal.stage.value}."
    await ctx.gateway.request_quote(ctx.deal_id, ctx.spec.role)
    return "Quotation requested; it will be reviewed by the team before it is sent."


@tool
async def schedule_follow_up(due_at: datetime, reason: str, runtime: Runtime) -> str:
    """Schedule a follow-up the buyer asked for (e.g. 'call me next week'). Max 30 days ahead."""
    now = datetime.now(UTC)
    due = due_at if due_at.tzinfo else due_at.replace(tzinfo=UTC)
    if not now < due <= now + _MAX_FOLLOW_UP:
        return "Choose a time within the next 30 days."
    ctx = runtime.context
    await ctx.gateway.schedule_follow_up(ctx.deal_id, due, reason[:200])
    return "scheduled"


@tool
async def add_timeline(summary: str, runtime: Runtime) -> str:
    """Add a short note to the deal timeline for the sales team."""
    ctx = runtime.context
    await ctx.gateway.add_timeline(ctx.deal_id, "ai_note", summary[:1000], ctx.spec.role)
    return "noted"


@tool
async def handoff_to_human(reason: str, runtime: Runtime) -> str:
    """Hand the conversation to a human (buyer asks for a person, complaint, or anything you cannot do)."""
    ctx = runtime.context
    await ctx.gateway.pause_ai(ctx.deal_id, "human_takeover", ctx.spec.role)
    await ctx.gateway.add_timeline(ctx.deal_id, "ai_handoff", reason[:1000], ctx.spec.role)
    ctx.log.handoff = True
    return "handed over; tell the buyer a team member will reply shortly"


ALL_TOOLS: Final[tuple[BaseTool, ...]] = (
    get_deal_context,
    update_requirement,
    set_contact,
    try_qualify,
    search_knowledge,
    delivery_estimate,
    request_quote,
    schedule_follow_up,
    add_timeline,
    handoff_to_human,
)
