"""App-side implementations of package Protocols that need more than one package or an external service."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx
import orjson
from sqlalchemy import text

from kx.agents.handlers import ReplyAddress
from kx.agents.knowledge import KbSource
from kx.agents.voice import VoiceRoute
from kx.core.db import Databases
from kx.core.settings import LogicalDb

_ADDRESS_SQL = """
    SELECT ca.channel, t.external_key, t.contact_ref
    FROM automation.threads t JOIN automation.channel_accounts ca ON ca.id = t.channel_account_id
    WHERE t.{column} = :ref AND ca.channel IN ('whatsapp', 'email')
    ORDER BY t.last_inbound_at DESC NULLS LAST LIMIT 1
"""
_THREAD_SQL = text(_ADDRESS_SQL.format(column="id"))
_DEAL_SQL = text(_ADDRESS_SQL.format(column="deal_ref"))


class ThreadAddressBook:
    """kx.agents ReplyAddressBook over automation.threads: agents never see raw addresses, the app
    resolves them here, from the automation DB, when a reply is about to be queued."""

    def __init__(self, databases: Databases) -> None:
        self._db = databases

    async def _one(self, sql: Any, ref: uuid.UUID) -> ReplyAddress | None:
        async with self._db.transaction(LogicalDb.AUTOMATION) as s:
            row = (await s.execute(sql, {"ref": ref})).first()
        if row is None:
            return None
        channel = cast("Literal['whatsapp', 'email']", row[0])
        return ReplyAddress(channel=channel, address=row[1], contact_ref=row[2])

    async def for_thread(self, thread_ref: uuid.UUID) -> ReplyAddress | None:
        return await self._one(_THREAD_SQL, thread_ref)

    async def for_deal(self, deal_ref: uuid.UUID) -> ReplyAddress | None:
        return await self._one(_DEAL_SQL, deal_ref)


class BedrockTitanEmbedder:
    """1024-dim embeddings (Titan Text Embeddings v2) via the shared aioboto3 bedrock-runtime client.
    Normalised vectors so pgvector cosine distance == 1 - dot product."""

    dimensions = 1024

    def __init__(self, client: Any, model_id: str, concurrency: int = 8) -> None:
        self._client = client
        self.model = model_id  # recorded on kb_chunks so a model change triggers re-embedding
        self._sem = asyncio.Semaphore(concurrency)

    async def _one(self, text_: str) -> list[float]:
        body = orjson.dumps({"inputText": text_[:30000], "dimensions": self.dimensions, "normalize": True})
        async with self._sem:
            resp = await self._client.invoke_model(
                modelId=self.model, body=body, contentType="application/json", accept="application/json"
            )
            payload = orjson.loads(await resp["body"].read())
        return cast("list[float]", payload["embedding"])

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return list(await asyncio.gather(*(self._one(t) for t in texts)))

    async def embed_query(self, text_: str) -> list[float]:
        return await self._one(text_)


class PayloadContentSource:
    """kx.agents ContentSource over the Payload CMS REST API (only published documents are indexed)."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str | None) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"users API-Key {api_key}"} if api_key else {}

    async def fetch(self, collection: str, cms_id: str, language: str) -> KbSource | None:
        r = await self._http.get(
            f"{self._base}/api/{collection}/{cms_id}",
            params={"locale": language, "depth": 0},
            headers=self._headers,
        )
        if r.status_code == httpx.codes.NOT_FOUND:
            return None
        r.raise_for_status()
        doc = r.json()
        if doc.get("_status", "published") != "published":
            return None
        updated = doc.get("updatedAt")
        return KbSource(
            collection=collection,
            cms_id=cms_id,
            cms_version=str(doc.get("version") or updated or ""),
            language=language,
            title=str(doc.get("title") or ""),
            body=str(doc.get("plainText") or doc.get("body") or ""),
            approved_at=datetime.fromisoformat(updated) if updated else datetime.now(UTC),
            url_path=doc.get("slug"),
        )


class LiveKitPlugins:
    """kx.agents VoicePlugins: picks the LiveKit plugin per vendor. Plugins are optional installs
    (livekit-plugins-elevenlabs / -sarvam / -azure / -silero); a missing one fails the voice worker
    at start-up with a clear ImportError rather than mid-call."""

    def stt(self, route: VoiceRoute) -> Any:
        return self._plugin(route.stt).STT(language=route.language)

    def tts(self, route: VoiceRoute) -> Any:
        mod = self._plugin(route.tts)
        return mod.TTS(voice_id=route.voice_id) if route.voice_id else mod.TTS()

    def vad(self) -> Any:
        from livekit.plugins import silero  # noqa: PLC0415 - optional extra

        return silero.VAD.load()

    @staticmethod
    def _plugin(vendor: str) -> Any:
        import importlib  # noqa: PLC0415

        return importlib.import_module(f"livekit.plugins.{vendor}")
