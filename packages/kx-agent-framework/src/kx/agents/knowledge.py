"""Knowledge base over approved CMS content (products, machines, FAQs, certifications).

Search is hybrid in ONE SQL statement: pgvector cosine (HNSW), `simple` tsvector (works for every
language, no stemming surprises) and trigram word-similarity (SKUs, typos), fused by Reciprocal Rank
Fusion. When nothing matches well the caller logs a knowledge gap so the team can add content
instead of the AI guessing manufacturing facts.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol, runtime_checkable

from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.dialects.postgresql import insert

from kx.agents.orm import KbChunk, KbDocument, KnowledgeGap
from kx.core.db import Databases
from kx.core.identity import sha256_hex
from kx.core.settings import LogicalDb

EMBEDDING_DIM: Final = 1024


@runtime_checkable
class Embedder(Protocol):
    """1024-dim embeddings (app wires the provider, e.g. Titan v2 / Cohere multilingual on Bedrock)."""

    model: str

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True, slots=True)
class KbHit:
    chunk_id: uuid.UUID
    text: str
    title: str
    url_path: str | None
    score: float  # RRF score
    similarity: float  # cosine similarity of this chunk (0 when not a vector candidate)


@dataclass(frozen=True, slots=True)
class KbResult:
    hits: tuple[KbHit, ...]
    weak: bool  # nothing relevant: answer "the team will confirm" and log a gap


@runtime_checkable
class KnowledgeSearch(Protocol):
    async def search(self, query: str, language: str, limit: int = 5) -> KbResult: ...
    async def record_gap(self, question: str, language: str, conversation_id: uuid.UUID | None) -> None: ...


_RRF_SQL: Final = text("""
WITH docs AS (SELECT id, title, url_path FROM kb_documents WHERE language = ANY(:langs)),
vec AS (
    SELECT c.id, 1 - (c.embedding <=> :qv) AS sim,
           row_number() OVER (ORDER BY c.embedding <=> :qv) AS r
    FROM kb_chunks c JOIN docs d ON d.id = c.document_id
    ORDER BY c.embedding <=> :qv LIMIT :n),
fts AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q) DESC) AS r
    FROM kb_chunks c JOIN docs d ON d.id = c.document_id, websearch_to_tsquery('simple', :q) q
    WHERE c.tsv @@ q ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT :n),
tri AS (
    SELECT c.id, row_number() OVER (ORDER BY word_similarity(:q, c.text) DESC) AS r
    FROM kb_chunks c JOIN docs d ON d.id = c.document_id
    WHERE :q <% c.text ORDER BY word_similarity(:q, c.text) DESC LIMIT :n),
fused AS (
    SELECT id, sum(1.0 / (:k + r)) AS score, max(sim) AS sim, count(*) FILTER (WHERE src <> 'vec') AS lexical
    FROM (SELECT id, r, sim, 'vec' AS src FROM vec
          UNION ALL SELECT id, r, NULL, 'fts' FROM fts
          UNION ALL SELECT id, r, NULL, 'tri' FROM tri) u
    GROUP BY id)
SELECT f.id, c.text, d.title, d.url_path, f.score, coalesce(f.sim, 0) AS sim, f.lexical
FROM fused f JOIN kb_chunks c ON c.id = f.id JOIN docs d ON d.id = c.document_id
ORDER BY f.score DESC, f.id LIMIT :limit
""").bindparams(bindparam("qv", type_=Vector(EMBEDDING_DIM)))


class KnowledgeBase:
    def __init__(
        self,
        databases: Databases,
        embedder: Embedder,
        *,
        rrf_k: int = 60,
        candidates: int = 40,
        min_similarity: float = 0.35,
    ) -> None:
        self._db = databases
        self._embedder = embedder
        self._k = rrf_k
        self._n = candidates
        self._min_sim = min_similarity

    async def search(self, query: str, language: str, limit: int = 5) -> KbResult:
        qv = await self._embedder.embed_query(query)
        async with self._db.transaction(LogicalDb.AGENTS) as s:
            rows = (
                await s.execute(
                    _RRF_SQL,
                    {
                        "qv": qv,
                        "q": query,
                        "langs": sorted({language, "en"}),
                        "n": self._n,
                        "k": self._k,
                        "limit": limit,
                    },
                )
            ).all()
        hits = tuple(KbHit(r.id, r.text, r.title, r.url_path, float(r.score), float(r.sim)) for r in rows)
        weak = not rows or (float(rows[0].sim) < self._min_sim and not rows[0].lexical)
        return KbResult(hits, weak)

    async def record_gap(self, question: str, language: str, conversation_id: uuid.UUID | None) -> None:
        async with self._db.transaction(LogicalDb.AGENTS) as s:
            s.add(KnowledgeGap(question=question[:2000], language=language, conversation_id=conversation_id))


# ---------------------------------------------------------------------------------- ingestion
@dataclass(frozen=True, slots=True)
class KbSource:
    """One approved CMS document, as fetched by the app's CMS client."""

    collection: str
    cms_id: str
    cms_version: str
    language: str
    title: str
    body: str
    approved_at: datetime
    url_path: str | None = None
    metadata: dict[str, Any] | None = None


@runtime_checkable
class ContentSource(Protocol):
    """App-side Payload CMS reader. None = no longer published (the document is removed)."""

    async def fetch(self, collection: str, cms_id: str, language: str) -> KbSource | None: ...


_CJK: Final = re.compile(r"[㐀-鿿஀-௿඀-෿ऀ-ॿ]")
_SENTENCE: Final = re.compile(r"(?<=[.!?。！？؟।])\s+|\n{2,}")  # noqa: RUF001 - CJK punctuation


def estimate_tokens(chunk: str) -> int:
    """~4 chars/token for Latin/Arabic, ~1 token per CJK/Indic char (Claude-tokenizer ballpark)."""
    dense = len(_CJK.findall(chunk))
    return dense + (len(chunk) - dense + 3) // 4


def chunk_text(body: str, max_tokens: int = 500, overlap: float = 0.15) -> list[str]:
    """Sentence-packed chunks of ~max_tokens with ~15% trailing-sentence overlap."""
    sentences = [s.strip() for s in _SENTENCE.split(body) if s.strip()]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for sent in sentences:
        n = estimate_tokens(sent)
        if current and size + n > max_tokens:
            chunks.append(" ".join(current))
            keep: list[str] = []
            kept = 0
            for prev in reversed(current):
                kept += estimate_tokens(prev)
                if kept > max_tokens * overlap:
                    break
                keep.insert(0, prev)
            current, size = keep, sum(estimate_tokens(x) for x in keep)
        current.append(sent)
        size += n
    if current:
        chunks.append(" ".join(current))
    return chunks


class KnowledgeIndexer:
    """KB_CONTENT_PUBLISHED -> fetch, chunk, embed, replace. Unchanged content is skipped."""

    def __init__(self, databases: Databases, embedder: Embedder, source: ContentSource) -> None:
        self._db = databases
        self._embedder = embedder
        self._source = source

    async def reindex(self, collection: str, cms_id: str, language: str) -> int:
        doc = await self._source.fetch(collection, cms_id, language)
        async with self._db.transaction(LogicalDb.AGENTS) as s:
            if doc is None:
                await s.execute(
                    delete(KbDocument).where(
                        KbDocument.cms_collection == collection,
                        KbDocument.cms_id == cms_id,
                        KbDocument.language == language,
                    )
                )
                return 0
            sha = sha256_hex(f"{doc.title}\n{doc.body}".encode())
            current = (
                await s.execute(
                    select(KbDocument.content_sha256).where(
                        KbDocument.cms_collection == collection,
                        KbDocument.cms_id == cms_id,
                        KbDocument.language == language,
                    )
                )
            ).scalar_one_or_none()
            if current == sha:
                return 0
        pieces = chunk_text(doc.body)
        vectors = await self._embedder.embed_documents(pieces)  # outside the transaction (network call)
        async with self._db.transaction(LogicalDb.AGENTS) as s:
            values = {
                "cms_collection": collection,
                "cms_id": cms_id,
                "language": language,
                "cms_version": doc.cms_version,
                "title": doc.title,
                "url_path": doc.url_path,
                "content_sha256": sha,
                "approved_at": doc.approved_at,
            }
            doc_id = (
                await s.execute(
                    insert(KbDocument)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["cms_collection", "cms_id", "language"],
                        set_={**values, "indexed_at": text("now()")},
                    )
                    .returning(KbDocument.id)
                )
            ).scalar_one()
            await s.execute(delete(KbChunk).where(KbChunk.document_id == doc_id))
            s.add_all(
                KbChunk(
                    document_id=doc_id,
                    chunk_no=i,
                    text=t,
                    metadata_=doc.metadata or {},
                    embedding=v,
                    embedding_model=self._embedder.model,
                )
                for i, (t, v) in enumerate(zip(pieces, vectors, strict=True))
            )
        return len(pieces)
