"""Conversation memory.

Short-term memory is the `agents.turns` table itself (the transcript we must keep anyway), replayed
as a window of recent turns plus a rolling summary of older ones. Keeping it out of LangGraph
checkpoints means one place to read, retain and anonymise (T20). Checkpoints are used only where a
graph must survive a long wait: the quotation approval interrupt.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final, Literal

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from kx.agents.models import ModelSet
from kx.agents.orm import Conversation, Turn, Visitor
from kx.core.contracts import Channel
from kx.core.db import Databases
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)
Speaker = Literal["buyer", "ai", "human", "system"]
_COVERED: Final = re.compile(r"^\[turns 1-(\d+)\] ")  # DDL has no summary watermark column (see report)


@dataclass(frozen=True, slots=True)
class History:
    messages: list[BaseMessage]
    summary: str | None
    turn_count: int


def _split_summary(raw: str | None) -> tuple[int, str | None]:
    if not raw:
        return 0, None
    m = _COVERED.match(raw)
    return (int(m.group(1)), raw[m.end() :]) if m else (0, raw)


class TurnStore:
    def __init__(self, window: int = 12, step: int = 8) -> None:
        self.window = window  # recent turns replayed verbatim
        self.step = step  # summarise in batches, so the summary model runs every `step` turns

    @staticmethod
    async def open_conversation(
        s: AsyncSession,
        *,
        thread_key: str,
        channel: Channel,
        deal_ref: uuid.UUID,
        contact_ref: uuid.UUID | None,
        language: str,
    ) -> uuid.UUID:
        stmt = insert(Conversation).values(
            langgraph_thread_id=thread_key,
            channel=channel,
            deal_ref=deal_ref,
            contact_ref=contact_ref,
            language=language,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["langgraph_thread_id"],
            set_={"last_activity_at": func.now(), "deal_ref": deal_ref, "language": language},
        )
        return (await s.execute(stmt.returning(Conversation.id))).scalar_one()

    @staticmethod
    async def append(
        s: AsyncSession,
        conversation_id: uuid.UUID,
        speaker: Speaker,
        text: str,
        *,
        role: str | None = None,
        language: str | None = None,
    ) -> int:
        """Next turn_no under a row lock on the conversation (concurrent workers cannot collide)."""
        await s.execute(select(Conversation.id).where(Conversation.id == conversation_id).with_for_update())
        last = (
            await s.execute(
                select(func.coalesce(func.max(Turn.turn_no), 0)).where(
                    Turn.conversation_id == conversation_id
                )
            )
        ).scalar_one()
        s.add(
            Turn(
                conversation_id=conversation_id,
                turn_no=last + 1,
                speaker=speaker,
                text=text,
                ai_role=role,
                language=language,
            )
        )
        await s.flush()
        return int(last) + 1

    async def history(self, s: AsyncSession, conversation_id: uuid.UUID) -> History:
        raw = (
            await s.execute(select(Conversation.summary).where(Conversation.id == conversation_id))
        ).scalar()
        covered, summary = _split_summary(raw)
        rows = (
            await s.execute(
                select(Turn.speaker, Turn.text, Turn.turn_no)
                .where(Turn.conversation_id == conversation_id, Turn.turn_no > covered)
                .order_by(Turn.turn_no.desc())
                .limit(self.window + 2 * self.step)
            )
        ).all()
        count = rows[0].turn_no if rows else covered
        messages: list[BaseMessage] = []
        for speaker, text, _ in reversed(rows):
            if speaker == "buyer":
                messages.append(HumanMessage(text))
            elif speaker in ("ai", "human"):
                messages.append(AIMessage(text, name="staff" if speaker == "human" else None))
        return History(messages, summary, count)

    async def summarise(self, databases: Databases, models: ModelSet, conversation_id: uuid.UUID) -> bool:
        """Fold turns older than the window into the rolling summary (one fast-model call per `step`)."""
        async with databases.transaction(LogicalDb.AGENTS) as s:
            raw = (
                await s.execute(select(Conversation.summary).where(Conversation.id == conversation_id))
            ).scalar()
            covered, summary = _split_summary(raw)
            count = (
                await s.execute(
                    select(func.coalesce(func.max(Turn.turn_no), 0)).where(
                        Turn.conversation_id == conversation_id
                    )
                )
            ).scalar_one()
            target = ((count - self.window) // self.step) * self.step
            if target <= covered:
                return False
            rows = (
                await s.execute(
                    select(Turn.speaker, Turn.text)
                    .where(
                        Turn.conversation_id == conversation_id,
                        Turn.turn_no > covered,
                        Turn.turn_no <= target,
                    )
                    .order_by(Turn.turn_no)
                )
            ).all()
        transcript = "\n".join(f"{sp}: {tx}" for sp, tx in rows)
        msg = await models.runnable().ainvoke(
            [
                SystemMessage(
                    "Update the running summary of a B2B sales chat in 5 lines: product, quantity, "
                    "country, contact details given (no numbers of phones/emails), open questions, "
                    "commitments made. Never invent prices."
                ),
                HumanMessage(f"Summary so far: {summary or '-'}\n\nNew turns:\n{transcript}"),
            ]
        )
        async with databases.transaction(LogicalDb.AGENTS) as s:
            await s.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(summary=f"[turns 1-{target}] {msg.text}")
            )
        return True

    @staticmethod
    async def anonymise(s: AsyncSession, contact_ref: uuid.UUID) -> list[uuid.UUID]:
        """Retention (T20): drop transcripts and visitors of the contact; returns deal refs whose
        checkpoint threads must also be deleted."""
        convs = (
            await s.execute(
                select(Conversation.id, Conversation.deal_ref).where(Conversation.contact_ref == contact_ref)
            )
        ).all()
        ids = [c.id for c in convs]
        if ids:
            await s.execute(delete(Turn).where(Turn.conversation_id.in_(ids)))
            await s.execute(
                update(Conversation)
                .where(Conversation.id.in_(ids))
                .values(summary=None, contact_ref=None, visitor_id=None)
            )
        await s.execute(delete(Visitor).where(Visitor.contact_ref == contact_ref))
        return sorted({c.deal_ref for c in convs if c.deal_ref is not None})


def psycopg_conninfo(dsn: str) -> str:
    """SQLAlchemy URL ('postgresql+asyncpg://...') -> libpq conninfo for psycopg."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", dsn)


@asynccontextmanager
async def open_checkpointer(
    dsn: str, *, max_size: int = 10, setup: bool = True
) -> AsyncIterator[AsyncPostgresSaver]:
    """AsyncPostgresSaver on a pooled psycopg connection set, tables in the `agents` schema.
    Call once per process (app lifespan / worker startup)."""
    pool: AsyncConnectionPool = AsyncConnectionPool(
        psycopg_conninfo(dsn),
        max_size=max_size,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": "-c search_path=agents,public",
        },
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        if setup:
            await saver.setup()
        yield saver
    finally:
        await pool.close()
