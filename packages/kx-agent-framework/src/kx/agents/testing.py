"""Test doubles shipped with the package (used by our tests, the offline eval runner and app tests).

`ScriptedChatModel`: langchain-core's fake chat models cannot bind tools or return structured output,
so this one replays scripted AIMessages (with tool_calls) and pydantic objects for
`with_structured_output`. `FakeCrmGateway` implements the CrmGateway Protocol in memory.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel, PrivateAttr

from kx.core.contracts import (
    AiRole,
    AllowedPrice,
    Channel,
    ContactPatch,
    ContactView,
    CounterVerdict,
    CounterVerdictKind,
    DealContext,
    DealStage,
    DeliveryEstimateView,
    OutboundRequest,
    QualifyResult,
    QuoteLineView,
    QuoteView,
    RequirementPatch,
)


class ScriptedChatModel(BaseChatModel):
    """Replays `script` in order; str -> AIMessage, BaseModel/dict -> structured output item."""

    script: list[Any]
    _i: int = PrivateAttr(default=0)
    _calls: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def calls(self) -> list[list[BaseMessage]]:
        return self._calls

    def _next(self) -> Any:
        if self._i >= len(self.script):
            raise AssertionError(f"ScriptedChatModel exhausted after {self._i} calls")
        item = self.script[self._i]
        self._i += 1
        return item

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._calls.append(list(messages))
        item = self._next()
        msg = item if isinstance(item, AIMessage) else AIMessage(content=str(item))
        if msg.usage_metadata is None:
            msg = msg.model_copy(
                update={"usage_metadata": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop, None, **kwargs)

    def bind_tools(
        self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self

    def with_structured_output(
        self, schema: Any, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[LanguageModelInput, Any]:
        def run(messages: Any) -> Any:
            self._calls.append(list(messages) if isinstance(messages, list) else [])
            item = self._next()
            return item if isinstance(item, BaseModel) else schema.model_validate(item)

        return RunnableLambda(run)


def tool_call(name: str, args: dict[str, Any] | None = None, call_id: str | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": call_id or f"call_{name}", "type": "tool_call"}],
    )


def make_deal(
    stage: DealStage = DealStage.NEW,
    *,
    ai_enabled: bool = True,
    currency: str = "AED",
    allowed: Sequence[tuple[str, str]] = (),
    missing: Sequence[str] = (),
    language: str = "en",
    unit_price: str | None = None,
) -> DealContext:
    quote = None
    if unit_price is not None:
        quote = QuoteView(
            quote_number="Q-1001",
            version_no=1,
            status="sent",
            currency=currency,
            total=Decimal(unit_price) * 50_000,
            valid_until=None,
            lines=(
                QuoteLineView(
                    description="PET bottle 500 ml",
                    quantity=Decimal(50_000),
                    unit="pcs",
                    unit_price=Decimal(unit_price),
                    line_total=Decimal(unit_price) * 50_000,
                ),
            ),
        )
    return DealContext(
        deal_id=uuid.uuid7(),
        number="D-1001",
        stage=stage,
        ai_enabled=ai_enabled,
        currency=currency,
        contact=ContactView(
            contact_id=uuid.uuid7(),
            name="Omar",
            company="Gulf Foods",
            language=language,
            timezone="Asia/Dubai",
            preferred_channel="whatsapp",
            has_phone=True,
            has_email=False,
            phone_verified=True,
        ),
        requirements=(),
        missing_fields=tuple(missing),
        latest_quote=quote,
        allowed_prices=tuple(
            AllowedPrice(amount=Decimal(a), currency=currency, label=lbl) for a, lbl in allowed
        ),
        concessions_left=3,
        estimated_delivery=None,
        next_action_at=None,
    )


class FakeCrmGateway:
    """In-memory CrmGateway. `secret_unit_cost` exists only to prove nothing exposes it."""

    def __init__(self, deal: DealContext, verdicts: Sequence[CounterVerdict] = ()) -> None:
        self.deal = deal
        self.verdicts = list(verdicts)
        self.secret_unit_cost = Decimal("0.1234")  # never part of any DTO
        self.calls: list[tuple[str, Any]] = []

    async def resolve_deal(
        self, channel: Channel, address: str | None, visitor_ref: uuid.UUID | None
    ) -> uuid.UUID:
        return self.deal.deal_id

    async def deal_context(self, deal_id: uuid.UUID) -> DealContext:
        return self.deal

    async def update_requirement(self, deal_id: uuid.UUID, patch: RequirementPatch, actor: AiRole) -> None:
        self.calls.append(("update_requirement", patch))

    async def set_contact(self, deal_id: uuid.UUID, patch: ContactPatch, actor: AiRole) -> None:
        self.calls.append(("set_contact", patch))

    async def try_qualify(self, deal_id: uuid.UUID, actor: AiRole) -> QualifyResult:
        self.calls.append(("try_qualify", None))
        return QualifyResult(qualified=not self.deal.missing_fields, missing_fields=self.deal.missing_fields)

    async def request_quote(self, deal_id: uuid.UUID, actor: AiRole) -> None:
        self.calls.append(("request_quote", None))

    async def evaluate_counter(
        self, deal_id: uuid.UUID, ask_unit_price: Decimal, ask_quantity: Decimal | None, channel: Channel
    ) -> CounterVerdict:
        self.calls.append(("evaluate_counter", ask_unit_price))
        verdict = (
            self.verdicts.pop(0)
            if self.verdicts
            else CounterVerdict(
                kind=CounterVerdictKind.NEEDS_MANAGER,
                offer_unit_price=None,
                currency=self.deal.currency,
                concessions_left=0,
            )
        )
        if verdict.kind is CounterVerdictKind.NEEDS_MANAGER:
            self.deal = self.deal.model_copy(update={"ai_enabled": False})  # as kx.crm does
        return verdict

    async def pause_ai(self, deal_id: uuid.UUID, reason: str, actor: AiRole) -> None:
        self.calls.append(("pause_ai", reason))
        self.deal = self.deal.model_copy(update={"ai_enabled": False})

    async def mark_agreed(self, deal_id: uuid.UUID, actor: AiRole) -> None:
        self.calls.append(("mark_agreed", None))

    async def schedule_follow_up(self, deal_id: uuid.UUID, due_at: datetime, reason: str) -> None:
        self.calls.append(("schedule_follow_up", due_at))

    async def delivery_estimate(self, deal_id: uuid.UUID) -> DeliveryEstimateView | None:
        return None

    async def add_timeline(self, deal_id: uuid.UUID, kind: str, summary: str, actor: AiRole) -> None:
        self.calls.append(("add_timeline", kind))


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[OutboundRequest] = []

    async def send(self, request: OutboundRequest) -> uuid.UUID:
        self.sent.append(request)
        return uuid.uuid7()


_WORD = re.compile(r"\w+")


class HashEmbedder:
    """Deterministic bag-of-words embedder (1024-dim, L2-normalised): shared words => high cosine."""

    model = "hash-bow-1024"

    def __init__(self, dim: int = 1024, tokenize: Callable[[str], list[str]] | None = None) -> None:
        self._dim = dim
        self._tok = tokenize or (lambda t: _WORD.findall(t.lower()))

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self._dim
        for w in self._tok(text):
            v[int.from_bytes(hashlib.blake2b(w.encode(), digest_size=4).digest()) % self._dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)
