"""Twilio IVR: TwiML content, signature validation, REST call placement over respx."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import respx
from tenacity import wait_none
from twilio.request_validator import RequestValidator

from kx.automation.channels import HttpRetry
from kx.automation.channels.voice_ivr import (
    TwilioCaller,
    alert_twiml,
    approval_alert_script,
    gather_reply_twiml,
    validate_signature,
)
from kx.automation.settings import TwilioSettings

SETTINGS = TwilioSettings(account_sid="AC123", auth_token="tw-token", from_number="+97140000000")  # type: ignore[arg-type]


def test_alert_twiml_is_an_alert_not_an_approval() -> None:
    xml = alert_twiml(
        approval_alert_script("Quote Q-1045 for AED 11,390.20"),
        "https://api.x/webhooks/twilio/voice/1",
        SETTINGS,
    )
    assert '<Gather action="https://api.x/webhooks/twilio/voice/1" method="POST" numDigits="1"' in xml
    assert "Quote Q-1045 for AED 11,390.20 is waiting for your approval" in xml
    assert "Press 1 to acknowledge" in xml and "Press 2 to send it to the manager" in xml
    assert "approve" not in gather_reply_twiml("9", SETTINGS).lower().split("check")[0]
    assert "<Hangup />" in gather_reply_twiml("1", SETTINGS)


def test_signature_validation() -> None:
    url = "https://api.arifapack.com/webhooks/twilio/status?attempt_id=abc"
    params = {"CallSid": "CA1", "CallStatus": "completed"}
    sig = RequestValidator("tw-token").compute_signature(url, params)
    assert validate_signature("tw-token", url, params, sig)
    assert not validate_signature("tw-token", url, params | {"CallStatus": "busy"}, sig)
    assert not validate_signature("tw-token", url, params, None)
    assert not validate_signature("", url, params, sig)


@respx.mock
async def test_place_call_posts_inline_twiml_with_status_callback() -> None:
    route = respx.post("https://api.twilio.com/2010-04-01/Accounts/AC123/Calls.json").mock(
        return_value=httpx.Response(201, json={"sid": "CA999", "status": "queued"})
    )
    async with httpx.AsyncClient() as http:
        sid = await TwilioCaller(http, SETTINGS, HttpRetry(attempts=2, wait=wait_none())).place_call(
            "+971501234567", "<Response/>", "https://api.x/webhooks/twilio/status?attempt_id=1"
        )
    assert sid == "CA999"
    form = parse_qs(route.calls[0].request.content.decode())
    assert form["To"] == ["+971501234567"] and form["Twiml"] == ["<Response/>"]
    assert form["StatusCallbackEvent"] == ["initiated", "ringing", "answered", "completed"]
    assert route.calls[0].request.headers["Authorization"].startswith("Basic ")
