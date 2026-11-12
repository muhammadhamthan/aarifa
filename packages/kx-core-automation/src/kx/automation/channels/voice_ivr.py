"""Scripted manager alert calls over Twilio (TwiML Say + Gather). No AI on these calls.

Business rule: a call is only an ALERT. Approving needs MFA in the CRM, so the keypad can only
acknowledge ("1": stop calling me) or hand the approval to the MD ("2"). Nothing a caller presses
can approve, change or send a quote.

Calls are placed with one REST request over the shared httpx client instead of the Twilio SDK's
aiohttp client: one connection pool per process, the same retry policy as every other channel,
and respx-testable. The SDK is still used for TwiML generation and signature validation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from kx.automation.channels import ChannelError, HttpRetry
from kx.automation.models import CallStatus
from kx.automation.settings import TwilioSettings

PROVIDER: Final = "twilio"
SIGNATURE_HEADER: Final = "X-Twilio-Signature"


class Digit(StrEnum):
    ACKNOWLEDGE = "1"
    ESCALATE = "2"  # §16: send it to the manager now


# Twilio CallStatus -> call_attempts.status
CALL_STATUS: Final[dict[str, CallStatus]] = {
    "queued": "queued",
    "initiated": "queued",
    "ringing": "ringing",
    "in-progress": "answered",
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "failed",
}
TERMINAL_STATUSES: Final[frozenset[CallStatus]] = frozenset({"completed", "busy", "no_answer", "failed"})


@dataclass(frozen=True, slots=True)
class AlertScript:
    """What the call says. `prompt` is spoken inside the Gather so a key press interrupts it."""

    intro: str
    prompt: str


def approval_alert_script(title: str) -> AlertScript:
    """§16 60-minute alert call (scripted, not AI): "A quotation is waiting for your approval in the CRM".
    It only reminds: approval always happens in the CRM with two-step login."""
    return AlertScript(
        intro="This is the Arifaa Pack sales system.",
        prompt=(
            f"{title} is waiting for your approval in the CRM. "
            f"Press {Digit.ACKNOWLEDGE.value} to acknowledge. "
            f"Press {Digit.ESCALATE.value} to send it to the manager."
        ),
    )


def change_request_script(title: str) -> AlertScript:
    """§13.5 / §16: the same reminder call for a buyer change request."""
    return AlertScript(
        intro="This is the Arifaa Pack sales system.",
        prompt=(
            f"{title} is waiting for you in the CRM. The buyer was promised an updated quotation. "
            f"Press {Digit.ACKNOWLEDGE.value} to acknowledge. "
            f"Press {Digit.ESCALATE.value} to send it to the manager."
        ),
    )


def escalation_script(title: str, approver_name: str) -> AlertScript:
    """§16 120-minute escalation to the manager, who may approve or reassign."""
    return AlertScript(
        intro="This is the Arifaa Pack sales system.",
        prompt=(
            f"{title} is still waiting. {approver_name} did not respond. "
            "Please review it in the CRM: you can approve it or reassign the deal. "
            f"Press {Digit.ACKNOWLEDGE.value} to acknowledge."
        ),
    )


def negotiation_paused_script(respond_by_local: str) -> AlertScript:
    return AlertScript(
        intro="This is the Arifaa Pack sales system.",
        prompt=(
            "A buyer asked for a price below the approved range. "
            f"The assistant has paused and promised a reply by {respond_by_local}, Dubai time. "
            f"Please open the paused deal in the CRM. Press {Digit.ACKNOWLEDGE.value} to acknowledge."
        ),
    )


def alert_twiml(script: AlertScript, action_url: str, settings: TwilioSettings) -> str:
    response = VoiceResponse()
    response.say(script.intro, voice=settings.voice, language=settings.language)
    gather = response.gather(num_digits=1, action=action_url, method="POST", timeout=8)
    gather.say(script.prompt, voice=settings.voice, language=settings.language, loop=2)
    response.say("No choice received. Goodbye.", voice=settings.voice, language=settings.language)
    return str(response)


def gather_reply_twiml(digit: str | None, settings: TwilioSettings) -> str:
    text = {
        Digit.ACKNOWLEDGE.value: "Thank you. Acknowledged. Please approve in the CRM.",
        Digit.ESCALATE.value: "Thank you. The manager is being alerted now.",
    }.get(digit or "", "That is not a valid option. Please check the CRM. Goodbye.")
    response = VoiceResponse()
    response.say(text, voice=settings.voice, language=settings.language)
    response.hangup()
    return str(response)


def bridge_twiml(intro: str, buyer_e164: str, settings: TwilioSettings) -> str:
    """§14 click-to-call, leg 2: the salesperson answered; dial the buyer from the company number."""
    response = VoiceResponse()
    response.say(intro, voice=settings.voice, language=settings.language)
    dial = response.dial(
        caller_id=settings.from_number, timeout=settings.ring_timeout_seconds, answer_on_bridge=True
    )
    dial.number(buyer_e164)
    return str(response)


def validate_signature(auth_token: str, url: str, params: Mapping[str, str], signature: str | None) -> bool:
    """X-Twilio-Signature: HMAC-SHA1 over the public URL + sorted POST params (SDK implementation)."""
    if not auth_token or not signature:
        return False
    return bool(RequestValidator(auth_token).validate(url, dict(params), signature))


class TwilioCaller:
    def __init__(
        self, http: httpx.AsyncClient, settings: TwilioSettings, retry: HttpRetry | None = None
    ) -> None:
        self._http = http
        self._settings = settings
        self._retry = retry or HttpRetry()

    async def place_call(self, to_e164: str, twiml: str, status_callback_url: str) -> str:
        """Start an outbound call with inline TwiML; returns the CallSid."""
        s = self._settings
        url = f"{s.api_base_url.rstrip('/')}/2010-04-01/Accounts/{s.account_sid}/Calls.json"
        data: dict[str, str | list[str]] = {
            "To": to_e164,
            "From": s.from_number,
            "Twiml": twiml,
            "Timeout": str(s.ring_timeout_seconds),
            "StatusCallback": status_callback_url,
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
        }
        response = await self._retry.request(
            self._http,
            PROVIDER,
            "POST",
            url,
            data=data,
            auth=(s.account_sid, s.auth_token.get_secret_value()),
        )
        sid = response.json().get("sid")
        if not sid:
            raise ChannelError(PROVIDER, "response without call sid", retryable=False)
        return str(sid)
