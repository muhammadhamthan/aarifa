"""D29 Phoenix tracing: one OpenInference-style span per model call, buyer contact details masked."""

from __future__ import annotations

import uuid

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kx.agents.tracing import Tracer, _SpanHandler


async def test_span_carries_model_tokens_and_masked_text() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    handler = _SpanHandler(provider.get_tracer("test"))
    run = uuid.uuid4()
    await handler.on_chat_model_start(
        {},
        [[HumanMessage("call me on +971501234567 or omar@buyer.example")]],
        run_id=run,
        metadata={"graph": "conversation", "deal_ref": "d-1", "role": "communication"},
    )
    reply = AIMessage(
        "Thanks!",
        response_metadata={"model_name": "claude-haiku-5-5"},
        usage_metadata={"input_tokens": 120, "output_tokens": 8, "total_tokens": 128},
    )
    await handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=reply)]]), run_id=run)
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert span.name == "conversation" and attrs["openinference.span.kind"] == "LLM"
    assert attrs["llm.model_name"] == "claude-haiku-5-5" and attrs["llm.token_count.prompt"] == 120
    assert "+971501234567" not in str(attrs["input.value"]) and "omar@buyer.example" not in str(
        attrs["input.value"]
    )
    assert attrs["kx.deal_ref"] == "d-1"


def test_tracing_is_off_without_a_collector() -> None:
    assert Tracer().callbacks() == [] and not Tracer().enabled
