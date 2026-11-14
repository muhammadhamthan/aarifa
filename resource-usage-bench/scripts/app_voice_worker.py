"""The platform's REAL voice worker, run with free speech engines (bench only, no platform file is changed).

This mirrors `arifaa_pack.cli._voice` (the `kx voice` command), with two required changes, both bugs the
unmodified platform has: (1) the job entry function sits at module level (`entry`: the unmodified command cannot
start a job process), (2) the session gets a placeholder LLM (`_session_with_placeholder_llm`: without one
LiveKit never calls the Kairox graphs). Same parts: the app container from
`arifaa_pack.container.build()`, `kx.agents.voice.make_livekit_entrypoint` (Kairox graphs through AgentService,
price guard before TTS, recording notice, voice opt-out, D34 voice-minute counter) and LiveKit's default
`WorkerOptions`. The ONLY difference: the `VoicePlugins` factory. Production's `LiveKitPlugins` builds Deepgram /
ElevenLabs plugins with fixed vendor endpoints; here `FreePlugins` builds the OpenAI-compatible plugins pointed at
`speech_server.py` (faster-whisper + Piper). VoicePlugins is the platform's own seam for exactly this swap.

  python app_voice_worker.py          (inside the app image; env as the app's api/worker + SPEECH_URL)
"""

from __future__ import annotations

import os
import sys
from contextlib import AsyncExitStack
from functools import partial
from typing import Any

from livekit.agents import WorkerOptions, cli
from livekit.plugins import openai, silero

from arifaa_pack.container import build
from kx.agents.voice import VoiceRoute, make_livekit_entrypoint

SPEECH = os.environ.get("SPEECH_URL", "http://host.docker.internal:8100/v1")
PLACEHOLDER_LLM_URL = os.environ.get("PLACEHOLDER_LLM_URL", "http://fakes:9000/v1")


def _session_with_placeholder_llm() -> None:
    """Second required change. kx.agents.voice builds `AgentSession(stt=..., tts=..., vad=...)` with no `llm`;
    livekit-agents 1.8.10 then skips every reply before the agent's `llm_node` runs
    (voice/agent_activity.py:2914 `elif self.llm is None: return  # skip response if no llm is set`), so the
    Kairox graphs are never called and the caller hears only the recording notice. Giving the session any LLM
    object fixes it: KairoxVoiceAgent.llm_node replaces the LLM call, so this placeholder is never used (it points
    at the fake server under the model name "placeholder-never-called"; the fake's counters show whether it was)."""
    from livekit import agents as lk

    original = lk.AgentSession.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("llm", openai.LLM(base_url=PLACEHOLDER_LLM_URL, api_key="bench",
                                            model="placeholder-never-called"))
        original(self, *args, **kwargs)

    lk.AgentSession.__init__ = init  # type: ignore[method-assign]


_session_with_placeholder_llm()  # at import: every job process imports this module
PIPER_VOICES = {"en", "ar", "hi", "zh"}  # Piper has no Tamil voice: Tamil calls fall back to the English voice


class FreePlugins:
    """kx.agents.voice.VoicePlugins with free, self-hosted engines."""

    def stt(self, route: VoiceRoute) -> Any:
        return openai.STT(base_url=SPEECH, api_key="bench", model="whisper-turbo", language=route.language,
                          use_realtime=False)

    def tts(self, route: VoiceRoute) -> Any:
        voice = route.language if route.language in PIPER_VOICES else "en"
        return openai.TTS(base_url=SPEECH, api_key="bench", model="piper", voice=voice, response_format="pcm")

    def vad(self) -> Any:
        return silero.VAD.load()


_STATE: dict[str, Any] = {}


async def entry(ctx: Any) -> None:
    """Same body as `arifaa_pack.cli._voice.<locals>.entry`, but at MODULE level. Required: LiveKit starts each
    job process with multiprocessing "forkserver" on Linux ("spawn" elsewhere; livekit-agents 1.8.10
    worker.py:263), which pickles the entrypoint; a function defined inside `_voice` cannot be pickled, so the
    unmodified `kx voice` fails every process start ("Can't pickle local object _voice.<locals>.entry",
    results/r2b_kx_voice_production.log). Moving it to module level is the whole fix."""
    if "fn" not in _STATE:  # build once per worker process, inside LiveKit's event loop (as cli._voice)
        stack = AsyncExitStack()
        c = await stack.enter_async_context(build())
        minutes = partial(c.spend.add, "voice_minutes")  # D34 daily voice-minute cap
        _STATE.update(stack=stack, fn=make_livekit_entrypoint(c.agent_service, FreePlugins(), on_minutes=minutes))
    await _STATE["fn"](ctx)


def main() -> None:
    if len(sys.argv) == 1:
        sys.argv = [sys.argv[0], "start"]  # `download-files` is passed through to fetch the Silero model
    cli.run_app(WorkerOptions(entrypoint_fnc=entry))


if __name__ == "__main__":
    main()
