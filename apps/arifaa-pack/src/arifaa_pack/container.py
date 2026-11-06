"""Composition root: builds every platform service once per process and wires the packages together.

Packages never import each other; this module is the only place kx.crm, kx.automation and kx.agents
meet. Event flow: each package relays its outbox -> `EventBus.publish` -> one durable procrastinate
job per subscribed package -> that package's handler (each handler de-duplicates with its inbox).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import aioboto3
import httpx
from procrastinate import App, Blueprint, PsycopgConnector
from redis.asyncio import Redis

from arifaa_pack.adapters import BedrockTitanEmbedder, PayloadContentSource, ThreadAddressBook
from arifaa_pack.config import AppSettings, app_settings, company
from kx.agents.handlers import AgentHandlers
from kx.agents.knowledge import KnowledgeBase, KnowledgeIndexer
from kx.agents.memory import open_checkpointer, psycopg_conninfo
from kx.agents.models import AnthropicApiProvider, BedrockProvider, ModelRouter
from kx.agents.registry import ModelTier
from kx.agents.service import AgentService
from kx.agents.tracing import Tracer
from kx.automation.alerts import ApprovalLadder, PauseAlerts, VoiceAlerts
from kx.automation.channels.email import EmailSender
from kx.automation.channels.sns import SnsVerifier
from kx.automation.channels.voice_ivr import TwilioCaller
from kx.automation.channels.whatsapp import WhatsAppClient
from kx.automation.handlers import EventHandlers, FollowUps
from kx.automation.jobs import AutomationJobs, AutomationRuntime, ProcrastinateScheduler, build_jobs
from kx.automation.killswitch import KillSwitch
from kx.automation.media import S3MediaStore
from kx.automation.outbound import OutboundService
from kx.automation.ratelimit import TokenBucketLimiter
from kx.automation.scraping import PriceScraper
from kx.automation.settings import AutomationSettings
from kx.automation.webhooks import WebhookDeps
from kx.core.db import Databases
from kx.core.events import Event, relay_once
from kx.core.settings import CoreSettings, LogicalDb, get_settings
from kx.crm import jobs as crm_jobs
from kx.crm.deps import CrmDeps
from kx.crm.gateway import ApprovalStatusReaderImpl, CrmGatewayImpl, GrantConsumerImpl
from kx.crm.pdf import Letterhead, S3PdfUploader
from kx.crm.services.settings import SettingsService

EventFn = Callable[[Event], Awaitable[Any]]


class EventBus:
    """Fan-out over procrastinate: durable, retried, ordered per queue. Subscriptions are the union of
    each package's handler tables, so adding a handler in a package needs no change here."""

    def __init__(self, proc: App, automation: AutomationJobs, automation_types: frozenset[str]) -> None:
        self._proc = proc
        self._auto = automation
        self._auto_types = automation_types
        self._local: dict[str, list[str]] = {}

    def route(self, event_type: str, task_name: str) -> None:
        self._local.setdefault(event_type, []).append(task_name)

    async def publish(self, event: Event) -> None:
        payload = event.model_dump(mode="json")
        if event.event_type in self._auto_types:
            await self._auto.handle_event.defer_async(event=payload)
        for task in self._local.get(event.event_type, ()):
            await self._proc.configure_task(task, queue="events").defer_async(event=payload)


@dataclass(frozen=True, slots=True)
class Container:
    core: CoreSettings
    app: AppSettings
    automation_settings: AutomationSettings
    databases: Databases
    http: httpx.AsyncClient
    proc: App
    bus: EventBus
    automation_runtime: AutomationRuntime
    webhook_deps: WebhookDeps
    crm_deps: CrmDeps
    agent_service: AgentService


def procrastinate_conninfo(core: CoreSettings) -> str:
    """The job queue lives in the automation database (procrastinate_* tables in public)."""
    return psycopg_conninfo(core.automation_dsn)


def _model_router(s: AppSettings) -> ModelRouter:
    providers: list[Any] = []
    if s.bedrock_region and s.bedrock_model_ids:
        ids = {ModelTier(k): v for k, v in s.bedrock_model_ids.items()}
        providers.append(BedrockProvider(ids, region=s.bedrock_region))
    providers.append(AnthropicApiProvider(s.anthropic_api_key))
    return ModelRouter(providers)


def _app_blueprint(
    crm_rt: crm_jobs.CrmRuntime,
    agent_handlers: dict[str, EventFn],
    databases: Databases,
    publish: EventFn,
    bus: EventBus,
) -> Blueprint:
    bp = Blueprint()

    @bp.task(name="crm_event", queue="events")
    async def crm_event(event: dict[str, Any]) -> None:
        await crm_jobs.dispatch(crm_rt, Event.model_validate(event))

    @bp.task(name="agents_event", queue="events")
    async def agents_event(event: dict[str, Any]) -> None:
        e = Event.model_validate(event)
        await agent_handlers[e.event_type](e)

    @bp.periodic(cron="* * * * * */5")
    @bp.task(name="relay_agents_outbox", queue="events", queueing_lock="relay_agents_outbox")
    async def relay_agents_outbox(timestamp: int) -> None:
        async with databases.transaction(LogicalDb.AGENTS) as s:
            await relay_once(s, "agents", publish)

    for event_type in crm_jobs.HANDLERS:
        bus.route(event_type, "app:crm_event")
    for event_type in agent_handlers:
        bus.route(event_type, "app:agents_event")
    return bp


@asynccontextmanager
async def build() -> AsyncIterator[Container]:
    core, s, co = get_settings(), app_settings(), company()
    auto_s = AutomationSettings()
    async with AsyncExitStack() as stack:
        dbs = Databases(core)
        stack.push_async_callback(dbs.dispose)
        http = await stack.enter_async_context(httpx.AsyncClient(timeout=auto_s.http_timeout_seconds))
        redis = Redis.from_url(str(core.redis_url))
        stack.push_async_callback(redis.aclose)
        session = aioboto3.Session(region_name=core.aws_region)
        s3 = await stack.enter_async_context(session.client("s3"))
        ses = await stack.enter_async_context(session.client("sesv2"))
        bedrock = await stack.enter_async_context(
            session.client("bedrock-runtime", region_name=s.embedding_region or core.aws_region)
        )

        # ---- job queue + automation
        proc = App(connector=PsycopgConnector(conninfo=procrastinate_conninfo(core)))
        async with dbs.transaction(LogicalDb.CRM) as crm_session:  # D14: v2 features are CRM settings
            features = await SettingsService(ttl_seconds=0).get(crm_session)
        jobs = build_jobs(scraping=features.feature_scraping)
        proc.add_tasks_from(jobs.blueprint, namespace="automation")
        scheduler = ProcrastinateScheduler(proc, jobs)

        crm_deps = CrmDeps(
            uploader=S3PdfUploader(session, s.media_bucket, core.aws_region, s.s3_kms_key_id),
            letterhead=Letterhead(**co.letterhead),
            company_code=co.code,
            base_currency=core.base_currency,
        )
        gateway = CrmGatewayImpl(dbs, crm_deps)
        media = S3MediaStore(s3, s.media_bucket)
        whatsapp = WhatsAppClient(http, auto_s.whatsapp)
        outbound = OutboundService(
            databases=dbs,
            settings=auto_s,
            grants=GrantConsumerImpl(dbs, crm_deps),
            kill_switch=KillSwitch(redis, co.code),
            limiter=TokenBucketLimiter(redis, co.code),
            scheduler=scheduler,
            media=media,
            whatsapp=whatsapp,
            email=EmailSender(ses, auto_s.ses),
        )
        voice = VoiceAlerts(dbs, auto_s, TwilioCaller(http, auto_s.twilio))
        ladder = ApprovalLadder(
            databases=dbs,
            settings=auto_s,
            approvals=ApprovalStatusReaderImpl(dbs),
            voice=voice,
            sender=outbound,
            scheduler=scheduler,
        )
        auto_handlers = EventHandlers(
            databases=dbs, settings=auto_s, sender=outbound, ladder=ladder, scheduler=scheduler
        )
        bus = EventBus(proc, jobs, frozenset(auto_handlers.registry()))

        # ---- AI agents
        embedder = BedrockTitanEmbedder(bedrock, s.embedding_model_id)
        checkpointer = await stack.enter_async_context(open_checkpointer(core.agents_dsn))
        agent_service = AgentService(
            databases=dbs,
            gateway=gateway,
            models=_model_router(s),
            knowledge=KnowledgeBase(dbs, embedder),
            checkpointer=checkpointer,
            tracer=Tracer(s.langfuse_public_key, s.langfuse_secret_key, s.langfuse_host, core.env.value),
        )
        cms_key = s.cms_api_key.get_secret_value() if s.cms_api_key else None
        agent_handlers = AgentHandlers(
            databases=dbs,
            service=agent_service,
            sender=outbound,
            addresses=ThreadAddressBook(dbs),
            indexer=KnowledgeIndexer(dbs, embedder, PayloadContentSource(http, s.cms_url, cms_key)),
            checkpointer=checkpointer,
        ).handlers()

        # ---- CRM jobs + app routing
        crm_rt = crm_jobs.CrmRuntime(dbs, crm_deps, bus.publish)
        proc.add_tasks_from(crm_jobs.build_blueprint(crm_rt), namespace="crm")
        proc.add_tasks_from(_app_blueprint(crm_rt, agent_handlers, dbs, bus.publish, bus), namespace="app")

        auto_rt = AutomationRuntime(
            databases=dbs,
            settings=auto_s,
            outbound=outbound,
            ladder=ladder,
            pause_alerts=PauseAlerts(auto_s, voice, outbound),
            followups=FollowUps(dbs, auto_s, outbound),
            handlers=auto_handlers,
            scraper=PriceScraper(
                databases=dbs, http=http, sources=auto_s.price_sources, timezone=core.company_timezone
            ),
            whatsapp_media=whatsapp,
            media=media,
            inbound_mail=S3MediaStore(s3, s.inbound_mail_bucket),
            publish=bus.publish,
        )
        webhook_deps = WebhookDeps(
            databases=dbs,
            settings=auto_s,
            scheduler=scheduler,
            voice=voice,
            sns=SnsVerifier(http, auto_s.ses.sns_topic_arns),
        )
        await stack.enter_async_context(proc.open_async())
        yield Container(
            core=core,
            app=s,
            automation_settings=auto_s,
            databases=dbs,
            http=http,
            proc=proc,
            bus=bus,
            automation_runtime=auto_rt,
            webhook_deps=webhook_deps,
            crm_deps=crm_deps,
            agent_service=agent_service,
        )
