"""Test doubles for kx.automation (imported by conftest and tests; unique name avoids module clashes)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kx.automation.alerts import LadderTicket
from kx.automation.channels.email import Attachment
from kx.automation.channels.whatsapp import DocumentRef
from kx.automation.ratelimit import Bucket, Decision
from kx.core.event_payloads import DealNextActionSet, NegotiationPaused

# Tuesday 3 Nov 2026, 10:00 Dubai: inside both the alert and the follow-up windows.
NOW = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# ---------------------------------------------------------------- fakes
@dataclass
class FakeKill:
    off: set[str] = field(default_factory=set)

    async def engaged(self, channel: str) -> bool:
        return channel in self.off or "all" in self.off


@dataclass
class FakeLimiter:
    allow: bool = True
    taken: list[tuple[str, str]] = field(default_factory=list)

    async def take(self, scope: str, key: str, bucket: Bucket, cost: int = 1) -> Decision:
        self.taken.append((scope, key))
        return Decision(self.allow, 0.0 if self.allow else 30.0)


@dataclass
class FakeGrants:
    result: bool = True
    calls: list[tuple[uuid.UUID, str, uuid.UUID, str]] = field(default_factory=list)

    async def consume_grant(
        self, grant_id: uuid.UUID, content_sha256: str, message_id: uuid.UUID, recipient_hash: str
    ) -> bool:
        self.calls.append((grant_id, content_sha256, message_id, recipient_hash))
        return self.result


@dataclass
class FakeMedia:
    objects: dict[str, bytes] = field(default_factory=dict)

    async def get(self, key: str) -> bytes:
        return self.objects[key]

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data


@dataclass
class FakeScheduler:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def named(self, name: str) -> list[dict[str, Any]]:
        return [kw for n, kw in self.calls if n == name]

    async def enqueue_delivery(
        self,
        message_id: uuid.UUID,
        *,
        lock: str | None,
        at: datetime | None,
        template_params: tuple[str, ...] = (),
    ) -> None:
        self.calls.append(
            (
                "delivery",
                {"message_id": message_id, "lock": lock, "at": at, "template_params": template_params},
            )
        )

    async def schedule_ladder_step(self, ticket: LadderTicket, step: int, at: datetime) -> None:
        self.calls.append(("ladder", {"ticket": ticket, "step": step, "at": at}))

    async def pull_ladder_forward(self, ref: uuid.UUID) -> None:
        self.calls.append(("pull", {"approval_ref": ref}))

    async def schedule_followup(self, action: DealNextActionSet, at: datetime) -> None:
        self.calls.append(("followup", {"action": action, "at": at}))

    async def cancel_followups(self, deal_ref: uuid.UUID) -> int:
        self.calls.append(("cancel_followups", {"deal_ref": deal_ref}))
        return sum(1 for kw in self.named("followup") if kw["action"].deal_ref == deal_ref)

    async def schedule_optout_check(self, unclear: Any, at: datetime) -> None:
        self.calls.append(("optout_check", {"unclear": unclear, "at": at}))

    async def schedule_quote_delivery_check(self, message_id: uuid.UUID, at: datetime) -> None:
        self.calls.append(("quote_check", {"message_id": message_id, "at": at}))

    async def cancel_optout_check(self, deal_ref: uuid.UUID) -> int:
        self.calls.append(("cancel_optout_check", {"deal_ref": deal_ref}))
        return 0

    async def schedule_pause_alert(self, paused: NegotiationPaused, at: datetime) -> None:
        self.calls.append(("pause", {"paused": paused, "at": at}))

    async def enqueue_media_ingest(self, inbound_id: uuid.UUID) -> None:
        self.calls.append(("media", {"inbound_id": inbound_id}))

    async def enqueue_email_ingest(self, key: str) -> None:
        self.calls.append(("email", {"key": key}))


@dataclass
class FakeWhatsApp:
    sent: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)
    error: Exception | None = None

    def _ok(self, kind: str, *args: Any) -> str:
        if self.error is not None:
            raise self.error
        self.sent.append((kind, args))
        return f"wamid.{len(self.sent)}"

    async def send_text(self, to_e164: str, body: str, *, preview_url: bool = False) -> str:
        return self._ok("text", to_e164, body)

    async def send_template(
        self,
        to_e164: str,
        name: str,
        language: str,
        params: tuple[str, ...] = (),
        document: DocumentRef | None = None,
    ) -> str:
        if document is not None:
            return self._ok("template", to_e164, name, language, params, document)
        return self._ok("template", to_e164, name, language, params)

    async def send_document(self, to_e164: str, document: DocumentRef, caption: str | None = None) -> str:
        return self._ok("document", to_e164, document, caption)

    async def upload_media(self, data: bytes, mime_type: str, filename: str) -> str:
        return "media-1"


@dataclass
class FakeEmail:
    sent: list[dict[str, Any]] = field(default_factory=list)

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
        self.sent.append(
            {
                "to": to_address,
                "subject": subject,
                "body": body,
                "attachment": attachment,
                "list_unsubscribe": list_unsubscribe,
                "from_name": from_name,
            }
        )
        return f"ses-{len(self.sent)}"
