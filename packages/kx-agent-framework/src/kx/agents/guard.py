"""Reply safety gate. Every AI sentence that can reach a buyer (chat, WhatsApp, email, voice before
TTS) passes `PriceGuard.enforce`: amounts must be in `DealContext.allowed_prices` (kx.core.price_guard,
the single implementation), and cost/margin talk is refused. On a violation the model regenerates ONCE
with a corrective note; if it still fails, a fixed fallback sentence replaces the reply and a
guard event is recorded. A wrong number said to a buyer is a commitment, so we fail closed.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, SystemMessage

from kx.agents.context import AgentContext, GuardEventRecord, RunLog
from kx.agents.registry import Phrase
from kx.core.contracts import AllowedPrice, Channel
from kx.core.price_guard import check_prices

_DISCLOSURE: Final = re.compile(
    r"\b(?:mark-?ups?|margins?|floor prices?|cost prices?|our costs?|unit costs?|profit)\b"
    r"|هامش|التكلفة|成本|利润",
    re.IGNORECASE,
)
_INJECTION: Final = re.compile(
    r"ignore (?:all |your |the )?(?:previous |above |prior )?(?:instructions|rules)|system prompt|"
    r"developer mode|jailbreak|reveal (?:your|the) (?:prompt|cost|margin)|show (?:me )?(?:the |your )?"
    r"(?:cost|margin|markup|floor)|تجاهل التعليمات|忽略(?:之前的)?指令",
    re.IGNORECASE,
)

_CHANNEL_LIMITS: Final[dict[str, int]] = {
    "web_voice": 600,
    "phone": 600,
    "web_chat": 1500,
    "whatsapp": 1500,
    "email": 4000,
}

Regenerate = Callable[[str], Awaitable[str | None]]


def looks_like_injection(text: str) -> bool:
    return _INJECTION.search(text) is not None


def clip_for_channel(text: str, channel: Channel) -> str:
    """Hard length cap per channel, cut at the last sentence end (voice must stay short)."""
    limit = _CHANNEL_LIMITS.get(channel, 1500)
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(p) for p in (". ", "! ", "? ", "。", "؟", "\n"))
    return head[: cut + 1].rstrip() if cut > limit // 2 else head.rstrip()


class PriceGuard:
    def __init__(self, allowed: Sequence[AllowedPrice], currency: str, fallback: str) -> None:
        self.allowed = tuple(allowed)
        self.currency = currency
        self.fallback = fallback

    @classmethod
    def for_context(cls, ctx: AgentContext, allowed: Sequence[AllowedPrice] | None = None) -> PriceGuard:
        """Roles that may not quote get an empty allow-list: any amount at all is a violation."""
        if allowed is None:
            allowed = ctx.deal.allowed_prices if ctx.spec.may_quote_prices else ()
        return cls(allowed, ctx.deal.currency, ctx.prompts.phrase(Phrase.PRICE_FALLBACK, ctx.language))

    def violations(self, text: str) -> tuple[list[str], bool]:
        """(offending raw amounts, mentions cost/margin)."""
        result = check_prices(text, self.allowed, default_currency=self.currency)
        return [v.raw for v in result.violations], _DISCLOSURE.search(text) is not None

    def corrective_note(self) -> str:
        if not self.allowed:
            return (
                "CORRECTION: your previous draft stated a price or amount. You may not state ANY price, "
                "amount, discount or cost in this reply. Rewrite it without numbers for money."
            )
        listed = "; ".join(f"{p.currency} {p.amount} ({p.label})" for p in self.allowed)
        return (
            f"CORRECTION: you may only state these prices, exactly as written: {listed}. "
            "Never mention cost, margin or markup. Rewrite the reply."
        )

    async def enforce(self, text: str, regenerate: Regenerate | None, log: RunLog) -> str:
        bad, disclosure = self.violations(text)
        if not bad and not disclosure:
            return text
        detail: dict[str, Any] = {"violations": bad, "disclosure": disclosure}
        if regenerate is not None and log.regenerations == 0:
            log.regenerations += 1
            retry = await regenerate(self.corrective_note())
            if retry is not None:
                bad2, disc2 = self.violations(retry)
                if not bad2 and not disc2:
                    log.guard_events.append(GuardEventRecord(_kind(bad, disclosure), "rewritten", detail))
                    return retry
                detail["after_retry"] = bad2
        log.guard_events.append(GuardEventRecord(_kind(bad, disclosure), "blocked", detail))
        return self.fallback


def _kind(bad: list[str], disclosure: bool) -> Any:
    return "price_number" if bad else "disclosure"


def final_ai(response: ModelResponse[Any]) -> AIMessage | None:
    """The buyer-facing message: an AI message with no pending tool calls."""
    msg = response.result[-1] if response.result else None
    return msg if isinstance(msg, AIMessage) and not msg.tool_calls else None


def with_note(system: SystemMessage | None, note: str) -> SystemMessage:
    blocks: list[str | dict[str, Any]] = []
    if system is not None:
        blocks = (
            list(system.content)
            if isinstance(system.content, list)
            else [{"type": "text", "text": system.content}]
        )
    return SystemMessage(content=[*blocks, {"type": "text", "text": note}])


class PriceGuardMiddleware(AgentMiddleware[AgentState[Any], AgentContext]):
    """Wraps every model call of the conversation agent; must sit inside RoleMiddleware so the
    corrective note is appended to the role prompt rather than replaced by it."""

    async def awrap_model_call(
        self,
        request: ModelRequest[AgentContext],
        handler: Callable[[ModelRequest[AgentContext]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        response = await handler(request)
        msg = final_ai(response)
        if msg is None:
            return response
        ctx = request.runtime.context

        async def regenerate(note: str) -> str | None:
            retry = final_ai(
                await handler(request.override(system_message=with_note(request.system_message, note)))
            )
            return None if retry is None else retry.text

        text = await PriceGuard.for_context(ctx).enforce(msg.text, regenerate, ctx.log)
        if text == msg.text:
            return response
        return ModelResponse(result=[AIMessage(content=text, id=msg.id)])
