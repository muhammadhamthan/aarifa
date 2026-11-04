from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from kx.agents.knowledge import KbSource, KnowledgeBase, KnowledgeIndexer
from kx.agents.memory import open_checkpointer
from kx.agents.models import ModelRouter
from kx.agents.orm import Base
from kx.agents.service import AgentService, InboundTurn
from kx.agents.testing import FakeCrmGateway, HashEmbedder, ScriptedChatModel, make_deal
from kx.core.contracts import DealStage
from kx.core.settings import LogicalDb

pytestmark = pytest.mark.db


async def _scalar(dbs: Any, sql: str, **params: Any) -> Any:
    async with dbs.transaction(LogicalDb.AGENTS) as s:
        return (await s.execute(text(sql), params)).scalar()


async def test_orm_matches_ddl(databases: Any) -> None:
    async with databases.transaction(LogicalDb.AGENTS) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'agents'"
                )
            )
        ).all()
    ddl = {(r.table_name, r.column_name): r.is_nullable == "YES" for r in rows}
    for table in Base.metadata.tables.values():
        for col in table.columns:
            assert (table.name, col.name) in ddl, f"{table.name}.{col.name} not in DDL"
            assert ddl[(table.name, col.name)] == bool(col.nullable) or col.primary_key, (
                f"{table.name}.{col.name}"
            )
        assert {c for t, c in ddl if t == table.name} == {c.name for c in table.columns}, table.name


async def test_one_run_per_trigger_event(databases: Any) -> None:
    deal = make_deal(DealStage.NEW, missing=["quantity"])
    model = ScriptedChatModel(script=["Hello! I'm the AI assistant. How many pieces do you need?"])
    svc = AgentService(databases=databases, gateway=FakeCrmGateway(deal), models=ModelRouter.static(model))
    sent: list[str] = []

    async def deliver(body: str, language: str) -> None:
        sent.append(body)

    turn = InboundTurn(
        channel="whatsapp",
        text="Hi, I need PET bottles",
        deal_id=deal.deal_id,
        thread_key=f"whatsapp:{uuid.uuid7()}",
        trigger_event="message.received.v1",
        trigger_event_id=uuid.uuid7(),
    )
    first = await svc.handle_turn(turn, deliver)
    second = await svc.handle_turn(turn, deliver)
    assert first.status == "ok" and second.status == "duplicate" and len(sent) == 1
    eid = turn.trigger_event_id
    assert await _scalar(databases, "SELECT count(*) FROM agent_runs WHERE trigger_event_id = :e", e=eid) == 1
    assert (
        await _scalar(databases, "SELECT input_tokens FROM agent_runs WHERE trigger_event_id = :e", e=eid)
        == 10
    )
    assert (
        await _scalar(
            databases,
            "SELECT count(*) FROM turns t JOIN conversations c ON c.id = t.conversation_id "
            "WHERE c.deal_ref = :d",
            d=deal.deal_id,
        )
        == 2
    )
    assert (
        await _scalar(
            databases,
            "SELECT count(*) FROM outbox_events WHERE event_type = 'agent.run_completed.v1' "
            "AND payload->>'deal_ref' = :d",
            d=str(deal.deal_id),
        )
        == 1
    )


class _Docs:
    def __init__(self, docs: dict[str, KbSource]) -> None:
        self.docs = docs

    async def fetch(self, collection: str, cms_id: str, language: str) -> KbSource | None:
        return self.docs.get(cms_id)


async def test_rrf_hybrid_search_ranks_and_flags_gaps(databases: Any) -> None:
    now = datetime.now(UTC)
    tag = uuid.uuid7().hex[:8]
    docs = {
        f"pet-{tag}": KbSource(
            "products",
            f"pet-{tag}",
            "1",
            "en",
            "PET bottles",
            f"PET bottles 500 ml crystal clear for water and juice {tag}. Food grade.",
            now,
        ),
        f"hdpe-{tag}": KbSource(
            "products",
            f"hdpe-{tag}",
            "1",
            "en",
            "HDPE jerrycans",
            "HDPE jerrycans 20 litre for chemicals and oil. UN certified.",
            now,
        ),
        f"iso-{tag}": KbSource(
            "certifications",
            f"iso-{tag}",
            "1",
            "en",
            "ISO 22000",
            "Our factory is ISO 22000 and FSSC certified for food packaging.",
            now,
        ),
    }
    embedder = HashEmbedder()
    indexer = KnowledgeIndexer(databases, embedder, _Docs(docs))
    for cms_id, d in docs.items():
        assert await indexer.reindex(d.collection, cms_id, "en") == 1
    assert await indexer.reindex("products", f"pet-{tag}", "en") == 0  # unchanged content is skipped
    kb = KnowledgeBase(databases, embedder)
    result = await kb.search(f"clear PET bottles for juice {tag}", "en")
    assert result.hits[0].title == "PET bottles" and not result.weak
    assert result.hits[0].score > result.hits[-1].score
    assert (await kb.search("zzqx unrelated wording", "en")).weak


async def test_quotation_waits_durably_and_resumes(databases: Any, pg_databases: dict[str, str]) -> None:
    deal = make_deal(DealStage.QUALIFIED)
    gw = FakeCrmGateway(deal)
    model = ScriptedChatModel(
        script=["Your quotation is being prepared.", "Our team will contact you shortly."]
    )
    sent: list[str] = []

    async def deliver(body: str, language: str) -> None:
        sent.append(body)

    async with open_checkpointer(pg_databases["agents"]) as saver:
        svc = AgentService(
            databases=databases, gateway=gw, models=ModelRouter.static(model), checkpointer=saver
        )
        turn = InboundTurn(
            channel="whatsapp",
            text="please send the price",
            deal_id=deal.deal_id,
            thread_key=f"whatsapp:{uuid.uuid7()}",
            trigger_event="message.received.v1",
            trigger_event_id=uuid.uuid7(),
        )
        first = await svc.handle_turn(turn, deliver)
        assert first.route.graph and first.route.graph.value == "quotation"
        assert ("request_quote", None) in gw.calls
    async with open_checkpointer(pg_databases["agents"], setup=False) as saver2:  # fresh process
        svc2 = AgentService(
            databases=databases, gateway=gw, models=ModelRouter.static(model), checkpointer=saver2
        )
        done = await svc2.resume_quotation(
            deal.deal_id,
            {"kind": "rejected", "note": "x"},
            trigger_event="quote.rejected.v1",
            trigger_event_id=uuid.uuid7(),
            deliver=deliver,
        )
    assert done is not None and done.status == "handoff"
    assert sent == ["Your quotation is being prepared.", "Our team will contact you shortly."]
