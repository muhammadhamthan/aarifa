"""Postmark: the email field's backup sender and the buyer-reply inbox (D33, Fallback plan "Email").

Send: `POST https://api.postmarkapp.com/email` with `X-Postmark-Server-Token`; fields From, To, Subject,
TextBody, ReplyTo, Headers [{Name, Value}], Attachments [{Name, Content (base64), ContentType}],
MessageStream; the answer carries MessageID and ErrorCode (0 = accepted). 429, 5xx and network errors are
retried by the job store; other refusals are permanent.

Inbound: Postmark posts the parsed email as JSON (From, FromFull, To, Subject, MessageID, TextBody,
StrippedTextReply, Headers, Attachments [{Name, Content, ContentType, ContentLength}]). Postmark does not
sign webhooks; it recommends HTTP Basic auth in the webhook URL plus HTTPS (and its IP ranges at the WAF).
Source: postmarkapp.com/developer/api/email-api, /webhooks/inbound-webhook, /webhooks/webhooks-overview.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from email.utils import formataddr
from typing import Any, Final

import httpx

from kx.automation.channels import ChannelError
from kx.automation.channels.email import Attachment, InboundEmail
from kx.automation.settings import PostmarkSettings
from kx.core.identity import normalise_email

PROVIDER: Final = "postmark"
SEND_URL: Final = "https://api.postmarkapp.com/email"


class PostmarkSender:
    """Same call shape as `EmailSender`, so `OutboundService` and the staff-code mailer can use either."""

    def __init__(self, http: httpx.AsyncClient, settings: PostmarkSettings, from_address: str) -> None:
        self._http = http
        self._settings = settings
        self._from = from_address

    async def send(
        self,
        *,
        to_address: str,
        subject: str,
        body: str,
        attachment: Attachment | None = None,
        in_reply_to: str | None = None,
        list_unsubscribe: str | None = None,
        from_name: str | None = None,
    ) -> str:
        headers: list[dict[str, str]] = []
        if in_reply_to:
            headers += [
                {"Name": "In-Reply-To", "Value": in_reply_to},
                {"Name": "References", "Value": in_reply_to},
            ]
        if list_unsubscribe:  # §8.1 step 10 / RFC 8058, the same headers SES sends
            headers += [
                {"Name": "List-Unsubscribe", "Value": f"<{list_unsubscribe}>"},
                {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"},
            ]
        payload: dict[str, Any] = {
            "From": formataddr((from_name, self._from)) if from_name else self._from,
            "To": to_address,
            "Subject": subject,
            "TextBody": body,
            "MessageStream": self._settings.message_stream,
            "Headers": headers,
        }
        if attachment is not None:
            payload["Attachments"] = [
                {
                    "Name": attachment.filename,
                    "Content": base64.b64encode(attachment.data).decode(),
                    "ContentType": attachment.content_type,
                }
            ]
        token = self._settings.server_token.get_secret_value()
        try:
            r = await self._http.post(
                SEND_URL,
                json=payload,
                headers={"X-Postmark-Server-Token": token, "Accept": "application/json"},
                timeout=self._settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise ChannelError(PROVIDER, type(exc).__name__, retryable=True) from exc
        data: dict[str, Any] = r.json() if r.content else {}
        if r.status_code != httpx.codes.OK or data.get("ErrorCode", 0) != 0:
            retryable = r.status_code == httpx.codes.TOO_MANY_REQUESTS or r.status_code >= 500
            detail = f"HTTP {r.status_code} ErrorCode {data.get('ErrorCode')}: {data.get('Message', '')}"
            raise ChannelError(PROVIDER, detail[:300], retryable=retryable, status=r.status_code)
        return str(data["MessageID"])


def _decode(content: str) -> bytes | None:
    try:
        return base64.b64decode(content, validate=True)
    except binascii.Error, ValueError:
        return None


def parse_inbound(payload: dict[str, Any], *, now: datetime | None = None) -> InboundEmail:
    """Postmark's inbound JSON -> the same `InboundEmail` the SES path produces (the reply text only)."""
    full = payload.get("FromFull") or {}
    headers = {str(h.get("Name", "")).lower(): str(h.get("Value", "")) for h in payload.get("Headers") or []}
    to = [str(t.get("Email", "")) for t in payload.get("ToFull") or [] if t.get("Email")]
    attachments = tuple(
        Attachment(
            str(a.get("Name") or "attachment"), str(a.get("ContentType") or "application/octet-stream"), data
        )
        for a in payload.get("Attachments") or []
        if (data := _decode(str(a.get("Content") or ""))) is not None
    )
    text = str(payload.get("StrippedTextReply") or payload.get("TextBody") or "").strip()
    return InboundEmail(
        message_id=str(payload.get("MessageID") or ""),
        from_address=normalise_email(str(full.get("Email") or payload.get("From") or "")),
        from_name=str(full.get("Name") or payload.get("FromName") or "") or None,
        to_addresses=tuple(to) or (str(payload.get("To") or ""),),
        subject=str(payload.get("Subject") or ""),
        text=text,
        in_reply_to=headers.get("in-reply-to"),
        received_at=now or datetime.now(UTC),  # Postmark's Date follows the sender's server format
        attachments=attachments,
    )
