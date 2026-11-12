"""A small, real "world" for the §23 scenario tests (the SRS §23).

* CRM: the real services on the test Postgres, reached by the AI through `CrmGatewayImpl`.
* AI: the real orchestrator and graphs, driven by `kx.agents.testing.ScriptedChatModel` (the fake model).
* Automation: the real `EventHandlers` and §16 `ApprovalLadder` on the automation test DB, fed by relaying the
  CRM outbox, with a recording sender that consumes each priced message's single-use grant exactly like
  `OutboundService` does (and refuses a second use).
* Every AI reply is checked against the deal's `allowed_prices` with `kx.core.price_guard.check_prices`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import ModuleType
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select, text, update

from kx.agents.context import AgentContext
from kx.agents.models import ModelRouter
from kx.agents.orchestrator import route
from kx.agents.registry import AgentRegistry, DefaultPromptStore
from kx.agents.service import GraphRunner
from kx.agents.testing import ScriptedChatModel
from kx.automation.alerts import ApprovalLadder, LadderTicket, PauseAlerts, VoiceAlerts
from kx.automation.handlers import EventHandlers, FollowUps
from kx.automation.settings import AutomationSettings
from kx.core.contracts import PRICED_KINDS, AiRole, ContactPatch, OutboundRequest, RequirementPatch
from kx.core.db import Databases
from kx.core.event_payloads import DealNextActionSet, NegotiationPaused
from kx.core.events import Event, relay_once
from kx.core.identity import recipient_hash
from kx.core.price_guard import check_prices
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import ApprovalStatusReaderImpl, CrmGatewayImpl, GrantConsumerImpl
from kx.crm.pdf import Letterhead
from kx.crm.services.settings import SettingsService

REPO = pathlib.Path(__file__).resolve().parents[2]
CRM = LogicalDb.CRM
REG = AgentRegistry()
PROMPTS = DefaultPromptStore()


def crm_conftest() -> ModuleType:
    """The CRM test seed (users Sara/Ravi/manager/MD, §23 price list) without duplicating it."""
    name = "_kx_crm_conftest"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / "packages/kx-core-crm/tests/conftest.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class Uploader:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, *, content_type: str = "application/pdf") -> None:
        self.objects[key] = data


class GrantingSender:
    """MessageSender: records requests; a priced one goes out only if its grant is consumed (single use)."""

    def __init__(self, consumer: GrantConsumerImpl) -> None:
        self.sent: list[OutboundRequest] = []
        self.refused: list[OutboundRequest] = []
        self._consumer = consumer

    async def send(self, request: OutboundRequest) -> uuid.UUID:
        message_id = uuid.uuid4()
        if request.kind in PRICED_KINDS:
            assert request.grant_id is not None and request.content_sha256 is not None
            ok = await self._consumer.consume_grant(
                request.grant_id,
                request.content_sha256,
                message_id,
                recipient_hash(request.channel, request.to_address),
            )
            if not ok:
                self.refused.append(request)
                return message_id
        self.sent.append(request)
        return message_id


class Caller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def place_call(self, to_e164: str, twiml: str, status_callback_url: str) -> str:
        self.calls.append((to_e164, twiml))
        return f"CA{uuid.uuid4().hex}"


@dataclass
class Scheduler:
    """TaskScheduler fake: keeps what automation deferred so the test can run it at its time."""

    ladder: list[tuple[datetime, LadderTicket, int]] = field(default_factory=list)
    pauses: list[NegotiationPaused] = field(default_factory=list)
    followups: list[tuple[DealNextActionSet, datetime]] = field(default_factory=list)
    optout_checks: list[tuple[Any, datetime]] = field(default_factory=list)

    async def enqueue_delivery(self, *a: Any, **k: Any) -> None:
        return None

    async def schedule_ladder_step(self, ticket: LadderTicket, step: int, at: datetime) -> None:
        self.ladder.append((at, ticket, step))

    async def pull_ladder_forward(self, ref: uuid.UUID) -> None:
        return None

    async def schedule_followup(self, action: DealNextActionSet, at: datetime) -> None:
        self.followups.append((action, at))

    async def cancel_followups(self, deal_ref: uuid.UUID) -> int:
        before = len(self.followups)
        self.followups = [(a, at) for a, at in self.followups if a.deal_ref != deal_ref]
        return before - len(self.followups)

    async def schedule_optout_check(self, unclear: Any, at: datetime) -> None:
        self.optout_checks.append((unclear, at))

    async def schedule_quote_delivery_check(self, message_id: uuid.UUID, at: datetime) -> None:
        return None

    async def cancel_optout_check(self, deal_ref: uuid.UUID) -> int:
        return 0

    async def schedule_pause_alert(self, paused: NegotiationPaused, at: datetime) -> None:
        self.pauses.append(paused)

    async def enqueue_media_ingest(self, inbound_id: uuid.UUID) -> None:
        return None

    async def enqueue_email_ingest(self, key: str) -> None:
        return None


class World:
    def __init__(self, dbs: Databases, now: datetime) -> None:
        self.dbs = dbs
        self.now = now
        self.uploader = Uploader()
        self.deps = CrmDeps(
            uploader=self.uploader,
            settings=SettingsService(ttl_seconds=0),
            letterhead=Letterhead(name="Arifa Pack", address="Al Jurf Industrial Area 2, Ajman, UAE"),
            clock=lambda: self.now,
        )
        self.gw = CrmGatewayImpl(dbs, self.deps)
        self.sender = GrantingSender(GrantConsumerImpl(dbs, self.deps))
        self.scheduler = Scheduler()
        self.caller = Caller()
        self.auto = AutomationSettings()
        voice = VoiceAlerts(dbs, self.auto, self.caller, lambda: self.now)
        self.ladder = ApprovalLadder(
            databases=dbs,
            settings=self.auto,
            approvals=ApprovalStatusReaderImpl(dbs),
            voice=voice,
            sender=self.sender,
            scheduler=self.scheduler,
            clock=lambda: self.now,
        )
        self.pause_alerts = PauseAlerts(self.auto, voice, self.sender)
        self.followups = FollowUps(dbs, self.auto, self.sender)
        self.handlers = EventHandlers(
            dbs, self.auto, self.sender, self.ladder, self.scheduler, lambda: self.now
        )
        self.replies: list[str] = []
        self.events: list[Event] = []

    # ------------------------------------------------------------ plumbing
    async def pump(self) -> None:
        """Relay the CRM outbox to automation (the bus in production) and run the pause alerts now."""

        async def publish(event: Event) -> None:
            self.events.append(event)
            await self.handlers.dispatch(event)

        while True:
            async with self.dbs.transaction(CRM) as s:
                if not await relay_once(s, "crm", publish, 100):
                    break
        while self.scheduler.pauses:
            await self.pause_alerts.alert(self.scheduler.pauses.pop(0))

    async def run_ladder_until(self, until: datetime) -> None:
        """Run every §16 ladder step due up to `until`, moving the clock to each step's time."""
        while True:
            due = sorted((x for x in self.scheduler.ladder if x[0] <= until), key=lambda x: x[0])
            if not due:
                break
            at, ticket, step = due[0]
            self.scheduler.ladder.remove(due[0])
            self.now = max(self.now, at)
            await self.ladder.step(ticket, step)
        self.now = max(self.now, until)

    def staff_messages(self, *, since: int = 0) -> list[tuple[str | None, str]]:
        return [
            (r.template_name, r.to_address) for r in self.sender.sent[since:] if r.kind.value == "md_notice"
        ]

    def buyer_messages(self) -> list[OutboundRequest]:
        return [r for r in self.sender.sent if r.kind.value != "md_notice"]

    # ------------------------------------------------------------ buyers, deals, AI
    async def sara_next(self) -> None:
        """Make Sara the next salesperson in the round-robin (scenario determinism)."""
        c = crm_conftest()
        async with self.dbs.transaction(CRM) as s:
            await s.execute(update(m.User).where(m.User.id == c.SALES_ID).values(round_robin_seq=None))
            await s.execute(update(m.User).where(m.User.id == c.SALES2_ID).values(round_robin_seq=10**12))

    async def new_lead(
        self,
        *,
        qty: int,
        country: str = "AE",
        city: str = "Dubai",
        name: str = "Ahmed",
        language: str = "en",
        spec: dict[str, Any] | None = None,
        phone: str | None = None,
    ) -> uuid.UUID:
        c = crm_conftest()
        await self.sara_next()
        deal_id = await self.gw.resolve_deal("whatsapp", phone or c.new_phone(), None)
        await self.gw.update_requirement(
            deal_id,
            RequirementPatch(
                product_sku=c.LIST_SKU,
                quantity=D(qty),
                delivery_country=country,
                delivery_city=city,
                spec=spec or {"lid": "yes"},
            ),
            AiRole.LEAD_QUALIFICATION,
        )
        await self.gw.set_contact(deal_id, ContactPatch(name=name, language=language), AiRole.CONTACT_CAPTURE)
        assert (await self.gw.try_qualify(deal_id, AiRole.LEAD_QUALIFICATION)).qualified
        await self.pump()
        return deal_id

    async def ai(self, deal_id: uuid.UUID, buyer: str, script: list[Any]) -> str | None:
        """One buyer message through the real router + graph with the fake model; guard-checked."""
        deal = await self.gw.deal_context(deal_id)
        rt = route(buyer, deal, disabled=REG.disabled())
        if rt.action != "reply":
            return None
        assert rt.graph is not None
        ctx = AgentContext(
            deal=deal,
            spec=REG.get(rt.role),
            channel="whatsapp",
            language=deal.contact.language,
            gateway=self.gw,
            prompts=PROMPTS,
        )
        runner = GraphRunner(ModelRouter.static(ScriptedChatModel(script=script)), REG, InMemorySaver())
        reply, _ = await runner.run(rt, ctx, buyer, (), {"recursion_limit": 30})
        text_ = reply or ""
        fresh = await self.gw.deal_context(deal_id)
        allowed = (*deal.allowed_prices, *fresh.allowed_prices)
        result = check_prices(text_, allowed, default_currency=deal.currency)
        assert result.ok, f"price outside allowed_prices in {text_!r}: {result.violations}"
        self.replies.append(text_)
        await self.pump()
        return reply

    # ------------------------------------------------------------ CRM reads
    async def stages(self, deal_id: uuid.UUID) -> list[str]:
        async with self.dbs.transaction(CRM) as s:
            rows = await s.execute(
                select(m.StageHistory.to_stage)
                .where(m.StageHistory.deal_id == deal_id)
                .order_by(m.StageHistory.id)
            )
            return list(rows.scalars())

    async def issued(self, deal_id: uuid.UUID) -> list[tuple[int, str]]:
        """(version_no, kind) of every version that was approved and sent (its grant was consumed)."""
        async with self.dbs.transaction(CRM) as s:
            rows = await s.execute(
                select(m.QuoteVersion.version_no, m.QuoteVersion.kind)
                .join(m.Quote, m.Quote.id == m.QuoteVersion.quote_id)
                .join(m.QuoteSendGrant, m.QuoteSendGrant.quote_version_id == m.QuoteVersion.id)
                .where(m.Quote.deal_id == deal_id, m.QuoteSendGrant.consumed_at.is_not(None))
                .order_by(m.QuoteVersion.version_no)
            )
            return [tuple(r) for r in rows.all()]  # type: ignore[misc]

    async def open_approval(self, deal_id: uuid.UUID) -> m.Approval:
        async with self.dbs.transaction(CRM) as s:
            row = (
                await s.execute(
                    select(m.Approval)
                    .join(m.QuoteVersion, m.QuoteVersion.id == m.Approval.quote_version_id)
                    .join(m.Quote, m.Quote.id == m.QuoteVersion.quote_id)
                    .where(m.Quote.deal_id == deal_id, m.Approval.status.in_(("pending", "overdue")))
                )
            ).scalar_one()
            return row

    async def sql(self, statement: str, **params: Any) -> list[Any]:
        async with self.dbs.transaction(CRM) as s:
            result = await s.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []


def dubai(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC) - timedelta(hours=4)


__all__ = ["CRM", "World", "crm_conftest", "dubai", "replace"]
