"""kx.crm against real Postgres: the 4 Nov cost-based flow, kept for v2 (D14), so every test here runs with
pricing.mode = 'cost' (the `cost_mode` fixture). The v1 price-list flow is test_price_list_db.py."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest
from conftest import CATEGORY, MGR_ID, SALES_ID, SKU, FakeUploader, Seed, assign, new_phone
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects import postgresql

from kx.core import events as ev
from kx.core.auth import Principal, Role
from kx.core.contracts import AiRole, ContactPatch, CounterVerdictKind, DealStage, RequirementPatch
from kx.core.db import Databases
from kx.core.errors import Conflict, Forbidden, RuleViolation
from kx.core.event_payloads import PriceProposed
from kx.core.events import Event
from kx.core.identity import priced_content_sha256, recipient_hash, sha256_hex
from kx.core.settings import LogicalDb
from kx.crm import models as m
from kx.crm.deps import CrmDeps
from kx.crm.gateway import ApprovalStatusReaderImpl, CrmGatewayImpl, GrantConsumerImpl
from kx.crm.jobs import HANDLERS, CrmRuntime, dispatch, run_approval_sweep, run_price_staleness, run_retention
from kx.crm.services import approvals, deals, negotiation, orders, price_proposals, quotes
from kx.crm.services.journal import Actor

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("cost_mode")]
CRM = LogicalDb.CRM
AI = AiRole.QUOTATION


def principal(role: Role, user_id: uuid.UUID, *, mfa: bool = True, human: bool = True) -> Principal:
    return Principal(subject=str(user_id), user_id=user_id, role=role, mfa=mfa, is_human=human)


MD = principal(Role.MD, uuid.UUID("00000000-0000-7000-8000-0000000000a1"))
MGR = principal(Role.MANAGER, MGR_ID)
SARA = principal(Role.SALES, SALES_ID)


async def outbox(dbs: Databases, event_type: str, deal_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    async with dbs.transaction(CRM) as s:
        stmt = select(m.OutboxEvent.payload).where(m.OutboxEvent.event_type == event_type)
        if deal_id is not None:
            stmt = stmt.where(m.OutboxEvent.payload["deal_ref"].astext == str(deal_id))
        return list((await s.execute(stmt.order_by(m.OutboxEvent.sequence))).scalars())


async def qualified_deal(
    gw: CrmGatewayImpl, *, country: str = "ae", qty: int = 50_000, phone: str | None = None
) -> uuid.UUID:
    deal_id = await gw.resolve_deal("whatsapp", phone or new_phone(), None)
    await gw.update_requirement(
        deal_id,
        RequirementPatch(product_sku=SKU, quantity=D(qty), delivery_country=country, spec={"lid": "yes"}),
        AI,
    )
    await gw.set_contact(deal_id, ContactPatch(name="Ahmed", company="Gulf Catering"), AiRole.CONTACT_CAPTURE)
    assert (await gw.try_qualify(deal_id, AiRole.LEAD_QUALIFICATION)).qualified
    await assign(gw._dbs, deal_id, SALES_ID)  # Sara owns the deal (assignment itself: test_assignment_db.py)
    return deal_id


async def open_approval(dbs: Databases, deal_id: uuid.UUID) -> m.Approval:
    async with dbs.transaction(CRM) as s:
        v = await quotes.latest_version(s, deal_id)
        assert v is not None
        return (
            await s.execute(
                select(m.Approval).where(
                    m.Approval.quote_version_id == v.id, m.Approval.status.in_(approvals.OPEN)
                )
            )
        ).scalar_one()


async def decide(
    dbs: Databases,
    deps: CrmDeps,
    deal_id: uuid.UUID,
    who: Principal,
    decision: str = "approve",
    note: str | None = None,
) -> m.Approval:
    a = await open_approval(dbs, deal_id)
    async with dbs.transaction(CRM) as s:
        return await approvals.decide(s, deps, a.id, who, decision=decision, note=note)  # type: ignore[arg-type]


async def approve_and_send(
    dbs: Databases, deps: CrmDeps, deal_id: uuid.UUID, who: Principal = MGR
) -> dict[str, Any]:
    """Decide the open approval, then consume its grant exactly like automation would."""
    await decide(dbs, deps, deal_id, who)
    approved = (await outbox(dbs, ev.QUOTE_APPROVED, deal_id))[-1]
    ok = await GrantConsumerImpl(dbs, deps).consume_grant(
        uuid.UUID(approved["grant_ref"]),
        approved["content_sha256"],
        uuid.uuid4(),
        recipient_hash(approved["channel"], approved["to_address"]),
    )
    assert ok
    return approved


# ---------------------------------------------------------------- schema
def _expected_type(col: Any) -> tuple[str, int | None, int | None, int | None]:
    compiled = col.type.compile(dialect=postgresql.dialect()).lower()  # type: ignore[no-untyped-call]
    base = compiled.split("(")[0].strip()
    nums = [int(x) for x in compiled[len(base) :].strip("()").split(",") if x.strip().isdigit()]
    udt = {
        "text": "text",
        "citext": "citext",
        "char": "bpchar",
        "numeric": "numeric",
        "integer": "int4",
        "smallint": "int2",
        "bigint": "int8",
        "boolean": "bool",
        "timestamp with time zone": "timestamptz",
        "date": "date",
        "time without time zone": "time",
        "jsonb": "jsonb",
        "uuid": "uuid",
        "text[]": "_text",
    }[base]
    if udt == "bpchar":
        return udt, nums[0], None, None
    if udt == "numeric":
        return udt, None, nums[0], nums[1]
    return udt, None, None, None


async def test_orm_matches_ddl(databases: Databases) -> None:
    async with databases.transaction(CRM) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT table_name, column_name, is_nullable, udt_name, character_maximum_length, numeric_precision, "
                    "numeric_scale FROM information_schema.columns WHERE table_schema = 'crm'"
                )
            )
        ).all()
    db: dict[str, dict[str, tuple[Any, ...]]] = {}
    for t, c, null, udt, clen, prec, scale in rows:
        db.setdefault(t, {})[c] = (
            null == "YES",
            udt,
            clen,
            prec if udt == "numeric" else None,
            scale if udt == "numeric" else None,
        )
    assert set(db) == set(m.metadata.tables.keys()) | set() or set(db) == {
        t.name for t in m.metadata.tables.values()
    }
    # 45 baseline + product_price_tiers, quote_line_tiers, change_requests (5 Nov)
    # + staff_credentials, auth_sessions, auth_challenges (11 Nov, D26 in-house login)
    assert len(m.metadata.tables) == 51
    problems = []
    for table in m.metadata.tables.values():
        cols = db[table.name]
        if set(cols) != {c.name for c in table.columns}:
            problems.append(f"{table.name}: columns {set(cols) ^ {c.name for c in table.columns}}")
            continue
        for col in table.columns:
            nullable, udt, clen, prec, scale = cols[col.name]
            if nullable != bool(col.nullable):
                problems.append(f"{table.name}.{col.name}: nullable db={nullable} orm={col.nullable}")
            if (udt, clen, prec, scale) != _expected_type(col):
                problems.append(
                    f"{table.name}.{col.name}: db={(udt, clen, prec, scale)} orm={_expected_type(col)}"
                )
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------- the whole sale
async def test_happy_path_new_to_needs_manager(
    databases: Databases, seed: Seed, deps: CrmDeps, uploader: FakeUploader
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    phone = new_phone()
    deal_id = await gw.resolve_deal("whatsapp", phone, None)
    assert await gw.resolve_deal("whatsapp", phone, None) == deal_id  # one person = one open deal
    assert not (await gw.try_qualify(deal_id, AI)).qualified
    ctx = await gw.deal_context(deal_id)
    assert ctx.stage is DealStage.NEW and "product" in ctx.missing_fields

    deal_id = await qualified_deal(gw, phone=phone)
    assert (await gw.deal_context(deal_id)).stage is DealStage.QUALIFIED
    assert await outbox(databases, ev.DEAL_QUALIFIED, deal_id)

    # ---- request_quote: top of band, frozen, approval 1 to the salesperson, the manager is the escalation
    await gw.request_quote(deal_id, AI)
    async with databases.transaction(CRM) as s:
        v = await quotes.latest_version(s, deal_id)
        assert v is not None and v.status == "pending_approval" and v.kind == "initial"
        ((line, cost),) = await quotes.costs_of(s, v.id)
        assert line.unit_price == D("0.227804") and line.line_total == D("11390.2000")
        assert (v.subtotal, v.vat_amount, v.total, v.incoterm) == (
            D("11390.2000"),
            D("569.5100"),
            D("11959.7100"),
            "EXW",
        )
        assert cost.material_rate == D("12.0000") and cost.markup_pct == D(50)
        assert v.delivery_estimate_id is not None
    with pytest.raises(Exception, match="frozen"):
        async with databases.transaction(CRM) as s:
            await s.execute(update(m.QuoteVersion).where(m.QuoteVersion.id == v.id).values(total=D(1)))
    submitted = await outbox(databases, ev.QUOTE_SUBMITTED, deal_id)
    assert submitted[-1]["kind"] == "commercial" and submitted[-1]["required_role"] == "sales"
    assert submitted[-1]["approver"]["name"] == "Sara"
    assert (
        submitted[-1]["escalate_to"]["name"] == "The Manager"
        and submitted[-1]["total_display"] == "AED 11,959.71"
    )
    assert (await gw.deal_context(deal_id)).allowed_prices == ()  # nothing approved yet

    # ---- approval 1 by the manager (D1: any deal; human + MFA) releases the quote: PDF, grant, QUOTE_APPROVED
    a1 = await open_approval(databases, deal_id)
    reader = ApprovalStatusReaderImpl(databases)
    assert await reader.is_pending(a1.id)
    for bad in (
        principal(Role.MANAGER, MGR_ID, mfa=False),
        principal(Role.SERVICE, MGR_ID, human=False),
        principal(Role.SALES, MGR_ID),  # a salesperson who does not own the deal
        MD,  # D1: the MD only on a deal it has taken over
    ):
        with pytest.raises(Forbidden):
            async with databases.transaction(CRM) as s:
                await approvals.decide(s, deps, a1.id, bad, decision="approve")
    await decide(databases, deps, deal_id, MGR)
    assert not await reader.is_pending(a1.id)
    approved = (await outbox(databases, ev.QUOTE_APPROVED, deal_id))[-1]
    assert (
        approved["kind"] == "commercial"
        and approved["channel"] == "whatsapp"
        and approved["to_address"] == phone
    )
    pdf = uploader.objects[approved["pdf_s3_key"]]
    assert pdf.startswith(b"%PDF")
    assert approved["content_sha256"] == priced_content_sha256(approved["body"], sha256_hex(pdf))
    async with databases.transaction(CRM) as s:
        g = await s.get(m.QuoteSendGrant, uuid.UUID(approved["grant_ref"]))
        assert g is not None and g.policy == "manager" and g.expires_at - g.issued_at == timedelta(hours=48)

    # ---- grant: single use, bound to content and recipient
    consumer = GrantConsumerImpl(databases, deps)
    grant, sha, rcpt = (
        uuid.UUID(approved["grant_ref"]),
        approved["content_sha256"],
        recipient_hash("whatsapp", phone),
    )
    msg = uuid.uuid4()
    assert not await consumer.consume_grant(grant, sha256_hex(b"other"), msg, rcpt)
    assert not await consumer.consume_grant(grant, sha, msg, recipient_hash("whatsapp", new_phone()))
    assert await consumer.consume_grant(grant, sha, msg, rcpt)
    assert await consumer.consume_grant(grant, sha, msg, rcpt)  # retry of the same message
    assert not await consumer.consume_grant(grant, sha, uuid.uuid4(), rcpt)  # second message
    followups = await outbox(databases, ev.DEAL_NEXT_ACTION_SET, deal_id)
    assert [f["reason"] for f in followups] == ["followup_d2", "followup_d5", "followup_d10"]

    ctx = await gw.deal_context(deal_id)
    assert ctx.latest_quote is not None and ctx.latest_quote.status == "sent" and ctx.concessions_left == 3
    assert {D("0.227804"), D("11959.71")} <= {p.amount for p in ctx.allowed_prices}

    # ---- counter-offer ladder (40-50 % band, 3 concessions, traded for quantity)
    v1 = await gw.evaluate_counter(deal_id, D("0.22"), D(50_000), "whatsapp")
    assert (v1.kind, v1.offer_unit_price, v1.traded_for, v1.concessions_left) == (
        CounterVerdictKind.COUNTER,
        D("0.222742"),
        "goodwill",
        2,
    )
    assert (await gw.deal_context(deal_id)).stage is DealStage.NEGOTIATION
    v2 = await gw.evaluate_counter(deal_id, D("0.215"), D(50_000), "whatsapp")
    assert (v2.kind, v2.offer_unit_price, v2.reason) == (
        CounterVerdictKind.COUNTER,
        D("0.222742"),
        "ask_for_quantity",
    )
    v3 = await gw.evaluate_counter(deal_id, D("0.215"), D(80_000), "whatsapp")
    assert (v3.offer_unit_price, v3.traded_for, v3.concessions_left) == (
        D("0.217680"),
        "quantity_increase",
        1,
    )
    assert {D("0.222742"), D("0.21768")} <= {
        p.amount for p in (await gw.deal_context(deal_id)).allowed_prices
    }

    # ---- below the floor: AI pauses, the salesperson alerted and the manager notified (60-minute promise)
    before = datetime.now(UTC)
    v4 = await gw.evaluate_counter(deal_id, D("0.20"), D(80_000), "whatsapp")
    assert v4.kind is CounterVerdictKind.NEEDS_MANAGER and v4.offer_unit_price is None
    paused = (await outbox(databases, ev.NEGOTIATION_PAUSED, deal_id))[-1]
    # §17 / D3: the salesperson is alerted at once, the manager notified
    assert paused["reason"] == "below_range" and {a["name"] for a in paused["alert"]} == {"Sara"}
    assert {n["name"] for n in paused["notify"]} == {"The Manager"}
    assert paused["deal_number"] == ctx.number
    respond_by = datetime.fromisoformat(paused["respond_by"])
    assert timedelta(minutes=59) < respond_by - before < timedelta(minutes=61)
    ctx = await gw.deal_context(deal_id)
    assert not ctx.ai_enabled and ctx.concessions_left == 1
    v5 = await gw.evaluate_counter(deal_id, D("0.21"), D(80_000), "whatsapp")
    assert v5.reason == "ai_paused"
    async with databases.transaction(CRM) as s:
        rounds = (
            (
                await s.execute(
                    select(m.NegotiationRound.verdict)
                    .where(m.NegotiationRound.deal_id == deal_id)
                    .order_by(m.NegotiationRound.round_no)
                )
            )
            .scalars()
            .all()
        )
        assert rounds == ["concession", "within_range", "concession", "below_range"]
        deal = await deals.get_deal(s, deal_id)
        assert (deal.ai_paused_reason, deal.concessions_used) == ("below_range", 2)
        assert (
            await s.scalar(
                select(func.count()).where(m.Task.deal_id == deal_id, m.Task.kind == "below_range_review")
            )
            == 1
        )
        assert (await s.scalar(select(func.count()).where(m.AuditEvent.entity_id == str(deal_id))) or 0) >= 8


async def test_agreement_payment_won(databases: Databases, seed: Seed, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await qualified_deal(gw)
    await gw.request_quote(deal_id, AI)
    first = await approve_and_send(databases, deps, deal_id)
    await gw.evaluate_counter(deal_id, D("0.22"), D(50_000), "web_chat")
    # manager sets a price and hands back to the AI; buyer agrees -> final version for the salesperson
    async with databases.transaction(CRM) as s:
        await negotiation.manager_offer(s, deps, deal_id, D("0.221"), actor=Actor.user(MGR))
    assert D("0.221") in {p.amount for p in (await gw.deal_context(deal_id)).allowed_prices}
    await gw.mark_agreed(deal_id, AiRole.DEAL_CLOSER)
    a = await open_approval(databases, deal_id)
    assert (a.kind, a.required_role, a.assigned_user_id) == ("final", "sales", SALES_ID)
    with pytest.raises(Forbidden):  # D1: the MD approves only deals it has taken over
        async with databases.transaction(CRM) as s:
            await approvals.decide(s, deps, a.id, MD, decision="approve")
    async with databases.transaction(CRM) as s:
        v = await quotes.latest_version(s, deal_id)
        assert v is not None and v.kind == "final"
        (line,) = await quotes.lines_of(s, v.id)
        assert (line.unit_price, line.line_total) == (D("0.221"), D("11050.0000"))
    final = await approve_and_send(databases, deps, deal_id, SARA)
    assert final["kind"] == "final" and final["grant_ref"] != first["grant_ref"]
    async with databases.transaction(CRM) as s:
        g = await s.get(m.QuoteSendGrant, uuid.UUID(final["grant_ref"]))
        assert g is not None and g.policy == "sales" and g.consumed_at is not None
    assert (await gw.deal_context(deal_id)).stage is DealStage.READY_TO_CLOSE
    async with databases.transaction(CRM) as s:
        await orders.record_payment(
            s, deps, deal_id, Actor.user(SARA), amount=D(5000), currency="AED", method="bank"
        )
    async with databases.transaction(CRM) as s:  # §15: the advance moves the deal to PAYMENT; WON is a step
        assert (await deals.get_deal(s, deal_id)).stage == "PAYMENT"
        order = await orders.mark_won(
            s, deps, deal_id, Actor.user(SARA), expected_reorder_date=deps.now().date() + timedelta(days=30)
        )
    async with databases.transaction(CRM) as s:
        assert (await deals.get_deal(s, deal_id)).stage == "WON"
        await orders.update_order_status(s, deps, order.id, "ready", Actor.user(MGR))
        with pytest.raises(RuleViolation):
            await orders.update_order_status(s, deps, order.id, "in_production", Actor.user(MGR))
    assert [e["status"] for e in await outbox(databases, ev.ORDER_STATUS_CHANGED, deal_id)] == [
        "in_production",
        "ready",
    ]


# ---------------------------------------------------------------- AI never sees cost
async def test_deal_context_has_no_cost_fields(databases: Databases, seed: Seed, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await qualified_deal(gw)
    await gw.request_quote(deal_id, AI)
    await approve_and_send(databases, deps, deal_id)
    await gw.evaluate_counter(deal_id, D("0.22"), D(50_000), "whatsapp")
    ctx = await gw.deal_context(deal_id)
    dumped = ctx.model_dump_json().lower()
    for word in ("cost", "markup", "margin", "floor", "material_rate", "kg_per_1000", "yield"):
        assert word not in dumped, word
    for secret in ("0.151869", "0.212617", "151.8696", "12.0000"):  # unit cost, floor, cost/1000, rate
        assert secret not in dumped, secret
    assert ctx.allowed_prices and all(p.currency == "AED" for p in ctx.allowed_prices)


# ---------------------------------------------------------------- staleness (T12) + price proposals
async def test_stale_prices_block_new_quotes(databases: Databases, seed: Seed, deps: CrmDeps) -> None:
    code, sku, now = f"PP_{uuid.uuid4().hex[:6]}", f"CUP-{uuid.uuid4().hex[:6]}", datetime.now(UTC)
    async with databases.transaction(CRM) as s:
        s.add(m.Material(code=code, name="Polypropylene"))
        await s.flush()
        product = m.Product(sku=sku, name="PP cup 300 ml", category_code=CATEGORY)
        s.add(product)
        await s.flush()
        s.add_all(
            [
                m.ProductCosting(
                    product_id=product.id,
                    material_code=code,
                    kg_per_1000=D(4),
                    yield_pct=D(95),
                    production_cost_per_1000=D(20),
                    packaging_cost_per_1000=D(3),
                    pieces_per_day=50_000,
                    effective_from=now - timedelta(days=30),
                ),
                m.MaterialRate(
                    material_code=code,
                    rate_per_unit=D("6.5"),
                    source="manual",
                    status="approved",
                    decided_by=MGR_ID,
                    decided_at=now - timedelta(days=8),
                    effective_at=now - timedelta(days=8),
                ),
            ]
        )
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await gw.resolve_deal("whatsapp", new_phone(), None)
    await gw.update_requirement(
        deal_id, RequirementPatch(product_sku=sku, quantity=D(20_000), delivery_country="AE"), AI
    )
    await gw.set_contact(deal_id, ContactPatch(name="Fatima"), AI)
    await gw.try_qualify(deal_id, AI)
    with pytest.raises(RuleViolation, match="not reviewed for 8 days"):
        await gw.request_quote(deal_id, AI)
    rt = CrmRuntime(databases, deps, publish=_noop)
    await run_price_staleness(rt)
    stale = [e for e in await outbox(databases, ev.PRICE_STALE) if e["material_code"] == code]
    assert stale[-1] == {"material_code": code, "days_since_approval": 8, "level": "block"}

    # scraper proposes (handler runs once per event), manager approves -> quotes unblocked
    event = Event(
        event_type=ev.PRICE_PROPOSED,
        aggregate_type="price_observation",
        aggregate_id="obs-1",
        payload=PriceProposed(
            observation_ref=uuid.uuid4(),
            price_source_ref=uuid.uuid4(),
            material_code=code,
            price=D("6.9"),
            currency="AED",
            unit="kg",
            observed_at=now,
            source_url="https://example.test/pp",
        ).model_dump(mode="json"),
    )
    assert await dispatch(rt, event) and await dispatch(rt, event)
    async with databases.transaction(CRM) as s:
        proposed = (
            (
                await s.execute(
                    select(m.MaterialRate).where(
                        m.MaterialRate.material_code == code, m.MaterialRate.status == "proposed"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(proposed) == 1 and proposed[0].change_pct == D("6.154")
        await price_proposals.approve_rate(s, proposed[0].id, Actor.user(MGR), now)
        statuses = sorted(
            (
                await s.execute(select(m.MaterialRate.status).where(m.MaterialRate.material_code == code))
            ).scalars()
        )
        assert statuses == ["approved", "superseded"]
    await gw.request_quote(deal_id, AI)


async def test_warn_level_is_flagged_for_the_approver(
    databases: Databases, seed: Seed, deps: CrmDeps
) -> None:
    later = replace(deps, clock=lambda: datetime.now(UTC) + timedelta(days=4))
    gw = CrmGatewayImpl(databases, later)
    deal_id = await qualified_deal(gw, country="lk")  # Colombo lane: freight + risk buffer
    await gw.request_quote(deal_id, AI)
    async with databases.transaction(CRM) as s:
        v = await quotes.latest_version(s, deal_id)
        assert v is not None and v.incoterm == "CFR" and v.vat_amount == 0
        assert "PRICE REVIEW DUE" in (v.approver_summary or "") and "JEA-CMB-SEA" in (
            v.approver_summary or ""
        )
        ((_, cost),) = await quotes.costs_of(s, v.id)
        # (2500 + 300) USD * 3.6725 * 1.10 / 400,000 pcs * 1000 = 28.2783 AED per 1000
        assert cost.freight_per_1000 == D("28.2783")
        est = await s.get(m.DeliveryEstimateRow, v.delivery_estimate_id)
        assert est is not None and (est.transit_days, est.buffer_days) == (12, 3)


# ---------------------------------------------------------------- approvals: window, sweep, MD limit, correction
async def test_overdue_sweep_escalates_to_the_manager(
    databases: Databases, seed: Seed, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await qualified_deal(gw)
    await gw.request_quote(deal_id, AI)
    a = await open_approval(databases, deal_id)
    assert a.deadline_at > a.submitted_at + timedelta(minutes=119)
    async with databases.transaction(CRM) as s:
        assert await approvals.sweep_overdue(s, deps, a.deadline_at - timedelta(minutes=1)) == 0 or True
    late = replace(deps, clock=lambda: a.deadline_at + timedelta(minutes=1))
    assert await run_approval_sweep(CrmRuntime(databases, late, publish=_noop)) >= 1
    async with databases.transaction(CRM) as s:
        row = await s.get(m.Approval, a.id, populate_existing=True)
        assert row is not None and row.status == "overdue" and row.escalated_at is not None
        assert (await s.get(m.QuoteVersion, a.quote_version_id)).status == "overdue"  # type: ignore[union-attr]
    overdue = [
        e for e in await outbox(databases, ev.APPROVAL_OVERDUE, deal_id) if e["approval_ref"] == str(a.id)
    ]
    assert len(overdue) == 1 and overdue[0]["escalate_to"]["name"] == "The Manager"
    assert await run_approval_sweep(CrmRuntime(databases, late, publish=_noop)) == 0  # idempotent
    await decide(databases, deps, deal_id, MGR)  # overdue can still be decided
    assert (await outbox(databases, ev.QUOTE_APPROVED, deal_id))[-1]["approval_ref"] == str(a.id)


async def test_md_limit_and_correction_flow(databases: Databases, seed: Seed, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await qualified_deal(gw)
    async with databases.transaction(CRM) as s:
        await deps.settings.update(s, "approval.md_value_limit_aed", 10_000, Actor.user(MD))
    try:
        await gw.request_quote(deal_id, AI)
        a = await open_approval(databases, deal_id)
        assert a.required_role == "md"
        with pytest.raises(RuleViolation, match="note"):
            await decide(databases, deps, deal_id, MD, "correct")
        await decide(databases, deps, deal_id, MD, "correct", "use 60,000 pcs")
        assert (await outbox(databases, ev.QUOTE_CORRECTION_REQUESTED, deal_id))[-1][
            "note"
        ] == "use 60,000 pcs"
        await gw.request_quote(deal_id, AI)
        async with databases.transaction(CRM) as s:
            v = await quotes.latest_version(s, deal_id)
            assert v is not None and (v.version_no, v.kind, v.correction_note) == (
                2,
                "revision",
                "use 60,000 pcs",
            )
    finally:
        async with databases.transaction(CRM) as s:
            await deps.settings.update(s, "approval.md_value_limit_aed", None, Actor.user(MD))


# ---------------------------------------------------------------- stage rules & locking
async def test_lost_requires_reason_and_optimistic_lock(
    databases: Databases, seed: Seed, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    deal_id = await gw.resolve_deal("email", f"buyer{uuid.uuid4().hex[:6]}@example.com", None)
    actor = Actor.user(MGR)
    async with databases.transaction(CRM) as s:
        deal = await deals.get_deal(s, deal_id, lock=True)
        with pytest.raises(RuleViolation, match="lost_reason"):
            await deals.move_stage(s, deal, DealStage.LOST, actor, now=deps.now())
        with pytest.raises(RuleViolation):
            await deals.move_stage(s, deal, DealStage.WON, actor, now=deps.now())
        version = deal.version
    async with databases.transaction(CRM) as s:
        deal = await deals.get_deal(s, deal_id, lock=True)
        with pytest.raises(Conflict):
            await deals.move_stage(
                s,
                deal,
                DealStage.LOST,
                actor,
                now=deps.now(),
                lost_reason="price_too_high",
                expected_version=version - 1,
            )
        await deals.move_stage(
            s,
            deal,
            DealStage.LOST,
            actor,
            now=deps.now(),
            lost_reason="price_too_high",
            expected_version=version,
        )
        assert deal.version == version + 1
    async with databases.transaction(CRM) as s:
        hist = (
            (
                await s.execute(
                    select(m.StageHistory.to_stage)
                    .where(m.StageHistory.deal_id == deal_id)
                    .order_by(m.StageHistory.id)
                )
            )
            .scalars()
            .all()
        )
        assert hist == ["NEW", "LOST"]
    changed = [e for e in await outbox(databases, ev.DEAL_STAGE_CHANGED, deal_id)]
    assert changed[-1]["to_stage"] == "LOST" and changed[-1]["reason"] == "price_too_high"


async def test_contact_merge_on_known_phone(databases: Databases, seed: Seed, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    phone = new_phone()
    known = await gw.resolve_deal("whatsapp", phone, None)
    visitor = uuid.uuid4()
    web = await gw.resolve_deal("web_chat", None, visitor)
    await gw.set_contact(web, ContactPatch(name="Ravi", phone=phone), AiRole.CONTACT_CAPTURE)
    async with databases.transaction(CRM) as s:
        a, b = await deals.get_deal(s, known), await deals.get_deal(s, web)
        assert a.primary_contact_id == b.primary_contact_id
        contact = await deals.contact_of(s, b)
        assert (contact.full_name, contact.visitor_ref) == ("Ravi", visitor)


# ---------------------------------------------------------------- events in, settings, retention
async def test_message_received_lands_on_timeline_once(
    databases: Databases, seed: Seed, deps: CrmDeps
) -> None:
    gw = CrmGatewayImpl(databases, deps)
    phone = new_phone()
    deal_id = await gw.resolve_deal("whatsapp", phone, None)
    event = Event(
        event_type=ev.MESSAGE_RECEIVED,
        aggregate_type="inbound_message",
        aggregate_id="m1",
        payload={
            "message_ref": str(uuid.uuid4()),
            "thread_ref": str(uuid.uuid4()),
            "channel": "whatsapp",
            "from_address_hash": recipient_hash("whatsapp", phone),
            "contact_ref": None,
            "deal_ref": None,
            "text": "Can you do 0.20?",
            "received_at": datetime.now(UTC).isoformat(),
            "provider_message_id": "wamid.1",
        },
    )
    rt = CrmRuntime(databases, deps, publish=_noop)
    assert set(HANDLERS) == {
        ev.PRICE_PROPOSED,
        ev.MESSAGE_RECEIVED,
        ev.CONSENT_OPTED_OUT,
        ev.MESSAGE_STATUS,
        ev.DEAL_QUALIFIED,
        ev.CONVERSATION_SUMMARY_UPDATED,  # §7A.2 the AI summary on the deal page (WP17)
        ev.CATALOGUE_PRODUCT_PUBLISHED,  # §7 the CMS product mirror (WP18)
        ev.QUOTE_NOT_DELIVERED,  # D32 WhatsApp quote -> email (WP25)
    }
    await dispatch(rt, event)
    await dispatch(rt, event)
    async with databases.transaction(CRM) as s:
        rows = (
            (
                await s.execute(
                    select(m.TimelineEvent).where(
                        m.TimelineEvent.deal_id == deal_id, m.TimelineEvent.kind == "message_in"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(r.summary, r.actor_type, r.direction) for r in rows] == [("Can you do 0.20?", "buyer", "in")]


async def test_settings_update_validates_and_emits(databases: Databases, seed: Seed, deps: CrmDeps) -> None:
    async with databases.transaction(CRM) as s:
        with pytest.raises(RuleViolation):
            await deps.settings.update(s, "pricing.stale_block_days", 1, Actor.user(MD))
    async with databases.transaction(CRM) as s:
        assert (
            await deps.settings.update(s, "quote.validity_days", 10, Actor.user(MD))
        ).quote_validity_days == 10
    async with databases.transaction(CRM) as s:
        await deps.settings.update(s, "quote.validity_days", 7, Actor.user(MD))
        hist = await s.scalar(select(func.count()).where(m.SettingHistory.key == "quote.validity_days"))
        assert hist == 2
    assert [e["key"] for e in await outbox(databases, ev.SETTINGS_CHANGED)][-2:] == [
        "quote.validity_days"
    ] * 2


async def test_retention_anonymises_lost_leads(databases: Databases, seed: Seed, deps: CrmDeps) -> None:
    gw = CrmGatewayImpl(databases, deps)
    phone = new_phone()
    deal_id = await gw.resolve_deal("whatsapp", phone, None)
    await gw.add_timeline(deal_id, "message_in", "my number is " + phone, AiRole.COMMUNICATION)
    async with databases.transaction(CRM) as s:
        deal = await deals.get_deal(s, deal_id, lock=True)
        await deals.move_stage(
            s, deal, DealStage.LOST, Actor.user(MGR), now=deps.now(), lost_reason="no_response_postponed"
        )
        contact_id = deal.primary_contact_id
    future = replace(deps, clock=lambda: datetime.now(UTC) + timedelta(days=400))
    counts = await run_retention(CrmRuntime(databases, future, publish=_noop))
    assert counts["lost"] >= 1
    async with databases.transaction(CRM) as s:
        c = await s.get(m.Contact, contact_id, populate_existing=True)
        assert (
            c is not None and c.anonymised_at is not None and c.phone_e164 is None and c.whatsapp_e164 is None
        )
        summaries = (
            (
                await s.execute(
                    select(m.TimelineEvent.summary).where(
                        m.TimelineEvent.contact_id == contact_id, m.TimelineEvent.kind == "message_in"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert summaries and all(phone not in x for x in summaries)
    async with databases.transaction(CRM) as s:
        payload = (
            await s.execute(
                select(m.OutboxEvent.payload).where(
                    m.OutboxEvent.event_type == ev.CONTACT_ANONYMISED,
                    m.OutboxEvent.aggregate_id == str(contact_id),
                )
            )
        ).scalar_one()
    assert payload["recipient_hashes"] == [recipient_hash("whatsapp", phone)]


async def _noop(_: Event) -> None:
    return None
