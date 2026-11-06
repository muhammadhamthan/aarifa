"""procrastinate tasks for automation, as a Blueprint the app adds under the `automation` namespace.

    jobs = build_jobs()
    app.add_tasks_from(jobs.blueprint, namespace="automation")
    scheduler = ProcrastinateScheduler(app, jobs)
    ... build services with `scheduler`, then AutomationRuntime(...)
    await app.run_worker_async(additional_context={RUNTIME_KEY: runtime})

Tasks hold no state; they read the `AutomationRuntime` from the job context, so the same
blueprint works in the API (deferring) and the worker (running).

Ordering: delivery jobs to one thread share the procrastinate `lock` "thread:<id>" (run one at a
time, in creation order, retries keep their place). `queueing_lock` is used only for de-duplication
(one pending job per message / approval / deal). Scheduled follow-ups get no thread lock: a job
waiting until tomorrow 09:00 would otherwise hold back today's replies on the same thread.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from procrastinate import App, Blueprint, JobContext, RetryStrategy
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.tasks import Task
from sqlalchemy import select

from kx.automation import inbound
from kx.automation.alerts import ApprovalLadder, LadderTicket, PauseAlerts
from kx.automation.channels.email import parse_inbound
from kx.automation.handlers import EventHandlers, FollowUps
from kx.automation.models import SCHEMA, OutboundMessage
from kx.automation.outbound import OutboundService, TransientDeliveryError
from kx.automation.ports import MediaStore, SweepCallback
from kx.automation.scraping import PriceScraper
from kx.automation.settings import AutomationSettings
from kx.core.db import Databases
from kx.core.event_payloads import DealNextActionSet, NegotiationPaused
from kx.core.events import Event, Publisher, relay_once
from kx.core.identity import sha256_hex
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)

RUNTIME_KEY: Final = "kx.automation"
DELIVERY_RETRY: Final = RetryStrategy(
    max_attempts=6, exponential_wait=4, retry_exceptions={TransientDeliveryError}
)
DEFAULT_RETRY: Final = RetryStrategy(max_attempts=5, exponential_wait=6)
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
    inbound_mail: MediaStore  # SES Mail Manager bucket (raw MIME)
    publish: Publisher  # where relayed automation events go (app bus -> other packages)
    approval_sweep: SweepCallback | None = None


def runtime_of(context: JobContext) -> AutomationRuntime:
    runtime = context.additional_context.get(RUNTIME_KEY)
    if not isinstance(runtime, AutomationRuntime):
        raise TypeError(f"worker started without additional_context[{RUNTIME_KEY!r}]")
    return runtime


@dataclass(frozen=True, slots=True)
class AutomationJobs:
    blueprint: Blueprint
    deliver: Task[Any, Any, Any]
    ladder_step: Task[Any, Any, Any]
    followup: Task[Any, Any, Any]
    pause_alert: Task[Any, Any, Any]
    media_ingest: Task[Any, Any, Any]
    email_ingest: Task[Any, Any, Any]
    handle_event: Task[Any, Any, Any]


def build_jobs(*, scraping: bool = False) -> AutomationJobs:
    """The automation task set. `scraping` is the CRM setting `features.scraping` (off in v1, D14)."""
    bp = Blueprint()

    @bp.task(name="deliver_outbound", pass_context=True, retry=DELIVERY_RETRY, queue="outbound")
    async def deliver_outbound(context: JobContext, message_id: str, template_params: list[str]) -> None:
        final = context.job.attempts >= (DELIVERY_RETRY.max_attempts or 0)
        await runtime_of(context).outbound.deliver(
            uuid.UUID(message_id), tuple(template_params), final_attempt=final
        )

    @bp.task(name="run_approval_ladder", pass_context=True, retry=DEFAULT_RETRY, queue="alerts")
    async def run_approval_ladder(context: JobContext, ticket: dict[str, Any], step: int) -> None:
        await runtime_of(context).ladder.step(LadderTicket.model_validate(ticket), step)

    @bp.task(name="send_followup", pass_context=True, retry=DEFAULT_RETRY, queue="outbound")
    async def send_followup(context: JobContext, action: dict[str, Any]) -> None:
        await runtime_of(context).followups.send(DealNextActionSet.model_validate(action))

    @bp.task(name="alert_negotiation_paused", pass_context=True, retry=DEFAULT_RETRY, queue="alerts")
    async def alert_negotiation_paused(context: JobContext, paused: dict[str, Any]) -> None:
        await runtime_of(context).pause_alerts.alert(NegotiationPaused.model_validate(paused))

    @bp.task(name="ingest_whatsapp_media", pass_context=True, retry=DEFAULT_RETRY, queue="inbound")
    async def ingest_whatsapp_media(context: JobContext, inbound_id: str) -> None:
        await _ingest_media(runtime_of(context), uuid.UUID(inbound_id))

    @bp.task(name="ingest_inbound_email", pass_context=True, retry=DEFAULT_RETRY, queue="inbound")
    async def ingest_inbound_email(context: JobContext, key: str) -> None:
        await _ingest_email(runtime_of(context), key)

    @bp.task(name="handle_event", pass_context=True, retry=DEFAULT_RETRY, queue="events")
    async def handle_event(context: JobContext, event: dict[str, Any]) -> None:
        await runtime_of(context).handlers.dispatch(Event.model_validate(event))

    @bp.periodic(cron="* * * * * */5")  # 6th field = seconds: chat replies must not wait a minute
    @bp.task(name="relay_outbox", pass_context=True, queue="events", queueing_lock="relay_outbox")
    async def relay_outbox(context: JobContext, timestamp: int) -> None:
        await relay_all(runtime_of(context))

    @bp.periodic(cron="*/5 * * * *")
    @bp.task(name="approval_sweep", pass_context=True, queue="alerts")
    async def approval_sweep(context: JobContext, timestamp: int) -> None:
        """Backstop for the event-driven ladder; the CRM-side callback decides what is overdue."""
        if (sweep := runtime_of(context).approval_sweep) is not None:
            await sweep(datetime.fromtimestamp(timestamp, UTC))

    @bp.task(name="run_price_scrapes", pass_context=True, queue="scrape")
    async def run_price_scrapes(context: JobContext, timestamp: int) -> None:
        log.info("scrape.done", results=await runtime_of(context).scraper.run_all())

    if scraping:  # v2 (D14): raw-material price scraping; v1 keeps the code but never schedules it
        bp.periodic(cron="0 2 * * *")(run_price_scrapes)  # 06:00 Dubai, before the manager's day starts

    @bp.periodic(cron="*/5 * * * *")
    @bp.task(name="requeue_outbound", pass_context=True, queue="outbound")
    async def requeue_outbound(context: JobContext, timestamp: int) -> None:
        await requeue_lost(runtime_of(context), datetime.fromtimestamp(timestamp, UTC))

    return AutomationJobs(
        blueprint=bp,
        deliver=deliver_outbound,
        ladder_step=run_approval_ladder,
        followup=send_followup,
        pause_alert=alert_negotiation_paused,
        media_ingest=ingest_whatsapp_media,
        email_ingest=ingest_inbound_email,
        handle_event=handle_event,
    )


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
    Re-enqueue those (the queueing lock makes it a no-op when a job exists). Template messages are
    skipped: their parameters live only in the job (DDL gap, see report) and are logged instead."""
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


# ---------------------------------------------------------------- TaskScheduler over procrastinate
class ProcrastinateScheduler:
    """`kx.automation.ports.TaskScheduler` implementation. Needs the opened App (API and worker)."""

    def __init__(self, app: App, jobs: AutomationJobs) -> None:
        self._app = app
        self._jobs = jobs

    async def enqueue_delivery(
        self,
        message_id: uuid.UUID,
        *,
        lock: str | None,
        at: datetime | None,
        template_params: tuple[str, ...] = (),
    ) -> None:
        await _defer(
            self._jobs.deliver,
            f"deliver:{message_id}",
            at,
            lock=lock,
            message_id=str(message_id),
            template_params=list(template_params),
        )

    async def schedule_ladder_step(self, ticket: LadderTicket, step: int, at: datetime) -> None:
        await _defer(
            self._jobs.ladder_step,
            f"ladder:{ticket.ref}",
            at,
            ticket=ticket.model_dump(mode="json"),
            step=step,
        )

    async def pull_ladder_forward(self, ref: uuid.UUID) -> None:
        """Approver pressed 2: run the waiting ladder step now (it escalates to the manager)."""
        await self._replace(self._jobs.ladder_step, f"ladder:{ref}", None, keep_kwargs=True)

    async def schedule_followup(self, action: DealNextActionSet, at: datetime) -> None:
        """The deal's newest next action replaces a still-pending one (CRM keeps one per deal)."""
        await self._replace(
            self._jobs.followup, f"followup:{action.deal_ref}", at, action=action.model_dump(mode="json")
        )

    async def schedule_pause_alert(self, paused: NegotiationPaused, at: datetime) -> None:
        await _defer(
            self._jobs.pause_alert,
            f"paused:{paused.deal_ref}:{paused.respond_by:%Y%m%d%H%M}",
            at,
            paused=paused.model_dump(mode="json"),
        )

    async def enqueue_media_ingest(self, inbound_id: uuid.UUID) -> None:
        await _defer(self._jobs.media_ingest, f"media:{inbound_id}", None, inbound_id=str(inbound_id))

    async def enqueue_email_ingest(self, key: str) -> None:
        await _defer(self._jobs.email_ingest, f"email:{sha256_hex(key.encode())}", None, key=key)

    async def _replace(
        self,
        task: Task[Any, Any, Any],
        queueing_lock: str,
        at: datetime | None,
        *,
        keep_kwargs: bool = False,
        **kwargs: Any,
    ) -> None:
        manager = self._app.job_manager
        for job in await manager.list_jobs_async(queueing_lock=queueing_lock, status="todo"):
            if (
                job.id is not None
                and await manager.cancel_job_by_id_async(job.id, delete_job=True)
                and keep_kwargs
            ):
                kwargs = dict(job.task_kwargs)
        if keep_kwargs and not kwargs:
            return
        await _defer(task, queueing_lock, at, **kwargs)


async def _defer(
    task: Task[Any, Any, Any],
    queueing_lock: str,
    at: datetime | None,
    *,
    lock: str | None = None,
    **kwargs: Any,
) -> None:
    try:
        await task.configure(queueing_lock=queueing_lock, lock=lock, schedule_at=at).defer_async(**kwargs)
    except AlreadyEnqueued:
        log.debug("job.already_enqueued", queueing_lock=queueing_lock)
