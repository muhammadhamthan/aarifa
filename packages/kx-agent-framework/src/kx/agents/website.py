"""§6 website conversation API (11 Nov plan WP19; D39 public-endpoint protection).

`/v1/public/...`, called by the Next.js site (no staff login):

* `POST /sessions`: start a conversation after a Cloudflare Turnstile check. Pressing "Enter the factory" is
  the visitor's consent to voice (§6 step 1). Returns a short-lived session token (send it as
  `Authorization: Bearer`) and whether live voice exists for the language (Sinhala is text-only, D36).
* `POST /sessions/{id}/messages`: one chat turn through the same `AgentService` as WhatsApp and email (same
  router, graphs and price guard). The CRM deal is created on the first message, not when the page opens.
* `POST /sessions/{id}/voice`: a LiveKit room token; its participant metadata carries `deal_ref` and
  `language`, which is what the voice worker reads.
* `POST /sessions/{id}/identify`: the phone or email the visitor typed. "Returning visitors are recognised by
  phone or email after they identify themselves. They are never recognised silently" (§6 step 7): the CRM
  merges the records; the answer is the same whether or not the buyer was known.

Every call is rate-limited per IP and per session; messages and voice are refused once the daily AI spend
cap is reached (D34), and the site then shows WhatsApp click-to-chat (the documented last resort).
"""

# No `from __future__ import annotations` here: FastAPI resolves the closure-local annotations.

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal, Protocol

import jwt
from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field

from kx.agents.language import normalise_language
from kx.agents.orm import Visitor
from kx.agents.service import AgentService, InboundTurn
from kx.agents.voice import VoiceRouter
from kx.core.contracts import AiRole, ContactPatch, CrmGateway
from kx.core.db import Databases
from kx.core.errors import Forbidden, RuleViolation, TooManyRequests, Unauthorized, Unavailable
from kx.core.logging import get_logger
from kx.core.settings import LogicalDb
from kx.core.signed import sign, verify

log = get_logger(__name__)
TRIGGER: Final = "website.message"
# the visitor typed it into the site; the CRM records the change under the contact-capture role
WEBSITE_ROLE: Final = AiRole.CONTACT_CAPTURE
Clock = Callable[[], datetime]


class HumanCheck(Protocol):
    """Cloudflare Turnstile (D39): the token the widget produced, checked server-side."""

    async def verify(self, token: str, remote_ip: str | None) -> bool: ...


class PublicLimits(Protocol):
    async def allow(self, scope: Literal["ip", "session"], key: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class LiveKitConfig:
    url: str
    api_key: str
    api_secret: str
    token_minutes: int = 30


@dataclass
class WebsiteDeps:
    databases: Databases
    service: AgentService
    gateway: CrmGateway
    secret: str  # KX_SERVICE_TOKEN_SECRET
    human_check: HumanCheck | None  # None only for local development (no Turnstile key)
    limits: PublicLimits
    ai_budget_left: Callable[[], Awaitable[bool]]  # D34 daily AI spend cap
    # D34 voice-minute cap and D35 kill switch at `text_only`: False -> the site uses chat instead
    voice_allowed: Callable[[], Awaitable[bool]] | None = None
    voice: VoiceRouter = field(default_factory=VoiceRouter)
    livekit: LiveKitConfig | None = None
    session_hours: int = 2
    clock: Clock = field(default=lambda: datetime.now(UTC))


# ---------------------------------------------------------------- request / response models
class SessionIn(BaseModel):
    turnstile_token: str = Field(default="", max_length=4096)
    language: str | None = Field(default=None, max_length=16)  # the site's or the browser's language
    voice_consent: bool = False  # "Enter the factory" pressed (§6 step 1); voice is refused without it
    cookie_consent: bool = False  # the site's cookie banner answer
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")


class SessionOut(BaseModel):
    session_id: uuid.UUID
    session_token: str
    expires_at: datetime
    language: str
    voice_available: bool  # false for Sinhala (text only) or when live voice is not configured


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    language: str | None = Field(default=None, max_length=16)  # the visitor switched language (§6 step 3)


class MessageOut(BaseModel):
    reply: str | None  # None: the conversation is with a salesperson now; nothing to show
    language: str
    handed_over: bool  # the sales team takes over (show "you'll receive it on WhatsApp shortly")


class VoiceOut(BaseModel):
    livekit_url: str
    token: str
    room: str
    language: str
    expires_at: datetime


class IdentifyIn(BaseModel):
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=320)
    name: str | None = Field(default=None, max_length=200)


class IdentifyOut(BaseModel):
    ok: bool = True  # same answer whether or not the buyer was known (§6 step 7)


# ---------------------------------------------------------------- helpers
def livekit_token(
    cfg: LiveKitConfig, *, identity: str, room: str, metadata: dict[str, str], now: datetime
) -> tuple[str, datetime]:
    """A LiveKit access token (HS256 JWT, LiveKit's documented claims): join this one room only."""
    expires = now + timedelta(minutes=cfg.token_minutes)
    claims: dict[str, Any] = {
        "iss": cfg.api_key,
        "sub": identity,
        "nbf": int(now.timestamp()) - 5,
        "exp": int(expires.timestamp()),
        "name": "Website visitor",
        "metadata": json.dumps(metadata, separators=(",", ":")),  # read by the voice worker
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    return jwt.encode(claims, cfg.api_secret, algorithm="HS256"), expires


def _ip(request: Request) -> str:
    """The visitor's address for the per-IP limit (D39). The AWS load balancer appends the address it saw
    as the LAST X-Forwarded-For entry; earlier entries are whatever the client sent: never trusted."""
    forwarded = [
        part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()
    ]
    return forwarded[-1] if forwarded else (request.client.host if request.client else "unknown")


def build_router(deps: WebsiteDeps) -> APIRouter:  # noqa: PLR0915 - one small handler per step
    r = APIRouter(prefix="/v1/public", tags=["website"])

    async def limit(scope: Literal["ip", "session"], key: str) -> None:
        if not await deps.limits.allow(scope, key):
            raise TooManyRequests("too many requests; slow down")

    def claims_for(session_id: uuid.UUID, authorization: str | None) -> dict[str, Any]:
        token = (authorization or "").removeprefix("Bearer ").strip()
        claims = verify(deps.secret, token, now=deps.clock()) if token else None
        if claims is None:
            raise Unauthorized("session expired; start again")
        if claims.get("sid") != str(session_id):
            raise Forbidden("this token belongs to another session")
        return claims

    async def deal_of(session_id: uuid.UUID) -> uuid.UUID:
        """The visitor's open deal (created on first need; the CRM keys it on the visitor id)."""
        return await deps.gateway.resolve_deal("web_chat", None, session_id)

    @r.post("/sessions", response_model=SessionOut, status_code=201)
    async def start(body: SessionIn, request: Request) -> SessionOut:
        """§6 step 1: start a website conversation (Turnstile-checked)."""
        ip = _ip(request)
        await limit("ip", ip)
        if deps.human_check is not None and not await deps.human_check.verify(body.turnstile_token, ip):
            raise Forbidden("the human check failed; reload the page")
        language = normalise_language(body.language)
        now = deps.clock()
        expires = now + timedelta(hours=deps.session_hours)
        async with deps.databases.transaction(LogicalDb.AGENTS) as s:
            visitor = Visitor(
                cookie_consent=body.cookie_consent,
                voice_consent_at=now if body.voice_consent else None,  # OPEN_QUESTIONS #57
                country_code=body.country_code,
                language=language,
            )
            s.add(visitor)
            await s.flush()
            session_id = visitor.id
        route = deps.voice.route(language)
        token = sign(
            deps.secret,
            {"sid": str(session_id), "lang": language, "vc": body.voice_consent},
            expires_at=expires,
        )
        return SessionOut(
            session_id=session_id,
            session_token=token,
            expires_at=expires,
            language=language,
            voice_available=route.voice_available and deps.livekit is not None,
        )

    @r.post("/sessions/{session_id}/messages", response_model=MessageOut)
    async def message(
        session_id: uuid.UUID,
        body: MessageIn,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> MessageOut:
        """One chat turn (§6 steps 2-6) through the same agents, guard and router as WhatsApp."""
        claims = claims_for(session_id, authorization)
        await limit("ip", _ip(request))
        await limit("session", str(session_id))
        if not await deps.ai_budget_left():
            raise Unavailable("the assistant is busy; please continue on WhatsApp")
        deal_id = await deal_of(session_id)
        started = time.perf_counter()
        result = await deps.service.handle_turn(
            InboundTurn(
                channel="web_chat",
                text=body.text,
                deal_id=deal_id,
                thread_key=f"web_chat:{session_id}",
                trigger_event=TRIGGER,
                trigger_event_id=uuid.uuid7(),
                language=body.language or claims.get("lang"),
            )
        )
        log.info("website.turn", ms=int((time.perf_counter() - started) * 1000), status=result.status)
        return MessageOut(
            reply=result.reply,
            language=result.language,
            handed_over=result.status in ("handoff", "acknowledged", "silent"),
        )

    @r.post("/sessions/{session_id}/voice", response_model=VoiceOut)
    async def voice(
        session_id: uuid.UUID,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> VoiceOut:
        """A LiveKit room token for live voice (participant metadata: deal_ref, language)."""
        claims = claims_for(session_id, authorization)
        await limit("ip", _ip(request))
        await limit("session", str(session_id))
        if not claims.get("vc"):
            raise RuleViolation("press Enter the factory to start voice", reason="no_voice_consent")
        route = deps.voice.route(claims.get("lang"))
        if deps.livekit is None or not route.voice_available:
            raise RuleViolation(
                "live voice is not available in this language; use the chat", reason="text_only"
            )
        if not await deps.ai_budget_left() or (
            deps.voice_allowed is not None and not await deps.voice_allowed()
        ):
            raise Unavailable("voice is busy; please use the chat")
        deal_id = await deal_of(session_id)
        room = f"web-{session_id}"
        token, expires = livekit_token(
            deps.livekit,
            identity=f"visitor-{session_id}",
            room=room,
            metadata={"deal_ref": str(deal_id), "language": route.language},
            now=deps.clock(),
        )
        return VoiceOut(
            livekit_url=deps.livekit.url, token=token, room=room, language=route.language, expires_at=expires
        )

    @r.post("/sessions/{session_id}/identify", response_model=IdentifyOut)
    async def identify(
        session_id: uuid.UUID,
        body: IdentifyIn,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> IdentifyOut:
        """§6 step 7: the visitor typed their phone or email; a returning buyer is recognised now, never
        before. The answer does not say whether the buyer was known."""
        claims_for(session_id, authorization)
        await limit("ip", _ip(request))
        await limit("session", str(session_id))
        if not (body.phone or body.email):
            raise RuleViolation("give a phone number or an email", reason="empty")
        deal_id = await deal_of(session_id)
        try:
            await deps.gateway.set_contact(
                deal_id, ContactPatch(phone=body.phone, email=body.email, name=body.name), WEBSITE_ROLE
            )
        except ValueError as exc:
            raise RuleViolation("that phone number or email does not look right", reason="invalid") from exc
        return IdentifyOut()

    return r
