"""Replay AI-trace spans shaped exactly like the platform's (kx/agents/tracing.py: one LLM span per model call,
OpenInference attributes, input/output up to 8,000 characters, deal/role/graph/run ids) to an OTLP/HTTP
endpoint, so Phoenix, Langfuse and Opik can be compared on the same stream.

  python trace_replay.py --endpoint http://localhost:26006/v1/traces --spans 2000 --rate 4
  python trace_replay.py --endpoint http://localhost:3000/api/public/otel/v1/traces --header "Authorization=Basic ..."
"""

from __future__ import annotations

import argparse
import json
import random
import time
import uuid
from pathlib import Path

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

PROMPT = ("System: You are Arifa Pack's sales assistant. Never state a price that is not in an approved quote. "
          "Tools: search_knowledge, update_requirements, request_quote. Buyer history follows.\n")
TURNS = ["Buyer: I need 750 ml aluminium containers with lids for my restaurant in Muscat.",
         "Assistant: Thank you. How many pieces do you need per month, and should we print your logo?",
         "Buyer: About 20,000 pieces a month, with our logo on the lid.",
         "Knowledge: 750 ml container, coil 8011, paper or plastic lid, 500 per carton, custom print available."]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", required=True)
    p.add_argument("--header", action="append", default=[])
    p.add_argument("--project", default="pack")
    p.add_argument("--spans", type=int, default=2000)
    p.add_argument("--rate", type=float, default=4.0, help="spans per second")
    p.add_argument("--out", type=Path)
    a = p.parse_args()
    headers = dict(h.split("=", 1) for h in a.header)
    provider = TracerProvider(resource=Resource.create({"service.name": "kx-agents", "openinference.project.name": a.project}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=a.endpoint, headers=headers)))
    tracer = provider.get_tracer("bench")
    t0 = time.time()
    chars = 0
    for n in range(a.spans):
        history = PROMPT + "\n".join(random.choice(TURNS) for _ in range(random.randint(6, 40)))
        inp = history[-8000:]
        out = random.choice(TURNS[1::2])
        chars += len(inp) + len(out)
        span = tracer.start_span(random.choice(["intake", "qualification", "negotiation", "support"]))
        span.set_attribute("openinference.span.kind", "LLM")
        span.set_attribute("input.value", inp)
        span.set_attribute("output.value", out)
        span.set_attribute("llm.model_name", random.choice(["claude-haiku-5-5", "claude-sonnet-5-5"]))
        span.set_attribute("llm.token_count.prompt", len(inp) // 4)
        span.set_attribute("llm.token_count.completion", len(out) // 4)
        for key in ("deal_ref", "run_id", "role"):
            span.set_attribute(f"kx.{key}", str(uuid.uuid4()))
        span.end()
        time.sleep(max(0.0, (n + 1) / a.rate - (time.time() - t0)))
    provider.force_flush()
    provider.shutdown()
    res = {"start": t0, "end": time.time(), "spans": a.spans, "text_chars": chars, "endpoint": a.endpoint}
    if a.out:
        a.out.write_text(json.dumps(res, indent=1))
    print(json.dumps(res))


if __name__ == "__main__":
    main()
