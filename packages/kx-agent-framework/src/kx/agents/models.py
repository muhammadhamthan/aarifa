"""Model factory: tier -> chat model, with provider fallback.

Providers: the Anthropic API (langchain-anthropic `ChatAnthropic`) and AWS Bedrock. langchain-anthropic
1.7.9 ships no Bedrock class, but `ChatAnthropic` documents a transport seam: a subclass that overrides
`_llm_type` and the SDK clients gets Bedrock-correct prompt caching (block-level breakpoints). We use
that seam with the official `anthropic.AsyncAnthropicBedrock` client, so no langchain-aws dependency.

Live agents (`create_agent`) get fallbacks through `ModelFallbackMiddleware` (a `.with_fallbacks()`
runnable is not a chat model and cannot bind tools); plain node calls use `.with_fallbacks()`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Final, Protocol, runtime_checkable

import anthropic
from langchain.agents.middleware import AgentMiddleware, ModelFallbackMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, SecretStr

from kx.agents.registry import ModelTier

ANTHROPIC_MODEL_IDS: Final[dict[ModelTier, str]] = {
    ModelTier.FAST: "claude-haiku-5-5",
    ModelTier.SMART: "claude-sonnet-5-5",
    ModelTier.JUDGE: "claude-opus-5-5",
}


@dataclass(frozen=True, slots=True)
class ModelOptions:
    temperature: float = 0.2  # sales replies: consistent, still natural
    max_tokens: int = 1024
    timeout_s: float = 30.0
    max_retries: int = 2


@runtime_checkable
class ModelProvider(Protocol):
    name: str

    def chat_model(self, tier: ModelTier, options: ModelOptions) -> BaseChatModel: ...


class AnthropicApiProvider:
    name = "anthropic"

    def __init__(
        self, api_key: SecretStr | None = None, model_ids: Mapping[ModelTier, str] = ANTHROPIC_MODEL_IDS
    ) -> None:
        self._key = api_key
        self._ids = dict(model_ids)

    def chat_model(self, tier: ModelTier, options: ModelOptions) -> BaseChatModel:
        kwargs: dict[str, Any] = {"api_key": self._key} if self._key is not None else {}
        return ChatAnthropic(
            model_name=self._ids[tier],
            temperature=options.temperature,
            max_tokens_to_sample=options.max_tokens,
            timeout=options.timeout_s,
            max_retries=options.max_retries,
            stop=None,
            **kwargs,
        )


class BedrockChatAnthropic(ChatAnthropic):
    """ChatAnthropic over AWS Bedrock (IAM task role credentials; model ids are inference profiles)."""

    aws_region: str = "me-central-1"

    @property
    def _llm_type(self) -> str:
        return "anthropic-bedrock-chat"  # not "anthropic-chat": switches caching to block breakpoints

    @cached_property
    def _client(self) -> anthropic.AnthropicBedrock:  # type: ignore[override]
        return anthropic.AnthropicBedrock(
            aws_region=self.aws_region, max_retries=self.max_retries, timeout=self.default_request_timeout
        )

    @cached_property
    def _async_client(self) -> anthropic.AsyncAnthropicBedrock:  # type: ignore[override]
        return anthropic.AsyncAnthropicBedrock(
            aws_region=self.aws_region, max_retries=self.max_retries, timeout=self.default_request_timeout
        )


class BedrockProvider:
    name = "bedrock"

    def __init__(self, model_ids: Mapping[ModelTier, str], region: str = "me-central-1") -> None:
        self._ids = dict(model_ids)
        self._region = region

    def chat_model(self, tier: ModelTier, options: ModelOptions) -> BaseChatModel:
        return BedrockChatAnthropic(
            model=self._ids[tier],
            aws_region=self._region,
            temperature=options.temperature,
            max_tokens_to_sample=options.max_tokens,
            timeout=options.timeout_s,
            max_retries=options.max_retries,
            stop=None,
        )


@dataclass(frozen=True, slots=True)
class ModelSet:
    """A tier's primary model plus ordered fallbacks."""

    primary: BaseChatModel
    fallbacks: tuple[BaseChatModel, ...] = ()

    @property
    def name(self) -> str:
        return str(
            getattr(self.primary, "model", None)
            or getattr(self.primary, "model_name", None)
            or type(self.primary).__name__
        )

    def runnable(self) -> Runnable[LanguageModelInput, BaseMessage]:
        return self.primary.with_fallbacks(list(self.fallbacks)) if self.fallbacks else self.primary

    def structured[T: BaseModel](self, schema: type[T]) -> Runnable[LanguageModelInput, T]:
        """Native JSON-schema structured output (Claude 5.5 models refuse forced tool_choice)."""

        def build(m: BaseChatModel) -> Runnable[LanguageModelInput, T]:
            return m.with_structured_output(schema, method="json_schema")  # type: ignore[return-value]

        first = build(self.primary)
        return first.with_fallbacks([build(m) for m in self.fallbacks]) if self.fallbacks else first

    def middleware(self) -> list[AgentMiddleware[Any, Any]]:
        return [ModelFallbackMiddleware(*self.fallbacks)] if self.fallbacks else []


@dataclass
class ModelRouter:
    """Tier -> ModelSet. The first provider is primary (Bedrock when configured), the rest fall back."""

    providers: Sequence[ModelProvider]
    options: Mapping[ModelTier, ModelOptions] = field(default_factory=dict)
    _cache: dict[ModelTier, ModelSet] = field(default_factory=dict, init=False)

    def for_tier(self, tier: ModelTier) -> ModelSet:
        if tier not in self._cache:
            opts = self.options.get(tier, ModelOptions())
            models = [p.chat_model(tier, opts) for p in self.providers]
            self._cache[tier] = ModelSet(models[0], tuple(models[1:]))
        return self._cache[tier]

    @classmethod
    def static(cls, model: BaseChatModel) -> ModelRouter:
        """Every tier answered by one model (tests, evals with a fake model)."""
        router = cls(providers=())
        for tier in ModelTier:
            router._cache[tier] = ModelSet(model)
        return router
