"""Meta WhatsApp: signature, challenge, webhook parsing with realistic payloads, client over respx."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from tenacity import wait_none

from kx.automation.channels import ChannelError, HttpRetry
from kx.automation.channels.whatsapp import (
    DocumentRef,
    WhatsAppClient,
    parse_webhook,
    verify_challenge,
    verify_signature,
)
from kx.automation.settings import WhatsAppSettings
from kx.core.identity import hmac_sha256_hex

SECRET = "app-secret"
GRAPH = "https://graph.facebook.com/v23.0"
SETTINGS = WhatsAppSettings(phone_number_id="PNID1", access_token="tok", app_secret=SECRET, verify_token="vt")  # type: ignore[arg-type]
FAST = HttpRetry(attempts=3, wait=wait_none())


def meta_payload(
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "971500000001", "phone_number_id": "PNID1"},
    }
    if contacts is not None:
        value["contacts"] = contacts
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA1", "changes": [{"value": value, "field": "messages"}]}],
    }


def text_message(
    wamid: str = "wamid.HBgM1", body: str = "Need 50,000 pouches", sender: str = "971501234567"
) -> dict[str, Any]:
    return {"from": sender, "id": wamid, "timestamp": "1793600000", "type": "text", "text": {"body": body}}


# ---------------------------------------------------------------- signature + challenge
def test_signature_accepts_exact_body_only() -> None:
    body = json.dumps(meta_payload([text_message()])).encode()
    good = "sha256=" + hmac_sha256_hex(SECRET.encode(), body)
    assert verify_signature(SECRET, body, good)
    assert not verify_signature(SECRET, body + b" ", good)
    assert not verify_signature("other", body, good)
    assert not verify_signature(SECRET, body, good.removeprefix("sha256="))
    assert not verify_signature(SECRET, body, None)


def test_empty_secret_never_verifies() -> None:
    body = b"{}"
    assert not verify_signature("", body, "sha256=" + hmac_sha256_hex(b"", body))


def test_challenge() -> None:
    assert verify_challenge("vt", "subscribe", "vt", "123") == "123"
    assert verify_challenge("vt", "subscribe", "nope", "123") is None
    assert verify_challenge("", "subscribe", "", "123") is None


# ---------------------------------------------------------------- parsing
def test_parse_text_with_profile_and_e164() -> None:
    hook = parse_webhook(
        meta_payload([text_message()], contacts=[{"profile": {"name": "Ahmed"}, "wa_id": "971501234567"}])
    )
    (m,) = hook.messages
    assert (m.kind, m.text, m.from_e164, m.profile_name, m.phone_number_id) == (
        "text",
        "Need 50,000 pouches",
        "+971501234567",
        "Ahmed",
        "PNID1",
    )
    assert m.media is None and m.received_at.year == 2026 and m.raw["from"] == "971501234567"


@pytest.mark.parametrize(
    ("msg", "kind", "text", "media"),
    [
        (
            {"type": "button", "button": {"text": "Yes, send quote", "payload": "YES"}},
            "button",
            "Yes, send quote",
            False,
        ),
        (
            {
                "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": "b1", "title": "Call me"}},
            },
            "interactive",
            "Call me",
            False,
        ),
        (
            {
                "type": "interactive",
                "interactive": {"type": "list_reply", "list_reply": {"id": "l1", "title": "PET jars"}},
            },
            "interactive",
            "PET jars",
            False,
        ),
        (
            {
                "type": "document",
                "document": {
                    "id": "M1",
                    "mime_type": "application/pdf",
                    "filename": "artwork.pdf",
                    "sha256": "abc",
                    "caption": "our artwork",
                },
            },
            "document",
            "our artwork",
            True,
        ),
        (
            {"type": "image", "image": {"id": "M2", "mime_type": "image/jpeg", "sha256": "def"}},
            "image",
            "",
            True,
        ),
        (
            {"type": "audio", "audio": {"id": "M3", "mime_type": "audio/ogg; codecs=opus", "voice": True}},
            "audio",
            "",
            True,
        ),
        ({"type": "video", "video": {"id": "M4", "mime_type": "video/mp4"}}, "other", "", True),
        (
            {"type": "reaction", "reaction": {"message_id": "wamid.X", "emoji": "\U0001f44d"}},
            "reaction",
            "\U0001f44d",
            False,
        ),
        ({"type": "unsupported", "errors": [{"code": 131051}]}, "other", "", False),
    ],
)
def test_parse_message_kinds(msg: dict[str, Any], kind: str, text: str, media: bool) -> None:
    base = {"from": "971501234567", "id": "wamid.K", "timestamp": "1793600000"}
    (m,) = parse_webhook(meta_payload([base | msg])).messages
    assert (m.kind, m.text, m.media is not None) == (kind, text, media)


def test_parse_statuses_with_error() -> None:
    statuses = [
        {
            "id": "wamid.OUT1",
            "status": "delivered",
            "timestamp": "1793600100",
            "recipient_id": "971501234567",
            "conversation": {"id": "c1", "origin": {"type": "utility"}},
            "pricing": {"billable": True},
        },
        {
            "id": "wamid.OUT2",
            "status": "failed",
            "timestamp": "1793600200",
            "recipient_id": "971501234567",
            "errors": [{"code": 131047, "title": "Re-engagement message"}],
        },
    ]
    hook = parse_webhook(meta_payload(statuses=statuses))
    assert [s.status for s in hook.statuses] == ["delivered", "failed"]
    assert hook.statuses[1].error == "131047 Re-engagement message"
    assert hook.messages == ()


def test_parse_ignores_other_objects_and_fields() -> None:
    assert parse_webhook({"object": "page", "entry": []}).messages == ()
    payload = meta_payload([text_message()])
    payload["entry"][0]["changes"][0]["field"] = "account_update"
    assert parse_webhook(payload).messages == ()


# ---------------------------------------------------------------- client
@respx.mock
async def test_send_text_template_document_and_read() -> None:
    route = respx.post(f"{GRAPH}/PNID1/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})
    )
    async with httpx.AsyncClient() as http:
        client = WhatsAppClient(http, SETTINGS, FAST)
        assert await client.send_text("+971501234567", "hi") == "wamid.OUT"
        await client.send_template("+971501234567", "quote_ready", "en", ("Q-1045", "AED 11,390.20"))
        await client.send_document(
            "+971501234567", DocumentRef("Q-1045.pdf", media_id="M9"), caption="Your quote"
        )
        await client.mark_read("wamid.IN")
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert bodies[0] | {} == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "971501234567",
        "type": "text",
        "text": {"body": "hi", "preview_url": False},
    }
    assert bodies[1]["template"]["components"][0]["parameters"][1] == {
        "type": "text",
        "text": "AED 11,390.20",
    }
    assert bodies[2]["document"] == {"filename": "Q-1045.pdf", "id": "M9", "caption": "Your quote"}
    assert bodies[3] == {"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.IN"}
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"


@respx.mock
async def test_retries_on_429_and_5xx_then_succeeds() -> None:
    route = respx.post(f"{GRAPH}/PNID1/messages").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"code": 130429, "message": "rate"}}),
            httpx.ConnectTimeout("slow"),
            httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]}),
        ]
    )
    async with httpx.AsyncClient() as http:
        assert await WhatsAppClient(http, SETTINGS, FAST).send_text("+971501234567", "hi") == "wamid.OK"
    assert route.call_count == 3


@respx.mock
async def test_no_retry_on_4xx() -> None:
    route = respx.post(f"{GRAPH}/PNID1/messages").mock(
        return_value=httpx.Response(400, json={"error": {"code": 131026, "message": "undeliverable"}})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(ChannelError) as info:
            await WhatsAppClient(http, SETTINGS, FAST).send_text("+971501234567", "hi")
    assert route.call_count == 1 and not info.value.retryable and "131026" in info.value.detail


@respx.mock
async def test_exhausted_5xx_is_retryable_error() -> None:
    respx.post(f"{GRAPH}/PNID1/messages").mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as http:
        with pytest.raises(ChannelError) as info:
            await WhatsAppClient(http, SETTINGS, FAST).send_text("+971501234567", "hi")
    assert info.value.retryable and info.value.status == 503


@respx.mock
async def test_media_upload_and_download() -> None:
    respx.post(f"{GRAPH}/PNID1/media").mock(return_value=httpx.Response(200, json={"id": "MEDIA1"}))
    respx.get(f"{GRAPH}/M3").mock(
        return_value=httpx.Response(
            200, json={"url": "https://lookaside.fbsbx.com/m/M3", "mime_type": "audio/ogg"}
        )
    )
    respx.get("https://lookaside.fbsbx.com/m/M3").mock(return_value=httpx.Response(200, content=b"OGG"))
    async with httpx.AsyncClient() as http:
        client = WhatsAppClient(http, SETTINGS, FAST)
        assert await client.upload_media(b"%PDF", "application/pdf", "q.pdf") == "MEDIA1"
        assert await client.download_media("M3") == (b"OGG", "audio/ogg")


def test_document_ref_needs_exactly_one_source() -> None:
    with pytest.raises(ValueError):
        DocumentRef("a.pdf")
    with pytest.raises(ValueError):
        DocumentRef("a.pdf", media_id="1", link="https://x")
