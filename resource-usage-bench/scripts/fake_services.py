"""Local stand-ins for every outside vendor the platform calls, so a load test runs the app's real code
without paying anyone or sending anything off this PC.

One HTTP server (port 9000) answers, by path:

* Claude API      POST /v1/messages            text, tool_use or JSON-schema answers after ~3 s
* Meta WhatsApp   /<graph version>/...          message ids for sends, media ids
* Amazon S3       PUT/GET/HEAD /<bucket>/<key>  objects kept under /data/s3 (so stored bytes are real)
* Amazon SES v2   POST /v2/email/outbound-emails  message id; sign-in codes captured for the load script
* Twilio          POST /2010-04-01/...          call sids
* Payload CMS     GET /api/<collection>/<id>    a published product page per locale (for re-indexing)

Every request is counted per vendor (requests, bytes in, bytes out): GET /_stats, POST /_reset.
This measures the bandwidth the platform would use towards each outside service.
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import json
import os
import random
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response

DATA = Path(os.environ.get("FAKE_DATA_DIR", "/data"))
LLM_DELAY = float(os.environ.get("FAKE_LLM_DELAY_S", "3.0"))
TOOL_RATE = float(os.environ.get("FAKE_TOOL_RATE", "0.3"))

app = FastAPI()
STATS: dict[str, dict[str, float]] = defaultdict(lambda: {"requests": 0, "bytes_in": 0, "bytes_out": 0})
CODES: dict[str, str] = {}
LLM: dict[str, float] = defaultdict(float)

REPLIES = [
    "Thank you for your message. Our aluminium containers come in several sizes with matching lids, and we "
    "can add custom printing on request. Could you tell me which size you need, roughly how many pieces, "
    "and the country they should be delivered to?",
    "Happy to help. We make food-grade aluminium containers and foil in our factory in Ajman. To prepare "
    "the right offer, may I ask about your business, the product you are interested in and the quantity?",
    "Understood. I have noted your requirement and shared it with our sales team, who will prepare a "
    "formal quotation for you. Is WhatsApp the best way to send it, or would you prefer email?",
    "Our containers are made from aluminium coil, are recyclable and suitable for freezer and oven use. "
    "Would you like the product datasheet, or should I ask a colleague to call you?",
]


def _vendor(path: str) -> str:
    if path.startswith("/v1/messages"):
        return "claude"
    if path.startswith("/v2/email"):
        return "ses"
    if path.startswith("/2010-04-01"):
        return "twilio"
    if path.startswith("/api/"):
        return "cms"
    if re.match(r"^/v\d+\.\d+/", path):
        return "whatsapp"
    if path.startswith("/_"):
        return "control"
    return "s3"


@app.middleware("http")
async def count(request: Request, call_next: Any) -> Response:
    body = await request.body()
    response: Response = await call_next(request)
    vendor = _vendor(request.url.path)
    s = STATS[vendor]
    s["requests"] += 1
    s["bytes_in"] += len(body) + sum(len(k) + len(v) + 4 for k, v in request.headers.items())
    length = response.headers.get("content-length")
    s["bytes_out"] += int(length) if length else 0
    return response


# ------------------------------------------------------------------ Claude
def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        node: Any = root
        for part in ref[2:].split("/"):
            node = node.get(part, {})
        return _resolve(node, root)
    return schema


def fill(schema: dict[str, Any], root: dict[str, Any], depth: int = 0) -> Any:
    """The smallest value that satisfies a JSON schema (nulls where allowed, required fields only)."""
    schema = _resolve(schema, root)
    if depth > 8:
        return None
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [_resolve(o, root) for o in schema[key]]
            if any(o.get("type") == "null" for o in options):
                return None
            return fill(options[0], root, depth + 1)
    if "allOf" in schema:
        return fill(schema["allOf"][0], root, depth + 1)
    if "default" in schema:
        return schema["default"]
    kind = schema.get("type")
    if isinstance(kind, list):
        if "null" in kind:
            return None
        kind = kind[0]
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: fill(props[k], root, depth + 1) for k in schema.get("required", []) if k in props}
    if kind == "array":
        return [fill(schema["items"], root, depth + 1)] * schema.get("minItems", 0) if "items" in schema else []
    if kind == "integer":
        return max(int(schema.get("minimum", 1)), 1)
    if kind == "number":
        return float(schema.get("minimum", 1))
    if kind == "boolean":
        return False
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date-time":
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if fmt == "date":
            return time.strftime("%Y-%m-%d", time.gmtime())
        if fmt == "uuid":
            return str(uuid.uuid4())
        return "test"
    return None


def _find_json_schema(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if node.get("type") == "json_schema" and isinstance(node.get("schema"), dict):
            return node["schema"]
        for value in node.values():
            found = _find_json_schema(value)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_json_schema(value)
            if found is not None:
                return found
    return None


def _last_is_tool_result(messages: list[dict[str, Any]]) -> bool:
    if not messages:
        return False
    content = messages[-1].get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


@app.post("/v1/messages")
async def messages(request: Request) -> Response:
    raw = await request.body()
    req = json.loads(raw)
    await asyncio.sleep(max(0.2, random.uniform(LLM_DELAY * 0.6, LLM_DELAY * 1.4)))
    model = str(req.get("model", "claude"))
    content: list[dict[str, Any]]
    stop = "end_turn"
    schema = _find_json_schema(req)
    tools = req.get("tools") or []
    if schema is not None:
        content = [{"type": "text", "text": json.dumps(fill(schema, schema))}]
        LLM["structured"] += 1
    elif tools and not _last_is_tool_result(req.get("messages", [])) and random.random() < TOOL_RATE:
        named = [t for t in tools if isinstance(t, dict) and "name" in t and "input_schema" in t]
        if named:
            preferred = [t for t in named if re.search(r"search|knowledge|product|faq", t["name"])]
            tool = random.choice(preferred or named)
            schema_in = tool["input_schema"]
            args = fill(schema_in, schema_in) or {}
            if isinstance(args, dict):
                for key in list(args):
                    if key in ("query", "question", "text") and args[key] == "test":
                        args[key] = "aluminium container 750 ml with lid"
            content = [{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}", "name": tool["name"], "input": args}]
            stop = "tool_use"
            LLM["tool_use"] += 1
        else:
            content = [{"type": "text", "text": random.choice(REPLIES)}]
    else:
        content = [{"type": "text", "text": random.choice(REPLIES)}]
        LLM["text"] += 1
    input_tokens = max(1, len(raw) // 4)
    output_tokens = max(1, len(json.dumps(content)) // 4)
    cached = int(input_tokens * 0.7) if b"cache_control" in raw else 0
    LLM["input_tokens"] += input_tokens
    LLM["output_tokens"] += output_tokens
    LLM["cache_read_tokens"] += cached
    LLM["calls_" + ("sonnet" if "sonnet" in model else "haiku" if "haiku" in model else "other")] += 1
    body = {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens - cached,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cached,
        },
    }
    return Response(json.dumps(body), media_type="application/json")


# ------------------------------------------------------------------ OpenAI-style chat (voice agent's LLM)
VOICE_REPLIES = [
    "Thank you. Which container size do you need, and roughly how many pieces?",
    "Got it. I will pass this to our sales team and they will send you a quotation on WhatsApp.",
    "Yes, we can print your logo on the lid. May I have your company name?",
]


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    from fastapi.responses import StreamingResponse

    req = json.loads(await request.body())
    text = random.choice(VOICE_REPLIES)
    LLM["voice_calls"] += 1
    created = int(time.time())

    def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
        body = {"id": "chatcmpl-bench", "object": "chat.completion.chunk", "created": created, "model": req.get("model", "x"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(body)}\n\n"

    async def gen() -> Any:
        await asyncio.sleep(random.uniform(1.0, 2.0))  # time to first token of a real hosted model
        yield chunk({"role": "assistant", "content": ""})
        for word in text.split(" "):
            yield chunk({"content": word + " "})
            await asyncio.sleep(0.02)
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    if req.get("stream"):
        return StreamingResponse(gen(), media_type="text/event-stream")
    await asyncio.sleep(1.5)
    body = {"id": "chatcmpl-bench", "object": "chat.completion", "created": created, "model": req.get("model", "x"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220}}
    return Response(json.dumps(body), media_type="application/json")


# ------------------------------------------------------------------ SES v2 (and sign-in code capture)
@app.post("/v2/email/outbound-emails")
async def ses_send(request: Request) -> Response:
    body = (await request.body()).decode("utf-8", "replace")
    to, text = "", body
    try:
        req = json.loads(body)
        to = (req.get("Destination", {}).get("ToAddresses") or [""])[0].lower()
        raw = req.get("Content", {}).get("Raw", {}).get("Data")
        if raw:  # the app sends a full MIME message, base64 in the JSON
            msg = email.message_from_bytes(base64.b64decode(raw), policy=email.policy.default)
            to = to or str(msg.get("To", "")).split("<")[-1].rstrip(">").strip().lower()
            parts = [p for p in msg.walk() if p.get_content_maintype() == "text"]
            text = " ".join(str(p.get_content()) for p in parts)
    except (ValueError, KeyError, TypeError):
        pass
    code = re.search(r"\b(\d{6})\b", text)
    if to and code:
        CODES[to] = code.group(1)
    return Response(json.dumps({"MessageId": f"ses-{uuid.uuid4()}"}), media_type="application/json")


@app.get("/_codes/{email}")
async def last_code(email: str) -> Response:
    return Response(json.dumps({"code": CODES.get(email.lower())}), media_type="application/json")


# ------------------------------------------------------------------ Twilio
@app.post("/2010-04-01/{rest:path}")
async def twilio(rest: str) -> Response:
    return Response(json.dumps({"sid": f"CA{uuid.uuid4().hex}", "status": "queued"}), media_type="application/json")


# ------------------------------------------------------------------ Payload CMS
LANG_TEXT = {
    "en": "Food-grade aluminium container made from coil 8011, suitable for freezer, oven and takeaway use. "
    "Available with paper or plastic lids, custom printing on request, packed in cartons for export.",
    "ar": "حاوية ألمنيوم آمنة للطعام مصنوعة من لفائف 8011، مناسبة للفريزر والفرن وطلبات التوصيل. "
    "متوفرة مع أغطية ورقية أو بلاستيكية، والطباعة حسب الطلب، ومعبأة في كراتين للتصدير.",
    "hi": "खाद्य-ग्रेड एल्यूमिनियम कंटेनर, 8011 कॉइल से बना, फ्रीज़र, ओवन और टेकअवे के लिए उपयुक्त। "
    "कागज़ या प्लास्टिक ढक्कन के साथ उपलब्ध, अनुरोध पर कस्टम प्रिंटिंग, निर्यात के लिए कार्टन में पैक।",
    "ta": "உணவுத் தர அலுமினியக் கொள்கலன், 8011 சுருளால் செய்யப்பட்டது, உறைவிப்பான், அடுப்பு மற்றும் "
    "எடுத்துச் செல்லும் பயன்பாட்டிற்கு ஏற்றது. காகித அல்லது பிளாஸ்டிக் மூடிகளுடன் கிடைக்கும்.",
    "zh": "食品级铝箔容器，采用8011铝卷制造，适用于冷冻、烤箱和外卖。可配纸盖或塑料盖，可按要求定制印刷，出口纸箱包装。",
    "si": "ආහාර ශ්‍රේණියේ ඇලුමිනියම් බහාලුම, 8011 දඟරයෙන් සාදන ලද, ශීතකරණ, උදුන සහ රැගෙන යාමේ "
    "භාවිතයට සුදුසුය. කඩදාසි හෝ ප්ලාස්ටික් පියන සමඟ ලබා ගත හැක.",
}


@app.get("/api/{collection}/{doc_id}")
async def cms_doc(collection: str, doc_id: str, locale: str = "en") -> Response:
    base = LANG_TEXT.get(locale, LANG_TEXT["en"])
    rng = random.Random(f"{doc_id}-{locale}")
    paragraphs = [f"{doc_id} · {i + 1}. {base}" for i in range(rng.randint(14, 22))]
    doc = {
        "id": doc_id,
        "_status": "published",
        "title": f"Aluminium container {doc_id}",
        "plainText": "\n\n".join(paragraphs),
        "slug": f"products/{doc_id}",
        "updatedAt": "2026-11-14T00:00:00Z",
    }
    return Response(json.dumps(doc, ensure_ascii=False), media_type="application/json")


# ------------------------------------------------------------------ control
@app.get("/_stats")
async def stats() -> Response:
    store = DATA / "s3"
    files = [p for p in store.rglob("*") if p.is_file()] if store.exists() else []
    out = {
        "vendors": STATS,
        "llm": LLM,
        "s3_objects": len(files),
        "s3_bytes": sum(p.stat().st_size for p in files),
        "s3_by_prefix": _by_prefix(files, store),
    }
    return Response(json.dumps(out, default=float), media_type="application/json")


def _by_prefix(files: list[Path], store: Path) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"objects": 0, "bytes": 0})
    for p in files:
        parts = p.relative_to(store).parts
        key = "/".join(parts[:3]) if len(parts) > 3 else "/".join(parts[:-1])
        out[key]["objects"] += 1
        out[key]["bytes"] += p.stat().st_size
    return out


@app.post("/_reset")
async def reset() -> Response:
    STATS.clear()
    LLM.clear()
    return Response("{}", media_type="application/json")


# ------------------------------------------------------------------ WhatsApp Graph + S3 (catch-all, last)
@app.api_route("/{rest:path}", methods=["GET", "PUT", "POST", "HEAD", "DELETE"])
async def catch_all(rest: str, request: Request) -> Response:
    path = "/" + rest
    if re.match(r"^/v\d+\.\d+/", path):
        if request.method == "POST":
            wamid = f"wamid.{uuid.uuid4().hex}"
            return Response(
                json.dumps({"messaging_product": "whatsapp", "id": wamid, "messages": [{"id": wamid}]}),
                media_type="application/json",
            )
        return Response(json.dumps({"url": "http://fakes:9000/media/x", "mime_type": "image/jpeg"}),
                        media_type="application/json")
    target = DATA / "s3" / rest
    if request.method == "PUT":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(await request.body())
        return Response(status_code=200, headers={"ETag": f'"{uuid.uuid4().hex}"'})
    if request.method in ("GET", "HEAD"):
        if target.is_file():
            return Response(target.read_bytes() if request.method == "GET" else b"", media_type="application/octet-stream")
        return Response(status_code=404)
    if request.method == "DELETE":
        target.unlink(missing_ok=True)
        return Response(status_code=204)
    return Response("{}", media_type="application/json")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="warning")  # noqa: S104 - container port
