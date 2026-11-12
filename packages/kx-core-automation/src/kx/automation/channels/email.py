"""Email over Amazon SES v2: MIME building + raw send, inbound MIME parsing, SES event parsing.

Inbound mail is written to S3 by SES Mail Manager (UAE region); an S3 -> SNS notification tells us
the object key, and a job parses the stored MIME with `parse_inbound`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import (
    format_datetime,
    formataddr,
    getaddresses,
    make_msgid,
    parseaddr,
    parsedate_to_datetime,
)
from typing import Any, Final, Literal, Protocol
from urllib.parse import unquote_plus

from selectolax.lexbor import LexborHTMLParser

from kx.automation.channels import ChannelError
from kx.automation.settings import SesSettings
from kx.core.identity import normalise_email

PROVIDER: Final = "ses"
_RETRYABLE_AWS: Final = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "InternalFailure",
        "RequestTimeout",
    }
)


class SesV2Client(Protocol):
    """The slice of an aioboto3 `sesv2` client we use (create it once in the app lifespan)."""

    async def send_email(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: str
    data: bytes


# ---------------------------------------------------------------- outbound
def build_mime(
    *,
    from_address: str,
    to_address: str,
    subject: str,
    body: str,
    attachment: Attachment | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    list_unsubscribe: str | None = None,
    from_name: str | None = None,
) -> EmailMessage:
    msg = EmailMessage(policy=policy.SMTP)
    # §14: the salesperson's name as sender, always on the company address (replies land in the deal)
    msg["From"] = formataddr((from_name, from_address)) if from_name else from_address
    msg["To"] = to_address
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = make_msgid(domain=from_address.rpartition("@")[2] or None)
    if reply_to:
        msg["Reply-To"] = reply_to
    if in_reply_to:  # keeps the buyer's mail client threading our reply under theirs
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if list_unsubscribe:  # §8.1 step 10 / RFC 8058 one-click: outside the body, so grant hashes are untouched
        msg["List-Unsubscribe"] = f"<{list_unsubscribe}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(body)
    if attachment is not None:
        maintype, _, subtype = attachment.content_type.partition("/")
        msg.add_attachment(
            attachment.data,
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return msg


class EmailSender:
    def __init__(self, ses: SesV2Client, settings: SesSettings) -> None:
        self._ses = ses
        self._settings = settings

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
        """Send a raw MIME message; returns the SES MessageId (later events reference it)."""
        msg = build_mime(
            from_address=self._settings.from_address,
            to_address=to_address,
            subject=subject,
            body=body,
            attachment=attachment,
            in_reply_to=in_reply_to,
            list_unsubscribe=list_unsubscribe,
            from_name=from_name,
        )
        request: dict[str, Any] = {
            "FromEmailAddress": self._settings.from_address,
            "Destination": {"ToAddresses": [to_address]},
            "Content": {"Raw": {"Data": msg.as_bytes()}},
        }
        if self._settings.configuration_set:
            request["ConfigurationSetName"] = self._settings.configuration_set
        try:
            response = await self._ses.send_email(**request)
        except Exception as exc:
            raise _channel_error(exc) from exc
        return str(response["MessageId"])


def _channel_error(exc: Exception) -> ChannelError:
    """botocore ClientError carries `response['Error']['Code']`; anything else is a transport failure."""
    error = getattr(exc, "response", {}).get("Error", {})
    code = str(error.get("Code", type(exc).__name__))
    retryable = code in _RETRYABLE_AWS or not error
    return ChannelError(PROVIDER, code, retryable=retryable)


# ---------------------------------------------------------------- inbound
@dataclass(frozen=True, slots=True)
class InboundEmail:
    message_id: str
    from_address: str
    from_name: str | None
    to_addresses: tuple[str, ...]
    subject: str
    text: str  # reply only: quoted history stripped
    in_reply_to: str | None
    received_at: datetime
    attachments: tuple[Attachment, ...]


def parse_inbound(raw: bytes, *, fallback_id: str) -> InboundEmail:
    msg = message_from_bytes(raw, policy=policy.default)
    name, address = parseaddr(str(msg.get("From", "")))
    received = msg.get("Date")
    return InboundEmail(
        message_id=str(msg.get("Message-ID") or fallback_id).strip(),
        from_address=normalise_email(address),
        from_name=name or None,
        to_addresses=tuple(_addresses(msg, "To", "Cc")),
        subject=str(msg.get("Subject", "")),
        text=strip_quoted(_body_text(msg)),
        in_reply_to=str(v).strip() if (v := msg.get("In-Reply-To")) else None,
        received_at=parsedate_to_datetime(str(received)).astimezone(UTC) if received else datetime.now(UTC),
        attachments=tuple(_attachments(msg)),
    )


def _addresses(msg: EmailMessage, *headers: str) -> Iterator[str]:
    for _, addr in getaddresses([str(v) for h in headers for v in msg.get_all(h, [])]):
        try:
            yield normalise_email(addr)
        except ValueError:
            continue


def _body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = str(part.get_content())
    return html_to_text(content) if part.get_content_subtype() == "html" else content


def _attachments(msg: EmailMessage) -> Iterator[Attachment]:
    for part in msg.iter_attachments():
        if not isinstance(part, EmailMessage):
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            yield Attachment(part.get_filename() or "attachment", part.get_content_type(), payload)


def html_to_text(html: str) -> str:
    tree = LexborHTMLParser(html)
    tree.strip_tags(["script", "style", "head"])
    for quote in tree.css("blockquote, div.gmail_quote, div#appendonsend, div.OutlookMessageHeader"):
        quote.decompose()
    root = tree.body or tree.root
    return root.text(separator="\n", strip=True) if root is not None else ""


# Lines that start the quoted history in common clients (Gmail, Outlook, Apple Mail; en + ar).
_QUOTE_HEADER: Final = re.compile(
    r"^(On .{4,200} wrote:|-{2,}\s*Original Message\s*-{2,}|From:\s.+|Sent from my \w+|"
    r"في .{4,200} كتب.*:|_{5,})\s*$",
    re.IGNORECASE,
)


def strip_quoted(text: str) -> str:
    """Keep only the new reply: stop at the first quote header; drop '>' quoted lines."""
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if _QUOTE_HEADER.match(stripped):
            break
        if not stripped.startswith(">"):
            kept.append(line.rstrip())
    return "\n".join(kept).strip()


# ---------------------------------------------------------------- SES events (via SNS)
SesEventKind = Literal["delivery", "bounce", "complaint", "send", "reject", "other"]
_SES_KINDS: Final[dict[str, SesEventKind]] = {
    "delivery": "delivery",
    "bounce": "bounce",
    "complaint": "complaint",
    "send": "send",
    "reject": "reject",
}


@dataclass(frozen=True, slots=True)
class SesEvent:
    kind: SesEventKind
    provider_message_id: str
    recipients: tuple[str, ...]
    permanent: bool  # permanent bounce -> suppress the address
    detail: str | None
    at: datetime


def parse_ses_event(message: dict[str, Any]) -> SesEvent | None:
    """Configuration-set event publishing uses `eventType`; identity notifications `notificationType`."""
    raw_kind = str(message.get("eventType") or message.get("notificationType") or "").lower()
    mail = message.get("mail")
    if not raw_kind or not isinstance(mail, dict):
        return None
    kind = _SES_KINDS.get(raw_kind, "other")
    section: dict[str, Any] = message.get(raw_kind) or {}
    recipients, permanent, detail = _event_details(kind, section, mail)
    stamp = section.get("timestamp") or mail.get("timestamp")
    return SesEvent(
        kind=kind,
        provider_message_id=str(mail["messageId"]),
        recipients=recipients,
        permanent=permanent,
        detail=detail,
        at=datetime.fromisoformat(stamp) if stamp else datetime.now(UTC),
    )


def _event_details(
    kind: SesEventKind, section: dict[str, Any], mail: dict[str, Any]
) -> tuple[tuple[str, ...], bool, str | None]:
    match kind:
        case "bounce":
            rows = section.get("bouncedRecipients", [])
            permanent = section.get("bounceType") == "Permanent"
            return _emails(r.get("emailAddress") for r in rows), permanent, section.get("bounceSubType")
        case "complaint":
            rows = section.get("complainedRecipients", [])
            return _emails(r.get("emailAddress") for r in rows), True, section.get("complaintFeedbackType")
        case "delivery":
            return _emails(section.get("recipients", [])), False, None
        case _:
            return _emails(mail.get("destination", [])), False, None


def _emails(values: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        try:
            out.append(normalise_email(parseaddr(str(value))[1]))
        except ValueError:
            continue
    return tuple(out)


def s3_objects(message: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """(bucket, key) pairs from an S3 event notification (keys arrive URL-encoded with '+')."""
    out: list[tuple[str, str]] = []
    for record in message.get("Records", []):
        s3 = record.get("s3") if isinstance(record, dict) else None
        if isinstance(s3, dict) and str(record.get("eventName", "")).startswith("ObjectCreated"):
            out.append((str(s3["bucket"]["name"]), unquote_plus(str(s3["object"]["key"]))))
    return tuple(out)
