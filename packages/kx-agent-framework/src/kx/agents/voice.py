"""Voice: speech vendor routing by language + a LiveKit Agents worker entrypoint.

Routing (decided 4 Nov): ElevenLabs Scribe/Flash for en, ar, zh; Sarvam for hi, ta; Azure for si.
The worker runs the SAME AgentService turn as chat (same graphs, same price guard) and re-checks
the reply against the deal's allowed prices right before TTS: speech cannot be recalled. The
recording notice always plays first (UAE law, T-law decisions). livekit-agents is an optional extra.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from kx.agents.guard import PriceGuard
from kx.agents.language import DEFAULT_LANGUAGE, normalise_language
from kx.agents.registry import Phrase, PromptStore
from kx.agents.service import AgentService, InboundTurn
from kx.core.contracts import CrmGateway

Vendor = Literal["elevenlabs", "sarvam", "azure"]


@dataclass(frozen=True, slots=True)
class VoiceRoute:
    language: str
    stt: Vendor
    tts: Vendor
    voice_id: str | None = None


DEFAULT_VOICE_MAP: Final[dict[str, VoiceRoute]] = {
    "en": VoiceRoute("en", "elevenlabs", "elevenlabs"),
    "ar": VoiceRoute("ar", "elevenlabs", "elevenlabs"),
    "zh": VoiceRoute("zh", "elevenlabs", "elevenlabs"),
    "hi": VoiceRoute("hi", "sarvam", "sarvam"),
    "ta": VoiceRoute("ta", "sarvam", "sarvam"),
    "si": VoiceRoute("si", "azure", "azure"),
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


class VoicePlugins(Protocol):
    """App-supplied factory for LiveKit STT/TTS/VAD plugin objects (vendor SDKs are app deps)."""

    def stt(self, route: VoiceRoute) -> Any: ...
    def tts(self, route: VoiceRoute) -> Any: ...
    def vad(self) -> Any: ...


def make_livekit_entrypoint(
    service: AgentService,
    plugins: VoicePlugins,
    router: VoiceRouter | None = None,
) -> Callable[[Any], Any]:
    """Entrypoint for `livekit.agents.cli.run_app(WorkerOptions(entrypoint_fnc=...))`.
    Participant metadata must carry `deal_ref` and `language` (set by the token endpoint)."""
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

        await ctx.connect()
        participant = await ctx.wait_for_participant()
        meta = json.loads(participant.metadata or "{}")
        route = voice_router.route(meta.get("language"))
        session: Any = lk.AgentSession(stt=plugins.stt(route), tts=plugins.tts(route), vad=plugins.vad())
        await session.start(
            room=ctx.room, agent=KairoxVoiceAgent(uuid.UUID(meta["deal_ref"]), route.language, ctx.room.name)
        )

    return entrypoint
