"""App-side implementations of package Protocols that need more than one package or an external service."""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

import httpx
from sqlalchemy import text

from kx.agents.handlers import ReplyAddress
from kx.agents.knowledge import KbSource
from kx.agents.voice import VoiceRoute
from kx.core.db import Databases
from kx.core.errors import Unavailable
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb

log = get_logger(__name__)

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


# Qwen3-Embedding model card: queries carry a one-line task instruction, documents do not.
QWEN3_QUERY_INSTRUCTION = "Given a web search query, retrieve relevant passages that answer the query"


class HttpEmbedder:
    """D28: Qwen3-Embedding-0.6B in our UAE container, over the OpenAI-compatible `POST /v1/embeddings`
    (OPEN_QUESTIONS #53). 1,024 dimensions (`kb_chunks.vector(1024)` stays); vectors are L2-normalised so
    pgvector cosine distance == 1 - dot product. The model name is stored on every chunk: a model change
    means re-indexing."""

    dimensions = 1024

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 30.0,
        query_instruction: str = QWEN3_QUERY_INSTRUCTION,
        batch: int = 32,
    ) -> None:
        self._http = http
        self._url = base_url.rstrip("/") + "/v1/embeddings"
        self.model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout_seconds
        self._instruction = query_instruction
        self._batch = batch

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        r = await self._http.post(
            self._url,
            json={"model": self.model, "input": list(texts)},
            headers=self._headers,
            timeout=self._timeout,
        )
        r.raise_for_status()
        rows = sorted(r.json()["data"], key=lambda d: int(d["index"]))
        vectors = [_normalise([float(x) for x in row["embedding"]]) for row in rows]
        if any(len(v) != self.dimensions for v in vectors):
            raise ValueError(f"{self.model} returned vectors that are not {self.dimensions}-dimensional")
        return vectors

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            out += await self._embed(texts[i : i + self._batch])
        return out

    async def embed_query(self, text_: str) -> list[float]:
        return (await self._embed([f"Instruct: {self._instruction}\nQuery:{text_}"]))[0]


def _normalise(v: list[float]) -> list[float]:
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v] if norm else v


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
    """kx.agents VoicePlugins (D36). Each field gets its primary and backup wrapped in LiveKit's fallback
    adapter, so a vendor outage switches mid-call (Fallback plan: Deepgram -> Soniox when no transcript comes
    3 s after speech ends; ElevenLabs -> Google Chirp 3 HD when no audio comes 1.5 s after the text). The
    vendor plugins (livekit-plugins-deepgram / -soniox / -elevenlabs / -google / -silero) are installed in the
    voice image only and read their keys from the environment (DEEPGRAM_API_KEY, SONIOX_API_KEY,
    ELEVEN_API_KEY, GOOGLE_APPLICATION_CREDENTIALS). A missing backup plugin is logged and skipped; a missing
    primary fails the worker at start-up rather than mid-call. Not exercised live yet (DELIVERY.md)."""

    def stt(self, route: VoiceRoute) -> Any:
        from livekit.agents import stt as lk_stt  # noqa: PLC0415 - optional extra

        engines = [e for e in (self._stt(v, route.language) for v in route.stt) if e is not None]
        return engines[0] if len(engines) == 1 else lk_stt.FallbackAdapter(engines)

    def tts(self, route: VoiceRoute) -> Any:
        from livekit.agents import tts as lk_tts  # noqa: PLC0415 - optional extra

        engines = [e for e in (self._tts(v, route) for v in route.tts) if e is not None]
        return engines[0] if len(engines) == 1 else lk_tts.FallbackAdapter(engines)

    def vad(self) -> Any:
        from livekit.plugins import silero  # noqa: PLC0415 - optional extra

        return silero.VAD.load()

    def _stt(self, vendor: str, language: str) -> Any:
        mod = self._plugin(vendor, primary=vendor == "deepgram")
        if mod is None:
            return None
        if vendor == "deepgram":  # Nova-3 Multilingual (en, ar, hi, ta, zh; no Sinhala)
            return mod.STT(model="nova-3", language="multi")
        return mod.STT()

    def _tts(self, vendor: str, route: VoiceRoute) -> Any:
        mod = self._plugin(vendor, primary=vendor == "elevenlabs")
        if mod is None:
            return None
        if vendor == "elevenlabs":  # Flash: the low-latency model the Recommendation prices
            return (
                mod.TTS(model="eleven_flash_v2_5", voice_id=route.voice_id)
                if route.voice_id
                else mod.TTS(model="eleven_flash_v2_5")
            )
        return mod.TTS()

    @staticmethod
    def _plugin(vendor: str, *, primary: bool) -> Any:
        import importlib  # noqa: PLC0415

        try:
            return importlib.import_module(f"livekit.plugins.{vendor}")
        except ImportError:
            if primary:
                raise
            log.warning("voice.backup_plugin_missing", vendor=vendor)
            return None


def write_google_credentials(raw_json: str | None, path: str = "/tmp/google-credentials.json") -> None:  # noqa: S108
    """The Chirp backup reads GOOGLE_APPLICATION_CREDENTIALS (a file); infra passes the JSON in
    KX_APP_GOOGLE_CREDENTIALS_JSON, so the voice worker writes it to /tmp (the writable task volume)."""
    import os  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    if not raw_json or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return
    pathlib.Path(path).write_text(raw_json, encoding="utf-8")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path


class PlainMailer(Protocol):
    async def send(self, *, to_address: str, subject: str, body: str) -> str: ...


class StaffCodeMailer:
    """kx.core StaffMailer (D26): a sign-in or recovery code to a staff work address, straight through the
    email field's vendors in order (SES, then Postmark). Never through the buyer outbox, so a code is never
    stored in a message table. Every vendor failing is a 503 for the login screen ("use your
    authenticator")."""

    def __init__(self, senders: Sequence[PlainMailer]) -> None:
        self._senders = tuple(senders)

    async def send_code(self, to_email: str, subject: str, body: str) -> None:
        for index, sender in enumerate(self._senders):
            try:
                await sender.send(to_address=to_email, subject=subject, body=body)
            except Exception as exc:
                log.warning("staff_code_mail_failed", vendor_index=index, error=type(exc).__name__)
                continue
            if index:
                log.warning("vendor_switch", field="email", purpose="staff_code", vendor_index=index)
            return
        raise Unavailable("the sign-in code could not be emailed; use the authenticator app")


TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class TurnstileVerifier:
    """kx.agents.website.HumanCheck over Cloudflare Turnstile's server-side check (D39)."""

    def __init__(self, http: httpx.AsyncClient, secret: str) -> None:
        self._http = http
        self._secret = secret

    async def verify(self, token: str, remote_ip: str | None) -> bool:
        if not token:
            return False
        data = {"secret": self._secret, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        try:
            r = await self._http.post(TURNSTILE_URL, data=data, timeout=5.0)
            r.raise_for_status()
        except httpx.HTTPError:
            log.warning("turnstile.unreachable")
            return False  # fail closed: the public AI costs money (D34, D39)
        return bool(r.json().get("success"))


class PublicLimiter:
    """kx.agents.website.PublicLimits over automation's Redis token buckets (D39 app rate limits). Values per
    OPEN_QUESTIONS #61 until Arifa sets them: 30 requests per minute per IP, 12 per minute per session."""

    def __init__(self, limiter: Any, buckets: dict[str, Any]) -> None:
        self._limiter = limiter
        self._buckets = buckets

    async def allow(self, scope: Literal["ip", "session"], key: str) -> bool:
        decision = await self._limiter.take(f"public_{scope}", key, self._buckets[scope])
        return bool(decision.allowed)


class VendorOrder:
    """D30: each field's vendor order from Parameter Store (`<prefix>/<field>`, a StringList such as
    "ses,postmark"), cached 60 s so an admin edit takes effect without a deploy. Forcing a vendor off = leave
    it out of the list. Unreadable or empty -> the documented default order."""

    def __init__(
        self, ssm: Any, prefix: str | None, defaults: dict[str, tuple[str, ...]], ttl_seconds: float = 60.0
    ) -> None:
        self._ssm = ssm
        self._prefix = (prefix or "").rstrip("/")
        self._defaults = defaults
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, tuple[str, ...]]] = {}

    async def order(self, field: str) -> tuple[str, ...]:
        default = self._defaults.get(field, ())
        if not self._prefix:
            return default
        cached = self._cache.get(field)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        try:
            resp = await self._ssm.get_parameter(Name=f"{self._prefix}/{field}")
            value = tuple(v.strip() for v in str(resp["Parameter"]["Value"]).split(",") if v.strip())
        except Exception:
            log.warning("vendor_order.unreadable", field=field)
            value = ()
        result = value or default
        self._cache[field] = (now, result)
        return result
