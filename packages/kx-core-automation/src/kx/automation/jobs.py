"""Automation background work over the PostgreSQL schedule table (11 Nov, D25: Celery replaces procrastinate).

    store = JobStore(databases, kicker=<the app's Celery kicker>)
    scheduler = JobScheduler(store)              # the TaskScheduler every service is built with
    ... build services, then AutomationRuntime(...)
    register_jobs(runner, runtime)               # task bodies for `kx.run_job`
    PERIODIC                                     # what Beat runs, by name (the app maps names to Celery)

Tasks hold no state; each body reads the `AutomationRuntime` it was registered with, so the API (which only
enqueues) and the worker (which runs) share one wiring.

Ordering: delivery jobs to one thread share the lock key "thread:<id>" (one at a time, in creation order;
retries keep their place). `dedup_key` de-duplicates (one pending job per message / approval / deal).
Scheduled follow-ups get no thread lock: a job waiting until tomorrow 09:00 would otherwise hold back
today's replies on the same thread.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import select

from kx.automation import inbound
from kx.automation.alerts import ApprovalLadder, LadderTicket, PauseAlerts
from kx.automation.channels.email import parse_inbound
from kx.automation.handlers import EventHandlers, FollowUps, stop_after_no_answer
from kx.automation.models import SCHEMA, OutboundMessage
from kx.automation.outbound import OutboundService, TransientDeliveryError
from kx.automation.ports import MediaStore, SweepCallback
from kx.automation.scraping import PriceScraper
from kx.automation.settings import AutomationSettings
from kx.core.db import Databases
from kx.core.event_payloads import DealNextActionSet, NegotiationPaused, StopUnclear
from kx.core.events import Event, Publisher, relay_once
from kx.core.identity import sha256_hex
from kx.core.jobs import Job, JobRunner, JobSpec, JobStore
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)

# task names (stable: they are stored in automation.scheduled_jobs)
DELIVER: Final = "automation.deliver_outbound"
LADDER: Final = "automation.run_approval_ladder"
FOLLOWUP: Final = "automation.send_followup"
PAUSE_ALERT: Final = "automation.alert_negotiation_paused"
MEDIA: Final = "automation.ingest_whatsapp_media"
EMAIL: Final = "automation.ingest_inbound_email"
EVENT: Final = "automation.handle_event"
OPTOUT_CHECK: Final = "automation.optout_no_answer"  # §8.1 step 6: the 24-hour timer
QUOTE_CHECK: Final = "automation.quote_delivery_check"  # D32: WhatsApp quote delivered in 10 min?

DELIVERY_ATTEMPTS: Final = 6
RELAY_BATCH: Final = 100
RELAY_MAX_ROUNDS: Final = 20
_MEDIA_EXT: Final = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
}


class MediaDownloader(Protocol):
    async def download_media(self, media_id: str) -> tuple[bytes, str]: ...


@dataclass(frozen=True, slots=True)
class AutomationRuntime:
    """Everything the tasks need, built once per process by the app."""

    databases: Databases
    settings: AutomationSettings
    outbound: OutboundService
    ladder: ApprovalLadder
    pause_alerts: PauseAlerts
    followups: FollowUps
    handlers: EventHandlers
    scraper: PriceScraper
    whatsapp_media: MediaDownloader
    media: MediaStore  # platform bucket (quote PDFs, inbound media)
    inbound_mail: MediaStore  # raw inbound MIME (SES Mail Manager bucket; Postmark posts JSON instead)
    publish: Publisher  # where relayed automation events go (app bus -> other packages)
    approval_sweep: SweepCallback | None = None
    scraping_enabled: Callable[[], Awaitable[bool]] | None = None  # CRM `features.scraping` (D14)


def register_jobs(runner: JobRunner, rt: AutomationRuntime) -> None:
    """The automation task set for `kx.run_job` (procrastinate's retries kept: delivery 6 attempts on a
    transient provider error only, everything else 5 attempts on any error)."""

    async def deliver(job: Job) -> None:
        await rt.outbound.deliver(
            uuid.UUID(job.args["message_id"]),
            tuple(job.args.get("template_params") or ()),
            final_attempt=job.final_attempt,
        )

    async def ladder(job: Job) -> None:
        await rt.ladder.step(LadderTicket.model_validate(job.args["ticket"]), int(job.args["step"]))

    async def followup(job: Job) -> None:
        await rt.followups.send(DealNextActionSet.model_validate(job.args["action"]))

    async def pause_alert(job: Job) -> None:
        await rt.pause_alerts.alert(NegotiationPaused.model_validate(job.args["paused"]))

    async def media(job: Job) -> None:
        await _ingest_media(rt, uuid.UUID(job.args["inbound_id"]))

    async def email(job: Job) -> None:
        await _ingest_email(rt, job.args["key"])

    async def event(job: Job) -> None:
        await rt.handlers.dispatch(Event.model_validate(job.args["event"]))

    async def optout_check(job: Job) -> None:
        async with rt.databases.transaction(LogicalDb.AUTOMATION) as s:
            unclear = StopUnclear.model_validate(job.args["unclear"])
            await stop_after_no_answer(s, unclear, rt.handlers.clock())

    async def quote_check(job: Job) -> None:
        await rt.outbound.check_quote_delivery(uuid.UUID(job.args["message_id"]))

    runner.register(
        DELIVER,
        JobSpec(
            deliver, max_attempts=DELIVERY_ATTEMPTS, backoff_seconds=4, retry_on=(TransientDeliveryError,)
        ),
    )
    for name, fn in (
        (LADDER, ladder),
        (FOLLOWUP, followup),
        (PAUSE_ALERT, pause_alert),
        (MEDIA, media),
        (EMAIL, email),
        (EVENT, event),
        (OPTOUT_CHECK, optout_check),
        (QUOTE_CHECK, quote_check),
    ):
        runner.register(name, JobSpec(fn))


Periodic = Callable[[datetime], Awaitable[Any]]


def periodic_jobs(rt: AutomationRuntime) -> dict[str, Periodic]:
    """Beat entries by name (schedules live in the app: `arifaa_pack.schedule`)."""

    async def relay(_: datetime) -> int:
        return await relay_all(rt)

    async def requeue(now: datetime) -> int:
        return await requeue_lost(rt, now)

    async def scrapes(_: datetime) -> Any:
        """v2 (D14): raw-material price scraping. v1 keeps the code but `features.scraping` is off, so
        the job returns without fetching anything."""
        if rt.scraping_enabled is None or not await rt.scraping_enabled():
            log.info("scrape.skipped", reason="features.scraping off")
            return None
        results = await rt.scraper.run_all()
        log.info("scrape.done", results=results)
        return results

    return {
        "relay.automation": relay,
        "automation.requeue_outbound": requeue,
        "automation.price_scrapes": scrapes,
    }


# ---------------------------------------------------------------- task bodies (plain functions: testable)
async def relay_all(rt: AutomationRuntime) -> int:
    total = 0
    for _ in range(RELAY_MAX_ROUNDS):
        async with rt.databases.transaction(LogicalDb.AUTOMATION) as s:
            n = await relay_once(s, SCHEMA, rt.publish, limit=RELAY_BATCH)
        total += n
        if n < RELAY_BATCH:
            break
    return total


async def requeue_lost(rt: AutomationRuntime, now: datetime) -> int:
    """`send()` commits the row, then enqueues; a crash in between leaves a queued row with no job.
    Re-enqueue those (the de-duplication key makes it a no-op when a job exists). Template messages are
    skipped: their parameters live only in the job and are logged instead."""
    async with rt.databases.transaction(LogicalDb.AUTOMATION) as s:
        rows = (
            await s.execute(
                select(OutboundMessage.id, OutboundMessage.thread_id, OutboundMessage.template_id)
                .where(
                    OutboundMessage.status == "queued",
                    OutboundMessage.created_at < now - timedelta(minutes=2),
                    OutboundMessage.created_at > now - timedelta(hours=24),
                    (OutboundMessage.not_before.is_(None)) | (OutboundMessage.not_before <= now),
                )
                .limit(500)
            )
        ).all()
    count = 0
    for message_id, thread_id, template_id in rows:
        if template_id is not None:
            log.warning("outbound.requeue_needs_params", message_id=str(message_id))
            continue
        await rt.outbound.requeue(message_id, f"thread:{thread_id}" if thread_id else None)
        count += 1
    return count


async def _ingest_media(rt: AutomationRuntime, inbound_id: uuid.UUID) -> None:
    async with rt.databases.transaction(LogicalDb.AUTOMATION) as s:
        found = await inbound.media_id_of(s, inbound_id)
    if found is None:
        return
    data, mime = await rt.whatsapp_media.download_media(found[0])
    key = f"whatsapp/{inbound_id}{_MEDIA_EXT.get(mime.split(';')[0], '')}"
    await rt.media.put(key, data, mime)
    async with rt.databases.transaction(LogicalDb.AUTOMATION) as s:
        await inbound.attach_media(s, inbound_id, key)


async def _ingest_email(rt: AutomationRuntime, key: str) -> None:
    mail = parse_inbound(await rt.inbound_mail.get(key), fallback_id=key)
    folder = f"email/{sha256_hex(mail.message_id.encode())[:24]}"  # deterministic: retries overwrite
    keys: list[str] = []
    for i, attachment in enumerate(mail.attachments):
        name = "".join(c if c.isalnum() or c in "._-" else "_" for c in attachment.filename)[:80]
        keys.append(f"{folder}/{i}-{name}")
        await rt.media.put(keys[-1], attachment.data, attachment.content_type)
    async with rt.databases.transaction(LogicalDb.AUTOMATION) as s:
        await inbound.record_email(s, mail, s3_key=key, attachment_keys=tuple(keys))


# ---------------------------------------------------------------- TaskScheduler over the schedule table
class JobScheduler:
    """`kx.automation.ports.TaskScheduler` over `kx.core.jobs.JobStore` (API and worker alike)."""

    def __init__(self, store: JobStore) -> None:
        self.store = store

    async def enqueue_delivery(
        self,
        message_id: uuid.UUID,
        *,
        lock: str | None,
        at: datetime | None,
        template_params: tuple[str, ...] = (),
    ) -> None:
        await self.store.enqueue(
            DELIVER,
            {"message_id": str(message_id), "template_params": list(template_params)},
            dedup_key=f"deliver:{message_id}",
            lock_key=lock,
            run_at=at,
            max_attempts=DELIVERY_ATTEMPTS,
        )

    async def schedule_ladder_step(self, ticket: LadderTicket, step: int, at: datetime) -> None:
        await self.store.enqueue(
            LADDER,
            {"ticket": ticket.model_dump(mode="json"), "step": step},
            dedup_key=f"ladder:{ticket.ref}",
            run_at=at,
        )

    async def pull_ladder_forward(self, ref: uuid.UUID) -> None:
        """Approver pressed 2: run the waiting ladder step now (it escalates to the manager)."""
        await self.store.pull_forward(f"ladder:{ref}")

    async def schedule_followup(self, action: DealNextActionSet, at: datetime) -> None:
        """The deal's newest next action replaces a still-pending one (CRM keeps one per deal)."""
        await self.store.replace(
            FOLLOWUP,
            {"action": action.model_dump(mode="json")},
            dedup_key=f"followup:{action.deal_ref}",
            run_at=at,
        )

    async def cancel_followups(self, deal_ref: uuid.UUID) -> int:
        """§8.1 step 2: an opt-out cancels the buyer's queued follow-ups at once."""
        return await self.store.cancel(dedup_key=f"followup:{deal_ref}")

    async def schedule_pause_alert(self, paused: NegotiationPaused, at: datetime) -> None:
        await self.store.enqueue(
            PAUSE_ALERT,
            {"paused": paused.model_dump(mode="json")},
            dedup_key=f"paused:{paused.deal_ref}:{paused.respond_by:%Y%m%d%H%M}",
            run_at=at,
        )

    async def enqueue_media_ingest(self, inbound_id: uuid.UUID) -> None:
        await self.store.enqueue(MEDIA, {"inbound_id": str(inbound_id)}, dedup_key=f"media:{inbound_id}")

    async def enqueue_email_ingest(self, key: str) -> None:
        await self.store.enqueue(EMAIL, {"key": key}, dedup_key=f"email:{sha256_hex(key.encode())}")

    async def schedule_optout_check(self, unclear: StopUnclear, at: datetime) -> None:
        """§8.1 step 6: the "should we stop messaging you?" timer (any buyer message answers it)."""
        await self.store.replace(
            OPTOUT_CHECK,
            {"unclear": unclear.model_dump(mode="json")},
            dedup_key=f"optout-q:{unclear.deal_ref}",
            run_at=at,
        )

    async def schedule_quote_delivery_check(self, message_id: uuid.UUID, at: datetime) -> None:
        await self.store.enqueue(
            QUOTE_CHECK, {"message_id": str(message_id)}, dedup_key=f"quote-check:{message_id}", run_at=at
        )

    async def cancel_optout_check(self, deal_ref: uuid.UUID) -> int:
        return await self.store.cancel(dedup_key=f"optout-q:{deal_ref}")
