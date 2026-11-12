"""D33 Postmark: the email field's backup sender and the buyer-reply inbox (fields per Postmark's docs)."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from kx.automation.channels import ChannelError
from kx.automation.channels.email import Attachment
from kx.automation.channels.email_chain import EmailChain
from kx.automation.channels.postmark import PostmarkSender, parse_inbound
from kx.automation.settings import PostmarkSettings
from kx.core.fallback import FallbackChain

SETTINGS = PostmarkSettings(server_token=SecretStr("pm-token"))


def sender(handler: Any) -> PostmarkSender:
    return PostmarkSender(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), SETTINGS, "sales@arifapack.com"
    )


async def test_send_posts_the_documented_fields() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["token"] = str(request.url), request.headers["X-Postmark-Server-Token"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ErrorCode": 0, "Message": "OK", "MessageID": "pm-1"})

    mid = await sender(handler).send(
        to_address="buyer@example.com",
        subject="Quote Q-1",
        body="Please find attached.",
        attachment=Attachment("q.pdf", "application/pdf", b"%PDF"),
        list_unsubscribe="https://api.example/u?t=x",
        from_name="Sara, Arifa Pack",
    )
    body = seen["body"]
    assert (
        mid == "pm-1" and seen["url"] == "https://api.postmarkapp.com/email" and seen["token"] == "pm-token"
    )
    assert (
        body["From"] == '"Sara, Arifa Pack" <sales@arifapack.com>'
        and body["TextBody"] == "Please find attached."
    )
    assert body["Attachments"][0] == {
        "Name": "q.pdf",
        "Content": base64.b64encode(b"%PDF").decode(),
        "ContentType": "application/pdf",
    }
    assert {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"} in body["Headers"]


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (422, False), (401, False)])
async def test_refusals_say_whether_to_retry(status: int, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"ErrorCode": 300, "Message": "nope"})

    with pytest.raises(ChannelError) as err:
        await sender(handler).send(to_address="b@example.com", subject="s", body="b")
    assert err.value.retryable is retryable


async def test_chain_falls_back_from_ses_to_postmark() -> None:
    class DownSes:
        async def send(self, **kw: Any) -> str:
            raise ChannelError("ses", "Throttling", retryable=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ErrorCode": 0, "MessageID": "pm-9"})

    async def order() -> tuple[str, ...]:
        return ("ses", "postmark")

    chain = EmailChain({"ses": DownSes(), "postmark": sender(handler)}, FallbackChain("email", "pack"), order)
    assert await chain.send(to_address="b@example.com", subject="s", body="b") == "pm-9"


def test_inbound_payload_becomes_the_reply_only() -> None:
    mail = parse_inbound(
        {
            "FromFull": {"Email": "Buyer@Example.com", "Name": "Omar"},
            "ToFull": [{"Email": "sales@arifapack.com"}],
            "Subject": "Re: Quote Q-1",
            "MessageID": "abc-123",
            "TextBody": "OK go ahead\n\n> old quoted text",
            "StrippedTextReply": "OK go ahead",
            "Headers": [{"Name": "In-Reply-To", "Value": "<q1@arifapack.com>"}],
            "Attachments": [
                {
                    "Name": "po.pdf",
                    "Content": base64.b64encode(b"PO").decode(),
                    "ContentType": "application/pdf",
                }
            ],
        }
    )
    assert mail.from_address == "buyer@example.com" and mail.text == "OK go ahead"
    assert mail.in_reply_to == "<q1@arifapack.com>" and mail.attachments[0].data == b"PO"
