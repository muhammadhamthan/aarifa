"""Applying a stop in the automation database (§8.1 steps 2 and 9; D18). The rules are `kx.core.optout`.

`apply_stop` runs inside the caller's transaction (the webhook that received "STOP", or the handler of a
stop the CRM recorded), so the suppression, the cancelled queue, the permanent log row and the
`consent.opted_out.v1` event commit together: there is no moment where the stop is logged but a queued
follow-up can still leave.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from kx.automation.models import OptOutLog
from kx.automation.outbox import emit
from kx.automation.repo import suppress, suppress_contact
from kx.core.event_payloads import ConsentScope, OptedOut, StopSource
from kx.core.identity import new_id

# §8.1 step 2: "All queued follow-ups, reminders and templates are cancelled" — everything still queued to
# the person on the stopped scope, except the one confirmation itself
_CANCEL_QUEUED: Final = text(
    """
    UPDATE automation.outbound_messages m SET status = 'cancelled', block_reason = 'opt_out'
      FROM automation.channel_accounts ca
     WHERE ca.id = m.channel_account_id AND m.status = 'queued' AND m.kind <> 'opt_out_confirmation'
       AND (m.recipient_hash = ANY(CAST(:h AS text[]))
            OR (CAST(:c AS uuid) IS NOT NULL AND m.contact_ref = :c))
       AND (:scope = 'all' OR ca.channel = :scope)
    """
)


@dataclass(frozen=True, slots=True)
class StopRecord:
    scope: ConsentScope
    source: StopSource
    channel: str
    address_hashes: tuple[str, ...]
    contact_ref: uuid.UUID | None
    deal_ref: uuid.UUID | None
    thread_ref: uuid.UUID | None
    words: str | None
    reason: str | None
    decided_by: str
    language: str | None
    at: datetime

    def event(self, log_ref: uuid.UUID) -> OptedOut:
        return OptedOut(
            contact_ref=self.contact_ref,
            deal_ref=self.deal_ref,
            thread_ref=self.thread_ref,
            address_hashes=self.address_hashes,
            scope=self.scope,
            source=self.source,
            channel=self.channel,
            exact_words=self.words,
            reason=self.reason,
            language=self.language,
            decided_by=self.decided_by,
            at=self.at,
            log_ref=log_ref,
            origin="automation",
        )


async def apply_stop(s: AsyncSession, stop: StopRecord, *, announce: bool = True) -> uuid.UUID:
    """Suppress (address + person), cancel what is queued, log it permanently and (unless the CRM already
    knows) announce it. Returns the log id. Idempotent in effect: suppressions do nothing twice."""
    reason = f"{stop.source}:{stop.scope}"
    for address_hash in stop.address_hashes:
        await suppress(s, stop.scope, address_hash, reason)
    if stop.contact_ref is not None:
        await suppress_contact(s, stop.contact_ref, stop.scope, reason)
    cancelled = 0
    if stop.scope != "phone":
        result = await s.execute(
            _CANCEL_QUEUED, {"h": list(stop.address_hashes), "c": stop.contact_ref, "scope": stop.scope}
        )
        cancelled = int(getattr(result, "rowcount", 0) or 0)
    log_id = new_id()
    s.add(
        OptOutLog(
            id=log_id,
            at=stop.at,
            channel=stop.channel,
            contact_ref=stop.contact_ref,
            deal_ref=stop.deal_ref,
            thread_ref=stop.thread_ref,
            address_hash=stop.address_hashes[0] if stop.address_hashes else None,
            scope=stop.scope,
            source=stop.source,
            exact_words=stop.words,
            volunteered_reason=stop.reason,
            decided_by=stop.decided_by,
            stopped={"scope": stop.scope, "queued_messages_cancelled": cancelled},
        )
    )
    await s.flush()
    if announce:
        await emit(s, stop.event(log_id))
    return log_id


async def record_confirmation(
    s: AsyncSession, log_id: uuid.UUID, body: str, message_ref: uuid.UUID | None, at: datetime
) -> None:
    """Step 9: the confirmation text and when it was queued (its sent time is on the outbound row)."""
    await s.execute(
        update(OptOutLog)
        .where(OptOutLog.id == log_id)
        .values(confirmation_text=body, confirmation_message_ref=message_ref, confirmation_queued_at=at)
    )


async def note_followups_cancelled(s: AsyncSession, log_id: uuid.UUID, count: int) -> None:
    row = await s.get(OptOutLog, log_id)
    if row is not None:
        row.stopped = {**row.stopped, "followups_cancelled": count}
