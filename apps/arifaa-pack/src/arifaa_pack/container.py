"""Composition root: builds every platform service once per process and wires the packages together.

Packages never import each other; this module is the only place kx.crm, kx.automation and kx.agents
meet. Event flow: each package relays its outbox -> `EventBus.publish` -> one durable job per subscribed
package in `automation.scheduled_jobs` -> a Celery worker runs it -> that package's handler (each handler
de-duplicates with its inbox). Celery itself lives in `arifaa_pack.jobqueue` / `arifaa_pack.celery_app`.
"""

from __future__ import annotations

import pathlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Any, Final

import aioboto3
import httpx
from redis.asyncio import Redis

from arifaa_pack.adapters import (
    HttpEmbedder,
    PayloadContentSource,
    PublicLimiter,
    StaffCodeMailer,
    ThreadAddressBook,
    TurnstileVerifier,
    VendorOrder,
)
from arifaa_pack.config import AppSettings, app_settings, company
from arifaa_pack.jobqueue import CeleryKicker
from arifaa_pack.observability import heartbeat
from kx.agents.handlers import AgentHandlers
from kx.agents.knowledge import KnowledgeBase, KnowledgeIndexer
from kx.agents.memory import open_checkpointer
from kx.agents.models import AnthropicApiProvider, ModelRouter
from kx.agents.service import AgentService
from kx.agents.tracing import Tracer
from kx.agents.website import LiveKitConfig, WebsiteDeps
from kx.automation import jobs as auto_jobs
from kx.automation.alerts import ApprovalLadder, CallBridge, PauseAlerts, VoiceAlerts
from kx.automation.channels.email import EmailSender
from kx.automation.channels.email_chain import EmailChain, EmailVendor
from kx.automation.channels.postmark import PostmarkSender
from kx.automation.channels.sns import SnsVerifier
from kx.automation.channels.voice_ivr import TwilioCaller
from kx.automation.channels.whatsapp import WhatsAppClient
from kx.automation.handlers import EventHandlers, FollowUps
from kx.automation.jobs import AutomationRuntime, JobScheduler
from kx.automation.killswitch import KillSwitch
from kx.automation.media import S3MediaStore
from kx.automation.outbound import OutboundService
from kx.automation.ratelimit import Bucket, TokenBucketLimiter
from kx.automation.scraping import PriceScraper
from kx.automation.settings import AutomationSettings, OptOutSettings
from kx.automation.unsubscribe import UnsubscribeDeps, UnsubscribeLinks
from kx.automation.webhooks import WebhookDeps
from kx.core.auth import TokenIssuer
from kx.core.db import Databases
from kx.core.events import Event, relay_once
from kx.core.fallback import FallbackChain
from kx.core.jobs import Job, JobRunner, JobSpec, JobStore, Kicker
from kx.core.logging import get_logger
from kx.core.passwords import SeedBox
from kx.core.settings import CoreSettings, Environment, LogicalDb, get_settings
from kx.core.spend import SpendCaps, SpendMeter
from kx.crm import jobs as crm_jobs
from kx.crm.deps import CrmDeps
from kx.crm.gateway import ApprovalStatusReaderImpl, CrmGatewayImpl, GrantConsumerImpl
from kx.crm.pdf import ChromiumEngine, Letterhead, PdfEngine, QuoteRenderer, S3PdfUploader, WeasyPrintEngine
from kx.crm.services.settings import SettingsService

EventFn = Callable[[Event], Awaitable[Any]]
Periodic = Callable[[datetime], Awaitable[Any]]
CRM_EVENT: Final = "crm.handle_event"
log = get_logger(__name__)
AGENTS_EVENT: Final = "agents.handle_event"


class EventBus:
    """Fan-out over the schedule table: durable, retried, one job per subscribed package. Subscriptions are
    the union of each package's handler tables, so adding a handler in a package needs no change here."""

    def __init__(self, store: JobStore) -> None:
        self._store = store
        self._routes: dict[str, list[str]] = {}

    def route(self, event_type: str, task: str) -> None:
        self._routes.setdefault(event_type, []).append(task)

    def subscribers(self, event_type: str) -> tuple[str, ...]:
        return tuple(self._routes.get(event_type, ()))

    async def publish(self, event: Event) -> None:
        payload = {"event": event.model_dump(mode="json")}
        for task in self._routes.get(event.event_type, ()):
            await self._store.enqueue(task, payload, dedup_key=f"event:{event.event_id}:{task}")


@dataclass(frozen=True, slots=True)
class Container:
    core: CoreSettings
    app: AppSettings
    automation_settings: AutomationSettings
    databases: Databases
    http: httpx.AsyncClient
    jobs: JobStore
    runner: JobRunner
    periodic: dict[str, Periodic]
    bus: EventBus
    automation_runtime: AutomationRuntime
    webhook_deps: WebhookDeps
    unsubscribe_deps: UnsubscribeDeps
    crm_deps: CrmDeps
    agent_service: AgentService
    website_deps: WebsiteDeps
    spend: SpendMeter


def _model_router(s: AppSettings) -> ModelRouter:
    """D27: Claude Sonnet 5.5 on money turns, Haiku 5.5 on the rest, directly through the Claude API (Bedrock
    dropped). The Gemini backups stay off until they pass the 50-scenario suite and Arifa agrees
    (OPEN_QUESTIONS #51); when every Claude call fails the agents give the safe reply and hand over."""
    if s.gemini_fallback_enabled:
        log.warning("gemini_fallback_requested", detail="no Gemini adapter is installed; Claude only")
    return ModelRouter([AnthropicApiProvider(s.anthropic_api_key)])


def _wire_events(
    runner: JobRunner,
    bus: EventBus,
    crm_rt: crm_jobs.CrmRuntime,
    agent_handlers: dict[str, EventFn],
    automation_types: frozenset[str],
) -> None:
    async def crm_event(job: Job) -> None:
        await crm_jobs.dispatch(crm_rt, Event.model_validate(job.args["event"]))

    async def agents_event(job: Job) -> None:
        e = Event.model_validate(job.args["event"])
        await agent_handlers[e.event_type](e)

    runner.register(CRM_EVENT, JobSpec(crm_event))
    runner.register(AGENTS_EVENT, JobSpec(agents_event))
    for event_type in crm_jobs.HANDLERS:
        bus.route(event_type, CRM_EVENT)
    for event_type in agent_handlers:
        bus.route(event_type, AGENTS_EVENT)
    for event_type in automation_types:
        bus.route(event_type, auto_jobs.EVENT)


@dataclass(frozen=True, slots=True)
class _PeriodicDeps:
    store: JobStore
    databases: Databases
    bus: EventBus
    automation: AutomationRuntime
    crm: crm_jobs.CrmRuntime
    http: httpx.AsyncClient
    settings: AppSettings
    company_code: str


def _periodic(d: _PeriodicDeps) -> dict[str, Periodic]:
    """Beat entries by name (`arifaa_pack.schedule`): the app's own plus each package's job bodies."""

    async def dispatch_due(_: datetime) -> int:
        return await d.store.dispatch_due()

    async def relay_agents(_: datetime) -> int:
        async with d.databases.transaction(LogicalDb.AGENTS) as session:
            return await relay_once(session, "agents", d.bus.publish)

    async def beat_heartbeat(_: datetime) -> None:
        await heartbeat(d.http, d.settings, d.company_code, await d.store.lag_seconds())

    return {
        "jobs.dispatch_due": dispatch_due,
        "relay.agents": relay_agents,
        "app.heartbeat": beat_heartbeat,
        **auto_jobs.periodic_jobs(d.automation),
        **crm_jobs.periodic_jobs(d.crm),
    }


@asynccontextmanager
async def build(kicker: Kicker | None = None) -> AsyncIterator[Container]:
    """Every process builds the same container. `kicker` defaults to Celery (API, worker, voice)."""
    core, s, co = get_settings(), app_settings(), company()
    async with AsyncExitStack() as stack:
        dbs = Databases(core)
        stack.push_async_callback(dbs.dispose)
        http = await stack.enter_async_context(
            httpx.AsyncClient(timeout=AutomationSettings().http_timeout_seconds)
        )
        redis = Redis.from_url(str(core.redis_url))
        stack.push_async_callback(redis.aclose)
        session = aioboto3.Session(region_name=core.aws_region)
        s3 = await stack.enter_async_context(session.client("s3"))
        ses = await stack.enter_async_context(session.client("sesv2"))
        ssm = await stack.enter_async_context(session.client("ssm"))

        # ---- job store (D25) + automation
        store = JobStore(dbs, kicker=kicker if kicker is not None else CeleryKicker())
        runner = JobRunner(store)
        scheduler = JobScheduler(store)
        crm_settings = SettingsService(ttl_seconds=30)
        async with dbs.transaction(LogicalDb.CRM) as crm_session:  # §8.1 settings live in the CRM
            business = await SettingsService(ttl_seconds=0).get(crm_session)
        auto_s = AutomationSettings(  # init values win over KX_AUTO_* env for these two
            company_name=co.name,
            optout=OptOutSettings(
                ask_reason=business.optout_ask_reason,
                live_minutes=business.optout_live_minutes,
                unclear_answer_hours=business.optout_unclear_answer_hours,
            ),
        )
        unsubscribe_links = UnsubscribeLinks(
            core.service_token_secret.get_secret_value(), auto_s.public_base_url
        )

        async def scraping_enabled() -> bool:  # D14: v2 features are CRM settings
            async with dbs.transaction(LogicalDb.CRM) as crm_session:
                return (await crm_settings.get(crm_session)).feature_scraping

        vendors = VendorOrder(ssm, s.vendor_order_ssm_prefix, VENDOR_DEFAULTS)  # D30
        email_sender = _email_chain(ses, http, auto_s, vendors, co.code)
        crm_deps = CrmDeps(
            uploader=S3PdfUploader(session, s.media_bucket, core.aws_region, s.s3_kms_key_id),
            letterhead=Letterhead(**co.letterhead),
            renderer=_pdf_renderer(s, co.code),  # D37 Chromium -> WeasyPrint
            company_code=co.code,
            base_currency=core.base_currency,
            tokens=_token_issuer(core),  # D26 in-house login (RS256)
            seed_box=_seed_box(core),
            mailer=StaffCodeMailer([email_sender]),
            auth=core.auth,
        )
        media = S3MediaStore(s3, s.media_bucket)
        whatsapp = WhatsAppClient(http, auto_s.whatsapp)
        twilio = TwilioCaller(http, auto_s.twilio)
        limiter = TokenBucketLimiter(redis, co.code)
        spend = SpendMeter(  # D34 daily caps (OPEN_QUESTIONS #52)
            redis,
            co.code,
            SpendCaps(s.spend_ai_usd_per_day, s.spend_voice_minutes_per_day, s.spend_wa_templates_per_day),
            timezone=core.company_timezone,
        )
        kill_switch = KillSwitch(redis, co.code)
        outbound = OutboundService(
            databases=dbs,
            settings=auto_s,
            grants=GrantConsumerImpl(dbs, crm_deps),
            kill_switch=kill_switch,
            limiter=limiter,
            scheduler=scheduler,
            media=media,
            whatsapp=whatsapp,
            email=email_sender,
            unsubscribe_link=unsubscribe_links,
            template_spend=spend,  # D34 WhatsApp-template cap
        )
        # §14 (D21): staff reply from the CRM inbox and click-to-call, both through the company number
        crm_deps = replace(
            crm_deps,
            sender=outbound,
            caller=CallBridge(dbs, auto_s, twilio, kill_switch),
            kill_switch=kill_switch,  # D35 ladder, set from the CRM admin screen
        )
        gateway = CrmGatewayImpl(dbs, crm_deps)
        voice = VoiceAlerts(dbs, auto_s, twilio)
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
        bus = EventBus(store)

        # ---- AI agents
        embedder = HttpEmbedder(  # D28 Qwen3-Embedding-0.6B (OPEN_QUESTIONS #53)
            http,
            s.embedding_url or "http://embeddings:80",
            s.embedding_model,
            api_key=s.embedding_api_key.get_secret_value() if s.embedding_api_key else None,
        )
        checkpointer = await stack.enter_async_context(open_checkpointer(core.agents_dsn))

        agent_service = AgentService(
            databases=dbs,
            gateway=gateway,
            models=_model_router(s),
            knowledge=KnowledgeBase(dbs, embedder, query_timeout_seconds=s.embedding_timeout_seconds),
            checkpointer=checkpointer,
            tracer=Tracer(s.phoenix_collector_endpoint, s.phoenix_project, core.env.value),  # D29
            live_minutes=business.optout_live_minutes,
            company=co.code,
            on_spend=partial(spend.add, "ai_usd"),  # D34 daily AI cap
            budget_left=partial(spend.left, "ai_usd"),
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

        # ---- CRM jobs + event routing
        crm_rt = crm_jobs.CrmRuntime(dbs, crm_deps, bus.publish)
        _wire_events(runner, bus, crm_rt, agent_handlers, frozenset(auto_handlers.registry()))

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
            scraping_enabled=scraping_enabled,
        )
        auto_jobs.register_jobs(runner, auto_rt)
        webhook_deps = WebhookDeps(
            databases=dbs,
            settings=auto_s,
            scheduler=scheduler,
            voice=voice,
            sns=SnsVerifier(http, auto_s.ses.sns_topic_arns),
            media=media,  # Postmark inbound attachments
        )
        periodic = _periodic(_PeriodicDeps(store, dbs, bus, auto_rt, crm_rt, http, s, co.code))
        yield Container(
            core=core,
            app=s,
            automation_settings=auto_s,
            databases=dbs,
            http=http,
            jobs=store,
            runner=runner,
            periodic=periodic,
            bus=bus,
            automation_runtime=auto_rt,
            webhook_deps=webhook_deps,
            unsubscribe_deps=UnsubscribeDeps(dbs, auto_s, core.service_token_secret.get_secret_value()),
            crm_deps=crm_deps,
            agent_service=agent_service,
            website_deps=_website(
                core,
                s,
                dbs=dbs,
                http=http,
                service=agent_service,
                gateway=gateway,
                spend=spend,
                limiter=limiter,
                kill_switch=kill_switch,
            ),
            spend=spend,
        )


def _seed_box(core: CoreSettings) -> SeedBox:
    """TOTP seeds are encrypted at rest with KX_AUTH__ENCRYPTION_KEY. Local runs and tests may start without
    one (a process-local key: authenticator enrolments do not survive a restart); staging and production
    refuse to start, because a lost key forces every staff member to enrol again."""
    if core.auth.encryption_key is not None:
        return SeedBox(core.auth.encryption_key.get_secret_value())
    if core.env in {Environment.STAGING, Environment.PRODUCTION}:
        raise RuntimeError("KX_AUTH__ENCRYPTION_KEY is required outside local and test")
    log.warning("auth_dev_seed_key", detail="KX_AUTH__ENCRYPTION_KEY unset; using a process-local key")
    return SeedBox(SeedBox.new_key())


def _token_issuer(core: CoreSettings) -> TokenIssuer:
    """Every API task must sign with the same key (KX_JWT_PRIVATE_KEY from Secrets Manager); a generated
    key is only for one local process."""
    if core.jwt_private_key is None and core.env in {Environment.STAGING, Environment.PRODUCTION}:
        raise RuntimeError("KX_JWT_PRIVATE_KEY is required outside local and test")
    return TokenIssuer.from_settings(core)


# D30 default vendor orders (Fallback plan); Parameter Store overrides them without a deploy
VENDOR_DEFAULTS: dict[str, tuple[str, ...]] = {"email": ("ses", "postmark"), "calls": ("twilio",)}
PUBLIC_BUCKETS = {"ip": Bucket(30, 30), "session": Bucket(12, 12)}  # OPEN_QUESTIONS #61


def _website(
    core: CoreSettings,
    s: AppSettings,
    *,
    dbs: Databases,
    http: httpx.AsyncClient,
    service: AgentService,
    gateway: CrmGatewayImpl,
    spend: SpendMeter,
    limiter: TokenBucketLimiter,
    kill_switch: KillSwitch,
) -> WebsiteDeps:
    """§6 public website API (D39): Turnstile + per-IP/per-session limits + the daily AI spend cap."""
    if s.turnstile_secret_key is None and core.env in {Environment.STAGING, Environment.PRODUCTION}:
        raise RuntimeError("KX_APP_TURNSTILE_SECRET_KEY is required outside local and test")
    livekit = (
        LiveKitConfig(s.livekit_url, s.livekit_api_key, s.livekit_api_secret.get_secret_value())
        if s.livekit_url and s.livekit_api_key and s.livekit_api_secret
        else None
    )

    async def ai_budget_left() -> bool:
        return await spend.left("ai_usd")

    async def voice_allowed() -> bool:  # D34 voice-minute cap, D35 kill switch at `text_only`
        return await spend.left("voice_minutes") and not await kill_switch.engaged("voice")

    return WebsiteDeps(
        databases=dbs,
        service=service,
        gateway=gateway,
        secret=core.service_token_secret.get_secret_value(),
        human_check=(
            TurnstileVerifier(http, s.turnstile_secret_key.get_secret_value())
            if s.turnstile_secret_key
            else None
        ),
        limits=PublicLimiter(limiter, PUBLIC_BUCKETS),
        ai_budget_left=ai_budget_left,
        voice_allowed=voice_allowed,
        livekit=livekit,
    )


def _email_chain(
    ses: Any, http: httpx.AsyncClient, auto_s: AutomationSettings, vendors: VendorOrder, company_code: str
) -> EmailChain:
    """D33 email field: SES -> Postmark (when its token is set), in the Parameter Store order."""
    email_vendors: dict[str, EmailVendor] = {"ses": EmailSender(ses, auto_s.ses)}
    if auto_s.postmark.enabled:
        email_vendors["postmark"] = PostmarkSender(http, auto_s.postmark, auto_s.ses.from_address)

    async def order() -> tuple[str, ...]:
        return await vendors.order("email")

    return EmailChain(email_vendors, FallbackChain("email", company_code), order)


def _pdf_renderer(s: AppSettings, company_code: str) -> QuoteRenderer:
    """D37: headless Chromium on the worker, WeasyPrint as the backup (every switch is counted). Without the
    Chromium binary (a developer laptop) WeasyPrint alone, said once at start-up."""
    engines: list[PdfEngine] = []
    if s.pdf_renderer == "chromium":
        if pathlib.Path(s.chromium_path).exists():
            engines.append(ChromiumEngine(s.chromium_path))
        else:
            log.warning("pdf_chromium_missing", path=s.chromium_path, detail="using WeasyPrint only")
    engines.append(WeasyPrintEngine())
    return QuoteRenderer(engines=engines, company=company_code)
