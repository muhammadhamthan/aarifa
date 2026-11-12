"""Voice: speech vendor routing by language + a LiveKit Agents worker entrypoint.

Routing (11 Nov, D36; platform docs Recommendation + Fallback plan): Deepgram Nova-3 Multilingual STT with
Soniox as backup, ElevenLabs Flash TTS with Google Chirp 3 HD as backup, for en/ar/hi/ta/zh. "No option
covers Sinhala in live voice; Sinhala stays text-only" (Platform contest round 1): the website offers chat.
Last resorts are the website's own: text chat, then WhatsApp click-to-chat.

The worker runs the SAME AgentService turn as chat (same graphs, same price guard) and re-checks the reply
against the deal's allowed prices right before TTS: speech cannot be recalled. The recording notice always
plays first (§6.1). On a call "stop" means "wait" (§8.1 step 10): the agent goes quiet and listens; it ends
the call only on a clear request, and only "stop calling me" / "don't call me" stops calls.
livekit-agents is an optional extra; the vendor plugins are installed with the voice image.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal, Protocol

from kx.agents.guard import PriceGuard
from kx.agents.language import DEFAULT_LANGUAGE, normalise_language
from kx.agents.registry import Phrase, PromptStore
from kx.agents.service import AgentService, InboundTurn
from kx.core.contracts import AiRole, CrmGateway
from kx.core.optout import match_voice

SttVendor = Literal["deepgram", "soniox"]
TtsVendor = Literal["elevenlabs", "google"]
STT_ORDER: Final[tuple[SttVendor, ...]] = ("deepgram", "soniox")  # Fallback plan: Deepgram -> Soniox
TTS_ORDER: Final[tuple[TtsVendor, ...]] = ("elevenlabs", "google")  # ElevenLabs Flash -> Chirp 3 HD


@dataclass(frozen=True, slots=True)
class VoiceRoute:
    language: str
    stt: tuple[SttVendor, ...] = STT_ORDER  # primary first; the app wraps them in a fallback adapter
    tts: tuple[TtsVendor, ...] = TTS_ORDER
    voice_id: str | None = None
    text_only: bool = False  # no live-voice vendor for this language: the website uses chat

    @property
    def voice_available(self) -> bool:
        return not self.text_only and bool(self.stt) and bool(self.tts)


DEFAULT_VOICE_MAP: Final[dict[str, VoiceRoute]] = {
    **{lang: VoiceRoute(lang) for lang in ("en", "ar", "hi", "ta", "zh")},
    "si": VoiceRoute("si", stt=(), tts=(), text_only=True),
}


class VoiceRouter:
    def __init__(
        self, routes: Mapping[str, VoiceRoute] = DEFAULT_VOICE_MAP, default: str = DEFAULT_LANGUAGE
    ) -> None:
        self._routes = dict(routes)
        self._default = default

    def route(self, language: str | None) -> VoiceRoute:
        return self._routes.get(normalise_language(language, self._default)) or self._routes[self._default]


async def speakable(
    text: str, gateway: CrmGateway, deal_id: uuid.UUID, prompts: PromptStore, language: str
) -> str:
    """Last check before TTS against the fresh deal context (CRM adds issued offers to it)."""
    deal = await gateway.deal_context(deal_id)
    guard = PriceGuard(deal.allowed_prices, deal.currency, prompts.phrase(Phrase.PRICE_FALLBACK, language))
    bad, disclosure = guard.violations(text)
    return guard.fallback if bad or disclosure else text


async def voice_stop(service: AgentService, deal_id: uuid.UUID, text: str, language: str) -> str | None:
    """§8.1 step 10. None = an ordinary turn; "" = say nothing and listen; else the fixed line to speak.
    Only a clear "stop calling me" / "don't call me" records a stop, and only for calls (scope phone)."""
    kind = match_voice(text)
    prompts = service.prompts
    match kind:
        case "wait":
            return ""
        case "phone_stop":
            await service.gateway.record_opt_out(
                deal_id,
                scope="phone",
                exact_words=text,
                reason=None,
                decided_by="rule:voice_phone_stop",
                actor=AiRole.COMMUNICATION,
            )
            return prompts.phrase(Phrase.VOICE_GOODBYE, language)
        case "end_call":
            return prompts.phrase(Phrase.VOICE_GOODBYE, language)
        case "unclear":
            return prompts.phrase(Phrase.VOICE_STOP_CHECK, language)
        case _:
            return None


class VoicePlugins(Protocol):
    """App-supplied factory for LiveKit STT/TTS/VAD plugin objects (vendor SDKs are app deps)."""

    def stt(self, route: VoiceRoute) -> Any: ...
    def tts(self, route: VoiceRoute) -> Any: ...
    def vad(self) -> Any: ...


def make_livekit_entrypoint(
    service: AgentService,
    plugins: VoicePlugins,
    router: VoiceRouter | None = None,
    on_minutes: Callable[[Decimal], Awaitable[object]] | None = None,
) -> Callable[[Any], Any]:
    """Entrypoint for `livekit.agents.cli.run_app(WorkerOptions(entrypoint_fnc=...))`.
    Participant metadata must carry `deal_ref` and `language` (set by the token endpoint). `on_minutes`
    receives each call's length in minutes when the room closes (D34 daily voice-minute cap)."""
    from livekit import agents as lk  # noqa: PLC0415 - optional extra: imported only when the voice worker starts

    voice_router = router or VoiceRouter()

    class KairoxVoiceAgent(lk.Agent):
        def __init__(self, deal_id: uuid.UUID, language: str, room: str) -> None:
            super().__init__(instructions="Kairox sales assistant (replies come from the Kairox graphs).")
            self._deal_id, self._language, self._room = deal_id, language, room

        async def on_enter(self) -> None:
            notice = service.prompts.phrase(Phrase.RECORDING_NOTICE, self._language)
            await self.session.say(notice, allow_interruptions=False)

        async def llm_node(self, chat_ctx: Any, tools: Any, model_settings: Any) -> AsyncIterator[str]:
            text = chat_ctx.items[-1].text_content or ""
            if (said := await voice_stop(service, self._deal_id, text, self._language)) is not None:
                if said:  # "" = stay quiet and listen ("stop" means "wait", §8.1 step 10)
                    yield said
                return
            result = await service.handle_turn(
                InboundTurn(
                    channel="web_voice",
                    text=text,
                    deal_id=self._deal_id,
                    thread_key=f"web_voice:{self._room}",
                    trigger_event="voice.utterance",
                    trigger_event_id=uuid.uuid7(),
                    language=self._language,
                )
            )
            if result.reply:
                yield await speakable(
                    result.reply, service.gateway, self._deal_id, service.prompts, result.language
                )

    async def entrypoint(ctx: Any) -> None:
        started = time.monotonic()

        async def count_minutes() -> None:
            if on_minutes is not None:
                await on_minutes(Decimal(str(round((time.monotonic() - started) / 60, 4))))

        ctx.add_shutdown_callback(count_minutes)
        await ctx.connect()
        participant = await ctx.wait_for_participant()
        meta = json.loads(participant.metadata or "{}")
        route = voice_router.route(meta.get("language"))
        if not route.voice_available:  # Sinhala: the token endpoint should not have issued a voice token
            await ctx.room.disconnect()
            return
        session: Any = lk.AgentSession(stt=plugins.stt(route), tts=plugins.tts(route), vad=plugins.vad())
        await session.start(
            room=ctx.room, agent=KairoxVoiceAgent(uuid.UUID(meta["deal_ref"]), route.language, ctx.room.name)
        )

    return entrypoint
