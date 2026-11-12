"""AI traces to Phoenix in the UAE (D29), over OpenTelemetry (OTLP/HTTP). No-op when not configured.

Every model call becomes one span named after the run, with the OpenInference attribute names Phoenix
understands (`openinference.span.kind`, `llm.model_name`, `llm.token_count.*`, `input.value`,
`output.value`) plus the deal, role, graph and run ids. Buyer phones and emails are masked with
`kx.core.logging.mask_pii` before a span leaves the process (UAE PDPL), even though Phoenix runs in our own
UAE account. Replaces Langfuse (11 Nov).
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from kx.core.logging import get_logger, mask_pii

log = get_logger(__name__)
MAX_CHARS = 8000


def mask_payload(*, data: Any, **_: Any) -> Any:
    """Recursively mask strings in span inputs and outputs."""
    if isinstance(data, str):
        return mask_pii(data)
    if isinstance(data, dict):
        return {k: mask_payload(data=v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [mask_payload(data=v) for v in data]
    return data


def _text(messages: list[list[BaseMessage]]) -> str:
    lines = [f"{m.type}: {m.text}" for batch in messages for m in batch]
    return mask_pii("\n".join(lines))[-MAX_CHARS:]


class _SpanHandler(AsyncCallbackHandler):
    """One OTel span per chat-model call; the run's ids come from the LangChain metadata."""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer
        self._spans: dict[uuid.UUID, Any] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: uuid.UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        meta = metadata or {}
        span = self._tracer.start_span(str(meta.get("graph") or "llm"))
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("input.value", _text(messages))
        for key in ("deal_ref", "role", "graph", "run_id"):
            if meta.get(key):
                span.set_attribute(f"kx.{key}", str(meta[key]))
        self._spans[run_id] = span

    async def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        span = self._spans.pop(run_id, None)
        if span is None:
            return
        for gens in response.generations:
            for gen in gens:
                msg = gen.message if isinstance(gen, ChatGeneration) else None
                if not isinstance(msg, AIMessage):
                    continue
                span.set_attribute("output.value", mask_pii(msg.text)[:MAX_CHARS])
                model = msg.response_metadata.get("model_name") or msg.response_metadata.get("model")
                if model:
                    span.set_attribute("llm.model_name", str(model))
                if usage := msg.usage_metadata:
                    span.set_attribute("llm.token_count.prompt", usage.get("input_tokens", 0))
                    span.set_attribute("llm.token_count.completion", usage.get("output_tokens", 0))
        span.end()

    async def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **kwargs: Any) -> None:
        span = self._spans.pop(run_id, None)
        if span is not None:
            span.record_exception(error)
            span.end()


class Tracer:
    def __init__(
        self,
        endpoint: str | None = None,
        project: str = "kairox",
        environment: str | None = None,
    ) -> None:
        """`endpoint`: Phoenix's OTLP/HTTP traces URL, e.g. http://phoenix.<ns>:6006/v1/traces."""
        self.enabled = bool(endpoint)
        self._handler: BaseCallbackHandler | None = None
        if not self.enabled:
            return
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415 - only when on
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

        assert endpoint is not None
        resource = Resource.create(
            {
                "service.name": "kx-agents",
                "openinference.project.name": project,
                "deployment.environment": environment or "",
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        self._provider = provider
        self._handler = _SpanHandler(provider.get_tracer("kx.agents"))
        log.info("tracing.phoenix", endpoint=endpoint, project=project)

    def callbacks(self) -> list[BaseCallbackHandler]:
        return [self._handler] if self._handler is not None else []

    @staticmethod
    def metadata(
        *, deal_ref: uuid.UUID | None, role: str, graph: str, run_id: uuid.UUID | None
    ) -> dict[str, Any]:
        return {
            "deal_ref": str(deal_ref) if deal_ref else None,
            "role": role,
            "graph": graph,
            "run_id": str(run_id) if run_id else None,
        }

    def flush(self) -> None:
        if self._handler is not None:
            self._provider.force_flush()
