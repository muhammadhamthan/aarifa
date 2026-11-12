"""ORM for the `agents` database. Mirrors db/ddl/03_agents.sql exactly (the DDL is the source of
truth; tests/test_db.py compares columns). Defaults that the DB computes are `server_default`."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Uuid,
)
from sqlalchemy import (
    text as sql,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from kx.core.db import make_base

SCHEMA: Final = "agents"
if TYPE_CHECKING:  # make_base builds the class at runtime; give mypy a static one with the same shape

    class Base(DeclarativeBase):
        """Static stand-in for mypy."""

else:
    Base = make_base(SCHEMA)

_NOW = sql("now()")
_UUID = sql("gen_random_uuid()")
TS = DateTime(timezone=True)


def _id() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, server_default=_UUID)


class Visitor(Base):
    __tablename__ = "visitors"
    id: Mapped[uuid.UUID] = _id()
    first_seen_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)
    last_seen_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)
    cookie_consent: Mapped[bool] = mapped_column(Boolean, server_default=sql("false"))
    country_code: Mapped[str | None] = mapped_column(String(2))
    language: Mapped[str | None] = mapped_column(Text)
    contact_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    expires_at: Mapped[datetime] = mapped_column(TS, server_default=sql("now() + interval '90 days'"))
    voice_consent_at: Mapped[datetime | None] = mapped_column(TS)  # §6.1 "Enter the factory" pressed


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[uuid.UUID] = _id()
    channel: Mapped[str] = mapped_column(Text)
    langgraph_thread_id: Mapped[str] = mapped_column(Text, unique=True)
    visitor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("visitors.id"))
    contact_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    deal_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    language: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)
    last_activity_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)
    ended_at: Mapped[datetime | None] = mapped_column(TS)
    summary: Mapped[str | None] = mapped_column(Text)
    recording_consent: Mapped[bool] = mapped_column(Boolean, server_default=sql("false"))
    summary_upto_turn: Mapped[int | None] = mapped_column(Integer)  # summary covers turns 1..N


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    turn_no: Mapped[int] = mapped_column(Integer)
    speaker: Mapped[str] = mapped_column(Text)
    ai_role: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    audio_s3_key: Mapped[str | None] = mapped_column(Text)
    stt_confidence: Mapped[float | None] = mapped_column(Float(precision=24))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # agent_runs.id of the run that wrote it


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = _id()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    deal_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    graph: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    trigger_event: Mapped[str] = mapped_column(Text)
    trigger_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    model: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    cached_tokens: Mapped[int] = mapped_column(Integer, server_default=sql("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default=sql("0"))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    langfuse_trace_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    tool: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)


class GuardEvent(Base):
    __tablename__ = "guard_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"))
    guard: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sql("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)


class KnowledgeGap(Base):
    __tablename__ = "knowledge_gaps"
    id: Mapped[uuid.UUID] = _id()
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    question: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=sql("'open'"))
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)


class KbDocument(Base):
    __tablename__ = "kb_documents"
    id: Mapped[uuid.UUID] = _id()
    cms_collection: Mapped[str] = mapped_column(Text)
    cms_id: Mapped[str] = mapped_column(Text)
    cms_version: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, server_default=sql("'en'"))
    url_path: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64))
    approved_at: Mapped[datetime] = mapped_column(TS)
    indexed_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)


class KbChunk(Base):
    __tablename__ = "kb_chunks"
    id: Mapped[uuid.UUID] = _id()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="CASCADE"))
    chunk_no: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default=sql("'{}'::jsonb"))
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    embedding_model: Mapped[str] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True), nullable=True
    )


class NewsItem(Base):
    __tablename__ = "news_items"
    id: Mapped[uuid.UUID] = _id()
    source: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(TS)
    lanes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=sql("ARRAY[]::text[]"))
    summary: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)


class LaneRiskAssessment(Base):
    __tablename__ = "lane_risk_assessments"
    id: Mapped[uuid.UUID] = _id()
    lane_code: Mapped[str] = mapped_column(Text)
    for_date: Mapped[date] = mapped_column(Date)
    score: Mapped[int] = mapped_column(SmallInteger)
    summary: Mapped[str] = mapped_column(Text)
    news_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), server_default=sql("ARRAY[]::uuid[]"))
    model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[uuid.UUID] = _id()
    suite: Mapped[str] = mapped_column(Text)
    git_sha: Mapped[str] = mapped_column(Text)
    passed: Mapped[int] = mapped_column(Integer)
    failed: Mapped[int] = mapped_column(Integer)
    report_s3_key: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(TS, server_default=_NOW)
