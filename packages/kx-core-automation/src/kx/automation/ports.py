"""Protocols this package depends on. The app wires real implementations; tests inject fakes.

Kept separate from implementations so services never import the job queue (jobs import services).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from kx.core.event_payloads import DealNextActionSet, NegotiationPaused

if TYPE_CHECKING:
    from kx.automation.alerts import LadderTicket

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@runtime_checkable
class ApprovalStatusReader(Protocol):
    """Implemented by the app over kx.crm: is this approval or change request still waiting for a person?

    The ladder re-reads this before every step so a decided one never rings anyone's phone.
    """

    async def is_pending(self, approval_ref: uuid.UUID) -> bool: ...


@runtime_checkable
class MediaStore(Protocol):
    """Object storage for quote PDFs, inbound media and raw inbound email (S3 in production)."""

    async def get(self, key: str) -> bytes: ...
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...


@runtime_checkable
class TaskScheduler(Protocol):
    """Deferral of background work. Implemented over procrastinate in `kx.automation.jobs`."""

    async def enqueue_delivery(
        self,
        message_id: uuid.UUID,
        *,
        lock: str | None,
        at: datetime | None,
        template_params: tuple[str, ...] = (),
    ) -> None: ...
    async def schedule_ladder_step(self, ticket: LadderTicket, step: int, at: datetime) -> None: ...
    async def pull_ladder_forward(self, ref: uuid.UUID) -> None: ...
    async def schedule_followup(self, action: DealNextActionSet, at: datetime) -> None: ...
    async def schedule_pause_alert(self, paused: NegotiationPaused, at: datetime) -> None: ...
    async def enqueue_media_ingest(self, inbound_id: uuid.UUID) -> None: ...
    async def enqueue_email_ingest(self, key: str) -> None: ...


SweepCallback = Callable[[datetime], Awaitable[None]]
