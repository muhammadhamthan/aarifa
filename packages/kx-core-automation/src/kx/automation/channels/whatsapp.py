"""Meta WhatsApp Cloud API: sending, webhook signature check and webhook payload parsing."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from kx.automation.channels import ChannelError, HttpRetry
from kx.automation.settings import WhatsAppSettings
from kx.core.identity import constant_time_equals, hmac_sha256_hex, normalise_phone

PROVIDER: Final = "meta_cloud"
SIGNATURE_HEADER: Final = "X-Hub-Signature-256"


# ---------------------------------------------------------------- signature
def verify_signature(app_secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """Meta signs the exact raw body with HMAC-SHA256(app_secret) as `sha256=<hex>`.

    An empty secret is refused outright: HMAC with an empty key would accept anyone's payload.
    """
    if not app_secret or not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac_sha256_hex(app_secret.encode(), raw_body)
    return constant_time_equals(expected, header_value.removeprefix("sha256="))


def verify_challenge(
    verify_token: str, mode: str | None, token: str | None, challenge: str | None
) -> str | None:
    """GET subscription handshake: echo `hub.challenge` only for our verify token."""
    if not verify_token or mode != "subscribe" or token is None or challenge is None:
        return None
    return challenge if constant_time_equals(verify_token, token) else None


# ---------------------------------------------------------------- webhook payload models
class _M(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class _Text(_M):
    body: str


class _Media(_M):
    id: str
    mime_type: str | None = None
    sha256: str | None = None
    caption: str | None = None
    filename: str | None = None


class _Button(_M):
    text: str
    payload: str | None = None


class _Reply(_M):
    id: str
    title: str
    description: str | None = None


class _Interactive(_M):
    type: str
    button_reply: _Reply | None = None
    list_reply: _Reply | None = None


class _Reaction(_M):
    message_id: str
    emoji: str | None = None


class _Context(_M):
    id: str | None = None


class _Message(_M):
    from_: str = Field(alias="from")
    id: str
    timestamp: str
    type: str
    text: _Text | None = None
    image: _Media | None = None
    audio: _Media | None = None
    document: _Media | None = None
    video: _Media | None = None
    sticker: _Media | None = None
    button: _Button | None = None
    interactive: _Interactive | None = None
    reaction: _Reaction | None = None
    context: _Context | None = None


class _StatusError(_M):
    code: int
    title: str | None = None
    message: str | None = None


class _Status(_M):
    id: str
    status: Literal["sent", "delivered", "read", "failed", "deleted"]
    timestamp: str
    recipient_id: str
    errors: tuple[_StatusError, ...] = ()


class _Profile(_M):
    name: str | None = None


class _Contact(_M):
    wa_id: str
    profile: _Profile | None = None


class _Metadata(_M):
    phone_number_id: str
    display_phone_number: str | None = None


class _Value(_M):
    metadata: _Metadata
    contacts: tuple[_Contact, ...] = ()
    messages: tuple[_Message, ...] = ()
    statuses: tuple[_Status, ...] = ()


class _Change(_M):
    field: str
    value: _Value


class _Entry(_M):
    id: str
    changes: tuple[_Change, ...] = ()


class WebhookEnvelope(_M):
    object: str
    entry: tuple[_Entry, ...] = ()


# ---------------------------------------------------------------- flattened, typed results
InboundKind = Literal["text", "audio", "image", "document", "button", "interactive", "reaction", "other"]
# Media types and the inbound_messages.kind each is stored as (the DDL has no video/sticker kind).
_MEDIA_KIND: Final[dict[str, InboundKind]] = {
    "image": "image",
    "audio": "audio",
    "document": "document",
    "video": "other",
    "sticker": "other",
}


@dataclass(frozen=True, slots=True)
class WaMedia:
    media_id: str
    mime_type: str | None
    filename: str | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class WaInbound:
    phone_number_id: str
    message_id: str
    from_e164: str
    profile_name: str | None
    received_at: datetime
    kind: InboundKind
    text: str
    media: WaMedia | None
    reply_to: str | None
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WaStatus:
    phone_number_id: str
    message_id: str
    status: Literal["sent", "delivered", "read", "failed", "deleted"]
    at: datetime
    recipient_e164: str
    error: str | None


@dataclass(frozen=True, slots=True)
class WaWebhook:
    messages: tuple[WaInbound, ...]
    statuses: tuple[WaStatus, ...]


def parse_webhook(payload: dict[str, Any]) -> WaWebhook:
    """Flatten Meta's entry/changes/value nesting into inbound messages and delivery statuses."""
    envelope = WebhookEnvelope.model_validate(payload)
    messages: list[WaInbound] = []
    statuses: list[WaStatus] = []
    for value in _values(envelope):
        names = {c.wa_id: c.profile.name if c.profile else None for c in value.contacts}
        pnid = value.metadata.phone_number_id
        messages.extend(_inbound(pnid, m, names.get(m.from_)) for m in value.messages)
        statuses.extend(_status(pnid, s) for s in value.statuses)
    return WaWebhook(tuple(messages), tuple(statuses))


def _values(envelope: WebhookEnvelope) -> Iterator[_Value]:
    if envelope.object != "whatsapp_business_account":
        return
    for entry in envelope.entry:
        yield from (c.value for c in entry.changes if c.field == "messages")


def _inbound(phone_number_id: str, m: _Message, profile_name: str | None) -> WaInbound:
    kind, text, media = _content(m)
    return WaInbound(
        phone_number_id=phone_number_id,
        message_id=m.id,
        from_e164=wa_id_to_e164(m.from_),
        profile_name=profile_name,
        received_at=_epoch(m.timestamp),
        kind=kind,
        text=text,
        media=media,
        reply_to=m.context.id if m.context else None,
        raw=m.model_dump(mode="json", by_alias=True, exclude_none=True),
    )


def _content(m: _Message) -> tuple[InboundKind, str, WaMedia | None]:
    """Kind, the text the AI should read, and media to fetch. Captions count as text."""
    if m.text is not None:
        return "text", m.text.body, None
    if m.button is not None:
        return "button", m.button.text, None
    if m.interactive is not None:
        reply = m.interactive.button_reply or m.interactive.list_reply
        return "interactive", reply.title if reply else "", None
    if m.reaction is not None:
        return "reaction", m.reaction.emoji or "", None
    if (kind := _MEDIA_KIND.get(m.type)) is not None and isinstance(media := getattr(m, m.type), _Media):
        return kind, media.caption or "", WaMedia(media.id, media.mime_type, media.filename, media.sha256)
    return "other", "", None


def _status(phone_number_id: str, s: _Status) -> WaStatus:
    error = "; ".join(f"{e.code} {e.title or e.message or ''}".strip() for e in s.errors) or None
    return WaStatus(
        phone_number_id=phone_number_id,
        message_id=s.id,
        status=s.status,
        at=_epoch(s.timestamp),
        recipient_e164=wa_id_to_e164(s.recipient_id),
        error=error,
    )


def wa_id_to_e164(wa_id: str) -> str:
    """wa_id is the international number without '+'; normalise so it hashes like CRM contacts."""
    return normalise_phone(wa_id if wa_id.startswith("+") else f"+{wa_id}")


def e164_to_wa_id(e164: str) -> str:
    return e164.removeprefix("+")


def _epoch(value: str) -> datetime:
    return datetime.fromtimestamp(int(value), UTC)


# ---------------------------------------------------------------- client
@dataclass(frozen=True, slots=True)
class DocumentRef:
    """A document is sent either from an uploaded media id or a public (presigned) https link."""

    filename: str
    media_id: str | None = None
    link: str | None = None

    def __post_init__(self) -> None:
        if (self.media_id is None) == (self.link is None):
            raise ValueError("DocumentRef needs exactly one of media_id or link")


class WhatsAppClient:
    """Thin Cloud API client over the process-wide httpx.AsyncClient."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: WhatsAppSettings,
        retry: HttpRetry | None = None,
        phone_number_id: str | None = None,
    ) -> None:
        self._http = http
        self._settings = settings
        self._retry = retry or HttpRetry()
        self._pnid = phone_number_id or settings.phone_number_id
        self._base = f"{settings.graph_base_url.rstrip('/')}/{settings.graph_version}"

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.access_token.get_secret_value()}"}

    async def send_text(self, to_e164: str, body: str, *, preview_url: bool = False) -> str:
        return await self._send(to_e164, {"type": "text", "text": {"body": body, "preview_url": preview_url}})

    async def send_template(
        self,
        to_e164: str,
        name: str,
        language: str,
        params: tuple[str, ...] = (),
        document: DocumentRef | None = None,
    ) -> str:
        """An approved template; `document` fills a DOCUMENT header (the §8.2 Requirement Summary PDF)."""
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        components: list[dict[str, Any]] = []
        if document is not None:
            doc: dict[str, Any] = {"filename": document.filename}
            doc.update({"id": document.media_id} if document.media_id else {"link": document.link})
            components.append({"type": "header", "parameters": [{"type": "document", "document": doc}]})
        if params:
            components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in params]})
        if components:
            template["components"] = components
        return await self._send(to_e164, {"type": "template", "template": template})

    async def send_document(self, to_e164: str, document: DocumentRef, caption: str | None = None) -> str:
        doc: dict[str, Any] = {"filename": document.filename}
        doc |= {"id": document.media_id} if document.media_id else {"link": document.link}
        if caption:
            doc["caption"] = caption
        return await self._send(to_e164, {"type": "document", "document": doc})

    async def mark_read(self, message_id: str) -> None:
        await self._post(
            f"{self._pnid}/messages",
            {"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
        )

    async def upload_media(self, data: bytes, mime_type: str, filename: str) -> str:
        """Upload once and send by id: Meta then never needs to reach our S3 bucket."""
        response = await self._retry.request(
            self._http,
            PROVIDER,
            "POST",
            f"{self._base}/{self._pnid}/media",
            headers=self._auth,
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, data, mime_type)},
        )
        return str(response.json()["id"])

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        meta = (
            await self._retry.request(
                self._http, PROVIDER, "GET", f"{self._base}/{media_id}", headers=self._auth
            )
        ).json()
        blob = await self._retry.request(self._http, PROVIDER, "GET", str(meta["url"]), headers=self._auth)
        return blob.content, str(meta.get("mime_type") or "application/octet-stream")

    async def _send(self, to_e164: str, message: dict[str, Any]) -> str:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": e164_to_wa_id(to_e164)}
        data = await self._post(f"{self._pnid}/messages", body | message)
        try:
            return str(data["messages"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ChannelError(PROVIDER, "response without message id", retryable=False) from exc

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._retry.request(
            self._http, PROVIDER, "POST", f"{self._base}/{path}", headers=self._auth, json=body
        )
        data: dict[str, Any] = response.json()
        return data
