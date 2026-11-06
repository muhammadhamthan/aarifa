"""Schema fidelity, retention, event handlers, approval ladder, scraper, ingest jobs, outbox relay."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import httpx
import pytest
import respx
from automation_fakes import Clock, FakeMedia, FakeScheduler
from sqlalchemy import text
from tenacity import wait_none
from test_auto_email import RAW_REPLY

from kx.automation import jobs
from kx.automation.alerts import ApprovalLadder, LadderTicket, PauseAlerts, VoiceAlerts
from kx.automation.channels import HttpRetry
from kx.automation.handlers import EventHandlers, FollowUps
from kx.automation.models import Base
from kx.automation.scraping import PriceScraper
from kx.automation.settings import AutomationSettings, PriceSourceConfig
from kx.core import events as ev
from kx.core.contracts import OutboundRequest
from kx.core.errors import RuleViolation
from kx.core.event_payloads import (
    ChangeRequested,
    ContactAnonymised,
    DealNextActionSet,
    NegotiationPaused,
    QuoteApproved,
    QuoteSubmitted,
    StaffContact,
    make_event,
)
from kx.core.events import Event
from kx.core.identity import recipient_hash
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db
SETTINGS = AutomationSettings()


async def sql(db: Any, statement: str, **params: Any) -> list[Any]:
    async with db.transaction(LogicalDb.AUTOMATION) as s:
        result = await s.execute(text(statement), params)
        return list(result.all()) if result.returns_rows else []


# ---------------------------------------------------------------- schema
async def test_orm_matches_ddl(auto_db: Any) -> None:
    rows = await sql(
        auto_db,
        "SELECT table_name, column_name, is_nullable, data_type FROM information_schema.columns "
        "WHERE table_schema = 'automation' AND table_name NOT LIKE 'procrastinate%'",
    )
    ddl = {(t, c): (n == "YES", ty) for t, c, n, ty in rows}
    orm = {(table.name, col.name): col for table in Base.metadata.tables.values() for col in table.columns}
    assert set(ddl) == set(orm)
    for key, col in orm.items():
        assert ddl[key][0] == col.nullable, key
    assert {t.name for t in Base.metadata.tables.values()} == {k[0] for k in ddl}


async def test_sql_recipient_hash_matches_python(auto_db: Any) -> None:
    from kx.automation.handlers import _SQL_RECIPIENT_HASH

    for address in ("+971501234567", "buyer@example.com"):
        (row,) = await sql(auto_db, f"SELECT {_SQL_RECIPIENT_HASH.format(col=':a')}", a=address)
        channel = "email" if "@" in address else "whatsapp"
        assert row[0] == recipient_hash(channel, address)


# ---------------------------------------------------------------- handlers
class RecordingSender:
    def __init__(self, error: RuleViolation | None = None) -> None:
        self.requests: list[OutboundRequest] = []
        self.error = error

    async def send(self, request: OutboundRequest) -> uuid.UUID:
        self.requests.append(request)
        if self.error:
            raise self.error
        return uuid.uuid4()


class Approvals:
    def __init__(self, pending: bool = True) -> None:
        self.pending = pending

    async def is_pending(self, approval_ref: uuid.UUID) -> bool:
        return self.pending


class Caller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def place_call(self, to_e164: str, twiml: str, status_callback_url: str) -> str:
        self.calls.append((to_e164, twiml))
        return f"CA{len(self.calls)}"


# §16: the salesperson approves, the manager is the escalation
SALES = StaffContact(
    user_ref=uuid.uuid4(), name="Sara", phone="+971501111111", email=None, whatsapp="+971501111111"
)
MANAGER = StaffContact(
    user_ref=uuid.uuid4(), name="Faisal", phone="+971502222222", email=None, whatsapp="+971502222222"
)


def submitted(clock: Clock) -> QuoteSubmitted:
    return QuoteSubmitted(
        approval_ref=uuid.uuid4(),
        quote_ref=uuid.uuid4(),
        quote_version_ref=uuid.uuid4(),
        deal_ref=uuid.uuid4(),
        kind="commercial",
        required_role="sales",
        approver=SALES,
        escalate_to=MANAGER,
        due_at=clock.now + timedelta(minutes=120),
        quote_number="Q-1045",
        total_display="AED 11,390.20",
        remind_at=(clock.now + timedelta(minutes=30), clock.now + timedelta(minutes=60)),
    )


def ticket(clock: Clock) -> LadderTicket:
    return LadderTicket.for_approval(submitted(clock))


def build(
    db: Any, clock: Clock, sender: RecordingSender | None = None, approvals: Approvals | None = None
) -> tuple[EventHandlers, ApprovalLadder, FakeScheduler, Caller]:
    scheduler, caller, sender = FakeScheduler(), Caller(), sender or RecordingSender()
    voice = VoiceAlerts(db, SETTINGS, caller, clock)
    ladder = ApprovalLadder(
        databases=db,
        settings=SETTINGS,
        approvals=approvals or Approvals(),
        voice=voice,
        sender=sender,
        scheduler=scheduler,
        clock=clock,
    )
    return EventHandlers(db, SETTINGS, sender, ladder, scheduler, clock), ladder, scheduler, caller


async def test_handler_runs_once_per_event(auto_db: Any, clock: Clock) -> None:
    handlers, _, scheduler, _ = build(auto_db, clock)
    event = make_event(ev.QUOTE_SUBMITTED, "approval", uuid.uuid4(), submitted(clock))
    assert await handlers.dispatch(event)
    assert not await handlers.dispatch(event)
    assert not await handlers.dispatch(Event(event_type="unknown.v1", aggregate_type="x", aggregate_id="1"))
    (step,) = scheduler.named("ladder")
    assert step["step"] == 1 and step["at"] == clock.now and step["ticket"].kind == "approval"


async def test_transient_refusal_rolls_back_inbox_for_retry(auto_db: Any, clock: Clock) -> None:
    sender = RecordingSender(RuleViolation("off", reason="kill_switch"))
    handlers, *_ = build(auto_db, clock, sender)
    approved = QuoteApproved(
        approval_ref=uuid.uuid4(),
        quote_ref=uuid.uuid4(),
        quote_version_ref=uuid.uuid4(),
        deal_ref=uuid.uuid4(),
        contact_ref=uuid.uuid4(),
        kind="final",
        grant_ref=uuid.uuid4(),
        channel="whatsapp",
        to_address="+971501234567",
        pdf_s3_key="q.pdf",
        body="Quote",
        subject=None,
        content_sha256="a" * 64,
    )
    event = make_event(ev.QUOTE_APPROVED, "approval", approved.approval_ref, approved)
    with pytest.raises(RuleViolation):
        await handlers.dispatch(event)
    sender.error = None
    assert await handlers.dispatch(event)
    request = sender.requests[-1]
    assert (request.kind.value, request.grant_id, request.idempotency_key) == (
        "final_quote",
        approved.grant_ref,
        f"quote-approved:{approved.approval_ref}",
    )


async def test_next_action_and_pause_are_scheduled(auto_db: Any, clock: Clock) -> None:
    handlers, _, scheduler, _ = build(auto_db, clock)
    action = DealNextActionSet(
        deal_ref=uuid.uuid4(),
        contact_ref=uuid.uuid4(),
        due_at=clock.now - timedelta(hours=1),
        reason="followup_d2",
        buyer_timezone="Asia/Dubai",
    )
    await handlers.dispatch(make_event(ev.DEAL_NEXT_ACTION_SET, "deal", action.deal_ref, action))
    assert scheduler.named("followup")[0]["at"] == clock.now  # overdue -> now (window applied at send)
    clock.now = clock.now.replace(hour=19)  # 23:00 Dubai
    paused = NegotiationPaused(
        deal_ref=uuid.uuid4(),
        reason="below_range",
        alert=(SALES, MANAGER),
        respond_by=clock.now + timedelta(minutes=60),
    )
    await handlers.dispatch(make_event(ev.NEGOTIATION_PAUSED, "deal", paused.deal_ref, paused))
    assert scheduler.named("pause")[0]["at"].isoformat() == "2026-11-04T04:00:00+00:00"


async def test_followup_uses_contact_thread_address(auto_db: Any) -> None:
    contact = uuid.uuid4()
    await sql(
        auto_db,
        "INSERT INTO threads (channel_account_id, external_key, contact_ref) "
        "SELECT id, '+971509999999', :c FROM channel_accounts WHERE external_account_id = 'PNID1'",
        c=contact,
    )
    sender = RecordingSender()
    action = DealNextActionSet(
        deal_ref=uuid.uuid4(),
        contact_ref=contact,
        due_at=datetime.now(UTC),
        reason="followup_d5",
        buyer_timezone="Asia/Kolkata",
    )
    await FollowUps(auto_db, SETTINGS, sender).send(action)
    (request,) = sender.requests
    assert (request.to_address, request.template_name, request.kind.value) == (
        "+971509999999",
        "followup_2",
        "followup",
    )


async def test_anonymise_blanks_copies_and_suppresses(auto_db: Any, clock: Clock) -> None:
    contact, phone = uuid.uuid4(), "+971501234567"
    (thread,) = await sql(
        auto_db,
        "INSERT INTO threads (channel_account_id, external_key, contact_ref) "
        "SELECT id, :p, :c FROM channel_accounts WHERE external_account_id = 'PNID1' "
        "RETURNING id",
        p=phone,
        c=contact,
    )
    await sql(
        auto_db,
        "INSERT INTO inbound_messages (thread_id, provider, provider_message_id, kind, text, raw) "
        "VALUES (:t, 'meta_cloud', 'w1', 'text', 'my address is ...', CAST(:r AS jsonb))",
        t=thread[0],
        r='{"a": 1}',
    )
    await sql(
        auto_db,
        "INSERT INTO outbound_messages (channel_account_id, to_address, kind, body, requested_by, "
        "idempotency_key) SELECT id, :p, 'reply', 'hi', 'ai', 'k' FROM channel_accounts "
        "WHERE external_account_id = 'PNID1'",
        p=phone,
    )
    handlers, *_ = build(auto_db, clock)
    payload = ContactAnonymised(contact_ref=contact, recipient_hashes=(recipient_hash("whatsapp", phone),))
    await handlers.dispatch(make_event(ev.CONTACT_ANONYMISED, "contact", contact, payload))
    assert (await sql(auto_db, "SELECT external_key LIKE 'anon:%' FROM threads")) == [(True,)]
    assert await sql(auto_db, "SELECT text, raw FROM inbound_messages") == [(None, {})]
    assert await sql(auto_db, "SELECT to_address, body FROM outbound_messages") == [("anonymised", None)]
    assert await sql(auto_db, "SELECT channel, reason FROM suppressions") == [("all", "anonymised")]


# ---------------------------------------------------------------- approval ladder
async def test_ladder_follows_section_16(auto_db: Any, clock: Clock) -> None:
    """0 min WhatsApp to the salesperson, 30 min WhatsApp reminder, 60 min alert call, 120 min the manager."""
    sender = RecordingSender()
    _, ladder, scheduler, caller = build(auto_db, clock, sender)
    t = ticket(clock)
    await ladder.step(t, 1)
    await ladder.step(t, 2)
    assert caller.calls == []  # no call before the 60-minute step
    for _ in range(2):
        await ladder.step(t, 3)  # retried job: no second call
    assert [to for to, _ in caller.calls] == [SALES.phone]
    assert "Quote Q-1045 for AED 11,390.20 is waiting for your approval" in caller.calls[0][1]
    assert [(c["step"], c["at"]) for c in scheduler.named("ladder")] == [
        (2, clock.now + timedelta(minutes=30)),
        (3, clock.now + timedelta(minutes=60)),
        (4, clock.now + timedelta(minutes=120)),
        (4, clock.now + timedelta(minutes=120)),
    ]
    await ladder.step(t, 4)
    assert caller.calls[-1][0] == MANAGER.phone and "did not respond" in caller.calls[-1][1]
    assert "reassign" in caller.calls[-1][1]
    assert [(r.template_name, r.to_address) for r in sender.requests] == [
        ("approval_request", SALES.whatsapp),
        ("approval_reminder", SALES.whatsapp),
        ("approval_escalation", MANAGER.whatsapp),
    ]
    assert sender.requests[-1].template_params == ("Quote Q-1045 for AED 11,390.20", "Sara")


async def test_ladder_stops_when_decided_and_waits_at_night(auto_db: Any, clock: Clock) -> None:
    approvals = Approvals()
    sender = RecordingSender()
    _, ladder, scheduler, caller = build(auto_db, clock, sender, approvals=approvals)
    t = ticket(clock)
    await ladder.step(t, 3)
    await sql(auto_db, "UPDATE call_attempts SET digits_pressed = '1'")  # acknowledged on the call
    await ladder.step(t, 3)
    assert len(caller.calls) == 1 and scheduler.named("ladder")[-1]["step"] == 4  # escalation still due
    approvals.pending = False  # decided in the CRM: nothing more, not even the escalation
    await ladder.step(t, 4)
    assert len(caller.calls) == 1 and sender.requests == []
    approvals.pending = True
    other = ticket(clock)
    clock.now = clock.now.replace(hour=19)  # 23:00 Dubai
    await ladder.step(other, 1)
    assert sender.requests == []
    assert scheduler.named("ladder")[-1]["at"].isoformat() == "2026-11-04T04:00:00+00:00"


async def test_press_2_escalates_to_the_manager_now(auto_db: Any, clock: Clock) -> None:
    sender = RecordingSender()
    _, ladder, _, caller = build(auto_db, clock, sender)
    t = ticket(clock)
    await ladder.step(t, 3)
    await sql(auto_db, "UPDATE call_attempts SET digits_pressed = '2'")
    await ladder.step(t, 3)  # the pulled-forward step
    assert caller.calls[-1][0] == MANAGER.phone
    assert sender.requests[-1].template_name == "approval_escalation"


async def test_change_request_runs_the_same_ladder_with_its_own_script(auto_db: Any, clock: Clock) -> None:
    """§13.5: immediate alert, the §16 reminders, then the manager; calls use the change-request script."""
    sender = RecordingSender()
    handlers, ladder, scheduler, caller = build(auto_db, clock, sender)
    cr = ChangeRequested(
        deal_ref=uuid.uuid4(),
        deal_number="D-2026-00042",
        change_request_ref=uuid.uuid4(),
        type="delivery",
        assigned_to=SALES,
        escalate_to=MANAGER,
        sla_due_at=clock.now + timedelta(hours=4),
        remind_at=(clock.now + timedelta(minutes=30), clock.now + timedelta(minutes=60)),
        escalate_at=clock.now + timedelta(minutes=120),
    )
    assert await handlers.dispatch(
        make_event(ev.CHANGE_REQUESTED, "change_request", cr.change_request_ref, cr)
    )
    (first,) = scheduler.named("ladder")
    t = first["ticket"]
    assert (first["step"], t.kind, t.ref, t.title) == (
        1,
        "change_request",
        cr.change_request_ref,
        "A buyer delivery change request on deal D-2026-00042",
    )
    await ladder.step(t, 1)
    await ladder.step(t, 3)
    assert sender.requests[0].template_name == "change_request"
    assert "waiting for you in the CRM" in caller.calls[0][1]
    (purpose,) = await sql(auto_db, "SELECT purpose FROM call_attempts")
    assert purpose == ("change_request_alert",)


async def test_pause_alerts_message_and_call_everyone(auto_db: Any, clock: Clock) -> None:
    sender, caller = RecordingSender(), Caller()
    alerts = PauseAlerts(SETTINGS, VoiceAlerts(auto_db, SETTINGS, caller, clock), sender)
    await alerts.alert(
        NegotiationPaused(
            deal_ref=uuid.uuid4(),
            reason="below_range",
            alert=(SALES, MANAGER),
            respond_by=clock.now + timedelta(minutes=60),
        )
    )
    assert [r.to_address for r in sender.requests] == [SALES.whatsapp, MANAGER.whatsapp]
    assert sender.requests[0].template_params == ("11:00",)
    assert [c[0] for c in caller.calls] == [SALES.phone, MANAGER.phone]


# ---------------------------------------------------------------- scraper
SOURCE = PriceSourceConfig(
    code="lme_al",
    source_ref=uuid.uuid4(),
    url="https://prices.example.com/al",  # type: ignore[arg-type]
    material_code="AL_INGOT",
    price_selector="td.cash",
    currency="usd",
    unit="tonne",
    terms_ok=True,
)


@respx.mock
async def test_scraper_stores_proposal_once_and_isolates_failures(auto_db: Any, clock: Clock) -> None:
    respx.get("https://prices.example.com/al").mock(
        return_value=httpx.Response(200, text="<table><td class=cash>USD 2,610.50</td></table>")
    )
    respx.get("https://prices.example.com/pe").mock(return_value=httpx.Response(403))
    broken = SOURCE.model_copy(update={"code": "pe", "url": "https://prices.example.com/pe"})
    unchecked = SOURCE.model_copy(update={"code": "unchecked", "terms_ok": False})
    async with httpx.AsyncClient() as http:
        scraper = PriceScraper(
            databases=auto_db,
            http=http,
            sources=(SOURCE, broken, unchecked),
            retry=HttpRetry(attempts=1, wait=wait_none()),
            clock=clock,
        )
        assert await scraper.run_all() == {"lme_al": "ok", "pe": "blocked", "unchecked": "blocked"}
        await scraper.run(SOURCE)  # same day again: no duplicate proposal
    assert await sql(auto_db, "SELECT value, currency, observed_for::text FROM price_observations") == [
        (D("2610.5000"), "USD", "2026-11-03")
    ]
    events = await sql(auto_db, "SELECT payload->>'price', payload->>'material_code' FROM outbox_events")
    assert events == [("2610.5000", "AL_INGOT")]


# ---------------------------------------------------------------- ingest + relay
class Downloader:
    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        return b"OGG", "audio/ogg"


def runtime(
    db: Any, published: list[Event], media: FakeMedia, inbound_mail: FakeMedia
) -> jobs.AutomationRuntime:
    return jobs.AutomationRuntime(
        databases=db,
        settings=SETTINGS,
        outbound=None,
        ladder=None,  # type: ignore[arg-type]
        pause_alerts=None,
        followups=None,
        handlers=None,
        scraper=None,  # type: ignore[arg-type]
        whatsapp_media=Downloader(),
        media=media,
        inbound_mail=inbound_mail,
        publish=_collect(published),
    )


def _collect(published: list[Event]) -> Any:
    async def publish(event: Event) -> None:
        published.append(event)

    return publish


async def test_media_ingest_then_relay_in_order(auto_db: Any) -> None:
    (thread,) = await sql(
        auto_db,
        "INSERT INTO threads (channel_account_id, external_key) SELECT id, '+971501234567' "
        "FROM channel_accounts WHERE external_account_id = 'PNID1' RETURNING id",
    )
    (inbound,) = await sql(
        auto_db,
        "INSERT INTO inbound_messages (thread_id, provider, provider_message_id, kind, raw) "
        "VALUES (:t, 'meta_cloud', 'wamid.A', 'audio', "
        '\'{"audio": {"id": "M3", "mime_type": "audio/ogg"}}\') RETURNING id',
        t=thread[0],
    )
    published: list[Event] = []
    media = FakeMedia()
    rt = runtime(auto_db, published, media, FakeMedia())
    await jobs._ingest_media(rt, inbound[0])
    await jobs._ingest_media(rt, inbound[0])  # retried job: announced once
    assert media.objects == {f"whatsapp/{inbound[0]}.ogg": b"OGG"}
    assert await jobs.relay_all(rt) == 1 and await jobs.relay_all(rt) == 0
    assert published[0].payload["media_s3_keys"] == [f"whatsapp/{inbound[0]}.ogg"]


async def test_email_ingest_stores_attachments_and_announces(auto_db: Any) -> None:
    published: list[Event] = []
    media, mail = FakeMedia(), FakeMedia({"inbound/1": RAW_REPLY})
    rt = runtime(auto_db, published, media, mail)
    await jobs._ingest_email(rt, "inbound/1")
    await jobs._ingest_email(rt, "inbound/1")
    assert len(media.objects) == 1 and next(iter(media.objects)).endswith("/0-po.pdf")
    assert await sql(auto_db, "SELECT kind, provider FROM inbound_messages") == [("email", "ses")]
    await jobs.relay_all(rt)
    (event,) = published
    assert event.payload["channel"] == "email" and event.payload["text"].startswith("Re: Your quote Q-1045")
